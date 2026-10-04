"""OK-1C T1 — EŞİK ÜSTÜ satınalmada zincir, `full` kapısını İKAME EDER (K1).

## Kapatılan üçüncü kapı

İkame kapısı (T2) bir ROUTE bağımlılığıdır ve eşik üstü satınalmada YETMEZ:
oradaki engel route'ta değil SERVİSTEDİR
(`procurement/transitions.py::_assert_approver_level`, `procurement: full`).
PM'in matristeki seviyesi `approve`tır, `full` DEĞİL — yani eşik üstü bir
talebin 2. adımı (Proje Müdürü) BUGÜN GEÇİLEMEZ ve zincir tam da orada durur:

    procurement → project_manager → accounting → patron
                        ▲ 403 APPROVAL_THRESHOLD_EXCEEDED

## Kararın gerekçesi ve SINIRI

Eşiğin kendi mekanizması zincirin **`patron` adımıdır**: tutar eşiği aşınca
zincire dördüncü bir imza EKLENİR. Yani "üst seviye yetki" koşulu zincirli
evrakta ZATEN karşılanmıştır — `full` kapısını ayrıca aramak, mekanizmayı iki
kez uygulamak ve zinciri işletilemez kılmaktır.

🔴 SINIR: kural yalnız **zinciri OLAN** evrak içindir. Zincirsiz (eski kayıt)
bir talepte `_assert_approver_level` BUGÜNKÜ gibi çalışmaya devam eder —
orada patron adımı YOKTUR, dolayısıyla eşiği koruyan tek katman odur.

🔴 Fixture'lar İTHAL EDİLMEZ, yardımcı FONKSİYONLAR edilir (`test_ok1a_chain_
binding.py` deseni): kardeş paketin `conftest.py`si pytest tarafından burada
yüklenmez ve fixture adını parametreye taşımak F811 üretirdi.

🔴 Beklenen hata metni ELLE yazılmıştır (`procurement/guards.py`den ithal
edilmemiştir): kendi ifadesini teste kopyalayan test hiçbir şey bekçilemez.
"""

import uuid
from datetime import date
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.approvals import service as approvals_service
from app.modules.approvals.models import ApprovalDocumentType, ApprovalRole
from app.modules.procurement.models import (
    PurchasePriority,
    PurchaseRequest,
    PurchaseRequestLine,
    PurchaseRequestStatus,
)
from app.modules.projects.models import Project
from app.modules.users.models import User
from tests.modules.approvals.conftest import (
    PAROLA,
    adim_durumlari,
    onay_rolu_ver,
    satinalma_evraki,
)

_SATINALMA = ApprovalDocumentType.purchase_request
_YOL = "/purchase-requests"

#: 🔴 ELLE YAZILDI (`procurement/guards.APPROVAL_THRESHOLD_EXCEEDED`ten değil).
_ESIK_ASILDI = "Bu tutardaki bir talebi onaylamak için üst seviye yetki gerekir"

#: 320 × ₺1.850 = **₺592.000** — varsayılan eşiğin (₺500.000) ÜSTÜ. Ayrı bir
#: `PUT /approvals/settings` çağrısına gerek yoktur.
_MIKTAR = Decimal("320")
_BIRIM_FIYAT = Decimal("1850.00")
_ESIK_USTU_TUTAR = _MIKTAR * _BIRIM_FIYAT


async def _aktor(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    *,
    email: str,
    role_key: str,
    approval_roles: tuple[ApprovalRole, ...] = (),
) -> tuple[User, dict[str, str]]:
    """Sistem rolü + onay rolleri AYRI verilir (K1); tüm projeler görünür."""
    user = await user_factory(email=email, password=PAROLA, role_key=role_key)
    user.all_projects = True
    await onay_rolu_ver(seeded_db, user, *approval_roles)
    yanit = await client.post("/auth/login", json={"email": email, "password": PAROLA})
    assert yanit.status_code == 200, yanit.text
    return user, {"Authorization": f"Bearer {yanit.json()['access_token']}"}


async def _esik_ustu_talep(seeded_db: AsyncSession, proje: Project, yaratan: User) -> uuid.UUID:
    return await satinalma_evraki(
        seeded_db, proje, yaratan, quantity=_MIKTAR, unit_price=_BIRIM_FIYAT
    )


async def _zincir(seeded_db: AsyncSession, document_id: uuid.UUID, yaratan: User):
    return await approvals_service.create_chain(
        seeded_db,
        document_type=_SATINALMA,
        document_id=document_id,
        amount=_ESIK_USTU_TUTAR,
        created_by_user_id=yaratan.id,
    )


async def _zincirsiz_talep(seeded_db: AsyncSession, proje: Project, yaratan: User) -> uuid.UUID:
    """Eşik ÜSTÜ ama ZİNCİRSİZ talep — `submit`ten geçmemiş ESKİ kayıt.

    API ile üretilemez (`submit` HER ZAMAN zincir açar); bu yüzden elle kurulur.
    Yapının kendisi eski kayıtları temsil eder, bir kaçamağı değil.
    """
    request = PurchaseRequest(
        request_no=f"ESKI-{uuid.uuid4().hex[:8]}",
        request_date=date(2026, 7, 17),
        priority=PurchasePriority.normal,
        project_id=proje.id,
        status=PurchaseRequestStatus.pending_approval,
        created_by_user_id=yaratan.id,
    )
    seeded_db.add(request)
    await seeded_db.flush()
    seeded_db.add(
        PurchaseRequestLine(
            request_id=request.id,
            free_text_name="C25/30 Hazır Beton",
            free_text_unit="m³",
            quantity=_MIKTAR,
            estimated_unit_price=_BIRIM_FIYAT,
            sort_order=0,
        )
    )
    await seeded_db.flush()
    return request.id


# --------------------------------------------------------------------------- #
# 12 (a) — eşik üstü zincirde PM adımı GEÇER
# --------------------------------------------------------------------------- #


async def test_esik_USTU_zincirde_PROJE_MUDURU_adimi_GECER(
    client, seeded_db, user_factory, project_factory
):
    """Aktörün `procurement` seviyesi `approve`tır, `full` DEĞİL.

    Bugün `_assert_approver_level` onu durduruyor; zincir varken durdurmamalı —
    eşiğin karşılığı zincirin 4. (`patron`) adımıdır.
    """
    proje = await project_factory(code="OK1C-E1", name="Eşik Projesi 1")
    yaratan, _ = await _aktor(
        client, seeded_db, user_factory, email="esik-t12a-yaratan@ok1c.co", role_key="accounting"
    )
    satinalma, _ = await _aktor(
        client,
        seeded_db,
        user_factory,
        email="esik-t12a-sat@ok1c.co",
        role_key="procurement",
        approval_roles=(ApprovalRole.procurement,),
    )
    _pm, pm_basliklari = await _aktor(
        client,
        seeded_db,
        user_factory,
        email="esik-t12a-pm@ok1c.co",
        role_key="project_manager",
        approval_roles=(ApprovalRole.project_manager,),
    )
    document_id = await _esik_ustu_talep(seeded_db, proje, yaratan)
    zincir = await _zincir(seeded_db, document_id, yaratan)
    assert len(await adim_durumlari(seeded_db, zincir.id)) == 4, "eşik üstü zincir DÖRT adımlı"
    await approvals_service.approve_next_step(
        seeded_db, actor=satinalma, document_type=_SATINALMA, document_id=document_id
    )

    yanit = await client.post(f"{_YOL}/{document_id}/approve", headers=pm_basliklari)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "pending_approval", "2. adım talebi ilerletmemeli"
    assert await adim_durumlari(seeded_db, zincir.id) == [True, True, False, False]


# --------------------------------------------------------------------------- #
# 12 (b) — talep PATRON imzası olmadan TAMAMLANAMAZ
# --------------------------------------------------------------------------- #


async def test_esik_USTU_talep_PATRON_adimi_imzalanmadan_TAMAMLANMAZ(
    client, seeded_db, user_factory, project_factory
):
    """🔴 İKİNCİ ZORUNLU İDDİA: ikame eşiği ORTADAN KALDIRMAZ, YER DEĞİŞTİRİR.

    İlk ÜÇ imza atıldıktan sonra talep HÂLÂ `pending_approval` olmalı ve
    `approved_at`/`approved_by_user_id` HÂLÂ boş kalmalıdır; talep ancak
    `patron` adımı imzalanınca `quote_wait`e geçer.

    Bugün de ölçülebilsin diye ilk üç imza `system_admin` SİSTEM rollü
    aktörlerle atılır (`procurement: admin` — hem modül kapısından hem `full`
    eşiğinden geçer). Böylece bu bekçi T2/T3'ten BAĞIMSIZ olarak bugün
    YEŞİLDİR ve ikame eklendikten sonra da yeşil KALMALIDIR: eşiğin mekanizması
    ikameyle birlikte kaybolursa test kırılır.
    """
    proje = await project_factory(code="OK1C-E2", name="Eşik Projesi 2")
    yaratan, _ = await _aktor(
        client, seeded_db, user_factory, email="esik-t12b-yaratan@ok1c.co", role_key="accounting"
    )
    roller = (ApprovalRole.procurement, ApprovalRole.project_manager, ApprovalRole.accounting)
    basliklar = []
    for sira, rol in enumerate(roller):
        _kullanici, baslik = await _aktor(
            client,
            seeded_db,
            user_factory,
            email=f"esik-t12b-adim{sira}@ok1c.co",
            role_key="system_admin",
            approval_roles=(rol,),
        )
        basliklar.append(baslik)
    _patron, patron_basliklari = await _aktor(
        client,
        seeded_db,
        user_factory,
        email="esik-t12b-patron@ok1c.co",
        role_key="patron",
        approval_roles=(ApprovalRole.patron,),
    )
    document_id = await _esik_ustu_talep(seeded_db, proje, yaratan)
    await _zincir(seeded_db, document_id, yaratan)

    for sira, baslik in enumerate(basliklar):
        yanit = await client.post(f"{_YOL}/{document_id}/approve", headers=baslik)
        assert yanit.status_code == 200, (sira, yanit.text)
        govde = yanit.json()
        assert govde["status"] == "pending_approval", f"adım {sira + 1} talebi ERKEN ilerletti"
        assert govde["approved_at"] is None, f"adım {sira + 1} ERKEN damgaladı"
        assert govde["approved_by_user_id"] is None, f"adım {sira + 1} ERKEN damgaladı"

    son = await client.post(f"{_YOL}/{document_id}/approve", headers=patron_basliklari)

    assert son.status_code == 200, son.text
    assert son.json()["status"] == "quote_wait", "patron imzası talebi ilerletmeliydi"
    assert son.json()["approved_at"] is not None


async def test_esik_USTU_zincir_DORT_GERCEK_ROLLE_UCTAN_UCA_isler(
    client, seeded_db, user_factory, project_factory
):
    """12(a) + 12(b) BİRLİKTE, GERÇEK sistem rolleriyle — dilimin nihai kanıtı.

    Yukarıdaki (b) bekçisi patron kuralını `system_admin` aktörlerle bugün
    ölçebiliyor ama ürünün gerçek kadrosuyla değil. Bu bekçi tam da onu ölçer:

    | Adım | Onay rolü | SİSTEM rolü | `procurement` seviyesi | Bugünkü engel |
    |---|---|---|---|---|
    | 1 | procurement | `procurement` | `full` | — |
    | 2 | project_manager | `project_manager` | `approve` | **eşik 403** |
    | 3 | accounting | `accounting` | `none` | **modül kapısı 403** |
    | 4 | patron | `patron` | `full` | — |

    Yani zincir BUGÜN 2. adımda duruyor ve 3. adım hiç denenemiyor bile.
    """
    proje = await project_factory(code="OK1C-E3", name="Eşik Projesi 3")
    yaratan, _ = await _aktor(
        client, seeded_db, user_factory, email="esik-t12c-yaratan@ok1c.co", role_key="hr_manager"
    )
    kadro = (
        (ApprovalRole.procurement, "procurement"),
        (ApprovalRole.project_manager, "project_manager"),
        (ApprovalRole.accounting, "accounting"),
        (ApprovalRole.patron, "patron"),
    )
    basliklar = []
    for sira, (rol, sistem_rolu) in enumerate(kadro):
        _kullanici, baslik = await _aktor(
            client,
            seeded_db,
            user_factory,
            email=f"esik-t12c-adim{sira}@ok1c.co",
            role_key=sistem_rolu,
            approval_roles=(rol,),
        )
        basliklar.append(baslik)
    document_id = await _esik_ustu_talep(seeded_db, proje, yaratan)
    zincir = await _zincir(seeded_db, document_id, yaratan)

    for sira, baslik in enumerate(basliklar[:3]):
        yanit = await client.post(f"{_YOL}/{document_id}/approve", headers=baslik)
        assert yanit.status_code == 200, (f"adım {sira + 1}", yanit.text)
        assert yanit.json()["status"] == "pending_approval", f"adım {sira + 1} ERKEN ilerletti"

    son = await client.post(f"{_YOL}/{document_id}/approve", headers=basliklar[3])

    assert son.status_code == 200, son.text
    assert son.json()["status"] == "quote_wait"
    assert await adim_durumlari(seeded_db, zincir.id) == [True, True, True, True]


# --------------------------------------------------------------------------- #
# 13 — ZİNCİRSİZ eşik üstü talepte kapı AYNEN kalır
# --------------------------------------------------------------------------- #


async def test_ZINCIRSIZ_esik_ustu_talepte_FULL_kapisi_KORUNUR(
    client, seeded_db, user_factory, project_factory
):
    """Zincir yoksa eşiği koruyan TEK katman `_assert_approver_level`tir.

    `approve` seviyesindeki PM 403 alır ve metin EŞİK metnidir, modül kapısının
    metni DEĞİL — hangi katmanın durdurduğu ayırt edilebilir kalmalıdır.
    `full` seviyesindeki satınalma ise AYNI talepte geçer.
    """
    proje = await project_factory(code="OK1C-E4", name="Eşik Projesi 4")
    yaratan, _ = await _aktor(
        client, seeded_db, user_factory, email="esik-t13-yaratan@ok1c.co", role_key="hr_manager"
    )
    _pm, pm_basliklari = await _aktor(
        client,
        seeded_db,
        user_factory,
        email="esik-t13-pm@ok1c.co",
        role_key="project_manager",
        approval_roles=(ApprovalRole.project_manager,),
    )
    _sat, satinalma_basliklari = await _aktor(
        client,
        seeded_db,
        user_factory,
        email="esik-t13-sat@ok1c.co",
        role_key="procurement",
        approval_roles=(ApprovalRole.procurement,),
    )
    document_id = await _zincirsiz_talep(seeded_db, proje, yaratan)

    engellenen = await client.post(f"{_YOL}/{document_id}/approve", headers=pm_basliklari)
    assert engellenen.status_code == 403, engellenen.text
    assert engellenen.json()["detail"] == _ESIK_ASILDI

    gecen = await client.post(f"{_YOL}/{document_id}/approve", headers=satinalma_basliklari)
    assert gecen.status_code == 200, gecen.text
    assert gecen.json()["status"] == "quote_wait"

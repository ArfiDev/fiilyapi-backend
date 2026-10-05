"""OK-1A T3 — SATINALMA talebi onay ZİNCİRİNE bağlandı.

Mockup zinciri (`projedesign/Onay Kutusu.dc.html:150-178`):
**Satınalma → Proje Müdürü → Muhasebe**, eşik aşılırsa sona **Patron**.

## Hakediş ikilisinden İKİ fark

1. **Ret TERMİNALDİR** (`rejected`), taslağa DÖNMEZ — bugünkü davranış korunur
   (SA §3). Yani zincir silinir ve YENİ bir zincir de açılamaz: ihtiyaç
   sürüyorsa YENİ talep açılır.
2. **İzin kapısı ÇİFT KATMANLIDIR:** uç `procurement ≥ approve` ister, eşik
   üstü ayrıca `full` ister (`transitions.APPROVAL_THRESHOLD_LEVEL`).
   🔴 **OK-1C K1 bunu DARALTTI:** zinciri OLAN bir talepte ikinci katman
   (`full`) İKAME EDİLİR, çünkü eşiğin karşılığı zincirin `patron` ADIMIDIR ve
   iki kez uygulanması zinciri 2. adımda kilitliyordu. Zincirsiz talepte
   katman AYNEN durur.

🔴 Onay rolü ≠ sistem rolü: matriste `accounting` sistem rolünün
`procurement` izni `_N`dir, yani gerçek muhasebeci bu uçtan GEÇEMEZ. Zincirin
"Muhasebe" adımını burada `project_manager` SİSTEM rolündeki bir aktör taşır ve
bu, ikisinin ayrı eksenler olduğunun somut kanıtıdır.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.approvals import guards as approval_guards
from app.modules.approvals import service as approvals_service
from app.modules.approvals.models import ApprovalDocumentType, ApprovalRole
from app.modules.procurement import transitions
from app.modules.procurement.models import PurchaseRequestStatus
from app.modules.projects.models import Project
from app.modules.users.models import ProjectMember
from tests.modules.approvals.conftest import (
    adim_durumlari,
    adim_rolleri,
    onay_rolu_ver,
    rol_sahipleri_dolgusu,  # noqa: F401  (fixture)
    zincir_getir,
)

pytestmark = pytest.mark.usefixtures("rol_sahipleri_dolgusu")

_TIP = ApprovalDocumentType.purchase_request
_YOL = "/purchase-requests"
_GEREKCE = {"reason": "Bütçe dönemi kapandı"}


async def _onaycı(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    proje: Project,
    *,
    email: str,
    approval_roles: tuple[ApprovalRole, ...],
    role_key: str = "project_manager",
) -> dict[str, str]:
    user = await user_factory(email=email, password="parola1234", role_key=role_key)
    seeded_db.add(ProjectMember(user_id=user.id, project_id=proje.id, role_id=user.role_id))
    await onay_rolu_ver(seeded_db, user, *approval_roles)
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _gonder(client: AsyncClient, headers: dict[str, str], request_id: uuid.UUID) -> None:
    yanit = await client.post(f"{_YOL}/{request_id}/submit", headers=headers)
    assert yanit.status_code == 200, yanit.text


# --------------------------------------------------------------------------- #
# 1. `submit` zincir kurar
# --------------------------------------------------------------------------- #


async def test_submit_UC_ADIMLI_zincir_kurar_MOCKUP_SIRASIYLA(
    client: AsyncClient,
    sef_headers: dict[str, str],
    seeded_db: AsyncSession,
    gorunen_proje: Project,
    talep_fabrikasi,
) -> None:
    talep = await talep_fabrikasi(gorunen_proje, lines=[("10.000", "40.00")])

    await _gonder(client, sef_headers, talep.id)

    zincir = await zincir_getir(seeded_db, _TIP, talep.id)
    assert zincir is not None, "submit zincir AÇMADI"
    assert await adim_rolleri(seeded_db, zincir.id) == [
        ApprovalRole.procurement,
        ApprovalRole.project_manager,
        ApprovalRole.accounting,
    ]
    # `repository.request_estimated_total` (KDV'siz, TEK sayı) — 10,000 × ₺40.
    assert zincir.amount_snapshot == Decimal("400.00")


async def test_esik_ustunde_PATRON_eklenir_altinda_EKLENMEZ(
    client: AsyncClient,
    sef_headers: dict[str, str],
    seeded_db: AsyncSession,
    gorunen_proje: Project,
    talep_fabrikasi,
) -> None:
    ucuz = await talep_fabrikasi(gorunen_proje, lines=[("1.000", "10.00")])
    pahali = await talep_fabrikasi(gorunen_proje, lines=[("1.000", "900000.00")])

    await _gonder(client, sef_headers, ucuz.id)
    await _gonder(client, sef_headers, pahali.id)

    ucuz_zincir = await zincir_getir(seeded_db, _TIP, ucuz.id)
    pahali_zincir = await zincir_getir(seeded_db, _TIP, pahali.id)
    assert ApprovalRole.patron not in await adim_rolleri(seeded_db, ucuz_zincir.id)
    assert (await adim_rolleri(seeded_db, pahali_zincir.id))[-1] is ApprovalRole.patron


# --------------------------------------------------------------------------- #
# 2. 🔴 FAIL-CLOSED — fiyatsız kalem eşiğin ÜSTÜ sayılır
# --------------------------------------------------------------------------- #


async def test_FIYATSIZ_kalem_tutari_BELIRSIZ_yapar_ve_PATRON_adimi_EKLENIR(
    seeded_db: AsyncSession,
    gorunen_proje: Project,
    talep_fabrikasi,
    kullanici_kimligi,
) -> None:
    """🔴 SA kanonunun ZİNCİR hâli (`transitions.chain_amount`).

    Uçtan geçilerek kurulamaz: `submit` fiyatsız kalemi zaten 422 ile reddeder
    (BİRİNCİ katman) — bu test İKİNCİ katmanı, yani zincirin tutarı NASIL
    okuduğunu ölçer. Fiyatsız kalem `SUM`da yutulur ve "eksik veri" ile "düşük
    tutar" aynı `0`ı üretir; bilinmeyen KÜÇÜK sayılsaydı ₺2M'lik bir talep tek
    alan boş bırakılarak Patron adımını ATLARDI (SA'da bu yol FİİLEN bulundu).
    """
    fiyatsiz = await talep_fabrikasi(gorunen_proje, lines=[("15.000", None)])
    fiyatli = await talep_fabrikasi(gorunen_proje, lines=[("1.000", "10.00")])

    assert await transitions.chain_amount(seeded_db, fiyatsiz) is None
    assert await transitions.chain_amount(seeded_db, fiyatli) == Decimal("10.00")

    zincir = await approvals_service.create_chain(
        seeded_db,
        document_type=_TIP,
        document_id=fiyatsiz.id,
        amount=await transitions.chain_amount(seeded_db, fiyatsiz),
        created_by_user_id=await kullanici_kimligi("sef@satinalma.co"),
    )
    assert (await adim_rolleri(seeded_db, zincir.id))[-1] is ApprovalRole.patron
    assert zincir.amount_snapshot is None


# --------------------------------------------------------------------------- #
# 3. Adımlar sırayla ilerler; SON adım `quote_wait`e taşır
# --------------------------------------------------------------------------- #


async def test_ARA_adimlar_PENDINGDE_birakir_SON_adim_QUOTE_WAITE_gecirir(
    client: AsyncClient,
    sef_headers: dict[str, str],
    seeded_db: AsyncSession,
    gorunen_proje: Project,
    talep_fabrikasi,
    user_factory,
) -> None:
    talep = await talep_fabrikasi(gorunen_proje, lines=[("1.000", "100.00")])
    satinalma = await _onaycı(
        client,
        seeded_db,
        user_factory,
        gorunen_proje,
        email="sa-satinalma@ok1a.co",
        approval_roles=(ApprovalRole.procurement,),
        role_key="procurement",
    )
    pm = await _onaycı(
        client,
        seeded_db,
        user_factory,
        gorunen_proje,
        email="sa-pm@ok1a.co",
        approval_roles=(ApprovalRole.project_manager,),
    )
    muhasebe = await _onaycı(
        client,
        seeded_db,
        user_factory,
        gorunen_proje,
        email="sa-muhasebe@ok1a.co",
        approval_roles=(ApprovalRole.accounting,),
    )
    await _gonder(client, sef_headers, talep.id)
    zincir = await zincir_getir(seeded_db, _TIP, talep.id)

    for basliklar in (satinalma, pm):
        yanit = await client.post(f"{_YOL}/{talep.id}/approve", headers=basliklar)
        assert yanit.status_code == 200, yanit.text
        assert yanit.json()["status"] == "pending_approval"
        assert yanit.json()["approved_at"] is None

    son = await client.post(f"{_YOL}/{talep.id}/approve", headers=muhasebe)
    assert son.status_code == 200, son.text
    assert son.json()["status"] == "quote_wait"
    assert son.json()["approved_at"] is not None
    assert await adim_durumlari(seeded_db, zincir.id) == [True, True, True]


async def test_onay_ROLU_olmayan_aktor_403(
    client: AsyncClient,
    sef_headers: dict[str, str],
    pm_headers: dict[str, str],
    gorunen_proje: Project,
    talep_fabrikasi,
) -> None:
    """`pm_headers` uç kapısını (`procurement=_APR`) GEÇER ama onay rolü YOKTUR."""
    talep = await talep_fabrikasi(gorunen_proje, lines=[("1.000", "100.00")])
    await _gonder(client, sef_headers, talep.id)

    yanit = await client.post(f"{_YOL}/{talep.id}/approve", headers=pm_headers)

    assert yanit.status_code == 403, yanit.text
    assert yanit.json()["detail"] == approval_guards.APPROVAL_ROLE_MISSING


# --------------------------------------------------------------------------- #
# 4. Ret — zincir SİLİNİR ve durum TERMİNALDİR
# --------------------------------------------------------------------------- #


async def test_ret_zinciri_DAMGALANIR_ve_durum_TERMINAL_kalir(
    client: AsyncClient,
    sef_headers: dict[str, str],
    seeded_db: AsyncSession,
    gorunen_proje: Project,
    talep_fabrikasi,
    user_factory,
) -> None:
    """Hakediş ikilisinden FARK: ret `draft`a DÖNMEZ, `rejected`ta KALIR ve
    yeniden gönderilemez (409) — yani zincir bir daha AÇILMAZ."""
    talep = await talep_fabrikasi(gorunen_proje, lines=[("1.000", "100.00")])
    satinalma = await _onaycı(
        client,
        seeded_db,
        user_factory,
        gorunen_proje,
        email="sa-ret-satinalma@ok1a.co",
        approval_roles=(ApprovalRole.procurement,),
        role_key="procurement",
    )
    pm = await _onaycı(
        client,
        seeded_db,
        user_factory,
        gorunen_proje,
        email="sa-ret-pm@ok1a.co",
        approval_roles=(ApprovalRole.project_manager,),
    )
    await _gonder(client, sef_headers, talep.id)
    await client.post(f"{_YOL}/{talep.id}/approve", headers=satinalma)

    # 🔴 Ret de bir KARARDIR ve AYNI bekçi hunisinden geçer: reddeden aktör
    # SIRADAKİ adımın (burada Proje Müdürü) rolünü taşımalıdır.
    ret = await client.post(f"{_YOL}/{talep.id}/reject", json=_GEREKCE, headers=pm)

    assert ret.status_code == 200, ret.text
    assert ret.json()["status"] == "rejected"
    assert ret.json()["rejection_reason"] == _GEREKCE["reason"]
    zincir = await zincir_getir(seeded_db, _TIP, talep.id)
    assert zincir is not None and zincir.rejected_at is not None, "ret zinciri DAMGALAMALI"

    tekrar = await client.post(f"{_YOL}/{talep.id}/submit", headers=sef_headers)
    assert tekrar.status_code == 409, tekrar.text
    # Yeni zincir AÇILMADI: en yeni kayıt hâlâ reddedilmiş olandır.
    assert (await zincir_getir(seeded_db, _TIP, talep.id)).id == zincir.id


async def test_ret_GEREKCESIZ_422_ZINCIR_AYAKTA(
    client: AsyncClient,
    sef_headers: dict[str, str],
    pm_headers: dict[str, str],
    seeded_db: AsyncSession,
    gorunen_proje: Project,
    talep_fabrikasi,
) -> None:
    talep = await talep_fabrikasi(gorunen_proje, lines=[("1.000", "100.00")])
    await _gonder(client, sef_headers, talep.id)

    yanit = await client.post(
        f"{_YOL}/{talep.id}/reject", json={"reason": "  "}, headers=pm_headers
    )

    assert yanit.status_code == 422, yanit.text
    assert await zincir_getir(seeded_db, _TIP, talep.id) is not None
    assert (
        await zincir_getir(seeded_db, _TIP, talep.id)
    ).document_type is ApprovalDocumentType.purchase_request


# --------------------------------------------------------------------------- #
# 5. ESKİ (zincirsiz) kayıt — geri uyumluluk yolu (dar ve ölçülmüş)
# --------------------------------------------------------------------------- #


async def test_ZINCIRSIZ_eski_talep_BUGUNKU_yolla_onaylanir(
    client: AsyncClient,
    pm_headers: dict[str, str],
    seeded_db: AsyncSession,
    gorunen_proje: Project,
    talep_fabrikasi,
) -> None:
    """Dağıtımdan ÖNCE `pending_approval`da kalmış talepler zincirsizdir; zincir
    ZORUNLU kılınsaydı ne onaylanabilir ne reddedilebilirlerdi."""
    talep = await talep_fabrikasi(
        gorunen_proje, status=PurchaseRequestStatus.pending_approval, lines=[("1.000", "100.00")]
    )
    assert await zincir_getir(seeded_db, _TIP, talep.id) is None

    yanit = await client.post(f"{_YOL}/{talep.id}/approve", headers=pm_headers)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "quote_wait"


async def test_ESIK_KAPISI_zincirli_talepte_PATRON_ADIMINA_TASINIR(
    client: AsyncClient,
    sef_headers: dict[str, str],
    seeded_db: AsyncSession,
    gorunen_proje: Project,
    talep_fabrikasi,
    user_factory,
) -> None:
    """🔴 **OK-1C K1 İLE DEĞİŞTİ** (kullanıcı kararı 2026-08-22).

    ESKİ İDDİA (OK-1A): "zincir eşik kapısını değiştirmez; eşik üstü talebi
    `approve` seviyeli bir aktör onay ROLÜ taşısa bile ilerletemez (403)".
    ÖLÇÜM bunun bir kusur olduğunu gösterdi: eşik üstü zincirin 2. adımı Proje
    Müdürü'nündür ve onun seviyesi tam olarak `approve`tır — yani zincir kendi
    2. adımında KİLİTLENİYOR, dördüncü (`patron`) adımı hiç denenemiyordu.

    YENİ İDDİA — ve eskisinden GEVŞEK DEĞİL: "üst seviye yetki" koşulu
    KAYBOLMAZ, `full` KAPISINDAN zincirin `patron` ADIMINA taşınır. Yani eşik
    üstü talep hâlâ `approve` seviyeli bir aktör tarafından TEK BAŞINA
    tamamlanamaz; üstelik zincirli yolda koşul DAHA SIKIDIR (görevler ayrılığı
    aynı kişinin iki adımı imzalamasını da yasaklar).

    Kuralın SINIRI (zincirsiz talepte `full` kapısı AYNEN durur) kardeş dosyada
    ölçülür: `test_ok1c_esik_zinciri.py::test_ZINCIRSIZ_esik_ustu_talepte_FULL_
    kapisi_KORUNUR`. Patron adımı olmadan tamamlanamadığı da orada
    (`test_esik_USTU_talep_PATRON_adimi_imzalanmadan_TAMAMLANMAZ`).
    """
    talep = await talep_fabrikasi(gorunen_proje, lines=[("1.000", "900000.00")])
    pm_rollu = await _onaycı(
        client,
        seeded_db,
        user_factory,
        gorunen_proje,
        email="sa-esik-pm@ok1a.co",
        approval_roles=(ApprovalRole.procurement,),
    )
    await _gonder(client, sef_headers, talep.id)
    zincir = await zincir_getir(seeded_db, _TIP, talep.id)
    assert zincir is not None
    assert await adim_rolleri(seeded_db, zincir.id) == [
        ApprovalRole.procurement,
        ApprovalRole.project_manager,
        ApprovalRole.accounting,
        ApprovalRole.patron,
    ], "eşik üstü zincire PATRON adımı eklenmemiş — kural taşınmadı, KAYBOLDU"

    yanit = await client.post(f"{_YOL}/{talep.id}/approve", headers=pm_rollu)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "pending_approval", "tek imza talebi ilerletti"
    assert yanit.json()["approved_at"] is None, "yarım onay talebi DAMGALADI"
    assert await adim_durumlari(seeded_db, zincir.id) == [True, False, False, False]

    # 🔴 TEK BAŞINA TAMAMLANAMAZ: aynı aktör ikinci kez denediğinde sıradaki
    # adım BAŞKA bir rolündür (`project_manager`) ve bekçi 4 daha görevler
    # ayrılığından ÖNCE ateşler — ölçüldü, varsayılmadı.
    tekrar = await client.post(f"{_YOL}/{talep.id}/approve", headers=pm_rollu)
    assert tekrar.status_code == 403, tekrar.text
    assert tekrar.json()["detail"] == approval_guards.APPROVAL_ROLE_MISSING
    assert await adim_durumlari(seeded_db, zincir.id) == [True, False, False, False]

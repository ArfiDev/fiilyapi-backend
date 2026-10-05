"""OK-1A — onay zinciri motorunun paylaşılan fixture'ları.

İzin matrisi (`roles/seed_data.py`, **`approvals`** — 2. modül, grup GENEL;
seed'de ZATEN VARDIR, matris DEĞİŞMEDİ):
system_admin=**_A** · patron=_F · site_chief=_OWN · field_engineer=_OWN ·
hr_manager=_OWN · accounting=_FIN · project_manager=_PRJ · procurement=_STK.

Yani `approvals: admin` kapısından **yalnız `system_admin`** geçer; ayar ve rol
atama uçlarının kapısı budur (sözleşme Y5).

🔴 IZN-B3b (K1): ONAY ROLÜ = PROJE ROLÜ. Ayrı bir "onay rolü" ataması YOKTUR (tablo söküldü).
Zincir adımı rol ANAHTARIYLA (`roles.key`) tanımlıdır ve adımı, belgenin projesinde o role
atanmış kişi (`project_members`) ya da "Tüm projeler" + ANA rolü o rol olan kişi onaylar.
Bir kişi bir projede TEK rol taşır (UQ user+project).

Eski testlerin `approval_roles=[...]` / `onay_rolu_ver(...)` çağrıları İKİ YERDEN KORUNUR:
* **tek rol**: kişi, o rolle her projenin ekip üyesi olur — var olan projelerde hemen,
  sonradan açılacak projelerde `before_flush` dinleyicisiyle (`session.info`te kayıtlı); yani
  eski "her yerde bu onay rolü" anlamı proje rolüne ÇEVRİLİR. `tum_projeler=False` / `projeler=`
  verilirse yalnız o projelerde üye olur (IDOR kurulumları aynen çalışır).
* **birden çok rol**: ARTIK MÜMKÜN DEĞİL (tek kişi tek projede tek rol) → `ValueError`; test
  ayrı kullanıcı / ayrı proje ile yeniden kurulmalıdır.
Yeni testler `proje_rolu_ver` ile açıkça kurar.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.modules.approvals.models import (
    ApprovalChain,
    ApprovalDocumentType,
    ApprovalRole,
    ApprovalStep,
)
from app.modules.contracts.models import SubcontractorContract
from app.modules.procurement.models import (
    PurchasePriority,
    PurchaseRequest,
    PurchaseRequestLine,
    PurchaseRequestStatus,
)
from app.modules.progress_payments.models import (
    ProgressPayment,
    ProgressPaymentLine,
    ProgressPaymentStatus,
)
from app.modules.projects.models import Project, ProjectContract
from app.modules.roles.models import Role
from app.modules.sites.models import Site
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
    SubcontractorProgressPaymentLine,
)
from app.modules.users.models import ProjectMember, User, UserStatus

PAROLA = "parola1234"

#: `session.info` anahtarı: sonradan açılacak HER projeye ekip üyesi yazılacak (kullanıcı, rol)
#: çiftleri.
_UYELIK_KAYDI = "izn_b3b_onay_uyelikleri"


#: `session.info` anahtarı: açıksa her yeni projeye her adım rolü için "dolgu" üye yazılır.
_DOLGU_KAYDI = "izn_b3b_rol_sahipleri_dolgusu"

#: `evrak_fabrikasi`nin ilk ihtiyaçta kurduğu dolgu kullanıcılar (global dinleyiciyi AÇMAZ).
_DOLGU_TEMBEL = "izn_b3b_rol_sahipleri_dolgusu_tembel"

_DOLGU_HASH = hash_password(PAROLA)


async def _dolgu_kullanicilari(session: AsyncSession) -> dict[str, tuple[uuid.UUID, uuid.UUID]]:
    """Her adım rolü için TEK dolgu kullanıcı: `{rol anahtarı: (kullanıcı id, rol id)}`.

    IZN-B3b: zincir açılırken HER adım rolünün projede sahibi olmalıdır (yoksa 409). Eski testler
    yalnız İMZA ATACAK aktörü kurduğundan zincir kuran hazırlık adımı bu dolguyla geçer; dolgu
    kullanıcılar hiçbir testte oturum açmaz ve kimsenin kutusunu etkilemez. Kullanıcılar AYRI bir
    flush'ta yazılır (ekip satırı ile aynı flush'ta FK sırası garanti değildir).
    """
    roller = {
        key: rid
        for key, rid in (await session.execute(select(Role.key, Role.id))).all()
        if key in {rol.value for rol in ApprovalRole}
    }
    sonuc: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}
    for anahtar, rid in roller.items():
        kisi = User(
            email=f"dolgu-{anahtar}-{uuid.uuid4().hex[:10]}@dolgu.co",
            password_hash=_DOLGU_HASH,
            full_name=f"Dolgu {anahtar}",
            role_id=rid,
            status=UserStatus.active,
        )
        session.add(kisi)
        await session.flush()
        sonuc[anahtar] = (kisi.id, rid)
    return sonuc


@event.listens_for(Session, "before_flush")
def _yeni_projelere_uyelik_yaz(session: Session, _ctx, _instances) -> None:
    """Bu flush'ta eklenen HER yeni projeye: (a) eski `onay_rolu_ver` semantiği ("her projede bu
    rol") için kayıtlı çiftlerin ekip üyeliği, (b) `rol_sahipleri_dolgusu` açıksa dolgu üyeler.
    Hiçbiri açık değilse maliyetsizdir."""
    kayit = list(session.info.get(_UYELIK_KAYDI) or ())
    kayit += [tuple(v) for v in (session.info.get(_DOLGU_KAYDI) or {}).values()]
    if not kayit:
        return
    for obj in [o for o in session.new if isinstance(o, Project)]:
        if obj.id is None:
            obj.id = uuid.uuid4()
        for user_id, role_id in kayit:
            session.add(ProjectMember(user_id=user_id, project_id=obj.id, role_id=role_id))


@pytest.fixture
async def rol_sahipleri_dolgusu(seeded_db: AsyncSession) -> None:
    """Bu testte açılan HER projeye her adım rolü için dolgu üye yazılır (IZN-B3b: boş adım rolü
    zincir açmayı engeller). Zincir açan akışları eski haliyle süren modüller
    `pytestmark = pytest.mark.usefixtures("rol_sahipleri_dolgusu")` ile açar."""
    seeded_db.info[_DOLGU_KAYDI] = await _dolgu_kullanicilari(seeded_db)


async def rol_sahipleri_kur(
    session: AsyncSession, project: Project, *, haric: Sequence[ApprovalRole] = ()
) -> None:
    """Dolgu üyeleri MEVCUT bir projeye yazar (`haric` roller dolgulanmaz)."""
    dolgu = session.info.get(_DOLGU_KAYDI) or session.info.get(_DOLGU_TEMBEL)
    if dolgu is None:
        dolgu = session.info[_DOLGU_TEMBEL] = await _dolgu_kullanicilari(session)
    for anahtar, (user_id, role_id) in dolgu.items():
        if ApprovalRole(anahtar) in haric:
            continue
        session.add(ProjectMember(user_id=user_id, project_id=project.id, role_id=role_id))
    await session.flush()


async def rol_id(session: AsyncSession, role_key: str) -> uuid.UUID:
    return (await session.execute(select(Role.id).where(Role.key == role_key))).scalar_one()


async def proje_rolu_ver(
    session: AsyncSession, user: User, project: Project, role_key: str
) -> ProjectMember:
    """Kişiyi projenin ekibine `role_key` rolüyle yazar (zaten üyeyse rolünü DEĞİŞTİRİR)."""
    rid = await rol_id(session, role_key)
    uye = await session.scalar(
        select(ProjectMember).where(
            ProjectMember.user_id == user.id, ProjectMember.project_id == project.id
        )
    )
    if uye is None:
        uye = ProjectMember(user_id=user.id, project_id=project.id, role_id=rid)
        session.add(uye)
    else:
        uye.role_id = rid
    await session.flush()
    return uye


@pytest.fixture
def aktor_fabrikasi(seeded_db: AsyncSession, user_factory) -> Callable[..., Awaitable[User]]:
    """Sistem rolü + onay rolleri AYRI verilir (modül docstring'i).

    🔴 ÜÇÜNCÜ bir eksen daha var (T4): PROJE GÖRÜNÜRLÜĞÜ. `GET /approvals`
    artık `projects.service.visible_projects` üzerinden süzer, dolayısıyla
    kapsamı olmayan bir aktör HİÇBİR satır görmez. `projeler` verilirse yalnız
    o projeler, verilmezse (`tum_projeler=True`) hepsi görünür; `tum_projeler=
    False` ise erişim satırı HİÇ açılmaz — IDOR bekçisinin kurulumu budur.
    """

    async def _kur(
        email: str,
        *,
        role_key: str = "accounting",
        approval_roles: Sequence[ApprovalRole] = (),
        full_name: str = "Onay Aktörü",
        projeler: Sequence[Project] | None = None,
        tum_projeler: bool = True,
    ) -> User:
        user = await user_factory(
            email=email, password=PAROLA, role_key=role_key, full_name=full_name
        )
        roller = list(approval_roles)
        if len(roller) > 1:
            raise ValueError("IZN-B3b: tek kisi tek projede tek rol — ayri kullanici kullanin")
        if projeler is not None:
            # Verilen projelerde ekip üyesi: onay rolü varsa O rolle (proje rolü), yoksa ana rolle.
            uye_rolu = await rol_id(seeded_db, roller[0].value) if roller else user.role_id
            for proje in projeler:
                seeded_db.add(ProjectMember(user_id=user.id, project_id=proje.id, role_id=uye_rolu))
        elif roller and tum_projeler:
            # Eski "her projede bu onay rolü": var olan + sonradan açılan her projede proje rolü.
            # Ekip kişisi: `all_projects=False` (üretimde "Tüm projeler" kişide ekip satırı yoktur;
            # `step_owner_clause` all_projects kişide ekip satırını yok sayar).
            user.all_projects = False
            await onay_rolu_ver(seeded_db, user, *roller)
        elif roller:
            pass  # kapsamsız: üyelik YOK (IDOR bekçisinin kurulumu)
        elif tum_projeler:
            user.all_projects = True
        await seeded_db.flush()
        return user

    return _kur


@pytest.fixture
def giris(client: AsyncClient) -> Callable[[str], Awaitable[dict[str, str]]]:
    async def _giris(email: str) -> dict[str, str]:
        resp = await client.post("/auth/login", json={"email": email, "password": PAROLA})
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    return _giris


async def adim_rolleri(session: AsyncSession, chain_id: uuid.UUID) -> list[ApprovalRole]:
    """Zincirin adım rollerini `step_no` sırasıyla döner."""
    rows = (
        await session.execute(
            select(ApprovalStep)
            .where(ApprovalStep.chain_id == chain_id)
            .order_by(ApprovalStep.step_no)
        )
    ).scalars()
    return [row.approval_role for row in rows]


async def zincir_getir(
    session: AsyncSession, document_type: ApprovalDocumentType, document_id: uuid.UUID
) -> ApprovalChain | None:
    """Evrağın EN YENİ zinciri (OKT-B1: ret zinciri silmediği için bir evrağın
    birden çok kaydı olabilir; açık zincir hep en yenisidir)."""
    return await session.scalar(
        select(ApprovalChain)
        .where(
            ApprovalChain.document_type == document_type,
            ApprovalChain.document_id == document_id,
        )
        .order_by(ApprovalChain.created_at.desc())
        .limit(1)
    )


# --------------------------------------------------------------------------- #
# T3 — evrak ailelerinin ORTAK yardimcilari
# --------------------------------------------------------------------------- #
#
# Bu üç yardımcı `tests/progress_payments/` · `tests/subcontractor_progress_
# payments/` · `tests/modules/procurement/` altındaki T3 dosyalarından
# İTHAL EDİLİR. pytest kardeş `conftest.py`leri otomatik yüklemez ama modül
# olarak ithal etmek serbesttir (`test_ok1a_chain_build.py` deseni) — üç ayrı
# kopya "onay rolü ver" yardımcısı doğsaydı biri değişip diğerleri unutulurdu.


async def onay_rolu_ver(session: AsyncSession, user: User, *roller: ApprovalRole) -> User:
    """Eski API'nin YENİ karşılığı: kişi, `rol` ROLÜYLE her projenin ekip üyesi olur.

    Var olan projelere hemen, sonradan açılacaklara `before_flush` dinleyicisiyle yazılır.
    `all_projects`e DOKUNMAZ; çağıran kişiyi `all_projects=False` kurmalıdır (üretimde "Tüm
    projeler" kişide ekip satırı bulunmaz ve `step_owner_clause` o satırı yok sayar). Adım
    sahipliği yalnız bu ekip satırlarından gelir.
    Birden çok rol → `ValueError` (tek kişi tek projede tek rol).
    """
    if not roller:
        return user
    if len(roller) != 1:
        raise ValueError("IZN-B3b: tek kisi tek projede tek rol — ayri kullanici kullanin")
    rid = await rol_id(session, roller[0].value)
    for proje in (await session.execute(select(Project))).scalars().all():
        mevcut = await session.scalar(
            select(ProjectMember).where(
                ProjectMember.user_id == user.id, ProjectMember.project_id == proje.id
            )
        )
        if mevcut is None:
            session.add(ProjectMember(user_id=user.id, project_id=proje.id, role_id=rid))
        else:
            mevcut.role_id = rid
    session.info.setdefault(_UYELIK_KAYDI, []).append((user.id, rid))
    await session.flush()
    return user


async def kullanici(session: AsyncSession, email: str) -> User:
    """E-postadan kullanıcıyı çözer (headers fixture'ları kullanıcıyı döndürmez)."""
    return (await session.execute(select(User).where(User.email == email))).scalar_one()


async def adim_durumlari(session: AsyncSession, chain_id: uuid.UUID) -> list[bool]:
    """Adımların KARARA BAĞLANMIŞ olup olmadığı, `step_no` sırasıyla."""
    rows = (
        await session.execute(
            select(ApprovalStep)
            .where(ApprovalStep.chain_id == chain_id)
            .order_by(ApprovalStep.step_no)
        )
    ).scalars()
    return [row.decided_at is not None for row in rows]


# --------------------------------------------------------------------------- #
# T4 — GERCEK evrak kurulumu
# --------------------------------------------------------------------------- #
#
# 🔴 T4'te `GET /approvals` iki yeni sey yapiyor: satiri EVRAK AILESINDEN
# zenginlestiriyor ve `visible_projects` uzerinden PROJE GORUNURLUGU suzuyor.
# Ikisi de zincirin `document_id`sinin GERCEK bir evraga cozulmesini gerektirir;
# uydurma bir UUID artik (dogru sekilde) kutuda GORUNMEZ — kaynagi cozulemeyen
# zincir fail-closed sayilir (SA kanonu).
#
# Bu yuzden T1/T3'te uydurma kimlikle kurulan zincirler bu fabrikaya tasindi.


async def _proje_kur(project_factory, kod: str, ad: str) -> Project:
    return await project_factory(code=kod, name=ad)


async def taseron_evraki(
    session: AsyncSession,
    project: Project,
    creator: User,
    *,
    subcontractor_name: str | None = "Akın İnşaat",
    work_category: str | None = "Betonarme",
    site_adi: str | None = None,
    description: str | None = None,
    period: tuple[int, int] | None = None,
    unit_price: Decimal = Decimal("1000.00"),
    quantity: Decimal = Decimal("100"),
) -> uuid.UUID:
    """Taşeron hakedişi (sözleşme + hakediş + TEK satır).

    Sözleşme KALEMİ yoktur: `contract_amount` 0 olur, avans tavanı da 0 —
    böylece net beklentisi elde hesaplanabilir kalır (brüt + KDV − teminat).
    """
    site = None
    if site_adi is not None:
        site = Site(project_id=project.id, code=f"{project.code}-SNT", name=site_adi)
        session.add(site)
        await session.flush()
    contract = SubcontractorContract(
        project_id=project.id,
        site_id=site.id if site is not None else None,
        subcontractor_name=subcontractor_name,
        work_category=work_category,
        contract_no=f"{project.code}-TSZ",
        advance_pct=Decimal("10"),
        retainage_pct=Decimal("5"),
        vat_pct=Decimal("20"),
        created_by=creator.id,
    )
    session.add(contract)
    await session.flush()
    payment = SubcontractorProgressPayment(
        contract_id=contract.id,
        project_id=project.id,
        sequence_no=47,
        status=SubcontractorPaymentStatus.pending_approval,
        period_year=period[0] if period else None,
        period_month=period[1] if period else None,
        description=description,
        vat_pct=contract.vat_pct,
        advance_pct=contract.advance_pct,
        retainage_pct=contract.retainage_pct,
        created_by=creator.id,
    )
    session.add(payment)
    await session.flush()
    session.add(
        SubcontractorProgressPaymentLine(
            payment_id=payment.id,
            code="A.001",
            description="Betonarme",
            unit="m³",
            contract_unit_price=unit_price,
            coefficient=Decimal("1.000"),
            quantity=quantity,
            sort_order=0,
        )
    )
    await session.flush()
    return payment.id


async def isveren_evraki(
    session: AsyncSession,
    project: Project,
    creator: User,
    *,
    site_adlari: Sequence[str] = ("A-Blok",),
    description: str | None = None,
    period: tuple[int, int] | None = None,
    unit_price: Decimal = Decimal("1000.00"),
    quantity: Decimal = Decimal("100"),
) -> uuid.UUID:
    """İşveren hakedişi. `advance_pct=0` seçildi: avans mahsubu zinciri BU
    dilimin konusu değil, net beklentisi elde hesaplanabilir kalsın."""
    if await session.get(ProjectContract, project.id) is None:
        session.add(
            ProjectContract(
                project_id=project.id,
                contract_no=f"{project.code}-SZL",
                amount=Decimal("10000000.00"),
            )
        )
        await session.flush()
    payment = ProgressPayment(
        project_id=project.id,
        sequence_no=5,
        status=ProgressPaymentStatus.pending_approval,
        period_year=period[0] if period else None,
        period_month=period[1] if period else None,
        description=description,
        vat_pct=Decimal("20"),
        advance_pct=Decimal("0"),
        retainage_pct=Decimal("5"),
        created_by=creator.id,
    )
    session.add(payment)
    await session.flush()
    pay = quantity / Decimal(len(site_adlari))
    for sira, ad in enumerate(site_adlari):
        site = Site(project_id=project.id, code=f"{project.code}-S{sira}", name=ad)
        session.add(site)
        await session.flush()
        session.add(
            ProgressPaymentLine(
                payment_id=payment.id,
                site_id=site.id,
                code=f"A.{sira + 1:03d}",
                description="Kaba yapı",
                unit="m³",
                contract_unit_price=unit_price,
                coefficient=Decimal("1.000"),
                quantity=pay,
                sort_order=sira,
            )
        )
    await session.flush()
    return payment.id


async def satinalma_evraki(
    session: AsyncSession,
    project: Project,
    creator: User,
    *,
    kalem_adi: str = "C25/30 Hazır Beton",
    birim: str = "m³",
    quantity: Decimal = Decimal("320"),
    unit_price: Decimal | None = Decimal("1850.00"),
    justification: str | None = None,
    request_no: str | None = None,
) -> uuid.UUID:
    """Satın alma talebi. 🔴 BRÜT/NET AYRIMI YOKTUR (mockup `:173` TEK kutu)."""
    request = PurchaseRequest(
        request_no=request_no or f"SAT-{uuid.uuid4().hex[:8]}",
        request_date=date(2026, 7, 17),
        priority=PurchasePriority.normal,
        project_id=project.id,
        justification=justification,
        status=PurchaseRequestStatus.pending_approval,
        created_by_user_id=creator.id,
    )
    session.add(request)
    await session.flush()
    session.add(
        PurchaseRequestLine(
            request_id=request.id,
            free_text_name=kalem_adi,
            free_text_unit=birim,
            quantity=quantity,
            estimated_unit_price=unit_price,
            sort_order=0,
        )
    )
    await session.flush()
    return request.id


@pytest.fixture
def evrak_fabrikasi(seeded_db: AsyncSession, project_factory):
    """Zincire GERÇEKTEN bağlanabilir bir evrak kurar ve `(document_id, project)`
    döner. Proje verilmezse kendi projesini açar. `rol_sahipleri=True` (varsayılan): projeye her
    adım rolü için dolgu üye yazılır (IZN-B3b: boş adım rolü zincir açmayı engeller)."""

    sayac = {"n": 0}
    dolgulu: set[uuid.UUID] = set()

    async def _kur(
        document_type: ApprovalDocumentType,
        *,
        creator: User,
        project: Project | None = None,
        rol_sahipleri: bool = True,
        **kwargs,
    ) -> tuple[uuid.UUID, Project]:
        if project is None:
            sayac["n"] += 1
            project = await _proje_kur(
                project_factory, f"OK1A-{uuid.uuid4().hex[:6]}", f"Güneşkent {sayac['n']}"
            )
        kurucular = {
            ApprovalDocumentType.subcontractor_progress_payment: taseron_evraki,
            ApprovalDocumentType.progress_payment: isveren_evraki,
            ApprovalDocumentType.purchase_request: satinalma_evraki,
        }
        document_id = await kurucular[document_type](seeded_db, project, creator, **kwargs)
        if rol_sahipleri and _DOLGU_KAYDI not in seeded_db.info and project.id not in dolgulu:
            dolgulu.add(project.id)
            await rol_sahipleri_kur(seeded_db, project)
        return document_id, project

    return _kur

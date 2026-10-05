"""OK-1A — onay zinciri motorunun paylaşılan fixture'ları.

İzin matrisi (`roles/seed_data.py`, **`approvals`** — 2. modül, grup GENEL;
seed'de ZATEN VARDIR, matris DEĞİŞMEDİ):
system_admin=**_A** · patron=_F · site_chief=_OWN · field_engineer=_OWN ·
hr_manager=_OWN · accounting=_FIN · project_manager=_PRJ · procurement=_STK.

Yani `approvals: admin` kapısından **yalnız `system_admin`** geçer; ayar ve rol
atama uçlarının kapısı budur (sözleşme Y5).

🔴 ONAY ROLÜ ≠ SİSTEM ROLÜ. `user.role_id` sistem rolüdür (izin matrisini
belirler); `user_approval_roles` ise onay zincirinin adım rolüdür. Bir kullanıcı
BİRDEN ÇOK onay rolü taşıyabilir (K1) ve onay rolü taşımak hiçbir izin vermez.
Fixture'lar ikisini bilerek AYRI parametre olarak alır.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.approvals.models import (
    ApprovalChain,
    ApprovalDocumentType,
    ApprovalRole,
    ApprovalStep,
    UserApprovalRole,
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
from app.modules.sites.models import Site
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
    SubcontractorProgressPaymentLine,
)
from app.modules.users.models import ProjectMember, User

PAROLA = "parola1234"


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
        for rol in approval_roles:
            seeded_db.add(UserApprovalRole(user_id=user.id, approval_role=rol))
        if projeler is not None:
            for proje in projeler:
                seeded_db.add(
                    ProjectMember(user_id=user.id, project_id=proje.id, role_id=user.role_id)
                )
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
    """Kullanıcıya ONAY ROLÜ verir — sistem rolüne DOKUNMAZ (K1).

    İkisi kasten ayrıdır: onay rolü hiçbir izin vermez, izin matrisi de hiçbir
    imza adaylığı vermez. Bir adımı onaylayacak aktörün İKİSİNE DE ihtiyacı
    vardır (uç kapısı + adım rolü) ve testler bunu ayrı ayrı kurar.
    """
    for rol in roller:
        session.add(UserApprovalRole(user_id=user.id, approval_role=rol))
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
    döner. Proje verilmezse kendi projesini açar."""

    sayac = {"n": 0}

    async def _kur(
        document_type: ApprovalDocumentType,
        *,
        creator: User,
        project: Project | None = None,
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
        return document_id, project

    return _kur

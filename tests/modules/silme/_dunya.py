"""SIL-B1 test dünyası: şantiye ağacını dolduran fabrikalar (puantaj, günlük, belge, plan…)."""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from app.modules.approvals.models import (
    ApprovalChain,
    ApprovalDocumentType,
    ApprovalRole,
    ApprovalStep,
)
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.contracts.models import (
    EmployerContractGroup,
    EmployerContractItem,
    SubcontractorContract,
)
from app.modules.documents.models.core import Document, DocumentFolder
from app.modules.personnel.models import Personnel
from app.modules.progress_payments.models import (
    ProgressPayment,
    ProgressPaymentLine,
    ProgressPaymentStatus,
)
from app.modules.projects.models import ProjectContract
from app.modules.site_diary.models import SiteDiaryEntry, WorkerSource
from app.modules.site_planning.models import (
    PlanGoalStatus,
    PlanResourceKind,
    SitePlanGoal,
    SitePlanRow,
    SitePlanSprint,
)
from app.modules.sites.models import Section, Site
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
)
from app.modules.timesheet.models import TimesheetEntry
from app.modules.units.models import Block, Unit, UnitKind


async def site(session, project, code: str = "A-BLOK", name: str = "A-Blok Şantiyesi") -> Site:
    kayit = Site(project_id=project.id, code=code, name=name)
    session.add(kayit)
    await session.flush()
    return kayit


async def section(session, site_, name: str = "Kaba İnşaat") -> Section:
    kayit = Section(site_id=site_.id, name=name)
    session.add(kayit)
    await session.flush()
    return kayit


async def boq(session, site_, code: str = "01.001") -> BoqItem:
    group = BoqGroup(site_id=site_.id, name="TOPRAK İŞLERİ")
    session.add(group)
    await session.flush()
    item = BoqItem(
        site_id=site_.id,
        group_id=group.id,
        code=code,
        description="Kazı",
        unit="m³",
        quantity=Decimal("10.000"),
        unit_price=Decimal("5.00"),
    )
    session.add(item)
    await session.flush()
    return item


async def block(session, project, site_, name: str = "A Blok") -> Block:
    kayit = Block(project_id=project.id, site_id=site_.id, name=name)
    session.add(kayit)
    await session.flush()
    return kayit


async def unit(session, project, block_, unit_no: str = "1") -> Unit:
    kayit = Unit(
        project_id=project.id, block_id=block_.id, unit_no=unit_no, unit_kind=UnitKind.apartment
    )
    session.add(kayit)
    await session.flush()
    return kayit


async def personel(session, ad: str = "Ahmet Yılmaz") -> Personnel:
    kayit = Personnel(full_name=ad, source=WorkerSource.company)
    session.add(kayit)
    await session.flush()
    return kayit


async def puantaj(session, project, site_, personel_, user) -> TimesheetEntry:
    kayit = TimesheetEntry(
        personnel_id=personel_.id,
        site_id=site_.id,
        project_id=project.id,
        work_date=date(2026, 3, 2),
        hours=Decimal("8.0"),
        created_by=user.id,
    )
    session.add(kayit)
    await session.flush()
    return kayit


async def gunluk(session, project, site_, user) -> SiteDiaryEntry:
    kayit = SiteDiaryEntry(
        site_id=site_.id, project_id=project.id, entry_date=date(2026, 3, 2), created_by=user.id
    )
    session.add(kayit)
    await session.flush()
    return kayit


async def belge(session, project, site_) -> Document:
    kayit = Document(
        project_id=project.id,
        site_id=site_.id,
        filename="isg-tutanak.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
    )
    session.add(kayit)
    await session.flush()
    return kayit


async def klasor(session, project, site_) -> DocumentFolder:
    kayit = DocumentFolder(project_id=project.id, site_id=site_.id, name="İSG")
    session.add(kayit)
    await session.flush()
    return kayit


async def plan(session, project, site_) -> None:
    session.add_all(
        [
            SitePlanRow(
                site_id=site_.id,
                project_id=project.id,
                kind=PlanResourceKind.equipment,
                label="Kule Vinç",
            ),
            SitePlanGoal(
                site_id=site_.id,
                project_id=project.id,
                week_start=date(2026, 3, 2),
                title="Temel betonu",
                status=PlanGoalStatus.in_progress,
            ),
            SitePlanSprint(site_id=site_.id, name="Mart Sprinti"),
        ]
    )
    await session.flush()


async def hakedis_satiri(
    session,
    project,
    site_,
    user,
    *,
    durum: ProgressPaymentStatus = ProgressPaymentStatus.draft,
    sira: int = 1,
) -> ProgressPayment:
    """İşveren hakedişi + `site_`ye bağlı bir satır (`progress_payment_lines.site_id` RESTRICT).

    Başlık şantiye silinince KALIR (proje düzeyindedir); yalnız şantiyeye bağlı satırlar gider.
    """
    if (await session.get(ProjectContract, project.id)) is None:
        session.add(
            ProjectContract(
                project_id=project.id,
                contract_no="SZL-SIL-1",
                amount=Decimal("1000000"),
                advance_pct=Decimal("10"),
                retainage_pct=Decimal("5"),
                vat_pct=Decimal("20"),
            )
        )
        await session.flush()
    group = EmployerContractGroup(project_id=project.id, name=f"G{sira}", sort_order=sira)
    session.add(group)
    await session.flush()
    item = EmployerContractItem(
        project_id=project.id,
        group_id=group.id,
        code=f"11.{sira:03d}",
        description="Kalem",
        unit="m³",
        quantity=Decimal("100"),
        unit_price=Decimal("1000"),
        sort_order=sira,
    )
    session.add(item)
    await session.flush()
    odeme = ProgressPayment(
        project_id=project.id,
        sequence_no=sira,
        status=durum,
        vat_pct=Decimal("20"),
        advance_pct=Decimal("10"),
        retainage_pct=Decimal("5"),
        created_by=user.id,
    )
    odeme.lines = [
        ProgressPaymentLine(
            contract_item_id=item.id,
            site_id=site_.id,
            code=item.code,
            description=item.description,
            unit=item.unit,
            contract_unit_price=item.unit_price,
            coefficient=Decimal("1.000"),
            quantity=Decimal("10"),
            group_name=group.name,
        )
    ]
    session.add(odeme)
    await session.flush()
    return odeme


async def tasaron_hakedisi(
    session,
    project,
    site_,
    user,
    *,
    durum: SubcontractorPaymentStatus = SubcontractorPaymentStatus.draft,
) -> SubcontractorProgressPayment:
    """Şantiyeye bağlı taşeron sözleşmesi (`site_id` RESTRICT) + hakedişi (CASCADE)."""
    sozlesme = SubcontractorContract(
        project_id=project.id,
        site_id=site_.id,
        subcontractor_name="Demir Ltd.",
        contract_no="TSZ-SIL-1",
        advance_pct=Decimal("10"),
        retainage_pct=Decimal("5"),
        vat_pct=Decimal("20"),
        created_by=user.id,
    )
    session.add(sozlesme)
    await session.flush()
    odeme = SubcontractorProgressPayment(
        contract_id=sozlesme.id,
        project_id=project.id,
        sequence_no=1,
        status=durum,
        vat_pct=sozlesme.vat_pct,
        advance_pct=sozlesme.advance_pct,
        retainage_pct=sozlesme.retainage_pct,
        created_by=user.id,
    )
    session.add(odeme)
    await session.flush()
    return odeme


async def onay_zinciri(session, odeme: SubcontractorProgressPayment, user) -> ApprovalChain:
    """FK OLMAYAN bağ: `approval_chains.document_id` taşeron hakedişini gösterir."""
    zincir = ApprovalChain(
        document_type=ApprovalDocumentType.subcontractor_progress_payment,
        document_id=odeme.id,
        threshold_snapshot=Decimal("500000"),
        amount_snapshot=Decimal("1000"),
        created_by_user_id=user.id,
    )
    session.add(zincir)
    await session.flush()
    session.add(ApprovalStep(chain_id=zincir.id, step_no=1, approval_role=ApprovalRole.site_chief))
    await session.flush()
    return zincir


async def sayim(session, model, *kosullar) -> int:
    sorgu = select(func.count()).select_from(model)
    for kosul in kosullar:
        sorgu = sorgu.where(kosul)
    return int((await session.execute(sorgu)).scalar_one())


def yeni_kimlik() -> uuid.UUID:
    return uuid.uuid4()


async def musteri(session, ad: str = "Ahmet Yılmaz"):
    from app.modules.customers.models import Customer, CustomerType  # noqa: PLC0415

    kayit = Customer(customer_type=CustomerType.person, name=ad)
    session.add(kayit)
    await session.flush()
    return kayit


async def satis(
    session,
    project,
    unit_,
    user,
    *,
    tur=None,
    durum=None,
    kapora: Decimal | None = None,
):
    """Ünite satışı. Varsayılan: tahsilatsız, kaporasız REZERVASYON."""
    from app.modules.sales.models import SaleType, UnitSale, UnitSaleStatus  # noqa: PLC0415

    musteri_ = await musteri(session)
    kayit = UnitSale(
        unit_id=unit_.id,
        project_id=project.id,
        customer_id=musteri_.id,
        created_by=user.id,
        sale_type=tur or SaleType.reservation,
        status=durum or UnitSaleStatus.reservation,
        sale_price=Decimal("1000000.00"),
        reservation_deposit=kapora,
    )
    session.add(kayit)
    await session.flush()
    return kayit


async def taksit(session, satis_, *, odenen: Decimal = Decimal("0"), tam_odendi: bool = False):
    """Satış taksiti. `tam_odendi=False` + `odenen>0` = KISMİ tahsilat (`paid_at` boş kalır)."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from app.modules.sales.models import SaleInstallment  # noqa: PLC0415

    kayit = SaleInstallment(
        sale_id=satis_.id,
        sequence_no=1,
        label="1. Taksit",
        due_date=date(2026, 6, 1),
        amount=Decimal("100000.00"),
        paid_amount=odenen,
        paid_at=datetime(2026, 6, 1, tzinfo=UTC) if tam_odendi else None,
    )
    session.add(kayit)
    await session.flush()
    return kayit


async def bordro_donemi(session, yil: int, ay: int, durum):
    from app.modules.payroll.models import PayrollPeriod  # noqa: PLC0415

    kayit = PayrollPeriod(year=yil, month=ay, status=durum)
    session.add(kayit)
    await session.flush()
    return kayit

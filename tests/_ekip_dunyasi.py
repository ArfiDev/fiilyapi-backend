"""TEST YARDIMCISI — iki projeli (Kule / Köprü) EKİP dünyası + rol kurucuları (IZN-B3 onarımı).

Hem `test_izn_b3_onarim_idor.py` (genel rota tabanlı bekçi) hem de özel senaryo testleri kullanır.
Her projede: şantiye, bölüm, BOQ grubu+kalemi, `pending_approval` işveren hakedişi (+ sözleşme),
`pending_approval` taşeron hakedişi (+ sözleşme), GÖNDERİLMİŞ günlük kaydı. Böylece onay/ret/
yeniden aç gibi geçişler durum kontrolüne takılmadan KAPI + GÖRÜNÜRLÜK aşamasını ölçer.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sayfalar import SAYFALAR, PageLevel
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.contracts.models import SubcontractorContract
from app.modules.progress_payments.models import ProgressPayment, ProgressPaymentStatus
from app.modules.projects.models import Project, ProjectContract
from app.modules.roles import service as roles_service
from app.modules.roles.models import Role, RolePagePermission
from app.modules.roles.schemas import RoleCreate
from app.modules.site_diary.models import DiaryStatus, SiteDiaryEntry
from app.modules.sites.models import Section, Site
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
)


async def rol_kur(session: AsyncSession, key: str, level: PageLevel, approve: bool = False) -> Role:
    """Her sayfası `level` (ve `approve` ise onay eylemi olan sayfalarda Onaylar) olan özel rol."""
    rol = await roles_service.create_custom_role(
        session, RoleCreate(key=key, name=key, emoji="", description="")
    )
    await session.execute(
        update(RolePagePermission).where(RolePagePermission.role_id == rol.id).values(level=level)
    )
    if approve:
        onayli = [s.key for s in SAYFALAR if s.onay_var]
        await session.execute(
            update(RolePagePermission)
            .where(RolePagePermission.role_id == rol.id, RolePagePermission.page_key.in_(onayli))
            .values(can_approve=True)
        )
    await session.flush()
    return rol


class EkipProje:
    """Bir projenin kaynakları."""

    def __init__(self, project: Project, site: Site, section: Section) -> None:
        self.project, self.site, self.section = project, site, section
        self.group: BoqGroup
        self.item: BoqItem
        self.payment: ProgressPayment
        self.sub_contract: SubcontractorContract
        self.sub_payment: SubcontractorProgressPayment
        self.entry: SiteDiaryEntry

    @property
    def doldurma(self) -> dict[tuple[str, str], str]:
        """(yol öneki, parametre) → bu projenin gerçek varlık kimliği (rota doldurma)."""
        return {
            ("/projects", "project_id"): str(self.project.id),
            ("/sites", "site_id"): str(self.site.id),
            ("/sections", "section_id"): str(self.section.id),
            ("/boq", "item_id"): str(self.item.id),
            ("/boq", "group_id"): str(self.group.id),
            ("/diary", "entry_id"): str(self.entry.id),
            ("/progress-payments", "payment_id"): str(self.payment.id),
            ("/subcontractor-progress-payments", "payment_id"): str(self.sub_payment.id),
            ("/subcontractor-contracts", "contract_id"): str(self.sub_contract.id),
            ("/sections", "owner_id"): str(self.section.id),
            ("/subcontractor-contracts", "owner_id"): str(self.sub_contract.id),
        }


async def proje_kur(
    session: AsyncSession, project_factory, kod: str, ad: str, olusturan_id: uuid.UUID
) -> EkipProje:
    project = await project_factory(kod, name=ad)
    site = Site(project_id=project.id, code=f"{kod}-S", name=f"{ad} Şantiyesi")
    session.add(site)
    await session.flush()
    section = Section(
        site_id=site.id,
        name=f"{ad} Bölümü",
        start_date=date(2026, 5, 4),
        end_date=date(2026, 5, 29),
        planned_worker_count=3,
        sort_order=1,
    )
    session.add(section)
    dunya = EkipProje(project, site, section)
    dunya.group = BoqGroup(site_id=site.id, name=f"{ad} Grubu")
    session.add(dunya.group)
    await session.flush()
    dunya.item = BoqItem(
        site_id=site.id,
        group_id=dunya.group.id,
        code=f"{kod}.001",
        description="Kalem",
        unit="m3",
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
    )
    session.add(dunya.item)
    session.add(
        ProjectContract(
            project_id=project.id,
            contract_no=f"SZL-{kod}",
            amount=Decimal("1000000"),
            advance_pct=Decimal("10"),
            retainage_pct=Decimal("5"),
            vat_pct=Decimal("20"),
        )
    )
    await session.flush()
    dunya.payment = ProgressPayment(
        project_id=project.id,
        sequence_no=1,
        status=ProgressPaymentStatus.pending_approval,
        vat_pct=Decimal("20"),
        advance_pct=Decimal("10"),
        retainage_pct=Decimal("5"),
        created_by=olusturan_id,
    )
    dunya.sub_contract = SubcontractorContract(
        project_id=project.id,
        subcontractor_name=f"{ad} Taşeron",
        contract_no=f"{kod}-TSZ",
        advance_pct=Decimal("10"),
        retainage_pct=Decimal("5"),
        vat_pct=Decimal("20"),
        created_by=olusturan_id,
    )
    session.add_all([dunya.payment, dunya.sub_contract])
    await session.flush()
    dunya.sub_payment = SubcontractorProgressPayment(
        contract_id=dunya.sub_contract.id,
        project_id=project.id,
        sequence_no=1,
        status=SubcontractorPaymentStatus.pending_approval,
        period_year=2026,
        period_month=5,
        vat_pct=Decimal("20"),
        advance_pct=Decimal("10"),
        retainage_pct=Decimal("5"),
        created_by=olusturan_id,
    )
    dunya.entry = SiteDiaryEntry(
        site_id=site.id,
        project_id=project.id,
        entry_date=date(2026, 5, 4),
        created_by=olusturan_id,
        status=DiaryStatus.submitted,
    )
    session.add_all([dunya.sub_payment, dunya.entry])
    await session.flush()
    return dunya

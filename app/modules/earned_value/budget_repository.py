"""Butce veri erisimi — BOQ/bolum okuma + revizyon girdileri + takvim ayari.

Yalniz okuma/yazma; kural `budget_service`te, agac kurulumu `budget_tree`te.
Cekirdek tablolar (BOQ, bolum) YALNIZ OKUNUR.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqGroup, BoqItem, BoqItemSectionAllocation
from app.modules.catalog.models import EvDiscipline
from app.modules.earned_value import defaults
from app.modules.earned_value.budget_tree import (
    BoqSnapshot,
    DisciplineInfo,
    GroupInfo,
    ItemInfo,
    ItemSetting,
    LeafSetting,
    RevisionInputs,
    SectionInfo,
)
from app.modules.earned_value.contractor_bridge import to_engine_ct
from app.modules.earned_value.engine import expand_holiday_ranges, working_day_predicate
from app.modules.earned_value.models import (
    EvDistribution,
    EvGroupDiscipline,
    EvHoliday,
    EvItemSettings,
    EvLeafSettings,
    EvRevision,
    EvSiteSettings,
    EvWindow,
    RevisionStatus,
)
from app.modules.sites.models import Section


async def load_boq(session: AsyncSession, site_id: uuid.UUID) -> BoqSnapshot:
    groups = (await session.execute(select(BoqGroup).where(BoqGroup.site_id == site_id))).scalars()
    items = (await session.execute(select(BoqItem).where(BoqItem.site_id == site_id))).scalars()
    item_rows = list(items)
    alloc_rows = (
        await session.execute(
            select(BoqItemSectionAllocation).where(
                BoqItemSectionAllocation.boq_item_id.in_([i.id for i in item_rows])
            )
        )
    ).scalars()
    allocations: dict[uuid.UUID, dict[uuid.UUID, Decimal]] = {}
    for a in alloc_rows:
        allocations.setdefault(a.boq_item_id, {})[a.section_id] = a.quantity
    sections = (await session.execute(select(Section).where(Section.site_id == site_id))).scalars()
    return BoqSnapshot(
        groups=tuple(GroupInfo(g.id, g.name, g.sort_order) for g in groups),
        items=tuple(
            ItemInfo(i.id, i.group_id, i.code, i.description, i.unit, i.quantity, i.sort_order)
            for i in item_rows
        ),
        allocations=allocations,
        sections=tuple(
            SectionInfo(
                s.id, s.name, s.start_date, s.end_date, s.planned_worker_count, s.sort_order, s.code
            )
            for s in sections
        ),
    )


async def boq_synced_at(session: AsyncSession, site_id: uuid.UUID) -> datetime | None:
    """ "BOQ senk." cipi: kalem ve tahsislerin en son degisme ani."""
    items = await session.scalar(
        select(func.max(BoqItem.updated_at)).where(BoqItem.site_id == site_id)
    )
    allocs = await session.scalar(
        select(func.max(BoqItemSectionAllocation.updated_at))
        .join(BoqItem, BoqItem.id == BoqItemSectionAllocation.boq_item_id)
        .where(BoqItem.site_id == site_id)
    )
    return max((t for t in (items, allocs) if t is not None), default=None)


async def load_disciplines(session: AsyncSession) -> list[DisciplineInfo]:
    rows = (await session.execute(select(EvDiscipline))).scalars()
    return [
        DisciplineInfo(
            d.id, d.code, d.name, d.color, to_engine_ct(d.default_contractor_type), d.sort_order
        )
        for d in rows
    ]


async def list_revisions(session: AsyncSession, site_id: uuid.UUID) -> list[EvRevision]:
    rows = await session.execute(
        select(EvRevision).where(EvRevision.site_id == site_id).order_by(EvRevision.number)
    )
    return list(rows.scalars())


async def revision_by_status(
    session: AsyncSession, site_id: uuid.UUID, status: RevisionStatus, *, for_update: bool = False
) -> EvRevision | None:
    stmt = select(EvRevision).where(EvRevision.site_id == site_id, EvRevision.status == status)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def load_inputs(session: AsyncSession, revision_id: uuid.UUID) -> RevisionInputs:
    gd = await session.execute(
        select(EvGroupDiscipline).where(EvGroupDiscipline.revision_id == revision_id)
    )
    items = await session.execute(
        select(EvItemSettings).where(EvItemSettings.revision_id == revision_id)
    )
    leaves = await session.execute(
        select(EvLeafSettings).where(EvLeafSettings.revision_id == revision_id)
    )
    dists = await session.execute(
        select(EvDistribution).where(EvDistribution.revision_id == revision_id)
    )
    windows = await session.execute(select(EvWindow).where(EvWindow.revision_id == revision_id))
    return RevisionInputs(
        group_disciplines={r.boq_group_id: r.discipline_id for r in gd.scalars()},
        items={
            r.boq_item_id: ItemSetting(r.contractor_type, r.is_direct, r.catalog_item_id)
            for r in items.scalars()
        },
        leaves={
            (r.boq_item_id, r.section_id): LeafSetting(
                r.unit_mhr, r.rate_source, r.contractor_type, r.is_direct
            )
            for r in leaves.scalars()
        },
        distributions={r.discipline_id: r.distribution for r in dists.scalars()},
        windows={
            (r.discipline_id, r.section_id): (r.start_date, r.end_date) for r in windows.scalars()
        },
    )


@dataclass(frozen=True, slots=True)
class CalendarConfig:
    week_start_dow: int
    weekly_off_days: frozenset[int]
    holidays: frozenset[date]
    standard_daily_hours: Decimal

    @property
    def is_working_day(self) -> Callable[[date], bool]:
        return working_day_predicate(self.weekly_off_days, self.holidays)


async def load_calendar(session: AsyncSession, site_id: uuid.UUID) -> CalendarConfig:
    """Santiye takvim ayari; satir yoksa `defaults` (K1, K5, S5, K10)."""
    row = await session.get(EvSiteSettings, site_id)
    holidays = (
        await session.execute(select(EvHoliday).where(EvHoliday.site_id == site_id))
    ).scalars()
    expanded = expand_holiday_ranges((h.date_from, h.date_to) for h in holidays)
    if row is None:
        return CalendarConfig(
            defaults.WEEK_START_DOW,
            defaults.WEEKLY_OFF_DAYS,
            expanded,
            defaults.STANDARD_DAILY_HOURS,
        )
    return CalendarConfig(
        row.week_start_dow,
        defaults.mask_to_days(row.weekly_off_days),
        expanded,
        row.standard_daily_hours,
    )

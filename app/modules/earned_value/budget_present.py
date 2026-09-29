"""Ic yapilar (agac, onizleme, fark) → yanit semalari. Hesap YAPMAZ, yalniz toplar/eslestirir."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import UNRESTRICTED, DisciplineScope
from app.modules.earned_value.budget_ops import PreviewResult, RevisionDiff
from app.modules.earned_value.budget_scope import catalog_link_ids, mask_catalog_links
from app.modules.earned_value.budget_service import BudgetState
from app.modules.earned_value.budget_tree import (
    BudgetTree,
    DisciplineNode,
    Finding,
    GroupNode,
    ItemNode,
    LeafNode,
)
from app.modules.earned_value.engine import SeriesPreview, WeekLoad
from app.modules.earned_value.models import EvCatalogItem, EvRevision
from app.modules.earned_value.schemas_budget import (
    BarOut,
    BudgetTotals,
    BudgetView,
    DayOut,
    DisciplineOut,
    DisciplinePreviewOut,
    FindingOut,
    GroupOut,
    ItemOut,
    LeafDiffOut,
    LeafOut,
    PreviewOut,
    RevisionDiffOut,
    RevisionOut,
    ScheduleOut,
    SectionOut,
    SeriesOut,
    UserRef,
    WeekOut,
)
from app.modules.users.models import User

ZERO = Decimal(0)


def _share(part: Decimal, total: Decimal) -> Decimal | None:
    return None if total == 0 else part / total


def _sum(leaves: Iterable[LeafNode], *, direct_only: bool = False) -> Decimal:
    return sum((lf.budget_mhr for lf in leaves if lf.is_direct or not direct_only), ZERO)


async def revision_out(session: AsyncSession, rev: EvRevision) -> RevisionOut:
    frozen_by = None
    if rev.frozen_by_user_id is not None:
        user = await session.get(User, rev.frozen_by_user_id)
        if user is not None:
            frozen_by = UserRef(id=user.id, full_name=user.full_name)
    return RevisionOut(
        id=rev.id,
        number=rev.number,
        name=rev.name,
        description=rev.description,
        status=rev.status,
        frozen_at=rev.frozen_at,
        frozen_by=frozen_by,
        created_at=rev.created_at,
        last_edited_at=rev.updated_at,
    )


def _leaf(lf: LeafNode, total: Decimal) -> LeafOut:
    return LeafOut(
        id=lf.id,
        item_id=lf.item_id,
        section_id=lf.section_id,
        section_name=lf.section_name,
        section_code=lf.section_code,
        planned_qty=lf.planned_qty,
        unit_mhr=lf.unit_mhr,
        rate_source=lf.rate_source,
        contractor_type=lf.contractor_type,
        contractor_source="override" if lf.contractor_overridden else "inherited",
        is_direct=lf.is_direct,
        is_direct_source="override" if lf.is_direct_overridden else "inherited",
        budget_mhr=lf.budget_mhr,
        share=_share(lf.budget_mhr, total) if lf.is_direct else None,
        window_start=lf.window_start,
        window_end=lf.window_end,
        window_source=lf.window_source,  # type: ignore[arg-type]
        outside_section_dates=lf.window_outside_section,
    )


def _item(i: ItemNode, total: Decimal) -> ItemOut:
    direct = _sum(i.leaves, direct_only=True)
    return ItemOut(
        id=i.id,
        item_id=i.item_id,
        code=i.code,
        description=i.description,
        uom=i.uom,
        planned_qty=sum((lf.planned_qty for lf in i.leaves), ZERO),
        contractor_type=i.contractor_type,
        contractor_source="inherited" if i.contractor_inherited else "item",
        is_direct=i.is_direct,
        catalog_item_id=i.catalog_item_id,
        empty_rate_count=sum(1 for lf in i.leaves if not lf.is_rated),
        budget_mhr=_sum(i.leaves),
        direct_budget_mhr=direct,
        share=_share(direct, total),
        leaves=[_leaf(lf, total) for lf in i.leaves],
    )


def _group(g: GroupNode, total: Decimal) -> GroupOut:
    leaves = [lf for i in g.items for lf in i.leaves]
    direct = _sum(leaves, direct_only=True)
    return GroupOut(
        id=g.id,
        group_id=g.group_id,
        code=g.code,
        name=g.name,
        discipline_id=g.discipline_id,
        budget_mhr=_sum(leaves),
        direct_budget_mhr=direct,
        share=_share(direct, total),
        items=[_item(i, total) for i in g.items],
    )


def _discipline(d: DisciplineNode, total: Decimal) -> DisciplineOut:
    leaves = [lf for g in d.groups for i in g.items for lf in i.leaves]
    direct = _sum(leaves, direct_only=True)
    return DisciplineOut(
        id=d.id,
        discipline_id=d.discipline_id,
        code=d.code,
        name=d.name,
        color=d.color,
        default_contractor_type=d.default_contractor_type,
        distribution=d.distribution,  # type: ignore[arg-type]
        budget_mhr=_sum(leaves),
        direct_budget_mhr=direct,
        share=_share(direct, total),
        groups=[_group(g, total) for g in d.groups],
    )


def _findings(items: Iterable[Finding]) -> list[FindingOut]:
    return [FindingOut(code=f.code, count=f.count, node_ids=list(f.node_ids)) for f in items]


#: CEO B3 eki: "taslak yok" durumu da bulgu KODU (istemci turetmesin).
BLOCKER_NO_DRAFT = "no_draft"


def _no_draft(state: BudgetState) -> list[FindingOut]:
    return [FindingOut(code=BLOCKER_NO_DRAFT, count=1, node_ids=[])]


def totals(tree: BudgetTree) -> BudgetTotals:
    leaves = [lf for *_, lf in tree.leaves()]
    return BudgetTotals(
        direct_budget_mhr=_sum(leaves, direct_only=True),
        indirect_budget_mhr=sum((lf.budget_mhr for lf in leaves if not lf.is_direct), ZERO),
        item_count=sum(len(g.items) for d in tree.disciplines for g in d.groups),
        leaf_count=len(leaves),
        empty_rate_leaf_count=sum(1 for lf in leaves if not lf.is_rated),
    )


async def _masked_tree(
    session: AsyncSession, tree: BudgetTree, scope: DisciplineScope
) -> BudgetTree:
    """Kisitlida kapsam disi katalog baglarini gizler (`budget_scope.mask_catalog_links`)."""
    ids = catalog_link_ids(tree) if scope.is_restricted else set()
    if not ids:
        return tree
    rows = await session.execute(
        select(EvCatalogItem.id, EvCatalogItem.discipline_id).where(EvCatalogItem.id.in_(ids))
    )
    return mask_catalog_links(tree, scope, dict(rows.tuples().all()))


async def budget_view(
    session: AsyncSession, state: BudgetState, scope: DisciplineScope = UNRESTRICTED
) -> BudgetView:
    state = replace(state, tree=await _masked_tree(session, state.tree, scope))
    t = totals(state.tree)
    total = t.direct_budget_mhr
    return BudgetView(
        revision=await revision_out(session, state.revision) if state.revision else None,
        editable=state.editable and not state.site_completed,
        boq_synced_at=state.boq_synced_at,
        totals=t,
        disciplines=[_discipline(d, total) for d in state.tree.disciplines],
        freeze_blockers=_findings(state.tree.blockers) if state.editable else _no_draft(state),
        freeze_warnings=_findings(state.tree.warnings) if state.editable else [],
    )


# ------------------------------------------------------------------ onizleme


def _week(w: WeekLoad, planned: Mapping[date, int] | None) -> WeekOut:
    return WeekOut(
        week_no=w.week_no,
        week_start=w.week_start,
        week_end=w.week_end,
        mhr=w.mhr,
        working_days=w.working_days,
        required_people=w.required_people,
        planned_people=None if planned is None else planned.get(w.week_start, 0),
    )


def _series(s: SeriesPreview, planned: Mapping[date, int] | None = None) -> SeriesOut:
    return SeriesOut(
        budget_mhr=s.budget_mhr,
        start=s.start,
        end=s.end,
        days=[
            DayOut(
                day=d,
                mhr=mhr,
                cumulative_mhr=s.cumulative[d],
                planned_pct_cum=s.planned_pct_cum[d],
            )
            for d, mhr in sorted(s.daily.items())
        ],
        weeks=[_week(w, planned) for w in s.weeks],
        peak_week=_week(s.peak_week, planned) if s.peak_week else None,
    )


def preview_out(result: PreviewResult) -> PreviewOut:
    by_node = {d.id: d for d in result.tree.disciplines}
    p = result.preview
    # Belirlenimli sira: AGAC sirasi (donmus egriler DB satir sirasiyla gelir); agacta olmayan
    # anahtarlar sona, kendi sirasiyla (kararli siralama).
    tree_order = {d.id: n for n, d in enumerate(result.tree.disciplines)}
    ordered = sorted(p.disciplines.items(), key=lambda kv: tree_order.get(kv[0], len(tree_order)))
    return PreviewOut(
        start=p.start,
        end=p.end,
        disciplines=[
            DisciplinePreviewOut(
                discipline_node_id=str(key),
                discipline_id=by_node[key].discipline_id if key in by_node else None,
                code=by_node[key].code if key in by_node else None,
                name=by_node[key].name if key in by_node else None,
                color=by_node[key].color if key in by_node else None,
                distribution=by_node[key].distribution if key in by_node else "linear",  # type: ignore[arg-type]
                share=dp.share,
                series=_series(dp.series),
            )
            for key, dp in ordered
        ],
        total=_series(p.total, result.planned_people),
        indirect_budget_mhr=p.indirect_budget_mhr,
        unspreadable=[str(x) for x in p.unspreadable],
    )


def schedule_out(
    tree: BudgetTree, sections, off_days: Iterable[int], holidays: Iterable[date]
) -> ScheduleOut:  # noqa: ANN001
    bars: dict[tuple[str, uuid.UUID | None], BarOut] = {}
    for d, _, _, lf in tree.leaves():
        if d.discipline_id is None:
            continue
        key = (d.id, lf.section_id)
        prev = bars.get(key)
        budget = (prev.budget_mhr if prev else ZERO) + (lf.budget_mhr if lf.is_direct else ZERO)
        bars[key] = BarOut(
            discipline_node_id=d.id,
            discipline_id=d.discipline_id,
            section_id=lf.section_id,
            section_name=lf.section_name,
            start_date=lf.window_start,
            end_date=lf.window_end,
            source=lf.window_source,  # type: ignore[arg-type]
            outside_section_dates=lf.window_outside_section,
            budget_mhr=budget,
        )
    return ScheduleOut(
        sections=[
            SectionOut(
                id=s.id,
                name=s.name,
                start_date=s.start_date,
                end_date=s.end_date,
                planned_worker_count=s.planned_worker_count,
            )
            for s in sorted(sections, key=lambda s: (s.sort_order, s.name))
        ],
        bars=list(bars.values()),
        weekly_off_days=sorted(off_days),
        holidays=sorted(holidays),
    )


# ------------------------------------------------------------------ fark


async def diff_out(session: AsyncSession, result: RevisionDiff) -> RevisionDiffOut:
    rows = []
    for x in result.leaves:
        removed = x.reason in ("removed", "moved_out")
        rows.append(
            LeafDiffOut(
                leaf_id=x.leaf.id,
                item_code=x.item.code,
                item_description=x.item.description,
                section_name=x.leaf.section_name,
                uom=x.item.uom,
                prev_qty=x.prev_qty,
                qty=None if removed else x.leaf.planned_qty,
                prev_unit_mhr=x.prev_rate,
                unit_mhr=None if removed else x.leaf.unit_mhr,
                prev_budget_mhr=x.prev_budget,
                budget_mhr=x.leaf.budget_mhr,
                delta_mhr=x.leaf.budget_mhr - x.prev_budget,
                reason=x.reason,  # type: ignore[arg-type]
            )
        )
    return RevisionDiffOut(
        revision=await revision_out(session, result.revision),
        against=await revision_out(session, result.against) if result.against else None,
        direct_before_mhr=result.direct_before,
        direct_after_mhr=result.direct_after,
        direct_delta_mhr=result.direct_after - result.direct_before,
        leaves=rows,
    )

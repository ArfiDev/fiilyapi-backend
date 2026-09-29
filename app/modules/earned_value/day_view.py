"""Gunun dagitim gorunumu: satirlar, kodlar, hucreler, toplamlar, kilit, ilerleme onizlemesi.

Ilerleme (kazanilmis / harcanan / PF) motorun KENDISIYLE hesaplanir (tek gercek kaynak,
§3): donmus baseline agaci → motor dugumleri; gunun gunluk satirlari (taslak dahil,
K13) → miktar; hucreler → saat (kodun kuraliyla).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.day_hooks import SubmitContext
from app.core.discipline_scope import UNRESTRICTED, DisciplineScope
from app.modules.earned_value import budget_repository as repo
from app.modules.earned_value import diary_adapter as adp
from app.modules.earned_value.budget_engine import calendar_bounds, to_engine_nodes
from app.modules.earned_value.budget_scope import node_ids, prune_tree
from app.modules.earned_value.budget_tree import BudgetTree, leaf_node_id
from app.modules.earned_value.engine import (
    AllocationRule,
    CalendarSettings,
    EngineInput,
    HoursEntry,
    ProjectCalendar,
    QtyEntry,
    compute_daily_report,
)
from app.modules.earned_value.schemas_day import (
    CellOut,
    CodeNodeOut,
    CodeOut,
    DayView,
    ItemProgressOut,
    LeafProgressOut,
    LockOut,
    ProgressOut,
    RowOut,
    SubmitCheckOut,
    SubmitReasonOut,
    TotalsOut,
    UnlockOut,
    UserRef,
)
from app.modules.site_diary.models import SiteDiaryLine
from app.modules.users.models import User

ZERO = Decimal(0)


def code_nodes(tree: BudgetTree) -> list[CodeNodeOut]:
    out: list[CodeNodeOut] = []
    for d in tree.disciplines:
        out.append(
            CodeNodeOut(
                id=d.id,
                parent_id=None,
                level=1,
                code=d.code,
                label=d.name or "Disiplinsiz",
                uom=None,
                has_rate=None,
                unit_mhr=None,
            )
        )
        for g in d.groups:
            out.append(
                CodeNodeOut(
                    id=g.id,
                    parent_id=d.id,
                    level=2,
                    code=None,
                    label=g.name,
                    uom=None,
                    has_rate=None,
                    unit_mhr=None,
                )
            )
            for i in g.items:
                out.append(
                    CodeNodeOut(
                        id=i.id,
                        parent_id=g.id,
                        level=3,
                        code=i.code,
                        label=i.description,
                        uom=i.uom,
                        has_rate=None,
                        unit_mhr=None,
                    )
                )
                out.extend(
                    CodeNodeOut(
                        id=lf.id,
                        parent_id=i.id,
                        level=4,
                        code=None,
                        label=lf.section_name or "Bölümsüz",
                        uom=i.uom,
                        has_rate=lf.is_rated,
                        unit_mhr=lf.unit_mhr,
                    )
                    for lf in i.leaves
                )
    return out


async def _user_ref(session: AsyncSession, user_id: uuid.UUID | None) -> UserRef | None:
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    return UserRef(id=user.id, full_name=user.full_name) if user else None


async def lock_out(session: AsyncSession, site_id: uuid.UUID, day: date) -> LockOut:
    state = await adp.lock_state(session, site_id, day)
    a, u = state.approval, state.unlock
    return LockOut(
        locked=state.locked,
        report_date=a.report_date if a else None,
        approved_at=a.approved_at if a else None,
        approved_by=await _user_ref(session, a.approved_by_user_id) if a else None,
        unlock=UnlockOut(
            unlocked_at=u.unlocked_at,
            unlocked_by=await _user_ref(session, u.unlocked_by_user_id),
            reason=u.reason,
        )
        if u
        else None,
    )


async def _progress(
    session: AsyncSession,
    tree: BudgetTree,
    day: date,
    entry_id: uuid.UUID | None,
    saved: adp.SavedDay,
) -> ProgressOut:
    nodes = to_engine_nodes(tree)
    known = {n.id for n in nodes}
    qty: list[QtyEntry] = []
    if entry_id is not None:
        lines = (
            await session.execute(select(SiteDiaryLine).where(SiteDiaryLine.entry_id == entry_id))
        ).scalars()
        for line in lines:
            if line.boq_item_id is None:
                continue
            node = leaf_node_id(line.boq_item_id, getattr(line, "section_id", None))
            if node in known:
                qty.append(QtyEntry(node, day, line.quantity))
    rules = {c.node_id: c.rule for c in saved.codes}
    by_node: dict[str, Decimal] = {}
    for cell in saved.cells:
        if cell.node_id in known:
            by_node[cell.node_id] = by_node.get(cell.node_id, ZERO) + cell.hours
    hours = [
        HoursEntry(node, day, h, AllocationRule(rules.get(node, "direct")))
        for node, h in by_node.items()
    ]
    if not nodes:
        return ProgressOut(leaves=[], earned_day=ZERO, spent_day=ZERO, pf_day=None)
    report = compute_daily_report(
        EngineInput(
            calendar=CalendarSettings(day, day),
            nodes=nodes,
            qty_entries=qty,
            hours_entries=hours,
        ),
        day,
    )
    leaves = [
        LeafProgressOut(
            node_id=lf.id,
            qty_day=report.nodes[lf.id].qty_day,
            earned_day=report.nodes[lf.id].earned_day,
            spent_day=report.nodes[lf.id].spent_day,
            pf_day=report.nodes[lf.id].pf_day,
        )
        for *_, lf in tree.leaves()
        if lf.id in report.nodes
    ]
    items = [
        ItemProgressOut(
            node_id=i.id,
            qty_day=report.nodes[i.id].qty_day,
            earned_day=report.nodes[i.id].earned_day,
            spent_day=report.nodes[i.id].spent_day,
            pf_day=report.nodes[i.id].pf_day,
        )
        for d in tree.disciplines
        for g in d.groups
        for i in g.items
        if i.id in report.nodes
    ]
    earned = sum(
        (report.nodes[d.id].earned_day for d in tree.disciplines if d.id in report.nodes), ZERO
    )
    spent = report.totals.spent_day
    return ProgressOut(
        leaves=leaves,
        earned_day=earned,
        spent_day=spent,
        pf_day=None if spent == 0 else earned / spent,
        items=items,
    )


async def _day_numbers(
    session: AsyncSession, site_id: uuid.UUID, tree: BudgetTree, day: date
) -> tuple[int | None, int | None]:
    bounds = calendar_bounds(tree, [day])
    if bounds is None:
        return None, None
    cal_cfg = await repo.load_calendar(session, site_id)
    cal = ProjectCalendar(
        CalendarSettings(bounds[0], bounds[1], week_start_dow=cal_cfg.week_start_dow)
    )
    return cal.day_no(day), cal.week_no(day)


def scoped_saved(saved: adp.SavedDay, visible: frozenset[str]) -> adp.SavedDay:
    """Kayitli kodlar/hucreler yalniz GORUNUR dugumler icin (satirlar/not ayni kalir)."""
    return replace(
        saved,
        codes=[c for c in saved.codes if c.node_id in visible],
        cells=[c for c in saved.cells if c.node_id in visible],
    )


async def build_view(
    session: AsyncSession,
    site_id: uuid.UUID,
    day: date,
    actor: User,
    scope: DisciplineScope = UNRESTRICTED,
) -> DayView:
    """Gunun gorunumu. Kisitli kapsamda (DSC-B1, K4/K5): aktif (DONMUS) agac `d:` kokunden
    budanir; kodlar/hucreler/ilerleme budanmis agactan; `totals` (kaynak/dagitilan/dagitilmamis
    KAYNAK SAATI) SUZULMEZ — baska disiplin payi ORTUK kalir (K4)."""
    full_tree = await adp.active_tree(session, site_id)
    tree = prune_tree(full_tree, scope, None, True) if full_tree else None
    rev = await adp.active_revision(session, site_id)
    live = await adp.source_rows(session, site_id, day)
    full_saved = await adp.load_saved(session, site_id, day)
    saved = (
        scoped_saved(full_saved, node_ids(tree) if tree else frozenset())
        if scope.is_restricted
        else full_saved
    )
    saved_rows = {(r.kind, r.personnel_id or r.subcontractor_id): r for r in saved.rows}
    by_row_id = {r.id: (r.kind, r.personnel_id or r.subcontractor_id) for r in saved.rows}
    labels = {n.id: n for n in code_nodes(tree)} if tree else {}
    # 🔴 Uyari TAM etiket kumesiyle hesaplanir: budanmis kumeyle hesaplansaydi gorunmeyen bir
    # kod "baseline'da yok: i:<uuid>" diye SIZARDI. Kisitlida "tam kumede de olmayan" (asili)
    # kodlar da gosterilmez — kimin oldugu ayirt edilemez (fail-closed).
    all_labels = {n.id for n in code_nodes(full_tree)} if full_tree else set()
    entry = await adp.diary_entry(session, site_id, day)
    source = sum((r.hours for r in live), ZERO)
    allocated = adp.allocated_hours(full_saved)
    rows = []
    for r in live:
        snap = saved_rows.get((r.kind, r.ref_id))
        rows.append(
            RowOut(
                kind=r.kind,  # type: ignore[arg-type]
                ref_id=r.ref_id,
                label=r.label,
                trade=r.trade,
                source=r.source,
                subcontractor_name=r.subcontractor_name,
                headcount=r.headcount,
                hours=r.hours,
                saved_hours=snap.source_hours if snap else None,
                changed=bool(snap and snap.source_hours != r.hours),
            )
        )
    warnings = adp.duplicate_firm_warnings(live)
    warnings += [
        f"İş kodu aktif baseline'da yok: {c.node_id}"
        for c in (saved.codes if not scope.is_restricted else [])
        if c.node_id not in all_labels
    ]
    day_no, week_no = (
        await _day_numbers(session, site_id, full_tree, day) if full_tree else (None, None)
    )
    submit = None
    if entry is not None:
        reasons = await adp.submit_blockers(
            session, SubmitContext(entry.id, site_id, day, actor.id)
        )
        submit = SubmitCheckOut(
            can_submit=not reasons,
            reasons=[r.message for r in reasons],
            reason_items=[SubmitReasonOut(code=r.code, message=r.message) for r in reasons],
        )
    return DayView(
        day=day,
        day_no=day_no,
        week_no=week_no,
        has_baseline=full_tree is not None,
        revision_number=rev.number if rev else None,
        lock=await lock_out(session, site_id, day),
        rows=rows,
        codes=[
            CodeOut(
                node_id=c.node_id,
                rule=c.rule,  # type: ignore[arg-type]
                label=labels[c.node_id].label if c.node_id in labels else None,
                level=labels[c.node_id].level if c.node_id in labels else None,
            )
            for c in saved.codes
        ],
        cells=[
            CellOut(
                kind=by_row_id[c.row_id][0],
                ref_id=by_row_id[c.row_id][1],
                node_id=c.node_id,
                hours=c.hours,
            )  # type: ignore[arg-type]
            for c in saved.cells
        ],
        totals=TotalsOut(
            source_hours=source, allocated_hours=allocated, unallocated_hours=source - allocated
        ),
        unallocated_reason=saved.note.unallocated_reason if saved.note else None,
        warnings=warnings,
        progress=await _progress(session, tree, day, entry.id if entry else None, saved)
        if tree
        else None,
        submit=submit,
    )

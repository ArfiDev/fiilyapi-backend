"""Butce islemleri: katalog onerisi / bosları doldur (B1-4), onizleme, dondurma (B1-7), fark.

Hepsi `budget_service.load_state` agacini kullanir — kural kopyasi YOK.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import UNRESTRICTED, DisciplineScope, visible_item_set
from app.core.errors import ConflictError, EarnedValueValidationError
from app.modules.catalog.models import EvCatalogItem
from app.modules.earned_value import budget_repository as repo
from app.modules.earned_value import budget_service as svc
from app.modules.earned_value import guards
from app.modules.earned_value.access import SiteContext
from app.modules.earned_value.budget_scope import item_ids, prune_tree
from app.modules.earned_value.budget_snapshot import load_curves, load_leaves, snapshot_rows
from app.modules.earned_value.budget_tree import (
    BudgetTree,
    ItemNode,
    LeafNode,
    RevisionInputs,
    build_tree,
)
from app.modules.earned_value.engine import (
    Distribution,
    SpreadLeaf,
    SpreadPreview,
    compute_spread_preview,
    preview_from_curves,
)
from app.modules.earned_value.labels import normalize_label  # tek kural (katalog tekilligi de)
from app.modules.earned_value.models import (
    EvItemSettings,
    EvLeafSettings,
    EvRevision,
    RateSource,
    RevisionStatus,
)
from app.modules.users.models import User

ZERO = Decimal(0)

# ------------------------------------------------------------ katalog eslesmesi


@dataclass(frozen=True, slots=True)
class Candidate:
    item: EvCatalogItem
    match: str  # linked | exact | partial


def _candidates(
    catalog: list[EvCatalogItem],
    node: ItemNode,
    discipline_id: uuid.UUID | None,
    scope: DisciplineScope = UNRESTRICTED,
) -> list[Candidate]:
    name, uom = normalize_label(node.description), normalize_label(node.uom)
    out: list[Candidate] = []
    for c in catalog:
        if node.catalog_item_id == c.id:
            # Kisitlida capraz bagli (kapsam disi disiplin) katalog kalemi aday OLMAZ (Ü8).
            if not scope.is_restricted or c.discipline_id in (scope.discipline_ids or ()):
                out.append(Candidate(c, "linked"))
            continue
        if discipline_id is not None and c.discipline_id != discipline_id:
            continue
        if normalize_label(c.uom) != uom:
            continue
        cname = normalize_label(c.name)
        if cname == name:
            out.append(Candidate(c, "exact"))
        elif cname in name or name in cname:
            out.append(Candidate(c, "partial"))
    order = {"linked": 0, "exact": 1, "partial": 2}
    return sorted(out, key=lambda x: (order[x.match], x.item.name))


async def _catalog(session: AsyncSession) -> list[EvCatalogItem]:
    return list((await session.execute(select(EvCatalogItem))).scalars())


def _find_item(tree: BudgetTree, item_id: uuid.UUID) -> tuple[uuid.UUID | None, ItemNode] | None:
    for d in tree.disciplines:
        for g in d.groups:
            for i in g.items:
                if i.item_id == item_id:
                    return d.discipline_id, i
    return None


async def suggestions(
    session: AsyncSession,
    ctx: SiteContext,
    item_id: uuid.UUID,
    scope: DisciplineScope = UNRESTRICTED,
) -> list:
    """Oneri adaylari. Kalem BUDANMIS agacta aranir: baska disiplinin kalemi olmayanla
    BIREBIR ayni 422 `BOQ_ITEM_FOREIGN` govdesini verir (Ü7)."""
    state = await svc.load_state(session, ctx, scope=scope)
    found = _find_item(state.tree, item_id)
    if found is None:
        raise EarnedValueValidationError(guards.BOQ_ITEM_FOREIGN)
    disc_id, node = found
    return _candidates(await _catalog(session), node, disc_id, scope)


@dataclass(frozen=True, slots=True)
class FillResult:
    filled_item_count: int
    filled_leaf_count: int
    ambiguous: list[tuple[ItemNode, list[Candidate]]]
    unmatched_count: int


async def fill_from_catalog(session: AsyncSession, ctx: SiteContext, actor: User) -> FillResult:
    """ "Bosları doldur" (B1-4): yalniz BAGLI ya da TEK tam eslesmeli kalem; bag kaydedilir.

    Doldurulan = orani BOS (None) yaprak. Acikca 0 yazilmis oran bir KARARDIR, ezilmez.
    Doldurulacak yaprak olmasa da bir YAZMA ucudur: tamamlanmis santiyede 409 (B1-12).
    """
    await svc._writable_site(session, ctx)  # noqa: SLF001
    state = await svc.load_state(session, ctx)
    catalog = await _catalog(session)
    plan: list[tuple[ItemNode, EvCatalogItem]] = []
    ambiguous: list[tuple[ItemNode, list[Candidate]]] = []
    unmatched = 0
    for d in state.tree.disciplines:
        for g in d.groups:
            for item in g.items:
                if not any(lf.unit_mhr is None for lf in item.leaves):
                    continue
                cands = _candidates(catalog, item, d.discipline_id)
                linked = [c for c in cands if c.match == "linked"]
                exact = [c for c in cands if c.match == "exact"]
                if linked:
                    plan.append((item, linked[0].item))
                elif len(exact) == 1:
                    plan.append((item, exact[0].item))
                elif len(exact) > 1:
                    ambiguous.append((item, exact))
                else:
                    unmatched += 1
    leaf_count = 0
    if plan:
        draft = await svc._draft_for_write(session, ctx, actor)  # noqa: SLF001
        for item, cat in plan:
            await _link(session, draft.id, item.item_id, cat.id)
            for lf in item.leaves:
                if lf.unit_mhr is None:
                    await _set_rate(session, draft.id, lf, cat.standard_unit_mhr)
                    leaf_count += 1
        await svc._touch(session, draft)  # noqa: SLF001
    return FillResult(len(plan), leaf_count, ambiguous, unmatched)


async def _link(
    session: AsyncSession, rev_id: uuid.UUID, item_id: uuid.UUID, catalog_id: uuid.UUID
) -> None:
    row = await session.get(EvItemSettings, (rev_id, item_id))
    if row is None:
        session.add(
            EvItemSettings(
                revision_id=rev_id, boq_item_id=item_id, is_direct=True, catalog_item_id=catalog_id
            )
        )
    else:
        row.catalog_item_id = catalog_id


async def _set_rate(session: AsyncSession, rev_id: uuid.UUID, lf: LeafNode, rate: Decimal) -> None:
    row = await svc._leaf_row(session, rev_id, lf.item_id, lf.section_id)  # noqa: SLF001
    if row is None:
        row = EvLeafSettings(revision_id=rev_id, boq_item_id=lf.item_id, section_id=lf.section_id)
        session.add(row)
    row.unit_mhr = rate
    row.rate_source = RateSource.CATALOG


# ------------------------------------------------------------ onizleme


@dataclass(frozen=True, slots=True)
class PreviewResult:
    tree: BudgetTree
    preview: SpreadPreview
    planned_people: Mapping[date, int]  # hafta basi → bolum plani (sections.planned_worker_count)


def spread_leaves(tree: BudgetTree) -> list[SpreadLeaf]:
    out: list[SpreadLeaf] = []
    for d, _, _, lf in tree.leaves():
        if d.discipline_id is None or lf.window_start is None or lf.window_end is None:
            continue
        if lf.budget_mhr <= 0:
            continue
        out.append(
            SpreadLeaf(
                node_id=lf.id,
                discipline=d.id,
                budget=lf.budget_mhr,
                start=lf.window_start,
                end=lf.window_end,
                distribution=Distribution(d.distribution),
                is_direct=lf.is_direct,
            )
        )
    return out


async def preview(
    session: AsyncSession,
    ctx: SiteContext,
    revision_id: uuid.UUID | None,
    distributions: Mapping[uuid.UUID, str] | None,
    windows: Mapping[tuple[uuid.UUID, uuid.UUID | None], tuple[date, date]] | None,
    scope: DisciplineScope = UNRESTRICTED,
) -> PreviewResult:
    """KALICI OLMAYAN onizleme (frontend istegi 2). Govde ezmeleri yalniz taslakta anlamlidir."""
    calendar = await repo.load_calendar(session, ctx.site.id)
    kwargs = dict(
        week_start_dow=calendar.week_start_dow,
        weekly_holidays=calendar.weekly_off_days,
        extra_holidays=calendar.holidays,
        standard_daily_hours=calendar.standard_daily_hours,
    )
    rev = (
        await svc.get_revision(session, ctx, revision_id)
        if revision_id
        else await svc.current_revision(session, ctx.site.id)
    )
    boq = await repo.load_boq(session, ctx.site.id)
    if rev is not None and rev.status is not RevisionStatus.DRAFT:
        state = await svc.load_state(session, ctx, rev.id, scope)
        leaves = await load_leaves(session, rev.id)
        curves = await load_curves(session, leaves)
        if scope.is_restricted:
            # DSC-B3 Ç2: egriler de budanmis yapraklara suzulur; yoksa `preview_from_curves`
            # esleme olmayan (yabanci) yaprakta ValueError → 500.
            kept = {lf.id for *_, lf in state.tree.leaves()}
            curves = {k: v for k, v in curves.items() if k in kept}
        disc_of = {lf.id: d.id for d, _, _, lf in state.tree.leaves() if lf.id in curves}
        indirect = sum((lf.budget_mhr for *_, lf in state.tree.leaves() if not lf.is_direct), ZERO)
        # Aralik = egriye giren yapraklarin PENCERE birlesimi (taslak onizlemeyle ayni kural;
        # egri yalniz is gunlerini tasir, pencere tatilde baslayabilir).
        spans = [
            (lf.window_start, lf.window_end)
            for *_, lf in state.tree.leaves()
            if lf.id in curves and lf.window_start and lf.window_end
        ]
        result = preview_from_curves(
            curves,
            disc_of,
            indirect_budget_mhr=indirect,
            start=min((a for a, _ in spans), default=None),
            end=max((b for _, b in spans), default=None),
            **kwargs,
        )
        return PreviewResult(state.tree, result, _planned_people(result, boq.sections))
    disciplines = await repo.load_disciplines(session)
    inputs = await repo.load_inputs(session, rev.id) if rev else RevisionInputs()
    inputs = replace(
        inputs,
        distributions={**inputs.distributions, **(distributions or {})},
        windows={**inputs.windows, **(windows or {})},
    )
    tree = build_tree(boq, disciplines, inputs, calendar.is_working_day)
    # DSC-B3: taslak dal budama (budget_service.load_state emsali); yabanci disiplin ezmeleri
    # budanan agacta etkisizdir (Ü7). Kisitsizda agac degismez.
    visible = await visible_item_set(session, scope, item_ids(tree))
    tree = prune_tree(tree, scope, visible, False, calendar.is_working_day)
    result = compute_spread_preview(spread_leaves(tree), **kwargs)
    return PreviewResult(tree, result, _planned_people(result, boq.sections))


def _planned_people(result: SpreadPreview, sections) -> dict[date, int]:  # noqa: ANN001
    """Mockup'taki basamakli "bolum plani" cizgisi: hafta ortasinda aktif bolumlerin
    `planned_worker_count` toplami (BUT:412 ile ayni kural: orta gun = hafta basi + 3)."""
    out: dict[date, int] = {}
    for week in result.total.weeks:
        mid = min(week.week_start + timedelta(days=3), week.week_end)
        out[week.week_start] = sum(
            s.planned_worker_count or 0
            for s in sections
            if s.start_date and s.end_date and s.start_date <= mid <= s.end_date
        )
    return out


# ------------------------------------------------------------ dondurma


async def freeze(
    session: AsyncSession, ctx: SiteContext, actor: User, name: str | None, description: str | None
) -> EvRevision:
    await svc._writable_site(session, ctx)  # noqa: SLF001
    draft = await repo.revision_by_status(session, ctx.site.id, RevisionStatus.DRAFT)
    if draft is None:
        raise ConflictError(guards.NO_DRAFT)
    state = await svc.load_state(session, ctx, draft.id)
    if state.tree.blockers:
        codes = ", ".join(f"{b.code} ({b.count})" for b in state.tree.blockers)
        raise EarnedValueValidationError(f"{guards.FREEZE_BLOCKED}: {codes}")
    calendar = await repo.load_calendar(session, ctx.site.id)
    result = compute_spread_preview(
        spread_leaves(state.tree),
        week_start_dow=calendar.week_start_dow,
        weekly_holidays=calendar.weekly_off_days,
        extra_holidays=calendar.holidays,
        standard_daily_hours=calendar.standard_daily_hours,
    )
    if result.unspreadable:  # blokerlarla ayni kural; savunma
        raise EarnedValueValidationError(f"{guards.FREEZE_BLOCKED}: no_working_day")
    leaves, points = snapshot_rows(draft.id, state.tree, result.leaf_curves)
    session.add_all(leaves)
    await session.flush()
    session.add_all(points)
    active = await repo.revision_by_status(session, ctx.site.id, RevisionStatus.ACTIVE)
    if active is not None:
        active.status = RevisionStatus.ARCHIVED
        await session.flush()
    draft.status = RevisionStatus.ACTIVE
    draft.frozen_at = datetime.now(UTC)
    draft.frozen_by_user_id = actor.id
    draft.name = name
    draft.description = description
    await session.flush()
    # `updated_at` sunucu tarafi `onupdate`tir: flush onu SURESI DOLMUS birakir ve yanit
    # semasi okurken async baglamda tembel yukleme MissingGreenlet verir (olculdu).
    await session.refresh(draft)
    return draft


# ------------------------------------------------------------ revizyon farki


@dataclass(frozen=True, slots=True)
class LeafDiff:
    leaf: LeafNode
    item: ItemNode
    prev_qty: Decimal | None
    prev_rate: Decimal | None
    prev_budget: Decimal
    reason: str  # new | removed | moved_out | qty_changed | rate_changed | qty_and_rate_changed


@dataclass(frozen=True, slots=True)
class RevisionDiff:
    revision: EvRevision
    against: EvRevision | None
    leaves: list[LeafDiff]
    direct_before: Decimal
    direct_after: Decimal


async def diff(
    session: AsyncSession,
    ctx: SiteContext,
    rev_id: uuid.UUID,
    scope: DisciplineScope = UNRESTRICTED,
) -> RevisionDiff:
    """Revizyon farki; iki agac da kapsamla budanir, `_direct` budanmis agactan (Ü3)."""
    rev = await svc.get_revision(session, ctx, rev_id)
    revisions = await repo.list_revisions(session, ctx.site.id)
    prev = max(
        (r for r in revisions if r.number < rev.number and r.status is not RevisionStatus.DRAFT),
        key=lambda r: r.number,
        default=None,
    )
    after = await svc.load_state(session, ctx, rev.id, scope)
    before = await svc.load_state(session, ctx, prev.id, scope) if prev else None
    now = {lf.id: (i, lf) for *_, i, lf in after.tree.leaves()}
    # S8 (DSC-B3): kisitlida "removed" iki anlama gelir; yaprak KISITSIZ sonraki agacta HALA
    # VARSA baska disipline tasinmistir (`moved_out`, hedef verilmez). Atamasizda olusamaz.
    full_now: set[str] = set()
    if scope.is_restricted:
        full = await svc.load_state(session, ctx, rev.id)
        full_now = {lf.id for *_, lf in full.tree.leaves()}
    old = {lf.id: (i, lf) for *_, i, lf in before.tree.leaves()} if before else {}
    out: list[LeafDiff] = []
    for key in sorted(set(now) | set(old)):
        n, o = now.get(key), old.get(key)
        reason = _reason(n[1] if n else None, o[1] if o else None)
        if reason == "removed" and key in full_now:
            reason = "moved_out"
        if reason is None:
            continue
        item, leaf = n if n else o  # type: ignore[misc]
        out.append(
            LeafDiff(
                leaf=leaf if n else replace(leaf, budget_mhr=ZERO, planned_qty=ZERO),
                item=item,
                prev_qty=o[1].planned_qty if o else None,
                prev_rate=o[1].unit_mhr if o else None,
                prev_budget=o[1].budget_mhr if o else ZERO,
                reason=reason,
            )
        )
    return RevisionDiff(
        revision=rev,
        against=prev,
        leaves=out,
        direct_before=_direct(before.tree) if before else ZERO,
        direct_after=_direct(after.tree),
    )


def _reason(new: LeafNode | None, old: LeafNode | None) -> str | None:
    if new is None:
        return "removed"
    if old is None:
        return "new"
    qty, rate = new.planned_qty != old.planned_qty, new.unit_mhr != old.unit_mhr
    if qty and rate:
        return "qty_and_rate_changed"
    if qty:
        return "qty_changed"
    if rate:
        return "rate_changed"
    return None


def _direct(tree: BudgetTree) -> Decimal:
    return sum((lf.budget_mhr for *_, lf in tree.leaves() if lf.is_direct), ZERO)

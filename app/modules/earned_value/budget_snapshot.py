"""Donmus baseline (K8): dondurma aninda yaprak fotografi + yaprak × gun egrisi.

Okuma yolu (`frozen_tree`) agaci CANLI BOQ'tan DEGIL snapshot'tan kurar: donmus revizyon
BOQ sonradan degisse de dondugu gunku miktari ve pencereyi gosterir. Revizyonun kendi
girdi satirlari (esleme, oranlar, ezmeler) dondurma sonrasi DEGISMEZ (yazma yollari
yalniz taslagi kabul eder), bu yuzden agac onlarla + snapshot miktarlariyla yeniden
kurulur ve pencere/butce snapshot'tan birebir basilir.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqGroup
from app.modules.earned_value import budget_repository as repo
from app.modules.earned_value.budget_tree import (
    BoqSnapshot,
    BudgetTree,
    DisciplineInfo,
    GroupInfo,
    ItemInfo,
    SectionInfo,
    build_tree,
    leaf_node_id,
)
from app.modules.earned_value.models import EvBaselineCurve, EvBaselineLeaf, EvRevision

MISSING_GROUP_NAME = "—"


async def load_leaves(session: AsyncSession, revision_id: uuid.UUID) -> list[EvBaselineLeaf]:
    rows = await session.execute(
        select(EvBaselineLeaf)
        .where(EvBaselineLeaf.revision_id == revision_id)
        .order_by(EvBaselineLeaf.boq_item_id, EvBaselineLeaf.section_id)
    )
    return list(rows.scalars())


async def load_curves(
    session: AsyncSession, leaves: Sequence[EvBaselineLeaf]
) -> dict[str, dict[date, Decimal]]:
    """Yaprak dugum kimligi → {gun: planned_mhr}."""
    by_id = {lf.id: leaf_node_id(lf.boq_item_id, lf.section_id) for lf in leaves}
    if not by_id:
        return {}
    rows = await session.execute(
        select(EvBaselineCurve)
        .where(EvBaselineCurve.leaf_id.in_(list(by_id)))
        .order_by(EvBaselineCurve.leaf_id, EvBaselineCurve.day)
    )
    out: dict[str, dict[date, Decimal]] = {}
    for r in rows.scalars():
        out.setdefault(by_id[r.leaf_id], {})[r.day] = r.mhr
    return out


async def frozen_tree(
    session: AsyncSession,
    rev: EvRevision,
    disciplines: Sequence[DisciplineInfo],
    is_working_day: Callable[[date], bool],
) -> BudgetTree:
    leaves = await load_leaves(session, rev.id)
    group_ids = {lf.boq_group_id for lf in leaves}
    # Ad VE sira BOQ'tan canli (sira dondurulmaz; ad zaten canliydi). 🔴 Eskiden sira UUID
    # metnine gore kuruluyordu → donmus gorunumde grup sirasi/kodu RASTGELE (§3.15 S1).
    groups = (
        {
            gid: (name, sort_order)
            for gid, name, sort_order in (
                await session.execute(
                    select(BoqGroup.id, BoqGroup.name, BoqGroup.sort_order).where(
                        BoqGroup.id.in_(group_ids)
                    )
                )
            ).all()
        }
        if group_ids
        else {}
    )
    boq = _snapshot_boq(leaves, groups)
    inputs = await repo.load_inputs(session, rev.id)
    tree = build_tree(boq, disciplines, inputs, is_working_day)
    frozen = {leaf_node_id(lf.boq_item_id, lf.section_id): lf for lf in leaves}
    return BudgetTree(_stamp(tree, frozen), (), ())


#: Silinmis BOQ grubunun sirasi: sona (ad ve kimlikle deterministik).
_MISSING_GROUP_ORDER = 1_000_000


def _snapshot_boq(
    leaves: Sequence[EvBaselineLeaf], groups: Mapping[uuid.UUID, tuple[str, int]]
) -> BoqSnapshot:
    items: dict[uuid.UUID, ItemInfo] = {}
    qty: dict[uuid.UUID, Decimal] = {}
    alloc: dict[uuid.UUID, dict[uuid.UUID, Decimal]] = {}
    sections: dict[uuid.UUID, SectionInfo] = {}
    for n, lf in enumerate(sorted(leaves, key=lambda x: (x.item_code, x.section_name or ""))):
        qty[lf.boq_item_id] = qty.get(lf.boq_item_id, Decimal(0)) + lf.planned_qty
        items[lf.boq_item_id] = ItemInfo(
            lf.boq_item_id,
            lf.boq_group_id,
            lf.item_code,
            lf.item_description,
            lf.uom,
            Decimal(0),
            n,
        )
        if lf.section_id is not None:
            alloc.setdefault(lf.boq_item_id, {})[lf.section_id] = lf.planned_qty
            sections[lf.section_id] = SectionInfo(
                lf.section_id, lf.section_name or "", None, None, None, len(sections)
            )
    return BoqSnapshot(
        groups=tuple(
            sorted(
                (
                    GroupInfo(g, *groups.get(g, (MISSING_GROUP_NAME, _MISSING_GROUP_ORDER)))
                    for g in {i.group_id for i in items.values()}
                ),
                key=lambda gi: (gi.sort_order, gi.name, str(gi.id)),
            )
        ),
        items=tuple(replace(i, quantity=qty[i.id]) for i in items.values()),
        allocations=alloc,
        sections=tuple(sections.values()),
    )


def _stamp(tree: BudgetTree, frozen: Mapping[str, EvBaselineLeaf]) -> tuple:
    """Pencere, butce, oran ve siniflamayi snapshot'tan BIREBIR bas (K8)."""

    def leaf(lf):  # noqa: ANN001, ANN202
        snap = frozen.get(lf.id)
        if snap is None:
            return lf
        return replace(
            lf,
            planned_qty=snap.planned_qty,
            unit_mhr=snap.unit_mhr,
            rate_source=snap.rate_source,
            contractor_type=snap.contractor_type,
            is_direct=snap.is_direct,
            budget_mhr=snap.budget_mhr,
            window_start=snap.window_start,
            window_end=snap.window_end,
            window_source="snapshot" if snap.window_start else None,
        )

    return tuple(
        replace(
            d,
            groups=tuple(
                replace(
                    g,
                    items=tuple(
                        replace(i, leaves=tuple(leaf(x) for x in i.leaves)) for i in g.items
                    ),
                )
                for g in d.groups
            ),
        )
        for d in tree.disciplines
    )


def snapshot_rows(
    revision_id: uuid.UUID,
    tree: BudgetTree,
    curves: Mapping[str, Mapping[date, Decimal]],
) -> tuple[list[EvBaselineLeaf], list[EvBaselineCurve]]:
    """Dondurma: agacin HER yapragi (dolayli dahil) + egriye giren yapraklarin gunleri."""
    leaves: list[EvBaselineLeaf] = []
    points: list[EvBaselineCurve] = []
    for d, g, i, lf in tree.leaves():
        # Disiplinsiz grup yalniz DOLAYLI/butcesiz yaprak tasiyabilir (direct butceli olan
        # dondurma ENGELIDIR, §3.9 B1-7 + §3.10 F0-2); fotografa discipline_id=NULL girer.
        row = EvBaselineLeaf(
            id=uuid.uuid4(),
            revision_id=revision_id,
            boq_item_id=lf.item_id,
            boq_group_id=g.group_id,
            section_id=lf.section_id,
            discipline_id=d.discipline_id,
            item_code=i.code,
            item_description=i.description,
            section_name=lf.section_name,
            uom=i.uom,
            planned_qty=lf.planned_qty,
            unit_mhr=lf.unit_mhr,
            rate_source=lf.rate_source,
            contractor_type=lf.contractor_type,
            is_direct=lf.is_direct,
            distribution=d.distribution,
            window_start=lf.window_start,
            window_end=lf.window_end,
            budget_mhr=lf.budget_mhr,
        )
        leaves.append(row)
        for day, mhr in sorted(curves.get(lf.id, {}).items()):
            if mhr != 0:
                points.append(EvBaselineCurve(leaf_id=row.id, day=day, mhr=mhr))
    return leaves, points

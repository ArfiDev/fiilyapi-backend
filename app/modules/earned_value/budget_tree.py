"""Butce agaci — BOQ + revizyon girdilerinden SAF kurulum (DB yok; `budget_repository` besler).

K2 hiyerarsisi: L1 disiplin · L2 BOQ grubu · L3 BOQ kalemi (is tipi) · L4 kalem × bolum.
Dugum kimligi (§3.9 kabul): `d:<disiplin|none>` · `g:<grup>` · `i:<kalem>` ·
`l:<kalem>:<bolum|none>`.

## Kurallar (hepsi spec'ten; kaynak satirlarda)
* Miktar BOQ bolum tahsisinden gelir, SALT OKUNUR (§2.5). Tahsis edilmemis kalan
  "Bolumsuz" yapraktir (B1-3).
* Kendi/Taseron + Dogrudan/Dolayli L3'te atanir, yaprakta ezilir (K3); L3'te atanmamissa
  disiplinin varsayilani · dogrudan. Disiplinsiz grupta varsayilan `own`.
* Butce = planned_qty × unit_mhr; oransiz (bos/0) yaprak butceye 0 katar (K12).
* Pencere (B1-3): ezme > bolum tarihleri > (Bolumsuz yaprak) disiplinin pencerelerinin
  birlesimi > yok.
* Dondurma engelleri / uyarilari (B1-7) ayni kurulumdan cikar — ayri bir kural kopyasi YOK.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal

from app.modules.earned_value.engine import ContractorType
from app.modules.earned_value.models import DEFAULT_DISTRIBUTION, RateSource

ZERO = Decimal(0)
NONE_KEY = "none"


def discipline_node_id(discipline_id: uuid.UUID | None) -> str:
    return f"d:{discipline_id or NONE_KEY}"


def group_node_id(group_id: uuid.UUID) -> str:
    return f"g:{group_id}"


def item_node_id(item_id: uuid.UUID) -> str:
    return f"i:{item_id}"


def leaf_node_id(item_id: uuid.UUID, section_id: uuid.UUID | None) -> str:
    return f"l:{item_id}:{section_id or NONE_KEY}"


# ------------------------------------------------------------------ girdiler


@dataclass(frozen=True, slots=True)
class SectionInfo:
    id: uuid.UUID
    name: str
    start_date: date | None
    end_date: date | None
    planned_worker_count: int | None
    sort_order: int
    code: str | None = None  # CEO B3 eki: yaprak kodu (bolum kodu)


@dataclass(frozen=True, slots=True)
class GroupInfo:
    id: uuid.UUID
    name: str
    sort_order: int


@dataclass(frozen=True, slots=True)
class ItemInfo:
    id: uuid.UUID
    group_id: uuid.UUID
    code: str
    description: str
    unit: str
    quantity: Decimal
    sort_order: int


@dataclass(frozen=True, slots=True)
class DisciplineInfo:
    id: uuid.UUID
    code: str
    name: str
    color: str
    default_contractor_type: ContractorType
    sort_order: int


@dataclass(frozen=True, slots=True)
class ItemSetting:
    contractor_type: ContractorType | None = None
    is_direct: bool = True
    catalog_item_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class LeafSetting:
    unit_mhr: Decimal | None = None
    rate_source: RateSource | None = None
    contractor_type: ContractorType | None = None
    is_direct: bool | None = None


LeafKey = tuple[uuid.UUID, uuid.UUID | None]
WindowKey = tuple[uuid.UUID, uuid.UUID | None]


@dataclass(frozen=True, slots=True)
class RevisionInputs:
    group_disciplines: Mapping[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    items: Mapping[uuid.UUID, ItemSetting] = field(default_factory=dict)
    leaves: Mapping[LeafKey, LeafSetting] = field(default_factory=dict)
    distributions: Mapping[uuid.UUID, str] = field(default_factory=dict)
    windows: Mapping[WindowKey, tuple[date, date]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BoqSnapshot:
    groups: Sequence[GroupInfo]
    items: Sequence[ItemInfo]
    allocations: Mapping[uuid.UUID, Mapping[uuid.UUID, Decimal]]  # item → {section: qty}
    sections: Sequence[SectionInfo]


# ------------------------------------------------------------------ cikti


@dataclass(frozen=True, slots=True)
class LeafNode:
    id: str
    item_id: uuid.UUID
    section_id: uuid.UUID | None
    section_name: str | None
    planned_qty: Decimal
    unit_mhr: Decimal | None
    rate_source: RateSource | None
    contractor_type: ContractorType
    contractor_overridden: bool
    is_direct: bool
    is_direct_overridden: bool
    budget_mhr: Decimal
    window_start: date | None
    window_end: date | None
    window_source: str | None  # override | section | union | None
    #: §3.10 F0-4: ezilen pencere bolum tarihlerinin DISINA tasiyor (uyari, engel DEGIL).
    window_outside_section: bool = False
    section_code: str | None = None

    @property
    def is_rated(self) -> bool:
        return bool(self.unit_mhr)


@dataclass(frozen=True, slots=True)
class ItemNode:
    id: str
    item_id: uuid.UUID
    code: str
    description: str
    uom: str
    contractor_type: ContractorType
    contractor_inherited: bool
    is_direct: bool
    catalog_item_id: uuid.UUID | None
    leaves: tuple[LeafNode, ...]


@dataclass(frozen=True, slots=True)
class GroupNode:
    id: str
    group_id: uuid.UUID
    name: str
    discipline_id: uuid.UUID | None
    items: tuple[ItemNode, ...]
    #: CEO B3 eki: grup "kodu" = santiyedeki SIRA NO (BOQ grubu kod tasimaz; numarayi
    #: `sort_order` sirasi verir — boq/models.py BoqGroup docstring'i).
    code: str | None = None


@dataclass(frozen=True, slots=True)
class DisciplineNode:
    id: str
    discipline_id: uuid.UUID | None
    code: str | None
    name: str | None
    color: str | None
    default_contractor_type: ContractorType
    distribution: str
    groups: tuple[GroupNode, ...]


@dataclass(frozen=True, slots=True)
class Finding:
    """Dondurma engeli ya da uyarisi (frontend istegi 1: kod, sayi, dugum kimlikleri)."""

    code: str
    count: int
    node_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BudgetTree:
    disciplines: tuple[DisciplineNode, ...]
    blockers: tuple[Finding, ...]
    warnings: tuple[Finding, ...]

    def leaves(self) -> list[tuple[DisciplineNode, GroupNode, ItemNode, LeafNode]]:
        return [
            (d, g, i, leaf)
            for d in self.disciplines
            for g in d.groups
            for i in g.items
            for leaf in i.leaves
        ]


# Dondurma engel/uyari kodlari (B1-7).
BLOCKER_DISCIPLINELESS_GROUP = "disciplineless_group"
BLOCKER_MISSING_WINDOW = "missing_window"
BLOCKER_NO_WORKING_DAY = "no_working_day"
WARNING_EMPTY_RATE = "empty_rate"
WARNING_DISCIPLINELESS_GROUP = "disciplineless_group_without_budget"


# ------------------------------------------------------------------ kurulum


def _section_window(section: SectionInfo | None) -> tuple[date, date] | None:
    if section is None or section.start_date is None or section.end_date is None:
        return None
    if section.end_date < section.start_date:
        return None
    return section.start_date, section.end_date


def build_tree(
    boq: BoqSnapshot,
    disciplines: Sequence[DisciplineInfo],
    inputs: RevisionInputs,
    is_working_day: Callable[[date], bool],
) -> BudgetTree:
    by_disc = {d.id: d for d in disciplines}
    sections = {s.id: s for s in boq.sections}
    items_by_group: dict[uuid.UUID, list[ItemInfo]] = {}
    for item in sorted(boq.items, key=lambda i: (i.sort_order, i.code)):
        items_by_group.setdefault(item.group_id, []).append(item)

    groups_by_disc: dict[uuid.UUID | None, list[GroupNode]] = {}
    for position, group in enumerate(sorted(boq.groups, key=lambda g: (g.sort_order, g.name)), 1):
        disc_id = inputs.group_disciplines.get(group.id)
        disc = by_disc.get(disc_id) if disc_id else None
        default_ct = disc.default_contractor_type if disc else ContractorType.OWN
        items = tuple(
            _item_node(it, boq, sections, inputs, default_ct, disc_id)
            for it in items_by_group.get(group.id, [])
        )
        node = GroupNode(
            group_node_id(group.id), group.id, group.name, disc_id, items, code=str(position)
        )
        groups_by_disc.setdefault(disc_id if disc else None, []).append(node)

    ordered = sorted(disciplines, key=lambda d: (d.sort_order, d.code))
    disc_nodes = [
        _discipline_node(d.id, d, groups_by_disc[d.id], inputs, sections)
        for d in ordered
        if d.id in groups_by_disc
    ]
    if None in groups_by_disc:
        disc_nodes.append(_discipline_node(None, None, groups_by_disc[None], inputs, sections))
    tree = BudgetTree(tuple(disc_nodes), (), ())
    blockers, warnings = compute_findings(tree, is_working_day)
    return BudgetTree(tree.disciplines, blockers, warnings)


def _item_node(
    item: ItemInfo,
    boq: BoqSnapshot,
    sections: Mapping[uuid.UUID, SectionInfo],
    inputs: RevisionInputs,
    default_ct: ContractorType,
    disc_id: uuid.UUID | None,
) -> ItemNode:
    setting = inputs.items.get(item.id, ItemSetting())
    item_ct = setting.contractor_type or default_ct
    alloc = boq.allocations.get(item.id, {})
    leaf_specs: list[tuple[uuid.UUID | None, Decimal]] = [
        (sid, qty)
        for sid, qty in sorted(
            alloc.items(), key=lambda kv: (sections[kv[0]].sort_order, sections[kv[0]].name)
        )
        if qty > 0 and sid in sections
    ]
    remainder = item.quantity - sum((q for _, q in leaf_specs), ZERO)
    if remainder > 0:
        leaf_specs.append((None, remainder))
    leaves = tuple(
        _leaf_node(item, sid, qty, sections, inputs, item_ct, setting.is_direct)
        for sid, qty in leaf_specs
    )
    return ItemNode(
        id=item_node_id(item.id),
        item_id=item.id,
        code=item.code,
        description=item.description,
        uom=item.unit,
        contractor_type=item_ct,
        contractor_inherited=setting.contractor_type is None,
        is_direct=setting.is_direct,
        catalog_item_id=setting.catalog_item_id,
        leaves=_with_windows(leaves, disc_id, inputs, sections),
    )


def _leaf_node(
    item: ItemInfo,
    section_id: uuid.UUID | None,
    qty: Decimal,
    sections: Mapping[uuid.UUID, SectionInfo],
    inputs: RevisionInputs,
    item_ct: ContractorType,
    item_direct: bool,
) -> LeafNode:
    s = inputs.leaves.get((item.id, section_id), LeafSetting())
    rate = s.unit_mhr
    return LeafNode(
        id=leaf_node_id(item.id, section_id),
        item_id=item.id,
        section_id=section_id,
        section_name=sections[section_id].name if section_id else None,
        section_code=sections[section_id].code if section_id else None,
        planned_qty=qty,
        unit_mhr=rate,
        rate_source=s.rate_source,
        contractor_type=s.contractor_type or item_ct,
        contractor_overridden=s.contractor_type is not None,
        is_direct=item_direct if s.is_direct is None else s.is_direct,
        is_direct_overridden=s.is_direct is not None,
        budget_mhr=qty * rate if rate else ZERO,
        window_start=None,
        window_end=None,
        window_source=None,
    )


def _with_windows(
    leaves: tuple[LeafNode, ...],
    disc_id: uuid.UUID | None,
    inputs: RevisionInputs,
    sections: Mapping[uuid.UUID, SectionInfo],
) -> tuple[LeafNode, ...]:
    """Bolumlu yapraklar once; Bolumsuz yaprak disiplin birlesimini `_discipline_node` alir."""
    out = []
    for leaf in leaves:
        window, source = _window_for(disc_id, leaf.section_id, inputs, sections)
        own = _section_window(sections.get(leaf.section_id)) if leaf.section_id else None
        outside = bool(
            source == "override" and window and own and (window[0] < own[0] or window[1] > own[1])
        )
        out.append(replace(_replace_window(leaf, window, source), window_outside_section=outside))
    return tuple(out)


def _window_for(
    disc_id: uuid.UUID | None,
    section_id: uuid.UUID | None,
    inputs: RevisionInputs,
    sections: Mapping[uuid.UUID, SectionInfo],
) -> tuple[tuple[date, date] | None, str | None]:
    if disc_id is not None and (disc_id, section_id) in inputs.windows:
        return inputs.windows[(disc_id, section_id)], "override"
    if section_id is not None:
        window = _section_window(sections.get(section_id))
        return window, "section" if window else None
    return None, None


def _replace_window(
    leaf: LeafNode, window: tuple[date, date] | None, source: str | None
) -> LeafNode:
    return replace(
        leaf,
        window_start=window[0] if window else None,
        window_end=window[1] if window else None,
        window_source=source,
    )


def _discipline_node(
    disc_id: uuid.UUID | None,
    disc: DisciplineInfo | None,
    groups: list[GroupNode],
    inputs: RevisionInputs,
    sections: Mapping[uuid.UUID, SectionInfo],
) -> DisciplineNode:
    groups_t = tuple(_fill_union_windows(groups))
    return DisciplineNode(
        id=discipline_node_id(disc_id),
        discipline_id=disc_id,
        code=disc.code if disc else None,
        name=disc.name if disc else None,
        color=disc.color if disc else None,
        default_contractor_type=disc.default_contractor_type if disc else ContractorType.OWN,
        distribution=inputs.distributions.get(disc_id, DEFAULT_DISTRIBUTION)
        if disc_id
        else DEFAULT_DISTRIBUTION,
        groups=groups_t,
    )


def _fill_union_windows(groups: list[GroupNode]) -> list[GroupNode]:
    """B1-3: penceresiz Bolumsuz yaprak = disiplinin bolumlu pencerelerinin birlesimi."""
    windows = [
        (leaf.window_start, leaf.window_end)
        for g in groups
        for i in g.items
        for leaf in i.leaves
        if leaf.section_id is not None and leaf.window_start and leaf.window_end
    ]
    if not windows:
        return groups
    union = (min(w[0] for w in windows), max(w[1] for w in windows))

    def fix(leaf: LeafNode) -> LeafNode:
        if leaf.section_id is None and leaf.window_start is None:
            return _replace_window(leaf, union, "union")
        return leaf

    return [
        replace(g, items=tuple(replace(i, leaves=tuple(map(fix, i.leaves))) for i in g.items))
        for g in groups
    ]


def has_working_day(start: date, end: date, is_working_day: Callable[[date], bool]) -> bool:
    return any(is_working_day(start + timedelta(days=k)) for k in range((end - start).days + 1))


def compute_findings(
    tree: BudgetTree, is_working_day: Callable[[date], bool]
) -> tuple[tuple[Finding, ...], tuple[Finding, ...]]:
    disciplineless_blocking: list[str] = []
    disciplineless_warning: list[str] = []
    missing_window: list[str] = []
    no_working_day: list[str] = []
    empty_rate: list[str] = []
    for d in tree.disciplines:
        for g in d.groups:
            direct_budget = sum(
                (lf.budget_mhr for i in g.items for lf in i.leaves if lf.is_direct), ZERO
            )
            if d.discipline_id is None:
                target = disciplineless_blocking if direct_budget > 0 else disciplineless_warning
                target.append(g.id)
            for i in g.items:
                for leaf in i.leaves:
                    if not leaf.is_rated:
                        empty_rate.append(leaf.id)
                    if not leaf.is_direct or leaf.budget_mhr <= 0 or d.discipline_id is None:
                        continue
                    if leaf.window_start is None or leaf.window_end is None:
                        missing_window.append(leaf.id)
                    elif not has_working_day(leaf.window_start, leaf.window_end, is_working_day):
                        no_working_day.append(leaf.id)
    blockers = tuple(
        Finding(code, len(ids), tuple(ids))
        for code, ids in (
            (BLOCKER_DISCIPLINELESS_GROUP, disciplineless_blocking),
            (BLOCKER_MISSING_WINDOW, missing_window),
            (BLOCKER_NO_WORKING_DAY, no_working_day),
        )
        if ids
    )
    warnings = tuple(
        Finding(code, len(ids), tuple(ids))
        for code, ids in (
            (WARNING_EMPTY_RATE, empty_rate),
            (WARNING_DISCIPLINELESS_GROUP, disciplineless_warning),
        )
        if ids
    )
    return blockers, warnings

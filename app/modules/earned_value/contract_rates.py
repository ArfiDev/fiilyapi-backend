"""Sozlesme → Planlama butcesi ORTAK uygulayicisi (TKL-B6.3 + B6.4; TKL-PLAN §4.3-§4.5).

Iki cagiran, TEK mantik (kopya yok):
* `contract_adapter` — teklif→proje donusturmesi (`core.contract_seed` kancasi): santiye varsa
  Rev.0 TASLAGI kurar.
* `budget_ops.fill_from_contract` ("Sozlesmeden doldur" ucu): sonradan acilan/dagitilan
  santiyede ayni isi yapar.

## Semantik: "BOSLARI DOLDUR" (`fill_from_catalog` ile ayni)
Dolu oran, dolu katalog bagi ve mevcut grup eslemesi EZILMEZ; yalniz bos olan yazilir. Tek
istisna: cagiranin acikca verdigi ELLE grup→disiplin eslemesi (SO-31) mevcut esleneni ezer.

## Okuma (sabit sayida sorgu — kalem sayisindan BAGIMSIZ)
BOQ kalemi ⋈ sozlesme kalemi ⟕ `ev_contract_item_rates` (1) · katalog (1) · agac (`load_state`,
sabit sayida) · taslagin kendi satirlari (3-4) · disiplin dogrulamasi (1). Yazma: `add_all` +
tek flush. Kalem basina `session.get` DONGUSU YOKTUR.

## Oran onceligi (SO-34)
1. `ev_contract_item_rates` yuvasi (kaynagi yuvanin `source`u: `offer`/`catalog`),
2. yuva yoksa sozlesme kaleminin katalog bagi varsa katalog standardi + `catalog`,
3. ikisi de yoksa yaprak bos kalir.

## Disiplin (SO-31)
BOQ grubu = sozlesme grubu (T15; adi ayni). Elle esleme > grubun kalemlerinin katalog
disiplini TEKSE o > KARISIKSA eslenmez + uyari (dondurmada `BLOCKER_DISCIPLINELESS_GROUP`).
Dondurma BURADA YAPILMAZ (T34).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EarnedValueValidationError, NotFoundError
from app.modules.boq.models import BoqItem
from app.modules.catalog.models import EvCatalogItem, EvDiscipline
from app.modules.contracts.models import EmployerContractItem
from app.modules.earned_value import budget_service as svc
from app.modules.earned_value import guards
from app.modules.earned_value.access import SiteContext
from app.modules.earned_value.budget_tree import BudgetTree, GroupNode, ItemNode
from app.modules.earned_value.models import (
    EvContractItemRate,
    EvGroupDiscipline,
    EvItemSettings,
    EvLeafSettings,
    EvWindow,
    RateSource,
)
from app.modules.users.models import User

REVISION_NAME_MAX = 150

#: Yapisal uyari kodu + metni (istemci metne degil koda bakar); adaptor ve uc AYNI sabiti kullanir.
CODE_MIXED_GROUP = "mixed_discipline_group"
MSG_MIXED_GROUP = (
    "Grubun kalemleri birden çok disiplinde; Planlama'da disiplin elle eşlenmeli "
    "(eşlenmeden baseline dondurulamaz)"
)


@dataclass(frozen=True, slots=True)
class ContractLink:
    """Santiyedeki BOQ kaleminin sozlesme kalemi (`boq_items.contract_item_id`) ve oran yuvasi."""

    boq_item_id: uuid.UUID
    boq_group_id: uuid.UUID
    contract_item_id: uuid.UUID
    contract_group_id: uuid.UUID
    catalog_item_id: uuid.UUID | None
    slot_mhr: Decimal | None
    slot_source: RateSource | None


@dataclass(frozen=True, slots=True)
class MixedGroup:
    """Katalog disiplinleri karisik + elle esleme yok → eslenmedi (SO-31)."""

    boq_group_id: uuid.UUID
    contract_group_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class ApplyResult:
    filled_item_count: int = 0  # en az bir yaprak orani dolan kalem
    filled_leaf_count: int = 0
    linked_item_count: int = 0  # yeni yazilan katalog bagi
    mapped_group_count: int = 0
    window_count: int = 0
    #: Santiyede BOQ satiri olan sozlesme kalemleri (cagiran "BOQ'da yok"u buradan turetir).
    linked_contract_item_ids: frozenset[uuid.UUID] = frozenset()
    mixed_groups: tuple[MixedGroup, ...] = ()
    #: Baglı olup hicbir yaprak orani alamayan kalem sayisi (yuva da katalog da yok).
    unrated_item_count: int = 0


@dataclass(slots=True)
class _Plan:
    group_targets: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    mixed: list[MixedGroup] = field(default_factory=list)
    item_links: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    leaf_rates: list[tuple[uuid.UUID, uuid.UUID | None, Decimal, RateSource]] = field(
        default_factory=list
    )
    rated_items: set[uuid.UUID] = field(default_factory=set)
    unrated_items: int = 0
    mapped_disciplines: set[uuid.UUID] = field(default_factory=set)


# ------------------------------------------------------------------ okuma


async def load_links(session: AsyncSession, site_id: uuid.UUID) -> list[ContractLink]:
    """TEK sorgu: BOQ kalemi → sozlesme kalemi → (varsa) oran yuvasi."""
    stmt = (
        select(
            BoqItem.id,
            BoqItem.group_id,
            BoqItem.contract_item_id,
            EmployerContractItem.group_id,
            EmployerContractItem.catalog_item_id,
            EvContractItemRate.unit_mhr,
            EvContractItemRate.source,
        )
        .join(EmployerContractItem, EmployerContractItem.id == BoqItem.contract_item_id)
        .outerjoin(
            EvContractItemRate, EvContractItemRate.contract_item_id == EmployerContractItem.id
        )
        .where(BoqItem.site_id == site_id, BoqItem.contract_item_id.is_not(None))
    )
    return [ContractLink(*row) for row in (await session.execute(stmt)).all()]


async def _catalog_index(
    session: AsyncSession,
) -> dict[uuid.UUID, tuple[uuid.UUID, Decimal]]:
    """katalog id → (disiplin, standart adam-saat)."""
    rows = await session.execute(
        select(EvCatalogItem.id, EvCatalogItem.discipline_id, EvCatalogItem.standard_unit_mhr)
    )
    return {cid: (disc, std) for cid, disc, std in rows.all()}


async def _require_disciplines(session: AsyncSession, ids: set[uuid.UUID]) -> None:
    if not ids:
        return
    found = set(
        (await session.execute(select(EvDiscipline.id).where(EvDiscipline.id.in_(ids)))).scalars()
    )
    if ids - found:
        raise NotFoundError(guards.DISCIPLINE_MISSING)


# ------------------------------------------------------------------ plan (saf)


def _groups(tree: BudgetTree) -> list[GroupNode]:
    return [g for d in tree.disciplines for g in d.groups]


def _plan_group(
    group: GroupNode,
    links: Mapping[uuid.UUID, ContractLink],
    catalog: Mapping[uuid.UUID, tuple[uuid.UUID, Decimal]],
    manual: Mapping[uuid.UUID, uuid.UUID],
    plan: _Plan,
) -> None:
    linked = [links[i.item_id] for i in group.items if i.item_id in links]
    if not linked:
        return
    contract_groups = tuple(sorted({lk.contract_group_id for lk in linked}, key=str))
    chosen = next((manual[cg] for cg in contract_groups if cg in manual), None)
    if chosen is not None:
        if group.discipline_id != chosen:
            plan.group_targets[group.group_id] = chosen
        return
    if group.discipline_id is not None:
        return  # mevcut esleme EZILMEZ
    found = {catalog[lk.catalog_item_id][0] for lk in linked if lk.catalog_item_id in catalog}
    if len(found) == 1:
        plan.group_targets[group.group_id] = next(iter(found))
    elif len(found) > 1:
        plan.mixed.append(MixedGroup(group.group_id, contract_groups))


def _item_rates(
    item: ItemNode,
    link: ContractLink,
    catalog: Mapping[uuid.UUID, tuple[uuid.UUID, Decimal]],
) -> tuple[Decimal, RateSource] | None:
    """SO-34 onceligi: yuva > katalog standardi > yok."""
    if link.slot_mhr is not None and link.slot_source is not None:
        return link.slot_mhr, link.slot_source
    if link.catalog_item_id in catalog:
        return catalog[link.catalog_item_id][1], RateSource.CATALOG
    return None


def _plan_item(
    item: ItemNode,
    link: ContractLink,
    catalog: Mapping[uuid.UUID, tuple[uuid.UUID, Decimal]],
    plan: _Plan,
) -> None:
    if link.catalog_item_id is not None and item.catalog_item_id is None:
        plan.item_links[item.item_id] = link.catalog_item_id
    empty = [lf for lf in item.leaves if lf.unit_mhr is None]
    if not empty:
        return
    rate = _item_rates(item, link, catalog)
    if rate is None:
        plan.unrated_items += 1
        return
    plan.rated_items.add(item.item_id)
    plan.leaf_rates.extend((item.item_id, lf.section_id, rate[0], rate[1]) for lf in empty)


def build_plan(
    tree: BudgetTree,
    links: list[ContractLink],
    catalog: Mapping[uuid.UUID, tuple[uuid.UUID, Decimal]],
    manual: Mapping[uuid.UUID, uuid.UUID],
) -> _Plan:
    by_item = {lk.boq_item_id: lk for lk in links}
    plan = _Plan()
    for group in _groups(tree):
        _plan_group(group, by_item, catalog, manual, plan)
        for item in group.items:
            if item.item_id in by_item:
                _plan_item(item, by_item[item.item_id], catalog, plan)
        final = plan.group_targets.get(group.group_id, group.discipline_id)
        if final is not None:
            plan.mapped_disciplines.add(final)
    return plan


# ------------------------------------------------------------------ yazma (toplu)


async def _write_groups(
    session: AsyncSession, rev_id: uuid.UUID, targets: Mapping[uuid.UUID, uuid.UUID]
) -> None:
    if not targets:
        return
    rows = await session.execute(
        select(EvGroupDiscipline).where(EvGroupDiscipline.revision_id == rev_id)
    )
    existing = {r.boq_group_id: r for r in rows.scalars()}
    for group_id, disc_id in targets.items():
        row = existing.get(group_id)
        if row is None:
            session.add(
                EvGroupDiscipline(revision_id=rev_id, boq_group_id=group_id, discipline_id=disc_id)
            )
        else:
            row.discipline_id = disc_id


async def _write_item_links(
    session: AsyncSession, rev_id: uuid.UUID, links: Mapping[uuid.UUID, uuid.UUID]
) -> None:
    if not links:
        return
    rows = await session.execute(select(EvItemSettings).where(EvItemSettings.revision_id == rev_id))
    existing = {r.boq_item_id: r for r in rows.scalars()}
    for item_id, catalog_id in links.items():
        row = existing.get(item_id)
        if row is None:
            session.add(
                EvItemSettings(
                    revision_id=rev_id,
                    boq_item_id=item_id,
                    is_direct=True,
                    catalog_item_id=catalog_id,
                )
            )
        elif row.catalog_item_id is None:
            row.catalog_item_id = catalog_id


async def _write_leaf_rates(
    session: AsyncSession,
    rev_id: uuid.UUID,
    rates: list[tuple[uuid.UUID, uuid.UUID | None, Decimal, RateSource]],
) -> None:
    if not rates:
        return
    rows = await session.execute(select(EvLeafSettings).where(EvLeafSettings.revision_id == rev_id))
    existing = {(r.boq_item_id, r.section_id): r for r in rows.scalars()}
    for item_id, section_id, rate, source in rates:
        row = existing.get((item_id, section_id))
        if row is None:
            row = EvLeafSettings(revision_id=rev_id, boq_item_id=item_id, section_id=section_id)
            session.add(row)
        row.unit_mhr = rate
        row.rate_source = source


async def _write_windows(
    session: AsyncSession,
    rev_id: uuid.UUID,
    disciplines: set[uuid.UUID],
    window: tuple[date, date],
) -> int:
    """Her eslenen disipline `(disiplin, NULL)` penceresi (SO-35); mevcut ezme EZILMEZ."""
    rows = await session.execute(
        select(EvWindow.discipline_id).where(
            EvWindow.revision_id == rev_id, EvWindow.section_id.is_(None)
        )
    )
    todo = disciplines - set(rows.scalars())
    for disc_id in sorted(todo, key=str):
        session.add(
            EvWindow(
                revision_id=rev_id,
                discipline_id=disc_id,
                section_id=None,
                start_date=window[0],
                end_date=window[1],
            )
        )
    return len(todo)


# ------------------------------------------------------------------ giris


async def apply_contract_to_draft(
    session: AsyncSession,
    ctx: SiteContext,
    actor: User,
    *,
    manual_disciplines: Mapping[uuid.UUID, uuid.UUID] | None = None,
    window: tuple[date, date] | None = None,
    force_draft: bool = False,
    label: str | None = None,
) -> ApplyResult:
    """Santiyenin TASLAK revizyonunu sozlesmeden doldurur (bkz. modul docstring'i).

    `manual_disciplines`: sozlesme grup id → disiplin id (SO-31). `window`: (baslangic, bitis)
    verilirse eslenen her disipline Bolumsuz pencere yazilir. `force_draft`: yazacak sey
    olmasa da Rev.0 taslagi acilir (donusturme). Yazma yoksa taslak acilmaz; aktif revizyon
    varken ve taslaksizken yazilacak sey varsa 409 (once "taslak ac").
    """
    manual = dict(manual_disciplines or {})
    if window is not None and window[1] < window[0]:
        raise EarnedValueValidationError(guards.WINDOW_RANGE_INVALID)
    await svc._writable_site(session, ctx)  # noqa: SLF001
    await _require_disciplines(session, set(manual.values()))
    state = await svc.load_state(session, ctx)
    links = await load_links(session, ctx.site.id)
    plan = build_plan(state.tree, links, await _catalog_index(session), manual)
    result = ApplyResult(
        filled_item_count=len(plan.rated_items),
        filled_leaf_count=len(plan.leaf_rates),
        linked_item_count=len(plan.item_links),
        mapped_group_count=len(plan.group_targets),
        linked_contract_item_ids=frozenset(lk.contract_item_id for lk in links),
        mixed_groups=tuple(plan.mixed),
        unrated_item_count=plan.unrated_items,
    )
    wants_window = window is not None and bool(plan.mapped_disciplines)
    if not (
        force_draft or plan.group_targets or plan.item_links or plan.leaf_rates or wants_window
    ):
        return result
    draft = await svc._draft_for_write(session, ctx, actor)  # noqa: SLF001
    if label and draft.name is None:
        draft.name = label[:REVISION_NAME_MAX]
    await _write_groups(session, draft.id, plan.group_targets)
    await _write_item_links(session, draft.id, plan.item_links)
    await _write_leaf_rates(session, draft.id, plan.leaf_rates)
    windows = 0
    if window is not None:
        windows = await _write_windows(session, draft.id, plan.mapped_disciplines, window)
    await svc._touch(session, draft)  # noqa: SLF001
    return replace(result, window_count=windows)

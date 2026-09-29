"""Disiplin kapsami icin butce agaci BUDAMA (DSC-B1, DISIPLIN-KAPSAMI-SPEC K5) — SAF, DB yok.

## Kural (CEO K5)
* DONMUS agac (aktif/arsiv revizyon): yalniz `d:` KOKUNDEN budanir — donmus agacin
  disiplini revizyonun kendi esleme girdisinden gelir ve o donmus gercektir.
* TASLAK / CANLI agac: `d:` koku ∩ R(site) kalem disiplini. Kalem, hem taslaktaki `d:`
  kokunun kapsamda olmasi HEM `visible_items` (R(site) = aktif ?? taslak, port cozumu)
  kumesinde olmasi halinde gorunur; ikisi birden tutmazsa budanir.
* `d:none` HER ZAMAN budanir (Ü1: disiplinsiz kalem kisitliya gorunmez, fail-closed).
* Kisitsiz kapsamda agac OLDUGU GIBI doner (atamasiz yanit degismez).

## Agregat tutarliligi (Ü3)
Budama yalniz AGACI keser; dugum toplamlari, `totals`, `share` ve dondurma bulgulari sunum
katmaninda budanmis agactan yeniden hesaplanir. Donmus agacta bulgu bos kalir (bugunku gibi);
taslakta `compute_findings` budanmis agacla yeniden kosar.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from datetime import date

from app.core.discipline_scope import DisciplineScope
from app.modules.earned_value.budget_tree import (
    BudgetTree,
    DisciplineNode,
    GroupNode,
    ItemNode,
    compute_findings,
)


def item_ids(tree: BudgetTree) -> list[uuid.UUID]:
    """Agactaki tum kalem kimlikleri (R(site) cozumu icin)."""
    return [i.item_id for d in tree.disciplines for g in d.groups for i in g.items]


def node_ids(tree: BudgetTree) -> frozenset[str]:
    """Agactaki tum dugum kimlikleri (`d:`/`g:`/`i:`/`l:`)."""
    out: set[str] = set()
    for d in tree.disciplines:
        out.add(d.id)
        for g in d.groups:
            out.add(g.id)
            for i in g.items:
                out.add(i.id)
                out.update(lf.id for lf in i.leaves)
    return frozenset(out)


def _prune_discipline(d: DisciplineNode, visible: Collection[uuid.UUID]) -> DisciplineNode | None:
    groups: list[GroupNode] = []
    for g in d.groups:
        items = tuple(i for i in g.items if i.item_id in visible)
        if items:
            groups.append(replace(g, items=items))
    return replace(d, groups=tuple(groups)) if groups else None


def prune_tree(
    tree: BudgetTree,
    scope: DisciplineScope,
    visible_items: Collection[uuid.UUID] | None,
    frozen: bool,
    is_working_day: Callable[[date], bool] | None = None,
) -> BudgetTree:
    """Kapsama gore budanmis agac. `frozen=True` → yalniz `d:` koku; aksi halde `d:` ∩
    `visible_items`. `visible_items=None` kisitlida "hicbir kalem" demektir (fail-closed).
    Taslakta bulgular budanmis agactan yeniden hesaplanir (`is_working_day` sart)."""
    if not scope.is_restricted:
        return tree
    allowed = scope.discipline_ids or frozenset()
    # `d:none` (discipline_id None) hicbir zaman `allowed` icinde degildir → hep budanir.
    kept = tuple(d for d in tree.disciplines if d.discipline_id in allowed)
    if frozen:
        return replace(tree, disciplines=kept)
    if is_working_day is None:
        raise ValueError("Taslak agac budamasi bulgu yeniden hesabi icin takvim ister")
    visible = frozenset(visible_items or ())
    pruned = tuple(p for d in kept if (p := _prune_discipline(d, visible)) is not None)
    blockers, warnings = compute_findings(BudgetTree(pruned, (), ()), is_working_day)
    return BudgetTree(pruned, blockers, warnings)


def catalog_link_ids(tree: BudgetTree) -> set[uuid.UUID]:
    """Agactaki kalemlerin bagli katalog kimlikleri."""
    return {
        i.catalog_item_id
        for d in tree.disciplines
        for g in d.groups
        for i in g.items
        if i.catalog_item_id is not None
    }


def mask_catalog_links(
    tree: BudgetTree, scope: DisciplineScope, catalog_disciplines: Mapping[uuid.UUID, uuid.UUID]
) -> BudgetTree:
    """Kisitlida kapsam disi disiplinin katalog kalemine BAGLI kalemin `catalog_item_id`si
    `None` olur (capraz bag yabanci katalog kimligini sizdirmasin, Ü8). Kisitsizda ayni agac."""
    if not scope.is_restricted:
        return tree
    allowed = scope.discipline_ids or frozenset()

    def item(i: ItemNode) -> ItemNode:
        if i.catalog_item_id is None or catalog_disciplines.get(i.catalog_item_id) in allowed:
            return i
        return replace(i, catalog_item_id=None)

    return replace(
        tree,
        disciplines=tuple(
            replace(d, groups=tuple(replace(g, items=tuple(map(item, g.items))) for g in d.groups))
            for d in tree.disciplines
        ),
    )

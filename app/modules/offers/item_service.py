"""Teklif grup + kalem yazma mantigi (TKL-B4.2). Servis COMMIT ETMEZ.

Her yazma `locking.lock_draft_revision` ile teklif satirini kilitler: yalniz SON revizyon ve
yalniz `draft` iken yazilir (aksi 409). Kilit ALTINDA grup/kalem/katalog dogrulanir.

## Kalem ekleme (tekil + toplu AYNI yol — tekil = 1 elemanli toplu)
Hep-ya-hic: ONCE hepsi dogrulanir (grup tek sorgu, katalog tek sorgu, son fiyat tek cagri), SONRA
yazilir. Sunucu katalogdan KOPYALAR: `poz_no`, `description` (= ad), `unit`; `unit_mhr` govdede
yoksa katalogun `standard_unit_mhr`i. **SO-6**: govdede `cost_unit_price` GONDERILMEMISSE maliyet
= `calc.suggest_cost(son fiyat, referans fiyat)`; ACIK `null` gonderilirse BOS kalir.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import last_price
from app.core.errors import NotFoundError, OfferValidationError
from app.modules.catalog.models import EvCatalogItem
from app.modules.offers import calc, locking
from app.modules.offers.models import Offer, OfferGroup, OfferItem, OfferRevision
from app.modules.offers.offer_schemas import (
    OfferGroupCreate,
    OfferGroupUpdate,
    OfferItemCreate,
    OfferItemUpdate,
)
from app.modules.offers.offer_views import item_input

CATALOG_ITEM_MISSING = "Katalog iş tipi bulunamadı"
GROUP_MISSING = "Teklif grubu bulunamadı"
ITEM_MISSING = "Teklif kalemi bulunamadı"
GROUP_NOT_IN_REVISION = "Grup bu revizyona ait değil"

_CENT = Decimal("0.01")

#: `calc.ItemInput` alanlari (PATCH govdesinden hesabi etkileyenler).
_CALC_FIELDS = frozenset(f.name for f in dataclasses.fields(calc.ItemInput))


def _cent(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------- grup


async def _next_group_order(session: AsyncSession, revision_id: uuid.UUID) -> int:
    top = await session.scalar(
        select(func.max(OfferGroup.sort_order)).where(OfferGroup.revision_id == revision_id)
    )
    return 0 if top is None else top + 1


async def _get_group(
    session: AsyncSession, revision_id: uuid.UUID, group_id: uuid.UUID
) -> OfferGroup:
    group = await session.scalar(
        select(OfferGroup)
        .where(OfferGroup.id == group_id, OfferGroup.revision_id == revision_id)
        .execution_options(populate_existing=True)
    )
    if group is None:
        raise NotFoundError(GROUP_MISSING)
    return group


async def create_group(
    session: AsyncSession, offer_id: uuid.UUID, rev_no: int, data: OfferGroupCreate
) -> OfferGroup:
    _offer, revision = await locking.lock_draft_revision(session, offer_id, rev_no)
    group = OfferGroup(
        revision_id=revision.id,
        name=data.name,
        sort_order=(
            data.sort_order
            if data.sort_order is not None
            else await _next_group_order(session, revision.id)
        ),
    )
    session.add(group)
    locking.touch_revision(revision)
    await session.flush()
    return group


async def update_group(
    session: AsyncSession,
    offer_id: uuid.UUID,
    rev_no: int,
    group_id: uuid.UUID,
    data: OfferGroupUpdate,
) -> OfferGroup:
    _offer, revision = await locking.lock_draft_revision(session, offer_id, rev_no)
    group = await _get_group(session, revision.id, group_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(group, field, value)
    locking.touch_revision(revision)
    await session.flush()
    return group


async def delete_group(
    session: AsyncSession, offer_id: uuid.UUID, rev_no: int, group_id: uuid.UUID
) -> tuple[Offer, OfferRevision, str, int]:
    """Grubu ICINDEKI KALEMLERLE birlikte siler (bilesik FK `ON DELETE CASCADE`). Doner:
    `(teklif, revizyon, grup adi, silinen kalem adedi)` (denetim satiri icin)."""
    offer, revision = await locking.lock_draft_revision(session, offer_id, rev_no)
    group = await _get_group(session, revision.id, group_id)
    name = group.name
    item_count = await session.scalar(
        select(func.count()).select_from(OfferItem).where(OfferItem.group_id == group.id)
    )
    await session.delete(group)
    locking.touch_revision(revision)
    await session.flush()
    return offer, revision, name, item_count or 0


# ---------------------------------------------------------------------------- kalem


def _assert_priceable(
    item_in: calc.ItemInput, revision: OfferRevision, *, label: str | None = None
) -> None:
    """SO-4: maliyet bos iken elle B.F. → Turkce 422 (calc istisnasi cevrilir). BIRLESIK durum
    (kayit + govde) uzerinde ve ORM nesnesi DEGISTIRILMEDEN ONCE kosar: reddedilen yazma oturumda
    kirli nesne birakmaz (DB CHECK'e carpip 409/500 donmez)."""
    try:
        calc.calc_item(item_in, overhead_pct=revision.overhead_pct, profit_pct=revision.profit_pct)
    except calc.OfferCalcError as exc:
        raise OfferValidationError(f"{label}: {exc}" if label else str(exc)) from exc


async def _group_ids_in_revision(
    session: AsyncSession, revision_id: uuid.UUID, group_ids: set[uuid.UUID]
) -> set[uuid.UUID]:
    return set(
        await session.scalars(
            select(OfferGroup.id).where(
                OfferGroup.revision_id == revision_id, OfferGroup.id.in_(group_ids)
            )
        )
    )


async def _groups_anywhere(session: AsyncSession, group_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """Grup HERHANGI bir revizyonda var mi (govde-ici referans: yok → 404, baskasinda → 422)."""
    if not group_ids:
        return set()
    return set(await session.scalars(select(OfferGroup.id).where(OfferGroup.id.in_(group_ids))))


def _foreign_group_error(
    group_id: uuid.UUID, anywhere: set[uuid.UUID], prefix: str = ""
) -> NotFoundError | OfferValidationError:
    if group_id not in anywhere:
        return NotFoundError(f"{prefix}{GROUP_MISSING}")
    return OfferValidationError(f"{prefix}{GROUP_NOT_IN_REVISION}")


async def _next_item_orders(session: AsyncSession, revision_id: uuid.UUID) -> dict[uuid.UUID, int]:
    rows = await session.execute(
        select(OfferItem.group_id, func.max(OfferItem.sort_order))
        .where(OfferItem.revision_id == revision_id)
        .group_by(OfferItem.group_id)
    )
    return {group_id: top + 1 for group_id, top in rows}


async def add_items(
    session: AsyncSession,
    offer_id: uuid.UUID,
    rev_no: int,
    bodies: Sequence[OfferItemCreate],
    *,
    bulk: bool = False,
) -> tuple[Offer, OfferRevision, list[OfferItem]]:
    offer, revision = await locking.lock_draft_revision(session, offer_id, rev_no)

    def label(index: int) -> str | None:
        return f"Kalem {index + 1}" if bulk else None

    # 1) dogrula (yazmadan ONCE): gruplar — TEK sorgu
    wanted_groups = {body.group_id for body in bodies}
    known_groups = await _group_ids_in_revision(session, revision.id, wanted_groups)
    anywhere = await _groups_anywhere(session, wanted_groups - known_groups)
    for index, body in enumerate(bodies):
        if body.group_id not in known_groups:
            prefix = f"{label(index)}: " if bulk else ""
            raise _foreign_group_error(body.group_id, anywhere, prefix)

    # 2) katalog — TEK sorgu
    catalog_ids = {body.catalog_item_id for body in bodies}
    catalog = {
        row.id: row
        for row in await session.scalars(
            select(EvCatalogItem).where(EvCatalogItem.id.in_(catalog_ids))
        )
    }
    if not catalog_ids <= catalog.keys():
        raise NotFoundError(CATALOG_ITEM_MISSING)

    # 3) SO-6 son fiyat — TEK cagri (yalniz maliyeti gonderilmeyen kalemler icin)
    suggest_ids = [b.catalog_item_id for b in bodies if "cost_unit_price" not in b.model_fields_set]
    latest = await last_price.latest(session, suggest_ids) if suggest_ids else {}

    orders = await _next_item_orders(session, revision.id)
    items: list[OfferItem] = []
    for index, body in enumerate(bodies):
        entry = catalog[body.catalog_item_id]
        if "cost_unit_price" in body.model_fields_set:
            cost = body.cost_unit_price
        else:
            last = latest.get(body.catalog_item_id)
            suggested = calc.suggest_cost(last.price if last is not None else None, entry.ref_price)
            cost = None if suggested is None else _cent(suggested)
        if body.sort_order is not None:
            sort_order = body.sort_order
        else:
            sort_order = orders.get(body.group_id, 0)
            orders[body.group_id] = sort_order + 1
        item = OfferItem(
            revision_id=revision.id,
            group_id=body.group_id,
            sort_order=sort_order,
            catalog_item_id=entry.id,
            poz_no=entry.poz_no,
            description=entry.name,
            unit=entry.uom,
            quantity=body.quantity,
            unit_mhr=body.unit_mhr if body.unit_mhr is not None else entry.standard_unit_mhr,
            cost_unit_price=cost,
            overhead_pct=body.overhead_pct,
            profit_pct=body.profit_pct,
            offer_unit_price=body.offer_unit_price,
        )
        _assert_priceable(item_input(item), revision, label=label(index))
        items.append(item)

    # 4) yaz
    session.add_all(items)
    locking.touch_revision(revision)
    await session.flush()
    return offer, revision, await _reload_items(session, items)


async def _reload_items(session: AsyncSession, items: Sequence[OfferItem]) -> list[OfferItem]:
    """Yazilan kalemleri DB'den TAZE okur (sira korunur): yazma yaniti, okuma yolunun gorecegi
    Numeric olcekli degerlerle AYNI bicimde olsun (`"1"` degil `"1.00"`) — R3."""
    fresh = {
        row.id: row
        for row in await session.scalars(
            select(OfferItem)
            .where(OfferItem.id.in_([i.id for i in items]))
            .execution_options(populate_existing=True)
        )
    }
    return [fresh[i.id] for i in items]


async def update_item(
    session: AsyncSession,
    offer_id: uuid.UUID,
    rev_no: int,
    item_id: uuid.UUID,
    data: OfferItemUpdate,
) -> tuple[Offer, OfferRevision, OfferItem]:
    offer, revision = await locking.lock_draft_revision(session, offer_id, rev_no)
    item = await _get_item(session, revision.id, item_id)
    changes = data.model_dump(exclude_unset=True)
    group_id = changes.get("group_id")
    if group_id is not None and group_id != item.group_id:
        if group_id not in await _group_ids_in_revision(session, revision.id, {group_id}):
            raise _foreign_group_error(group_id, await _groups_anywhere(session, {group_id}))
    merged = dataclasses.replace(
        item_input(item), **{k: v for k, v in changes.items() if k in _CALC_FIELDS}
    )
    _assert_priceable(merged, revision)  # yazmadan ONCE (birlesik durum)
    for field, value in changes.items():
        setattr(item, field, value)
    locking.touch_revision(revision)
    await session.flush()
    await session.refresh(item)  # yanit GET ile ayni bicim (DB'nin Numeric olcegi) — R3
    return offer, revision, item


async def delete_item(
    session: AsyncSession, offer_id: uuid.UUID, rev_no: int, item_id: uuid.UUID
) -> None:
    _offer, revision = await locking.lock_draft_revision(session, offer_id, rev_no)
    item = await _get_item(session, revision.id, item_id)
    await session.delete(item)
    locking.touch_revision(revision)
    await session.flush()


async def _get_item(session: AsyncSession, revision_id: uuid.UUID, item_id: uuid.UUID) -> OfferItem:
    item = await session.scalar(
        select(OfferItem)
        .where(OfferItem.id == item_id, OfferItem.revision_id == revision_id)
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise NotFoundError(ITEM_MISSING)
    return item

"""Yeni revizyonun ICERIGINI doldurma: onceki revizyondan kopya + sablondan (TKL-B5.1).

Servis COMMIT ETMEZ. Cagiran revizyonu (ve teklif satirini) zaten kilitlemis/olusturmustur.

* `copy_content`: revizyondan revizyona grup + kalem kopyasi (fiyat, oran, ELLE B.F., adam-saat,
  MIKTAR (NULL dahil) birebir). Yeni revizyon (T36) ve "mevcut tekliften kopyala" (SO-8) AYNI
  yolu kullanir; kalemin grubu hedefteki KARSILIK grubuna eslenir.
* `seed_from_template`: sablonun gruplari + katalog kalemleri. MIKTAR BOS (SO-21), maliyet =
  son fiyat → referans → bos (T32/T38; TEK `last_price.latest` cagrisi), `unit_mhr` katalogdan.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import last_price
from app.modules.catalog.models import EvCatalogItem
from app.modules.offers.item_service import suggested_cost
from app.modules.offers.models import (
    OfferGroup,
    OfferItem,
    OfferTemplateGroup,
    OfferTemplateItem,
)


async def copy_content(
    session: AsyncSession, *, source_revision_id: uuid.UUID, target_revision_id: uuid.UUID
) -> None:
    group_map: dict[uuid.UUID, uuid.UUID] = {}
    for group in await session.scalars(
        select(OfferGroup).where(OfferGroup.revision_id == source_revision_id)
    ):
        new_id = uuid.uuid4()
        group_map[group.id] = new_id
        session.add(
            OfferGroup(
                id=new_id,
                revision_id=target_revision_id,
                name=group.name,
                sort_order=group.sort_order,
            )
        )
    await session.flush()

    for item in await session.scalars(
        select(OfferItem).where(OfferItem.revision_id == source_revision_id)
    ):
        session.add(
            OfferItem(
                revision_id=target_revision_id,
                group_id=group_map[item.group_id],
                sort_order=item.sort_order,
                catalog_item_id=item.catalog_item_id,
                poz_no=item.poz_no,
                source_code=item.source_code,
                description=item.description,
                unit=item.unit,
                quantity=item.quantity,
                unit_mhr=item.unit_mhr,
                cost_unit_price=item.cost_unit_price,
                overhead_pct=item.overhead_pct,
                profit_pct=item.profit_pct,
                offer_unit_price=item.offer_unit_price,
            )
        )
    await session.flush()


async def seed_from_template(
    session: AsyncSession, *, template_id: uuid.UUID, target_revision_id: uuid.UUID
) -> None:
    groups = list(
        await session.scalars(
            select(OfferTemplateGroup).where(OfferTemplateGroup.template_id == template_id)
        )
    )
    group_map: dict[uuid.UUID, uuid.UUID] = {}
    for group in groups:
        new_id = uuid.uuid4()
        group_map[group.id] = new_id
        session.add(
            OfferGroup(
                id=new_id,
                revision_id=target_revision_id,
                name=group.name,
                sort_order=group.sort_order,
            )
        )
    await session.flush()

    template_items = list(
        await session.scalars(
            select(OfferTemplateItem).where(OfferTemplateItem.template_id == template_id)
        )
    )
    if not template_items:
        return
    catalog_ids = {i.catalog_item_id for i in template_items}
    catalog = {
        row.id: row
        for row in await session.scalars(
            select(EvCatalogItem).where(EvCatalogItem.id.in_(catalog_ids))
        )
    }
    latest = await last_price.latest(session, list(catalog_ids))  # TEK cagri (T32)
    for item in template_items:
        entry = catalog[item.catalog_item_id]  # FK RESTRICT: katalog kalemi silinemez
        session.add(
            OfferItem(
                revision_id=target_revision_id,
                group_id=group_map[item.group_id],
                sort_order=item.sort_order,
                catalog_item_id=entry.id,
                poz_no=entry.poz_no,
                source_code=entry.source_code,
                description=entry.name,
                unit=entry.uom,
                quantity=None,  # SO-21: miktar bos gelir
                unit_mhr=entry.standard_unit_mhr,
                cost_unit_price=suggested_cost(latest.get(entry.id), entry),
            )
        )
    await session.flush()

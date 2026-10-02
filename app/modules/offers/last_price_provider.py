"""Son fiyat saglayicisi — TKL (kazanilan teklif kalemi) (TKL-B4.3 / T29, T33).

Kaynak: `offer_items` — YALNIZ `status = 'won'` revizyonlarin, `cost_unit_price` DOLU kalemleri.
Taslak / gonderilmis / kaybedilmis / vazgecilmis revizyon kaynak OLMAZ. Fiyat = MALIYET B.F.
(`cost_unit_price`; teklif B.F. DEGIL — son fiyat "bu isi ne maliyetle yaptik" bilgisidir).
Zaman = `won_at`; belge no = `"{offer_no} Rev.{rev_no}"`; belge id = TEKLIF id.

## Secim kurali (tek toplu sorgu: alt sorgu GROUP BY + DISTINCT ON; N+1 yok)

1. Ayni revizyonda ayni `catalog_item_id`ye bagli BIRDEN COK kalem varsa o revizyonun adayi:
   fiyat = MAX(`cost_unit_price`) (T29: ayni belgede birden cok fiyat → EN YUKSEK).
2. Revizyonlar arasi: en yeni `won_at` kazanir. ESITLIKTE deterministik ikincil anahtar
   (SZL emsali, kucuk olan kazanir): teklif no, sonra `rev_no`, sonra teklif id.

Import edilince `app.core.last_price`e KAYDOLUR (teklif router'i `register()` cagirir); kayit
bekcisi `tests/modules/catalog/test_last_price_kaynaklar.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import last_price
from app.core.last_price import LastPrice
from app.modules.offers.models import Offer, OfferItem, OfferRevision, OfferRevisionStatus

SOURCE = "TKL"


async def provide(
    session: AsyncSession, catalog_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, LastPrice]:
    item = OfferItem
    per_revision = (
        select(
            item.catalog_item_id.label("catalog_item_id"),
            item.revision_id.label("revision_id"),
            func.max(item.cost_unit_price).label("price"),
        )
        .where(item.catalog_item_id.in_(list(catalog_ids)), item.cost_unit_price.is_not(None))
        .group_by(item.catalog_item_id, item.revision_id)
        .subquery()
    )
    stmt = (
        select(
            per_revision.c.catalog_item_id,
            per_revision.c.price,
            OfferRevision.won_at,
            Offer.offer_no,
            OfferRevision.rev_no,
            Offer.id,
        )
        .join(OfferRevision, OfferRevision.id == per_revision.c.revision_id)
        .join(Offer, Offer.id == OfferRevision.offer_id)
        .where(OfferRevision.status == OfferRevisionStatus.won)
        .distinct(per_revision.c.catalog_item_id)
        .order_by(
            per_revision.c.catalog_item_id,
            OfferRevision.won_at.desc(),
            Offer.offer_no,
            OfferRevision.rev_no,
            Offer.id,
        )
    )
    return {
        catalog_id: LastPrice(
            price=price,
            at=won_at,
            source=SOURCE,
            doc_no=f"{offer_no} Rev.{rev_no}",
            doc_id=offer_id,
        )
        for catalog_id, price, won_at, offer_no, rev_no, offer_id in (await session.execute(stmt))
    }


def register() -> None:
    last_price.register_provider(SOURCE, provide)

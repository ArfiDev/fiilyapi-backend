"""Cekirdek `/catalog/items` okuma yardimcilari (TKL-B2.2): liste sorgusu + satir → sema.

Yazma mantigi `service.py`dedir (EV ile ortak); burasi yalniz okuma/sunum. EV'yi import ETMEZ.
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import last_price
from app.core.discipline_scope import DisciplineScope
from app.core.text import like_contains_pattern
from app.modules.catalog import service
from app.modules.catalog.models import EvCatalogItem, EvDiscipline
from app.modules.catalog.schemas import WorkDisciplineRead, WorkItemRead


def to_read(
    item: EvCatalogItem, discipline: EvDiscipline, price: last_price.LastPrice | None = None
) -> WorkItemRead:
    return WorkItemRead.model_validate(
        {
            "id": item.id,
            "poz_no": item.poz_no,
            "discipline": discipline,
            "name": item.name,
            "uom": item.uom,
            "description": item.description,
            "standard_unit_mhr": item.standard_unit_mhr,
            "default_contractor_type": item.default_contractor_type,
            "ref_price": item.ref_price,
            "last_price": price,
            "price_updated_at": item.price_updated_at,
            "standard_updated_at": item.standard_updated_at,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
    )


async def read_item(session: AsyncSession, item: EvCatalogItem) -> WorkItemRead:
    """Yazimdan sonra: sunucu uretimli kolonlar (`updated_at`...) tazelenir, disiplin okunur."""
    await session.refresh(item)
    discipline = await service.get_discipline(session, item.discipline_id)
    prices = await last_price.latest(session, [item.id])
    return to_read(item, discipline, prices.get(item.id))


async def list_items(
    session: AsyncSession,
    scope: DisciplineScope,
    *,
    q: str | None = None,
    discipline_id: uuid.UUID | None = None,
) -> list[WorkItemRead]:
    """poz_no sirasiyla; `q` ad VEYA poz no icinde (harf duyarsiz). Kisitli kullanici yalniz
    kendi disiplinlerinin kalemlerini gorur; yabanci `discipline_id` kapsamla KESISIR → `[]`."""
    stmt = select(EvCatalogItem, EvDiscipline).join(
        EvDiscipline, EvDiscipline.id == EvCatalogItem.discipline_id
    )
    if scope.is_restricted:
        stmt = stmt.where(
            EvCatalogItem.discipline_id.in_(sorted(scope.discipline_ids or (), key=str))
        )
    if discipline_id is not None:
        stmt = stmt.where(EvCatalogItem.discipline_id == discipline_id)
    if q is not None and q.strip():
        pattern = like_contains_pattern(q.strip())
        stmt = stmt.where(
            or_(
                EvCatalogItem.name.ilike(pattern, escape="\\"),
                EvCatalogItem.poz_no.ilike(pattern, escape="\\"),
            )
        )
    # poz_no metin sirasi: ayni disiplinde sifir dolgulu (0001 < 0002); 5+ hane (10000) tasar.
    stmt = stmt.order_by(EvCatalogItem.poz_no, EvCatalogItem.id)
    rows = (await session.execute(stmt)).all()
    # Son fiyat: TEK toplu `latest` (kalem basina cagri YOK → N+1 yok). Kapsam SUZMESI yok
    # (sirket geneli; kisitli kullanici erisemedigi projenin fiyatini gorebilir — izin turu
    # notu, `app/core/last_price.py`). `limited` rolde alan maskeyle zaten bosalir.
    prices = await last_price.latest(session, [item.id for item, _ in rows])
    return [to_read(item, discipline, prices.get(item.id)) for item, discipline in rows]


async def list_disciplines(
    session: AsyncSession, scope: DisciplineScope
) -> list[WorkDisciplineRead]:
    """`sort_order`, sonra `code` (EV `GET /earned-value/disciplines` ile ayni); kisitli
    kullanici yalniz KENDI disiplinlerini gorur."""
    stmt = select(EvDiscipline).order_by(EvDiscipline.sort_order, EvDiscipline.code)
    if scope.is_restricted:
        stmt = stmt.where(EvDiscipline.id.in_(sorted(scope.discipline_ids or (), key=str)))
    rows = (await session.execute(stmt)).scalars()
    return [WorkDisciplineRead.model_validate(row) for row in rows]

"""Son fiyat saglayicisi — SZL (isveren sozlesme kalemi) (TKL-B3.2 / T29).

Kaynak: `employer_contract_items` (`catalog_item_id` bagli kalemler). "Belge" = PROJE
SOZLESMESI; belge no = proje kodu, belge id = proje id.

## Secim kurali (tek toplu sorgu: alt sorgu GROUP BY + DISTINCT ON; N+1 yok)

1. Ayni projede ayni `catalog_item_id`ye bagli BIRDEN COK kalem varsa o projenin adayi:
   fiyat = MAX(`unit_price`) (T29: ayni belgede birden cok fiyat → EN YUKSEK),
   zaman = o projedeki bu kalemlerin MAX(`price_changed_at`).
2. Projeler arasi: en yeni zaman kazanir. ESITLIKTE deterministik ikincil anahtar:
   proje kodu (kucuk olan), sonra proje id.

Import edilince `app.core.last_price`e KAYDOLUR (router import yan etkisi); kayit bekçisi
`tests/modules/catalog/test_last_price_kaynaklar.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import last_price
from app.core.last_price import LastPrice
from app.modules.contracts.models import EmployerContractItem
from app.modules.projects.models import Project

SOURCE = "SZL"


async def provide(
    session: AsyncSession, catalog_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, LastPrice]:
    item = EmployerContractItem
    per_project = (
        select(
            item.catalog_item_id.label("catalog_item_id"),
            item.project_id.label("project_id"),
            func.max(item.unit_price).label("price"),
            func.max(item.price_changed_at).label("at"),
        )
        .where(item.catalog_item_id.in_(list(catalog_ids)))
        .group_by(item.catalog_item_id, item.project_id)
        .subquery()
    )
    stmt = (
        select(
            per_project.c.catalog_item_id,
            per_project.c.price,
            per_project.c.at,
            Project.code,
            Project.id,
        )
        .join(Project, Project.id == per_project.c.project_id)
        .distinct(per_project.c.catalog_item_id)
        .order_by(per_project.c.catalog_item_id, per_project.c.at.desc(), Project.code, Project.id)
    )
    return {
        catalog_id: LastPrice(
            price=price, at=changed_at, source=SOURCE, doc_no=project_code, doc_id=project_id
        )
        for catalog_id, price, changed_at, project_code, project_id in (await session.execute(stmt))
    }


def register() -> None:
    last_price.register_provider(SOURCE, provide)

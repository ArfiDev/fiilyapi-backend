"""Son fiyat saglayicisi — HK (isveren hakedisi) (TKL-B3.2 / T29).

Kaynak: `progress_payment_lines` ⨝ `progress_payments` (YALNIZ `approved`/`paid`) ⨝
`employer_contract_items` (satirin `contract_item_id` → kalemin `catalog_item_id`).
Fiyat = duzeltilmis B.F. = `round(contract_unit_price * coefficient, 2)` (PG numeric round,
pozitif degerde `calculations.adjusted_unit_price`in ROUND_HALF_UP'una ESDEGER; esdegerlik
testi `test_hk_onayli_ve_odenmis_...`). Zaman = `approved_at`; belge no =
`HK-{proje kodu}-{sira}`; belge id = hakedis id. TASERON hakedisi kaynak DEGILDIR.

## Secim kurali (tek toplu sorgu: alt sorgu GROUP BY + DISTINCT ON; N+1 yok)

1. Ayni hakedisin ayni katalog kalemine dusen birden cok uygun satiri varsa o hakedisin
   adayi: fiyat = MAX(duzeltilmis B.F.), zaman = `approved_at` (T29: ayni belgede birden
   cok fiyat → EN YUKSEK).
2. Hakedisler arasi: en yeni `approved_at` kazanir. ESITLIKTE deterministik ikincil
   anahtar: hakedis id (kucuk olan).

## Bayat taban (T29/S6): HK satiri kaynak OLMAZ

Satir ancak `line.contract_unit_price == employer_contract_items.unit_price` (GUNCEL
sozlesme fiyati; Numeric(18,2) esitligi) ise aday olur; farkliysa o satir DISLANIR.
Senaryo: taslak 100'le acildi, sozlesme T5'te 150 oldu, hakedis T10'da onaylandi → bu HK
satiri kaynak degil (taban bayat); SZL 150 kalir.

`approved_at IS NULL` olan onayli/odenmis hakedis (veri tutarsizligi; servis onayda damga
yazar) DISLANIR: zamansiz fiyat "en yeni" sayilamaz.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import last_price
from app.core.last_price import LastPrice
from app.modules.contracts.models import EmployerContractItem
from app.modules.progress_payments.models import (
    ProgressPayment,
    ProgressPaymentLine,
    ProgressPaymentStatus,
)
from app.modules.projects.models import Project

SOURCE = "HK"
_COUNTED = (ProgressPaymentStatus.approved, ProgressPaymentStatus.paid)


async def provide(
    session: AsyncSession, catalog_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, LastPrice]:
    line, payment, item = ProgressPaymentLine, ProgressPayment, EmployerContractItem
    per_payment = (
        select(
            item.catalog_item_id.label("catalog_item_id"),
            payment.id.label("payment_id"),
            payment.approved_at.label("approved_at"),
            payment.sequence_no.label("sequence_no"),
            payment.project_id.label("project_id"),
            func.max(func.round(line.contract_unit_price * line.coefficient, 2)).label("price"),
        )
        .join(payment, payment.id == line.payment_id)
        .join(item, item.id == line.contract_item_id)
        .where(
            item.catalog_item_id.in_(list(catalog_ids)),
            payment.status.in_(_COUNTED),
            payment.approved_at.is_not(None),
            line.contract_unit_price == item.unit_price,  # bayat taban dislanir
        )
        .group_by(
            item.catalog_item_id,
            payment.id,
            payment.approved_at,
            payment.sequence_no,
            payment.project_id,
        )
        .subquery()
    )
    stmt = (
        select(
            per_payment.c.catalog_item_id,
            per_payment.c.price,
            per_payment.c.approved_at,
            Project.code,
            per_payment.c.sequence_no,
            per_payment.c.payment_id,
        )
        .join(Project, Project.id == per_payment.c.project_id)
        .distinct(per_payment.c.catalog_item_id)
        .order_by(
            per_payment.c.catalog_item_id,
            per_payment.c.approved_at.desc(),
            per_payment.c.payment_id,
        )
    )
    return {
        catalog_id: LastPrice(
            price=price,
            at=approved_at,
            source=SOURCE,
            doc_no=f"HK-{project_code}-{sequence_no}",
            doc_id=payment_id,
        )
        for (
            catalog_id,
            price,
            approved_at,
            project_code,
            sequence_no,
            payment_id,
        ) in (await session.execute(stmt))
    }


def register() -> None:
    last_price.register_provider(SOURCE, provide)

"""Teklif modeli → hesap girdisi/yanit donusumleri (TKL-B4.2). DB BILMEZ (hesap `calc.py`dedir).

Hesap TEK yerdedir: bu dosya yalniz `calc` sonucunu yanit semasina esler.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, timedelta

from app.modules.offers import calc
from app.modules.offers.models import (
    Offer,
    OfferGroup,
    OfferItem,
    OfferRevision,
    OfferRevisionStatus,
)
from app.modules.offers.offer_read_schemas import (
    OfferCustomerTotalsRead,
    OfferGroupRead,
    OfferInternalTotalsRead,
    OfferItemCustomerRead,
    OfferItemInternalRead,
    OfferItemRead,
    OfferRevisionRead,
    OfferTotalsRead,
)

__all__ = [
    "build_item_read",
    "build_revision_read",
    "build_totals",
    "read_item",
    "item_input",
    "revision_result",
    "valid_until",
]


def valid_until(revision: OfferRevision) -> date:
    """Gecerlilik sonu. Sema `offer_date`i sinirlar; yine de tasarsa `date.max`a kenetlenir."""
    try:
        return revision.offer_date + timedelta(days=revision.validity_days)
    except OverflowError:
        return date.max


def item_input(item: OfferItem) -> calc.ItemInput:
    return calc.ItemInput(
        quantity=item.quantity,
        unit_mhr=item.unit_mhr,
        cost_unit_price=item.cost_unit_price,
        overhead_pct=item.overhead_pct,
        profit_pct=item.profit_pct,
        offer_unit_price=item.offer_unit_price,
    )


def revision_result(
    revision: OfferRevision, inputs: Sequence[calc.ItemInput]
) -> calc.RevisionResult:
    return calc.calc_revision(
        inputs,
        overhead_pct=revision.overhead_pct,
        profit_pct=revision.profit_pct,
        vat_pct=revision.vat_pct,
    )


def build_item_read(item: OfferItem, result: calc.ItemResult) -> OfferItemRead:
    customer = (
        OfferItemCustomerRead(unit_price=result.customer.unit_price, amount=result.customer.amount)
        if result.customer is not None
        else None
    )
    internal = result.internal
    return OfferItemRead(
        id=item.id,
        group_id=item.group_id,
        sort_order=item.sort_order,
        catalog_item_id=item.catalog_item_id,
        poz_no=item.poz_no,
        description=item.description,
        unit=item.unit,
        quantity=item.quantity,
        unit_mhr=item.unit_mhr,
        cost_unit_price=item.cost_unit_price,
        overhead_pct=item.overhead_pct,
        profit_pct=item.profit_pct,
        offer_unit_price=item.offer_unit_price,
        priced=result.priced,
        customer=customer,
        internal=OfferItemInternalRead(
            cost=internal.cost,
            overhead=internal.overhead,
            profit=internal.profit,
            profit_pct=internal.profit_pct,
            man_hours=internal.man_hours,
        ),
    )


def read_item(item: OfferItem, revision: OfferRevision) -> OfferItemRead:
    """Tek kalemin okumasi (revizyon yuzdeleriyle hesaplanir)."""
    result = calc.calc_item(
        item_input(item), overhead_pct=revision.overhead_pct, profit_pct=revision.profit_pct
    )
    return build_item_read(item, result)


def build_totals(result: calc.RevisionResult) -> OfferTotalsRead:
    return OfferTotalsRead(
        customer=OfferCustomerTotalsRead(
            net=result.customer.net, vat=result.customer.vat, gross=result.customer.gross
        ),
        internal=OfferInternalTotalsRead(
            cost=result.internal.cost,
            overhead=result.internal.overhead,
            profit=result.internal.profit,
            profit_pct=result.internal.profit_pct,
            man_hours=result.internal.man_hours,
        ),
        unpriced_count=result.unpriced_count,
        unquantified_count=result.unquantified_count,
    )


def sort_groups(groups: Sequence[OfferGroup]) -> list[OfferGroup]:
    return sorted(groups, key=lambda g: (g.sort_order, g.name, str(g.id)))


def sort_items(items: Sequence[OfferItem]) -> list[OfferItem]:
    return sorted(items, key=lambda i: (i.sort_order, i.poz_no, str(i.id)))


def build_revision_read(
    offer: Offer,
    revision: OfferRevision,
    *,
    latest_rev_no: int,
    groups: Sequence[OfferGroup],
    items: Sequence[OfferItem],
) -> OfferRevisionRead:
    by_group: dict[uuid.UUID, list[OfferItem]] = {g.id: [] for g in groups}
    for item in sort_items(items):
        by_group[item.group_id].append(item)
    ordered = [item for group in sort_groups(groups) for item in by_group[group.id]]
    result = revision_result(revision, [item_input(i) for i in ordered])
    read_by_id = {i.id: build_item_read(i, r) for i, r in zip(ordered, result.items, strict=True)}
    is_latest = revision.rev_no == latest_rev_no
    return OfferRevisionRead(
        offer_id=offer.id,
        offer_no=offer.offer_no,
        rev_no=revision.rev_no,
        status=revision.status,
        is_latest=is_latest,
        is_editable=is_latest and revision.status == OfferRevisionStatus.draft,
        offer_date=revision.offer_date,
        validity_days=revision.validity_days,
        valid_until=valid_until(revision),
        overhead_pct=revision.overhead_pct,
        profit_pct=revision.profit_pct,
        vat_pct=revision.vat_pct,
        payment_terms=revision.payment_terms,
        delivery_days=revision.delivery_days,
        price_escalation=revision.price_escalation,
        price_index_type=revision.price_index_type,
        notes=revision.notes,
        sent_at=revision.sent_at,
        won_at=revision.won_at,
        lost_at=revision.lost_at,
        withdrawn_at=revision.withdrawn_at,
        lost_reason=revision.lost_reason,
        winning_amount=revision.winning_amount,
        created_at=revision.created_at,
        updated_at=revision.updated_at,
        groups=[
            OfferGroupRead(
                id=g.id,
                name=g.name,
                sort_order=g.sort_order,
                items=[read_by_id[i.id] for i in by_group[g.id]],
            )
            for g in sort_groups(groups)
        ],
        totals=build_totals(result),
    )

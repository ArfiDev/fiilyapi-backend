"""T2 — BOQ Pydantic semalari (spec §5.1-5.2)."""

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.modules.boq import schemas
from app.modules.boq.schemas import (
    BoqGroupResponse,
    BoqGroupUpdate,
    BoqItemCreate,
    BoqItemResponse,
    BoqItemUpdate,
    BoqTotals,
)
from app.modules.projects.schemas import MetricPlaceholder


def test_metric_placeholder_is_imported_not_copied():
    """Plan T2 notu: kopyalanmaz, projects.schemas'tan import edilir."""
    assert schemas.MetricPlaceholder is MetricPlaceholder


def _metric(pending_module: str = "contracts") -> MetricPlaceholder:
    return MetricPlaceholder(pending_module=pending_module)


def _item(**overrides) -> BoqItemResponse:
    defaults = dict(
        id=uuid.uuid4(),
        code="01.001",
        description="Kazı (Makine ile)",
        unit="m³",
        quantity=Decimal("1240.000"),
        unit_price=Decimal("280.00"),
        progress_pct=_metric("progress_payments"),
        sort_order=1,
        contract_item_id=None,
        # BOQ-SEC K6: additive alanlar ZORUNLUDUR (varsayilanlari yok) — bir cagri
        # yerinin "tahsis bilmiyorum" diyerek sessizce 0 basmasi engellenir.
        allocated_quantity=Decimal("0.000"),
        unallocated_quantity=Decimal("1240.000"),
    )
    defaults.update(overrides)
    return BoqItemResponse(**defaults)


def test_item_amount_is_derived_and_rounded():
    item = _item(quantity=Decimal("1240.000"), unit_price=Decimal("280.00"))
    assert item.amount == Decimal("347200.00")


def test_item_amount_rounds_half_up():
    item = _item(quantity=Decimal("1.005"), unit_price=Decimal("1.00"))
    # 1.005 -> quantize(0.01, ROUND_HALF_UP) => 1.01 (banker's rounding would give 1.00)
    assert item.amount == Decimal("1.01")


def test_group_total_sums_item_amounts():
    item_a = _item(code="01.001", quantity=Decimal("1240.000"), unit_price=Decimal("280.00"))
    item_b = _item(code="01.002", quantity=Decimal("10.000"), unit_price=Decimal("50.00"))
    group = BoqGroupResponse(
        id=uuid.uuid4(), name="TOPRAK VE TEMEL İŞLERİ", sort_order=1, items=[item_a, item_b]
    )
    assert group.group_total == Decimal("347700.00")


def test_group_total_is_zero_for_empty_group():
    group = BoqGroupResponse(id=uuid.uuid4(), name="BOS GRUP", sort_order=1, items=[])
    assert group.group_total == Decimal("0.00")


def test_totals_serializes_grand_total_as_string():
    totals = BoqTotals(
        contract_total=_metric("contracts"),
        realized_total=_metric("progress_payments"),
        remaining_total=_metric("progress_payments"),
        revision_total=_metric("contracts"),
        grand_total=Decimal("12399900.00"),
        grand_progress_pct=_metric("progress_payments"),
    )
    dumped = totals.model_dump(mode="json")
    assert dumped["grand_total"] == "12399900.00"
    assert dumped["contract_total"]["available"] is False


def test_item_create_rejects_zero_quantity():
    with pytest.raises(ValidationError):
        BoqItemCreate(
            group_id=uuid.uuid4(),
            code="01.001",
            description="Kazı",
            unit="m³",
            quantity=Decimal("0"),
            unit_price=Decimal("280.00"),
        )


def test_item_create_rejects_negative_unit_price():
    with pytest.raises(ValidationError):
        BoqItemCreate(
            group_id=uuid.uuid4(),
            code="01.001",
            description="Kazı",
            unit="m³",
            quantity=Decimal("1"),
            unit_price=Decimal("-1"),
        )


def test_item_create_accepts_valid_payload():
    item = BoqItemCreate(
        group_id=uuid.uuid4(),
        code="01.001",
        description="Kazı",
        unit="m³",
        quantity=Decimal("1"),
        unit_price=Decimal("0"),
    )
    assert item.sort_order == 0


def test_item_update_has_no_site_id_field():
    assert "site_id" not in BoqItemUpdate.model_fields


def test_item_update_rejects_zero_quantity_when_given():
    with pytest.raises(ValidationError):
        BoqItemUpdate(quantity=Decimal("0"))


def test_group_update_has_no_site_id_field():
    assert "site_id" not in BoqGroupUpdate.model_fields


def test_group_update_all_fields_optional():
    update = BoqGroupUpdate()
    assert update.name is None
    assert update.sort_order is None

"""Ayar PUT + teklif kosul PATCH denetim metni: DEGISEN her alan icin `eski → yeni`
(TKL-B4.2, E8; kosul PATCH'i TKL-B4.4).

Hicbir alan degismediyse `None` doner → denetim satiri YAZILMAZ (`catalog` R8 emsali).
Metinlerin kendisi `audit/messages/offers.py`dedir; bu dosya yalniz FARKI bulur. Ayar ve kosul
alanlari AYNI ortak yuzdeler/gun/odeme kosulu kodunu paylasir (`_shared_parts`): fark yalniz
kolon on ekidir (`default_` ayarda, bos kosulda).
"""

from __future__ import annotations

from typing import Any

from app.modules.audit import messages
from app.modules.offers.models import OfferPriceEscalation, OfferRevision, OfferSettings

#: (kolon, etiket) — yuzde alanlari, ayar ekranindaki sirayla.
_PCT_FIELDS = (
    ("overhead_pct", "genel gider"),
    ("profit_pct", "kâr"),
    ("vat_pct", "KDV"),
)

#: Kosul alanlari (revizyon), ayardakilerden FAZLA olanlar dahil — PATCH oncesi anlik goruntu.
CONDITION_FIELDS = (
    "offer_date",
    "validity_days",
    "overhead_pct",
    "profit_pct",
    "vat_pct",
    "payment_terms",
    "delivery_days",
    "price_escalation",
    "price_index_type",
    "notes",
)

_ESCALATION_LABEL = {
    OfferPriceEscalation.fixed: "sabit",
    OfferPriceEscalation.tuik: "TÜİK endeksli",
}


def _shared_parts(before: dict[str, Any], after: Any, prefix: str) -> list[str]:
    """Yuzdeler + gecerlilik gunu + odeme kosulu farki (`prefix`: kolon on eki)."""
    parts: list[str] = []
    for field, label in _PCT_FIELDS:
        name = prefix + field
        if before[name] != getattr(after, name):
            parts.append(
                messages.offer_setting_pct_changed(label, before[name], getattr(after, name))
            )
    days = prefix + "validity_days"
    if before[days] != getattr(after, days):
        parts.append(messages.offer_setting_days_changed(before[days], getattr(after, days)))
    terms = prefix + "payment_terms"
    if before[terms] != getattr(after, terms):
        parts.append(messages.offer_setting_terms_changed(before[terms], getattr(after, terms)))
    return parts


def settings_audit_detail(before: dict[str, Any], after: OfferSettings) -> str | None:
    parts = _shared_parts(before, after, "default_")
    return messages.offer_settings_changed(parts) if parts else None


def snapshot_conditions(revision: OfferRevision) -> dict[str, Any]:
    """PATCH ONCESI kosul degerleri (`conditions_audit_detail` icin)."""
    return {field: getattr(revision, field) for field in CONDITION_FIELDS}


def _escalation_text(escalation: OfferPriceEscalation, index_type: Any) -> str:
    label = _ESCALATION_LABEL[escalation]
    return label if index_type is None else f"{label} ({getattr(index_type, 'value', index_type)})"


def conditions_audit_detail(
    offer_no: str, before: dict[str, Any], after: OfferRevision
) -> str | None:
    """`eski → yeni` metni; hicbir kosul fiilen degismediyse `None` (satir yazilmaz)."""
    parts = _shared_parts(before, after, "")
    if before["offer_date"] != after.offer_date:
        parts.append(messages.offer_date_changed(before["offer_date"], after.offer_date))
    if before["delivery_days"] != after.delivery_days:
        parts.append(messages.offer_delivery_changed(before["delivery_days"], after.delivery_days))
    if (before["price_escalation"], before["price_index_type"]) != (
        after.price_escalation,
        after.price_index_type,
    ):
        parts.append(
            messages.offer_escalation_changed(
                _escalation_text(before["price_escalation"], before["price_index_type"]),
                _escalation_text(after.price_escalation, after.price_index_type),
            )
        )
    if before["notes"] != after.notes:
        parts.append(messages.offer_notes_changed(before["notes"], after.notes))
    return messages.offer_conditions_updated(offer_no, after.rev_no, parts) if parts else None

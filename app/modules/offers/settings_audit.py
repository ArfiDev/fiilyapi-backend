"""Ayar PUT denetim metni: DEGISEN her alan icin `eski → yeni` (TKL-B4.2, E8).

Hicbir alan degismediyse `None` doner → denetim satiri YAZILMAZ (`catalog` R8 emsali).
Metinlerin kendisi `audit/messages/offers.py`dedir; bu dosya yalniz FARKI bulur.
"""

from __future__ import annotations

from typing import Any

from app.modules.audit import messages
from app.modules.offers.models import OfferSettings

#: (kolon, etiket) — yuzde alanlari, ayar ekranindaki sirayla.
_PCT_FIELDS = (
    ("default_overhead_pct", "genel gider"),
    ("default_profit_pct", "kâr"),
    ("default_vat_pct", "KDV"),
)


def settings_audit_detail(before: dict[str, Any], after: OfferSettings) -> str | None:
    parts: list[str] = []
    for field, label in _PCT_FIELDS:
        if before[field] != getattr(after, field):
            parts.append(
                messages.offer_setting_pct_changed(label, before[field], getattr(after, field))
            )
    if before["default_validity_days"] != after.default_validity_days:
        parts.append(
            messages.offer_setting_days_changed(
                before["default_validity_days"], after.default_validity_days
            )
        )
    if before["default_payment_terms"] != after.default_payment_terms:
        parts.append(
            messages.offer_setting_terms_changed(
                before["default_payment_terms"], after.default_payment_terms
            )
        )
    return messages.offer_settings_changed(parts) if parts else None

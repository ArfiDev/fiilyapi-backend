"""Teklif modulu API semalari (TKL-B4.1: yalniz ayar kismi; teklif semalari B4.2).

Yuzdeler YUZDE biriminde (12 = %12). Aralik sabitleri `models`taki CHECK tavanlariyla AYNI
kaynaktan gelir. Para birimi YALNIZ TL (T36) — hicbir semada para birimi alani yoktur.

Maske kovasi: ayar yuzdeleri VARSAYILAN ORANLARDIR (hicbir tutardan turemez) → `kimlik`
(emsal `contracts.advance_pct`); aciklamasi `test_kapsam_oran_kovasi` "paradan turemis oran"
tanimina GIRMEZ. Teklif revizyonunun kar/maliyet alanlari B4.2'de `para` olacaktir.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.field_mask import Hassas
from app.modules.offers.models import MAX_PCT, MAX_PROFIT_PCT, MAX_VALIDITY_DAYS

PAYMENT_TERMS_MAX_LEN = 2000


class OfferSettingsUpdate(BaseModel):
    """`PUT /offers/settings` — TAM degistirme (bes alanin hepsi zorunlu)."""

    model_config = ConfigDict(extra="forbid")

    default_overhead_pct: Annotated[Decimal, Hassas.yok] = Field(
        ge=0, le=MAX_PCT, max_digits=5, decimal_places=2
    )
    default_profit_pct: Annotated[Decimal, Hassas.yok] = Field(
        ge=0, le=MAX_PROFIT_PCT, max_digits=6, decimal_places=2
    )
    default_vat_pct: Annotated[Decimal, Hassas.yok] = Field(
        ge=0, le=MAX_PCT, max_digits=5, decimal_places=2
    )
    default_validity_days: int = Field(ge=1, le=MAX_VALIDITY_DAYS)
    default_payment_terms: str = Field(min_length=1, max_length=PAYMENT_TERMS_MAX_LEN)

    @field_validator("default_payment_terms")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Ödeme koşulu boş olamaz")
        return stripped


class OfferSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    default_overhead_pct: Annotated[Decimal, Hassas.yok]
    default_profit_pct: Annotated[Decimal, Hassas.yok]
    default_vat_pct: Annotated[Decimal, Hassas.yok]
    default_validity_days: int
    default_payment_terms: str
    updated_at: datetime

"""Cekirdek is kalemi katalogu uclari (`/catalog/items`) semalari (TKL-B2.2).

EV (`earned_value/schemas_catalog.py`) bu modulu IMPORT ETMEZ, bu modul da EV'yi: alan
kisitlari (ad/birim uzunlugu, oran > 0 + hassasiyet) ayni kural olarak BURADA yeniden
tanimlanir; DB sinirlariyla (`models.RATE_PRECISION`, kolon uzunluklari) ayni kaynaga baglidir.

Bilesen adlari EV'ninkilerle CAKISMAZ (`WorkItem*`): OpenAPI bilesen adlari tekildir.
`default_contractor_type` `Literal["own", "subcon"]`dur — EV'nin `ContractorType` enum
bileseni yeniden adlandirilmasin diye cekirdek enum sinifi semaya SIZDIRILMAZ.

## PATCH kanonu
Gecmeyen alan dokunulmaz. NOT NULL kolona ACIK `null` → 422 (alan adli, EV ile ayni metin).
Istisna: `description` ve `ref_price` nullable → `null` = temizle.

## Poz no
`poz_no` YALNIZ okuma semasindadir; govdede gelirse `extra="forbid"` 422 verir.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.discipline_ref import DisciplineRef
from app.core.field_scope import Gorunurluk
from app.core.text import FREE_TEXT_MAX_LENGTH
from app.modules.catalog.models import RATE_PRECISION

__all__ = [
    "WorkDisciplineListResponse",
    "WorkDisciplineRead",
    "WorkItemCreate",
    "WorkItemListResponse",
    "WorkItemRead",
    "WorkItemUpdate",
]

_STRICT = ConfigDict(extra="forbid")
_NULL_REJECTED = "Alan boşaltılamaz; değiştirmemek için gövdeden çıkarın."

#: `ev_catalog_items.ref_price` Numeric(18, 2) ve `ck_..._ref_price_nonneg` (>= 0).
REF_PRICE_PRECISION = (18, 2)

ItemName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Uom = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Description = Annotated[str, StringConstraints(max_length=FREE_TEXT_MAX_LENGTH)]
#: `ck_ev_catalog_items_rate_positive` (> 0) + Numeric(12,4): fazla ondalik SESSIZ
#: yuvarlanmasin diye semada reddedilir.
StandardRate = Annotated[
    Decimal, Field(gt=0, max_digits=RATE_PRECISION[0], decimal_places=RATE_PRECISION[1])
]
RefPrice = Annotated[
    Decimal,
    Field(ge=0, max_digits=REF_PRICE_PRECISION[0], decimal_places=REF_PRICE_PRECISION[1]),
]
WorkContractorType = Literal["own", "subcon"]


def _reject_null(value: object) -> object:
    if value is None:
        raise ValueError(_NULL_REJECTED)
    return value


class WorkItemCreate(BaseModel):
    model_config = _STRICT

    discipline_id: uuid.UUID
    name: ItemName
    uom: Uom
    standard_unit_mhr: StandardRate
    default_contractor_type: WorkContractorType
    description: Description | None = None
    ref_price: RefPrice | None = None


class WorkItemUpdate(BaseModel):
    """Kismi guncelleme. `description` ve `ref_price` `null` = temizle; digerleri 422."""

    model_config = _STRICT

    discipline_id: uuid.UUID | None = None
    name: ItemName | None = None
    uom: Uom | None = None
    standard_unit_mhr: StandardRate | None = None
    default_contractor_type: WorkContractorType | None = None
    description: Description | None = None
    ref_price: RefPrice | None = None

    _no_null = field_validator(
        "discipline_id",
        "name",
        "uom",
        "standard_unit_mhr",
        "default_contractor_type",
        mode="before",
    )(_reject_null)


class WorkItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    poz_no: str
    discipline: DisciplineRef
    name: str
    uom: str
    description: str | None
    # Adam-saat/birim standardi: para DEGIL, miktar da DEGIL (katalog standardi; EV KAT'ta her
    # gorene acik) → ACIKCA `kimlik` (hicbir kapsamda gizlenmez; zorunlu alan `null` olmaz).
    standard_unit_mhr: Annotated[Decimal, Gorunurluk.kimlik]
    default_contractor_type: WorkContractorType
    # KDV haric TL birim fiyat: PARA → `limited` rol GORMEZ (alan maskesi).
    ref_price: Annotated[Decimal | None, Gorunurluk.para]
    # `ref_price` ile ayni gizlilik: fiyatin NE ZAMAN degistigi de fiyat bilgisidir.
    price_updated_at: Annotated[datetime | None, Gorunurluk.para]
    standard_updated_at: datetime
    created_at: datetime
    updated_at: datetime

    @field_validator("default_contractor_type", mode="before")
    @classmethod
    def _enum_value(cls, value: object) -> object:
        """ORM cekirdek `ContractorType` enumu → duz metin degeri."""
        return getattr(value, "value", value)


class WorkItemListResponse(BaseModel):
    items: list[WorkItemRead]


class WorkDisciplineRead(BaseModel):
    """Disiplin listesi satiri (secici). `poz_counter` DONMEZ (ic sayac)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    color: str
    default_contractor_type: WorkContractorType
    sort_order: int

    @field_validator("default_contractor_type", mode="before")
    @classmethod
    def _enum_value(cls, value: object) -> object:
        return getattr(value, "value", value)


class WorkDisciplineListResponse(BaseModel):
    items: list[WorkDisciplineRead]

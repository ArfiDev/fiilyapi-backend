"""Planlama (EV) sirket katalogu + disiplin semalari (PLANLAMA-SPEC §3.8 K2/K4, §3.9 B1-9).

Ekranlar: `Planlama - Birim Oran Katalogu` (KAT) ve katalog formunun disiplin secicisi.

## PATCH kanonu (KARARLAR "Acik `null` bir PATCH'te → 422, ALAN ADLI")
Kismi guncelleme govdesinde GECMEYEN alan dokunulmaz; NOT NULL kolona AÇIK `null`
gonderilirse 422 alan adiyla doner. Tek istisna `description` (kolon nullable →
`null` = temizle).

## `extra="forbid"`
Govdede taninmayan alan sessizce yutulmaz (`accounting/schemas.py` emsali); turev
alanlar (`standard_updated_at`, `used_by_site_count`, `actual`) istemciden gelemez.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.discipline_ref import DisciplineRef
from app.core.text import FREE_TEXT_MAX_LENGTH
from app.modules.catalog.models import RATE_PRECISION
from app.modules.earned_value.contractor_bridge import to_engine_ct
from app.modules.earned_value.decimal_out import EvDecimal
from app.modules.earned_value.engine import ContractorType

_STRICT = ConfigDict(extra="forbid")
_NULL_REJECTED = "Alan boşaltılamaz; değiştirmemek için gövdeden çıkarın."

DisciplineCode = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)
]
DisciplineName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
#: `ck_ev_disciplines_color_hex` ile ayni kural; semada da durur ki 422 alan adli olsun.
HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")]
ItemName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Uom = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Description = Annotated[str, StringConstraints(max_length=FREE_TEXT_MAX_LENGTH)]
#: `ck_ev_catalog_items_rate_positive` (> 0) + Numeric(12,4): fazla ondalik SESSIZ
#: yuvarlanmasin diye semada reddedilir.
StandardRate = Annotated[
    EvDecimal, Field(gt=0, max_digits=RATE_PRECISION[0], decimal_places=RATE_PRECISION[1])
]


def _reject_null(value: object) -> object:
    if value is None:
        raise ValueError(_NULL_REJECTED)
    return value


# ------------------------------------------------------------------ disiplin


class DisciplineCreate(BaseModel):
    model_config = _STRICT

    code: DisciplineCode
    name: DisciplineName
    color: HexColor
    default_contractor_type: ContractorType
    sort_order: int = 0


class DisciplineUpdate(BaseModel):
    """Kismi guncelleme. Butun kolonlar NOT NULL → acik `null` her alanda 422."""

    model_config = _STRICT

    code: DisciplineCode | None = None
    name: DisciplineName | None = None
    color: HexColor | None = None
    default_contractor_type: ContractorType | None = None
    sort_order: int | None = None

    _no_null = field_validator(
        "code", "name", "color", "default_contractor_type", "sort_order", mode="before"
    )(_reject_null)


class DisciplineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    color: str
    default_contractor_type: ContractorType
    sort_order: int
    used_by_item_count: int = 0  # katalog is tipi sayisi
    used_by_site_count: int = 0  # BOQ grubu eslenmis / baseline'i olan santiye; >0 silinemez
    user_count: int = 0  # disiplinin atandigi kullanici sayisi (DSC-B0); >0 silinemez

    # TKL-B1: ORM (cekirdek `ContractorType`) → motor enumu (`contractor_bridge`).
    _engine_ct = field_validator("default_contractor_type", mode="before")(to_engine_ct)


# ------------------------------------------------------------------- katalog


class CatalogItemCreate(BaseModel):
    model_config = _STRICT

    discipline_id: uuid.UUID
    name: ItemName
    uom: Uom
    standard_unit_mhr: StandardRate
    default_contractor_type: ContractorType
    description: Description | None = None


class CatalogItemUpdate(BaseModel):
    """Kismi guncelleme. `description` nullable (`null` = temizle); digerleri 422."""

    model_config = _STRICT

    discipline_id: uuid.UUID | None = None
    name: ItemName | None = None
    uom: Uom | None = None
    standard_unit_mhr: StandardRate | None = None
    default_contractor_type: ContractorType | None = None
    description: Description | None = None

    _no_null = field_validator(
        "discipline_id",
        "name",
        "uom",
        "standard_unit_mhr",
        "default_contractor_type",
        mode="before",
    )(_reject_null)


class CatalogActualSite(BaseModel):
    """Gercekleseni besleyen TAMAMLANMIS santiye (KAT acilir satir tablosu).

    B1'de hic uretilmez (`catalog_service.catalog_actuals` bos doner); sekil B3'te
    saha verisiyle dolacak alanlari simdiden sabitler ki openapi devri tek sefer olsun.
    """

    site_id: uuid.UUID
    site_name: str
    end_date: date | None
    qty: EvDecimal
    rate: EvDecimal


class CatalogActual(BaseModel):
    """K4: yalniz tamamlanmis santiyeler, miktar agirlikli ortalama = Σspent / Σqty."""

    avg: EvDecimal | None
    min: EvDecimal | None
    max: EvDecimal | None
    site_count: int
    sites: list[CatalogActualSite]


class CatalogItemRead(BaseModel):
    id: uuid.UUID
    poz_no: str  # TKL-B2: SUNUCU uretir (`MIM-0001`); istemci gonderemez, salt okunur
    discipline: DisciplineRef
    name: str
    uom: str
    standard_unit_mhr: EvDecimal
    default_contractor_type: ContractorType
    description: str | None
    standard_updated_at: datetime
    used_by_site_count: int
    actual: CatalogActual
    #: (avg − standart) ÷ standart, ORAN (0,139 = %13,9); avg yoksa `null`. Yuvarlama
    #: yalniz sunumda (spec §3.6); ±%10 "buyuk fark" esigi (K4) istemcide uygulanir.
    diff_pct: EvDecimal | None

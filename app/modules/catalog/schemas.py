"""Cekirdek is kalemi katalogu uclari (`/catalog/items`) semalari (TKL-B2.2).

EV (`earned_value/schemas_catalog.py`) bu modulu IMPORT ETMEZ, bu modul da EV'yi: alan
kisitlari (ad/birim uzunlugu, oran > 0 + hassasiyet) ayni kural olarak BURADA yeniden
tanimlanir; DB sinirlariyla (`models.RATE_PRECISION`, kolon uzunluklari) ayni kaynaga baglidir.

Bilesen adlari EV'ninkilerle CAKISMAZ (`WorkItem*`): OpenAPI bilesen adlari tekildir.
`default_contractor_type` `Literal["own", "subcon"]`dur — EV'nin `ContractorType` enum
bileseni yeniden adlandirilmasin diye cekirdek enum sinifi semaya SIZDIRILMAZ.

## PATCH kanonu
Gecmeyen alan dokunulmaz. NOT NULL kolona ACIK `null` → 422 (alan adli, EV ile ayni metin).
Istisna: `description`, `ref_price`, `source_code` ve `ref_price_date` nullable → `null` = temizle.

## Kaynak kodu + fiyat tarihi (KAT-B1)
`source_code` Bakanlik/kaynak poz kodu (serbest metin, kirpilir, <= 32; bos/yalniz bosluk →
`null`); kismi UNIQUE. `ref_price_date` fiyatin gecerlilik tarihi — yalniz `ref_price` ile
anlamlidir (kural servis katmaninda, BIRLESTIRILMIS degerde: `catalog.service.resolve_price`).

## Poz no
`poz_no` YALNIZ okuma semasindadir; govdede gelirse `extra="forbid"` 422 verir.
"""

from __future__ import annotations

import unicodedata
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from app.core.discipline_ref import DisciplineRef
from app.core.field_mask import Hassas
from app.core.text import FREE_TEXT_MAX_LENGTH
from app.core.timezone import today
from app.modules.catalog import guards
from app.modules.catalog.models import RATE_PRECISION

__all__ = [
    "LastPriceRead",
    "WorkDisciplineListResponse",
    "WorkDisciplineRead",
    "WorkItemBulkResultRow",
    "WorkItemCreate",
    "WorkItemsBulkCreate",
    "WorkItemsBulkResponse",
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
#: `ev_catalog_items.source_code` String(32); kirpilir, bos → None (`_blank_to_none`).
SOURCE_CODE_MAX_LEN = 32
SourceCode = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=SOURCE_CODE_MAX_LEN)
]
#: Bir toplu istekteki en cok kalem (sozlesme kalemi / teklif toplu uclariyla ayni tavan).
BULK_ITEMS_MAX = 200
BulkSourceConflict = Literal["error", "update_price"]
BulkAction = Literal["created", "price_updated", "unchanged"]


def _reject_null(value: object) -> object:
    if value is None:
        raise ValueError(_NULL_REJECTED)
    return value


#: Unicode kontrol (Cc) ve bicim (Cf: ZWSP, BOM, yon isaretleri...) kategorileri: kaynak
#: kodunda gorunmez ikiz uretir (KAT-B1.1 D2/D3; NUL ayrica DB'de 500 verirdi).
_FORBIDDEN_CODE_CATEGORIES = frozenset({"Cc", "Cf"})


def _clean_source_code(value: str | None) -> str | None:
    """Kirpilmis bos metin (`""`) → `None` ('kod yok'); kontrol/bicim karakteri → 422."""
    if not value:
        return None
    if any(unicodedata.category(ch) in _FORBIDDEN_CODE_CATEGORIES for ch in value):
        raise ValueError(guards.SOURCE_CODE_CONTROL_CHARS)
    return value


#: Fiyat tarihi araligi (CEO karari, KAT-B1.1): 2000-01-01 ≤ tarih ≤ bugun + 366 gun.
REF_PRICE_DATE_MIN = date(2000, 1, 1)
REF_PRICE_DATE_MAX_DAYS_AHEAD = 366


def _check_price_date(value: date | None) -> date | None:
    """Tekil POST/PATCH ve toplu istek AYNI dogrulayiciyi kullanir. `today()` her cagrida
    okunur (testte sabitlenebilir)."""
    if value is None:
        return None
    high = today() + timedelta(days=REF_PRICE_DATE_MAX_DAYS_AHEAD)
    if not REF_PRICE_DATE_MIN <= value <= high:
        raise ValueError(
            guards.REF_PRICE_DATE_OUT_OF_RANGE.format(low=REF_PRICE_DATE_MIN, high=high)
        )
    return value


class WorkItemCreate(BaseModel):
    model_config = _STRICT

    discipline_id: uuid.UUID
    name: ItemName
    uom: Uom
    standard_unit_mhr: Annotated[StandardRate, Hassas.yok]
    default_contractor_type: WorkContractorType
    description: Description | None = None
    ref_price: Annotated[RefPrice | None, Hassas.sozlesme_fiyat] = None
    source_code: SourceCode | None = None
    ref_price_date: Annotated[date | None, Hassas.sozlesme_fiyat] = None

    _clean_code = field_validator("source_code", mode="after")(_clean_source_code)
    _price_date_range = field_validator("ref_price_date", mode="after")(_check_price_date)


class WorkItemUpdate(BaseModel):
    """Kismi guncelleme. `description` ve `ref_price` `null` = temizle; digerleri 422."""

    model_config = _STRICT

    discipline_id: uuid.UUID | None = None
    name: ItemName | None = None
    uom: Uom | None = None
    standard_unit_mhr: Annotated[StandardRate | None, Hassas.yok] = None
    default_contractor_type: WorkContractorType | None = None
    description: Description | None = None
    ref_price: Annotated[RefPrice | None, Hassas.sozlesme_fiyat] = None
    source_code: SourceCode | None = None
    ref_price_date: Annotated[date | None, Hassas.sozlesme_fiyat] = None

    _clean_code = field_validator("source_code", mode="after")(_clean_source_code)
    _price_date_range = field_validator("ref_price_date", mode="after")(_check_price_date)
    _no_null = field_validator(
        "discipline_id",
        "name",
        "uom",
        "standard_unit_mhr",
        "default_contractor_type",
        mode="before",
    )(_reject_null)


class LastPriceRead(BaseModel):
    """Kalemin son gorulen birim fiyati (TKL-B3.2; port: `app/core/last_price.py`).

    `source` kasitli olarak `str`: kaynak kumesi buyur (TKL B4, SA B7); `Literal` olsaydi yeni
    kaynak eklendiginde sema 500 verir ve OpenAPI enumu her turda degisirdi. Bilinen degerler
    `SZL` (sozlesme), `HK` (isveren hakedisi); istemci bilmedigi kaynagi ham metin gosterir.
    `doc_no` insan etiketi (SZL: proje kodu, HK: `HK-<proje kodu>-<sira>`), `doc_id` belge
    baglantisi icin (SZL: proje id, HK: hakedis id).
    """

    model_config = ConfigDict(from_attributes=True)

    # Iceride de `para`: maske ust alani zaten bosaltir; etiket para-alani bekcisinin ic modeli
    # de acik siniflandirmasi icin gerekir (savunma derinligi).
    price: Annotated[Decimal | None, Hassas.sozlesme_fiyat]
    at: datetime
    source: str
    doc_no: str
    doc_id: uuid.UUID | None


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
    standard_unit_mhr: Annotated[Decimal, Hassas.yok]
    default_contractor_type: WorkContractorType
    # KDV haric TL birim fiyat: PARA → `limited` rol GORMEZ (alan maskesi).
    ref_price: Annotated[Decimal | None, Hassas.sozlesme_fiyat]
    # `ref_price` ile ayni gizlilik: fiyatin NE ZAMAN degistigi de fiyat bilgisidir.
    price_updated_at: Annotated[datetime | None, Hassas.sozlesme_fiyat]
    # Son fiyat: TAMAMI para (fiyat + tarih + kaynak) → `limited` rol hicbirini gormez;
    # `price_updated_at` ile ayni gerekce. Kaynaksiz kalemde `null`.
    last_price: Annotated[LastPriceRead | None, Hassas.sozlesme_fiyat] = None
    # Fiyatin gecerlilik tarihi: `ref_price` ile ayni gizlilik (tutarlilik: fiyat gizliyken
    # tarihi de gorunmez). Kaynak kodu para DEGIL → ACIKCA `kimlik`.
    ref_price_date: Annotated[date | None, Hassas.sozlesme_fiyat] = None
    source_code: Annotated[str | None, Hassas.yok] = None
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


class WorkItemsBulkCreate(BaseModel):
    """`POST /catalog/items/bulk` govdesi (hep-ya-hic, 1..200).

    `on_source_conflict`: `source_code`u DB'de ZATEN olan kalem icin `error` (varsayilan →
    422) ya da `update_price` (yalniz `ref_price` + `ref_price_date` guncellenir, digerleri
    yok sayilir). Yeni kodlu / kodsuz kalemler her iki kipte EKLENIR.
    """

    model_config = _STRICT

    items: list[WorkItemCreate] = Field(min_length=1, max_length=BULK_ITEMS_MAX)
    on_source_conflict: BulkSourceConflict = "error"


class WorkItemBulkResultRow(BaseModel):
    index: int
    id: uuid.UUID
    poz_no: str
    action: BulkAction


class WorkItemsBulkResponse(BaseModel):
    created: int
    updated: int
    unchanged: int
    items: list[WorkItemBulkResultRow]


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

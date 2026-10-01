"""Teklif ISTEK semalari (TKL-B4.2). Yanit semalari `offer_read_schemas.py`dedir.

Yuzdeler YUZDE biriminde (12 = %12); aralik sabitleri `models` CHECK tavanlariyla AYNI
kaynaktan gelir. Para birimi YALNIZ TL — hicbir semada para birimi alani yoktur.

## PATCH kanonu
Gecmeyen alan dokunulmaz. NOT NULL kolona ACIK `null` → 422 (alan adli). Istisna: nullable
kolonlar (`scope_summary`, `payment_terms`, `delivery_days`, `price_index_type`, `notes`, kalemde
`cost_unit_price`, `overhead_pct`, `profit_pct`, `offer_unit_price`) → `null` = temizle.

## Kalem: katalog bagi DEGISMEZ
`catalog_item_id` ve katalogdan KOPYA alanlar (`poz_no`, `description`, `unit`) PATCH govdesinde
ACIK Turkce mesajla 422 verir (`extra="forbid"` genel mesajina birakilmaz).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.core.text import FREE_TEXT_MAX_LENGTH
from app.modules.offers.models import (
    MAX_PCT,
    MAX_PROFIT_PCT,
    MAX_VALIDITY_DAYS,
    OfferPriceEscalation,
)
from app.modules.offers.schemas import PAYMENT_TERMS_MAX_LEN
from app.modules.projects.models import PriceIndexType

#: Toplu kalem ekleme tavani (`contracts.EMPLOYER_ITEMS_BULK_MAX` ile AYNI sayi).
OFFER_ITEMS_BULK_MAX = 200

_STRICT = ConfigDict(extra="forbid")
_NULL_REJECTED = "Alan boşaltılamaz; değiştirmemek için gövdeden çıkarın."

Pct = Annotated[Decimal, Field(ge=0, le=MAX_PCT, max_digits=5, decimal_places=2)]
ProfitPct = Annotated[Decimal, Field(ge=0, le=MAX_PROFIT_PCT, max_digits=6, decimal_places=2)]
#: Tavanlar (E4): hesap `Decimal` tasmasina dusmesin diye girdiler SINIRLIDIR. Kalem tutari
#: <= 1e12 x 2 x 10,9999 x 1e9 ≈ 2,2e22 → 25 hane (`calc` 60 haneli baglamda kosar).
MAX_UNIT_PRICE = Decimal("1000000000000")  # 1e12 TL (maliyet / elle B.F.)
MAX_QUANTITY = Decimal("1000000000")  # 1e9
MAX_UNIT_MHR = Decimal("1000000")  # 1e6 adam-saat / birim

Money = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=2)]
#: Birim fiyat (maliyet B.F. / elle teklif B.F.): kurus hassasiyeti + tavan.
UnitPrice = Annotated[Decimal, Field(ge=0, le=MAX_UNIT_PRICE, max_digits=15, decimal_places=2)]
Quantity = Annotated[Decimal, Field(gt=0, le=MAX_QUANTITY, max_digits=14, decimal_places=3)]
ManHourRate = Annotated[Decimal, Field(gt=0, le=MAX_UNIT_MHR, max_digits=12, decimal_places=4)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
GroupName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ScopeSummary = Annotated[str, StringConstraints(max_length=FREE_TEXT_MAX_LENGTH)]
PaymentTerms = Annotated[str, StringConstraints(max_length=PAYMENT_TERMS_MAX_LEN)]
Notes = Annotated[str, StringConstraints(max_length=FREE_TEXT_MAX_LENGTH)]
ValidityDays = Annotated[int, Field(ge=1, le=MAX_VALIDITY_DAYS)]
#: Teklif tarihi araligi: `valid_until` (tarih + gecerlilik gunu) `date.max`i asip 500 vermesin.
MIN_OFFER_DATE = date(2000, 1, 1)
MAX_OFFER_DATE = date(2999, 12, 31)
OfferDate = Annotated[date, Field(ge=MIN_OFFER_DATE, le=MAX_OFFER_DATE)]
DeliveryDays = Annotated[int, Field(ge=0, le=36500)]
SortOrder = Annotated[int, Field(ge=0, le=1_000_000)]


def _reject_null(value: object) -> object:
    if value is None:
        raise ValueError(_NULL_REJECTED)
    return value


# --------------------------------------------------------------------- teklif


class OfferCreate(BaseModel):
    """`POST /offers`. Kosul alanlari verilmezse `offer_settings`ten KOPYALANIR; `price_escalation`
    varsayilani `fixed` (SO-5); `offer_date` varsayilani bugun (Istanbul)."""

    model_config = _STRICT

    employer_id: uuid.UUID
    title: Title
    scope_summary: ScopeSummary | None = None
    offer_date: OfferDate | None = None
    validity_days: ValidityDays | None = None
    overhead_pct: Pct | None = None
    profit_pct: ProfitPct | None = None
    vat_pct: Pct | None = None
    #: Gonderilmezse ayar metni; ACIK `null` = odeme kosulu bos.
    payment_terms: PaymentTerms | None = None
    delivery_days: DeliveryDays | None = None
    price_escalation: OfferPriceEscalation = OfferPriceEscalation.fixed
    price_index_type: PriceIndexType | None = None
    notes: Notes | None = None


class OfferUpdate(BaseModel):
    """`PATCH /offers/{id}` — kunye (revizyonlar arasi ortak alanlar)."""

    model_config = _STRICT

    employer_id: uuid.UUID | None = None
    title: Title | None = None
    scope_summary: ScopeSummary | None = None

    @field_validator("employer_id", "title", mode="before")
    @classmethod
    def _not_null(cls, value: object) -> object:
        return _reject_null(value)


class OfferRevisionUpdate(BaseModel):
    """`PATCH /offers/{id}/revisions/{rev_no}` — kosullar (yalniz son revizyon `draft` iken)."""

    model_config = _STRICT

    offer_date: OfferDate | None = None
    validity_days: ValidityDays | None = None
    overhead_pct: Pct | None = None
    profit_pct: ProfitPct | None = None
    vat_pct: Pct | None = None
    payment_terms: PaymentTerms | None = None
    delivery_days: DeliveryDays | None = None
    price_escalation: OfferPriceEscalation | None = None
    price_index_type: PriceIndexType | None = None
    notes: Notes | None = None

    @field_validator(
        "offer_date",
        "validity_days",
        "overhead_pct",
        "profit_pct",
        "vat_pct",
        "price_escalation",
        mode="before",
    )
    @classmethod
    def _not_null(cls, value: object) -> object:
        return _reject_null(value)


class OfferLoseRequest(BaseModel):
    """`POST …/lose` govdesi (T37) — ikisi de istege bagli."""

    model_config = _STRICT

    lost_reason: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=FREE_TEXT_MAX_LENGTH)]
        | None
    ) = None
    winning_amount: Money | None = None


# ------------------------------------------------------------------ grup / kalem


class OfferGroupCreate(BaseModel):
    model_config = _STRICT

    name: GroupName
    sort_order: SortOrder | None = None


class OfferGroupUpdate(BaseModel):
    model_config = _STRICT

    name: GroupName | None = None
    sort_order: SortOrder | None = None

    @field_validator("name", "sort_order", mode="before")
    @classmethod
    def _not_null(cls, value: object) -> object:
        return _reject_null(value)


class OfferItemCreate(BaseModel):
    """Kalem ekleme (tekil govde ve toplu govdenin ogesi).

    `cost_unit_price` GONDERILMEMISSE maliyet katalogdan onerilir (SO-6: son fiyat → referans
    → bos); ACIK `null` gonderilirse bos kalir. Ayrim `model_fields_set` ile yapilir.
    """

    model_config = _STRICT

    catalog_item_id: uuid.UUID
    group_id: uuid.UUID
    quantity: Quantity
    cost_unit_price: UnitPrice | None = None
    overhead_pct: Pct | None = None
    profit_pct: ProfitPct | None = None
    offer_unit_price: UnitPrice | None = None
    unit_mhr: ManHourRate | None = None
    sort_order: SortOrder | None = None

    @field_validator("unit_mhr", "sort_order", mode="before")
    @classmethod
    def _not_null(cls, value: object) -> object:
        # Gonderilmemis = katalog degeri / siradaki sira; ACIK null anlamsiz → 422.
        return _reject_null(value)


class OfferItemsBulkCreate(BaseModel):
    """`POST …/items/bulk` govdesi (1..200, hep-ya-hic)."""

    model_config = _STRICT

    items: list[OfferItemCreate] = Field(min_length=1, max_length=OFFER_ITEMS_BULK_MAX)


_IMMUTABLE_ITEM_FIELDS = ("catalog_item_id", "poz_no", "description", "unit")


class OfferItemUpdate(BaseModel):
    """`PATCH …/items/{id}`. Katalog bagi ve kopya alanlar DEGISTIRILEMEZ (acik 422)."""

    model_config = _STRICT

    quantity: Quantity | None = None
    cost_unit_price: UnitPrice | None = None
    #: `null` = revizyon geneli kullanilir.
    overhead_pct: Pct | None = None
    profit_pct: ProfitPct | None = None
    #: `null` = elle fiyat kilidi kalkar.
    offer_unit_price: UnitPrice | None = None
    unit_mhr: ManHourRate | None = None
    group_id: uuid.UUID | None = None
    sort_order: SortOrder | None = None

    @model_validator(mode="before")
    @classmethod
    def _immutable_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            bad = [name for name in _IMMUTABLE_ITEM_FIELDS if name in data]
            if bad:
                raise ValueError(
                    f"{', '.join(bad)}: katalogdan gelen alan değiştirilemez; "
                    "kalemi silip katalogdan yeniden ekleyin"
                )
        return data

    @field_validator("quantity", "unit_mhr", "group_id", "sort_order", mode="before")
    @classmethod
    def _not_null(cls, value: object) -> object:
        return _reject_null(value)

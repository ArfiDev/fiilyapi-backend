"""Teklif SABLONU istek/yanit semalari (TKL-B5.1, TKL-PLAN §2.3, T12).

Sablon FIYAT ve MIKTAR TASIMAZ: yalniz ad/aciklama/GG-kar yuzdesi + gruplar + katalog bagli
kalemler. Yuzdeler girdi yuzdesidir (`kimlik` kovasi, B4 karari); yanitta para alani YOKTUR.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.field_scope import Gorunurluk
from app.core.text import FREE_TEXT_MAX_LENGTH
from app.modules.offers.offer_schemas import GroupName, Pct, ProfitPct, SortOrder

_Id = Gorunurluk.kimlik
_STRICT = ConfigDict(extra="forbid")
_NULL_REJECTED = "Alan boşaltılamaz; değiştirmemek için gövdeden çıkarın."

TEMPLATE_NAME_MAX = 80
#: Tam-degistirme govdesi tavanlari (hep-ya-hic; ekran sablonu yuzlerce kalemi gecmez).
TEMPLATE_GROUPS_MAX = 100
TEMPLATE_ITEMS_MAX = 1000
TEMPLATE_GROUPS_TOO_MANY = f"Şablonda en fazla {TEMPLATE_GROUPS_MAX} grup olabilir"
TEMPLATE_ITEMS_TOO_MANY = f"Şablonda en fazla {TEMPLATE_ITEMS_MAX} kalem olabilir"

TemplateName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TEMPLATE_NAME_MAX)
]
TemplateDescription = Annotated[str, StringConstraints(max_length=FREE_TEXT_MAX_LENGTH)]
#: İyimser kilit (TKL-B5.4): istemci okuduğu `updated_at` metnini AYNEN geri yollar; tz'siz → 422.
ExpectedUpdatedAt = Annotated[
    AwareDatetime,
    Field(description="Şablonun okunan `updated_at` değeri (iyimser kilit; uyuşmazsa 409)."),
]


class TemplateCreate(BaseModel):
    """`POST /offers/templates` — BOS sablon (icerik `PUT …/content` ile)."""

    model_config = _STRICT

    name: TemplateName
    description: TemplateDescription | None = None
    overhead_pct: Pct | None = None
    profit_pct: ProfitPct | None = None


class TemplateUpdate(BaseModel):
    """`PATCH /offers/templates/{id}`. `null` = oran/aciklama temizle (ad ve `is_default` NOT NULL:
    acik `null` → 422). `is_default: true` = varsayilan yap (eskisi AYNI islemde duser)."""

    model_config = _STRICT

    name: TemplateName | None = None
    description: TemplateDescription | None = None
    overhead_pct: Pct | None = None
    profit_pct: ProfitPct | None = None
    is_default: bool | None = None
    expected_updated_at: ExpectedUpdatedAt

    @field_validator("name", "is_default", mode="before")
    @classmethod
    def _not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError(_NULL_REJECTED)
        return value


class TemplateItemInput(BaseModel):
    model_config = _STRICT

    catalog_item_id: uuid.UUID


class TemplateGroupInput(BaseModel):
    model_config = _STRICT

    name: GroupName
    items: list[TemplateItemInput] = Field(default_factory=list)


class TemplateContentReplace(BaseModel):
    """`PUT /offers/templates/{id}/content` — TUM gruplar + kalemler TAM degistirilir. Gruplar ve
    kalemler govdedeki SIRAYLA siralanir (`sort_order` = dizin)."""

    model_config = _STRICT

    groups: list[TemplateGroupInput] = Field(max_length=TEMPLATE_GROUPS_MAX)
    expected_updated_at: ExpectedUpdatedAt

    @field_validator("groups")
    @classmethod
    def _total_items(cls, groups: list[TemplateGroupInput]) -> list[TemplateGroupInput]:
        if sum(len(g.items) for g in groups) > TEMPLATE_ITEMS_MAX:
            raise ValueError(TEMPLATE_ITEMS_TOO_MANY)
        return groups


class TemplateFromOffer(BaseModel):
    """`POST /offers/templates/from-offer` — tekliften sablon (fiyat/miktar KOPYALANMAZ)."""

    model_config = _STRICT

    offer_id: uuid.UUID
    rev_no: Annotated[int, Field(ge=0, le=100_000)]
    name: TemplateName
    description: TemplateDescription | None = None


class TemplateCopy(BaseModel):
    """`POST /offers/templates/{id}/copy` — ad verilmezse `<ad> (kopya)`."""

    model_config = _STRICT

    name: TemplateName | None = None


class TemplateItemRead(BaseModel):
    id: uuid.UUID
    sort_order: int
    catalog_item_id: uuid.UUID
    #: Katalogtan CANLI okunur (sablonda kopya saklanmaz).
    poz_no: str
    description: str
    unit: str


class TemplateGroupRead(BaseModel):
    id: uuid.UUID
    name: str
    sort_order: SortOrder
    items: list[TemplateItemRead]


class TemplateListItem(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    overhead_pct: Annotated[Decimal | None, _Id]
    profit_pct: Annotated[Decimal | None, _Id]
    is_default: bool
    group_count: int
    item_count: int
    #: Bu sablondan olusturulmus teklif sayisi (`offers.template_id` bagindan TUREV).
    usage_count: int
    updated_at: datetime


class TemplateListResponse(BaseModel):
    items: list[TemplateListItem]
    total: int


class TemplateDetailRead(TemplateListItem):
    created_at: datetime
    groups: list[TemplateGroupRead]

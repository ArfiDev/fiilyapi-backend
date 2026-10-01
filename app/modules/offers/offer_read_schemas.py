"""Teklif YANIT semalari (TKL-B4.2). Istek semalari `offer_schemas.py`dedir.

## Maske kovalari
* `para`: maliyet, B.F., tutar, GG, kar, toplamlar, `winning_amount`, ELLE B.F. ve paradan
  TUREYEN oranlar (turev kar %, genel kar % — `test_kapsam_oran_kovasi` karari). Maskede `None`
  olur → tipler `| None`.
* `kimlik`: GIRDI yuzdeleri (revizyon/kalem GG-kar-KDV; B4.1 karari: hicbir tutardan turemez),
  adam-saat (katalog emsali), sayaclar, durumlar, kazanma orani (adetlerden turer).
* `operasyonel`: miktar (sozlesme kalemi emsali).

## Musteri / ic AYRIMI (B5)
Her kalem ve toplam `customer` (isveren ciktisinda gorunur: B.F., tutar, net/KDV/brut) ve `internal`
(ASLA gorunmez: maliyet, GG, kar, kar %, adam-saat) ALT NESNELERINDE tasinir; B5 isveren ciktisi
`internal`i atar. Fiyatsiz kalemde `customer` `None`'dur.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel

from app.core.field_scope import Gorunurluk
from app.modules.offers.models import OfferPriceEscalation, OfferRevisionStatus
from app.modules.projects.models import PriceIndexType

_Para = Gorunurluk.para
_Ops = Gorunurluk.operasyonel
_Id = Gorunurluk.kimlik

HistoryKind = Literal["opened", "sent", "won", "lost", "withdrawn"]


class OfferItemCustomerRead(BaseModel):
    """Isverene GORUNUR kalem degerleri (B.F. + tutar)."""

    unit_price: Annotated[Decimal | None, _Para]
    amount: Annotated[Decimal | None, _Para]


class OfferItemInternalRead(BaseModel):
    """IC kalem degerleri. Fiyatsiz kalemde para alanlari `None`, adam-saat DOLUDUR."""

    cost: Annotated[Decimal | None, _Para]
    overhead: Annotated[Decimal | None, _Para]
    profit: Annotated[Decimal | None, _Para]
    #: Elle B.F. varsa TUREV kar %; yoksa uygulanan kar % (paradan turedigi icin `para`).
    profit_pct: Annotated[Decimal | None, _Para]
    man_hours: Annotated[Decimal, _Id]


class OfferItemRead(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    sort_order: int
    catalog_item_id: uuid.UUID
    poz_no: str
    description: str
    unit: str
    quantity: Annotated[Decimal | None, _Ops]
    unit_mhr: Annotated[Decimal, _Id]
    # girdiler (kalem degeri; `None` = revizyon geneli / hesaplanir / maliyet girilmemis)
    cost_unit_price: Annotated[Decimal | None, _Para]
    overhead_pct: Annotated[Decimal | None, _Id]
    profit_pct: Annotated[Decimal | None, _Id]
    offer_unit_price: Annotated[Decimal | None, _Para]
    # hesap sonucu
    priced: bool
    customer: OfferItemCustomerRead | None
    internal: OfferItemInternalRead


class OfferItemsBulkResponse(BaseModel):
    """Toplu ekleme yaniti (sarmalayici: kapsam maskesi modele uygulanir)."""

    items: list[OfferItemRead]


class OfferGroupRead(BaseModel):
    id: uuid.UUID
    name: str
    sort_order: int
    items: list[OfferItemRead]


class OfferGroupBasicRead(BaseModel):
    """Grup POST/PATCH yaniti (kalemsiz)."""

    id: uuid.UUID
    name: str
    sort_order: int


class OfferCustomerTotalsRead(BaseModel):
    net: Annotated[Decimal | None, _Para]
    vat: Annotated[Decimal | None, _Para]
    gross: Annotated[Decimal | None, _Para]


class OfferInternalTotalsRead(BaseModel):
    cost: Annotated[Decimal | None, _Para]
    overhead: Annotated[Decimal | None, _Para]
    profit: Annotated[Decimal | None, _Para]
    #: Genel kar % = kar / (maliyet + GG); payda 0 ise `None`.
    profit_pct: Annotated[Decimal | None, _Para]
    man_hours: Annotated[Decimal, _Id]


class OfferTotalsRead(BaseModel):
    customer: OfferCustomerTotalsRead
    internal: OfferInternalTotalsRead
    unpriced_count: int
    #: Miktari girilmemis kalem sayisi (SO-21); `unpriced_count`tan bagimsiz.
    unquantified_count: int


class OfferRevisionRead(BaseModel):
    """`GET /offers/{id}/revisions/{rev_no}` — kosullar + gruplar + kalemler + toplamlar."""

    offer_id: uuid.UUID
    offer_no: str
    rev_no: int
    status: OfferRevisionStatus
    #: Teklifin SON revizyonu mu.
    is_latest: bool
    #: Icerik yazilabilir mi (son revizyon + taslak).
    is_editable: bool
    offer_date: date
    validity_days: int
    valid_until: date
    overhead_pct: Annotated[Decimal, _Id]
    profit_pct: Annotated[Decimal, _Id]
    vat_pct: Annotated[Decimal, _Id]
    payment_terms: str | None
    delivery_days: int | None
    price_escalation: OfferPriceEscalation
    price_index_type: PriceIndexType | None
    notes: str | None
    sent_at: datetime | None
    won_at: datetime | None
    lost_at: datetime | None
    withdrawn_at: datetime | None
    lost_reason: str | None
    winning_amount: Annotated[Decimal | None, _Para]
    created_at: datetime
    #: Son KAYIT zamani (kosul/grup/kalem yazimi + gecis ilerletir; okuma ilerletmez).
    updated_at: datetime
    groups: list[OfferGroupRead]
    totals: OfferTotalsRead


class OfferRevisionSummaryRead(BaseModel):
    """Teklif detayindaki revizyon ozeti (kalemsiz)."""

    rev_no: int
    status: OfferRevisionStatus
    offer_date: date
    valid_until: date
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None
    won_at: datetime | None
    lost_at: datetime | None
    withdrawn_at: datetime | None
    lost_reason: str | None
    winning_amount: Annotated[Decimal | None, _Para]
    net: Annotated[Decimal | None, _Para]
    gross: Annotated[Decimal | None, _Para]
    unpriced_count: int
    unquantified_count: int


class OfferHistoryEventRead(BaseModel):
    """Revizyon gecmisi olayi: revizyon acilisi + durum damgalari (ayri tablo yok, T32)."""

    at: datetime
    kind: HistoryKind
    rev_no: int
    user_id: uuid.UUID | None
    #: Olayi yapan kullanicinin adi; kullanici silinmisse `None`.
    user_name: str | None


class OfferDetailRead(BaseModel):
    id: uuid.UUID
    offer_no: str
    employer_id: uuid.UUID
    employer_name: str
    title: str
    scope_summary: str | None
    prepared_by_user_id: uuid.UUID | None
    #: Hazirlayanin adi; kullanici silinmisse `None`.
    prepared_by_name: str | None
    #: Sablondan olusturulduysa sablon kimligi (sablon silinmisse `None`).
    template_id: uuid.UUID | None
    #: Teklifin durumu = SON revizyonun durumu.
    status: OfferRevisionStatus
    latest_rev_no: int
    created_at: datetime
    updated_at: datetime
    revisions: list[OfferRevisionSummaryRead]
    history: list[OfferHistoryEventRead]


class OfferListItem(BaseModel):
    id: uuid.UUID
    offer_no: str
    rev_no: int
    title: str
    employer_id: uuid.UUID
    employer_name: str
    scope_summary: str | None
    offer_date: date
    valid_until: date
    status: OfferRevisionStatus
    net: Annotated[Decimal | None, _Para]
    gross: Annotated[Decimal | None, _Para]
    unpriced_count: int
    unquantified_count: int
    created_at: datetime


class OfferStatusSummaryRead(BaseModel):
    status: OfferRevisionStatus
    count: int
    #: Bu durumdaki tekliflerin son revizyon KDV haric toplami.
    net: Annotated[Decimal | None, _Para]


class OfferListSummaryRead(BaseModel):
    """`status` filtresi UYGULANMAZ (kartlar dagilimi gosterir); `q`, `employer_id` ve teklif
    tarihi araligi uygulanir."""

    by_status: list[OfferStatusSummaryRead]
    #: Suresi GECMIS gonderilmis teklif adedi: son revizyon `sent` ve `valid_until` < bugun
    #: (Istanbul gunu; `valid_until` = bugun → suresi gecmemis).
    expired_count: int
    #: Kazanma orani % = kazanilan / (kazanilan + kaybedilen) x 100; payda 0 ise `None`.
    win_rate: Annotated[Decimal | None, _Id]


class OfferListResponse(BaseModel):
    items: list[OfferListItem]
    total: int
    limit: int
    offset: int
    summary: OfferListSummaryRead

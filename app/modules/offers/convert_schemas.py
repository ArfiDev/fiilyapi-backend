"""Teklif → proje DONUSTURME semalari (TKL-B6.2, TKL-PLAN §4.2/§4.5, SO-29…SO-44).

## Govde = "Donustur" ekraninin SON hali (T8)
Ekran teklifin son revizyonunu gruplar + kalemler olarak acar; kullanici kalem cikarir/ekler,
fiyat ve miktari degistirir. Sunucu teklifi OKUYUP yeniden kurmaz: sozlesmeye yazilacak sey
govdedeki `groups` listesidir (sira = govde sirasi, T15). Tasarim bilincli SADE tutuldu:

* `project`: yalniz zorunlu cekirdek (ad, il, tarih penceresi) + birkac serbest alan. Tarih
  penceresi ZORUNLU (SO-35: bolumsuz santiyede EV penceresi = proje baslangic-bitis).
* `contract`: alanlarin cogu istege bagli; verilmeyen = teklif revizyonundan (KDV) ya da sozlesme
  semasinin varsayilani (SO-39). `has_price_escalation` ACIKCA zorunlu (varsayilani True olan bir
  alani sessizce miras birakmak, endeks zorunlulugunu kullanicinin habersiz tetiklerdi).
* `groups[].items[].offer_item_id`: ekranda teklif kaleminden gelen satirin kimligi. Adam-saat
  (`unit_mhr`) sunucuda ONDAN okunur — govdede adam-saat YOKTUR (SO-33: ekranda duzenlenmez).
  Ekranda katalogdan YENI eklenen satirda `null` (adam-saat katalog standardi).
* `group_disciplines`: ad → disiplin kimligi (karisik disiplinli grup icin elle esleme, SO-31).

Sinirlar (miktar/birim fiyat tavanlari) B4 teklif semalariyla AYNI tiplerdir (`Quantity`,
`UnitPrice`): donusturme teklifin kabul ettigini reddetmez ve sozlesme kolonlarini
(`Numeric(14,3)` / `Numeric(18,2)`) tasirmaz.

YANIT PARASIZDIR: iki kisitli izni (`projects` + `contracts`) birlestiren router para alani
donduremez (kapsam bekcisi `test_kapsam_baglantisi`).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.core.text import FREE_TEXT_MAX_LENGTH
from app.modules.offers.offer_schemas import GroupName, Pct, Quantity, UnitPrice
from app.modules.projects.models import PriceIndexType

__all__ = [
    "CONVERT_MAX_ITEMS",
    "ConvertContract",
    "ConvertGroup",
    "ConvertItem",
    "ConvertProject",
    "ConvertRequest",
    "ConvertResponse",
    "ConvertWarning",
]

_STRICT = ConfigDict(extra="forbid")

#: Tek donusturmede toplam kalem tavani (BOQ dagitim hucre tavani 20.000'in cok altinda).
CONVERT_MAX_ITEMS = 2000

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
_Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
_Unit = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
_Description = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=FREE_TEXT_MAX_LENGTH)
]
_Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
_Parcel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
_Address = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
#: `project_contracts.base_index_value` = `Numeric(12, 3)`.
_BaseIndex = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=3)]
_Money = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=2)]

DATES_REVERSED = "project.end_date: Bitiş tarihi başlangıçtan önce olamaz"


class ConvertProject(BaseModel):
    """Yeni projenin cekirdegi. Tip her zaman `taahhut`, taslak DEGIL (SO-37)."""

    model_config = _STRICT

    name: _Text
    city: _Short
    start_date: date
    end_date: date
    category: _Short | None = None
    parcel: _Parcel | None = None
    address: _Address | None = None

    @model_validator(mode="after")
    def _window(self) -> ConvertProject:
        if self.end_date < self.start_date:
            raise ValueError(DATES_REVERSED)
        return self


class ConvertContract(BaseModel):
    """Isveren sozlesmesi. `amount` verilmezse sunucu Σ kalem tutarini yazar (KDV HARIC, kurus)."""

    model_config = _STRICT

    contract_no: _Short
    signature_date: date
    #: `None` = Σ kalem tutari (S-D3). Verilirse o yazilir (kalem toplamindan bagimsiz olabilir).
    amount: _Money | None = None
    #: `None` = teklif revizyonunun KDV'si.
    vat_pct: Pct | None = None
    #: `None` = sozlesme semasi varsayilani (SO-39).
    advance_pct: Pct | None = None
    retainage_pct: Pct | None = None
    late_penalty_daily: _Money | None = None
    has_price_escalation: bool
    #: `None` ve fiyat farki varsa: teklif `tuik` ise teklifin endeks turu; degilse 422.
    index_type: PriceIndexType | None = None
    base_index_value: _BaseIndex | None = None


class ConvertItem(BaseModel):
    model_config = _STRICT

    catalog_item_id: uuid.UUID
    #: Teklifin SON revizyonundaki kalem (adam-saat kaynagi); ekranda yeni eklenen satirda `null`.
    offer_item_id: uuid.UUID | None = None
    code: _Code
    description: _Description
    unit: _Unit
    quantity: Quantity
    unit_price: UnitPrice


class ConvertGroup(BaseModel):
    model_config = _STRICT

    name: GroupName
    items: list[ConvertItem] = Field(min_length=1)


class ConvertRequest(BaseModel):
    model_config = _STRICT

    project: ConvertProject
    contract: ConvertContract
    groups: list[ConvertGroup] = Field(min_length=1)
    #: True = tek santiye acilir ve TUM kalemler tam miktarla ona dagitilir (S-D2).
    open_site: bool = False
    #: `None` = proje adi.
    site_name: _Text | None = None
    #: Grup ADI → disiplin kimligi (SO-31). Santiyesiz donusturmede SAKLANMAZ (SO-32; uyari doner).
    group_disciplines: dict[str, uuid.UUID] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _shape(self) -> ConvertRequest:
        if sum(len(group.items) for group in self.groups) > CONVERT_MAX_ITEMS:
            raise ValueError(f"groups: en fazla {CONVERT_MAX_ITEMS} kalem dönüştürülebilir")
        if self.site_name is not None and not self.open_site:
            raise ValueError("site_name: yalnız open_site açıkken verilebilir")
        return self


class ConvertWarning(BaseModel):
    """Yapisal `code` (istemci metne bakmaz) + Turkce metin; gruba ozgu ise grubun ADI."""

    code: str
    message: str
    group_name: str | None = None


class ConvertResponse(BaseModel):
    """PARASIZ yanit: yeni proje + (varsa) santiye kimlikleri, sayac ve uyarilar."""

    project_id: uuid.UUID
    project_slug: str | None
    project_code: str
    site_id: uuid.UUID | None
    contract_item_count: int
    warnings: list[ConvertWarning]

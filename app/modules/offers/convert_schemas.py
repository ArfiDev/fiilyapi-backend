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
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.core.field_mask import Hassas
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
    "ConvertValidationErrorOut",
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


class ConvertProject(BaseModel):
    """Yeni projenin cekirdegi. Tip her zaman `taahhut`, taslak DEGIL (SO-37)."""

    model_config = _STRICT

    #: Elle proje kodu (mockup "Proje kodu*"). `None` = sunucu `PRJ-YYYY-NNN` uretir. Kural
    #: `ProjectCreate.code` ile AYNI (serbest metin, 1–50; ek: bas/son bosluk kirpilir).
    #: SABAH ONAYI adayi: PRJ-YYYY-NNN biciminde zorlama YOK (proje olusturma da zorlamaz).
    code: _Code | None = None
    name: _Text
    city: _Short
    start_date: date
    end_date: date
    category: _Short | None = None
    parcel: _Parcel | None = None
    address: Annotated[_Address | None, Hassas.yok] = None


class ConvertContract(BaseModel):
    """Isveren sozlesmesi. `amount` verilmezse sunucu Σ kalem tutarini yazar (KDV HARIC, kurus)."""

    model_config = _STRICT

    contract_no: _Short
    signature_date: date
    #: `None` = Σ kalem tutari (S-D3). Verilirse o yazilir (kalem toplamindan bagimsiz olabilir).
    amount: Annotated[_Money | None, Hassas.sozlesme_fiyat] = None
    #: `None` = teklif revizyonunun KDV'si.
    vat_pct: Annotated[Pct | None, Hassas.yok] = None
    #: `None` = sozlesme semasi varsayilani (SO-39).
    advance_pct: Annotated[Pct | None, Hassas.yok] = None
    retainage_pct: Annotated[Pct | None, Hassas.yok] = None
    late_penalty_daily: Annotated[_Money | None, Hassas.sozlesme_fiyat] = None
    has_price_escalation: bool
    #: `None` ve fiyat farki varsa: teklif `tuik` ise teklifin endeks turu; degilse 422.
    index_type: PriceIndexType | None = None
    base_index_value: Annotated[_BaseIndex | None, Hassas.yok] = None


class ConvertItem(BaseModel):
    model_config = _STRICT

    catalog_item_id: uuid.UUID
    #: Teklifin SON revizyonundaki kalem (adam-saat kaynagi); ekranda yeni eklenen satirda `null`.
    offer_item_id: uuid.UUID | None = None
    code: _Code
    description: _Description
    unit: _Unit
    quantity: Annotated[Quantity, Hassas.yok]
    unit_price: Annotated[UnitPrice, Hassas.sozlesme_fiyat]


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
    #: ANAHTARLAR HAM gelir; normalize (strip) + cakisma denetimi SERVISTE (`errors[].loc` =
    #: `["group_disciplines", <NORMALIZE anahtar>]`, ham degil — grup adiyla ayni bicim).
    group_disciplines: dict[str, uuid.UUID] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _shape(self) -> ConvertRequest:
        if sum(len(group.items) for group in self.groups) > CONVERT_MAX_ITEMS:
            raise ValueError(f"groups: en fazla {CONVERT_MAX_ITEMS} kalem dönüştürülebilir")
        return self


class ConvertFieldError(BaseModel):
    """Servis dogrulama 422'sinin tek yapisal hatasi: gövde konumu + mesaj."""

    #: ör. `["groups", 1, "items", 3, "code"]`, `["contract", "base_index_value"]`.
    loc: list[str | int]
    message: str


class ConvertValidationErrorOut(BaseModel):
    """Donusturme hata gövdesi (422 ve proje kodu 409'u). IKI BICIM SOZLESMESI:

    * IS KURALI hatasi (servis dogrulamasi: tarih araligi, `open_site`/`site_name` tutarliligi,
      grup/kalem/endeks/bedel kurallari, `group_disciplines` cakismasi; ve 409 "proje kodu
      kullaniliyor"): `detail` = `str` (`; ` ile birlesik insan metni) + `errors` =
      `[{loc, message}]` (yapisal; FE alan vurgusu icin).
    * GOVDE SEKLI hatasi (tip, eksik alan, uzunluk/aralik, 2000 kalem tavani, bilinmeyen alan —
      Pydantic): STANDART FastAPI 422 — `detail` = hata LISTESI, `errors` YOK.

    `group_disciplines` hatalarinda `loc[1]` gonderilen HAM anahtar degil NORMALIZE (strip)
    anahtardir (grup adiyla ayni bicim; FE grup adiyla eslestirir). Gerekce: ham anahtar bos
    olabilir/birden cok ham anahtar tek normalize anahtara iner; normalize anahtar tekildir.
    """

    detail: str | list[dict[str, Any]]
    errors: list[ConvertFieldError] | None = None


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

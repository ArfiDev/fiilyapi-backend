"""FIN-1 semalari — govde ve yanit uclusu (E10 tablosu + K7 + K8).

## 🔴 `extra="forbid"` — sessiz yok sayma YOK

Govde semalari bilinmeyen alani **422** ile reddeder. Bu modulde kararin IKI
somut karsiligi vardir:

1. **`status` GOVDEDEN GELEMEZ (K7).** Gecis kurallari, terminal korumasi ve
   `direction` uyumu TEK bir kapidan (`POST .../status`) gecmelidir; alan
   duzeltmesi ile durum degisimi ayni uca konsaydi **invaryantin IKI YAZMA
   KAPISI** dogar (BOQ-SEC-B kanonu) ve biri kilitsiz kalirdi. Pydantic'in
   varsayilani `status`u SESSIZCE atmak olurdu — istemci gonderdigi durumun
   yazildigini sanirdi.
2. **`is_due` TUREVDIR (K2).** Govdede kabul edilseydi ayni sinif kusur para
   tarafinda `balance` icin ne ise burada rozet icin o olurdu.

## Tarih SIRASI burada DEGIL, `guards`ta

`due_date >= issue_date` kurali PATCH'te **BIRLESIK** degerler uzerinde kosmak
zorundadir (kullanici yalniz `due_date` gonderse bile kayittaki `issue_date` ile
karsilastirilir). Semaya yazilsaydi ayni kural PATCH yolunda IKINCI kez ve farkli
bicimde yazilirdi (`treasury._assert_cash_has_name` deseninin ayni gerekcesi).

## Yeni kayit HER ZAMAN `portfolio` dogar

`FinancialInstrumentCreate` bir `status` alani TASIMAZ. Gecmise donuk (or. zaten
tahsil edilmis) bir cek girilmek istenirse yol POST + gecis ucudur; boylece her
durum degisimi K2 tablosundan ve denetim gunlugunden gecer.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.field_mask import Hassas
from app.modules.treasury.instruments import derive
from app.modules.treasury.models import (
    FinancialInstrumentDirection,
    FinancialInstrumentKind,
    FinancialInstrumentStatus,
)

__all__ = [
    "FinancialInstrumentCreate",
    "FinancialInstrumentListResponse",
    "FinancialInstrumentResponse",
    "FinancialInstrumentStatusChange",
    "FinancialInstrumentSummaryCard",
    "FinancialInstrumentSummaryResponse",
    "FinancialInstrumentUpdate",
]

#: TEK sabit: her govde semasi ayni karari tasisin, biri unutuldugunda o uctan
#: `status` ya da turev bir alan SIZMASIN.
_SIKI = ConfigDict(extra="forbid")

# Model sinirlari birebir (`financial_instruments`): serial_no String(50) ·
# drawer_name String(200) · description String(200) · bank_name String(100).
_SERIAL_NO = Annotated[str, Field(min_length=1, max_length=50)]
_DRAWER_NAME = Annotated[str, Field(min_length=1, max_length=200)]
_DESCRIPTION = Annotated[str | None, Field(default=None, min_length=1, max_length=200)]
_BANK_NAME = Annotated[str | None, Field(default=None, min_length=1, max_length=100)]


def _zorunlu_metin(deger: str | None) -> str | None:
    """Kenar bosluklarini atar; BOSA DONEN metin bir HATADIR.

    🔴 T3 bulgusu: `min_length=1` tek basina `"   "` gibi yalnizca bosluktan
    olusan bir degeri GECIRIR — "Cek No" ve "Kesideci" sutunlari o zaman ekranda
    GORUNMEZ ama kayit gecerli sayilirdi (E10:104-105 iki sutun da her satirda
    doludur). Boslugu atmak yetmez, ATTIKTAN SONRA bos kalani REDDETMEK gerekir.
    """
    if deger is None:
        return None
    temiz = deger.strip()
    if not temiz:
        raise ValueError("bos birakilamaz")
    return temiz


def _opsiyonel_metin(deger: str | None) -> str | None:
    """Opsiyonel alanda bosa donen metin NULL'dir, hata DEGIL.

    Ayrim bilinclidir: kullanici acik bir alani bosluklarla doldurmus olabilir ve
    bunu "temizle" diye okumak dogrudur; ama ZORUNLU bir alanda ayni girdi
    kimliksiz bir satir uretirdi.
    """
    if deger is None:
        return None
    return deger.strip() or None


#: 🔴 `decimal_places=2` YARIM KURUSU SINIRDA KESER: `0.005` **422**dir, sessizce
#: `0.01`e YUVARLANMAZ — yuvarlansaydi kullanici girdiginden baska bir tutari
#: kaydetmis olur ve fark yalnizca mutabakatta gorunurdu.
#: `gt=0` DB'deki `ck_financial_instruments_amount_positive`in sema karsiligidir
#: ve ondan ONCE kosar.
_AMOUNT = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2), Hassas.banka_kasa]
#: 🔴 GECE KARARI (IZN-B4b): çek/senet tutarı = `banka_kasa` (alacak/borç çeki fark etmez).
KasaTutar = Annotated[Decimal | None, Hassas.banka_kasa]


class FinancialInstrumentCreate(BaseModel):
    """`POST /financial-instruments` — E10 tablosunun yazma yolu.

    ⚠️ **MOCKUP'TA FORM YOKTUR.** E10:65'te `+ Cek Ekle` dugmesi cizilmis ama
    acildiginda gorunecek form CIZILMEMISTIR. Alan kumesi bu yuzden UYDURULMADI:
    tablonun sutunlarindan (E10:104-110) ve emrin K1 tablosundan BIREBIR alindi.
    Formun kendisi frontend dilimi icin ACIK BORCTUR (rapor).
    """

    model_config = _SIKI

    instrument_kind: FinancialInstrumentKind
    direction: FinancialInstrumentDirection
    serial_no: _SERIAL_NO
    drawer_name: _DRAWER_NAME
    description: _DESCRIPTION = None
    bank_name: _BANK_NAME = None
    issue_date: date
    due_date: date
    amount: _AMOUNT
    project_id: uuid.UUID | None = None
    bank_account_id: uuid.UUID | None = None

    _zorunlular = field_validator("serial_no", "drawer_name")(_zorunlu_metin)
    _opsiyoneller = field_validator("description", "bank_name")(_opsiyonel_metin)


class FinancialInstrumentUpdate(BaseModel):
    """`PATCH /financial-instruments/{id}` — KISMI govde, `status` HARIC (K7).

    Her alan `| None`dir ama anlamlari AYRIDIR ve servis ikisini `exclude_unset`
    ile ayirir: gonderilmeyen alan DEGISMEZ, acikca `null` gonderilen alan
    TEMIZLENIR. Zorunlu kolonlara (`serial_no`, `drawer_name`, tarihler, tutar)
    acikca `null` gondermek servis katmaninda korunur — sema hepsini `| None`
    yazmak zorundadir cunku govde kismidir.
    """

    model_config = _SIKI

    instrument_kind: FinancialInstrumentKind | None = None
    direction: FinancialInstrumentDirection | None = None
    serial_no: str | None = Field(default=None, min_length=1, max_length=50)
    drawer_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: _DESCRIPTION = None
    bank_name: _BANK_NAME = None
    issue_date: date | None = None
    due_date: date | None = None
    amount: KasaTutar = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    project_id: uuid.UUID | None = None
    bank_account_id: uuid.UUID | None = None

    # 🔴 AYNI kural PATCH'te de kosar: POST'a konan bir sinir PATCH'i KORUMAZ
    # (BOR-TEMIZ kanonu) — burada unutulsaydi kullanici mevcut bir kaydin
    # "Kesideci" alanini bosluklarla EZEBILIRDI.
    _zorunlular = field_validator("serial_no", "drawer_name")(_zorunlu_metin)
    _opsiyoneller = field_validator("description", "bank_name")(_opsiyonel_metin)


class FinancialInstrumentStatusChange(BaseModel):
    """`POST /financial-instruments/{id}/status` — TEK alan.

    Sema hedefin GECERLILIGINI dogrulamaz, yalniz tipini: kural tablosu
    `transitions.py`dedir ve tabloda olmayan her cift **409**dur. Sema burada
    reddetseydi "gecersiz gecis" hatasi iki ayri kod donduren iki ayri katmana
    bolunurdu ve istemci hangisini bekleyecegini bilemezdi.
    """

    model_config = _SIKI

    status: FinancialInstrumentStatus


class _FinancialInstrumentStored(BaseModel):
    """Yalniz SAKLANAN kolonlar — turev alan tasimaz."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    instrument_kind: FinancialInstrumentKind
    direction: FinancialInstrumentDirection
    serial_no: str
    drawer_name: str
    description: str | None
    bank_name: str | None
    issue_date: date
    due_date: date
    amount: KasaTutar
    status: FinancialInstrumentStatus
    project_id: uuid.UUID | None
    bank_account_id: uuid.UUID | None
    created_at: datetime | None
    updated_at: datetime | None


class FinancialInstrumentResponse(_FinancialInstrumentStored):
    """Satir + 🔴 TUREV `is_due` (K2).

    `status` (kalici) ve `is_due` (turev) **AYRI alanlardir** ve frontend rozeti
    IKISINDEN kurar. Tek alanda birlestirilseydi (or. `status='due'`) kalici
    durum kaybolur, ertesi ay geri getirilemez ve E10:130 `Portfoyde` ile
    E10:121 `Vadede` ayni kolona yazilmis olurdu.
    """

    is_due: bool

    @classmethod
    def from_row(cls, instrument, *, as_of: date) -> "FinancialInstrumentResponse":  # noqa: ANN001
        """Satir + "bugun" ikilisini TEK yerde birlestirir.

        Dort uc (liste · detay · yazma · gecis) da buradan gecer: ayri ayri
        kurulsalardi biri `as_of`u yanlis kaynaktan alir ve liste ile detay ayni
        kayit icin FARKLI rozet basardi.
        """
        return cls(
            **_FinancialInstrumentStored.model_validate(instrument).model_dump(),
            is_due=derive.is_due(instrument.status, instrument.due_date, as_of=as_of),
        )


class FinancialInstrumentListResponse(BaseModel):
    """TB3 liste zarfi: `items` + `total` + `limit`/`offset` (repo kanonu).

    🔴 Sayfalama BASINDAN vardir: portfoy buyur ve sayfasiz liste bir sonraki
    turun borcu olurdu (`/projects` bunu zaten yasadi).

    `as_of` ECHO edilir: `is_due` onsuz DOGRULANAMAZ — istemci kendi saatiyle
    hesaplarsa TR gecesi 00:00-03:00 arasinda sunucudan bir gun sapar
    (`UpcomingPaymentsResponse.as_of` emsali).
    """

    items: list[FinancialInstrumentResponse]
    total: int
    limit: int
    offset: int
    as_of: date


class FinancialInstrumentSummaryCard(BaseModel):
    """E10:70-89'un TEK karti — tutar VE adet (E10:73 `8 adet`).

    Adet ayri bir alandir, `items` uzunlugundan turetilmez: kart TUM kumeyi
    sayar, liste ise SAYFAYI dondurur (BOR-TEMIZ'in "iki sayac ayri seydir"
    kanonu).
    """

    amount: KasaTutar
    count: int


class FinancialInstrumentSummaryResponse(BaseModel):
    """E10:69-90 — dort kart, DORDU DE TUREV (K8).

    🔴 **`due_this_month` obur ucuyle ORTUSUR** ve bu bir kusur degil TANIMDIR:
    portfoydeki bir cek ayni anda "bu ay vadeli"dir. Mockup da 8 + 5 ≠ 3'u ayri
    sayar (E10:73,78,83). Kartlarin toplaminin portfoye esit olmasini bekleyen
    bir test YAZILMAZ; tersine, ortusmeyi KANITLAYAN bir bekci yazilir.

    `as_of` yine ECHO edilir: "Bu Ay Vadeli" hangi aya gore hesaplandigi
    bilinmeden dogrulanamaz.
    """

    portfolio_received: FinancialInstrumentSummaryCard
    issued: FinancialInstrumentSummaryCard
    due_this_month: FinancialInstrumentSummaryCard
    returned_cancelled: FinancialInstrumentSummaryCard
    as_of: date

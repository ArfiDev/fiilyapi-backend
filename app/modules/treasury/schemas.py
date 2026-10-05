"""Hazine şemaları (HZ-1 T3) — banka/kasa hesabı gövde ve yanıtları.

`invoicing/schemas.py` üçlüsünün (Create/Update/Response) kardeşi.

## 🔴 `extra="forbid"` — sessiz yok sayma YOK

Gövde şemaları bilinmeyen alanı **422** ile reddeder. Gerekçe bu modülde
doğrudan PARA'dır: `balance` TÜREVDİR (K2, `balance.py`) ve saklanan bir kolonu
yoktur. Pydantic'in varsayılan davranışı gövdedeki `balance`ı SESSİZCE atmak
olurdu; istemci gönderdiği bakiyenin yazıldığını sanır, ekranda ise formülün
ürettiği başka bir sayı görürdü.

Reddedilenler: `balance` (türev) · `id` · `created_at`/`updated_at` (sunucu
damgaları) · isimsiz her alan.

## Normalizasyon + DOĞRULAMA: IBAN

🔴 Kural artık `app/core/iban.py`dedir ve `personnel` ile PAYLAŞILIR: canlı
smoke'ta `iban: "BUNUBIRIBANDEGIL!!"` gövdesi **201** aldı — ne uzunluk, ne ülke
kodu, ne mod-97 sağlaması vardı. Doğrulama iki modüle ayrı ayrı yazılsaydı biri
güncellenip öteki unutulurdu; bu yüzden dört giriş noktası (iki Create + iki
Update) TEK fonksiyonu çağırır. Alan **nullable KALIR** — zorunlu yapılmadı.

IBAN'dan TÜM boşluklar atılır ve harfler BÜYÜTÜLÜR. Tekillik kısmi bir UNIQUE
indeksle kurulduğu için (`uq_bank_accounts_iban`) ham değer saklansaydı
`TR12 0006…` ile `TR120006…` İKİ AYRI satır olurdu ve kural sessizce
anlamsızlaşırdı. Gruplama bir GÖSTERİM kararıdır (E9:73 `TR33 0006 …`) ve
istemciye aittir; ISO 13616 alfabesi zaten büyük harftir.

## AÇILMAYAN alanlar (spec §1, icat yasağı)

Şube · hesap no · SWIFT · kart rengi · para birimi/kur · bakiye bileşenleri
(kullanılabilir/bloke) çizilmemiştir; gövdede gönderilirlerse **422**dir.
"""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# 🔴 `normalize_iban` ARTIK BURADA TANIMLI DEĞİL, `app/core/iban.py`dedir (kural
# `personnel` ile PAYLAŞILIR). Ad buradan YENİDEN DIŞA AKTARILIR: `service.py`
# onu bu modülden import eder ve `__all__` listeler — bağ koparsa banka hesabı
# create/update akışının tamamı çöker.
from app.core.field_mask import Hassas
from app.core.iban import iban_field_validator, normalize_iban
from app.core.text import FREE_TEXT_MAX_LENGTH
from app.modules.treasury.models import BankAccountType, PaymentMethodKind

__all__ = [
    "BankAccountCreate",
    "BankAccountListResponse",
    "BankAccountResponse",
    "BankAccountUpdate",
    "CashFlowBucket",
    "CashFlowResponse",
    "PaymentCreate",
    "PaymentListResponse",
    "PaymentResponse",
    "UpcomingPaymentItem",
    "UpcomingPaymentsResponse",
    "UpcomingSourceType",
    "normalize_iban",
]

#: Bilinmeyen alan = 422 (modül docstring'i). TEK sabit: her gövde şeması aynı
#: kararı taşısın, biri unutulduğunda o uçtan türev alan sızmasın.
_SIKI = ConfigDict(extra="forbid")

# Model sınırları: `bank_accounts.bank_name` String(100) · `display_name`
# String(100) · `iban` String(34) (ISO 13616 azamisi; TR'de 26).
_BANK_NAME = Field(min_length=1, max_length=100)
_DISPLAY_NAME = Field(default=None, min_length=1, max_length=100)
#: Girişte boşluklu format kabul edilir (E9:73); normalize edilince KISALIR,
#: yani 34 tavanı saklanan değer için de sağlanır.
_IBAN = Field(default=None, min_length=1, max_length=34)
#: Para ölçeği repo standardı: `Numeric(18, 2)`. Negatif MEŞRUDUR — kredili
#: mevduat hesabı eksi açılışla girilebilir ve `ge=0` bunu yasaklardı.
_OPENING_BALANCE = Field(default=Decimal("0.00"), max_digits=18, decimal_places=2)

# 🔴 GECE KARARI (IZN-B4b): banka/kasa bakiyesi, açılış bakiyesi, IBAN, nakit akışı ve yaklaşan
# ödemeler = `banka_kasa` (fail-closed: yaklaşan ödeme tutarı fatura/hakediş/bordro kaynaklı olsa
# da ödeme/tahsilat YÖNÜ kasa-banka hareketidir).
KasaTutar = Annotated[Decimal | None, Hassas.banka_kasa]
#: İSTEK gövdesinde aynı etiket; tip `None` taşımaz.
KasaTutarGirdi = Annotated[Decimal, Hassas.banka_kasa]
Iban = Annotated[str | None, Hassas.banka_kasa]


class BankAccountCreate(BaseModel):
    """`POST /bank-accounts` (E9:70-84 kartının yazma yolu).

    `account_type` KAPALI kümedir ve yalnız iki değer taşır (K1). `display_name`
    şemada opsiyoneldir ama kural TİPE bağlıdır (`cash` → zorunlu) ve tek kaynağı
    servistedir: şemaya tipe bağlı bir validator yazılsaydı aynı kural PATCH
    yolunda İKİNCİ kez (ve birleşik değerler üzerinde farklı biçimde) yazılırdı.

    `is_active` gövdeden gelebilir çünkü kullanımdan kaldırma yolu odur (repo
    kanonu: DELETE değil `is_active=false`).
    """

    model_config = _SIKI

    bank_name: str = _BANK_NAME
    account_type: BankAccountType
    iban: Iban = _IBAN
    display_name: str | None = _DISPLAY_NAME
    opening_balance: KasaTutarGirdi = _OPENING_BALANCE
    is_active: bool = True

    _iban_dogrula = iban_field_validator()


class BankAccountUpdate(BaseModel):
    """`PATCH /bank-accounts/{id}` — KISMİ gövde.

    Her alan `| None`dır ama anlamları AYRIDIR ve servis ikisini ayırır
    (`exclude_unset`): gönderilmeyen alan DEĞİŞMEZ, açıkça `null` gönderilen
    alan TEMİZLENİR. `display_name: null` bir kasada 422'dir (aşağıdaki kural
    birleşik değerler üzerinde koşar).

    `opening_balance` DEĞİŞEBİLİR (spec §4 md.4): elle düzeltme meşrudur ve
    bakiye kendiliğinden yeniden türetilir — saklanan bir bakiye olsaydı bu
    düzeltme iki sayıyı ayrıştırırdı.
    """

    model_config = _SIKI

    bank_name: str | None = Field(default=None, min_length=1, max_length=100)
    account_type: BankAccountType | None = None
    iban: Iban = _IBAN
    display_name: str | None = _DISPLAY_NAME
    opening_balance: KasaTutar = Field(default=None, max_digits=18, decimal_places=2)
    is_active: bool | None = None

    _iban_dogrula = iban_field_validator()


class _BankAccountStored(BaseModel):
    """Yalnız SAKLANAN kolonlar — türev alan taşımaz."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bank_name: str
    account_type: BankAccountType
    iban: Iban
    display_name: str | None
    opening_balance: KasaTutar
    is_active: bool
    created_at: datetime
    updated_at: datetime


class BankAccountResponse(_BankAccountStored):
    """Kart künyesi + 🔴 TÜRETİLMİŞ `balance` (K2).

    `balance` saklanan bir kolon DEĞİLDİR ve buraya `balance.py`nin TEK
    kaynağından gelir; ikinci bir formül yazılsaydı liste ile detay aynı hesap
    için farklı sayı basar ve hiçbir kolon farkı ele vermezdi.

    `opening_balance` de yanıtta DURUR: kullanıcı düzelteceği değeri görmeden
    düzeltemez ve bakiyenin nereden başladığı ekranda okunabilir olmalıdır.
    """

    balance: KasaTutar

    @classmethod
    def from_row(cls, account, balance: Decimal) -> "BankAccountResponse":  # noqa: ANN001
        """Satır + bakiye ikilisini TEK yerde birleştirir.

        Üç uç (liste · detay · yazma) da buradan geçer: ayrı ayrı kurulsalardı
        biri `balance`ı `opening_balance`la doldurabilir ve fark yalnız ödemeli
        hesapta ortaya çıkardı.
        """
        return cls(**_BankAccountStored.model_validate(account).model_dump(), balance=balance)


class BankAccountListResponse(BaseModel):
    """TB3 liste zarfı: `items` + `total` + `limit`/`offset` (repo kanonu)."""

    items: list[BankAccountResponse]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- #
# HZ-1 T4 — ödeme (tahsilat/ödeme) şemaları · FGI:220-247 formu
#
# 🔴 K4: yön kolonu YOKTUR. Ödemenin işareti bağlı FATURANIN `direction`'ından
# gelir; gövdede bir `direction` alanı açılsaydı iki gerçek kaynak olur ve biri
# ötekinden sapabilirdi (`balance.py` modül docstring'i).
#
# 🔴 K5: `paid_total`/`remaining` TÜREVDİR — `invoices` üzerinde `paid_amount`
# kolonu YOKTUR. Bu yüzden ikisi de yalnız YANIT zarfındadır; gövdede
# gönderilirlerse (`extra="forbid"`) 422'dir.
# --------------------------------------------------------------------------- #

#: FGI:233 `Tahsil Edilen Tutar`. `gt=0` DB'deki `ck_payments_amount_positive`in
#: şema karşılığıdır ve ondan ÖNCE koşar: sıfır hiçbir şey ifade etmez, negatif
#: ise gizli bir İADE olurdu (iade/avans hiçbir mockup'ta modellenmemiştir).
#: ⚠️ K6 (aşırı tahsilat) burada DENETLENEMEZ: başka satırların toplamını
#: gerektirir ve KİLİTLİ olmak zorundadır (K7) — o kapı `payments_service`tedir.
_AMOUNT = Field(gt=0, max_digits=18, decimal_places=2)


class PaymentCreate(BaseModel):
    """`POST /invoices/{id}/payments` — FGI:220-247'nin BEŞ alanı BİREBİR.

    Fatura gövdede DEĞİL YOLDADIR (`invoice_id` alanı yoktur): iki yerden
    gelseydi yoldaki kimlik ile gövdedeki ayrışabilir ve ödeme kilitlenen
    faturadan BAŞKA bir faturaya yazılabilirdi.

    `created_by_id` de gövdeden GELMEZ — oturumdan damgalanır.
    """

    model_config = _SIKI

    bank_account_id: uuid.UUID
    method: PaymentMethodKind
    #: 🔴 FIN-PAY — ödemenin bağlandığı çek/senet. **İSTEĞE BAĞLIDIR ve öyle
    #: kalır**: `method='cheque'` iken bile zorunlu değildir, çünkü etiket
    #: (`method`) ile varlık (`financial_instrument_id`) AYRI iki olgudur
    #: (`models.py:226`). Zorunlu kılınsaydı alanı hiç bilmeyen mevcut
    #: istemcilerin tamamı 422 alır ve boş kolonlu mevcut kayıtlar
    #: geçersizleşirdi.
    #:
    #: ⚠️ VAR OLUP OLMADIĞI burada denetlenemez (başka bir tabloya ve KAPSAM
    #: süzgecine bakmayı gerektirir) — o kapı `payments_service`tedir ve
    #: **404**tür, sessiz `None` DEĞİL.
    financial_instrument_id: uuid.UUID | None = None
    amount: Annotated[Decimal, Hassas.banka_kasa] = _AMOUNT
    paid_on: date
    note: str | None = Field(default=None, max_length=FREE_TEXT_MAX_LENGTH)


class PaymentResponse(BaseModel):
    """Ödeme satırı — yalnız SAKLANAN kolonlar.

    `direction` YOKTUR ve türetilip eklenmez: ekran faturanın yönünü zaten
    faturadan okur, burada ikinci bir kopya taşımak K4'ün tek kaynağını bozardı.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    invoice_id: uuid.UUID
    bank_account_id: uuid.UUID
    #: 🔴 FIN-PAY — YALNIZ saklanan kolon. Çek no / vade / keşideci gibi TÜREV
    #: alan EKLENMEZ: eklenseydi aynı olgunun ikinci bir kopyası doğar, çek
    #: düzeltildiğinde ödeme yanıtı BAYAT kalır ve hiçbir kolon farkı bunu ele
    #: vermezdi. İstemci ayrıntıyı `/financial-instruments/{id}`den okur.
    financial_instrument_id: uuid.UUID | None
    method: PaymentMethodKind
    amount: Annotated[Decimal | None, Hassas.banka_kasa]
    paid_on: date
    note: str | None
    created_by_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class PaymentListResponse(BaseModel):
    """TB3 liste zarfı + 🔴 K5'in iki TÜREV toplamı.

    `paid_total` ve `remaining` **TÜM satırlardan** gelir, SAYFADAN DEĞİL:
    sayfadan hesaplansaydı `limit`li bir okumada "kalan" birdenbire büyür ve
    ekran ile K6 kapısı ayrışırdı (kullanıcı ekrana bakıp girdiği tutarda 422
    alırdı).

    `remaining` NEGATİF olabilir: K6 yeni tahsilatı keser ama elle düzeltilmiş
    ya da politika öncesi satırlar toplamı aşabilir — `max(0, …)` ile
    kırpılsaydı aşım ekranda GÖRÜNMEZ olurdu.
    """

    items: list[PaymentResponse]
    total: int
    limit: int
    offset: int
    paid_total: Annotated[Decimal | None, Hassas.banka_kasa]
    remaining: Annotated[Decimal | None, Hassas.banka_kasa]


# --------------------------------------------------------------------------- #
# HZ-1 T5 — türev uçlar (9, 10) · E9:90-125
#
# İkisi de SALT OKUNURDUR: gövde şeması yoktur, dolayısıyla `extra="forbid"`
# kararı burada geçerli değildir. Riskin tamamı YANITTADIR.
# --------------------------------------------------------------------------- #


class UpcomingSourceType(str, enum.Enum):
    """Yaklaşan ödemenin KAYNAK EVRAK tipi (K9).

    E9:113/117/121 ÜÇ kaynak çizer — hakediş · bordro · fatura — ve ÜÇÜ DE
    üretilir; her üyenin arkasında GERÇEK bir vade vardır:

    * `invoice` — `invoices.due_date`, doğrudan bir kolon;
    * `subcontractor_progress_payment` — `approved_at` +
      `subcontractor_contracts.payment_term_days`ten TÜRER;
    * `payroll` — `payroll_periods.payment_due_date` (BY 63 "Son ödeme"),
      doğrudan bir kolon. TB8'de ÖLÇÜLDÜ: alan İK-3'ten beri vardır, yani
      "bordroda vade kavramı yok" gerekçesi yanlıştı. Vade UYDURULMAZ — NULL
      olan dönem listeye girmez (fail-closed) ve yalnız `approved` dönem
      girer: `service.update_period` vadeyi `draft`/`pending_approval`da
      serbestçe değiştirir, `approved`/`paid`de 409 verir, yani vade ancak
      onaydan sonra bir TAAHHÜTTÜR.

    Üye yalnız satır ÜRETİLEBİLDİĞİNDE açılır: üretilemeyen bir üye istemciye
    tutulamayacak bir söz verir (ekran rozeti çizer, hiçbir satır taşımaz).
    """

    invoice = "invoice"
    subcontractor_progress_payment = "subcontractor_progress_payment"
    payroll = "payroll"


class UpcomingPaymentItem(BaseModel):
    """E9:109-125 satırı — mockup'ın BEŞ alanı + kaynak damgası.

    🔴 **K10: aciliyet/renk alanı YOKTUR ve açılmaz.** E9'un kodlaması kendi
    içinde tutarsızdır (2 gün→turuncu, 3 gün→**kırmızı**, 7 gün→yeşil); sunucu
    bir eşik uydurursa mockup'ın hangi yarısının doğru olduğuna KARAR VERMİŞ
    olur. Yanıt yalnız `days_remaining` sayısını ve `source_type`ı verir; renk
    istemcinindir (SA'nın "EN HIZLI rozeti sunucuda üretilmez" kanonu).

    `counterparty` NULLABLE'dır ve olmalıdır: taşeron sözleşmesinde
    `subcontractor_name` taslak aşamasında boş bırakılabilir. Boş metinle
    ya da "Bilinmeyen" gibi bir dolgu ile doldurmak, kullanıcıya var olmayan
    bir kayıt adı gösterirdi.

    `document_no` yalnız TANIMLAYICIDIR, cümle DEĞİL: faturada `invoice_no`,
    hakedişte `sequence_no`nun metni, bordroda dönemin `"YYYY-MM"`i. Sunucu
    "Hakediş #47" cümlesini KURMAZ — o birleştirme (E9:113) `source_type` ile
    birlikte istemcide yapılır ve çeviri/biçim kararı sunucuya sızmaz. Aynı
    kanon bordroda E9:117'nin "Temmuz"unu da DIŞARIDA bırakır: ay adı bir
    ÇEVİRİ kararıdır, `"2026-07"`den istemcide üretilir.

    Bordro satırı `counterparty`yi **`None`** taşır: E9:117 bir karşı taraf adı
    çizmez ("Bordro – Temmuz"), oysa fatura satırı "Yılmaz Elektrik" basar.
    Alan yine de zarfta DURUR — kaynağa göre şekil değiştiren bir zarf,
    istemcide kaynak başına ayrı bir okuma yolu doğururdu.
    """

    model_config = ConfigDict(from_attributes=True)

    source_type: UpcomingSourceType
    source_id: uuid.UUID
    counterparty: str | None
    document_no: str
    due_date: date
    days_remaining: int
    amount: KasaTutar
    #: Kaynağın projesi (IZN-B4b onarımı; eklemeli): satır KENDİ projesindeki rolle maskelenir.
    #: Bordro dönemi şirket geneli → `null` (birleşim, fail-closed). Çözülemeyen fatura da `null`.
    project_id: uuid.UUID | None = None

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "UpcomingPaymentItem", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        """Bordro kaynaklı satırın tutarı personel NET MAAŞ toplamıdır: `maas_kisisel` de gizler
        (kasa-banka hareketi OLMASI maaş gizliliğini kaldırmaz)."""
        if model.source_type is UpcomingSourceType.payroll:
            return etiketler | {Hassas.maas_kisisel}
        return etiketler


class UpcomingPaymentsResponse(BaseModel):
    """`days` ve `as_of` ECHO edilir.

    Sayfalama YOKTUR: pencere `days` ile zaten SINIRLIDIR (tavan 90 gün) ve
    kart bir kesittir, tablo değil — `limit` eklemek pencerenin üstüne ikinci
    ve sessiz bir kırpma koyardı.

    `as_of` olmadan `days_remaining` DOĞRULANAMAZ: istemci kendi saatiyle
    hesaplarsa TR gecesi 00:00-03:00 arasında sunucudan bir gün sapar.
    """

    items: list[UpcomingPaymentItem]
    days: int
    as_of: date


class CashFlowBucket(BaseModel):
    """E9:92-101 serisinin TEK GÜNÜ. İki yön AYRI alandır, net DEĞİL.

    Net tek sayı basılsaydı grafiğin iki eğrisi (giriş/çıkış) çizilemezdi ve
    E9:104-105'in iki toplamı da tek bir farka çökerdi.
    """

    day: date
    inflow: KasaTutar
    outflow: KasaTutar


class CashFlowResponse(BaseModel):
    """E9:90-106 — günlük seri + iki toplam (`Giriş` / `Çıkış`).

    🔴 **Seri SEYREKTİR:** yalnız hareket GÖRMÜŞ günler satır üretir. Ayın 31
    gününü sıfırla doldurmak, veri olmayan bir günü "0 TL hareket oldu"
    diye gösterirdi ve boş ayın 31 satırlık yanıtı "boş" görünmezdi.

    🔴 **Toplamlar `0`dır, NULL DEĞİL** (`coalesce`, `paid_sum` dersi): ödemesiz
    bir ayda `SUM()` NULL döner ve kart "₺" yanında boşluk basardı.
    """

    year: int
    month: int
    series: list[CashFlowBucket]
    inflow_total: KasaTutar
    outflow_total: KasaTutar

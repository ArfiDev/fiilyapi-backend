"""Tedarikci + satin alma talebi semalari (SA spec §2, §4) — T2.

`inventory/schemas.py` uclusunun (Create/Update/Response) kardesi.

## Uzunluk tavanlari kolon sinirlariyla BIREBIRDIR

`String(N)` kolonlarin tavani N'dir; `app.core.text.FREE_TEXT_MAX_LENGTH`
YALNIZCA kolonu `Text` (DB'de sinirsiz) olan alan icindir — bu dosyada tek
ornegi `justification`tir (TB4 standardi, T1'in devrettigi borc).

## Kapsam disi alanlar (spec §5, icat yasagi)

Tedarikci PUANI/performansi · adres/e-posta/IBAN · onay ZINCIRI adimlari ·
teklif isteme e-postasi · FST ekleri (sartname/gorsel) · kismi teslim alanlari
bu semalarda YOKTUR. Govdede gonderilseler bile Pydantic onlari YOK SAYAR.

## Turevler sema katmaninda TASINIR, kolon olarak DEGIL

"Bu Yil Toplam Siparis" (TED 52) · satir tutari · tahmini toplam (FST) ·
"Mevcut Stok" (FST 75) hepsi YANIT alanidir; DB'de karsiliklari yoktur
(`models.py` docstring'i). Kolon acilsaydi iki kaynak olur ve biri otekinden
kacinilmaz sekilde saparadi.
"""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.text import FREE_TEXT_MAX_LENGTH
from app.modules.procurement.mask_types import (
    Maliyet,
    MaliyetGirdi,
    Sayac,
    Yok,
    YokGirdi,
    YokMetin,
)
from app.modules.procurement.models import (
    PaymentTerms,
    PurchaseOrderStatus,
    PurchasePriority,
    PurchaseRequestStatus,
)

# Model sinirlari: `suppliers.name` String(200) · `category` String(100) ·
# `tax_no` String(11) · `phone` String(30).
_SUPPLIER_NAME = Field(min_length=1, max_length=200)
_CATEGORY = Field(default=None, max_length=100)
_TAX_NO = Field(default=None, max_length=11)
_PHONE = Field(default=None, max_length=30)

# `purchase_request_lines.free_text_name` String(200) · `free_text_unit` String(20).
_FREE_TEXT_NAME = Field(default=None, min_length=1, max_length=200)
_FREE_TEXT_UNIT = Field(default=None, min_length=1, max_length=20)

# Olcekler repo standardi: miktar Numeric(14, 3), para Numeric(18, 2).
# `gt=0`: ST'nin NEGATIF duzeltme istisnasi TALEPTE YOKTUR — sifir/negatif
# miktarli bir talep kalemi hicbir durumda anlamli degildir ve esik hesabini
# da bozardi (DB'de de CHECK'lidir, bu ilk katmandir).
_QUANTITY = Field(gt=0, max_digits=14, decimal_places=3)
_UNIT_PRICE = Field(default=None, ge=0, max_digits=18, decimal_places=2)


# --- Tedarikci (TED) ---


class SupplierCreate(BaseModel):
    """`POST /suppliers` govdesi — TED kartinin alanlari.

    `category` SERBEST METINDIR (spec §2): TED alt-etiketi ("Hazir Beton",
    "Nakliye", …) acik uclu bir kumedir, enum ICAT EDILSEYDI her yeni tedarikci
    turu migration gerektirirdi. `payment_terms` ise KAPALI kumedir (TED
    50/71/91/112 + FST 134), bu yuzden enum'dur.

    `tax_no` TEK alandir ve HEM VKN'yi (tuzel kisi, 10 hane) HEM TCKN'yi
    (SAHIS SIRKETI, 11 hane) tasir — sinir `max_length=11` (SUP-TCKN,
    2026-08-25 kullanici karari; onceki 10 sahis tedarikcisini 422'ye
    dusuruyordu). BICIM kurali (yalniz rakam / tam 10 ya da 11 hane)
    UYDURULMAZ: mockup'ta alan zorunlu bile degildir ve dis ulke tedarikcisi
    ya da sahis firmasi kaliba oturmayabilir. Tek sinir kolonun kendi
    genisligidir.
    """

    name: str = _SUPPLIER_NAME
    category: str | None = _CATEGORY
    tax_no: YokMetin = _TAX_NO
    phone: YokMetin = _PHONE
    payment_terms: PaymentTerms
    is_active: bool = True


class SupplierUpdate(BaseModel):
    """`PATCH /suppliers/{id}` — TUM alanlar istege bagli.

    Alanin GONDERILMEMESI ile `null` GONDERILMESI farklidir ve fark
    `model_fields_set` ile korunur: `category: null` etiketi SILER, hic
    gondermemek ona DOKUNMAZ (`StockItemUpdate` dersi).

    **KULLANIMDAN KALDIRMA YOLU BUDUR** (`is_active: false`) — DELETE ucu
    yoktur (spec §4).
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = _CATEGORY
    tax_no: YokMetin = _TAX_NO
    phone: YokMetin = _PHONE
    payment_terms: PaymentTerms | None = None
    is_active: bool | None = None


class SupplierResponse(BaseModel):
    """Tedarikci kunyesi (POST/PATCH yaniti).

    **PUAN ALANI YOKTUR** (spec §5): TED 55-58'deki yildizlarin giris yuzeyi
    hicbir ekranda yoktur ve uydurma bir puan gostermektense hic gostermemek
    dogrudur.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    category: str | None
    tax_no: YokMetin
    phone: YokMetin
    payment_terms: PaymentTerms
    is_active: bool
    created_at: datetime


class SupplierCard(SupplierResponse):
    """TED kartinin TAMAMI: kunye + "Bu Yil Toplam Siparis" TUREVI (TED 52).

    Turev iki parcalidir cunku kart tutari basar ama kullanici "kac siparis"
    sorusunu da sorar; sayac olmadan buyuk bir tutarin tek dev siparisten mi
    yoksa yuzlerce kucuk alimdan mi geldigi anlasilmazdi.

    Siparissiz tedarikcide deger `null` DEGIL SIFIRDIR: ekran "veri yok" ile
    "hic siparis verilmedi"yi ayirt etmek zorunda kalmasin.

    **KAPSAM:** turev yalnizca AKTORUN GORDUGU projelerin siparislerini sayar
    (`repository` gerekcesi) — katalog global olsa da PARA degildir.
    """

    orders_total_this_year: Maliyet
    orders_count_this_year: Sayac


class SupplierListResponse(BaseModel):
    """`personnel`/`audit`/`inventory` liste deseni: `total` + `limit`/`offset`."""

    items: list[SupplierCard]
    total: int
    limit: int
    offset: int


# --- Talep kalemi (FST kalem tablosu) ---


class PurchaseRequestLineCreate(BaseModel):
    """FST kalem tablosunun BIR satiri.

    **IKI KAPILI (XOR):** kalem ya bir stok KARTINA baglanir (`stock_item_id`,
    FST 104 "stok kartindan sec") ya da KATALOGSUZDUR (`free_text_name` +
    `free_text_unit`, FST "yeni malzeme tanimla"). Kural SEMADADIR cunku
    tamamen govdenin kendi icinde cozulur ve boylece ihlal DB'ye hic
    DOKUNMADAN reddedilir (atomikligin ilk katmani) — kod **422**dir, cunku bu
    bir BICIM/KURAL ihlalidir, varlik referansi degil.

    DB'de CHECK ile zorlanmadi (T1 karari): taslak talep GEVSEKTIR ve XOR
    CHECK'i ileride kalemi yarim birakan bir akisi kilitlerdi. Bugun sema her
    yazmada uygular, `validation.submit_blockers` ikinci katman olarak kalir.

    "Mevcut Stok" (FST 75) ve "Tahmini Tutar" (FST 100) BURADA YOKTUR: ikisi de
    TUREVDIR ve yanit semasinda hesaplanir.
    """

    stock_item_id: uuid.UUID | None = None
    free_text_name: str | None = _FREE_TEXT_NAME
    free_text_unit: str | None = _FREE_TEXT_UNIT
    quantity: YokGirdi = _QUANTITY
    estimated_unit_price: Maliyet = _UNIT_PRICE

    @staticmethod
    def _xor_kurali(
        stock_item_id: uuid.UUID | None, free_text_name: str | None, free_text_unit: str | None
    ) -> None:
        serbest = free_text_name is not None or free_text_unit is not None
        if stock_item_id is not None:
            if serbest:
                raise ValueError(
                    "Kalem ya stok kartından seçilir ya da serbest tanımlanır, ikisi birden olmaz."
                )
            return
        if free_text_name is None or free_text_unit is None:
            raise ValueError("Katalogsuz kalemde malzeme adı ve birim zorunludur.")

    @model_validator(mode="after")
    def _xor(self) -> "PurchaseRequestLineCreate":
        self._xor_kurali(self.stock_item_id, self.free_text_name, self.free_text_unit)
        return self


class PurchaseRequestLineUpdate(BaseModel):
    """`PATCH` kalem girdisi (IZN-B4d onarimi): satir bazinda KISMI birlestirme.

    GECE KARARI: `lines` gonderilince tam degistirme semantigi surer (govdede OLMAYAN eski
    satir silinir) AMA satir `id` ile eslesirse GONDERILMEYEN alan (`model_fields_set` disi)
    eski degerden KORUNUR ve satir kimligi degismez. Boylece `maliyet_kar` gizli rolun formu
    (fiyat gelmez) yalniz miktari duzeltince tahmini fiyati SILMEZ. `id`siz satir YENIDIR ve
    `PurchaseRequestLineCreate` kurallarina (XOR, miktar zorunlu) tabidir. POST girdisi
    (`PurchaseRequestLineCreate`) degismedi.
    """

    id: uuid.UUID | None = None
    stock_item_id: uuid.UUID | None = None
    free_text_name: str | None = _FREE_TEXT_NAME
    free_text_unit: str | None = _FREE_TEXT_UNIT
    quantity: Yok = Field(default=None, gt=0, max_digits=14, decimal_places=3)
    estimated_unit_price: Maliyet = _UNIT_PRICE

    @model_validator(mode="after")
    def _yeni_satir_kurallari(self) -> "PurchaseRequestLineUpdate":
        if self.id is not None:
            if "quantity" in self.model_fields_set and self.quantity is None:
                raise ValueError("Miktar boş olamaz.")
            return self
        PurchaseRequestLineCreate._xor_kurali(
            self.stock_item_id, self.free_text_name, self.free_text_unit
        )
        if self.quantity is None:
            raise ValueError("Yeni kalemde miktar zorunludur.")
        return self


class PurchaseRequestLineResponse(BaseModel):
    """Kalem kunyesi + TUREVLER.

    `unit`: stok kartli kalemde KARTIN birimi, katalogsuz kalemde girilen
    birim — ekran iki dal icin ayri sutun okumak zorunda kalmasin.

    `line_total` fiyat yoksa `null`dur ve toplama GIRMEZ; sessizce 0 sayilsaydi
    "tahmini toplam neden dusuk" sorusu cevapsiz kalirdi (ST'nin
    `items_without_price` dersi).

    `current_stock` (FST 75) katalogsuz kalemde `null`dur: stok karti yoksa
    bakiye de yoktur ve 0 yazmak "stokta yok" ile "stok karti bile yok"u ayni
    gosterirdi.

    `sort_order` YANITTA VARDIR ama GOVDEDE (Create) YOKTUR: sunucu onu dizinin
    indeksinden uretir. Ekran, satirlari yeniden sirasa dizmek zorunda kalmadan
    listeyi oldugu gibi basar; alan yine de dondurulur ki istemci kendi yerel
    durumunu (surukle-birak) sunucununkiyle karsilastirabilsin.
    """

    id: uuid.UUID
    sort_order: int
    stock_item_id: uuid.UUID | None
    stock_item_code: str | None
    free_text_name: str | None
    free_text_unit: str | None
    name: str
    unit: str | None
    quantity: YokGirdi
    estimated_unit_price: Maliyet
    line_total: Maliyet
    current_stock: Yok


# --- Talep basligi (FST + SAT) ---


class PurchaseRequestCreate(BaseModel):
    """`POST /purchase-requests` — baslik + kalemler TEK govde, ATOMIK yazilir.

    **TASLAK-FARKINDALIKLI (P6 emsali):** zorunlu TEK alan `project_id`dir.
    FST'de `Oncelik`/`Ihtiyac Tarihi` yildizlidir ama o yildizlar "Onaya
    Gonder" icindir; "Taslak Kaydet" yarim formu saklayabilmelidir. Siki taraf
    `validation.submit_blockers`tadir ve **T3'un** `submit` ucunda kosar.

    `lines` VARSAYILAN OLARAK BOSTUR — ST'nin `min_length=1` kurali burada
    GECERLI DEGILDIR: kalemsiz bir HAREKET anlamsizdir ama kalemsiz bir TASLAK
    gayet anlamlidir.

    **GOVDEDE OLMAYANLAR (icat yasagi):** `request_no` sunucu uretir (§7 S6) ·
    `status` her zaman `draft`tir (gecisler T3'un) · onay meta'si (`approved_*`/
    `rejected_*`) yalnizca T3'un uclariyla dolar. Ucu de govdede gonderilse
    Pydantic onlari yok sayar.

    FST'nin "Teklif Istenecek Tedarikciler" listesi ve "Odeme Vadesi Tercihi"
    burada YOKTUR: sema (spec §2) o kolonlari ACMAZ, teklif toplama T3'un
    `purchase_quotes` alt-kaynagidir.
    """

    project_id: uuid.UUID
    request_date: date | None = None
    priority: PurchasePriority = PurchasePriority.normal
    site_id: uuid.UUID | None = None
    section_id: uuid.UUID | None = None
    needed_by: date | None = None
    justification: str | None = Field(default=None, max_length=FREE_TEXT_MAX_LENGTH)
    quote_deadline: date | None = None
    lines: list[PurchaseRequestLineCreate] = Field(default_factory=list)


class PurchaseRequestUpdate(BaseModel):
    """`PATCH /purchase-requests/{id}` — YALNIZ taslakta (spec §4), tum alanlar
    istege bagli.

    **`lines` gondermek REPLACE'tir:** gelen liste eskisinin YERINE gecer (tek
    atomik islem). Hic GONDERMEMEK kalemlere DOKUNMAZ, BOS liste gondermek
    hepsini SILER — iki durum `model_fields_set` ile ayrilir. Satir `id` tasirsa
    o satirla KISMI birlestirilir (bkz. `PurchaseRequestLineUpdate`). Satir bazli
    ekle/cikar ucu ACILMAZ: FST kalem tablosu tek "Kaydet" ile gonderilir ve
    parcali uclar yarim kaydedilmis bir tablo birakabilirdi.
    """

    project_id: uuid.UUID | None = None
    request_date: date | None = None
    priority: PurchasePriority | None = None
    site_id: uuid.UUID | None = None
    section_id: uuid.UUID | None = None
    needed_by: date | None = None
    justification: str | None = Field(default=None, max_length=FREE_TEXT_MAX_LENGTH)
    quote_deadline: date | None = None
    lines: list[PurchaseRequestLineUpdate] | None = None


class PurchaseRequestBase(BaseModel):
    """SAT tablosunun ve FST basliginin ORTAK kunyesi.

    `estimated_total` TUREVDIR (kalemlerin `quantity × estimated_unit_price`
    toplami) — `purchase_requests`ta tutar kolonu YOKTUR (spec §2). Kolon
    olsaydi kalem degisiminde bayatlar ve ₺500K esigi SESSIZCE atlatilabilirdi.

    `can_delete` AKTORE GOREDIR (`app/core/access.py.can_delete`): taslagi ACAN
    siler, `admin` kosulsuz siler. Ekran dugmeyi bu bayrakla gosterir ve kurali
    kendi yeniden hesaplamaz.
    """

    id: uuid.UUID
    request_no: str
    # URL-4: URL'de tasinacak okunabilir anahtar (`/satinalma/talepler/FST-2026-001`).
    # `request_no`nun KENDISIDIR — ayri bir slug kolonu ACILMADI cunku bu kolon
    # ZATEN `unique=True` ve NOT NULL'dur (`models.py:201`), yani migration
    # gerektirmeden sirket geneli tekil bir anahtar sunar.
    # `None` olabilir: numara URL'e giremeyecek bir karakter tasirsa (`url_safe_key`)
    # istemci `slug ?? id` ile UUID'ye duser.
    slug: str | None = None
    request_date: date
    priority: PurchasePriority
    project_id: uuid.UUID
    site_id: uuid.UUID | None
    section_id: uuid.UUID | None
    needed_by: date | None
    justification: str | None
    status: PurchaseRequestStatus
    quote_deadline: date | None
    approved_by_user_id: uuid.UUID | None
    approved_at: datetime | None
    rejected_at: datetime | None
    rejection_reason: str | None
    created_by_user_id: uuid.UUID
    created_at: datetime
    estimated_total: Maliyet
    can_delete: bool


class PurchaseRequestListRow(PurchaseRequestBase):
    """SAT tablosunun bir satiri — **KALEMLERI TASIMAZ.**

    Tablo kalemleri gostermez; tasimak sayfadaki her satir icin ikinci bir
    sorgu (ve her kalem icin bir bakiye turevi) demek olurdu. Kalem SAYISI
    yeterlidir ve tek toplu sorgudan gelir.
    """

    line_count: int


class PurchaseRequestResponse(PurchaseRequestBase):
    """FST'nin detay govdesi: baslik + kalemler."""

    lines: list[PurchaseRequestLineResponse]


class PurchaseRequestListResponse(BaseModel):
    items: list[PurchaseRequestListRow]
    total: int
    limit: int
    offset: int


# --- Ret gerekcesi (T3) ---


class PurchaseRequestRejection(BaseModel):
    """`POST /purchase-requests/{id}/reject` govdesi.

    Gerekce ZORUNLUDUR (TH emsali): "reddedildi" tek basina eyleme donuk
    degildir — talebi acan sef neyi duzeltip yeniden acacagini bilmelidir.
    `min_length=1` bosu, `strip_whitespace` ise yalniz bosluktan olusan bir
    gerekceyi reddeder (Pydantic kirpmayi ONCE yapar).
    """

    reason: str = Field(min_length=1, max_length=FREE_TEXT_MAX_LENGTH)

    @field_validator("reason")
    @classmethod
    def _bos_olmasin(cls, deger: str) -> str:
        kirpik = deger.strip()
        if not kirpik:
            raise ValueError("Ret gerekçesi zorunludur.")
        return kirpik


# --- Teklif (TEK) ---

# `purchase_quotes.delivery_time` String(100) · `warranty_note` String(200).
_DELIVERY_TIME = Field(min_length=1, max_length=100)
_WARRANTY_NOTE = Field(default=None, max_length=200)
_MONEY = Field(ge=0, max_digits=18, decimal_places=2)


class PurchaseQuoteCreate(BaseModel):
    """`POST /purchase-requests/{id}/quotes` govdesi.

    `delivery_time` SERBEST METINDIR (TEK 67: "3 is gunu" / "Yarin sabah") ve
    gun SAYISINA ZORLANMAZ. Siralanabilir bir `delivery_days` alani ACILSAYDI
    mockup'un yazdigi ifadeler kaybolurdu; "EN HIZLI" rozeti bu yuzden sunucu
    turevi DEGILDIR (bkz. `PurchaseQuoteCard`).

    NAKLIYE IKI HALLIDIR (TEK 90): "Dahil" ya da "Hariç (+₺8.000)". Ikisi
    birden gonderilirse hangisinin gecerli oldugu belirsizdir — 422.
    """

    supplier_id: uuid.UUID
    unit_price: MaliyetGirdi = _MONEY
    delivery_time: str = _DELIVERY_TIME
    warranty_note: str | None = _WARRANTY_NOTE
    payment_terms: PaymentTerms
    shipping_included: bool = False
    shipping_cost: Maliyet = Field(default=None, ge=0, max_digits=18, decimal_places=2)

    @model_validator(mode="after")
    def _nakliye(self) -> "PurchaseQuoteCreate":
        if self.shipping_included and self.shipping_cost is not None:
            raise ValueError("Nakliye dahilse ayrıca nakliye tutarı girilemez.")
        return self


class PurchaseQuoteUpdate(BaseModel):
    """`PATCH …/quotes/{quote_id}` — TUM alanlar istege bagli.

    Nakliye kurali BURADA DEGIL SERVISTE kosar: kismi govde yalniz
    `shipping_cost` tasiyabilir ve kural ancak DB'deki kayitla BIRLESTIRILMIS
    degerler uzerinde anlamlidir (`CustomerValidationError` dersi).

    `supplier_id` DEGISTIRILEMEZ ve alan burada YOKTUR: teklif bir tedarikcinin
    verdigi fiyattir, tedarikcisi degisen sey artik BASKA bir tekliftir.
    """

    unit_price: Maliyet = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    delivery_time: str | None = Field(default=None, min_length=1, max_length=100)
    warranty_note: str | None = _WARRANTY_NOTE
    payment_terms: PaymentTerms | None = None
    shipping_included: bool | None = None
    shipping_cost: Maliyet = Field(default=None, ge=0, max_digits=18, decimal_places=2)


class PurchaseQuoteResponse(BaseModel):
    """Teklifin kunyesi (POST/PATCH yaniti)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    request_id: uuid.UUID
    supplier_id: uuid.UUID
    supplier_name: str
    unit_price: Maliyet
    delivery_time: str
    warranty_note: str | None
    payment_terms: PaymentTerms
    shipping_included: bool
    shipping_cost: Maliyet
    is_selected: bool
    created_at: datetime


class PurchaseQuoteCard(PurchaseQuoteResponse):
    """TEK karsilastirma karti: kunye + TOPLAM MALIYET + "EN IYI FIYAT" rozeti.

    ⚠️ `total_cost` BIRIM FIYAT DEGILDIR: `unit_price × talebin toplam miktari`
    ve nakliye HARICSE `shipping_cost` eklenir. Rozet birim fiyata bakilarak
    verilseydi, nakliyesi haric ucuz gorunen bir teklif "EN IYI FIYAT" damgasi
    alir ve kullanici daha pahali olani secerdi (TEK 90'in tam senaryosu).

    **"EN HIZLI" rozeti YOKTUR** ve uydurulmaz: `delivery_time` serbest
    metindir ("Yarin sabah" ile "3 is gunu" karsilastirilamaz). Rozet mockup'ta
    vardir ama sunucunun sirali bir veri kaynagi yoktur — uydurma bir siralama
    yanlis tedarikciyi one cikarirdi. Istemci isterse metni kendi yorumlar.

    Beraberlikte HEPSI rozetlenir: iki teklif ayni toplamdaysa birini keyfi
    secmek yaniltici olurdu.
    """

    total_cost: Maliyet
    is_best_price: bool


class PurchaseQuoteListResponse(BaseModel):
    """TEK karsilastirma yanitinin zarfi.

    `limit`/`offset` YOKTUR ve bu bilinclidir: teklifler bir TALEBIN altindadir
    ve sayilari doga geregi tek hanelidir (TEK ekrani hepsini yan yana dizer).
    Sayfalama eklenseydi ekran "en iyi fiyat" rozetini eksik bir kume uzerinden
    hesaplamak zorunda kalirdi.

    `request_quantity_total` yanitta durur cunku `total_cost`un carpanidir:
    ekran tutari kendi yeniden hesaplamak isterse tabani gormeli.
    """

    items: list[PurchaseQuoteCard]
    total: int
    request_quantity_total: YokGirdi


# --- Siparis (SIP) ---


class PurchaseOrderCreate(BaseModel):
    """`POST /purchase-orders` — DOGRUDAN (talepsiz) siparis (§7 S3, SIP 35).

    **`request_id` GOVDEDE YOKTUR** ve bu bir eksiklik degil karardir: talebe
    bagli siparisin TEK yolu `select-and-order`dir. Burada kabul edilseydi
    talebin durum makinesi (talep → `ordered`) atlanir ve talebi hala
    `quote_wait` gorunen bir siparis dogardi.

    KALEM DE YOKTUR: spec §2'de siparis kalemi TABLOSU acilmadi — dogrudan
    siparis tek bir `total_amount` tasir (SIP tablosu da tutari tek sutunda
    gosterir).
    """

    project_id: uuid.UUID
    supplier_id: uuid.UUID
    total_amount: MaliyetGirdi = _MONEY
    expected_delivery: date | None = None
    note: str | None = Field(default=None, max_length=FREE_TEXT_MAX_LENGTH)


class PurchaseOrderUpdate(BaseModel):
    """`PATCH /purchase-orders/{id}` — durum gecisi + duzeltilebilir alanlar.

    `status` GONDERILMEZSE durum degismez: not/tarih duzeltmesi bir gecis
    DEGILDIR ve onu bir gecis saymak, kargo notunu duzelten kullaniciyi durum
    makinesine carpar.

    `total_amount` DEGISTIRILEMEZ ve alan burada YOKTUR: siparis, teklifin o
    andaki fiyatinin DONMUS halidir (T1 karari) — tutari elle duzeltilebilseydi
    "sipariste ne uzerinde anlasildi" sorusunun cevabi kaybolurdu.
    """

    status: PurchaseOrderStatus | None = None
    expected_delivery: date | None = None
    note: str | None = Field(default=None, max_length=FREE_TEXT_MAX_LENGTH)


class PurchaseOrderResponse(BaseModel):
    """SIP satiri/detayi. `request_no` TUREVDIR (JOIN) — talepsiz sipariste `null`."""

    id: uuid.UUID
    order_no: str
    request_id: uuid.UUID | None
    request_no: str | None
    quote_id: uuid.UUID | None
    supplier_id: uuid.UUID
    supplier_name: str
    project_id: uuid.UUID
    total_amount: Maliyet
    expected_delivery: date | None
    status: PurchaseOrderStatus
    note: str | None
    created_by_user_id: uuid.UUID
    created_at: datetime


class PurchaseOrderListResponse(BaseModel):
    """TB3 sayfalama zarfi (`inventory`/`personnel` deseni)."""

    items: list[PurchaseOrderResponse]
    total: int
    limit: int
    offset: int


# --- Ozet (T4) ---


class PurchasingSummaryResponse(BaseModel):
    """SAT 69-86 + SIP 38-43 KPI seridi — alan gerekceleri `summary.py`de.

    ZARF YOKTUR (`MetricPlaceholder`): bu ucun TUM alanlarinin veri kaynagi
    VARDIR. Yer tutucu zarf "kaynagi henuz yazilmamis alan" icindir; sifir
    degeri ise gercek bir cevaptir ("hic acik talep yok") ve ikisi ayni sey
    DEGILDIR.

    Kart ETIKETLERI burada YOKTUR — Turkce basliklar ekranin isidir; sunucu
    yalniz sayilari verir (`StockSummaryKpis` deseni).
    """

    open_requests: int
    quote_wait_requests: int
    pending_approval_requests: int
    orders_this_month_total: Maliyet
    active_orders: int
    in_transit_orders: int
    delivered_orders: int

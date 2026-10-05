"""Malzeme kartı + depo şemaları (ST spec §2, §4) — T2.

`personnel/schemas.py` üçlüsünün (Create/Update/Response) kardeşi.

## Uzunluk tavanları kolon sınırlarıyla BİREBİRDİR

`app.core.text.FREE_TEXT_MAX_LENGTH` BURADA KULLANILMAZ ve bu bir eksiklik
değildir: o sabit yalnız kolonu `Text` (DB'de sınırsız) olan alanlar içindir
(`tests/test_serbest_metin_tavani.py` "mevcut dar sınırlar gevşetilmedi"). T2'nin
alanlarının hepsi `String(N)`dir, dolayısıyla tavanları N'dir — 2000'e çekilseydi
kullanıcı 422 yerine anlaşılmaz bir DB hatası alırdı. Tek `Text` alan
(`stock_entries.note`) T3'ündür ve O SABİTİ kullanmak ZORUNDADIR.

## Kapsam dışı alanlar (spec §5, icat yasağı)

Sipariş bağı · tedarikçi kataloğu · bakiye/durum alanı · "Aylık İhtiyaç" ·
belge slotu bu şemalarda YOKTUR. Bakiye ve durum TÜREVDİR (spec §3) ve T3'ün
özet uçlarından gelir; kart gövdesine kolon olarak sızmaz.
"""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.text import FREE_TEXT_MAX_LENGTH
from app.modules.dashboard.schemas import ListPlaceholder
from app.modules.inventory.balance import StockStatus
from app.modules.inventory.mask_types import Maliyet, Sayac, Yok, YokGirdi, YokZarf
from app.modules.inventory.models import (
    StockCategory,
    StockEntryType,
    StockQuality,
)

# Model `String(30)`/`String(200)`/`String(20)` — şema ile DB sınırı AYNI olmalı.
_CODE = Field(min_length=1, max_length=30)
_NAME = Field(min_length=1, max_length=200)
_UNIT = Field(min_length=1, max_length=20)

# Eşik NEGATİF OLAMAZ: durum formülü (spec §3, `%50×min` / `min` / `5×min`)
# negatif bir eşikte anlamını yitirir ve her kalem "fazla" görünürdü. Ölçek
# kolonla aynıdır (`Numeric(14, 3)`).
_MIN_STOCK = Field(default=None, ge=0, max_digits=14, decimal_places=3)

# Depo adı `String(100)`.
_WAREHOUSE_NAME = Field(min_length=1, max_length=100)


class StockItemCreate(BaseModel):
    """`POST /stock/items` gövdesi.

    `unit` SERBEST METİNDİR (spec §2): Ton/Torba/Metre/Adet/m³ kümesi açık
    uçludur ve yeni bir birim migration gerektirmemelidir. `category` ise
    KAPALI kümedir (E3 99 select'i), bu yüzden enum'dur.
    """

    code: str = _CODE
    name: str = _NAME
    category: StockCategory
    unit: str = _UNIT
    min_stock: Yok = _MIN_STOCK
    is_active: bool = True


class StockItemUpdate(BaseModel):
    """`PATCH /stock/items/{id}` — TÜM alanlar isteğe bağlı.

    Alanın GÖNDERİLMEMESİ ile `null` GÖNDERİLMESİ farklıdır ve fark
    `model_fields_set` ile korunur: `min_stock: null` eşiği SİLER (durum
    `None` olur, spec §3), hiç göndermemek ona DOKUNMAZ.

    Kullanımdan kaldırma YOLU budur (`is_active: false`) — DELETE ucu yoktur.
    """

    code: str | None = Field(default=None, min_length=1, max_length=30)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: StockCategory | None = None
    unit: str | None = Field(default=None, min_length=1, max_length=20)
    min_stock: Yok = _MIN_STOCK
    is_active: bool | None = None


class StockItemResponse(BaseModel):
    """Kart künyesi. **Bakiye / durum ALANI YOKTUR** (spec §3): ikisi de
    hareketlerden TÜREVDİR ve T3'ün özet uçlarından gelir. Buraya konsaydı
    katalog listesi her çizilişte hareket tablosunu tarardı."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    category: StockCategory
    unit: str
    min_stock: Yok
    is_active: bool
    created_at: datetime


class StockItemListResponse(BaseModel):
    """`personnel`/`audit`/`users` liste deseni: `total` + `limit`/`offset`."""

    items: list[StockItemResponse]
    total: int
    limit: int
    offset: int


class WarehouseCreate(BaseModel):
    """`POST /warehouses` gövdesi.

    `site_id` NULL = **MERKEZ DEPO** (SG 84 "Merkez Depo (Sincan)"): hiçbir
    şantiyeye bağlı değildir ve görünürlüğü proje kapsamına DEĞİL yalnız stok
    iznine bağlıdır (spec §7 S2b).
    """

    name: str = _WAREHOUSE_NAME
    site_id: uuid.UUID | None = None


class WarehouseUpdate(BaseModel):
    """`PATCH /warehouses/{id}` — YALNIZ ad.

    `site_id` BİLİNÇLİ olarak YOKTUR (`DocumentFolderUpdate` deseni): kapsam
    değiştirmek bir IDOR yüzeyidir — merkez depo şantiyeye çekilerek gizlenebilir
    ya da tersi yapılabilirdi — ve hiçbir mockup depo taşımayı istemez. Alan
    gövdede gönderilse bile Pydantic onu yok sayar, kapsam DEĞİŞMEZ.
    """

    name: str = _WAREHOUSE_NAME


class WarehouseResponse(BaseModel):
    """Depo künyesi. **Bakiye alanı YOKTUR** — kart gövdesiyle aynı gerekçe."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    site_id: uuid.UUID | None
    created_at: datetime


class WarehouseListResponse(BaseModel):
    items: list[WarehouseResponse]
    total: int
    limit: int
    offset: int


# --- Hareket (T3) — SG formunun birebiri, SİPARİŞ ALANLARI HARİÇ ---


class StockEntryLineCreate(BaseModel):
    """SG kalem tablosunun BİR satırı (SG 96-124).

    **"Sipariş" sütunu (SG 95/113) YOKTUR** ve "Tutar" sütunu (SG 101) TÜREVDİR
    (`quantity × unit_price`) — kolon da alan da açılmaz (spec §2, §5).

    `quantity` işaret kısıtı TAŞIMAZ: `adjustment` satırları NEGATİF olabilir
    (§7 S4 — sayım farkı/iade/SARF tek kapısı). Tipe bağlı kural başlıktadır
    (`StockEntryCreate._tip_kurallari`) çünkü satır kendi başına hangi tipte
    olduğunu bilmez.

    `unit_price` NULL olabilir: transfer ve düzeltme satırlarında fiyat yoktur
    ve fiyatsız kalem toplam stok değerine GİRMEZ (§7 S6).
    """

    item_id: uuid.UUID
    quantity: YokGirdi = Field(max_digits=14, decimal_places=3)
    unit_price: Maliyet = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    quality: StockQuality = StockQuality.ok
    # 🔴 STOK-BOLUM: atif SATIR bazindadir (kullanici karari 2026-08-29). Ikisi
    # de opsiyoneldir; tutarlilik kapilari SERVIS katmanindadir (iki tabloya
    # birden bakarlar, sema onlari cozemez).
    section_id: uuid.UUID | None = None
    boq_item_id: uuid.UUID | None = None


class StockEntryCreate(BaseModel):
    """`POST /stock/entries` — başlık + satırlar TEK gövde, atomik yazılır.

    ## SG'den GELEN alanlar
    `entry_type` (53-76) · `entry_date` (84) · `warehouse_id` (84) ·
    `source_warehouse_id` (transfer) · `supplier_name` (86, SERBEST METİN —
    §7 S3) · `delivery_note_no` (87) · `received_by_user_id` (88) · `note` (169).

    ## SG 85 "İlgili Sipariş" — SA T4'te AÇILDI
    `purchase_order_id` isteğe bağlıdır ve YALNIZ `purchase` hareketinde
    verilir. Taşındığında sipariş (ve varsa bağlı talep) otomatik `delivered`
    olur (§7 S4): ayrı bir "mal kabul" ucu YOKTUR.

    ## SG'de OLUP BURAYA ALINMAYANLAR (icat yasağı, spec §5)
    "Sipariş" SÜTUNU (95/113 — satır düzeyi sipariş bağı) · "eksik teslimat"
    rozeti (107) · otomatik tedarikçi bildirimi (176) → **hiçbir dilimde
    açılmaz** (kısmi teslim ayrımı bilinen sınırdır). Belge slotları (149-166)
    → **BC form-slot**. Gövdede gönderilseler bile Pydantic onları yok sayar.

    `note` tavanı `app.core.text.FREE_TEXT_MAX_LENGTH`tir: kolonu `Text`
    (DB'de sınırsız) olan TEK alan budur ve TB4 standardı gereği tavanı
    şemadadır (T1/T2'nin devrettiği borç).
    """

    entry_type: StockEntryType
    entry_date: date
    warehouse_id: uuid.UUID
    source_warehouse_id: uuid.UUID | None = None
    supplier_name: str | None = Field(default=None, max_length=200)
    purchase_order_id: uuid.UUID | None = None
    delivery_note_no: str | None = Field(default=None, max_length=50)
    received_by_user_id: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=FREE_TEXT_MAX_LENGTH)
    # Satırsız hareket bakiyeye HİÇBİR ŞEY katmaz; kaydı boş başlıkla kirletir.
    lines: list[StockEntryLineCreate] = Field(min_length=1)

    @model_validator(mode="after")
    def _tip_kurallari(self) -> "StockEntryCreate":
        """Tipe bağlı kurallar — hepsi GÖVDE düzeyinde, yani **422**.

        Servise değil şemaya konmalarının sebebi: hiçbiri veritabanına bakmaz,
        tamamı gövdenin kendi içinde çözülür ve böylece kural ihlali DB'ye hiç
        DOKUNMADAN reddedilir (atomikliğin ilk katmanı).

        * `transfer` → `source_warehouse_id` ZORUNLU: kaynağı olmayan transfer
          çift bacağın kaynak ayağını üretemez ve YOKTAN STOK YARATIR.
        * kendine transfer YASAK: iki bacak birbirini götürür, kayıt anlamsızdır.
        * `purchase`/`adjustment` → `source_warehouse_id` YASAK: dolu bırakılsa
          bakiye sorgusu o depodan sessizce düşerdi.
        * miktar SIFIR olamaz (her tipte): stoğa hiçbir etkisi olmayan satır.
        * `purchase`/`transfer` → miktar POZİTİF: eksi alım/eksi transfer
          düzeltmenin işidir ve `adjustment` ile yapılır.
        * sipariş bağı YALNIZ `purchase`ta: serbest bırakılsaydı bir depo
          transferi ya da sayım düzeltmesi siparişi sessizce "teslim edildi"
          yapardı (SG 85 alanı da ALIM formundadır).
        """
        if self.purchase_order_id is not None and self.entry_type is not StockEntryType.purchase:
            raise ValueError("Sipariş bağı yalnızca alım hareketinde verilir.")

        if self.entry_type is StockEntryType.transfer:
            if self.source_warehouse_id is None:
                raise ValueError("Transferde kaynak depo zorunludur.")
            if self.source_warehouse_id == self.warehouse_id:
                raise ValueError("Kaynak ve hedef depo aynı olamaz.")
        elif self.source_warehouse_id is not None:
            raise ValueError("Kaynak depo yalnızca transfer hareketinde verilir.")

        for satir in self.lines:
            if satir.quantity == 0:
                raise ValueError("Satır miktarı sıfır olamaz.")
            if self.entry_type is not StockEntryType.adjustment and satir.quantity < 0:
                raise ValueError("Negatif miktar yalnızca manuel düzeltmede kullanılır.")
            if self.entry_type is StockEntryType.transfer and (
                satir.section_id is not None or satir.boq_item_id is not None
            ):
                raise ValueError(
                    "Bölüm/iş kalemi atfı transfer hareketinde verilmez "
                    "(transfer tüketim değildir, iki bacaklıdır)."
                )
        return self


class StockEntryLineResponse(BaseModel):
    """Satır künyesi. **Tutar alanı YOKTUR** — `quantity × unit_price` türevdir."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    item_id: uuid.UUID
    quantity: YokGirdi
    unit_price: Maliyet
    quality: StockQuality
    # STOK-BOLUM: yazilan atif GERI OKUNUR. Bolumun/pozun ADI burada YOKTUR —
    # kunye kimlik tasir; iki JOIN her hareket listesine `sections` ve
    # `boq_items` tablolarini baglardi (`purchase_order_id` emsali).
    section_id: uuid.UUID | None
    boq_item_id: uuid.UUID | None


class StockEntryResponse(BaseModel):
    """Hareket künyesi + satırları.

    SG 85 "İlgili Sipariş" (`purchase_order_id`) SA T4'te gerçeğe döndü.
    Siparişin NUMARASI burada YOKTUR: künye kimliği taşır, ekran sipariş
    detayını kendi ucundan çeker — ikinci bir JOIN her hareket listesine
    satınalma tablosunu bağlardı.

    Bakiye de yoktur: hareket bakiyeyi TAŞIMAZ, bakiye hareketlerden TÜREVDİR
    (spec §3).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entry_type: StockEntryType
    entry_date: date
    warehouse_id: uuid.UUID
    source_warehouse_id: uuid.UUID | None
    supplier_name: str | None
    purchase_order_id: uuid.UUID | None
    delivery_note_no: str | None
    received_by_user_id: uuid.UUID | None
    note: str | None
    created_at: datetime
    lines: list[StockEntryLineResponse]


class StockEntryListResponse(BaseModel):
    items: list[StockEntryResponse]
    total: int
    limit: int
    offset: int


# --- Türev özetler (E3 / ŞS) ---


class StockWarehouseBalance(BaseModel):
    """E3 "Depo" sütunu: kalemin TEK bir depodaki bakiyesi.

    `site_id` NULL ise MERKEZ depodur — ekran şantiye adı basamadığında bunu
    kimliğin yokluğundan değil bu alandan anlar.
    """

    warehouse_id: uuid.UUID
    warehouse_name: str
    site_id: uuid.UUID | None
    balance: YokGirdi


class StockSummaryRow(BaseModel):
    """E3 tablosunun bir satırı: kart künyesi + TÜREVLER.

    `balance` ve `status` KOLON DEĞİLDİR (spec §3): ikisi de hareketlerden
    türetilir ve bu yüzden `StockItemResponse`da yoktur, YALNIZ burada durur.

    `status` `min_stock` yoksa `None`dur — eşik olmadan durum uydurulmaz.

    `last_unit_price` toplam değerin kaynağıdır (§7 S6: SON giriş fiyatı);
    ekran "hangi fiyattan değerlendi" sorusunu bu alandan cevaplar.
    """

    id: uuid.UUID
    code: str
    name: str
    category: StockCategory
    unit: str
    min_stock: Yok
    balance: YokGirdi
    status: StockStatus | None
    last_unit_price: Maliyet
    warehouses: list[StockWarehouseBalance]


class StockSummaryKpis(BaseModel):
    """E3 KPI şeridi (72-89) — SÜZÜLEN KÜMENİN özeti, sayfanın değil.

    `total_value` = Σ (kalemin SON giriş fiyatı × bakiyesi) (§7 S6).
    Ağırlıklı ortalama maliyet İCAT EDİLMEZ.

    `items_without_price`: bakiyesi olup fiyatı olmayan kalem sayısı. Bu kalemler
    değere GİRMEZ ve sessizce 0 SAYILMAZ — sayaç olmasaydı "değer neden düşük"
    sorusu cevapsız kalırdı.

    `pending_orders` ("Bekleyen Sipariş", E3 81): sipariş tablosu YOKTUR, değer
    UYDURULMAZ — `MetricPlaceholder` zarfı SA dilimini bildirir.
    """

    total_value: Maliyet
    critical_count: int
    low_count: int
    total_items: Sayac
    items_without_price: Sayac
    pending_orders: YokZarf


class StockSummaryResponse(BaseModel):
    items: list[StockSummaryRow]
    total: int
    limit: int
    offset: int
    kpis: StockSummaryKpis


class SiteStockRow(BaseModel):
    """ŞS tablosunun bir satırı (Şantiye - Stok, 96-104).

    `balance` YALNIZ o şantiyenin depolarını kapsar; merkez depo (`site_id IS
    NULL`) hiçbir şantiyenin bakiyesine girmez (spec §3).

    `monthly_need` ("Aylık İhtiyaç") ve `section` ("Bölüm") sütunlarının değeri
    ÜRETİLMEZ; mevcut yer tutucu zarfları taşınır — `section` metin listesi
    olduğu için `ListPlaceholder`, `monthly_need` tek sayı olduğu için
    `MetricPlaceholder`.

    🔴 **P-YT3 DENETİMİ (2026-08-23) — GEREKÇE TAZELENDİ.** Eski cümle *"ikisi
    de ileride planlama/BOQ türevi olacaktır"* diyordu; `site_planning` modülü
    o gün geldi ve iki sütun da dolmadı. Bugünkü ölçülmüş olgu:

    | alan | sınıf | engel |
    |---|---|---|
    | `monthly_need` | (B) GEÇERLİ | kaynak YOK ve gelmeyecek (aşağıda) |
    | `section` | (C) TUZAK | makul görünen kaynak VAR ama ANLAMI yanlış |

    **`monthly_need` — kaynak yok.** `PlanResourceKind` yalnız `crew` ve
    `equipment` taşır; `SitePlanRow`da ne `stock_item_id` ne bir malzeme
    miktarı vardır (tek sayısal kolon `planned_worker_count`) ve modelin kendi
    docstring'i *"Plan-gerçekleşen kıyas kolonu YOKTUR (spec §5)"* der. Yani
    bekleyen şey MODÜL değil, o modülün hiç taşımadığı bir KAVRAMdır.

    🔴 **STOK-BOLUM (2026-08-29) — `section` ARTIK DOLAR.** Tablodaki sınıfı (C)
    TUZAK'tan çıktı: `stock_entry_lines.section_id` açıldı ve o alan *"bu satırın
    malzemesi hangi bölüm için hareket etti"* demektir — yani ekranın sorduğu
    şeyin TA KENDİSİ. Zarf, o kalemin bu şantiyedeki hareketlerinde atfedilmiş
    bölümlerin ADLARINI taşır; hiç atıf yoksa BOŞ kalır (uydurma yok).

    ⚠️ **ESKİ TUZAK KAYDI DURUYOR ve hâlâ geçerlidir.** Görünüşte işleyen İKİNCİ
    bir kaynak vardır: `purchase_requests.section_id` + `purchase_request_lines.
    stock_item_id`. K4 onu engellemez (`inventory` okuyup `procurement`ta `none`
    olan rol YOKTUR). Engel ANLAMdır: o bağ *"bu malzemeyi HANGİ BÖLÜM TALEP
    ETTİ"*dir (satınalma niyeti) — stok gerçeği değil. Bu sütun ONDAN
    BESLENMEZ ve beslenmemelidir; bekçisi
    `test_pyt3_yer_tutucu_denetimi.py::test_PLAN_ve_TALEP_VARKEN_DE_zarflar_BOS_KALIR`
    talep+plan varken ama stok atfı yokken zarfın BOŞ kaldığını çakar.

    ⚠️ **Dolu zarf `pending_module` TAŞIR** (`ListPlaceholder`da alan zorunludur
    ve `MetricPlaceholder`ın "dolu zarf taşımaz" kuralı oraya UYGULANMAZ —
    `CountPlaceholder` emsali). Anahtar `site_planning` olarak KORUNUR: frontend
    `SiteStockTable.tsx` etiketi YALNIZ `available=false` iken basar, dolu zarfta
    hiç okumaz. Anahtarı değiştirmek canlı bir gerekçe metnini bayatlatırdı.

    ⚠️ **İkinci engel — K4:** `site_planning` bir izin modülü DEĞİLDİR; router'ı
    `site_diary` kapısını kullanır ve `procurement` `inventory=full` iken
    `site_diary=none`dur. Plan verisi buraya basılsaydı o kapı atlanırdı.
    Bekçi: `tests/modules/inventory/test_pyt3_yer_tutucu_denetimi.py`.
    """

    id: uuid.UUID
    code: str
    name: str
    category: StockCategory
    unit: str
    min_stock: Yok
    balance: YokGirdi
    status: StockStatus | None
    monthly_need: YokZarf
    section: ListPlaceholder


class SiteStockKpis(BaseModel):
    """ŞS KPI şeridi (86-91): Toplam Malzeme · Kritik · Düşük · Stok Değeri.

    E3'ün aksine **"Bekleyen Sipariş" YOKTUR** — ŞS mockup'ında o kart çizilmemiş
    ve olmayan bir kart için zarf bile üretilmez.
    """

    total_value: Maliyet
    critical_count: int
    low_count: int
    total_items: Sayac
    items_without_price: Sayac


class SiteStockResponse(BaseModel):
    items: list[SiteStockRow]
    total: int
    limit: int
    offset: int
    kpis: SiteStockKpis


# --- Bolum malzeme kirilimi (STOK-BOLUM) ---


class SectionStockRow(BaseModel):
    """Bir bölümün BİR (malzeme, poz) çiftindeki hareket toplamı.

    🔴 **BURADA "BAKİYE" YOKTUR ve bu bilinçlidir.** Ürün kararı *"STOK DEPODA
    DURUR, BÖLÜM TÜKETİR"*: bakiye depo düzeyinde kalır (`balance.legs()`
    değişmedi). Bölüme "bakiye" basmak, aynı malzemenin hem depo hem bölüm
    bakiyesi olduğu izlenimini verir ve klasik iki-kaynak problemini doğururdu.

    Onun yerine ÜÇ farklı sayı döner, üçü de tek bir toplamdan türetilir ve
    tanımları ÖRTÜŞMEZ:

    | alan | tanım |
    |---|---|
    | `assigned_quantity` | atfedilmiş POZİTİF miktarlar — "bu bölüm için depoya girdi" |
    | `issued_quantity` | NEGATİF miktarların MUTLAK toplamı — "bu bölüme çıkıldı / sarf edildi" |
    | `net_quantity` | `assigned − issued` (işaretli toplam) |

    İkisi ayrı tutulur çünkü tek bir "toplam" basılsaydı `+5 alım` ile
    `−5 sarf` birbirini götürür ve ekran *"bu bölümde hiç malzeme kullanılmadı"*
    derdi — oysa 5 birim gerçekten harcanmıştır. Sarf ekranının okuduğu alan
    `issued_quantity`dir.

    `boq_item_id` NULL olabilir: bölüme çıkılmış ama bir poza bağlanmamış
    malzeme meşrudur (poz kırılımı ZORUNLU DEĞİLDİR — fail-open, bkz. servis).
    Satırlar (malzeme, poz) çifti başına açılır; poz kırılımı istemeyen ekran
    malzemeye göre kendisi toplar.

    `total_value` YALNIZCA fiyatı olan satırlardan gelir (`unit_price` NULL olan
    transfer/düzeltme satırı toplam değere GİRMEZ — §7 S6 ile aynı kural).
    """

    model_config = ConfigDict(from_attributes=True)

    item_id: uuid.UUID
    code: str
    name: str
    category: StockCategory
    unit: str
    boq_item_id: uuid.UUID | None
    boq_code: str | None
    boq_description: str | None
    assigned_quantity: YokGirdi
    issued_quantity: YokGirdi
    net_quantity: YokGirdi
    total_value: Maliyet


class SectionStockKpis(BaseModel):
    """Bölüm malzeme şeridi. **YER TUTUCU YOKTUR** — dördü de gerçek sayıdır.

    `lines_without_price`, `total_value`in EKSİKLİĞİNİ dürüstçe bildirir: fiyatsız
    satır varken tutar "eksik" demektir ve ekran bunu söyleyebilmelidir
    (`SiteStockKpis.items_without_price` emsali).
    """

    issued_value: Maliyet
    total_value: Maliyet
    item_count: int
    lines_without_price: Sayac


class SectionStockResponse(BaseModel):
    items: list[SectionStockRow]
    total: int
    limit: int
    offset: int
    kpis: SectionStockKpis

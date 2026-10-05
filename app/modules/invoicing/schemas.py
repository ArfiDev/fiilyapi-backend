"""Fatura şemaları (FAT-1 spec §2, §7) — T3.

`procurement/schemas.py` üçlüsünün (Create/Update/Response) kardeşi.

## 🔴 `extra="forbid"` — sessiz yok sayma YOK

Bu modülün gövde şemaları bilinmeyen alanı **422** ile reddeder
(`site_planning/schemas.py` emsali). Gerekçe PARA'dır: kullanıcı `total` ya da
`line_total` gönderdiğinde Pydantic'in varsayılan davranışı onu SESSİZCE atmak
olurdu ve istemci gönderdiği tutarın yazıldığını sanırdı. Reddedilenler:

* **`invoice_no` (giden)** — sunucu üretir (§4); şemada VARDIR çünkü gelen
  faturada ZORUNLUDUR (S5), yön kuralı `validation.invoice_no_blockers`tadır.
* **`line_total` · `sort_order`** — ikisi de sunucunun hesabıdır (K7/SA dersi).
* **hesaplanmış para alanlarının HEPSİ** (`subtotal` · `advance_amount` ·
  `retention_amount` · `tax_base` · `vat_amount` · `withholding_amount` ·
  `total`) — tek kaynak `amounts.py`dir. **ORANLAR (`*_rate`) İSTEMCİDEN
  GELİR** (FK:223/229/235 checkbox + oran girişi).
* **`status`** — başlangıç durumu yöne göre `transitions.INITIAL_STATUS`tan,
  sonraki durumlar yalnız geçiş uçlarından (T4) gelir.

## Uzunluk tavanları

`String(N)` kolonların tavanı N'dir; `app.core.text.FREE_TEXT_MAX_LENGTH`
YALNIZCA kolonu `Text` (DB'de sınırsız) olan alanlar içindir: `note`,
`party_address` ve kalem `description`ı. Tavan **TÜM giriş noktalarından aynı
sabitten** okunur (TB4/B4 dersi: iki modüle ayrı yazılan sayı birinden
güncellenir, ötekinden atlatılır).

## Kapsam dışı alanlar (spec §1, icat yasağı)

GİB/e-Fatura alanları (UUID/ETTN · zarf no · GİB durumu) · muhasebe fişi ·
tahsilat kaydı ve banka hesabı · eşleştirme/kısmi onay · para birimi/kur ·
iskonto sütunu bu şemalarda YOKTUR ve gövdede gönderilirlerse **422**dir.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.core.field_mask import Hassas
from app.core.slug import url_safe_key
from app.core.text import FREE_TEXT_MAX_LENGTH
from app.modules.invoicing.models import (
    InvoiceDirection,
    InvoiceDocumentType,
    InvoicePaymentMethod,
    InvoiceStatus,
)

__all__ = [
    "InvoiceCreate",
    "InvoiceDetailResponse",
    "InvoiceLineCreate",
    "InvoiceLineResponse",
    "InvoiceLinesReplace",
    "InvoiceListResponse",
    "InvoiceResponse",
    "InvoiceSummaryMetric",
    "InvoiceSummaryResponse",
    "InvoiceUpdate",
]

#: Bilinmeyen alan = 422 (modül docstring'i). TEK sabit: her gövde şeması aynı
#: kararı taşısın, biri unutulduğunda o uçtan para alanı sızmasın.
_SIKI = ConfigDict(extra="forbid")

# Model sınırları: `invoices.invoice_no` String(30) · `party_name` String(200) ·
# `party_tax_number` String(11) · `party_tax_office` String(100).
_INVOICE_NO = Field(default=None, min_length=1, max_length=30)
_PARTY_NAME = Field(min_length=1, max_length=200)
_TAX_NUMBER = Field(default=None, min_length=1, max_length=11)
_TAX_OFFICE = Field(default=None, min_length=1, max_length=100)
_ADDRESS = Field(default=None, max_length=FREE_TEXT_MAX_LENGTH)
_NOTE = Field(default=None, max_length=FREE_TEXT_MAX_LENGTH)

# Oranlar `Numeric(5, 2)` ve 0..100 (DB CHECK'iyle birebir). `None` "kesinti
# İŞARETLENMEMİŞ" demektir ve 0'dan FARKLIDIR (FK:223/229/235).
_RATE = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)

# Ölçekler repo standardı: miktar Numeric(14, 3), para Numeric(18, 2).
# `gt=0`: sıfır/negatif miktarlı bir fatura kalemi hiçbir durumda anlamlı
# değildir (DB'de de CHECK'lidir, bu ilk katmandır).
_QUANTITY = Field(gt=0, max_digits=14, decimal_places=3)
_UNIT_PRICE = Field(ge=0, max_digits=18, decimal_places=2)
_VAT_RATE = Field(ge=0, le=100, max_digits=5, decimal_places=2)


# 🔴 GECE KARARI (IZN-B4b): fatura satış YA DA gider olabilir ve tek satırda hem maliyet/kâr hem
# kasa-banka hem satış bedeli anlamı taşır. Fail-closed: üç kategori BİRLİKTE etiketlenir
# (HERHANGİ biri gizliyse tutar gizlenir). `party_*` vergi no/adres: satis_alici.
_MALI = (Hassas.maliyet_kar, Hassas.banka_kasa, Hassas.satis_alici)
MaliTutar = Annotated[Decimal | None, *_MALI]
#: İSTEK gövdesinde aynı etiket (gizli rol dolu tutar gönderirse 403); tip `None` taşımaz.
MaliTutarGirdi = Annotated[Decimal, *_MALI]
#: ORAN/miktar: tutar değil (yüzde, metraj) → açıkça hassas DEĞİL.
Oran = Annotated[Decimal | None, Hassas.yok]
OranGirdi = Annotated[Decimal, Hassas.yok]
AliciMetni = Annotated[str | None, Hassas.satis_alici]


class InvoiceLineCreate(BaseModel):
    """Fatura kaleminin BİR satırı (FGI:116-130 · FGE:150-160 · FK:168-183).

    **`line_total` ve `sort_order` YOKTUR** (gönderilirse 422): ilki
    `amounts.line_total`ın, ikincisi gövdedeki dizinin İNDEKSİDİR. `sort_order`
    istemciden gelseydi çakışan ya da boşluklu sıralar doğar, sunucunun yeniden
    numaralandırması gerekirdi (SA/T3 dersi).

    `unit` SERBEST METİNDİR (S3): FK:169 bir input'tur — kapalı küme İCAT
    EDİLMEZ. `description` poz numarasını İÇİNDE taşır (S2, FK:178); ayrı bir
    `boq_item_id` açılsaydı olmayan bir katalog bağı vaat edilirdi.
    """

    model_config = _SIKI

    description: str = Field(min_length=1, max_length=FREE_TEXT_MAX_LENGTH)
    unit: str | None = Field(default=None, min_length=1, max_length=20)
    quantity: OranGirdi = _QUANTITY
    unit_price: MaliTutarGirdi = _UNIT_PRICE
    vat_rate: OranGirdi = _VAT_RATE
    detail_note: str | None = Field(default=None, max_length=200)


class InvoiceLinesReplace(BaseModel):
    """`PUT /invoices/{id}/lines` — kalem kümesini TOPTAN yazar.

    Zarf (`{"lines": [...]}`) çıplak listeye tercih edildi (hakediş/puantaj
    emsali): ileride kalem yazımına eşlik eden bir alan gerekirse gövde şekli
    KIRILMADAN büyür.

    Boş liste MEŞRUDUR ve hepsini siler: kalemsiz TASLAK anlamlıdır (K6 kapısı
    `send`/`approve` anındadır, T4).
    """

    model_config = _SIKI

    lines: list[InvoiceLineCreate]


class InvoiceLineResponse(BaseModel):
    """Kalem künyesi — YALNIZCA saklanan kolonlar.

    Satır bazında KDV/matrah payı (`amounts.line_tax_bases` /
    `line_vat_amounts`) BURADA YOKTUR ve bu bilinçlidir: onlar kolon değildir
    (K7) ve okuma anında yeniden hesaplanmaları, donmuş bir faturanın ekranda
    canlı türetilmiş sayı göstermesi demek olurdu. Başlık toplamları saklanır ve
    otoritedir.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sort_order: int
    description: str
    unit: str | None
    quantity: Annotated[Decimal, Hassas.yok]
    unit_price: MaliTutar
    vat_rate: Annotated[Decimal, Hassas.yok]
    line_total: MaliTutar
    detail_note: str | None


class InvoiceCreate(BaseModel):
    """`POST /invoices` — başlık + kalemler TEK gövde, ATOMİK yazılır.

    Zorunlu çekirdek: `direction` · `document_type` · `issue_date` ·
    `party_name`. Taraf ADI zorunludur çünkü SNAPSHOT'tır (S4/K7): cari kartı
    silinse ya da düzeltilse bile faturanın üzerindeki ünvan DEĞİŞMEZ. Dört
    taraf FK'sı yalnızca İZDİR ve en fazla BİRİ dolar (servis 422).

    `invoice_no` şemada opsiyoneldir ama kural YÖNE bağlıdır (§4/S5) ve tek
    kaynağı `validation.invoice_no_blockers`tır: giden'de gönderilemez,
    gelen'de zorunludur. Şemaya `direction`a bağlı bir validator yazılsaydı
    aynı kural iki yerde dururdu.

    `status` YOKTUR: başlangıç `transitions.INITIAL_STATUS[direction]`tan gelir
    (giden → `draft`, gelen → `pending`, K2).
    """

    model_config = _SIKI

    direction: InvoiceDirection
    invoice_no: str | None = _INVOICE_NO
    document_type: InvoiceDocumentType
    issue_date: date
    due_date: date | None = None
    payment_method: InvoicePaymentMethod | None = None
    note: str | None = _NOTE

    party_name: Annotated[str, Hassas.satis_alici] = _PARTY_NAME
    party_tax_number: AliciMetni = _TAX_NUMBER
    party_tax_office: str | None = _TAX_OFFICE
    party_address: AliciMetni = _ADDRESS

    employer_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    supplier_id: uuid.UUID | None = None
    subcontractor_id: uuid.UUID | None = None

    progress_payment_id: uuid.UUID | None = None
    subcontractor_progress_payment_id: uuid.UUID | None = None
    equipment_rental_invoice_id: uuid.UUID | None = None
    purchase_order_id: uuid.UUID | None = None

    project_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None

    advance_rate: Oran = _RATE
    retention_rate: Oran = _RATE
    withholding_rate: Oran = _RATE

    lines: list[InvoiceLineCreate] = Field(default_factory=list)


class InvoiceUpdate(BaseModel):
    """`PATCH /invoices/{id}` — tüm alanlar isteğe bağlı (spec §7 md.5).

    Alanın GÖNDERİLMEMESİ ile `null` GÖNDERİLMESİ farklıdır ve fark
    `model_fields_set` ile korunur: `site_id: null` bağı KOPARIR, hiç
    göndermemek ona DOKUNMAZ (`StockItemUpdate` dersi).

    **`invoice_no`/`direction` BURADA YOKTUR:** yön faturanın kimliğidir,
    numara ise ya sunucunundur ya satıcınındır — ikisi de sonradan
    düzeltilemez. **`lines` de yoktur:** kalem kümesinin TEK yolu `PUT lines`tır
    (§7 md.7), iki yazma yolu olsaydı `sort_order` üretimi ikiye bölünürdü.

    Gelen faturada bu şemanın yalnız ÜÇ alanı kabul edilir
    (`guards.INCOMING_PATCHABLE_FIELDS`, 422 aksi hâlde) — ayrı bir şema
    açılmadı çünkü kural DURUMA bağlıdır ve tip sistemi onu taşıyamaz.
    """

    model_config = _SIKI

    document_type: InvoiceDocumentType | None = None
    issue_date: date | None = None
    due_date: date | None = None
    payment_method: InvoicePaymentMethod | None = None
    note: str | None = _NOTE

    party_name: AliciMetni = Field(default=None, min_length=1, max_length=200)
    party_tax_number: AliciMetni = _TAX_NUMBER
    party_tax_office: str | None = _TAX_OFFICE
    party_address: AliciMetni = _ADDRESS

    employer_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    supplier_id: uuid.UUID | None = None
    subcontractor_id: uuid.UUID | None = None

    progress_payment_id: uuid.UUID | None = None
    subcontractor_progress_payment_id: uuid.UUID | None = None
    equipment_rental_invoice_id: uuid.UUID | None = None
    purchase_order_id: uuid.UUID | None = None

    project_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None

    advance_rate: Oran = _RATE
    retention_rate: Oran = _RATE
    withholding_rate: Oran = _RATE


class InvoiceResponse(BaseModel):
    """Fatura künyesi — FY listesinin satırı ve detayın başlığı AYNI şemadır.

    İki ayrı şema açılsaydı liste "Matrah/KDV/Toplam" sütunlarını (FY:116-118)
    ayrı bir hesapla doldurur ve detaydan sapabilirdi. Para alanlarının hepsi
    SAKLANAN kolonlardır (K7): okumada yeniden hesaplanmazlar.

    Türev alan YOKTUR: "kalan gün" (FGI:68) `due_date`ten, "Vadeli" rozeti
    (K1) `status` + `due_date`ten ekranda türer.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    direction: InvoiceDirection
    invoice_no: str
    document_type: InvoiceDocumentType
    status: InvoiceStatus
    issue_date: date
    due_date: date | None
    payment_method: InvoicePaymentMethod | None
    note: str | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def slug(self) -> str | None:
        """URL-4 — URL'de taşınacak okunabilir anahtar; `invoice_no`nun kendisi.

        AYRI BİR `slug` KOLONU AÇILMADI: `invoice_no` NOT NULL'dır ve zaten
        yön başına tekildir; ikinci bir kolon aynı bilgiyi iki yerde tutar ve
        migration doğururdu. Türev olduğu için LİSTE ve DETAY otomatik olarak
        AYNI değeri taşır — `InvoiceResponse` ikisinin de şemasıdır, yani
        URL-2'nin "liste şemasına slug eklenmedi" tuzağı burada YAPISAL olarak
        imkânsızdır.

        `None` olabilir: numara yol segmentine giremeyecek bir karakter
        taşıyorsa (GELEN faturada `invoice_no` SERBEST METİNDİR — `2026/0001`
        yazılabilir) istemci `slug ?? id` ile UUID'ye düşer.
        """
        return url_safe_key(self.invoice_no)

    party_name: AliciMetni
    party_tax_number: AliciMetni
    party_tax_office: str | None
    party_address: AliciMetni

    employer_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    supplier_id: uuid.UUID | None
    subcontractor_id: uuid.UUID | None

    progress_payment_id: uuid.UUID | None
    subcontractor_progress_payment_id: uuid.UUID | None
    equipment_rental_invoice_id: uuid.UUID | None
    purchase_order_id: uuid.UUID | None

    project_id: uuid.UUID | None
    site_id: uuid.UUID | None

    subtotal: MaliTutar
    advance_rate: Oran
    advance_amount: MaliTutar
    retention_rate: Oran
    retention_amount: MaliTutar
    tax_base: MaliTutar
    vat_amount: MaliTutar
    withholding_rate: Oran
    withholding_amount: MaliTutar
    total: MaliTutar

    created_by_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class InvoiceDetailResponse(InvoiceResponse):
    """Detay = künye + KALEMLER (FGI/FGE kalem tablosu), `sort_order` sırasında.

    Liste bu şemayı KULLANMAZ: 50 faturanın kalemlerini çekmek N+1 üretir ve FY
    tablosu zaten kalem göstermez.
    """

    lines: list[InvoiceLineResponse]


class InvoiceListResponse(BaseModel):
    """`personnel`/`audit`/`inventory` liste deseni: `total` + `limit`/`offset`."""

    items: list[InvoiceResponse]
    total: int
    limit: int
    offset: int


class InvoiceSummaryMetric(BaseModel):
    """FY:71-73 kartlarının İKİ satırı: büyük rakam (tutar) + alt satır (adet).

    İkisi TEK nesnede durur çünkü aynı kümeden gelirler; `*_amount` ve
    `*_count` diye düz alanlara açılsalardı bir kart yanlış eşleştirilebilir ve
    "18 fatura · ₺0,00" gibi imkânsız bir çift ekrana çıkabilirdi.
    """

    amount: MaliTutar
    count: int


class InvoiceSummaryResponse(BaseModel):
    """`GET /invoices/summary` — FY:69-75 KPI şeridi, BEŞ kart.

    🔴 **`pending_approval` ADETTİR, tutar DEĞİL** (FY:75 tek sayı basar, `₺`
    yoktur); ilk üç kart hem tutar hem adet taşır. `vat_difference` tek bir para
    değeridir ("Ödenecek KDV", FY:74) ve NEGATİF olabilir — gelen KDV giden
    KDV'yi aştığında devreden KDV doğar ve bu meşrudur, sıfıra KIRPILMAZ.

    Ay penceresi ve kapsam kuralları `summary.py` modül docstring'indedir.
    """

    issued_this_month: InvoiceSummaryMetric
    received_this_month: InvoiceSummaryMetric
    receivable: InvoiceSummaryMetric
    vat_difference: MaliTutar
    pending_approval: int

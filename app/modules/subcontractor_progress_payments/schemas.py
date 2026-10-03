"""Taşeron hakedişi Pydantic şemaları (T2) — okuma/yazma ayrı.

Alan sınırları modeldeki kolon tipleriyle BİREBİR (spec §2): snapshot alanları
`code`/`unit`/`group_name` modeldeki `String(N)` uzunluklarını taşır, `description`
sınırsız `Text`tir.

Hesap blokları (brüt/KDV/avans/teminat/net) T3'te eklendi ve gövdeleri
`progress_payments.calculations`ten okunur — bu modül yalnız ŞEKLİ tanımlar,
formülün ikinci bir kopyasını TAŞIMAZ.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.modules.subcontractor_progress_payments.models import (
    QuantitySource,
    SubcontractorPaymentStatus,
)

__all__ = [
    "SubcontractorPaymentCalculation",
    "SubcontractorProgressPaymentCreate",
    "SubcontractorProgressPaymentDetail",
    "SubcontractorProgressPaymentLineInput",
    "SubcontractorProgressPaymentLineRead",
    "SubcontractorProgressPaymentLinesSave",
    "SubcontractorProgressPaymentListItem",
    "SubcontractorProgressPaymentListResponse",
    "SubcontractorProgressPaymentSummary",
    "SubcontractorProgressPaymentUpdate",
    "SubcontractorRefreshPricesResponse",
    "SubcontractorRejectBody",
]


# --- Yazma şemaları ---

#: KAT-B2.4 (K5): hakediş satırında Bakanlık poz no'su istemciden alınmaz (PUT/POST gövdesinde 422).
SOURCE_CODE_SERVER_OWNED = "source_code istemciden alınmaz; sözleşme kaleminden sunucu kopyalar"


class SubcontractorProgressPaymentCreate(BaseModel):
    """`POST /subcontractor-contracts/{id}/progress-payments` gövdesi.

    `lines[]` YOKTUR (işveren şemasından ayrılan nokta): satırlar sözleşme
    kalemlerinden OTOMATİK yüklenir (O66) ve miktarlar `PUT …/lines` ile
    girilir (T3). Tüm alanlar isteğe bağlıdır — taslak boş gövdeyle açılır.
    """

    period_year: int | None = None
    period_month: int | None = Field(default=None, ge=1, le=12)
    description: str | None = None
    default_coefficient: Decimal | None = Field(default=None, gt=0)
    section_id: uuid.UUID | None = None
    """O58 "Bölüm" seçici — bilgi alanı (spec §8 S2). NULL = "Tüm Bölümler";
    kota/hesaba GİRMEZ, salt etiket/filtredir."""


class SubcontractorProgressPaymentLineInput(BaseModel):
    """`PUT …/lines` gövdesindeki tek satır (T3).

    İşveren `ProgressPaymentLineInput`tan İKİ FARK:
    * `site_id` YOK — taşeron satırında şantiye kırılımı yoktur (spec §2);
    * `coefficient` KİLİTSİZ (yalnız `> 0`): taşeron sözleşmesinde
      `has_price_escalation` kolonu yoktur, işverendeki FF kilidi uygulanmaz
      (şef kararı 2026-08-02).

    `quantity_source` BİLEREK YOKTUR: bu dilimde her satır `manual`dır (spec §2),
    istekten alınması `diary` rozetini sahte doldurmanın yolu olurdu.
    `coefficient` gönderilmezse yeni satır hakedişin `default_coefficient`'ını
    alır, MEVCUT satırın katsayısı KORUNUR.
    """

    contract_item_id: uuid.UUID
    quantity: Decimal = Field(ge=0)
    """0 meşrudur (satır "girilmedi" değil "bu dönem sıfır" demektir)."""
    coefficient: Decimal | None = Field(default=None, gt=0)
    sort_order: int | None = Field(default=None, ge=0)
    """Gönderilmezse GÖVDE SIRASI otoritedir."""

    @model_validator(mode="before")
    @classmethod
    def _kaynak_kod_istemciden_alinmaz(cls, data: object) -> object:
        """KAT-B2.4 (K5): `source_code` yalniz sunucudan (kalem snapshot'i) gelir; govdede
        gelirse (degeri `null` olsa da) 422. `extra="forbid"` BILEREK konmadi (mevcut
        davranis: bilinmeyen alanlar sessiz yok sayilir)."""
        if isinstance(data, dict) and "source_code" in data:
            raise ValueError(SOURCE_CODE_SERVER_OWNED)
        return data


class SubcontractorProgressPaymentLinesSave(BaseModel):
    """`PUT …/lines` gövdesi — DEĞİŞTİRME semantiği: gövdede olmayan satır
    SİLİNİR. Boş liste = tüm satırları temizle."""

    lines: list[SubcontractorProgressPaymentLineInput] = Field(default_factory=list)


class SubcontractorProgressPaymentUpdate(BaseModel):
    """`PATCH /subcontractor-progress-payments/{id}` — yalnız başlık alanları.

    `status`/`sequence_no`/satırlar YOK: durum yalnız geçiş uçlarıyla (T4),
    satırlar yalnız `PUT …/lines` (T3) ile değişir. Servis `status != draft`
    ise 409 döner (kural burada TEKRARLANMAZ).
    """

    period_year: int | None = None
    period_month: int | None = Field(default=None, ge=1, le=12)
    description: str | None = None
    default_coefficient: Decimal | None = Field(default=None, gt=0)
    section_id: uuid.UUID | None = None


class SubcontractorRejectBody(BaseModel):
    """`POST …/reject` gövdesi (T4, spec §5).

    İşveren `RejectBody`den AYRILIR: gerekçe ZORUNLUDUR (`str`, opsiyonel değil)
    çünkü burada `rejection_reason` KOLONUNA yazılır ve L177 "Revize Gerekli"
    rozetinin kullanıcıya gösterilen açıklamasıdır — gerekçesiz bir rozet
    taşerona neyi revize edeceğini söylemez.

    Boş/yalnız boşluktan oluşan metnin reddi `min_length` ile YAPILMAZ ("   " üç
    karakterdir): kırpma kuralı `guards.validate_reject`te TEK kopyadır.
    `max_length=500` işveren gövdesindeki gerekçenin aynısıdır.
    """

    reason: str = Field(max_length=500)


# --- Okuma şemaları ---


class SubcontractorProgressPaymentLineRead(BaseModel):
    """O72-100 satırı. Şantiye kırılımı YOKTUR — taşeron sözleşmesi zaten tek
    şantiyeye (ya da proje geneline) bağlıdır (spec §2)."""

    id: uuid.UUID
    contract_item_id: uuid.UUID | None
    code: str = Field(max_length=50)
    source_code: str | None = Field(max_length=32)
    """KAT-B2.4: Bakanlık poz no SNAPSHOT'ı (maskesiz rota: düz alan)."""
    description: str
    unit: str = Field(max_length=50)
    contract_unit_price: Decimal
    coefficient: Decimal
    quantity: Decimal
    group_name: str | None = Field(default=None, max_length=200)
    sort_order: int
    quantity_source: QuantitySource
    """O87 "Günlük kayıttan" rozetinin kaynağı; `site_diary` dilimi gelene kadar
    HER satır `manual`dır (spec §2)."""
    adjusted_unit_price: Decimal
    """O102 "Düz. B.F." = `contract_unit_price × coefficient` (kuruşa yuvarlı, K5)."""
    line_total: Decimal
    """O106 satır hakediş tutarı = `adjusted_unit_price × quantity` (K5 formül sırası)."""


class SubcontractorPaymentCalculation(BaseModel):
    """O147-163 tfoot'u (spec §3) — `calculations.py` çıktısı, SAKLANMAZ (türev).

    Teminat kesintisi ve fiyat farkı katsayısı ONAYLI SAPMA olarak dahildir;
    KDV tevkifatı bu dilimde hesaba GİRMEZ (spec §8 S4).
    """

    gross: Decimal
    vat: Decimal
    advance_deduction: Decimal
    retention: Decimal
    net: Decimal


class SubcontractorRefreshPricesResponse(BaseModel):
    """`POST …/refresh-prices` yanıtı — güncel ekran ayrı bir `GET` ile okunur
    (işveren `RefreshPricesResponse` deseni)."""

    refreshed_count: int


class SubcontractorProgressPaymentListItem(BaseModel):
    """L112-180 liste satırı."""

    id: uuid.UUID
    # URL-4: URL'de taşınacak okunabilir anahtar. `None` olabilir (adı
    # slug'lanamayan kayıt) — istemci `slug ?? id` kullanır (`routes.ts`).
    slug: str | None = None
    contract_id: uuid.UUID
    project_id: uuid.UUID
    project_name: str
    subcontractor_name: str | None
    contract_no: str | None
    work_category: str | None
    """L131 "İş Kategorisi" — SÖZLEŞMEDEN okunur (`subcontractor_contracts.
    work_category`), hakediş tablosunda KOLONU YOKTUR: liste sorgusunda zaten
    kurulu olan sözleşme JOIN'inden gelir, ek istek AÇILMAZ (TB3-A).
    Taslak sözleşmede boş bırakılabildiği için NULL meşrudur."""
    sequence_no: int
    period_year: int | None
    period_month: int | None
    description: str | None
    status: SubcontractorPaymentStatus
    section_id: uuid.UUID | None
    contract_site_id: uuid.UUID | None
    """HAK-NULL · satırın KAPSAMI — sözleşmenin şantiyesi (`subcontractor_
    contracts.site_id`), `NULL` = **proje geneli** (projenin tüm şantiyeleri).

    `section_id` ile KARIŞTIRILMAZ; ikisi FARKLI eksenlerdir ve ikisinin `NULL`ı
    farklı şey söyler: `section_id IS NULL` "bölüm kırılımı yok",
    `contract_site_id IS NULL` "şantiye kırılımı yok, sözleşme proje geneli".

    Neden yayımlanıyor: `?site_id=` süzgeci artık KAPSAYAN kümeyi döndürür
    (proje geneli satırlar dahil — `repository._list_stmt`). Satır kendi
    kapsamını SÖYLEMESEYDİ çağıran, her şantiyede tekrar dönen proje geneli
    parayı şantiyenin parası sanıp N kez toplardı. Görünürlüğü açan değişiklik
    toplamları yalancı YAPMASIN diye ayrım satırdadır."""
    created_at: datetime
    gross_total: Decimal
    net_total: Decimal
    """L143-146: liste ekranı brüt ve NET taşır. Mockup'ın "Net = Brüt − KDV"
    görünümü hesap hatasıdır — doğru formül (`calculations.net_amount`) uygulanır."""
    is_revision_required: bool
    """L177 "Revize Gerekli" rozeti — BEŞİNCİ durum DEĞİL, `draft AND rejected_at
    IS NOT NULL` türevi (spec §5). Rozet listede gösterildiği için liste satırı
    da taşır; türev TEK kopya `read._is_revision_required`tedir."""


class SubcontractorProgressPaymentSummary(BaseModel):
    """L105-122 KPI şeridi — DÖRT kart (T4). Gövde `summary.py`dedir.

    Para KPI'ları BRÜTtür (mockup kanıtı: L118 "Onay Bekliyor ₺1,24M" = L143
    Brüt Tutar hücresi ₺1.240.000, Net Ödeme ₺1.016.800 DEĞİL).
    """

    total_gross: Decimal
    """L108 "Toplam Hakediş" — süzgeçteki TÜM hakedişlerin brütü."""
    pending_gross: Decimal
    """L112 "Onay Bekliyor" — `pending_approval` durumundakilerin brütü."""
    paid_period_gross: Decimal
    """L116 "Bu Ay Ödenen" — `paid` + ETKİN DÖNEM'dekilerin brütü."""
    active_subcontractor_count: int
    """L120 "Aktif Taşeron" — süzgeçteki farklı taşeron SÖZLEŞMESİ sayısı
    (şemada ayrı taşeron cari tablosu yoktur; `subcontractor_name` NULL olabilir)."""
    period_year: int
    period_month: int
    """"Bu Ay Ödenen"in dayandığı dönemin ECHO'su: dönem süzgeci verilmişse o,
    verilmemişse içinde bulunulan ay — ekran hangi ayı gösterdiğini bilmelidir."""


class SubcontractorProgressPaymentListResponse(BaseModel):
    """`audit`/`users` liste deseninin aynısı: `total` + `limit`/`offset`."""

    items: list[SubcontractorProgressPaymentListItem]
    total: int
    limit: int
    offset: int


class SubcontractorProgressPaymentDetail(BaseModel):
    """`GET /subcontractor-progress-payments/{id}` — O ekranının başlığı + satırları."""

    id: uuid.UUID
    # URL-4: URL'de taşınacak okunabilir anahtar. `None` olabilir (adı
    # slug'lanamayan kayıt) — istemci `slug ?? id` kullanır (`routes.ts`).
    slug: str | None = None
    contract_id: uuid.UUID
    project_id: uuid.UUID
    project_name: str
    subcontractor_name: str | None
    contract_no: str | None
    work_category: str | None
    """Liste satırıyla TUTARLILIK için detayda da bulunur (TB3-A) — kaynağı aynı:
    sözleşmenin kendi kolonu, hakedişte snapshot'ı YOKTUR."""
    sequence_no: int
    period_year: int | None
    period_month: int | None
    description: str | None
    status: SubcontractorPaymentStatus
    vat_pct: Decimal
    advance_pct: Decimal
    retainage_pct: Decimal
    default_coefficient: Decimal
    section_id: uuid.UUID | None
    submitted_at: datetime | None
    approved_at: datetime | None
    approved_by: uuid.UUID | None
    paid_at: datetime | None
    rejected_at: datetime | None
    rejection_reason: str | None
    is_revision_required: bool
    """`draft AND rejected_at IS NOT NULL` türevi (spec §5) — `SubcontractorProgress
    PaymentListItem` ile AYNI kaynaktan gelir."""
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    lines: list[SubcontractorProgressPaymentLineRead]
    calculation: SubcontractorPaymentCalculation
    dropped_orphan_count: int = 0
    """`PUT …/lines` yanıtında: kalemi silindiği için (`contract_item_id IS NULL`)
    gövdeden adreslenemeyen ve bu kaydetmede DÜŞEN satır sayısı — zarif düşüş +
    bildirim, sessiz atlama YOK. Okuma uçlarında her zaman `0`'dır."""

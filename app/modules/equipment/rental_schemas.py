"""Kira hakedişi şemaları (MK-2 spec §4) — M5'in TÜM yüzeyi.

## 🔴 Türev alanlar KOLON DEĞİLDİR ama YANITTA VARDIR

`our_amount` · `breakdown_amount` · `hours_variance` · `variance_status` ·
`vat_amount` · `payable_total` ve üç tür toplamı DB'de KOLON DEĞİLDİR
(`rental.py` her okumada üretir), ama M5 hepsini ekrana basar ve rozet SUNUCU
DAMGASIDIR (F-P10 kanonu) — istemci kendi eşiğiyle hesaplasaydı iki ekran aynı
faturayı farklı gösterirdi.

## Liste yanıtında TOPLAM YOKTUR — ve bu bilinçlidir

`our_total`/`owned_total` SATIRLARDAN türer (MK-1 K15) ve liste satırı başına
tüm satırları taramayı gerektirir. `vat_amount`/`payable_total` ise yalnız
başlığın kendi iki kolonundan (`invoice_amount`, `vat_rate`) türer ve ucuzdur —
bu yüzden listede DE vardır. Ayrım keyfi değil, MALİYETİN kendisidir.

## Satır PATCH'i `extra="forbid"`dur

Spec §4 satır PATCH'inin YALNIZ `rate_amount` + `invoiced_hours` taşıdığını
söyler. Fazla alan sessizce YOKSAYILSAYDI `{"worked_hours": 1}` gönderen istemci
K2 snapshot'ını değiştirdiğini sanır, ekranda başka bir sayı görürdü — MK-1'in
K11 "sunucu hesabının üzerine yazma girişimi 422'dir" kararının aynısı.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.equipment.mask_types import Maliyet, Yok, YokGirdi
from app.modules.equipment.models import (
    DEFAULT_VAT_RATE,
    EquipmentRatePeriod,
    RentalInvoiceStatus,
    RentalLineKind,
)
from app.modules.equipment.rental import VarianceStatus

# Model sınırlarıyla BİREBİR: `String(100)` · `Numeric(18,2)` · `Numeric(5,2)` ·
# `Numeric(8,2)`. Gevşetilseydi kullanıcı 422 yerine anlaşılmaz bir DB hatası
# alırdı.
_INVOICE_NO = Field(default=None, max_length=100)
_MONEY = Field(default=None, ge=0, max_digits=18, decimal_places=2)
_VAT_RATE = Field(default=DEFAULT_VAT_RATE, ge=0, le=100, max_digits=5, decimal_places=2)
_VAT_RATE_OPTIONAL = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)
_RENTAL_HOURS = Field(default=None, ge=0, max_digits=8, decimal_places=2)
_YEAR = Field(ge=2000, le=2200)
_MONTH = Field(ge=1, le=12)


class RentalInvoiceCreate(BaseModel):
    """`POST /equipment/rental-invoices` — M5'in üst formu.

    Satırlar GÖVDEDE YOKTUR: "Çalışma kaydından otomatik yüklendi" (M5:83) —
    sunucu onları çalışma kaydından KURAR (K2 snapshot'ı). İstemci saat
    gönderebilseydi doğrulamanın iki bağımsız tarafı (bizim kaydımız ↔ firmanın
    faturası) tek kaynağa çöker ve M5'in tüm amacı kaybolurdu.

    `invoice_amount` isteğe bağlıdır: taslak açan kullanıcı henüz faturayı
    almamış olabilir (o hâlde KDV ve ödenecek toplam `null`dur — K1).
    """

    supplier_id: uuid.UUID
    invoice_no: str | None = _INVOICE_NO
    # K1: KDV HARİÇ matrah.
    invoice_amount: Maliyet = _MONEY
    period_year: int = _YEAR
    period_month: int = _MONTH
    # M5:73 "Tüm Projeler" = NULL.
    site_id: uuid.UUID | None = None
    rate_period: EquipmentRatePeriod
    # K1: oran VERİDİR, koda gömülü sabit değil.
    vat_rate: YokGirdi = _VAT_RATE


class RentalInvoiceUpdate(BaseModel):
    """`PATCH /equipment/rental-invoices/{id}` — `draft` + `pending_verification`.

    `approved`/`paid` faturada 409 (K5). Alanın GÖNDERİLMEMESİ ile `null`
    GÖNDERİLMESİ farklıdır (F-İK "touched" dersi) ve fark `model_fields_set` ile
    korunur.

    🔴 Dönem/şantiye değişikliği satırları KENDİLİĞİNDEN tazelemez: tazeleme
    AÇIK bir eylemdir (`POST …/reload`, K2). Sessizce yeniden kurulsalardı
    kullanıcının girdiği fatura saatleri bir alan değişikliğiyle silinirdi.
    """

    supplier_id: uuid.UUID | None = None
    invoice_no: str | None = _INVOICE_NO
    invoice_amount: Maliyet = _MONEY
    period_year: int | None = Field(default=None, ge=2000, le=2200)
    period_month: int | None = Field(default=None, ge=1, le=12)
    site_id: uuid.UUID | None = None
    rate_period: EquipmentRatePeriod | None = None
    vat_rate: Yok = _VAT_RATE_OPTIONAL


class RentalInvoiceLineUpdate(BaseModel):
    """`PATCH /equipment/rental-invoice-lines/{id}` — M5'in İKİ input'u.

    `rate_amount` (M5:93 "Kira B.F. ₺") ve `invoiced_hours` (M5:95 "Fatura
    Saati") DIŞINDA hiçbir alan kabul edilmez (`extra="forbid"`, modül
    docstring'i): `worked_hours` gövdeden yazılabilseydi K2 snapshot'ı bir
    PATCH ile delinirdi.
    """

    model_config = ConfigDict(extra="forbid")

    rate_amount: Maliyet = _MONEY
    invoiced_hours: Yok = _RENTAL_HOURS


class RentalInvoiceLineResponse(BaseModel):
    """M5 tablosunun BİR satırı — kolonlar + TÜREVLER birlikte.

    `our_amount`/`breakdown_amount`/`effective_rate_amount` AYRI AYRI `null`
    olabilir (MK-1 K16 fail-closed): saati bilinen bir makinenin bedeli
    bilinmiyor olabilir ve uydurma bir `0` "bedava çalıştı" derdi.
    """

    id: uuid.UUID
    equipment_id: uuid.UUID
    equipment_name: str
    equipment_brand: str | None
    equipment_plate_no: str | None
    site_id: uuid.UUID | None
    site_name: str | None
    line_kind: RentalLineKind
    worked_hours: YokGirdi
    breakdown_hours: YokGirdi
    rate_amount: Maliyet
    effective_rate_amount: Maliyet
    our_amount: Maliyet
    breakdown_amount: Maliyet
    invoiced_hours: Yok
    hours_variance: Yok
    variance_status: VarianceStatus


class RentalInvoiceTotals(BaseModel):
    """M5 tfoot'u — 🔴 K3'ün ÜÇ AYRI toplamı + K1'in KDV zinciri.

    Üç toplam tek alana indirgenselerdi çift ödeme güvencesi hesapta kaybolurdu:
    `our_total` YALNIZ `rented` satırlardan gelir, `owned_total` ve
    `excluded_breakdown_amount` hiçbir ödenecek toplamın kaynağı DEĞİLDİR.

    `*_unknown_count` bedeli bilinmediği için toplama GİRMEYEN satırların
    adedidir (MK-1 `summarize` kanonu): sessizce atlanan satır kullanıcıya eksik
    bir parayı TAM gösterirdi.
    """

    our_total: Maliyet
    our_total_unknown_count: int
    owned_total: Maliyet
    owned_total_unknown_count: int
    excluded_breakdown_amount: Maliyet
    excluded_breakdown_unknown_count: int
    invoice_amount: Maliyet
    vat_rate: YokGirdi
    vat_amount: Maliyet
    payable_total: Maliyet


class RentalSiteDistributionEquipment(BaseModel):
    """Dağılım kovasına katkı veren ekipman (M5:181 "Tower Crane TC-48 · …")."""

    id: uuid.UUID
    name: str


class RentalSiteDistributionEntry(BaseModel):
    """M5:177-193 proje bazlı maliyet dağılımının BİR kovası.

    `site_id`/`site_name` `null` ise kova "Atanmamış"tır — uydurma bir proje adı
    BASILMAZ. Kovaya YALNIZ `rented` satırlar girer (`rental._site_distribution`
    gerekçesi): M5'in kartı da yalnız ödenecek toplama giren satırları basar.
    """

    site_id: uuid.UUID | None
    site_name: str | None
    hours: YokGirdi
    amount: Maliyet
    unknown_count: int
    equipments: list[RentalSiteDistributionEquipment]


class RentalInvoiceResponse(BaseModel):
    """Fatura BAŞLIĞI — liste satırı ve durum uçlarının yanıtı.

    `vat_amount`/`payable_total` burada da vardır çünkü ikisi de yalnız başlığın
    kendi kolonlarından türer (modül docstring'i); satır toplamları YOKTUR.
    """

    id: uuid.UUID
    # URL-4: URL'de taşınacak okunabilir anahtar. `None` olabilir (adı
    # slug'lanamayan kayıt) — istemci `slug ?? id` kullanır (`routes.ts`).
    slug: str | None = None
    supplier_id: uuid.UUID
    supplier_name: str | None
    invoice_no: str | None
    invoice_amount: Maliyet
    period_year: int
    period_month: int
    site_id: uuid.UUID | None
    site_name: str | None
    rate_period: EquipmentRatePeriod
    vat_rate: YokGirdi
    vat_amount: Maliyet
    payable_total: Maliyet
    status: RentalInvoiceStatus
    approved_by_id: uuid.UUID | None
    approved_at: datetime | None
    paid_at: datetime | None
    created_at: datetime


class RentalInvoiceDetailResponse(RentalInvoiceResponse):
    """`GET /equipment/rental-invoices/{id}` — M5'in TAMAMI (spec §4).

    Tablo + tfoot + proje dağılımı tek istekte gelir: üç ayrı uca bölünseydi
    ekran üç farklı anın fotoğrafını yan yana basabilirdi.
    """

    lines: list[RentalInvoiceLineResponse]
    totals: RentalInvoiceTotals
    site_distribution: list[RentalSiteDistributionEntry]


class RentalInvoiceListResponse(BaseModel):
    """TB3 sayfalama kanonu: `limit ≤ 200`, `total` SÜZÜLMÜŞ kümeyi sayar."""

    items: list[RentalInvoiceResponse]
    total: int
    limit: int
    offset: int

"""Ekipman kartı şemaları (MK-1 spec §2.1, §4) — T1 iskeleti.

`inventory`/`personnel` üçlüsünün (Create/Update/Response) kardeşi.

## Bu dosyada NE YOK — ve niçin

* **Yakıt kaydı yazma şemaları YOKTUR** (T5).
* **Türev alanlar KART gövdesinde YOKTUR:** kullanım %, maliyet, sapma, tüketim
  rozeti ve toplamlar KOLON DEĞİL TÜREVDİR (K15/K16/K17/K18); özet uçlarının
  şemalarındadır, `EquipmentResponse`a sızmaz.
* **Belge slotu / kira hakedişi alanı YOKTUR:** MK-2'ye devredildi (spec §9).

## 🔴 K11 niçin ŞEMADA değil SERVİSTE denetlenir

`hours`ın sunucu hesabı olduğu kuralı `WorkLogCreate`in bir `model_validator`ına
konsaydı PATCH'te İKİNCİ KEZ yazılmak zorunda kalırdı: kısmi güncellemede kural
gövdeye DEĞİL, gövde ile DB'deki satırın BİRLEŞİMİNE bakar (yalnız `start_time`
gönderen bir PATCH'in `end_time`ı satırdan gelir). İki kopya, biri
güncellenmediğinde POST'ta yasak olanı PATCH'ten geçirirdi. Kural bu yüzden
`service._resolve_hours`ta TEK yerdedir ve K2 ile aynı emsali izler (koşullu
kural DB CHECK'i de şema kuralı da olamaz). Sonuç yine **422**'dir —
`EquipmentValidationError` üzerinden, üstelik Türkçe bir cümleyle.

## Uzunluk tavanları kolon sınırlarıyla BİREBİRDİR

`app.core.text.FREE_TEXT_MAX_LENGTH` yalnız kolonu `Text` olan alanlar içindir
(`status_note`); `String(N)` alanların tavanı N'dir — gevşetilseydi kullanıcı
422 yerine anlaşılmaz bir DB hatası alırdı.
"""

import uuid
from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field

from app.core.text import FREE_TEXT_MAX_LENGTH
from app.modules.equipment.consumption import ConsumptionStatus, DeviationReason, UsageReason
from app.modules.equipment.mask_types import Maliyet, MaliyetGirdi, Yok, YokGirdi
from app.modules.equipment.models import (
    DEFAULT_MONTHLY_CAPACITY_HOURS,
    EquipmentCategory,
    EquipmentFinancing,
    EquipmentFuelType,
    EquipmentMaintenancePeriod,
    EquipmentNormUnit,
    EquipmentOwnership,
    EquipmentRatePeriod,
    EquipmentStatus,
    WorkLogType,
)

# Model `String(200)`/`String(100)`/`String(30)` — şema ile DB sınırı AYNI.
_NAME = Field(min_length=1, max_length=200)
_SHORT_TEXT = Field(default=None, max_length=100)
_PLATE = Field(default=None, max_length=30)
_STATUS_NOTE = Field(default=None, max_length=FREE_TEXT_MAX_LENGTH)

# Para alanları `Numeric(18, 2)`; negatif bedel hiçbir okumada anlamlı değildir.
_MONEY = Field(default=None, ge=0, max_digits=18, decimal_places=2)
# K5: norm tüketim SAYIDIR ve pozitiftir — 0 norm, sapma hesabını sıfıra bölerdi.
_NORM = Field(default=None, gt=0, max_digits=10, decimal_places=2)
# MK-4 — motor gücü `Numeric(10, 2)` ve POZİTİFTİR (0 kW bir ölçüm değil,
# bir giriş hatasıdır); hourmeter okumaları aynı ölçekte ama 0 GEÇERLİDİR
# (sıfır saatte teslim alınmış yeni makine).
_POWER = Field(default=None, gt=0, max_digits=10, decimal_places=2)
_HOURMETER = Field(default=None, ge=0, max_digits=10, decimal_places=2)
# `String(200)` — kapasite metni ve ödeme biçimi; kolon sınırıyla BİREBİR.
_MEDIUM_TEXT = Field(default=None, max_length=200)


class EquipmentCreate(BaseModel):
    """`POST /equipment` — M2 formunun gövdesi.

    `purchase_amount` BURADA isteğe bağlıdır ve bu bir eksiklik DEĞİLDİR (K2):
    kural `ownership == owned` iken zorunluluk şeklindedir ve SERVİStedir (422).
    Şemaya konsaydı kiralık makine hiç kaydedilemezdi.
    """

    name: str = _NAME
    category: EquipmentCategory
    brand: str | None = _SHORT_TEXT
    model: str | None = _SHORT_TEXT
    serial_no: str | None = _SHORT_TEXT
    plate_no: str | None = _PLATE
    model_year: int | None = Field(default=None, ge=1900, le=2200)
    ownership: EquipmentOwnership = EquipmentOwnership.owned
    purchase_amount: Maliyet = _MONEY
    purchase_date: date | None = None
    depreciation_years: int | None = Field(default=None, gt=0, le=100)
    supplier_id: uuid.UUID | None = None
    financing: EquipmentFinancing | None = None
    market_value: Maliyet = _MONEY
    rate_amount: Maliyet = _MONEY
    rate_period: EquipmentRatePeriod | None = None
    # K4: NULL = "Depoda (Atanmadı)". `warehouse_id` YOKTUR — bilinçli.
    site_id: uuid.UUID | None = None
    operator_id: uuid.UUID | None = None
    status: EquipmentStatus = EquipmentStatus.working
    status_note: str | None = _STATUS_NOTE
    status_expected_date: date | None = None
    fuel_type: EquipmentFuelType | None = None
    norm_consumption: Yok = _NORM
    norm_unit: EquipmentNormUnit | None = None
    maintenance_period: EquipmentMaintenancePeriod | None = None
    # K7: VERİDİR. 0 verilebilir (kullanım % `null` döner, K16) ama negatif olamaz.
    monthly_capacity_hours: int = Field(default=DEFAULT_MONTHLY_CAPACITY_HOURS, ge=0)
    # --- MK-4: Ekipman Detay alanları (mockup `Makine - Ekipman Detay.dc.html`) ---
    engine_power_kw: Yok = _POWER
    capacity_description: str | None = _MEDIUM_TEXT
    hourmeter_hours: Yok = _HOURMETER
    rental_contract_no: str | None = _SHORT_TEXT
    rental_start_date: date | None = None
    rental_end_date: date | None = None
    # 🔴 `monthly_capacity_hours` DEĞİLDİR (model yorumu): o bir PAYDA, bu bir
    # SÖZLEŞME TAAHHÜDÜ. 0 verilebilir (asgari yok), negatif olamaz.
    rental_min_monthly_hours: int | None = Field(default=None, ge=0)
    rental_payment_terms: str | None = _MEDIUM_TEXT
    last_service_date: date | None = None
    last_service_hourmeter: Yok = _HOURMETER

    # K8: yalnız bir işaret; hiçbir yan etki tetiklemez.
    is_company_asset: bool = True
    is_active: bool = True


class EquipmentUpdate(BaseModel):
    """`PATCH /equipment/{id}` — TÜM alanlar isteğe bağlı.

    Alanın GÖNDERİLMEMESİ ile `null` GÖNDERİLMESİ farklıdır ve fark
    `model_fields_set` ile korunur (F-İK dersi: dokunulmamış bir seçici
    sunucudaki değeri EZMEMELİDİR).

    Kullanımdan kaldırma YOLU budur (`is_active: false`) — DELETE ucu yoktur.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: EquipmentCategory | None = None
    brand: str | None = _SHORT_TEXT
    model: str | None = _SHORT_TEXT
    serial_no: str | None = _SHORT_TEXT
    plate_no: str | None = _PLATE
    model_year: int | None = Field(default=None, ge=1900, le=2200)
    ownership: EquipmentOwnership | None = None
    purchase_amount: Maliyet = _MONEY
    purchase_date: date | None = None
    depreciation_years: int | None = Field(default=None, gt=0, le=100)
    supplier_id: uuid.UUID | None = None
    financing: EquipmentFinancing | None = None
    market_value: Maliyet = _MONEY
    rate_amount: Maliyet = _MONEY
    rate_period: EquipmentRatePeriod | None = None
    site_id: uuid.UUID | None = None
    operator_id: uuid.UUID | None = None
    status: EquipmentStatus | None = None
    status_note: str | None = _STATUS_NOTE
    status_expected_date: date | None = None
    fuel_type: EquipmentFuelType | None = None
    norm_consumption: Yok = _NORM
    norm_unit: EquipmentNormUnit | None = None
    maintenance_period: EquipmentMaintenancePeriod | None = None
    monthly_capacity_hours: int | None = Field(default=None, ge=0)
    # --- MK-4: Ekipman Detay alanları (mockup `Makine - Ekipman Detay.dc.html`) ---
    engine_power_kw: Yok = _POWER
    capacity_description: str | None = _MEDIUM_TEXT
    hourmeter_hours: Yok = _HOURMETER
    rental_contract_no: str | None = _SHORT_TEXT
    rental_start_date: date | None = None
    rental_end_date: date | None = None
    # 🔴 `monthly_capacity_hours` DEĞİLDİR (model yorumu): o bir PAYDA, bu bir
    # SÖZLEŞME TAAHHÜDÜ. 0 verilebilir (asgari yok), negatif olamaz.
    rental_min_monthly_hours: int | None = Field(default=None, ge=0)
    rental_payment_terms: str | None = _MEDIUM_TEXT
    last_service_date: date | None = None
    last_service_hourmeter: Yok = _HOURMETER
    is_company_asset: bool | None = None
    is_active: bool | None = None


class EquipmentResponse(BaseModel):
    """Kart künyesi — M1 kartının veri tabanı.

    **Kullanım % / maliyet / son bakım ALANI YOKTUR:** hepsi çalışma ve yakıt
    kayıtlarından TÜREVDİR (K15/K18) ve özet uçlarından gelir. Buraya konsaydı
    liste her çizilişte hareket tablosunu tarardı.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # URL-4: URL'de taşınacak okunabilir anahtar. `None` olabilir (adı
    # slug'lanamayan kayıt) — istemci `slug ?? id` kullanır (`routes.ts`).
    slug: str | None = None
    name: str
    category: EquipmentCategory
    brand: str | None
    model: str | None
    serial_no: str | None
    plate_no: str | None
    model_year: int | None
    ownership: EquipmentOwnership
    purchase_amount: Maliyet
    purchase_date: date | None
    depreciation_years: int | None
    supplier_id: uuid.UUID | None
    financing: EquipmentFinancing | None
    market_value: Maliyet
    rate_amount: Maliyet
    rate_period: EquipmentRatePeriod | None
    site_id: uuid.UUID | None
    operator_id: uuid.UUID | None
    status: EquipmentStatus
    status_note: str | None
    status_expected_date: date | None
    fuel_type: EquipmentFuelType | None
    norm_consumption: Yok
    norm_unit: EquipmentNormUnit | None
    maintenance_period: EquipmentMaintenancePeriod | None
    monthly_capacity_hours: int
    # --- MK-4: SAKLANAN detay alanları ---
    # 🔴 Bunlar TÜREV DEĞİLDİR, bu yüzden buradadırlar. Ekranın kalan altı
    # sayısı (sonraki bakım saati · kalan saat · tahmini tarih · `%57` çubuğu ·
    # kümülatif ödenen) BURAYA GİRMEZ: `EquipmentDetailResponse`ta yaşarlar,
    # çünkü liste her çizilişte hareket tablosunu taramak zorunda kalırdı.
    engine_power_kw: Yok
    capacity_description: str | None
    hourmeter_hours: Yok
    rental_contract_no: str | None
    rental_start_date: date | None
    rental_end_date: date | None
    rental_min_monthly_hours: int | None
    rental_payment_terms: str | None
    last_service_date: date | None
    last_service_hourmeter: Yok
    is_company_asset: bool
    is_active: bool
    created_at: datetime


class EquipmentListResponse(BaseModel):
    """`personnel`/`inventory` liste deseni: `total` + `limit`/`offset`
    (TB3 sayfalama kanonu, `limit ≤ 200`)."""

    items: list[EquipmentResponse]
    total: int
    limit: int
    offset: int


class EquipmentSummaryResponse(BaseModel):
    """`GET /equipment/summary` — M1'in dört KPI kartı.

    🔴 **K21:** mockup ÜÇ durum rozeti çiziyor (Çalışıyor/Arızalı/Bakımda) ama
    sunucu DÖRDÜNÜ verir; `idle` basılmazsa sayaçların toplamı filoyu vermez ve
    "sunucu mockup'tan fazla veri verebilir, eksik veremez" kuralı çiğnenirdi.
    Hangisinin ekrana basılacağı frontend dilimin kararıdır.

    `monthly_cost` **cari ayın çalışma maliyeti toplamıdır** ve SATIRLARDAN
    türer (K15) — M1'in ₺124K'sı mockup'ın kendi aritmetik hatasıdır,
    kopyalanmaz. Bedeli bilinmeyen makine toplama uydurma bir `0` ile GİRMEZ
    (K16, gerekçe `service.summarize`) — bunun yerine ADETÇE
    `monthly_cost_unknown_count` ile bildirilir, çünkü sessizce atlanan makine
    kullanıcıya eksik bir parayı TAM gösterirdi (K21: sunucu fazla veri
    verebilir, eksik veremez).
    """

    model_config = ConfigDict(from_attributes=True)

    working: int
    broken: int
    maintenance: int
    idle: int
    monthly_cost: Maliyet
    monthly_cost_unknown_count: int


# --- Çalışma kaydı (M3 · spec §2.2, §4) ---

# `Numeric(6, 2)` — tek kaydın kendi saati de 24'ü aşamaz (DB CHECK'i de aynısını
# söyler). GÜNLÜK toplam tavanı (K12) bundan AYRIDIR ve serviste kilitle ölçülür.
_HOURS = Field(default=None, ge=0, le=24, max_digits=6, decimal_places=2)
_NOTE = Field(default=None, max_length=FREE_TEXT_MAX_LENGTH)


class WorkLogCreate(BaseModel):
    """`POST /equipment/work-logs` — M3 kaydı.

    🔴 **K11:** `hours` gövdede VARDIR ama YALNIZ aralıksız kayıt içindir
    (M3:283 arıza satırı saat basar, aralık basmaz). `start_time`+`end_time`
    verilmişken `hours` göndermek **422**'dir — sunucu hesabının üzerine
    yazılamaz. Kural `service._resolve_hours`ta tek yerdedir (modül docstring'i).

    `site_id` KAYDIN KENDİ şantiyesidir (K9), ekipmanın bugünkü ataması değil:
    makine taşındığında geçmiş aylar geriye dönük başka projeye yazılmasın.
    """

    equipment_id: uuid.UUID
    work_date: date
    site_id: uuid.UUID | None = None
    # K10: arıza kaydında operatör YOKTUR (M3:280) — zorunlu değildir.
    operator_id: uuid.UUID | None = None
    record_type: WorkLogType = WorkLogType.worked
    start_time: time | None = None
    end_time: time | None = None
    hours: Yok = _HOURS
    note: str | None = _NOTE


class WorkLogUpdate(BaseModel):
    """`PATCH /equipment/work-logs/{id}` — kayıt hatası düzeltilebilir.

    Çalışma kaydı MALİ İZ DEĞİLDİR (maliyet ondan TÜREV): hakediş satırının
    aksine düzeltilir ve silinir.

    Alanın GÖNDERİLMEMESİ ile `null` GÖNDERİLMESİ farklıdır (F-İK "touched"
    dersi) ve fark `model_fields_set` ile korunur — aralığı BOŞALTMANIN yolu
    `{"start_time": null, "end_time": null, "hours": …}`tır.
    """

    equipment_id: uuid.UUID | None = None
    work_date: date | None = None
    site_id: uuid.UUID | None = None
    operator_id: uuid.UUID | None = None
    record_type: WorkLogType | None = None
    start_time: time | None = None
    end_time: time | None = None
    hours: Yok = _HOURS
    note: str | None = _NOTE


class WorkLogResponse(BaseModel):
    """Kayıt künyesi. `hours` HER ZAMAN doludur ve HER ZAMAN sunucunundur."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    equipment_id: uuid.UUID
    work_date: date
    site_id: uuid.UUID | None
    operator_id: uuid.UUID | None
    record_type: WorkLogType
    start_time: time | None
    end_time: time | None
    hours: YokGirdi
    note: str | None
    created_by_id: uuid.UUID | None
    created_at: datetime


class WorkLogListResponse(BaseModel):
    """TB3 sayfalama kanonu: `limit ≤ 200`, `total` SÜZÜLMÜŞ kümeyi sayar."""

    items: list[WorkLogResponse]
    total: int
    limit: int
    offset: int


# --- Çalışma özeti (M3 ana tablosu · spec §4) ---


class WorkSummaryRow(BaseModel):
    """M3 tablosunun BİR satırı.

    `cost` ve `usage_pct` AYRI AYRI `null` olabilir (K16): saati bilinen bir
    makinenin bedeli bilinmiyor olabilir. Tek alana sıkıştırılsalardı bilinen
    bir olgu, eksik bir ölçüt yüzünden kaybolurdu.
    """

    equipment_id: uuid.UUID
    equipment_name: str
    site_id: uuid.UUID | None
    hours: YokGirdi
    usage_pct: Yok
    usage_reason: UsageReason | None
    breakdown_hours: YokGirdi
    cost: Maliyet


class WorkSummaryTotals(BaseModel):
    """🔴 K15 — tfoot. **HER ZAMAN satırlardan** toplanır.

    M3'ün kendi tfoot'u (428 saat · ₺124.800 · %69) satırlarıyla TUTARSIZDIR
    (692 · ₺144.200 · %57,7) — mockup'ın aritmetik hatasıdır ve kopyalanmaz
    (TSD `contract_total` TEK KAYNAK emsali, F-P5 K5).

    `cost` bilinmeyen satırı UYDURMA bir 0 ile İÇERMEZ (K16): satır `null`
    kalır, toplam bilinenlerden oluşur. Toplamın kendisi `null` yapılmadı çünkü
    tek bilinmeyen makine yüzünden bütün tabloyu gizlemek kullanıcıyı ekranın
    tamamından ederdi.
    """

    hours: YokGirdi
    breakdown_hours: YokGirdi
    cost: Maliyet
    usage_pct_avg: Yok


class WorkSummaryWeek(BaseModel):
    """M3:219-243 haftalık kovası.

    `dominant_record_type` SUNUCU DAMGASIDIR (F-P10 kanonu): barın rengini
    istemci kendi eşiğiyle seçseydi iki ekran aynı haftayı farklı boyardı.
    Kayıtsız haftada `null`dur — uydurma bir "çalışıyor" damgası basılmaz.
    """

    index: int
    start_date: date
    end_date: date
    hours: YokGirdi
    dominant_record_type: WorkLogType | None


class WorkSummaryResponse(BaseModel):
    """`GET /equipment/work-summary` — M3'ün TAMAMI (tablo + tfoot + mini grafik)."""

    year: int
    month: int
    rows: list[WorkSummaryRow]
    totals: WorkSummaryTotals
    weeks: list[WorkSummaryWeek]


# --- Yakıt kaydı (M4 · spec §2.3, §4 · T5) ---

_LITERS = Field(gt=0, max_digits=10, decimal_places=2)
_UNIT_PRICE = Field(gt=0, max_digits=10, decimal_places=4)
_LITERS_OPTIONAL = Field(default=None, gt=0, max_digits=10, decimal_places=2)
_UNIT_PRICE_OPTIONAL = Field(default=None, gt=0, max_digits=10, decimal_places=4)
_FUEL_NOTE = Field(default=None, max_length=FREE_TEXT_MAX_LENGTH)


class FuelLogCreate(BaseModel):
    """`POST /equipment/fuel-logs` — M4 kaydı.

    `liters`/`unit_price` DB `CHECK`i ile aynı sınırı (`> 0`) burada da taşır:
    422'nin anlaşılır olması için (İK-3/K2 emsali) — DB'ye düşseydi kullanıcı
    bütünlük hatası görürdü. `entered_by_id` GÖVDEDE YOKTUR (K14): oturum
    kullanıcısından serviste damgalanır, istemci başka birini giren gösteremez.
    """

    equipment_id: uuid.UUID
    fuel_date: date
    # K4 ile aynı hedef: NULL = depoda yapılan/kaydedilen iş.
    site_id: uuid.UUID | None = None
    liters: YokGirdi = _LITERS
    unit_price: MaliyetGirdi = _UNIT_PRICE
    note: str | None = _FUEL_NOTE


class FuelLogUpdate(BaseModel):
    """`PATCH /equipment/fuel-logs/{id}` — kayıt hatası düzeltilebilir.

    Alanın GÖNDERİLMEMESİ ile `null` GÖNDERİLMESİ farklıdır (F-İK "touched"
    dersi); fark `model_fields_set` ile korunur.
    """

    equipment_id: uuid.UUID | None = None
    fuel_date: date | None = None
    site_id: uuid.UUID | None = None
    liters: Yok = _LITERS_OPTIONAL
    unit_price: Maliyet = _UNIT_PRICE_OPTIONAL
    note: str | None = _FUEL_NOTE


class FuelLogResponse(BaseModel):
    """Kayıt künyesi. `amount` **KOLON DEĞİLDİR** — `Equipment FuelLog.amount`
    özelliğinden (`cost.fuel_amount`) TÜRETİLİR ve `from_attributes` bunu bir
    kolon gibi okur; ikinci bir çarpım burada YAZILMAZ."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    equipment_id: uuid.UUID
    fuel_date: date
    site_id: uuid.UUID | None
    liters: YokGirdi
    unit_price: Maliyet
    amount: Maliyet
    entered_by_id: uuid.UUID | None
    note: str | None
    created_at: datetime


class FuelLogListResponse(BaseModel):
    """TB3 sayfalama kanonu: `limit ≤ 200`, `total` SÜZÜLMÜŞ kümeyi sayar."""

    items: list[FuelLogResponse]
    total: int
    limit: int
    offset: int


# --- Yakıt özeti (M4 üst blok + tablo · spec §4 · T5) ---


class FuelSummaryRow(BaseModel):
    """M4 tablosunun BİR satırı — ekipman başına.

    `actual` ve `deviation_pct` AYRI AYRI `null` olabilir (K16): fiili tüketim
    biliniyorken sapma bilinmiyor olabilir (`lt_km` ya da norm yok). Rozet
    (`consumption_status`) SUNUCUDAN gelir (K17, F-P10 kanonu).
    """

    equipment_id: uuid.UUID
    equipment_name: str
    site_id: uuid.UUID | None
    liters: YokGirdi
    amount: Maliyet
    actual: Yok
    norm: Yok
    deviation_pct: Yok
    deviation_reason: DeviationReason | None
    consumption_status: ConsumptionStatus | None


class FuelSummaryResponse(BaseModel):
    """`GET /equipment/fuel-summary` — M4'ün üst bloğu + tablosu.

    🔴 **K15:** `total_liters`/`total_amount` HER ZAMAN satırlardan türer,
    mockup'ın üst blok sayıları kopyalanmaz. 🔴 **K16:** `lt_per_hour_avg`
    paydası (dönemin ÇALIŞMA KAYDI saat toplamı) 0 ise `null`dur — uydurma 0
    basılmaz. `avg_unit_price` de aynı sebeple litre toplamı 0 ise `null`dur.
    """

    year: int
    month: int
    total_liters: YokGirdi
    total_amount: Maliyet
    lt_per_hour_avg: Yok
    avg_unit_price: Maliyet
    abnormal_count: int
    rows: list[FuelSummaryRow]

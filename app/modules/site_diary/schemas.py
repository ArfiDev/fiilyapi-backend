"""Şantiye günlüğü Pydantic şemaları (T2) — okuma/yazma ayrı.

Alan sınırları modeldeki kolon tipleriyle BİREBİR (spec §2): `code`/`unit`
`String(50)`, `trade` `String(100)`, `description`/`work_done` sınırsız `Text`.

Türevler (satır ₺ katkısı, satır toplamı, işçi toplamı) yalnız OKUMA şemalarında
yaşar — kolon açılmaz (spec §2).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.modules.progress_payments.schemas import ProgressPaymentLineInput
from app.modules.site_diary import guards
from app.modules.site_diary.models import DiaryStatus, Weather, WorkerSource
from app.modules.subcontractor_progress_payments.schemas import (
    SubcontractorProgressPaymentLineInput,
)

__all__ = [
    "EmployerDiarySuggestion",
    "SiteDiaryEntryCreate",
    "SiteDiaryEntryDetail",
    "SiteDiaryEntryListItem",
    "SiteDiaryEntryListResponse",
    "SiteDiaryEntryUpdate",
    "SiteDiaryLineInput",
    "SiteDiaryLineRead",
    "SiteDiaryLinesSave",
    "SiteDiarySkeleton",
    "SiteDiarySkeletonLine",
    "SiteDiarySummary",
    "SiteDiarySummaryItem",
    "SiteDiaryWorkerCountInput",
    "SiteDiaryWorkerCountRead",
    "SubcontractorDiarySuggestion",
]

# `ck_site_diary_entries_temp_min_range` / `_temp_max_range` ile BİREBİR: DB CHECK'i son
# savunmadır ama kullanıcıya "Veri bütünlüğü hatası" der; alan hatası burada yakalanınca 422
# gövdesi hangi alanın yanlış olduğunu söyler.
_TEMP_MIN = Decimal("-60")
_TEMP_MAX = Decimal("60")
# PLN-B2.1 (B2-2): `ck_site_diary_entries_wind_range` ile BİREBİR.
_WIND_MIN = Decimal("0")
_WIND_MAX = Decimal("80")
# `Numeric(4,1)` — sıcaklık/rüzgâr alanları ölçeği Pydantic'te zorlar.
_TENTHS_DIGITS = 4
_TENTHS_DECIMALS = 1
# PLN-B2.1 (B2-5): `ck_site_diary_worker_counts_hours_range` ile BİREBİR (0 < h ≤ 24).
_HOURS_MAX = Decimal("24")

#: CLEAN-B1 Faz 1: `temperature_c` API'den KALDIRILDI (PLN-B2.1'de kullanımdan kalkmıştı);
#: CLEAN-B2: modelden ve servisten de kalktı. Eski istemciye açık 422 KALIR (API sözleşmesi).
TEMPERATURE_C_REMOVED = "`temperature_c` kaldırıldı — `temp_min_c` ve `temp_max_c` kullanın"


def _reject_temperature_c(data: object) -> object:
    """Eski istemci imzası (`temperature_c`) AÇIK 422 alır: Create'te `extra` serbest olduğu
    için alan aksi hâlde SESSİZCE yutulur ve sıcaklık kaybolurdu; Update'te genel
    `extra_forbidden` yerine ne yapılacağını söyleyen metin döner."""
    if isinstance(data, dict) and "temperature_c" in data:
        raise ValueError(TEMPERATURE_C_REMOVED)
    return data


# `Numeric(14,3)` ile BİREBİR: 11 tam + 3 ondalık basamak. Sınır Pydantic'te
# durmasaydı taşan sayı asyncpg'de `DataError`a, yani kullanıcıya 500'e dönerdi.
_QUANTITY_DIGITS = 14
_QUANTITY_DECIMALS = 3


# --- Yazma şemaları ---


class SiteDiaryLineInput(BaseModel):
    """`PUT /diary/{entry_id}/lines` gövdesindeki tek satır (T3).

    Satır kimliği `boq_item_id`dir (kısmi UQ `uq_site_diary_lines_boq_item`);
    satırın `id`si gövdede TAŞINMAZ — ekran BOQ pozunu bilir, satır kimliğini değil.

    **Snapshot dörtlüsü (`code/description/unit/unit_price`) BİLEREK YOKTUR** ve
    `extra="forbid"` onları 422 yapar: fiyat istemciden alınsaydı kullanıcı
    günlüğün ₺ katkısını (GK230) BOQ'dan bağımsız uydurabilirdi. Kaynak her zaman
    BOQ kalemidir (yeni satır) ya da mevcut satırın donmuş snapshot'ıdır.
    """

    model_config = {"extra": "forbid"}

    boq_item_id: uuid.UUID
    quantity: Decimal = Field(ge=0, max_digits=_QUANTITY_DIGITS, decimal_places=_QUANTITY_DECIMALS)
    """0 meşrudur: iskelet TÜM pozları açar, o gün dokunulmayan poz sıfır kalır (GK228)."""
    section_id: uuid.UUID | None = None
    """PLN-B2.1 — yaprak = kalem × bölüm. `None` = "Bölümsüz" (kalemin bölüme tahsis
    edilmemiş kalanı; eski istemcinin tek dalı). Dolu ise bölüm şantiyeye ait VE
    kalemin o bölüme tahsisi (`boq_item_section_allocations`) olmalıdır — yoksa 422.
    Satır kimliği artık (`boq_item_id`, `section_id`) ikilisidir."""
    overrun_reason: str | None = None
    """PLN-B2.1 (B2-8) — planlı miktar aşımının gerekçesi. Boşluk kırpılır, boş = `None`.
    Zorunluluğu çekirdekte DEĞİL Gönder portundadır (EV'li şantiyede)."""

    @field_validator("overrun_reason")
    @classmethod
    def _gerekce_kirp(cls, value: str | None) -> str | None:
        if value is None:
            return None
        kirpilmis = value.strip()
        return kirpilmis or None


class SiteDiaryLinesSave(BaseModel):
    """`PUT …/lines` gövdesi — **DEĞİŞTİRME** semantiği: gövdede geçmeyen satır
    SİLİNİR (taşeron `lines.py` deseninin aynısı). Boş liste = tüm satırları temizle."""

    lines: list[SiteDiaryLineInput] = Field(default_factory=list)


class SiteDiaryWorkerCountInput(BaseModel):
    """`PATCH /diary/{entry_id}` gövdesindeki iç içe işçi kırılımı satırı (T3).

    Satır kimliği (`trade`, `source`) İKİLİSİDİR (UQ ile birebir); aynı meslek
    FARKLI kaynakla meşrudur (GK418-430). Taşeron ADI bağlanmaz — mockup'ta
    seçici yoktur (spec §2).
    """

    model_config = {"extra": "forbid"}

    trade: str = Field(max_length=100)
    source: WorkerSource
    count: int = Field(ge=0)
    subcontractor_id: uuid.UUID | None = None
    """PLN-B2.1 (B2-5) — taşeron FİRMA satırı (puantajı tutulmayan firma ekibi). Dolu
    ise `source` `subcontractor` olmalıdır; satır kimliği o zaman FİRMADIR (firma
    başına tek satır). Boşsa eski (`trade`, `source`) kimliği geçerlidir."""
    hours: Decimal | None = Field(
        default=None,
        gt=0,
        le=_HOURS_MAX,
        max_digits=_TENTHS_DIGITS,
        decimal_places=_TENTHS_DECIMALS,
    )
    """PLN-B2.1 (B2-5) — kişi başı saat (0 < h ≤ 24). Adam-saat = `count × hours`."""

    @model_validator(mode="after")
    def _firma_yalniz_taseronda(self) -> "SiteDiaryWorkerCountInput":
        if self.subcontractor_id is not None and self.source is not WorkerSource.subcontractor:
            raise ValueError(guards.WORKER_SUBCONTRACTOR_SOURCE)
        return self

    @field_validator("trade")
    @classmethod
    def _kirp_ve_dogrula(cls, value: str) -> str:
        """Kırpma DOĞRULAMADAN ÖNCE koşar: "   " geçerli bir meslek adı değildir
        ve " Kalıpçı" ile "Kalıpçı" UQ'da AYRI iki satır olmamalıdır."""
        kirpilmis = value.strip()
        if not kirpilmis:
            raise ValueError(guards.TRADE_REQUIRED)
        return kirpilmis


class SiteDiaryEntryCreate(BaseModel):
    """`POST /sites/{site_id}/diary` gövdesi.

    Satır iskeleti OTOMATİK üretilir (`skeleton.skeleton_keys`, kural A; başlığın
    `section_id`si iskeleti süzer). `lines[]` (GKS-B1) isteğe bağlıdır: verilirse miktarlar
    AYNI işlemde iskeletle birleşir; verilmezse miktar girişi `PUT …/lines` ile yapılır (T3).

    `worker_counts[]` de YOKTUR: işçi kırılımının yazma semantiği T3'ündür.

    `status` YOKTUR: kayıt her zaman `draft` doğar, `submit` ucu (T4) gönderir.

    `entry_date` DIŞINDA her alan isteğe bağlıdır — taslak yarım doldurulabilir,
    zorunluluk kuralları `submit` katmanındadır (model docstring'i).
    """

    entry_date: date
    section_id: uuid.UUID | None = None
    weather: Weather | None = None
    temp_min_c: Decimal | None = Field(
        default=None,
        ge=_TEMP_MIN,
        le=_TEMP_MAX,
        max_digits=_TENTHS_DIGITS,
        decimal_places=_TENTHS_DECIMALS,
    )
    temp_max_c: Decimal | None = Field(
        default=None,
        ge=_TEMP_MIN,
        le=_TEMP_MAX,
        max_digits=_TENTHS_DIGITS,
        decimal_places=_TENTHS_DECIMALS,
    )
    wind_ms: Decimal | None = Field(
        default=None,
        ge=_WIND_MIN,
        le=_WIND_MAX,
        max_digits=_TENTHS_DIGITS,
        decimal_places=_TENTHS_DECIMALS,
    )
    work_done: str | None = None
    chief_note: str | None = None
    safety_meeting_held: bool = False
    ppe_checked: bool = False
    has_incident: bool = False
    incident_note: str | None = None
    lines: list[SiteDiaryLineInput] | None = None
    """GKS-B1 — İSTEĞE BAĞLI miktar satırları (önizlemeden gelen ekran). Gönderilmezse (ya da
    `null`) iskelet + 0 (bugünkü davranış). Verilirse iskeletle BİRLEŞİR (silme YOK): gövde
    miktarı iskelet satırının üzerine yazar, iskelette olmayan geçerli satırı ekler. Satır
    doğrulaması `PUT …/lines` ile aynıdır ve günlük yazılmadan ÖNCE koşar (kısmi yazma yok)."""

    @model_validator(mode="before")
    @classmethod
    def _eski_sicaklik_alani(cls, data: object) -> object:
        return _reject_temperature_c(data)

    @model_validator(mode="after")
    def _min_max(self) -> "SiteDiaryEntryCreate":
        """Gövde İÇİ min ≤ max. PATCH'te karar birleşik değer üzerinden serviste verilir."""
        if not guards.temp_order_ok(self.temp_min_c, self.temp_max_c):
            raise ValueError(guards.TEMP_ORDER)
        return self


class SiteDiaryEntryUpdate(BaseModel):
    """`PATCH /diary/{entry_id}` — yalnız BAŞLIK alanları, yalnız `draft` kayıtta.

    `status` alanı YOKTUR: durum yalnız geçiş uçlarıyla (T4) değişir; gövdeden
    kabul edilseydi `submit`in damgası ve doğrulamaları atlanabilirdi. Gövdeye
    yazılan bilinmeyen alan `extra="forbid"` ile 422'dir — sessizce yutulup
    "güncelledim" demek, kullanıcıya yalan söylemektir.

    `entry_date` DEĞİŞTİRİLEBİLİR (yanlış güne açılmış kayıt düzeltilebilsin);
    hedef gün doluysa servis 409 döner.

    `worker_counts` (T3) İÇ İÇE ve **DEĞİŞTİRME** semantiğindedir: gönderilmeyen
    (meslek, kaynak) çifti SİLİNİR, boş liste hepsini temizler. Alanın kendisi
    gönderilmezse kırılım KORUNUR (`exclude_unset`) — başlık alanı güncelleyen
    bir istek işçi kırılımını sessizce silmez. Poz satırları burada YOKTUR: onlar
    yalnız `PUT …/lines` ile değişir (iki yazma kapısı tek kaynaklıdır).
    """

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def _eski_sicaklik_alani(cls, data: object) -> object:
        return _reject_temperature_c(data)

    entry_date: date | None = None
    section_id: uuid.UUID | None = None
    weather: Weather | None = None
    temp_min_c: Decimal | None = Field(
        default=None,
        ge=_TEMP_MIN,
        le=_TEMP_MAX,
        max_digits=_TENTHS_DIGITS,
        decimal_places=_TENTHS_DECIMALS,
    )
    temp_max_c: Decimal | None = Field(
        default=None,
        ge=_TEMP_MIN,
        le=_TEMP_MAX,
        max_digits=_TENTHS_DIGITS,
        decimal_places=_TENTHS_DECIMALS,
    )
    wind_ms: Decimal | None = Field(
        default=None,
        ge=_WIND_MIN,
        le=_WIND_MAX,
        max_digits=_TENTHS_DIGITS,
        decimal_places=_TENTHS_DECIMALS,
    )
    work_done: str | None = None
    chief_note: str | None = None
    safety_meeting_held: bool | None = None
    ppe_checked: bool | None = None
    has_incident: bool | None = None
    incident_note: str | None = None
    worker_counts: list[SiteDiaryWorkerCountInput] | None = None

    @field_validator("worker_counts")
    @classmethod
    def _null_reddedilir(
        cls, value: list[SiteDiaryWorkerCountInput] | None
    ) -> list[SiteDiaryWorkerCountInput]:
        """Öntanım DOĞRULANMAZ (Pydantic), bu yüzden bu kural YALNIZ alan AÇIKÇA
        gönderildiğinde koşar. `null` ile `[]` arasındaki belirsizlik böyle kapanır:
        "hepsini sil" demek isteyen boş liste gönderir, `null` bir niyet DEĞİLDİR
        ve sessizce yok sayılırsa kullanıcı sildiğini sanır."""
        if value is None:
            raise ValueError(guards.WORKER_COUNTS_NULL)
        return value


# --- Okuma şemaları ---


class SiteDiaryLineRead(BaseModel):
    """GK212-230 satırı — poz snapshot'ı + o günkü miktar + İKİ TÜREV."""

    id: uuid.UUID
    boq_item_id: uuid.UUID | None
    code: str = Field(max_length=50)
    description: str
    unit: str = Field(max_length=50)
    unit_price: Decimal
    quantity: Decimal
    cumulative_quantity: Decimal
    """GK229 kümülatif — TÜREV (kolon yok, spec §2). Tanım: aynı ay + aynı şantiye
    + aynı poz için bu günden ÖNCEKİ `submitted` kayıtların toplamı **artı bu
    kaydın kendi miktarı** (kaydın durumu ne olursa olsun).

    Neden bu tanım: T4 `summary` YALNIZ `submitted` sayar (spec §3). Gönderilmiş
    bir kayıtta bu tanım "≤ bugün olan gönderilmişler"e BİREBİR eşitlenir, yani
    ekrandaki kümülatif ile hakediş özeti aynı sayıyı söyler; taslakta ise
    "gönderirsem kümülatif ne olacak" sorusunu yanıtlar. BAŞKA günlerin
    TASLAKLARI sayılmaz — sayılsaydı iki ekran iki farklı sayı gösterirdi."""
    line_amount: Decimal
    """GK230 ₺ katkısı = `quantity × unit_price`, KATSAYISIZ (spec §2): fiyat
    farkı katsayısı hakediş katmanının işidir, günlüğün değil. TÜREV — kolon yok."""
    section_id: uuid.UUID | None = None
    """PLN-B2.1 — satırın bölümü; `None` = "Bölümsüz"."""
    overrun_reason: str | None = None
    """PLN-B2.1 (B2-8) — planlı miktar aşımı gerekçesi."""
    leaf_cumulative_quantity: Decimal | None = None
    """PLN-B2.1 — YAPRAK (kalem × bölüm) kümülatifi, TÜM ZAMANLAR: aynı şantiye + aynı
    kalem + AYNI bölüm (Bölümsüz = Bölümsüz) için bu günden ÖNCEKİ **gönderilmiş**
    günlüklerin toplamı + BU satırın miktarı (kaydın durumu ne olursa olsun).
    `cumulative_quantity`den FARKI: ay sınırı YOK ve bölüm kırılımlıdır (planlı
    miktarla kıyaslanan budur). Bağı kopmuş satırda `None`."""
    planned_quantity: Decimal | None = None
    """PLN-B2.1 — yaprağın planlı miktarı: bölümlü satırda kalemin o bölüme TAHSİSİ;
    Bölümsüz'de kalem miktarı − Σ tahsis (tahsis edilmemiş kalan, ≥ 0). Tahsisi
    sonradan kaldırılmış bölümlü satırda 0. Bağı kopmuş satırda `None`."""
    remaining_quantity: Decimal | None = None
    """PLN-B2.1 — `planned_quantity − leaf_cumulative_quantity`; NEGATİF = aşım."""
    section_name: str | None
    """DET-1.B — satırın bölümünün ANLIK adı; `None` = "Bölümsüz"."""


class SiteDiarySkeletonLine(BaseModel):
    """GKS-B1 — `SiteDiaryLineRead`in KİMLİKSİZ ikizi (kaydedilmemiş iskelet satırı).

    Alan adları ve anlamları `SiteDiaryLineRead` ile AYNIDIR (yalnız `id` yok); değerler
    detay ucuyla AYNI hesaptan gelir (`read.line_fields`), `quantity` 0'dır. Alan kümesi
    eşitliği bekçilidir (`tests/site_diary/test_gks_b1_onizleme_post_bekcisi.py`)."""

    boq_item_id: uuid.UUID | None
    code: str = Field(max_length=50)
    description: str
    unit: str = Field(max_length=50)
    unit_price: Decimal
    quantity: Decimal
    cumulative_quantity: Decimal
    line_amount: Decimal
    section_id: uuid.UUID | None = None
    overrun_reason: str | None = None
    leaf_cumulative_quantity: Decimal | None = None
    planned_quantity: Decimal | None = None
    remaining_quantity: Decimal | None = None
    section_name: str | None


class SiteDiarySkeleton(BaseModel):
    """`GET /sites/{site_id}/diary/skeleton` — kaydetmeden önizleme (GKS-B1)."""

    entry_date: date
    section_id: uuid.UUID | None
    section_name: str | None
    existing_entry_id: uuid.UUID | None
    """O tarihte bu şantiyede günlük ZATEN varsa kimliği (409 DEĞİL): istemci detaya yönlenir.
    İskelet yine aynı kuralla döner."""
    locked: bool
    lock_report_date: date | None
    lines: list[SiteDiarySkeletonLine]
    lines_total: Decimal


class SiteDiaryWorkerCountRead(BaseModel):
    """GK418-430 işçi kırılımı satırı. Yazma semantiği T3'tedir."""

    id: uuid.UUID
    trade: str = Field(max_length=100)
    source: WorkerSource
    count: int
    subcontractor_id: uuid.UUID | None = None
    """PLN-B2.1 (B2-5) — taşeron firma satırı ise firma."""
    hours: Decimal | None = None
    """PLN-B2.1 (B2-5) — kişi başı saat."""
    subcontractor_name: str | None
    """DET-1.B — taşeron firma satırında firmanın ANLIK adı; firmasız satırda `None`."""


class SiteDiaryEntryListItem(BaseModel):
    """GK "Son Kayıtlar" satırı — durum + işçi toplamı + satır ₺ toplamı.

    Üç alanın da kolonu YOKTUR (spec §2); listede taşınmalarının nedeni ekranın
    kayıt başına ayrı bir detay isteği atmak zorunda kalmamasıdır.
    """

    id: uuid.UUID
    site_id: uuid.UUID
    project_id: uuid.UUID
    entry_date: date
    section_id: uuid.UUID | None
    section_name: str | None
    """DET-1.B ek (#129) — başlık bölümünün ANLIK adı; başlıksız kayıtta `None`."""
    section_line_count: int | None
    """DET-1.B ek (#129) — YALNIZ `?section_id=` verilince: o bölüme düşen miktar satırı
    sayısı (başlık kolundan gelen, satırı olmayan günde 0); süzgeçsiz listede `None`."""
    weather: Weather | None
    has_incident: bool
    status: DiaryStatus
    worker_total: int
    lines_total: Decimal
    created_by: uuid.UUID
    created_at: datetime


class SiteDiaryEntryListResponse(BaseModel):
    """`audit`/`users` liste deseninin aynısı: `total` + `limit`/`offset`."""

    items: list[SiteDiaryEntryListItem]
    total: int
    limit: int
    offset: int


class OwnCrewFromTimesheet(BaseModel):
    """Puantajdan türeyen ekip satırı (EV-BORC-2): personelin KENDİ `trade` + `source` alanı."""

    trade: str
    source: WorkerSource
    headcount: int
    hours: Decimal


class SiteDiaryEntryDetail(BaseModel):
    """`GET /diary/{entry_id}` — GK'nin tek gün görünümü."""

    id: uuid.UUID
    site_id: uuid.UUID
    project_id: uuid.UUID
    entry_date: date
    section_id: uuid.UUID | None
    weather: Weather | None
    work_done: str | None
    chief_note: str | None
    safety_meeting_held: bool
    ppe_checked: bool
    has_incident: bool
    incident_note: str | None
    status: DiaryStatus
    submitted_at: datetime | None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    lines: list[SiteDiaryLineRead]
    worker_counts: list[SiteDiaryWorkerCountRead]
    lines_total: Decimal
    worker_total: int
    temp_min_c: Decimal | None = None
    temp_max_c: Decimal | None = None
    wind_ms: Decimal | None = None
    own_crew_from_timesheet: list[OwnCrewFromTimesheet] = Field(default_factory=list)
    """EV-BORC-2: o günün PUANTAJINDAN türeyen ekip (salt okunur) — meslek + kaynak başına
    kişi sayısı ve saat. Günlük işçi satırlarıyla EŞLEME YOKTUR (ikisi de serbest metin);
    ekran kendi ekip bloğunu buradan basar. Meslek boş personel `"Belirtilmemiş"`te toplanır."""
    site_name: str
    project_name: str
    section_name: str | None
    """DET-1.B — başlık bölümünün ANLIK adı (salt okunur detay TEK istekte kurulsun)."""
    created_by_name: str | None
    submitted_by: uuid.UUID | None
    """DET-1.B — gönderen; `submitted_at` ile birlikte yazılır, reopen temizler. DET-1.B
    ÖNCESİ gönderimlerde `None` (geri doldurma yok)."""
    submitted_by_name: str | None
    locked: bool
    """DET-1.B — gün kilidi (`app.core.day_hooks` portu; kilit sağlayıcı modül yoksa `False`).
    EV izni gerektirmez: günlük görüntüleyen kilidi de görür."""
    lock_report_date: date | None
    """DET-1.B — kilidi koyan raporun tarihi (sağlayıcı bildirmiyorsa `None`)."""
    prev_id: uuid.UUID | None
    """DET-1.B — tarih sırasında ÖNCEKİ kayıt. `GET /diary/{id}?section_id=` verilirse bölüm
    bağlamında (Kural A: başlık ∪ satır), verilmezse şantiye bağlamında. İlk kayıtta `None`."""
    next_id: uuid.UUID | None
    """DET-1.B — tarih sırasında SONRAKİ kayıt (bağlam `prev_id` ile aynı). Son kayıtta `None`."""
    prev_entry_date: date | None
    """DET-1.B — `prev_id` kaydının günü (aynı sorgudan; `prev_id` `None` ise `None`)."""
    next_entry_date: date | None
    """DET-1.B — `next_id` kaydının günü (aynı sorgudan; `next_id` `None` ise `None`)."""
    dropped_orphan_count: int | None = None
    """YALNIZ `PUT …/lines` yanıtında dolar (T3). Bağı kopmuş satır (`boq_item_id
    IS NULL`, FK `SET NULL`) gövdeden ADRESLENEMEZ, bu yüzden ilk kaydetmede
    düşer — kaçınılmaz ama SESSİZ değil: sayısı kullanıcıya bildirilir.

    `submit`/`reopen` yanıtlarında NULL kalır: durum geçişi satır DÜŞÜRMEZ."""


# --- Agregasyon (T4) — `Şantiye - Hakediş Özeti` ekranının veri sözleşmesi ---


class SiteDiarySummaryItem(BaseModel):
    """Hakediş Özeti tablosunun bir SATIRI (mockup `Şantiye - Hakediş Özeti` L127).

    Sütun eşlemesi mockup'tan okunur, tahmin EDİLMEZ:
    L131 "İş Kalemi" → `code`/`description` · L132 "Sözleşme" → `boq_quantity` ×
    `unit_price` = `boq_amount` (GK L226 "Sözleşme: 1.200 m³ · Birim fiyat:
    ₺1.850") · L133 "Bu Ay" → `quantity`/`amount` · L134 "%" → `completion_ratio`.
    """

    boq_item_id: uuid.UUID
    code: str = Field(max_length=50)
    description: str
    unit: str = Field(max_length=50)
    unit_price: Decimal
    """BOQ kaleminin GÜNCEL birim fiyatı ("Sözleşme" sütununun çarpanı). `amount`
    bundan HESAPLANMAZ — o, satırların DONMUŞ snapshot fiyatlarından gelir."""
    quantity: Decimal
    """Dönemde **gönderilmiş** günlerin poz bazlı miktar toplamı (HÖ L133).
    Taslak günler GİRMEZ (spec §3) — T3 kümülatifiyle AYNI süzgeç."""
    amount: Decimal
    """Dönemin ₺ katkısı: satır bazında yuvarlanmış `line_amount` toplamı
    (`read.line_amount`, TEK kopya). Fiyat SATIRIN snapshot'ından gelir: geçmiş
    ayın hakedişi, BOQ fiyatı bugün değiştiği için yeniden yazılamaz."""
    boq_quantity: Decimal
    boq_amount: Decimal
    completion_ratio: Decimal | None
    """HÖ L134 "%" — `quantity / boq_quantity` (GK L229 "900 / 1.200" = %75).
    Sözleşme miktarı sıfırsa NULL: sıfıra bölmek yerine "oran tanımsız" denir."""
    contract_item_id: uuid.UUID | None
    """`boq_items.contract_item_id` KÖPRÜSÜ (plan T4). T5 "günlükten doldur"
    önerisi günlük miktarını bu kimlik üzerinden işveren hakedişi satırına
    eşleyecektir. Şantiyenin kendi açtığı poz için NULL'dur — sessizce
    düşürülmez, ekranda "sözleşmesiz" olarak görünür."""
    contract_item_quantity: Decimal | None
    contract_item_unit_price: Decimal | None


class SiteDiarySummary(BaseModel):
    """`GET /sites/{site_id}/diary/summary` — YALNIZ `submitted` günler (spec §3).

    `total_amount` mockup'ın tfoot'udur (HÖ L165-168 "Bu Ay Toplam ₺269.200").
    Sözleşme sütununun TOPLAMI YOKTUR: mockup'ta o tfoot hücresi BOŞTUR (L166) —
    icat edilmez.

    Ekranın diğer KPI kartları (HÖ L101 "İşveren Hakediş", L106 "Taşeron
    Ödemeleri", L111 "Brüt Kar", L116 "Kümülatif Hakediş") günlükten DEĞİL
    hakediş modüllerinden beslenir; bu uç onları ÜRETMEZ.
    """

    site_id: uuid.UUID
    year: int | None
    month: int | None
    """Seçilen dönem ECHO edilir — ekran hangi ayı gösterdiğini bilmelidir (HÖ L86)."""
    entry_count: int
    """Özete giren GÜN sayısı. "Hiç gönderilmemiş" ile "gönderilmiş ama miktarı
    sıfır" hâllerini ayırır; boş küme 404 değil sıfırlı özettir (zarif düşüş)."""
    items: list[SiteDiarySummaryItem]
    total_amount: Decimal


# --- T5: hakediş "günlükten doldur" önerisi (spec §4, §7 S2) ---
#
# `lines[]` tipi HAKEDİŞ MODÜLLERİNİN KENDİ giriş şemasıdır — yeni bir "öneri
# satırı" şeması TANIMLANMAZ. Sözleşme şudur: yanıtın `lines` alanı DEĞİŞTİRİLMEDEN
# ilgili `PUT …/lines` gövdesine yapıştırılabilir. İkinci bir şekil tanımlansaydı
# alanlar zamanla ayrışır ve öneri sessizce yapıştırılamaz hâle gelirdi.
#
# 🛑 Uçlar SALT OKUNURDUR: hiçbir hakediş satırı YAZILMAZ, hiçbir `quantity_source`
# damgalanmaz. Öneriyi uygulamak kullanıcının AYRI `PUT …/lines` çağrısıdır
# (spec §4: otomasyon YOK).


class _DiarySuggestionBase(BaseModel):
    """İki önerinin ORTAK gövdesi."""

    year: int | None
    month: int | None
    """Seçilen dönem ECHO edilir — ekran hangi ayı önerdiğini bilmelidir."""
    skipped_unbridged_count: int
    """Miktarı olduğu hâlde sözleşme kalemine bağlanamadığı için öneriye GİRMEYEN
    poz sayısı. Sessiz atlama yok (T3 `dropped_orphan_count` deseninin aynısı)."""
    reason: str | None
    """Liste BOŞSA neden boş olduğu (`guards.SUGGESTION_*`). Dolu listede `None`.
    Sessiz boş liste, kullanıcıyı olmayan bir hatayı aramaya gönderirdi."""


class EmployerDiarySuggestion(_DiarySuggestionBase):
    """`GET /projects/{project_id}/progress-payments/diary-suggestion`.

    Satırların kırılımı (kalem, şantiye) çiftidir — işveren hakediş hücresinin
    kimliği budur. `coefficient` BİLEREK `None` gelir: katsayı bir GÜNLÜK verisi
    değildir, hakedişin `default_coefficient`'ı uygulanır (spec §4.1).
    """

    project_id: uuid.UUID
    lines: list[ProgressPaymentLineInput]


class SubcontractorDiarySuggestion(_DiarySuggestionBase):
    """`GET /subcontractor-contracts/{contract_id}/progress-payments/diary-suggestion`.

    `site_id` sözleşmenin şantiyesidir. `None` ise (proje geneli sözleşme) öneri
    kapsam dışı DEĞİLDİR: miktarlar sözleşmenin PROJESİNDEKİ TÜM şantiyelerin
    günlüğünden toplanır (kullanıcı kararı 2026-08-27 — eski spec §7 S5 kuralı
    TERSİNE ÇEVRİLDİ). Alan yine de NULL döner, çünkü sözleşme gerçekten
    şantiyesizdir; `lines` gruplaması her iki hâlde de yalnız KALEMDİR.
    """

    contract_id: uuid.UUID
    site_id: uuid.UUID | None
    lines: list[SubcontractorProgressPaymentLineInput]

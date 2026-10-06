"""Bordro şemaları — `compute` özeti (T2) + dönem/satır sözleşmeleri (T3).

## `extra="forbid"` bu modülde ÇEKİRDEK KURALDIR

İK-2'nin `days` emsali: istemci SUNUCU HESABINI gönderemez. Bordroda bu kural
para sınıfıdır — `net_amount`, `deduction_amount`, `status` ya da
`is_overridden` gövdeden kabul edilseydi bir istemci kendi hesabını yazdırabilir
ve S3/S5 kapıları anlamsızlaşırdı. Yazma şemaları **yalnız** kullanıcının
gerçekten girdiği üç alanı taşır: brüt (K3 override) + banka/elden bölüşümü.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.field_mask import Hassas
from app.modules.payroll import income_tax
from app.modules.payroll.models import (
    BRACKET_BOUND_PRECISION,
    MONEY_PRECISION,
    MONEY_SCALE,
    RATE_PRECISION,
    RATE_SCALE,
    IncomeKind,
    PayrollLineStatus,
    PayrollPeriodStatus,
)
from app.modules.site_diary.models import WorkerSource

#: Dönem yılı için akla yatkın sınırlar. Serbest bırakılsaydı bir yazım hatası
#: (20226) UQ'yu kirletir ve geri alınamayan bir dönem satırı yaratırdı.
MIN_PAYROLL_YEAR = 2000
MAX_PAYROLL_YEAR = 2100

#: Para alanlarının şema karşılığı — `Numeric(12,2)` ile AYNI ölçek (models.py).
#: `Annotated` tipidir, paylaşılan bir `Field` NESNESİ değil: tek bir `FieldInfo`
#: üç alana birden verilseydi Pydantic onu paylaşılan durum olarak taşırdı.
Money = Annotated[Decimal, Field(ge=0, max_digits=MONEY_PRECISION, decimal_places=MONEY_SCALE)]

# 🔴 GECE KARARI (IZN-B4c, payroll): bordro TUTARLARI (brüt, net, kesinti, SGK/vergi/damga payları,
# banka/elden bölüşümü, matrah, toplamlar, işveren maliyeti) = `maas_kisisel`. Rolün gizlediği
# alan `null` döner → yanıt tipi `| None`dır. `bank_amount`/`cash_amount` bir HESAP DEĞİL, o satırın
# ödeme bölüşümüdür (hesap no/IBAN bordroda YOKTUR) → `banka_kasa` EKLENMEDİ. Oranlar, yüzdeler,
# vergi dilimi sınırları (mevzuat parametresi), gün sayısı = `Hassas.yok`. Sayaçlar (`int`) bekçiye
# takılmaz ve gizlenmez. İstek şemasındaki `Girdi` alanları (brüt/banka/elden) PATCH'te gizli rol
# tarafından dolu gönderilirse 403.
BordroTutar = Annotated[Decimal | None, Hassas.maas_kisisel]
BordroTutarGirdisi = Annotated[Money | None, Hassas.maas_kisisel]
Gun = Annotated[Decimal | None, Hassas.yok]
Oran = Annotated[Decimal, Hassas.yok]
OranOpsiyonel = Annotated[Decimal | None, Hassas.yok]
# Sayaç: tutar DEĞİL (bekçi ad listesi `paid` tokenini yakalar, IZN-B4b onarımı).
SatirSayaci = Annotated[int, Hassas.yok]


class PayrollComputeResult(BaseModel):
    """`POST /payroll/periods/{id}/compute` özeti — **sessiz atlama YOKTUR**.

    Atlanan satırlar sayıyla raporlanır (WORKFLOW §3): kullanıcı "hesapladım"
    yanıtını alıp elle düzelttiği satırın niçin değişmediğini merak etmemelidir.
    İki atlama sebebi AYRI sayılır çünkü anlamları farklıdır — biri kullanıcının
    kendi düzeltmesidir (K3/S6), diğeri ödeme izidir (S5).
    """

    created: int = Field(description="Yeni açılan satır sayısı")
    updated: int = Field(description="Yeniden hesaplanıp güncellenen satır sayısı")
    skipped_overridden: int = Field(
        description="Elle düzeltildiği için KORUNAN satır sayısı (S6)",
    )
    skipped_approved: int = Field(
        description="Onaylı/ödenmiş olduğu için KORUNAN satır sayısı (S5)",
    )
    #: 🔴 IK3-GV K4 — SIRASIZ DÖNEM sayacı. Aynı yılın daha erken bir ayı hiç
    #: açılmamışsa ya da hâlâ `draft` ise o ayın matrahı kümülatife GİRMEMİŞTİR
    #: ve bu dönemin gelir vergisi olması gerekenden DÜŞÜK çıkar.
    #:
    #: 409 ile REDDEDİLMEZ (yıl ortasında sisteme geçişi imkânsız kılardı) ama
    #: SESSİZ DE GEÇİLMEZ: doğru sırayla hesaplanmış bir dönem ile sırasız
    #: hesaplanmış bir dönem ayırt edilebilir olmalıdır ("aynı yeşil iki anlam
    #: taşır" kanonu). İK-2'nin `unknown_entitlement_personnel` emsali.
    missing_prior_period_count: int = Field(
        default=0,
        description=(
            "Aynı yılda bu aydan ÖNCE gelen ve henüz açılmamış ya da taslak olan "
            "dönem sayısı: kümülatif vergi matrahı EKSİK olabilir (K4)"
        ),
    )


class PayrollPeriodApproveResult(BaseModel):
    """`POST /payroll/periods/{id}/approve` — BY 303 "Tümünü Onayla".

    🔴 **Atlananlar SEBEBE GÖRE ayrı sayılır** (WORKFLOW §3): "3 satır onaylandı"
    tek başına, iki satırın niçin dışarıda kaldığını gizlerdi ve kullanıcı eksik
    ödemeyi banka ekstresinden öğrenirdi. Sebepler farklı İŞ gerektirir:

    * `skipped_excluded` → **K2**: taşeron satırı; ödemesi hakediş modülünün
      (TH) işidir, burada yapılacak bir şey YOKTUR;
    * `skipped_uncomputed` → **S4**: ücret verisi eksik; kullanıcı ya personelin
      ücretini tanımlar ya da brütü elle girer;
    * `skipped_already_approved` → satır zaten onaylı/ödenmiş; bilgi amaçlıdır.

    `period_status` DÖNÜŞTE VERİLİR çünkü uç dönemi TEK ADIM ilerletir
    (`draft → pending_approval → approved`) ve ekran hangi adımda olduğunu
    yanıttan öğrenmelidir — ikinci bir `GET` ile tahmin etmemelidir.
    """

    period_status: PayrollPeriodStatus
    approved: int = Field(description="Onaylanan satır sayısı")
    skipped_uncomputed: int = Field(description="Brütü hesaplanamadığı için atlanan satır (S4)")
    skipped_excluded: int = Field(description="Taşeron olduğu için atlanan satır (K2)")
    skipped_already_approved: int = Field(description="Zaten onaylı/ödenmiş satır")


class PayrollPeriodPayResult(BaseModel):
    """`POST /payroll/periods/{id}/pay` — ödendi damgası (spec §5).

    🔴 `paid_net_total` ÖDENEN satırların netidir; **taşeron satırı bu toplama
    GİRMEZ** (K2). Girseydi banka talimatı taşeron işçisinin netini de taşır ve
    aynı emek hem hakedişten hem bordrodan ödenirdi.

    `skipped_unapproved` sessiz atlamayı kapatır: onayı geri alınmış bir satır
    ödenmez ve bu ekranda GÖRÜNÜR.
    """

    period_status: PayrollPeriodStatus
    paid_at: datetime
    paid: SatirSayaci = Field(description="Ödendi damgası basılan satır sayısı")
    paid_net_total: BordroTutar = Field(description="Ödenen satırların net toplamı")
    skipped_unapproved: int = Field(description="Onaylanmadığı için ödenmeyen satır")
    skipped_uncomputed: int = Field(description="Brütü hesaplanamadığı için ödenmeyen satır (S4)")
    skipped_excluded: int = Field(description="Taşeron olduğu için ödenmeyen satır (K2)")


# --- Dönem yazma -----------------------------------------------------------


class PayrollPeriodCreate(BaseModel):
    """`POST /payroll/periods` gövdesi — ay AÇAR, doldurmaz.

    `status` gövdeden ALINMAZ: yeni dönem HER ZAMAN `draft`tır ve ileri
    durumlara yalnız geçiş tablosundan (`transitions.py`, S8) gidilir. Alan
    açılsaydı istemci bir ayı doğrudan `paid` açıp onay zincirini atlardı.
    """

    model_config = ConfigDict(extra="forbid")

    year: int = Field(ge=MIN_PAYROLL_YEAR, le=MAX_PAYROLL_YEAR)
    month: int = Field(ge=1, le=12)
    #: BY 63 "Son ödeme" — bilgi alanıdır, geçiş kapısı DEĞİLDİR (models.py).
    payment_due_date: date | None = None


class PayrollPeriodUpdate(BaseModel):
    """`PATCH /payroll/periods/{id}` gövdesi — YALNIZ ödeme tarihi (T4b).

    Dönemin başka hiçbir alanı buradan değişmez: `year`/`month` kimliktir (UQ),
    `status` geçiş tablosunun (S8) işidir, damgalar (`approved_at`/`paid_at`/
    `sgk_submitted_at`) kendi uçlarında basılır. Alan eklemek bu uçtan onay
    zincirini atlamayı mümkün kılardı.

    🔴 **Boş gövde 422'dir ve bu `null` göndermekten AYRIDIR.** Tek alanlı ve
    nullable bir şemada `{}` ile `{"payment_due_date": null}` varsayılan
    değerle ayırt edilemez; ayrım `model_fields_set` ile korunur. İkisi tek
    davranışa indirgenseydi ya boş bir istek tarihi sessizce SİLERDİ ya da
    yanlış girilmiş bir tarihi temizlemek imkânsız olurdu.
    """

    model_config = ConfigDict(extra="forbid")

    #: BY 63 "Son ödeme". **Sunucu tarih ÜRETMEZ, varsayılan KOYMAZ ve dönemin
    #: yıl/ayıyla tutarlılığını DENETLEMEZ:** mockup bu alanın formunu çizmez,
    #: BG'nin üç dönemde de ayın 20'sini göstermesi bir iş kuralı DEĞİLDİR
    #: (WORKFLOW §3, uydurma yasağı) ve ödeme gerçek hayatta sonraki aya sarkar.
    payment_due_date: date | None = None

    @model_validator(mode="after")
    def _en_az_bir_alan(self) -> "PayrollPeriodUpdate":
        if "payment_due_date" not in self.model_fields_set:
            raise ValueError("Güncellenecek en az bir alan gönderilmelidir")
        return self


# --- Satır yazma -----------------------------------------------------------


class PayrollLineUpdate(BaseModel):
    """`PATCH /payroll/lines/{id}` gövdesi — kullanıcının GİRDİĞİ üç alan.

    * `gross_amount` → K3 brüt override'ı; kesinti ve net bundan YENİDEN türer
      (`compute.deduction_and_net`), gövdeden alınmaz.
    * `bank_amount` + `cash_amount` → BY 142-147'deki iki ayrı `input`.
      **İkisi birlikte gönderilir**: yalnız biri gönderilip öteki sunucuya
      tamamlatılsaydı S3 bir DOĞRULAMA değil bir HESAP olurdu ve "gerisi elden
      mi, yoksa yanlış mı yazdım?" ayrımı kaybolurdu.

    Boş gövde reddedilir: hiçbir alan göndermemek bir işlem değildir ve 200
    dönmek kullanıcıya "kaydettim" demek olurdu.
    """

    model_config = ConfigDict(extra="forbid")

    gross_amount: BordroTutarGirdisi = None
    bank_amount: BordroTutarGirdisi = None
    cash_amount: BordroTutarGirdisi = None

    @model_validator(mode="after")
    def _en_az_bir_alan_ve_bolusum_butun(self) -> "PayrollLineUpdate":
        if self.gross_amount is None and self.bank_amount is None and self.cash_amount is None:
            raise ValueError("Güncellenecek en az bir alan gönderilmelidir")
        if (self.bank_amount is None) != (self.cash_amount is None):
            raise ValueError("Banka ve elden tutarları BİRLİKTE gönderilmelidir")
        return self


# --- Okuma -----------------------------------------------------------------


class PayrollLineResponse(BaseModel):
    """BY tablosunun bir satırı (110-118 başlıkları + 133-148 gövdesi).

    `personnel_name` satıra GÖMÜLÜR (BY 137): ekran her satır için ikinci bir
    personel isteği atmak zorunda kalmamalıdır. `personnel_source` satırın
    SNAPSHOT'ıdır (models.py) — canlı personel tipi değil.

    Beş para alanı da `null` OLABİLİR (S4): "0 ödenecek" ile "hesaplanamadı"
    ayırt edilebilir kalmalıdır.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    personnel_id: uuid.UUID
    personnel_name: str
    personnel_source: WorkerSource
    #: 🔴 PUAN-SAAT-3: **`int` DEĞİL `Decimal`** — adam-gün artık bir SAYIM değil
    #: bir TÜREVDİR (`toplam saat ÷ 9`, E5 349-350) ve yarım günü temsil eder.
    days: Gun
    gross_amount: BordroTutar
    deduction_amount: BordroTutar
    net_amount: BordroTutar
    bank_amount: BordroTutar
    cash_amount: BordroTutar
    #: --- IK3-GV K1: vergi SNAPSHOT'ı (üçü birlikte dolar ya da birlikte `null`) ---
    #: `tax_base_amount` = brüt − SGK işçi − işsizlik işçi (asgari ücret
    #: istisnası bu matrahta KALIR — indirim değil KREDİdir, KK-7);
    #: `cumulative_tax_base` = yıl başından bu ay DAHİL biriken matrah;
    #: `income_tax_amount` = o ayın gelir vergisi, istisna DÜŞÜLMÜŞ (taban 0).
    tax_base_amount: BordroTutar
    cumulative_tax_base: BordroTutar
    income_tax_amount: BordroTutar
    status: PayrollLineStatus
    #: K2 — satırın niçin ödemeye girmediği YAZILI (sessiz atlama yok).
    excluded_reason: str | None
    #: K3 izi — ekran "elle düzeltildi" rozetini bundan basar.
    is_overridden: bool
    overridden_at: datetime | None
    previous_gross_amount: BordroTutar


class PayrollSectionResponse(BaseModel):
    """BY 124/172/240/268 bölüm başlıkları — tip bazında gruplama.

    Bölüm ETİKETİ ("ŞİRKET KADROSU — SGK 4a") sunucudan DÖNMEZ: o bir ekran
    metnidir ve mockup'ta rejim adıyla birlikte yazılır. Sunucu tipi ve sayıyı
    verir; sayı BURADAN gelir çünkü başlık "· 12 çalışan" basar ve ekranın
    kendi `lines.length`ini sayması sayfalanmış bir listede yanlış olurdu.
    """

    personnel_source: WorkerSource
    line_count: int
    lines: list[PayrollLineResponse]


class PayrollSummaryResponse(BaseModel):
    """BY 69-93'ün dört kartı + görünür sayaçlar (`summary.PeriodSummary` aynası).

    🔴 İki taban ayrıdır: ilk üç kart ÖDEME tabanını (`excluded`/`uncomputed`
    hariç), dördüncü kart MALİYET tabanını (`excluded` DAHİL) gösterir.
    Ayrıntı `summary.py` docstring'inde.
    """

    model_config = ConfigDict(from_attributes=True)

    line_count: int
    net_total: BordroTutar
    net_personnel_count: int
    bank_total: BordroTutar
    bank_personnel_count: int
    #: Ödeme tabanı boşken **`null`** — 0 basmak "hepsi banka" yalanı olurdu.
    bank_pct: OranOpsiyonel
    cash_total: BordroTutar
    cash_personnel_count: int
    cash_pct: OranOpsiyonel
    gross_total: BordroTutar
    sgk_employer_total: BordroTutar
    #: brüt + (SGK işveren + işsizlik işveren + kısa çalışma) — spec §7.
    total_employer_cost: BordroTutar
    uncomputed_count: int
    excluded_count: int
    unknown_cost_count: int


class PayrollPeriodDetailResponse(BaseModel):
    """BY ekranının tamamı: dönem künyesi + dört kart + tip bazında satırlar."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    year: int
    month: int
    status: PayrollPeriodStatus
    payment_due_date: date | None
    approved_at: datetime | None
    paid_at: datetime | None
    sgk_submitted_at: datetime | None
    summary: PayrollSummaryResponse
    sections: list[PayrollSectionResponse]


class PayrollPeriodListRow(BaseModel):
    """BG tablosunun bir satırı (44-47 başlıkları).

    `personnel_count` dönemin TÜM satırlarını sayar (BY tfoot 48 = 12+29+5+2);
    BY 71'in kart sayısı ise ÖDENEBİLİR satırlardır. İkisi aynı değildir
    (`summary.py` gerekçesi) ve tek alana indirgenirse biri yalan söyler.
    """

    id: uuid.UUID
    year: int
    month: int
    status: PayrollPeriodStatus
    payment_due_date: date | None
    paid_at: datetime | None
    personnel_count: int
    gross_total: BordroTutar
    #: BG 47 — YALNIZ SGK işveren payı; toplam maliyetin üç kaleminden biri.
    sgk_employer_total: BordroTutar
    net_total: BordroTutar
    #: BG 49 — `total_employer_cost` ile AYNI kaynaktan (`compute`), kopya değil.
    total_cost: BordroTutar


class PayrollPeriodListResponse(BaseModel):
    items: list[PayrollPeriodListRow]
    total: int
    limit: int
    offset: int


# --- T5: SGK bildirimi (SGK 55-95) -----------------------------------------


class PayrollSgkSummaryResponse(BaseModel):
    """SGK 55-95 — KPI dörtlüsü + işçi payları + işveren payları + ödenecek prim.

    🔴 **Mockup TUTARLARI beklenti değildir** (spec S1): SGK mockup'ı kendi
    aritmetiğine uymuyor (SGK 82 işveren toplamını 148.800 yazar, kendi
    oranlarından 174.652 çıkar). Açık ORAN kazanır ve buradaki sayılar
    mockup'takinden BÜYÜKTÜR — gerekçe `sgk.py` docstring'inde.

    🔴 **SGK 96-118 (çalışan listesi + "SGK No") YOKTUR:** spec §5 bu ucu 55-95
    ile sınırlar ve `sgk_no` diye bir kolon İK-1'de yoktur (uydurulmaz).

    İki sayaç `null` sayı ÜRETMEDEN eksiği görünür kılar (WORKFLOW §3):
    `uncomputed_count` ücreti tanımsız satırları, `unknown_rate_count` oran seti
    olmayan tipleri sayar; ikisi de matraha GİRMEZ (fail-closed).
    """

    model_config = ConfigDict(from_attributes=True)

    period_id: uuid.UUID
    year: int
    month: int
    #: SGK 44-47 banner'ı: damga basılmışsa bildirim "gönderildi" sayılır.
    sgk_submitted_at: datetime | None
    #: --- KPI dörtlüsü (SGK 55-58) ---
    declared_personnel_count: int = Field(description="SGK 55 — bildirilen çalışan (4a + 4b)")
    sgk_base_total: BordroTutar = Field(description="SGK 56 — SGK matrahı")
    sgk_premium_total: BordroTutar = Field(description="SGK 57 — SGK primi (işçi + işveren)")
    unemployment_total: BordroTutar = Field(
        description="SGK 58 — işsizlik sigortası (işçi + işveren)"
    )
    #: --- işçi payları (SGK 69-73) ---
    sgk_employee_total: BordroTutar
    unemployment_employee_total: BordroTutar
    income_tax_total: BordroTutar
    stamp_tax_total: BordroTutar
    employee_deduction_total: BordroTutar = Field(description="SGK 73 — toplam işçi kesintisi")
    #: --- işveren payları (SGK 79-82) ---
    sgk_employer_total: BordroTutar
    unemployment_employer_total: BordroTutar
    short_work_total: BordroTutar
    #: SGK 82 — **ÜÇ kalemin tamamı** (spec §7); brüt DAHİL DEĞİLDİR (o BY 90'ın
    #: `total_employer_cost`udur, ayrı bir kavramdır).
    employer_burden_total: BordroTutar
    #: SGK 86-91 — etiket AÇIKÇA "İşçi + İşveren SGK + İşsizlik" (SGK 89): gelir
    #: vergisi/damga (vergi dairesine gider) ve kısa çalışma bu toplamda YOKTUR.
    sgk_payable_total: BordroTutar
    uncomputed_count: int
    unknown_rate_count: int
    #: 🔴 IK3-GV K6 — gelir vergisi ve damga artık ORANDAN değil SATIRDAN gelir
    #: (`payroll_lines.income_tax_amount`). IK3-GV öncesinde hesaplanmış
    #: satırlarda o kolon `NULL`dur: satır matrahta ve PRİM kalemlerinde KALIR
    #: (prim brütten türer, vergiden bağımsızdır) ama vergi/damga toplamlarına
    #: GİRMEZ ve burada GÖRÜNÜR. Satırı tamamen düşürmek, ilgisiz bir eksik
    #: yüzünden ekranın asıl sayısını yok ederdi.
    unknown_tax_count: int


class PayrollSgkSubmitResult(BaseModel):
    """`POST /payroll/periods/{id}/sgk-submit` — YALNIZ damga (spec §1).

    Dış sistem entegrasyonu YOKTUR: ne HTTP isteği, ne kuyruk, ne dosya
    gönderimi. Yanıt bu yüzden bir "gönderim sonucu" değil, damganın ZAMANIDIR.
    """

    period_id: uuid.UUID
    sgk_submitted_at: datetime


# --- T5: oran tablosu (K1) -------------------------------------------------

#: Oran alanlarının şema karşılığı — `Numeric(6,3)` ile AYNI ölçek (models.py).
#: **Üst sınır %100:** bir kalem brütün tamamından fazlasını kesemez. Sınırsız
#: bırakılsaydı bir yazım hatası (%2000) neti eksiye düşürür ve
#: `ck_payroll_lines_net_positive` ihlaliyle `compute` 500'e patlardı —
#: kullanıcı hatası sunucu hatası gibi görünürdü.
Rate = Annotated[Decimal, Field(ge=0, le=100, max_digits=RATE_PRECISION, decimal_places=RATE_SCALE)]

#: `compute.EMPLOYEE_RATE_FIELDS`in şema tarafındaki aynası: neti belirleyen
#: kalemler. İşveren kalemleri KASTEN dışarıdadır — onlar maliyettir, kesinti
#: değil (spec §7) ve toplamları %100'ü aşsa bile net eksiye düşmez.
_EMPLOYEE_RATE_FIELDS = (
    "sgk_employee_pct",
    "unemployment_employee_pct",
    "income_tax_pct",
    "stamp_tax_pct",
)

MAX_TOTAL_PCT = Decimal("100")

#: Oran girdileri mevzuat parametresidir, kişisel veri DEĞİL → `Hassas.yok` (GECE KARARI).
RateGirdisi = Annotated[Rate, Hassas.yok]
RateGirdisiOpsiyonel = Annotated[Rate | None, Hassas.yok]


class PayrollRateUpdate(BaseModel):
    """`PUT /payroll/rates/{year}/{source}` gövdesi — **TAM SET** (K1).

    Yedi oranın hepsi ZORUNLUDUR: kısmi gönderim kabul edilseydi eksik alan
    sessizce 0 olur ve "kesinti yok" yalanı üretilirdi. PUT bir DEĞİŞTİRMEDİR,
    yama değildir; anahtar (`year`, `source`) yoldadır, gövdede TEKRARLANMAZ —
    ikisi çelişirse hangisinin kazandığı sorusu doğardı.
    """

    model_config = ConfigDict(extra="forbid")

    sgk_employee_pct: RateGirdisi
    unemployment_employee_pct: RateGirdisi
    #: 🔴 IK3-GV K3 — `null` = DİLİMLİ MOTOR, dolu = DÜZ ORAN. Alan ZORUNLUDUR
    #: (varsayılanı YOKTUR): `null`ın atlanarak da elde edilebilmesi, kısmi
    #: gönderimin "sessizce 0" olmasıyla aynı sınıf bir yalan üretirdi —
    #: kullanıcı rejim seçimini AÇIKÇA yapar.
    income_tax_pct: RateGirdisiOpsiyonel
    stamp_tax_pct: RateGirdisi
    sgk_employer_pct: RateGirdisi
    unemployment_employer_pct: RateGirdisi
    short_work_pct: RateGirdisi
    #: Eski yılın seti SİLİNMEZ, pasifleştirilir (models.py): geçmiş bordronun
    #: hesabı okunabilir kalmalıdır.
    is_active: bool = True

    @model_validator(mode="after")
    def _isci_paylari_yuzu_asamaz(self) -> "PayrollRateUpdate":
        """🔴 Dört İŞÇİ kaleminin TOPLAMI da %100'ü aşamaz.

        Tek tek geçerli (her biri ≤ %100) ama toplamı %101 olan bir set, brütü
        tanımlı HER personelin netini negatife çevirir ve DB CHECK'ine çarpardı.
        Sınır kalem başına değil TOPLAM üzerinde de durmalıdır.

        🔴 `income_tax_pct` `None` ise (dilimli rejim, K3) toplamdan DÜŞER, 0
        SAYILMAZ: dilimli verginin oranı bir sabit değildir ve onu 0 gibi
        toplamak sınırı gerçekte olduğundan gevşek gösterirdi. Dilimli rejimde
        neti negatife düşüren asıl koruma matrahtadır
        (`compute.employee_deductions` eksi matrahta fail-closed'dur).
        """
        toplam = sum(
            deger for alan in _EMPLOYEE_RATE_FIELDS if (deger := getattr(self, alan)) is not None
        )
        if toplam > MAX_TOTAL_PCT:
            raise ValueError(
                f"İşçi kesinti oranlarının toplamı %100'ü aşamaz (gönderilen: %{toplam})"
            )
        return self


class PayrollRateResponse(BaseModel):
    """Bir oran seti — `(yıl, personel tipi)` anahtarlı (S2)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    year: int
    personnel_source: WorkerSource
    sgk_employee_pct: Oran
    unemployment_employee_pct: Oran
    #: 🔴 K3 — `null` = dilimli motor (`payroll_tax_brackets`), dolu = düz oran.
    income_tax_pct: OranOpsiyonel
    stamp_tax_pct: Oran
    sgk_employer_pct: Oran
    unemployment_employer_pct: Oran
    short_work_pct: Oran
    is_active: bool


class PayrollRateListResponse(BaseModel):
    """Oran setleri — **sayfalama YOKTUR ve bu bilinçlidir.**

    Tablo yılda en çok DÖRT satır büyür (spec §4'ün dört bordro tipi); TB3
    sayfalama korkuluğu sınırsız büyüyen listeler içindir. `limit` eklenseydi
    ekran oran matrisini sayfalamak zorunda kalır ve kullanıcı bir yılın
    setini parça parça görürdü.
    """

    items: list[PayrollRateResponse]
    total: int


# --- TB6 T1: gelir vergisi tarifesi (IK3-GV K2'nin ERTELENMİŞ ucu) ----------

#: Dilimin üst eşiği — `Numeric(14,2)` ile AYNI ölçek (models.py). `> 0`:
#: sıfır/eksi bir üst sınır hiçbir matrahı kapsamaz, dilim ÖLÜ olurdu
#: (`ck_payroll_tax_brackets_upper_bound_positive`in şema karşılığı).
BracketBound = Annotated[
    Decimal, Field(gt=0, max_digits=BRACKET_BOUND_PRECISION, decimal_places=MONEY_SCALE)
]


#: Vergi dilimi üst sınırı = şirket geneli mevzuat parametresi (`Hassas.yok`, GECE KARARI).
BracketSiniriGirdisi = Annotated[BracketBound | None, Hassas.yok]


class PayrollTaxBracketInput(BaseModel):
    """Tarifenin BİR dilimi — gövdedeki hâli.

    `is_active` burada YOKTUR: aktiflik SETİN özelliğidir, dilimin değil. Dilim
    başına bırakılsaydı yarısı aktif bir tarife kurulabilir ve
    `income_tax.normalize_brackets` onu "delikli" sayıp TÜM yılı fail-closed'a
    düşürürdü — üstelik kullanıcı bunu hiçbir uçtan göremezdi.
    """

    model_config = ConfigDict(extra="forbid")

    ordinal: int = Field(ge=1)
    #: 🔴 `null` = **SON dilim** ("üstü"), "sınır girilmedi" DEĞİL. Varsayılanı
    #: `None`dır çünkü son dilimde alanın YOKLUĞU anlamın kendisidir.
    upper_bound: BracketSiniriGirdisi = None
    rate_pct: RateGirdisi


class PayrollTaxBracketSetUpdate(BaseModel):
    """`PUT /payroll/tax-brackets/{year}/{income_kind}` gövdesi — **TAM KÜME**.

    🔴 Kısmi güncelleme YOKTUR ve bu bir tercih değil, ZORUNLULUKTUR: tarife
    birikimli okunur (`tax_for_base` dilimleri baştan tarar), yani tek bir
    dilimi değiştirmek setin BÜTÜNÜNÜN anlamını değiştirir. Beş dilimli bir
    setin 3.'sünü yamalayan bir uç, geri kalan dördünü sessizce eski mevzuatta
    bırakırdı.

    🔴 Küme doğrulaması `income_tax.normalize_brackets`e DEVREDİLİR — ikinci bir
    kopya yazılsaydı hesap motoru ile uç iki farklı "geçerli tarife" tanımına
    sahip olurdu ve uçtan geçen bir set motorda `uncomputed` üretebilirdi.
    """

    model_config = ConfigDict(extra="forbid")

    brackets: list[PayrollTaxBracketInput] = Field(min_length=1)
    #: Setin tamamı için: `false` = o yıl fail-closed (satır `uncomputed`
    #: kalır), "vergi yok" DEĞİL. `payroll_rates`in `is_active`i ile aynı kural.
    is_active: bool = True

    @model_validator(mode="after")
    def _kume_butunlugu(self) -> "PayrollTaxBracketSetUpdate":
        """Boşluk/örtüşme/açık uç denetimi — motorun KENDİ kuralları.

        `TaxBracketSetError` bir `ValueError`dür; Pydantic onu 422'ye çevirir ve
        kullanıcı hangi kuralın kırıldığını METİNDEN okur.
        """
        income_tax.normalize_brackets(
            [
                income_tax.TaxBracket(
                    ordinal=dilim.ordinal, upper_bound=dilim.upper_bound, rate_pct=dilim.rate_pct
                )
                for dilim in self.brackets
            ]
        )
        return self


class PayrollTaxBracketResponse(BaseModel):
    """Bir dilim — `(yıl, gelir türü, sıra)` anahtarlı (models.py)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    year: int
    income_kind: IncomeKind
    ordinal: int
    #: 🔴 `null` = son dilim ("üstü") — 0 ile KARIŞTIRILMAZ.
    upper_bound: OranOpsiyonel
    rate_pct: Oran
    is_active: bool


class PayrollTaxBracketListResponse(BaseModel):
    """Dilimler — **sayfalama YOKTUR ve bu bilinçlidir** (`PayrollRateListResponse` emsali).

    Tablo yılda en çok bir avuç satır büyür (2026 ücret tarifesi BEŞ dilim);
    TB3 sayfalama korkuluğu sınırsız büyüyen listeler içindir. `limit`
    eklenseydi kullanıcı bir yılın tarifesini PARÇA PARÇA görür ve setin
    bütünlüğünü (deliği olup olmadığını) ekrandan hiç okuyamazdı.
    """

    items: list[PayrollTaxBracketResponse]
    total: int

"""Puantaj şemaları — **saat bazlı** (PUAN-SAAT) · mockup `Ekran 5 - Puantaj.dc.html`.

Alanların TAMAMI mockup'tan gelir ve satır numarasıyla gerekçelidir (WORKFLOW §3):

| Alan | Kaynak |
|---|---|
| `cells[].hours` | E5 236 `<input type="number" value="9">` |
| `cells[].code` | E5 262 "İzin" · E5 283 "Görev" rozetleri |
| `normal_hours` / `overtime_hours` / `total_hours` | E5 225-227 satır kolonları |
| `day_totals[].total_hours` | E5 320-326 "Günlük Toplam" |
| `week.*_hours` | E5 328-330 tfoot + E5 179-198 KPI kartları |
| `month_weeks[]` | E5 141-176 ay içi hafta şeridi |
| `month_total_hours` / `month_man_days` | E5 171-174 · E5 347-350 "588 saat · 65,3 adam/gün" |
| `full_name` / `trade` / `source` / `subcontractor_name` | E5 233-234 kişi hücresi |

**Toplam kolonu AÇILMAZ** (spec §2): buradaki her toplam TÜREVDİR, her istekte
hücrelerden hesaplanır. FM ÖZELLİKLE saklanmaz — `hours.week_totals` üretir.

**Onay/durum alanı YOKTUR** (spec §7 S3): mockup'ta yalnız "Haftayı Kaydet"
vardır (E5 76); `status`/`submitted_at` benzeri bir alan bu şemalara EKLENMEZ.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.field_mask import Hassas
from app.modules.site_diary.models import WorkerSource
from app.modules.timesheet.models import TimesheetCode

# 🔴 GECE KARARI (IZN-B4c, timesheet): puantaj bir SAAT kaydıdır, tutar değil. Saat, adam-gün
# (saat ÷ 9), normal/FM/toplam saat, haftalık normal saat ve ay toplamları = `Hassas.yok` (rol
# bayrağı ne olursa olsun GÖRÜNÜR). Modülde ücret/yevmiye/işçilik maliyeti, TC/telefon/IBAN alanı
# YOKTUR (ücret bordroda, kişisel veri personelde) → gizlenecek alan yok; işçi adı/meslek/taşeron
# firma GİZLENMEZ.
Saat = Annotated[Decimal, Hassas.yok]
SaatOpsiyonel = Annotated[Decimal | None, Hassas.yok]


class TimesheetCell(BaseModel):
    """Tek gün hücresi — **saat VEYA kod**. Girilmemiş gün hücre ÜRETMEZ.

    Ayın/haftanın her günü için boş hücre üretmek gövdeyi çoğunlukla `null` ile
    doldururdu; ekran gün iskeletini `day_totals`ten alır.
    """

    work_date: date
    hours: SaatOpsiyonel
    code: TimesheetCode | None
    section_id: uuid.UUID | None


class TimesheetRowTotals(BaseModel):
    """Bir satırın haftalık türevleri (E5 225-227 · kural `hours.week_totals`)."""

    normal_hours: Saat
    overtime_hours: Saat
    total_hours: Saat


# --- Aylık matris (arşiv/Excel yüzeyi) ---


class TimesheetMatrixRow(BaseModel):
    """Bir personelin AY satırı.

    `man_days` artık **TÜREVDİR ve ondalıklıdır**: `toplam saat ÷ 9`
    (E5 349-350 "588 saat · 65,3 adam/gün"). Gün SAYMAZ — yarım günün
    temsil edilebildiği bir dünyada gün saymak 4 saatlik günü tam gün gösterirdi.
    """

    personnel_id: uuid.UUID
    full_name: str
    trade: str | None
    source: WorkerSource
    # Meslek ile firma AYRI alanlardır: E5 234'teki "Demir Ustası · Akın İnş."
    # birleştirmesi bir SUNUM kararıdır, backend iki bilgiyi yapıştırmaz.
    subcontractor_name: str | None
    total_hours: Saat
    man_days: Saat
    cells: list[TimesheetCell]


class TimesheetDayTotal(BaseModel):
    """Bir gün sütununun ayak satırı (E5 320-326).

    `total_hours` o günün girilmiş saat toplamıdır. `worked_day_count` SAATLİ
    hücreleri sayar (kodlu hücre çalışılmış değildir); `leave_count` ve
    `temporary_duty_count` ayrı sayaçlardır — bir gün hem izinli hem çalışılmış
    sayılmaz.
    """

    work_date: date
    total_hours: Saat
    worked_day_count: int
    leave_count: int
    temporary_duty_count: int


class TimesheetMatrix(BaseModel):
    """Aylık matris — Excel çıktısının ve arşiv okumasının zarfı."""

    site_id: uuid.UUID
    site_name: str
    project_id: uuid.UUID
    project_name: str
    year: int
    month: int
    # Bölüm süzgeci. Seçilmemişse şerit bir bölüm adı İDDİA ETMEZ.
    section_id: uuid.UUID | None
    section_name: str | None
    worker_count: int
    total_hours: Saat
    #: `total_hours ÷ 9` — satır adam-günlerinin toplamı DEĞİL saatten türer
    #: (satır bazında yuvarlanıp toplansaydı ekranın iki yeri farklı sayı gösterirdi).
    total_man_days: Saat
    rows: list[TimesheetMatrixRow]
    day_totals: list[TimesheetDayTotal]


# --- Haftalık ekran (E5'in kendisi) ---


class TimesheetWeekRow(BaseModel):
    """Bir personelin HAFTA satırı: 7 gün + Normal/FM/Toplam (E5 230-249)."""

    personnel_id: uuid.UUID
    full_name: str
    trade: str | None
    source: WorkerSource
    subcontractor_name: str | None
    cells: list[TimesheetCell]
    totals: TimesheetRowTotals


class TimesheetWeekSummary(BaseModel):
    """Ay içi hafta şeridinin bir kutusu (E5 143-176).

    `has_entries` "girilmedi" rozetini (E5 163) belirler ve `total_hours == 0`
    ile AYNI ŞEY DEĞİLDİR: hepsi izinli geçmiş bir hafta girilmiştir ve 0 saattir.
    """

    iso_year: int
    iso_week: int
    start_date: date
    end_date: date
    total_hours: Saat
    has_entries: bool


class TimesheetDayLock(BaseModel):
    day: date
    report_date: date | None


class TimesheetWeek(BaseModel):
    """E5 ekranının tamamı: hafta şeridi + KPI + 7 günlük ızgara + tfoot."""

    site_id: uuid.UUID
    site_name: str
    project_id: uuid.UUID
    project_name: str
    iso_year: int
    iso_week: int
    start_date: date
    end_date: date
    section_id: uuid.UUID | None
    section_name: str | None
    #: E5 71 başlığı ekranda YAZILIDIR; istemci sabiti kendi yazmasın diye yayınlanır.
    normal_day_hours: Saat
    weekly_normal_hours: Saat
    worker_count: int
    #: E5 179-198 KPI kartları + E5 328-330 tfoot — TEK kaynak.
    totals: TimesheetRowTotals
    #: E5 191-194 "İzin 27 saat · 3 gün". ⚠️ Mockup o kartta geçici görevi de
    #: izne katıyor (2 izin + 1 görev = "3 gün"); backend İKİSİNİ AYIRIR ve
    #: uydurma toplama YAPMAZ (kusur raporlandı).
    leave_day_count: int
    temporary_duty_day_count: int
    rows: list[TimesheetWeekRow]
    day_totals: list[TimesheetDayTotal]
    #: PLN-B2.x-B: rapor onayiyla KILITLI gunler (`app.core.day_hooks` portu; EV kurulu
    #: degilse bos). Kilitli gunde hucre yazmasi 409 — istemci hucreyi salt okunur basar.
    locked_days: list[date] = Field(default_factory=list)
    #: EV-BORC-4: kilitli gun + kilidi koyan rapor tarihi ("25.09.2026 raporuyla kilitli");
    #: gunler ardisik olmayabilir, farkli raporlara bagli olabilir. `locked_days` = gun kolonu.
    day_locks: list[TimesheetDayLock] = Field(default_factory=list)
    #: Ay şeridi (E5 137-176). Haftanın İÇİNDE bulunduğu takvim ayıdır.
    month_year: int
    month_month: int
    month_total_hours: Saat
    month_man_days: Saat
    month_weeks: list[TimesheetWeekSummary]


# --- Yazma ---


class TimesheetCellInput(BaseModel):
    """`PUT` gövdesinin tek hücresi — **saat XOR kod**.

    `project_id` gövdede YOKTUR: kapsam alanı şantiyeden KOPYALANIR, istemciden
    alınsaydı görünür bir şantiyeye görünmez bir projenin hücresi yazılabilirdi.
    """

    model_config = ConfigDict(extra="forbid")

    personnel_id: uuid.UUID
    work_date: date
    # Aralık DB CHECK'i (`0 < saat <= 24`) ile BİREBİR aynı.
    hours: SaatOpsiyonel = Field(default=None, gt=0, le=24, decimal_places=1)
    code: TimesheetCode | None = None
    section_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _hours_xor_code(self) -> "TimesheetCellInput":
        """Şema kapısı DB CHECK'inin (`ck_timesheet_entries_hours_xor_code`)
        İKİZİDİR, YERİNE GEÇMEZ: burası kullanıcıya 422 ile hangi hücrenin bozuk
        olduğunu söyler, DB ise kuralı her yazma yolunda (migration, elle SQL,
        gelecekteki ikinci bir uç) zorlar. Biri düşerse öteki ayakta kalır."""
        if (self.hours is None) == (self.code is None):
            raise ValueError(
                "Puantaj hücresi ya çalışılan saati ya da bir kodu (izin/tatil/görev) "
                "taşımalıdır; ikisi birden ya da hiçbiri gönderilemez."
            )
        return self


class TimesheetWeekSave(BaseModel):
    """⚠️ **DEĞİŞTİRME semantiği**: gövde **HAFTA**+şantiye kapsamının TAM kümesidir;

    gövdede geçmeyen hücre SİLİNİR. Mockup'ta tek "Haftayı Kaydet" düğmesi
    vardır (E5 76) — ekran haftayı bütün olarak kaydeder.

    🔴 Kapsam **AY DEĞİL HAFTADIR.** Ay kapsamlı bir silme, bir haftayı
    kaydetmeyi ayın geri kalanını süpüren bir işleme çevirirdi (bekçi:
    `tests/timesheet/test_week_save.py::test_hafta_kaydetmek_ayin_diger_haftasina_DOKUNMAZ`).
    """

    model_config = ConfigDict(extra="forbid")

    cells: list[TimesheetCellInput] = Field(default_factory=list)

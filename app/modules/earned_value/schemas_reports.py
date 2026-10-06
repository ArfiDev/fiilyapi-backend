"""Rapor semalari: haftalik QURR (Q) · gunluk ilerleme raporu (GIR) · panel (PNL) · onay.

Kaynak: PLANLAMA-SPEC Ek A §4.1–§4.3, §3.7–§3.13; frontend veri ihtiyaci F0 §4.3.
Sayilar EvDecimal (JSON'da SABIT gosterimli string, ustel yok — decimal_out); yuzde/oran
0–1 kesir; yuvarlama yalniz sunumda.
Bant/durum karari HAM degerle degil GOSTERILEN degerle (K18, K27) — motor verir.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.modules.earned_value.engine import ContractorType, PfBand, RowKind, Status
from app.modules.earned_value.mask_types import Yok, YokOpt


class UserRef(BaseModel):
    id: uuid.UUID
    full_name: str


class RevisionRef(BaseModel):
    id: uuid.UUID
    number: int
    name: str | None
    frozen_at: datetime | None


class WarningOut(BaseModel):
    """Uyari + hedef tipi (istemci baglantiyi hedefe gore kurar)."""

    code: Literal[
        "pf_out_of_band",
        "undistributed_hours",
        "qty_overrun",
        "missing_diary",
        "draft_diary",
        "unrated_entry",
        "unknown_line",
        "empty_rate",  # EV-BORC-3 G3: donmus baseline'da orani BOS yaprak (girisden bagimsiz)
    ]
    message: str
    target: Literal["node", "day", "leaf"]
    target_id: str | None  # dugum kimligi ya da ISO gun
    value: YokOpt = None
    # EV-BORC-3 G7: yalniz YAPRAK hedefli uyarilarda dolar (ekran metni ayristirmasin)
    item_name: str | None = None
    section_name: str | None = None  # None = Bolumsuz
    uom: str | None = None
    # §3.15 S9: yalniz `qty_overrun`da (asim = qty_cum − planned_qty = `value`)
    qty_cum: YokOpt = None
    planned_qty: YokOpt = None


class PfBandOut(BaseModel):
    red_below: Yok
    green_from: Yok
    high_above: YokOpt


class PfBandsOut(BaseModel):
    """EV-BORC-3 G2: raporun KULLANDIGI PF esikleri. Haftalik PF kumulatif esigini kullanir
    (motor: `pf_week_band` = cumulative). Gunluk raporda SNAPSHOT'a girer."""

    daily: PfBandOut
    cumulative: PfBandOut


# ------------------------------------------------------------------ QURR


class QurrRow(BaseModel):
    """L3 (is tipi) satiri — Ek A §4.2 a–r kolonlari."""

    node_id: str
    level: int
    code: str | None
    name: str
    uom: str | None
    contractor_type: ContractorType | None
    is_direct: bool | None
    parent_id: str | None = None  # §3.15 S1: grup dugumu (g:…) — deterministik agac
    a_prev_qty: YokOpt
    b_qty: YokOpt
    c_qty_cum: YokOpt
    d_remaining_qty: YokOpt
    e_qty_week: YokOpt
    f_prev_budget_mhr: YokOpt
    g_budget_mhr: Yok
    h_earned_cum: Yok
    i_spent_cum: Yok
    j_remaining_mhr: Yok  # K26: remaining_qty × unit_mhr (togo)
    k_earned_week: Yok
    l_spent_week: Yok
    m_prev_unit_mhr: YokOpt
    n_unit_mhr: YokOpt
    o_actual_unit_mhr_cum: YokOpt
    p_actual_unit_mhr_week: YokOpt
    q_pf_cum: YokOpt
    r_pf_week: YokOpt
    q_band: PfBand | None
    r_band: PfBand | None
    changed_qty: bool  # a ≠ b
    changed_rate: bool  # m ≠ n
    changed_budget: bool  # f ≠ g


class QurrTotal(BaseModel):
    """Ara toplam / Σ direct / Σ direct+non-direct — f–l + q, r."""

    kind: Literal["discipline", "group", "direct_total", "all_total"]
    node_id: str | None
    name: str
    parent_id: str | None = None  # §3.15 S1: grup → disiplin (d:…); disiplin/toplam → None
    code: str | None = None  # S2: grup konum no / disiplin kodu
    contractor_mix: str | None = None  # S2: own | subcon | mixed (direct yapraklardan, S7)
    q_band: PfBand | None = None  # S2: kumulatif esik (motor kurali, K18)
    r_band: PfBand | None = None
    f_prev_budget_mhr: YokOpt
    g_budget_mhr: Yok
    h_earned_cum: Yok
    i_spent_cum: Yok
    j_remaining_mhr: Yok
    k_earned_week: Yok
    l_spent_week: Yok
    q_pf_cum: YokOpt
    r_pf_week: YokOpt


class KpiPf(BaseModel):
    scope: Literal["overall_own", "overall_subcon"]
    pf_cum: YokOpt
    pf_week: YokOpt
    pf_cum_band: PfBand | None
    pf_week_band: PfBand | None


class CompositeCard(BaseModel):
    id: uuid.UUID
    name: str
    measure: Literal["spent", "earned", "budget"]
    unit: str | None  # payda is tipinin birimi
    actual: YokOpt
    planned: YokOpt  # B3-3: pay butcesi ÷ payda planli miktar
    deviation: YokOpt  # (gercek − planli) ÷ planli
    # EV-BORC-3 G5: tanim PARCALARI (bicimi frontend kurar); baseline'da olmayan kalem yazilmaz
    numerator_names: list[str] = Field(default_factory=list)
    denominator_name: str | None = None


class QurrReport(BaseModel):
    week_no: int
    week_start: date
    week_end: date
    report_date: date  # = min(hafta sonu, bugun, takvim sonu) (B3-2)
    revision: RevisionRef
    previous_revision: RevisionRef | None
    draft_diary_dates: list[date]
    rows: list[QurrRow]
    totals: list[QurrTotal]  # sira: disiplin/grup ara toplamlari, sonra direct, sonra all
    kpis: list[KpiPf]
    composites: list[CompositeCard]
    warnings: list[WarningOut]
    generated_at: datetime | None = None  # EV-BORC-3 G4
    has_field_data: bool | None = None  # §3.15 S7: rapor gunune kadar miktar/saat girisi var mi
    pf_bands: PfBandsOut | None = None  # G2
    calendar_start: date | None = None  # G6: gezgin sinirlari
    calendar_end: date | None = None
    last_week_no: int | None = None


# ------------------------------------------------------------------ GIR / PNL


class KpiRowOut(BaseModel):
    kind: RowKind
    node_id: str | None
    name: str | None
    contractor_mix: str | None
    budget_mhr: Yok
    earned_day: Yok
    earned_cum: Yok
    spent_day: Yok
    spent_cum: Yok
    progress_pct_day: YokOpt
    progress_pct_cum: YokOpt
    planned_pct_day: YokOpt
    planned_pct_cum: YokOpt
    variance: YokOpt
    status: Status | None
    pf_day: YokOpt
    pf_cum: YokOpt
    pf_week: YokOpt
    pf_day_band: PfBand | None
    pf_cum_band: PfBand | None
    pf_week_band: PfBand | None


class TrendPoint(BaseModel):
    day: date
    is_holiday: bool
    is_draft: bool
    is_future: bool
    planned_pct_cum: YokOpt
    progress_pct_cum: YokOpt
    delta: YokOpt
    earned_day: YokOpt
    spent_day: YokOpt
    pf_day: YokOpt
    pf_rolling: YokOpt


class QtyTreeRow(BaseModel):
    node_id: str
    level: int
    name: str
    uom: str | None
    contractor_type: ContractorType | None
    is_direct: bool | None
    planned_unit_mhr: YokOpt
    actual_unit_mhr_day: YokOpt
    actual_unit_mhr_cum: YokOpt
    planned_qty: YokOpt
    qty_day: YokOpt
    qty_cum: YokOpt
    remaining_qty: YokOpt
    pf_day: YokOpt
    pf_day_band: PfBand | None
    spent_day: Yok
    progress_pct_cum: YokOpt
    pf_cum: YokOpt = None  # §3.15 S4
    pf_cum_band: PfBand | None = None


class WeatherDay(BaseModel):
    day: date
    condition: str | None
    temp_min_c: YokOpt
    temp_max_c: YokOpt
    wind_ms: YokOpt


class DailyFooter(BaseModel):
    spent_total_day: Yok
    timesheet_total_day: Yok  # puantaj + taseron kaynak saati (§4 mutabakat)
    undistributed_day: Yok  # K14 "dagitilmamis saat" = kaynak − dagitilan
    undistributed_reason: str | None
    unallocated_day: Yok  # K14 "atanamayan saat" (§3.3 prorata miktarsiz)


class DailyReport(BaseModel):
    status: Literal["not_generated", "draft", "approved"]
    report_date: date
    report_no: int | None  # K23 = proje gun no
    version: int | None  # onayli surum (B3-5)
    day_no: int | None
    week_no: int | None
    week_start: date | None
    week_end: date | None
    project_start: date | None
    revision: RevisionRef | None
    generated_at: datetime
    approved_at: datetime | None
    approved_by: UserRef | None
    missing_diary_dates: list[date]
    draft_diary_dates: list[date]
    tolerance_points: YokOpt
    weather: list[WeatherDay]
    kpis: list[KpiRowOut]
    trend: list[TrendPoint]
    quantities: list[QtyTreeRow]
    footer: DailyFooter | None
    unrated_entries: list[WarningOut]
    warnings: list[WarningOut]
    # EV-BORC-3 — varsayilanli: eski onayli snapshot'lar bu alanlar olmadan da dogrulanir
    pf_bands: PfBandsOut | None = None  # G2 (snapshot'a girer: onayli rapor KENDI esigiyle)
    calendar_start: date | None = None  # G6
    calendar_end: date | None = None


class ApprovalResult(BaseModel):
    report: DailyReport
    missing_diary_dates: list[date]  # onay modalinda da gorunur (B3-1)


# ------------------------------------------------------------------ PANEL (PNL)


class PanelKpi(BaseModel):
    budget_mhr: Yok
    earned_day: YokOpt
    earned_cum: YokOpt
    spent_day: YokOpt
    progress_pct_cum: YokOpt
    planned_pct_cum: YokOpt
    variance: YokOpt
    status: Status | None
    pf_cum: YokOpt
    pf_week: YokOpt
    pf_cum_band: PfBand | None
    pf_week_band: PfBand | None
    timesheet_total_day: YokOpt  # puantaj + taseron (filtresiz — kisi disipline atanmaz)
    undistributed_day: YokOpt  # K14 "dagitilmamis saat" (filtresiz)


class CurvePoint(BaseModel):
    day: date
    is_future: bool
    planned_pct_cum: YokOpt
    progress_pct_cum: YokOpt
    variance: YokOpt = None  # §3.15 S22: motor (tek kaynak)
    status: Status | None = None  # K27: gosterilen puanla karar


class BarPoint(BaseModel):
    day: date
    is_holiday: bool
    diary_status: Literal["none", "draft", "submitted"]
    earned_day: YokOpt
    spent_day: YokOpt


class PfPoint(BaseModel):
    day: date
    pf_day: YokOpt
    pf_rolling: YokOpt  # B3-4: son 7 is gunu Σearned/Σspent


class HistogramWeek(BaseModel):
    week_start: date
    week_no: int | None = None  # §3.15 S5
    week_end: date | None = None  # takvimle kirpilmis
    working_days: int
    is_future: bool  # hafta d'den sonra baslar → gerceklesen yok
    planned_people: YokOpt  # planli a-s ÷ (is gunu × standart saat)
    actual_people: YokOpt  # basis'e gore: kisi SAYIMI ya da ESDEGER kisi (is gunu ort.)


class PanelRow(BaseModel):
    scope: Literal[
        "overall",
        "overall_own",
        "overall_subcon",
        "discipline",
        "discipline_own",
        "discipline_subcon",
        "non_direct",
        "item",
    ]
    node_id: str | None
    parent_id: str | None  # item satirinda disiplin kokü
    name: str
    uom: str | None
    contractor_type: ContractorType | None
    contractor_mix: str | None
    budget_mhr: Yok
    earned_cum: YokOpt
    spent_cum: YokOpt
    planned_pct_cum: YokOpt
    progress_pct_cum: YokOpt
    variance: YokOpt
    status: Status | None
    pf_cum: YokOpt
    pf_week: YokOpt
    pf_cum_band: PfBand | None
    pf_week_band: PfBand | None
    #: EV-BORC-9 (spec S32, mockup "Planlama - Panel"): yalnız `non_direct` satırında dolu —
    #: dolaylı (is_direct=False) kalemlerin adları, BOQ sırasında, ilk görünüşle tekilleştirilmiş.
    indirect_item_names: list[str] = Field(default_factory=list)


class PanelDiscipline(BaseModel):
    """EV-BORC-3 G1: filtre acilir listesi — `discipline_id` filtresinden BAGIMSIZ, tum kokler."""

    id: str
    name: str
    contractor_mix: str | None  # own | subcon | mixed (S7 rozeti)


class PanelReport(BaseModel):
    day: date
    range: Literal["4w", "3m", "all"]
    discipline_id: str | None
    contractor_type: ContractorType | None
    has_baseline: bool
    has_field_data: bool
    day_no: int | None
    week_no: int | None
    week_start: date | None
    week_end: date | None
    revision: RevisionRef | None
    tolerance_points: YokOpt
    kpi: PanelKpi | None
    s_curve: list[CurvePoint]
    bars: list[BarPoint]
    pf_trend: list[PfPoint]
    histogram: list[HistogramWeek]
    # headcount = puantaj kisi + gunluk taseron kisi (kendi/taseron filtresi uygulanir) ·
    # equivalent = filtreli harcanan ÷ (is gunu × standart saat) — DISIPLIN filtresinde
    actual_basis: Literal["headcount", "equivalent"]
    standard_daily_hours: YokOpt
    rows: list[PanelRow]
    warnings: list[WarningOut]
    disciplines: list[PanelDiscipline] = Field(default_factory=list)  # EV-BORC-3 G1
    pf_bands: PfBandsOut | None = None  # G2
    calendar_start: date | None = None  # G6
    calendar_end: date | None = None


# ------------------------------------------------------------------ AYP canli onizleme


class CompositeValueOut(BaseModel):
    """Duzenlenen (kaydedilmemis) pacal metrigin canli degeri — B3-3 formulu."""

    unit: str | None
    actual: YokOpt
    planned: YokOpt
    deviation: YokOpt


class SettingsPreview(BaseModel):
    """AYP ekrani canli degerleri (KAYITLI ayarla). Istemci taslak tolerans/bantla yeniden
    siniflar: durum `variance_points`a (K27), bant PF'nin 2 ondaligina (K18) bakar."""

    day: date
    has_baseline: bool
    day_no: int | None
    week_no: int | None
    week_start: date | None
    week_end: date | None
    variance: YokOpt  # Overall (yalniz direct)
    variance_points: YokOpt  # K27: round_half_up(variance × 100, 1)
    status: Status | None
    pf_day: YokOpt
    pf_week: YokOpt
    pf_day_band: PfBand | None
    pf_week_band: PfBand | None
    composites: list[CompositeCard]

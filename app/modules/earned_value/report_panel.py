"""Planlama PANELI (PNL; Ek A §4.3, F0 §4.3; spec §3.13) — filtre BUTUN panele uygulanir.

* TEK motor cagrisi `compute_panel` (tek agac, tek saat inisi): grafikler + filtreli KPI =
  seri (pencerelerin birlesimi; as_of=d) · disiplin tablosu + is tipi satirlari = d gunu
  raporu (KPI satirlari S7 mix'iyle, dugum metrikleri). Birebirlik bekcileri
  `test_engine_series.py` + `test_engine_panel.py`.
* Pencereler (mockup birebir, CEO onayi): S-egrisi 4w = d−27..d+28 · 3m = d−83..d+42 · all =
  takvim · cubuk = son 28 gun · PF trendi 28 / 84 gun / takvim basi..d · histogram
  4w = H−3..H+2 · 3m = H−12..H+4 · all = tum haftalar. Hepsi takvimle kirpilir.
* Histogram: planli kisi = Σ(butce × planli_%_gun) ÷ (is gunu × standart saat); gerceklesen =
  kisi SAYIMI (kendi/taseron filtresi uygulanir) ya da DISIPLIN filtresinde ESDEGER kisi
  (filtreli harcanan ÷ (gecen is gunu × standart saat)) — `actual_basis`.
* Uyarilar santiye geneli (dugum hedefli olanlar dahil; istemci filtreye gore suzebilir).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import UNRESTRICTED, DisciplineScope
from app.core.errors import NotFoundError
from app.modules.earned_value import budget_repository as repo
from app.modules.earned_value import report_warnings as rw
from app.modules.earned_value.engine import (
    ContractorMix,
    ContractorType,
    ProjectCalendar,
    RowKind,
    Scope,
    ScopeSeries,
    classify_status,
    compute_panel,
    pf_band,
)
from app.modules.earned_value.engine import DailyReport as EngineReport
from app.modules.earned_value.ev_input import SiteInput, build_site_input
from app.modules.earned_value.panel_headcount import daily_headcount
from app.modules.earned_value.report_daily import day_footer
from app.modules.earned_value.report_qurr import pf_bands_out, revision_ref, week_range
from app.modules.earned_value.schemas_reports import (
    BarPoint,
    CurvePoint,
    HistogramWeek,
    PanelDiscipline,
    PanelKpi,
    PanelReport,
    PanelRow,
    PfPoint,
)
from app.modules.site_diary.models import DiaryStatus

Range = Literal["4w", "3m", "all"]
ZERO = Decimal(0)
NO_DISCIPLINE = "Disiplin bu şantiyenin aktif baseline'ında yok"
# EV-BORC-9 (spec S32, kullanıcı 2026-09-26): mockup "Planlama - Panel" son satırının adı
# BİREBİR (Ö:542 civarı). Tek yer — değiştirilecekse yalnız burası.
NON_DIRECT_ROW_NAME = "Genel / Dolaylı · bütçe dışı"
BAR_DAYS = 28
# range → (S-egrisi geri, ileri gun) · PF trendi gun · histogram (geri, ileri hafta)
_CURVE = {"4w": (27, 28), "3m": (83, 42)}
_PF_DAYS = {"4w": 28, "3m": 84}
_HIST = {"4w": (3, 2), "3m": (12, 4)}
_SPLIT = {
    ContractorType.OWN: RowKind.DISCIPLINE_OWN,
    ContractorType.SUBCON: RowKind.DISCIPLINE_SUBCON,
}


@dataclass(frozen=True, slots=True)
class PanelQuery:
    day: date
    range: Range
    discipline_id: str | None
    contractor: ContractorType | None


@dataclass(frozen=True, slots=True)
class _Windows:
    curve: tuple[date, date]
    bars: tuple[date, date]
    pf: tuple[date, date]
    weeks: list[tuple[date, date]]

    @property
    def span(self) -> tuple[date, date]:
        starts = [self.curve[0], self.bars[0], self.pf[0], *(w[0] for w in self.weeks)]
        ends = [self.curve[1], self.bars[1], self.pf[1], *(w[1] for w in self.weeks)]
        return min(starts), max(ends)


def _clip(cal: ProjectCalendar, a: date, b: date) -> tuple[date, date]:
    return max(a, cal.start_date), min(b, cal.end_date)


def windows(cal: ProjectCalendar, day: date, rng: Range) -> _Windows:
    week = cal.week_no(day)
    if rng == "all":
        curve = (cal.start_date, cal.end_date)
        pf = (cal.start_date, day)
        first, last = 1, cal.week_no(cal.end_date)
    else:
        back, ahead = _CURVE[rng]
        curve = _clip(cal, day - timedelta(days=back), day + timedelta(days=ahead))
        pf = _clip(cal, day - timedelta(days=_PF_DAYS[rng] - 1), day)
        wb, wa = _HIST[rng]
        first, last = max(1, week - wb), min(week + wa, cal.week_no(cal.end_date))
    weeks = [w for n in range(first, last + 1) if (w := week_range(cal, n)) is not None]
    bars = _clip(cal, day - timedelta(days=BAR_DAYS - 1), day)
    return _Windows(curve, bars, pf, [_clip(cal, *w) for w in weeks])


def _pf(earned: Decimal, spent: Decimal) -> Decimal | None:
    return earned / spent if spent else None


def _week_sums(s: ScopeSeries, start: date, end: date) -> tuple[Decimal, Decimal]:
    pts = [p for p in s.points if start <= p.day <= end and not p.is_future]
    return (
        sum((p.earned_day or ZERO for p in pts), ZERO),
        sum((p.spent_day or ZERO for p in pts), ZERO),
    )


def _series_row(
    site: SiteInput,
    s: ScopeSeries,
    day: date,
    week_start: date,
    scope: str,
    name: str,
    indirect_item_names: list[str] | None = None,
) -> PanelRow:
    p = s.at(day)
    pf_week = _pf(*_week_sums(s, week_start, day))
    bands = site.inp.cumulative_pf_bands
    return PanelRow(
        scope=scope,  # type: ignore[arg-type]
        node_id=s.scope.root,
        parent_id=None,
        name=name,
        uom=None,
        contractor_type=s.scope.contractor,
        contractor_mix=None,
        budget_mhr=s.budget_mhr,
        earned_cum=p.earned_cum,
        spent_cum=p.spent_cum,
        planned_pct_cum=p.planned_pct_cum,
        progress_pct_cum=p.progress_pct_cum,
        variance=p.variance,
        status=p.status,
        pf_cum=p.pf_cum,
        pf_week=pf_week,
        pf_cum_band=pf_band(p.pf_cum, bands),
        pf_week_band=pf_band(pf_week, bands),
        indirect_item_names=indirect_item_names or [],
    )


def _indirect_item_names(site: SiteInput, contractor: ContractorType | None) -> list[str]:
    """EV-BORC-9: `non_direct` satırının alt etiketi — dolaylı kalem adları.

    Ağacın MEVCUT sıralı gezinmesi (`site.tree` disiplin → grup → kalem; bkz.
    `_discipline_rows`/`_item_rows`); ikinci bir sıralama anahtarı YOK. Satırın kendi
    kapsamıyla (contractor filtresi dahil) tutarlı — `main`/`non_direct` scope'unun
    kullandığı AYNI `contractor` parametresiyle süzülür. Ad tekrarında ilk görünüş korunur.
    """
    seen: set[str] = set()
    names: list[str] = []
    for d in site.tree.disciplines:
        for g in d.groups:
            for i in g.items:
                if i.is_direct:
                    continue
                if contractor is not None and i.contractor_type is not contractor:
                    continue
                if i.description in seen:
                    continue
                seen.add(i.description)
                names.append(i.description)
    return names


def _kpi(site: SiteInput, s: ScopeSeries, day: date, week_start: date, footer) -> PanelKpi:  # noqa: ANN001
    row = _series_row(site, s, day, week_start, "overall", "")
    p = s.at(day)
    return PanelKpi(
        budget_mhr=s.budget_mhr,
        earned_day=p.earned_day,
        earned_cum=p.earned_cum,
        spent_day=p.spent_day,
        progress_pct_cum=p.progress_pct_cum,
        planned_pct_cum=p.planned_pct_cum,
        variance=p.variance,
        status=p.status,
        pf_cum=p.pf_cum,
        pf_week=row.pf_week,
        pf_cum_band=row.pf_cum_band,
        pf_week_band=row.pf_week_band,
        timesheet_total_day=footer.timesheet_total_day,
        undistributed_day=footer.undistributed_day,
    )


def _report_row(r, name: str, kind: RowKind | None = None) -> PanelRow:  # noqa: ANN001
    return PanelRow(
        scope=(kind or r.kind).value,  # type: ignore[arg-type]
        node_id=r.node_id,
        parent_id=None,
        name=name,
        uom=None,
        contractor_type=None,
        contractor_mix=r.contractor_mix.value if r.contractor_mix else None,
        budget_mhr=r.budget_mhr,
        earned_cum=r.earned_cum,
        spent_cum=r.spent_cum,
        planned_pct_cum=r.planned_pct_cum,
        progress_pct_cum=r.progress_pct_cum,
        variance=r.variance,
        status=r.status,
        pf_cum=r.pf_cum,
        pf_week=r.pf_week,
        pf_cum_band=r.pf_cum_band,
        pf_week_band=r.pf_week_band,
    )


def _discipline_rows(site: SiteInput, report: EngineReport, q: PanelQuery) -> list[PanelRow]:
    out: list[PanelRow] = []
    for d in site.tree.disciplines:
        if q.discipline_id is not None and d.id != q.discipline_id:
            continue
        name = d.name or "Disiplinsiz"
        try:
            head = report.row(RowKind.DISCIPLINE, d.id)
        except KeyError:
            continue
        mix = head.contractor_mix
        split = mix is ContractorMix.MIXED
        if q.contractor is None:
            out.append(_report_row(head, name))
            if split:
                out += [_report_row(report.row(k, d.id), name) for k in _SPLIT.values()]
        elif split:
            out.append(_report_row(report.row(_SPLIT[q.contractor], d.id), name))
        elif mix is not None and mix.value == q.contractor.value:
            out.append(_report_row(head, name))
        else:
            continue
        out += _item_rows(site, report, d, q)
    return out


def _item_rows(site: SiteInput, report: EngineReport, disc, q: PanelQuery) -> list[PanelRow]:  # noqa: ANN001
    out = []
    tol = site.inp.tolerance_points
    for g in disc.groups:
        for i in g.items:
            m = report.nodes.get(i.id)
            if m is None or (q.contractor is not None and i.contractor_type is not q.contractor):
                continue
            variance = (
                m.progress_pct_cum - m.planned_pct_cum
                if m.progress_pct_cum is not None and m.planned_pct_cum is not None
                else None
            )
            out.append(
                PanelRow(
                    scope="item",
                    node_id=i.id,
                    parent_id=disc.id,
                    name=f"{i.code} {i.description}",
                    uom=m.uom,
                    contractor_type=i.contractor_type,
                    contractor_mix=None,
                    budget_mhr=m.budget_mhr,
                    earned_cum=m.earned_cum,
                    spent_cum=m.spent_cum,
                    planned_pct_cum=m.planned_pct_cum,
                    progress_pct_cum=m.progress_pct_cum,
                    variance=variance,
                    status=classify_status(variance, tol),
                    pf_cum=m.pf_cum,
                    pf_week=m.pf_week,
                    pf_cum_band=m.pf_cum_band,
                    pf_week_band=m.pf_week_band,
                )
            )
    return out


def _disciplines(site: SiteInput, report: EngineReport) -> list[PanelDiscipline]:
    """EV-BORC-3 G1: filtre acilir listesi — `discipline_id`den BAGIMSIZ, agacin tum kokleri."""
    out = []
    for d in site.tree.disciplines:
        try:
            mix = report.row(RowKind.DISCIPLINE, d.id).contractor_mix
        except KeyError:
            mix = None
        out.append(
            PanelDiscipline(
                id=d.id, name=d.name or "Disiplinsiz", contractor_mix=mix.value if mix else None
            )
        )
    return out


def _curve(s: ScopeSeries, a: date, b: date) -> list[CurvePoint]:
    return [
        CurvePoint(
            day=p.day,
            is_future=p.is_future,
            planned_pct_cum=p.planned_pct_cum,
            progress_pct_cum=p.progress_pct_cum,
            variance=p.variance,
            status=p.status,
        )
        for p in s.points
        if a <= p.day <= b
    ]


def _bars(site: SiteInput, s: ScopeSeries, a: date, b: date) -> list[BarPoint]:
    status = {DiaryStatus.draft: "draft", DiaryStatus.submitted: "submitted"}
    return [
        BarPoint(
            day=p.day,
            is_holiday=p.is_holiday,
            diary_status=status.get(site.diary_status.get(p.day), "none"),  # type: ignore[arg-type]
            earned_day=p.earned_day,
            spent_day=p.spent_day,
        )
        for p in s.points
        if a <= p.day <= b
    ]


def _pf_trend(s: ScopeSeries, a: date, b: date) -> list[PfPoint]:
    return [
        PfPoint(day=p.day, pf_day=p.pf_day, pf_rolling=p.pf_rolling)
        for p in s.points
        if a <= p.day <= b and not p.is_holiday
    ]


def _histogram(
    cal: ProjectCalendar,
    s: ScopeSeries,
    weeks: list[tuple[date, date]],
    day: date,
    hours: Decimal,
    heads: dict[date, int] | None,
) -> list[HistogramWeek]:
    out = []
    for a, b in weeks:
        pts = [p for p in s.points if a <= p.day <= b]
        work = [p for p in pts if not p.is_holiday]
        planned_mhr = sum((s.budget_mhr * (p.planned_pct_day or ZERO) for p in pts), ZERO)
        cap = len(work) * hours
        past = [p for p in work if p.day <= day]
        actual = None
        if past and hours:
            if heads is not None:
                actual = Decimal(sum(heads.get(p.day, 0) for p in past)) / len(past)
            else:
                spent = sum((p.spent_day or ZERO for p in pts if p.day <= day), ZERO)
                actual = spent / (len(past) * hours)
        out.append(
            HistogramWeek(
                week_start=a,
                week_no=cal.week_no(a),
                week_end=b,
                working_days=len(work),
                is_future=a > day,
                planned_people=planned_mhr / cap if cap else None,
                actual_people=actual,
            )
        )
    return out


def _warnings(site: SiteInput, report: EngineReport, day: date, footer) -> list:  # noqa: ANN001
    missing, drafts = rw.diary_dates(site, day)
    return (
        rw.pf_warnings(site.tree, report)
        + rw.undistributed_warning(day, footer.undistributed_day)
        + rw.overrun_warnings(site.tree, report)
        + rw.diary_warnings(missing, drafts)
        + rw.unrated_warnings(site.tree, report)
        + rw.unknown_line_warnings(site, day)
    )


def _empty(q: PanelQuery, site: SiteInput | None, restricted: bool = False) -> PanelReport:
    return PanelReport(
        day=q.day,
        range=q.range,
        discipline_id=q.discipline_id,
        contractor_type=q.contractor,
        has_baseline=site is not None,
        has_field_data=False,
        day_no=None,
        week_no=None,
        week_start=None,
        week_end=None,
        revision=revision_ref(site.revision) if site else None,
        tolerance_points=None,
        kpi=None,
        s_curve=[],
        bars=[],
        pf_trend=[],
        histogram=[],
        actual_basis="equivalent" if q.discipline_id or restricted else "headcount",
        standard_daily_hours=None,
        rows=[],
        warnings=[],
    )


def _has_field_data(site: SiteInput, day: date) -> bool:
    return any(e.day <= day for e in site.inp.qty_entries) or any(
        e.day <= day for e in site.inp.hours_entries
    )


async def build_panel(
    session: AsyncSession,
    site_id: uuid.UUID,
    q: PanelQuery,
    visibility: DisciplineScope = UNRESTRICTED,
) -> PanelReport:
    # DSC-B3: kisitlida budanmis orman (coklu kok kendiliginden); yabanci `discipline_id`
    # budanmis agacta yok → bugunku 404 (Ü7). Puantaj kaynagi site duzeyi kalir (Ü3).
    site = await build_site_input(session, site_id, q.day, visibility)
    if site is None:
        return _empty(q, None, visibility.is_restricted)
    if q.discipline_id is not None and q.discipline_id not in {d.id for d in site.tree.disciplines}:
        raise NotFoundError(NO_DISCIPLINE)
    cal = ProjectCalendar(site.inp.calendar)
    win = windows(cal, q.day, q.range)
    scope = Scope(root=q.discipline_id, contractor=q.contractor)
    non_direct = Scope(direct_only=False, indirect_only=True, contractor=q.contractor)
    scopes = [scope] if q.discipline_id else [scope, non_direct]
    result, report = compute_panel(site.inp, *win.span, scopes, as_of=q.day)
    main = result.get(scope)
    pos = report.position
    footer = await day_footer(session, site_id, q.day, report)
    hours = (await repo.load_calendar(session, site_id)).standard_daily_hours
    # S2: kisitlida kisi SAYIMI site geneli olurdu → esdeger kisi (disiplin filtresi emsali)
    heads = (
        None
        if q.discipline_id or visibility.is_restricted
        else await daily_headcount(session, site_id, *win.span, q.contractor)
    )
    rows = [_series_row(site, main, q.day, pos.week_start, "overall", "Genel")]
    if q.discipline_id is None and q.contractor is None:
        rows += [
            _report_row(report.row(RowKind.OVERALL_OWN), "Genel – Kendi"),
            _report_row(report.row(RowKind.OVERALL_SUBCON), "Genel – Taşeron"),
        ]
    discipline_rows = _discipline_rows(site, report, q)
    rows += discipline_rows
    if q.discipline_id is None:
        nd = result.get(non_direct)
        rows.append(
            _series_row(
                site,
                nd,
                q.day,
                pos.week_start,
                "non_direct",
                NON_DIRECT_ROW_NAME,
                _indirect_item_names(site, q.contractor),
            )
        )
    if main.budget_mhr == 0 and not discipline_rows:
        rows = []  # mockup: "Seçilen filtrede kalem yok"
    return PanelReport(
        day=q.day,
        range=q.range,
        discipline_id=q.discipline_id,
        contractor_type=q.contractor,
        has_baseline=True,
        has_field_data=_has_field_data(site, q.day),
        day_no=pos.day_no,
        week_no=pos.week_no,
        week_start=pos.week_start,
        week_end=pos.week_end,
        revision=revision_ref(site.revision),
        tolerance_points=site.inp.tolerance_points,
        kpi=_kpi(site, main, q.day, pos.week_start, footer),
        s_curve=_curve(main, *win.curve),
        bars=_bars(site, main, *win.bars),
        pf_trend=_pf_trend(main, *win.pf),
        histogram=_histogram(cal, main, win.weeks, q.day, hours, heads),
        actual_basis="equivalent" if heads is None else "headcount",
        standard_daily_hours=hours,
        rows=rows,
        warnings=_warnings(site, report, q.day, footer) + rw.empty_rate_warnings(site.tree),
        disciplines=_disciplines(site, report),
        pf_bands=pf_bands_out(site),
        calendar_start=cal.start_date,
        calendar_end=cal.end_date,
    )

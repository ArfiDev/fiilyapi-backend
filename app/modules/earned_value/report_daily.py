"""Gunluk ilerleme raporu (GIR) + ONAY (Ek A §4.1; §3.13 B3-1, B3-5; K13, K14, K23).

* d gunu gunlugu HIC yoksa `not_generated` (B3-1).
* Onayli + kilitli gun → en son snapshot SURUMU AYNEN doner (onayli rapor DEGISMEZ).
* Aksi hâlde canli: motor (`compute_daily_report`) + hafta trendi (`compute_series`).
* Onay: takvim basi..d arasinda gunlugu OLAN her gun GONDERILMIS olmali (taslak → 422);
  gunlugu OLMAYAN is gunu engel degil, `missing_diary_dates` olarak doner.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import UNRESTRICTED, DisciplineScope
from app.core.errors import ConflictError, EarnedValueValidationError
from app.modules.earned_value import diary_adapter as adp
from app.modules.earned_value import report_warnings as rw
from app.modules.earned_value.access import assert_site_writable
from app.modules.earned_value.engine import (
    DailyReport as EngineReport,
)
from app.modules.earned_value.engine import (
    ProjectCalendar,
    Scope,
    compute_daily_report,
    compute_series,
)
from app.modules.earned_value.ev_input import SiteInput, build_site_input
from app.modules.earned_value.guards import SITE_COMPLETED_BUDGET_READ_ONLY
from app.modules.earned_value.models import EvReportApproval, EvReportSnapshot
from app.modules.earned_value.report_qurr import pf_bands_out, revision_ref
from app.modules.earned_value.report_snapshot_scope import restrict_snapshot
from app.modules.earned_value.schemas_reports import (
    ApprovalResult,
    DailyFooter,
    DailyReport,
    KpiRowOut,
    QtyTreeRow,
    TrendPoint,
    UserRef,
    WeatherDay,
)
from app.modules.site_diary.models import DiaryStatus, SiteDiaryEntry
from app.modules.users.models import User

ZERO = Decimal(0)
NOT_GENERATED = "Bu günün günlüğü yok — rapor üretilemedi"
DRAFTS_BLOCK = "Onay için şu günlükler gönderilmeli: {days}"


def _empty(day: date, site: SiteInput | None) -> DailyReport:
    return DailyReport(
        status="not_generated",
        report_date=day,
        report_no=None,
        version=None,
        day_no=None,
        week_no=None,
        week_start=None,
        week_end=None,
        project_start=site.inp.calendar.start_date if site else None,
        revision=revision_ref(site.revision) if site else None,
        generated_at=datetime.now(UTC),
        approved_at=None,
        approved_by=None,
        missing_diary_dates=[],
        draft_diary_dates=[],
        tolerance_points=None,
        weather=[],
        kpis=[],
        trend=[],
        quantities=[],
        footer=None,
        unrated_entries=[],
        warnings=[],
        pf_bands=pf_bands_out(site) if site else None,
        calendar_start=site.inp.calendar.start_date if site else None,
        calendar_end=site.inp.calendar.end_date if site else None,
    )


def _kpis(site: SiteInput, report: EngineReport) -> list[KpiRowOut]:
    names = {d.id: d.name or "Disiplinsiz" for d in site.tree.disciplines}
    return [
        KpiRowOut(
            kind=r.kind,
            node_id=r.node_id,
            name=names.get(r.node_id) if r.node_id else None,
            contractor_mix=r.contractor_mix.value if r.contractor_mix else None,
            budget_mhr=r.budget_mhr,
            earned_day=r.earned_day,
            earned_cum=r.earned_cum,
            spent_day=r.spent_day,
            spent_cum=r.spent_cum,
            progress_pct_day=r.progress_pct_day,
            progress_pct_cum=r.progress_pct_cum,
            planned_pct_day=r.planned_pct_day,
            planned_pct_cum=r.planned_pct_cum,
            variance=r.variance,
            status=r.status,
            pf_day=r.pf_day,
            pf_cum=r.pf_cum,
            pf_week=r.pf_week,
            pf_day_band=r.pf_day_band,
            pf_cum_band=r.pf_cum_band,
            pf_week_band=r.pf_week_band,
        )
        for r in report.rows
    ]


def _quantities(site: SiteInput, report: EngineReport) -> list[QtyTreeRow]:
    out: list[QtyTreeRow] = []

    def row(node_id: str, level: int, name: str, ct, direct) -> None:  # noqa: ANN001
        m = report.nodes.get(node_id)
        if m is None:
            return
        out.append(
            QtyTreeRow(
                node_id=node_id,
                level=level,
                name=name,
                uom=m.uom,
                contractor_type=ct,
                is_direct=direct,
                planned_unit_mhr=m.planned_unit_mhr,
                actual_unit_mhr_day=m.actual_unit_mhr_day,
                actual_unit_mhr_cum=m.actual_unit_mhr_cum,
                planned_qty=m.planned_qty,
                qty_day=m.qty_day,
                qty_cum=m.qty_cum,
                remaining_qty=m.remaining_qty,
                pf_day=m.pf_day,
                pf_day_band=m.pf_day_band,
                spent_day=m.spent_day,
                progress_pct_cum=m.progress_pct_cum,
                pf_cum=m.pf_cum,
                pf_cum_band=m.pf_cum_band,
            )
        )

    for d in site.tree.disciplines:
        row(d.id, 1, d.name or "Disiplinsiz", d.default_contractor_type, None)
        for g in d.groups:
            row(g.id, 2, g.name, None, None)
            for i in g.items:
                row(i.id, 3, f"{i.code} {i.description}", i.contractor_type, i.is_direct)
    return out


def _trend(site: SiteInput, day: date, cal: ProjectCalendar) -> list[TrendPoint]:
    start = cal.week_start(day)
    end = min(cal.week_end(day), cal.end_date)
    series = compute_series(site.inp, start, end, [Scope()], as_of=day).get(Scope())
    out = []
    for p in series.points:
        delta = (
            p.progress_pct_cum - p.planned_pct_cum
            if p.progress_pct_cum is not None and p.planned_pct_cum is not None
            else None
        )
        out.append(
            TrendPoint(
                day=p.day,
                is_holiday=p.is_holiday,
                is_draft=site.diary_status.get(p.day) is DiaryStatus.draft,
                is_future=p.is_future,
                planned_pct_cum=p.planned_pct_cum,
                progress_pct_cum=p.progress_pct_cum,
                delta=delta,
                earned_day=p.earned_day,
                spent_day=p.spent_day,
                pf_day=p.pf_day,
                pf_rolling=p.pf_rolling,
            )
        )
    return out


async def _weather(
    session: AsyncSession, site_id: uuid.UUID, start: date, end: date
) -> list[WeatherDay]:
    rows = (
        await session.execute(
            select(SiteDiaryEntry).where(
                SiteDiaryEntry.site_id == site_id,
                SiteDiaryEntry.entry_date >= start,
                SiteDiaryEntry.entry_date <= end,
            )
        )
    ).scalars()
    by_day = {e.entry_date: e for e in rows}
    out = []
    day = start
    while day <= end:
        e = by_day.get(day)
        out.append(
            WeatherDay(
                day=day,
                condition=getattr(e.weather, "value", e.weather) if e else None,
                temp_min_c=e.temp_min_c if e else None,
                temp_max_c=e.temp_max_c if e else None,
                wind_ms=e.wind_ms if e else None,
            )
        )
        day += timedelta(days=1)
    return out


async def day_footer(
    session: AsyncSession, site_id: uuid.UUID, day: date, report: EngineReport
) -> DailyFooter:
    source = sum((r.hours for r in await adp.source_rows(session, site_id, day)), ZERO)
    saved = await adp.load_saved(session, site_id, day)
    return DailyFooter(
        spent_total_day=report.totals.spent_day,
        timesheet_total_day=source,
        undistributed_day=source - adp.allocated_hours(saved),
        undistributed_reason=saved.note.unallocated_reason if saved.note else None,
        unallocated_day=report.totals.unallocated_day,
    )


async def latest_snapshot(
    session: AsyncSession, site_id: uuid.UUID, day: date
) -> EvReportSnapshot | None:
    return (
        await session.execute(
            select(EvReportSnapshot)
            .where(EvReportSnapshot.site_id == site_id, EvReportSnapshot.report_date == day)
            .order_by(EvReportSnapshot.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def build_live(
    session: AsyncSession,
    site_id: uuid.UUID,
    day: date,
    scope: DisciplineScope = UNRESTRICTED,
) -> DailyReport:
    # DSC-B3: canli yol kapsama gore budanmis girdiyle; ONAY (`approve`) kisitsiz cagirir (Ç5).
    site = await build_site_input(session, site_id, day, scope)
    if site is None or day not in site.diary_status:
        return _empty(day, site)
    cal = ProjectCalendar(site.inp.calendar)
    report = compute_daily_report(site.inp, day)
    pos = report.position
    missing, drafts = rw.diary_dates(site, day)
    footer = await day_footer(session, site_id, day, report)
    warnings = (
        rw.pf_warnings(site.tree, report)
        + rw.overrun_warnings(site.tree, report)
        + rw.diary_warnings(missing, drafts)
        + rw.unknown_line_warnings(site, day)
        + rw.undistributed_warning(day, footer.undistributed_day)
    )
    return DailyReport(
        status="draft",
        report_date=day,
        report_no=pos.day_no,
        version=None,
        day_no=pos.day_no,
        week_no=pos.week_no,
        week_start=pos.week_start,
        week_end=pos.week_end,
        project_start=cal.start_date,
        revision=revision_ref(site.revision),
        generated_at=datetime.now(UTC),
        approved_at=None,
        approved_by=None,
        missing_diary_dates=missing,
        draft_diary_dates=drafts,
        tolerance_points=site.inp.tolerance_points,
        weather=await _weather(session, site_id, pos.week_start, min(pos.week_end, cal.end_date)),
        kpis=_kpis(site, report),
        trend=_trend(site, day, cal),
        quantities=_quantities(site, report),
        footer=footer,
        unrated_entries=rw.unrated_warnings(site.tree, report),
        warnings=warnings,
        pf_bands=pf_bands_out(site),  # snapshot'a girer: onayli rapor KENDI esigiyle
        calendar_start=cal.start_date,
        calendar_end=cal.end_date,
    )


async def build_daily(
    session: AsyncSession, site_id: uuid.UUID, day: date, scope: DisciplineScope = UNRESTRICTED
) -> DailyReport:
    state = await adp.lock_state(session, site_id, day)
    if state.locked:
        snap = await latest_snapshot(session, site_id, day)
        if snap is not None:
            report = DailyReport.model_validate(snap.payload)
            if not scope.is_restricted:
                return report  # kisitsiz: payload AYNEN
            # DSC-B3 Ü4: yeniden hesap YOK; onay anindaki satirlardan izinli `d:` bloklari kalir.
            allowed = {f"d:{i}" for i in scope.discipline_ids or ()}
            return restrict_snapshot(report, allowed)
    return await build_live(session, site_id, day, scope)


async def approve(
    session: AsyncSession, site_id: uuid.UUID, day: date, actor: User
) -> ApprovalResult:
    # EV-BORC-5: servis içi TEK kural — şantiye satırı `FOR UPDATE` + durum kilit ALTINDA
    # (uç bağımlılığı kilitsiz erken kontroldür). Kilit iki işi görür: tamamlanmaya karşı
    # yarışta onay geçmez; iki eşzamanlı onay sıraya girer → sürüm (max+1) kilit altında
    # hesaplanır, ikincisi UQ'ya çarpıp genel 409 almaz (bekçi test_evborc5_approve_lock).
    await assert_site_writable(session, site_id, message=SITE_COMPLETED_BUDGET_READ_ONLY)
    live = await build_live(session, site_id, day)
    if live.status == "not_generated":
        # `calendar_start` yalnız baseline yoksa None kalır (bkz. `_empty`/`build_live`):
        # bu, ikinci bir "baseline var mı" kuralı yazmadan mevcut sinyali yeniden kullanır.
        if live.calendar_start is None:
            raise ConflictError(adp.NO_BASELINE)
        raise ConflictError(NOT_GENERATED)
    drafts = [d for d in live.draft_diary_dates if d <= day]
    if drafts:
        raise EarnedValueValidationError(
            DRAFTS_BLOCK.format(days=", ".join(d.strftime("%d.%m.%Y") for d in drafts))
        )
    approval = EvReportApproval(site_id=site_id, report_date=day, approved_by_user_id=actor.id)
    session.add(approval)
    await session.flush()
    await session.refresh(approval)
    version = (
        await session.scalar(
            select(func.coalesce(func.max(EvReportSnapshot.version), 0)).where(
                EvReportSnapshot.site_id == site_id, EvReportSnapshot.report_date == day
            )
        )
    ) + 1
    report = live.model_copy(
        update={
            "status": "approved",
            "version": version,
            "approved_at": approval.approved_at,
            "approved_by": UserRef(id=actor.id, full_name=actor.full_name),
        }
    )
    session.add(
        EvReportSnapshot(
            site_id=site_id,
            report_date=day,
            day_no=live.day_no or 0,
            version=version,
            approval_id=approval.id,
            payload=report.model_dump(mode="json"),
            approved_at=approval.approved_at,
            approved_by_user_id=actor.id,
        )
    )
    await session.flush()
    return ApprovalResult(report=report, missing_diary_dates=live.missing_diary_dates)

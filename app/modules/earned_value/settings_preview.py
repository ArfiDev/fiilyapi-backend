"""AYP (ayarlar) canli onizleme — bugunun sapmasi/PF'i, hafta no, pacal gerceklesen (PLN-B3).

Deger KAYITLI ayarla hesaplanir; ekrandaki taslak tolerans/bant ile yeniden siniflama
istemcidedir (K27 `variance_points` + K18 2 ondalik — sunucu ham ve gosterilen degeri verir).
Pacal formulu TEK: `report_qurr.composite_value` (QURR kartlariyla ayni).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import UNRESTRICTED, DisciplineScope
from app.modules.earned_value import settings_service
from app.modules.earned_value.engine import RowKind, compute_daily_report, variance_points
from app.modules.earned_value.ev_input import build_site_input
from app.modules.earned_value.report_qurr import composite_cards, composite_value
from app.modules.earned_value.schemas_reports import CompositeValueOut, SettingsPreview


async def preview(
    session: AsyncSession, site_id: uuid.UUID, day: date, scope: DisciplineScope = UNRESTRICTED
) -> SettingsPreview:
    site = await build_site_input(session, site_id, day, scope)
    settings = await settings_service.get_settings(session, site_id)
    if site is None:
        return SettingsPreview(
            day=day,
            has_baseline=False,
            day_no=None,
            week_no=None,
            week_start=None,
            week_end=None,
            variance=None,
            variance_points=None,
            status=None,
            pf_day=None,
            pf_week=None,
            pf_day_band=None,
            pf_week_band=None,
            composites=[],
        )
    report = compute_daily_report(site.inp, day)
    overall = report.row(RowKind.OVERALL)
    pos = report.position
    return SettingsPreview(
        day=day,
        has_baseline=True,
        day_no=pos.day_no,
        week_no=pos.week_no,
        week_start=pos.week_start,
        week_end=pos.week_end,
        variance=overall.variance,
        variance_points=None if overall.variance is None else variance_points(overall.variance),
        status=overall.status,
        pf_day=overall.pf_day,
        pf_week=overall.pf_week,
        pf_day_band=overall.pf_day_band,
        pf_week_band=overall.pf_week_band,
        composites=composite_cards(site, report, settings.composite_metrics),
    )


async def composite_preview(
    session: AsyncSession,
    site_id: uuid.UUID,
    day: date,
    measure: str,
    numerator_item_ids: list[uuid.UUID],
    denominator_item_id: uuid.UUID,
    scope: DisciplineScope = UNRESTRICTED,
) -> CompositeValueOut:
    # Ü7: yabanci kalem budanmis agacta yok → "baseline'da yok" ile ayni yanit
    site = await build_site_input(session, site_id, day, scope)
    if site is None:
        return CompositeValueOut(unit=None, actual=None, planned=None, deviation=None)
    report = compute_daily_report(site.inp, day)
    v = composite_value(site, report, measure, numerator_item_ids, denominator_item_id)
    return CompositeValueOut(unit=v.unit, actual=v.actual, planned=v.planned, deviation=v.deviation)

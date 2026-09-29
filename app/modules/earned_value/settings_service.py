"""Planlama (EV) santiye ayarlari — okuma + TAM DEGISTIRME (AYP; PLANLAMA-SPEC §3.8 K1).

K1: ayarlar santiye kapsamlidir, sirket varsayilani katmani YOKTUR. Satir yoksa
`defaults.py` sabitleri doner (`is_default` = true); ilk PUT satiri GOVDEYLE yazar.
Ayarlar revizyonsuzdur (B1-6); donmus baseline kendi egrisini tasir (K8).

Kapsam: cagiran `access.visible_site` ile santiyeyi COZMUS olmalidir (router);
bu modul yalniz `site_id` uzerinden yazar ve BASKA santiyenin satirina dokunmaz.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import UNRESTRICTED, DisciplineScope, visible_item_set
from app.core.errors import EarnedValueValidationError
from app.modules.boq.models import BoqItem
from app.modules.earned_value import defaults, guards
from app.modules.earned_value.access import assert_site_writable
from app.modules.earned_value.models import (
    EvCompositeMetric,
    EvCompositeMetricTerm,
    EvHoliday,
    EvSiteSettings,
)
from app.modules.earned_value.schemas_settings import (
    CompositeMetricRead,
    DailyPfBands,
    HolidayInput,
    HolidayRead,
    PfBands,
    SettingsRead,
    SettingsSave,
    UserRef,
    WeeklyPfBands,
)
from app.modules.users.models import User

_ALL_DAYS = frozenset(range(7))


# ------------------------------------------------------------------- okuma


def _default_bands() -> PfBands:
    return PfBands(
        daily=DailyPfBands(
            red_below=defaults.DAILY_RED_BELOW,
            green_from=defaults.DAILY_GREEN_FROM,
            high_above=defaults.DAILY_HIGH_ABOVE,
        ),
        weekly=WeeklyPfBands(
            red_below=defaults.WEEKLY_RED_BELOW, green_from=defaults.WEEKLY_GREEN_FROM
        ),
    )


def _row_bands(row: EvSiteSettings) -> PfBands:
    return PfBands(
        daily=DailyPfBands(
            red_below=row.daily_red_below,
            green_from=row.daily_green_from,
            high_above=row.daily_high_above,
        ),
        weekly=WeeklyPfBands(red_below=row.weekly_red_below, green_from=row.weekly_green_from),
    )


async def _holidays(session: AsyncSession, site_id: uuid.UUID) -> list[HolidayRead]:
    stmt = (
        select(EvHoliday)
        .where(EvHoliday.site_id == site_id)
        .order_by(EvHoliday.date_from, EvHoliday.date_to)
    )
    return [
        HolidayRead(id=h.id, date_from=h.date_from, date_to=h.date_to, note=h.note)
        for h in (await session.execute(stmt)).scalars()
    ]


async def _composite_metrics(
    session: AsyncSession, site_id: uuid.UUID
) -> list[CompositeMetricRead]:
    metrics = list(
        (
            await session.execute(
                select(EvCompositeMetric)
                .where(EvCompositeMetric.site_id == site_id)
                .order_by(EvCompositeMetric.sort_order, EvCompositeMetric.name)
            )
        ).scalars()
    )
    if not metrics:
        return []
    terms: dict[uuid.UUID, list[uuid.UUID]] = {m.id: [] for m in metrics}
    term_rows = await session.execute(
        select(EvCompositeMetricTerm.metric_id, EvCompositeMetricTerm.boq_item_id)
        .join(BoqItem, BoqItem.id == EvCompositeMetricTerm.boq_item_id)
        .where(EvCompositeMetricTerm.metric_id.in_(terms))
        .order_by(BoqItem.sort_order, BoqItem.code)
    )
    for metric_id, item_id in term_rows.all():
        terms[metric_id].append(item_id)
    return [
        CompositeMetricRead(
            id=m.id,
            name=m.name,
            measure=m.measure,
            numerator_item_ids=terms[m.id],
            denominator_item_id=m.denominator_boq_item_id,
        )
        for m in metrics
    ]


async def _updated_by(session: AsyncSession, user_id: uuid.UUID | None) -> UserRef | None:
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    return None if user is None else UserRef(id=user.id, full_name=user.full_name)


async def _visible_metrics(
    session: AsyncSession, metrics: list[CompositeMetricRead], scope: DisciplineScope
) -> list[CompositeMetricRead]:
    """DSC-B3 S11/S3: kisitlida kapsam disi (disiplinsiz dahil) kalemli pacal kart DUSER."""
    # Bilincli fark: kart gizleme `visible_item_set` (R(site)) ile calisir; RAPOR tarafi ise
    # `hidden_items` (TAM donmus agac) ile. Baseline'da olmayan ama R(site)'ta yabanci olan
    # kalem icin GET karti gizler, rapor tutar (GET daha katidir). Kart degeri yabanci kalemi
    # icermez cunku kalem baseline'da yoktur → sizinti yok.
    ids = [i for m in metrics for i in (*m.numerator_item_ids, m.denominator_item_id)]
    visible = await visible_item_set(session, scope, ids)
    if visible is None:
        return metrics
    return [m for m in metrics if {*m.numerator_item_ids, m.denominator_item_id} <= visible]


async def get_settings(
    session: AsyncSession, site_id: uuid.UUID, scope: DisciplineScope = UNRESTRICTED
) -> SettingsRead:
    row = await session.get(EvSiteSettings, site_id, populate_existing=True)
    holidays = await _holidays(session, site_id)
    metrics = await _visible_metrics(session, await _composite_metrics(session, site_id), scope)
    if row is None:
        return SettingsRead(
            week_start_dow=defaults.WEEK_START_DOW,
            weekly_off_days=sorted(defaults.WEEKLY_OFF_DAYS),
            standard_daily_hours=defaults.STANDARD_DAILY_HOURS,
            tolerance_points=defaults.TOLERANCE_POINTS,
            pf_bands=_default_bands(),
            holidays=holidays,
            composite_metrics=metrics,
            is_default=True,
            updated_at=None,
            updated_by=None,
        )
    return SettingsRead(
        week_start_dow=row.week_start_dow,
        weekly_off_days=sorted(defaults.mask_to_days(row.weekly_off_days)),
        standard_daily_hours=row.standard_daily_hours,
        tolerance_points=row.tolerance_points,
        pf_bands=_row_bands(row),
        holidays=holidays,
        composite_metrics=metrics,
        is_default=False,
        updated_at=row.updated_at,
        updated_by=await _updated_by(session, row.updated_by_user_id),
    )


# --------------------------------------------------------------- dogrulama


def _check_bands(bands: PfBands) -> None:
    daily = bands.daily
    if not (daily.red_below <= daily.green_from <= daily.high_above):
        raise EarnedValueValidationError(guards.DAILY_BANDS_ORDER)
    if bands.weekly.red_below > bands.weekly.green_from:
        raise EarnedValueValidationError(guards.WEEKLY_BANDS_ORDER)


def _check_holidays(holidays: list[HolidayInput]) -> None:
    for holiday in holidays:
        if holiday.date_to < holiday.date_from:
            raise EarnedValueValidationError(guards.HOLIDAY_RANGE_INVALID)
    ordered = sorted(holidays, key=lambda h: (h.date_from, h.date_to))
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.date_from <= previous.date_to:
            raise EarnedValueValidationError(guards.HOLIDAY_RANGE_OVERLAP)


async def _check_composite_items(
    session: AsyncSession, site_id: uuid.UUID, data: SettingsSave
) -> None:
    """Pacal pay/payda kalemleri BU santiyenin BOQ'unda olmali; yoksa 422.

    Baska santiyenin kalemi ile var olmayan kimlik AYNI 422'yi alir (kimlik sizdirmaz).
    """
    wanted = {
        item_id
        for metric in data.composite_metrics
        for item_id in (*metric.numerator_item_ids, metric.denominator_item_id)
    }
    if not wanted:
        return
    found = set(
        (
            await session.execute(
                select(BoqItem.id).where(BoqItem.site_id == site_id, BoqItem.id.in_(wanted))
            )
        ).scalars()
    )
    if found != wanted:
        raise EarnedValueValidationError(guards.COMPOSITE_ITEM_FOREIGN)


async def validate(session: AsyncSession, site_id: uuid.UUID, data: SettingsSave) -> None:
    _check_bands(data.pf_bands)
    if set(data.weekly_off_days) >= _ALL_DAYS:
        raise EarnedValueValidationError(guards.ALL_DAYS_OFF)
    _check_holidays(data.holidays)
    await _check_composite_items(session, site_id, data)


# ------------------------------------------------------------------- yazma


async def _upsert_row(
    session: AsyncSession, site_id: uuid.UUID, data: SettingsSave, actor: User
) -> None:
    row = await session.get(EvSiteSettings, site_id)
    if row is None:
        row = EvSiteSettings(site_id=site_id)
        session.add(row)
    bands = data.pf_bands
    row.week_start_dow = data.week_start_dow
    row.weekly_off_days = defaults.days_to_mask(set(data.weekly_off_days))
    row.standard_daily_hours = data.standard_daily_hours
    row.tolerance_points = data.tolerance_points
    row.daily_red_below = bands.daily.red_below
    row.daily_green_from = bands.daily.green_from
    row.daily_high_above = bands.daily.high_above
    row.weekly_red_below = bands.weekly.red_below
    row.weekly_green_from = bands.weekly.green_from
    row.updated_by_user_id = actor.id


async def _replace_holidays(
    session: AsyncSession, site_id: uuid.UUID, holidays: list[HolidayInput]
) -> None:
    await session.execute(delete(EvHoliday).where(EvHoliday.site_id == site_id))
    session.add_all(
        EvHoliday(site_id=site_id, date_from=h.date_from, date_to=h.date_to, note=h.note)
        for h in holidays
    )


async def _replace_composite_metrics(
    session: AsyncSession, site_id: uuid.UUID, data: SettingsSave
) -> None:
    # Terimler `ev_composite_metric_terms.metric_id` FK CASCADE ile gider.
    await session.execute(delete(EvCompositeMetric).where(EvCompositeMetric.site_id == site_id))
    for order, metric in enumerate(data.composite_metrics):
        row = EvCompositeMetric(
            id=uuid.uuid4(),
            site_id=site_id,
            name=metric.name,
            measure=metric.measure,
            denominator_boq_item_id=metric.denominator_item_id,
            sort_order=order,
        )
        session.add(row)
        # Pay bir KUMEDIR: tekrar eden kimlik tek terime iner (PK metric × kalem).
        unique_items = dict.fromkeys(metric.numerator_item_ids)
        session.add_all(
            EvCompositeMetricTerm(metric_id=row.id, boq_item_id=item_id) for item_id in unique_items
        )


async def save_settings(
    session: AsyncSession, site_id: uuid.UUID, data: SettingsSave, actor: User
) -> SettingsRead:
    """TAM DEGISTIRME: ayar satiri + tatiller + pacal metrikler govdeyle ayni olur.

    Tamamlanmis santiye SALT OKUNUR (§3.10 F0-8): kilit + durum kilit altinda (TEK kural).
    """
    await assert_site_writable(session, site_id, message=guards.SITE_COMPLETED_READ_ONLY)
    await validate(session, site_id, data)
    await _upsert_row(session, site_id, data, actor)
    await _replace_holidays(session, site_id, data.holidays)
    await _replace_composite_metrics(session, site_id, data)
    await session.flush()
    return await get_settings(session, site_id)

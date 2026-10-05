"""Planlama (EV) santiye ayarlari semalari — AYP ekrani (PLANLAMA-SPEC §3.8 K1/K5/K6/K19/K25).

Tek GET + tek PUT. PUT TAM DEGISTIRMEDIR: tatiller ve pacal metrikler dahil govde
santiyenin ayar kumesinin TAMAMIDIR, govdede olmayan satir SILINIR.

## Alanda OLMAYANLAR (bilerek)
* Baslangic/bitis tarihi — ayar DEGILDIR, aktif baseline'dan turer (K7).
* "Her n. haftanin X gunu" tatil kurali — ilk surumde YOK (S5).
* Tatil / pacal satir `id`si PUT govdesinde — degistirme semantiginde kimlik
  tasinmaz (baska tablo bu satirlara baglanmaz); `extra="forbid"` ile 422.

## Dogrulama katmanlari
Tek alanli sinirlar (1–16 saat, tolerans ≥ 0, gun 0–6, ondalik hassasiyeti) semada
durur → Pydantic 422 alan adli. Alanlar ARASI kurallar (bant sirasi, en az bir is
gunu, tatil araligi/cakismasi, pacal kaleminin santiyesi) `settings_service`tedir →
`EarnedValueValidationError` (422) `guards` metinleriyle.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.field_mask import Hassas
from app.modules.earned_value.decimal_out import EvDecimal
from app.modules.earned_value.mask_types import Yok
from app.modules.earned_value.models import CompositeMeasure

_STRICT = ConfigDict(extra="forbid")

Dow = Annotated[int, Field(ge=0, le=6)]
#: `ev_site_settings.daily_*`/`weekly_*` Numeric(5,3).
Band = Annotated[EvDecimal, Field(ge=0, max_digits=5, decimal_places=3), Hassas.yok]
DailyHours = Annotated[EvDecimal, Field(ge=1, le=16, max_digits=4, decimal_places=2), Hassas.yok]
Tolerance = Annotated[EvDecimal, Field(ge=0, max_digits=5, decimal_places=2), Hassas.yok]
HolidayNote = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
MetricName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]


class DailyPfBands(BaseModel):
    """Gunluk PF: < `red_below` kirmizi · [`green_from`; `high_above`] yesil ·
    > `high_above` "supheli yuksek" (S6, K19). Sira: red ≤ green ≤ high."""

    model_config = _STRICT

    red_below: Band
    green_from: Band
    high_above: Band


class WeeklyPfBands(BaseModel):
    """Haftalik (ve kumulatif, K19) PF: < `red_below` kirmizi · < `green_from` sari."""

    model_config = _STRICT

    red_below: Band
    green_from: Band


class PfBands(BaseModel):
    model_config = _STRICT

    daily: DailyPfBands
    weekly: WeeklyPfBands


class HolidayInput(BaseModel):
    """Elle tatil; tek gun icin `date_to` = `date_from`."""

    model_config = _STRICT

    date_from: date
    date_to: date
    note: HolidayNote = ""


class HolidayRead(BaseModel):
    id: uuid.UUID
    date_from: date
    date_to: date
    note: str


class CompositeMetricInput(BaseModel):
    """Pacal metrik (K25): pay = is tipleri (BOQ kalemi) toplami, TEK olcu; payda = bir
    is tipinin miktari. Kalemler bu santiyenin BOQ'unda olmalidir (servis)."""

    model_config = _STRICT

    name: MetricName
    measure: CompositeMeasure
    numerator_item_ids: list[uuid.UUID] = Field(min_length=1)
    denominator_item_id: uuid.UUID


class CompositeMetricRead(BaseModel):
    id: uuid.UUID
    name: str
    measure: CompositeMeasure
    numerator_item_ids: list[uuid.UUID]
    denominator_item_id: uuid.UUID


class SettingsSave(BaseModel):
    """PUT govdesi — santiyenin ayar kumesinin TAMAMI."""

    model_config = _STRICT

    week_start_dow: Dow
    weekly_off_days: list[Dow] = Field(max_length=7)
    standard_daily_hours: DailyHours
    tolerance_points: Tolerance
    pf_bands: PfBands
    holidays: list[HolidayInput] = Field(default_factory=list)
    composite_metrics: list[CompositeMetricInput] = Field(default_factory=list)


class UserRef(BaseModel):
    id: uuid.UUID
    full_name: str


class SettingsRead(BaseModel):
    """GET/PUT yaniti. Ayar satiri yoksa `defaults.py` degerleri, `is_default` = true."""

    week_start_dow: int
    #: 0 = Pazartesi … 6 = Pazar (`date.weekday()`), artan sirali.
    weekly_off_days: list[int]
    standard_daily_hours: Yok
    tolerance_points: Yok
    pf_bands: PfBands
    #: `date_from` sirali.
    holidays: list[HolidayRead]
    #: `sort_order` (govdedeki sira) sirali.
    composite_metrics: list[CompositeMetricRead]
    is_default: bool
    updated_at: datetime | None
    updated_by: UserRef | None

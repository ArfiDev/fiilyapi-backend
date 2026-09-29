"""Haftalik QURR (Miktar & Birim Oran Raporu) — Ek A §4.2; B3-2 (onaylanmaz, canli).

Satirlar agactan TURER (Ek A §7: rapor satir listesi agactan ayri tutulmaz): L3 = is tipi
(BOQ kalemi). Ara toplamlar L1/L2; "Σ dogrudan" ve "Σ dogrudan + dolayli" ayri (S3).
Degerler motorun dugum metriklerinden (`compute_daily_report`, rapor gunu = hafta sonu /
bugun / takvim sonunun en erkeni); onceki revizyon = aktiften ONCEKI en son donmus
revizyonun baseline yapraklari.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import today
from app.modules.earned_value import report_warnings as rw
from app.modules.earned_value import settings_service
from app.modules.earned_value.budget_snapshot import load_leaves
from app.modules.earned_value.engine import (
    DailyReport,
    NodeMetrics,
    PfBands,
    ProjectCalendar,
    RowKind,
    compute_daily_report,
    pf_band,
)
from app.modules.earned_value.engine.policy import (
    DEFAULT_CUMULATIVE_PF_BANDS,
    DEFAULT_DAILY_PF_BANDS,
    contractor_mix,
)
from app.modules.earned_value.ev_input import SiteInput
from app.modules.earned_value.models import EvRevision, RevisionStatus
from app.modules.earned_value.schemas_reports import (
    CompositeCard,
    KpiPf,
    PfBandOut,
    PfBandsOut,
    QurrReport,
    QurrRow,
    QurrTotal,
    RevisionRef,
)
from app.modules.site_diary.models import DiaryStatus

ZERO = Decimal(0)


def _ratio(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return None if a is None or b is None or b == 0 else a / b


def revision_ref(rev: EvRevision | None) -> RevisionRef | None:
    if rev is None:
        return None
    return RevisionRef(id=rev.id, number=rev.number, name=rev.name, frozen_at=rev.frozen_at)


def _band(b: PfBands) -> PfBandOut:
    return PfBandOut(red_below=b.red_below, green_from=b.green_from, high_above=b.high_above)


def pf_bands_out(site: SiteInput) -> PfBandsOut:
    """EV-BORC-3 G2: raporun kullandigi esikler — motorun kullandigiyla AYNI (girdi, yoksa
    motor varsayilani). Haftalik PF kumulatif esigini kullanir."""
    return PfBandsOut(
        daily=_band(site.inp.daily_pf_bands or DEFAULT_DAILY_PF_BANDS),
        cumulative=_band(site.inp.cumulative_pf_bands or DEFAULT_CUMULATIVE_PF_BANDS),
    )


def week_range(cal: ProjectCalendar, week_no: int) -> tuple[date, date] | None:
    """K23 hafta no → [hafta basi, hafta sonu] (ilk hafta kismi; takvim disi → None)."""
    first = cal.start_date
    nominal = first - timedelta(days=(first.weekday() - cal._settings.week_start_dow) % 7)  # noqa: SLF001
    start = nominal + timedelta(days=7 * (week_no - 1))
    end = start + timedelta(days=6)
    start = max(start, first)
    if week_no < 1 or start > cal.end_date:
        return None
    return start, end


@dataclass(frozen=True, slots=True)
class PrevItem:
    qty: Decimal
    budget: Decimal


async def previous_revision(
    session: AsyncSession, site_id: uuid.UUID, active: EvRevision
) -> EvRevision | None:
    return (
        await session.execute(
            select(EvRevision)
            .where(
                EvRevision.site_id == site_id,
                EvRevision.number < active.number,
                EvRevision.status != RevisionStatus.DRAFT,
            )
            .order_by(EvRevision.number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def previous_items(session: AsyncSession, prev: EvRevision | None) -> dict[str, PrevItem]:
    """Onceki revizyon: is tipi (i:<kalem>) → Σ planli miktar, Σ butce."""
    if prev is None:
        return {}
    out: dict[str, PrevItem] = {}
    for lf in await load_leaves(session, prev.id):
        key = f"i:{lf.boq_item_id}"
        cur = out.get(key, PrevItem(ZERO, ZERO))
        out[key] = PrevItem(cur.qty + lf.planned_qty, cur.budget + lf.budget_mhr)
    return out


def _row(
    node_id: str,
    m: NodeMetrics,
    meta,
    prev: PrevItem | None,
    parent_id: str,  # noqa: ANN001
) -> QurrRow:
    prev_rate = _ratio(prev.budget, prev.qty) if prev else None
    return QurrRow(
        node_id=node_id,
        level=3,
        parent_id=parent_id,
        code=meta.code,
        name=meta.description,
        uom=meta.uom,
        contractor_type=meta.contractor_type,
        is_direct=meta.is_direct,
        a_prev_qty=prev.qty if prev else None,
        b_qty=m.planned_qty,
        c_qty_cum=m.qty_cum,
        d_remaining_qty=m.remaining_qty,
        e_qty_week=m.qty_week,
        f_prev_budget_mhr=prev.budget if prev else None,
        g_budget_mhr=m.budget_mhr,
        h_earned_cum=m.earned_cum,
        i_spent_cum=m.spent_cum,
        j_remaining_mhr=m.togo_mhr,  # K26: Σ yaprak togo (kirpmasiz)
        k_earned_week=m.earned_week,
        l_spent_week=m.spent_week,
        m_prev_unit_mhr=prev_rate,
        n_unit_mhr=m.planned_unit_mhr,
        o_actual_unit_mhr_cum=m.actual_unit_mhr_cum,
        p_actual_unit_mhr_week=m.actual_unit_mhr_week,
        q_pf_cum=m.pf_cum,
        r_pf_week=m.pf_week,
        q_band=m.pf_cum_band,
        r_band=m.pf_week_band,
        changed_qty=prev is not None and prev.qty != m.planned_qty,
        changed_rate=prev is not None and prev_rate != m.planned_unit_mhr,
        changed_budget=prev is not None and prev.budget != m.budget_mhr,
    )


@dataclass(frozen=True, slots=True)
class _TotalMeta:
    """Toplam satirinin agac baglami (§3.15 S1/S2)."""

    parent_id: str | None = None
    code: str | None = None
    contractor_mix: str | None = None


def _mix(leaves: Iterable) -> str | None:  # noqa: ANN001
    """S7 rozeti: direct yapraklarin yuklenici tipleri (motorun disiplin kurali — policy)."""
    mix = contractor_mix({lf.contractor_type for lf in leaves if lf.is_direct})
    return mix.value if mix else None


def _total(
    kind: str,
    node_id: str | None,
    name: str,
    parts: list[QurrRow],
    bands: PfBands,
    meta: _TotalMeta = _TotalMeta(),  # noqa: B008 - donmus dataclass, paylasimli varsayilan guvenli
) -> QurrTotal:
    def s(attr: str) -> Decimal:
        return sum((getattr(p, attr) for p in parts), ZERO)

    prev = [p.f_prev_budget_mhr for p in parts if p.f_prev_budget_mhr is not None]
    q_pf = _ratio(s("h_earned_cum"), s("i_spent_cum"))
    r_pf = _ratio(s("k_earned_week"), s("l_spent_week"))
    return QurrTotal(
        kind=kind,  # type: ignore[arg-type]
        node_id=node_id,
        name=name,
        parent_id=meta.parent_id,
        code=meta.code,
        contractor_mix=meta.contractor_mix,
        q_band=pf_band(q_pf, bands),  # motor kurali: hafta PF'i de kumulatif esikle
        r_band=pf_band(r_pf, bands),
        f_prev_budget_mhr=sum(prev, ZERO) if prev else None,
        g_budget_mhr=s("g_budget_mhr"),
        h_earned_cum=s("h_earned_cum"),
        i_spent_cum=s("i_spent_cum"),
        j_remaining_mhr=s("j_remaining_mhr"),
        k_earned_week=s("k_earned_week"),
        l_spent_week=s("l_spent_week"),
        q_pf_cum=q_pf,
        r_pf_week=r_pf,
    )


@dataclass(frozen=True, slots=True)
class _ItemMeta:
    code: str
    description: str
    uom: str
    contractor_type: object
    is_direct: bool


def build_rows(
    site: SiteInput, report: DailyReport, prev: dict[str, PrevItem]
) -> tuple[list[QurrRow], list[QurrTotal]]:
    rows: list[QurrRow] = []
    totals: list[QurrTotal] = []
    direct: list[QurrRow] = []
    everything: list[QurrRow] = []
    bands = site.inp.cumulative_pf_bands or DEFAULT_CUMULATIVE_PF_BANDS
    for d in site.tree.disciplines:
        disc_rows: list[QurrRow] = []
        for g in d.groups:
            group_rows: list[QurrRow] = []
            for i in g.items:
                if i.id not in report.nodes:
                    continue
                meta = _ItemMeta(i.code, i.description, i.uom, i.contractor_type, i.is_direct)
                row = _row(i.id, report.nodes[i.id], meta, prev.get(i.id), g.id)
                group_rows.append(row)
                rows.append(row)
                everything.append(row)
                if i.is_direct:
                    direct.append(row)
            if group_rows:
                leaves = [lf for i in g.items for lf in i.leaves]
                gmeta = _TotalMeta(d.id, g.code, _mix(leaves))
                totals.append(_total("group", g.id, g.name, group_rows, bands, gmeta))
                disc_rows += group_rows
        if disc_rows:
            leaves = [lf for g in d.groups for i in g.items for lf in i.leaves]
            dmeta = _TotalMeta(None, d.code, _mix(leaves))
            name = d.name or "Disiplinsiz"
            totals.append(_total("discipline", d.id, name, disc_rows, bands, dmeta))
    totals.append(_total("direct_total", None, "Σ Doğrudan", direct, bands))
    totals.append(_total("all_total", None, "Σ Doğrudan + Dolaylı", everything, bands))
    return rows, totals


def tree_lines(report: QurrReport) -> list[QurrRow | QurrTotal]:
    """`build_rows` agacini TEK duz listeye acar (EV-BORC-7, Excel): her grup toplaminin
    ONUNE o grubun kalemleri. Yeniden SIRALAMAZ — `rows` ve `totals` zaten `build_rows`un
    agac sirasindadir; kalem gruba `parent_id` (S1) ile baglanir."""
    by_group: dict[str | None, list[QurrRow]] = {}
    for row in report.rows:
        by_group.setdefault(row.parent_id, []).append(row)
    lines: list[QurrRow | QurrTotal] = []
    for total in report.totals:
        if total.kind == "group":
            lines += by_group.pop(total.node_id, [])
        lines.append(total)
    if by_group:
        raise ValueError(f"QURR: grup toplami olmayan kalem(ler): {sorted(map(str, by_group))}")
    return lines


def _kpis(report: DailyReport) -> list[KpiPf]:
    out = []
    for kind, scope in (
        (RowKind.OVERALL_OWN, "overall_own"),
        (RowKind.OVERALL_SUBCON, "overall_subcon"),
    ):
        r = report.row(kind)
        out.append(
            KpiPf(
                scope=scope,  # type: ignore[arg-type]
                pf_cum=r.pf_cum,
                pf_week=r.pf_week,
                pf_cum_band=r.pf_cum_band,
                pf_week_band=r.pf_week_band,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class CompositeValue:
    unit: str | None
    actual: Decimal | None
    planned: Decimal | None
    deviation: Decimal | None
    numerator_names: list[str]
    denominator_name: str | None


def composite_value(
    site: SiteInput,
    report: DailyReport,
    measure: str,
    numerator_item_ids: Iterable[uuid.UUID],
    denominator_item_id: uuid.UUID,
) -> CompositeValue:
    """B3-3 TEK formul: gercek = Σ pay (olcuye gore) ÷ payda qty_cum · planli = Σ pay
    butcesi ÷ payda planli miktar · sapma = (gercek − planli) ÷ planli. Kalem baseline'da
    yoksa (ya da payda miktarsizsa) deger None."""
    attr = {"spent": "spent_cum", "earned": "earned_cum", "budget": "budget_mhr"}[measure]
    den_id = f"i:{denominator_item_id}"
    uoms = {i.id: i.uom for d in site.tree.disciplines for g in d.groups for i in g.items}
    terms = [report.nodes[f"i:{t}"] for t in numerator_item_ids if f"i:{t}" in report.nodes]
    den = report.nodes.get(den_id)
    actual = planned = None
    if den is not None and terms:
        actual = _ratio(sum((getattr(t, attr) for t in terms), ZERO), den.qty_cum)
        planned = _ratio(sum((t.budget_mhr for t in terms), ZERO), den.planned_qty)
    deviation = _ratio(actual - planned, planned) if actual is not None and planned else None
    names = {i.id: i.description for d in site.tree.disciplines for g in d.groups for i in g.items}
    return CompositeValue(
        uoms.get(den_id),
        actual,
        planned,
        deviation,
        [names[f"i:{t}"] for t in numerator_item_ids if f"i:{t}" in names],
        names.get(den_id),
    )


def composite_hidden(
    site: SiteInput, numerator_item_ids: Iterable[uuid.UUID], denominator_item_id: uuid.UUID
) -> bool:
    """S3: pay/payda terimlerinden biri tam agacta var ama kapsama gorunmuyor (kisitsizda False)."""
    return bool(site.hidden_items.intersection([*numerator_item_ids, denominator_item_id]))


def composite_cards(site: SiteInput, report: DailyReport, metrics) -> list[CompositeCard]:  # noqa: ANN001
    cards = []
    for cm in metrics:
        if composite_hidden(site, cm.numerator_item_ids, cm.denominator_item_id):
            continue  # S3: gorunmeyen terimli kart kisitliya DUSER
        v = composite_value(site, report, cm.measure, cm.numerator_item_ids, cm.denominator_item_id)
        cards.append(
            CompositeCard(
                id=cm.id,
                name=cm.name,
                measure=cm.measure,
                unit=v.unit,
                actual=v.actual,
                planned=v.planned,
                deviation=v.deviation,
                numerator_names=v.numerator_names,
                denominator_name=v.denominator_name,
            )
        )
    return cards


def current_week(cal: ProjectCalendar) -> int:
    """§3.15 S6: `week` verilmezse BUGUNUN haftasi — takvime kirpilir (basindan once → 1,
    sonundan sonra → son hafta)."""
    return cal.week_no(min(max(today(), cal.start_date), cal.end_date))


async def build_qurr(
    session: AsyncSession, site_id: uuid.UUID, site: SiteInput, week_no: int | None
) -> QurrReport | None:
    cal = ProjectCalendar(site.inp.calendar)
    if week_no is None:
        week_no = current_week(cal)
    rng = week_range(cal, week_no)
    if rng is None:
        return None
    report_date = min(rng[1], today(), cal.end_date)
    report_date = max(report_date, rng[0])
    report = compute_daily_report(site.inp, report_date)
    prev_rev = await previous_revision(session, site_id, site.revision)
    rows, totals = build_rows(site, report, await previous_items(session, prev_rev))
    settings = await settings_service.get_settings(session, site_id)
    drafts = sorted(
        d
        for d, st in site.diary_status.items()
        if st is DiaryStatus.draft and rng[0] <= d <= report_date
    )
    return QurrReport(
        week_no=week_no,
        week_start=rng[0],
        week_end=rng[1],
        report_date=report_date,
        revision=revision_ref(site.revision),  # type: ignore[arg-type]
        previous_revision=revision_ref(prev_rev),
        draft_diary_dates=drafts,
        rows=rows,
        totals=totals,
        kpis=_kpis(report),
        composites=composite_cards(site, report, settings.composite_metrics),
        warnings=rw.pf_warnings(site.tree, report)
        + rw.overrun_warnings(site.tree, report)
        + rw.diary_warnings([], drafts)
        + rw.unrated_warnings(site.tree, report),
        generated_at=datetime.now(UTC),
        has_field_data=any(e.day <= report_date for e in site.inp.qty_entries)
        or any(e.day <= report_date for e in site.inp.hours_entries),
        pf_bands=pf_bands_out(site),
        calendar_start=cal.start_date,
        calendar_end=cal.end_date,
        last_week_no=cal.week_no(cal.end_date),
    )

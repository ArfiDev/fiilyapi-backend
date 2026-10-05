"""🔶 PLANLAMA ↔ SANTIYE GUNLUGU ADAPTORU — PLANLAMA-SPEC §2.7'nin TEK ISARETLI dosyasi.

Kacinilmaz iki temas burada toplanir, baska yerde YOKTUR:
1. **Gunluk "Gonder" on-kosulu** (§2, §3.12 B2-3/B2-4/B2-8) — `submit_blockers`
   (kodlu `SubmitReason`).
2. **Gunluk ekranindaki saat dagitim bolumu** (§2) — kaynak `source_rows`, kayit
   `save_allocation`; gorunum `day_view.build_view`.
Ayrica rapor onayinin gunluk + puantaj KILIDI (§2, B2-6) — `day_lock` (+ kilidi koyan
rapor tarihi `lock_report_date`).

Cekirdek bu dosyayi GORMEZ: `app.core.day_hooks` portunu cagirir, bu dosya `register()`
ile porta kaydolur. 🔴 Kayit YERI `day_router.py` import yan etkisidir (`adp.register()`);
day_router kaydi dusurulurse cekirdek kilitsiz/on-kosulsuz calisir — bekcisi
`tests/earned_value_budget/test_day_integration.py` (port kayitli). Modul kurulu degilse port
bos kalir ve cekirdek bugunku gibi calisir.

## EV kurallari YALNIZ aktif baseline'li santiyede (B2-3)
Donmus baseline yoksa Gonder on-kosulu YOKTUR ve dagitim yazilamaz (kod agaci yok).

## Saat kaynaklari (B2-5)
* Kisi satiri = o gun o santiyede PUANTAJI olan HER kisi (kaynagi/firmasi ile), saat
  puantajdan SALT OKUNUR.
* Taseron satiri = gunlukteki firma satiri (kisi × saat) — puantaji tutulmayan ekipler.
  Ayni firma puantajda da varsa UYARI (engel degil).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import day_hooks
from app.core.access import AccessLevel
from app.core.day_hooks import SubmitContext, SubmitReason
from app.core.discipline_scope import UNRESTRICTED, DisciplineScope, user_scope
from app.core.errors import ConflictError, EarnedValueValidationError
from app.core.page_gate import gate_ok
from app.modules.contracts.models import Subcontractor
from app.modules.earned_value import budget_repository as repo
from app.modules.earned_value import guards
from app.modules.earned_value.access import PERMISSION_MODULE, assert_site_writable
from app.modules.earned_value.budget_scope import node_ids, prune_tree
from app.modules.earned_value.budget_snapshot import frozen_tree
from app.modules.earned_value.budget_tree import (
    BudgetTree,
    discipline_node_id,
    group_node_id,
    item_node_id,
    leaf_node_id,
)
from app.modules.earned_value.models import (
    ALLOCATION_RULE_VALUES,
    EvBaselineLeaf,
    EvDayCell,
    EvDayCode,
    EvDayNote,
    EvDayRow,
    EvDayUnlock,
    EvGroupDiscipline,
    EvReportApproval,
    EvRevision,
    RevisionStatus,
)
from app.modules.site_diary.models import (
    DiaryStatus,
    SiteDiaryEntry,
    SiteDiaryLine,
    SiteDiaryWorkerCount,
)
from app.modules.timesheet import repository as timesheet_repository
from app.modules.users.models import User

ZERO = Decimal(0)

LOCKED_MESSAGE = "Bu gün {report} tarihli ilerleme raporuyla kilitli"
NO_BASELINE = "Şantiyede aktif (dondurulmuş) baseline yok"
UNKNOWN_CODE = "İş kodu aktif baseline'da yok: {node}"
UNRATED_CODE = "Oransız yaprak iş kodu olamaz: {node}"
UNKNOWN_ROW = "Dağıtım satırı bu günün puantajında/taşeron kaydında yok"
CELL_CODE_MISSING = "Hücrenin iş kodu gün kodlarında yok: {node}"
NOT_LOCKED = "Bu gün kilitli değil"


async def _assert_site_writable(session: AsyncSession, site_id: uuid.UUID) -> None:
    """Tamamlanmis santiyede gun yazmalari (dagitim, kilit acma) SALT OKUNUR — TEK kural."""
    await assert_site_writable(session, site_id, message=guards.SITE_COMPLETED_DAY_READ_ONLY)


# ------------------------------------------------------------------ kilit (B2-6)


@dataclass(frozen=True, slots=True)
class LockState:
    approval: EvReportApproval | None  # kilidi koyan (en son) onay
    unlock: EvDayUnlock | None  # onu ezen gun istisnasi

    @property
    def locked(self) -> bool:
        return self.approval is not None and self.unlock is None


async def lock_state(session: AsyncSession, site_id: uuid.UUID, day: date) -> LockState:
    """Kilit = `report_date >= gun` olan EN SON onay; o onaydan SONRA acilmis gun
    istisnasi varsa gun acik (istisna eski onayi ezer, yeniden onay istisnayi ezer)."""
    approval = (
        await session.execute(
            select(EvReportApproval)
            .where(EvReportApproval.site_id == site_id, EvReportApproval.report_date >= day)
            .order_by(EvReportApproval.approved_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if approval is None:
        return LockState(None, None)
    unlock = (
        await session.execute(
            select(EvDayUnlock)
            .where(
                EvDayUnlock.site_id == site_id,
                EvDayUnlock.day == day,
                EvDayUnlock.unlocked_at > approval.approved_at,
            )
            .order_by(EvDayUnlock.unlocked_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return LockState(approval, unlock)


async def day_lock(session: AsyncSession, site_id: uuid.UUID, day: date) -> str | None:
    """`day_hooks.DayLockCheck` uygulamasi."""
    state = await lock_state(session, site_id, day)
    if not state.locked:
        return None
    return LOCKED_MESSAGE.format(report=state.approval.report_date.strftime("%d.%m.%Y"))  # type: ignore[union-attr]


async def lock_report_date(session: AsyncSession, site_id: uuid.UUID, day: date) -> date | None:
    """`day_hooks.DayLockReport` uygulamasi: kilidi KOYAN (en son) onayin rapor tarihi."""
    state = await lock_state(session, site_id, day)
    return state.approval.report_date if state.locked else None  # type: ignore[union-attr]


async def unlock_day(
    session: AsyncSession, site_id: uuid.UUID, day: date, actor: User, reason: str
) -> EvDayUnlock:
    await _assert_site_writable(session, site_id)
    if not (await lock_state(session, site_id, day)).locked:
        raise ConflictError(NOT_LOCKED)
    row = EvDayUnlock(site_id=site_id, day=day, reason=reason.strip(), unlocked_by_user_id=actor.id)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


# ------------------------------------------------------------------ kaynaklar


async def active_revision(session: AsyncSession, site_id: uuid.UUID) -> EvRevision | None:
    return await repo.revision_by_status(session, site_id, RevisionStatus.ACTIVE)


async def active_tree(session: AsyncSession, site_id: uuid.UUID) -> BudgetTree | None:
    rev = await active_revision(session, site_id)
    if rev is None:
        return None
    calendar = await repo.load_calendar(session, site_id)
    return await frozen_tree(
        session, rev, await repo.load_disciplines(session), calendar.is_working_day
    )


async def visible_node_ids(
    session: AsyncSession, site_id: uuid.UUID, scope: DisciplineScope
) -> frozenset[str]:
    """Kisitli kapsamda AKTIF baseline'in gorunur dugum kimlikleri (`d:`/`g:`/`i:`/`l:`),
    agaci KURMADAN tek sorguyla — `active_tree` + `budget_scope.prune_tree(frozen)` ile AYNI
    kume, AYNI kuraldan: d: koku CANLI `ev_group_disciplines` (revizyonun kendi esleme girdisi)
    uzerinden grubun disiplinidir (`build_tree` ile ayni kaynak); fotograftaki
    `EvBaselineLeaf.discipline_id` KULLANILMAZ (dondurma sonrasi silinen grubun esleme satiri
    da gider → yapraklari d:none olur, budanir). Eslemesiz/tanimsiz → hicbir kapsamda degil
    (fail-closed)."""
    stmt = (
        select(
            EvGroupDiscipline.discipline_id,
            EvBaselineLeaf.boq_group_id,
            EvBaselineLeaf.boq_item_id,
            EvBaselineLeaf.section_id,
        )
        .join(EvRevision, EvRevision.id == EvBaselineLeaf.revision_id)
        .join(
            EvGroupDiscipline,
            and_(
                EvGroupDiscipline.revision_id == EvBaselineLeaf.revision_id,
                EvGroupDiscipline.boq_group_id == EvBaselineLeaf.boq_group_id,
            ),
        )
        .where(
            EvRevision.site_id == site_id,
            EvRevision.status == RevisionStatus.ACTIVE,
            EvGroupDiscipline.discipline_id.in_(sorted(scope.discipline_ids or (), key=str)),
        )
    )
    out: set[str] = set()
    for disc, group, item, section in (await session.execute(stmt)).all():
        out.update(
            (
                discipline_node_id(disc),
                group_node_id(group),
                item_node_id(item),
                leaf_node_id(item, section),
            )
        )
    return frozenset(out)


@dataclass(frozen=True, slots=True)
class SourceRow:
    """Dagitim izgarasinin bir satiri (canli kaynak)."""

    kind: str  # personnel | subcontractor
    ref_id: uuid.UUID
    label: str
    trade: str | None
    source: str | None
    subcontractor_id: uuid.UUID | None
    subcontractor_name: str | None
    headcount: int | None  # yalniz taseron
    hours: Decimal  # canli saat: puantaj ya da kisi × saat


async def diary_entry(
    session: AsyncSession, site_id: uuid.UUID, day: date
) -> SiteDiaryEntry | None:
    return (
        await session.execute(
            select(SiteDiaryEntry).where(
                SiteDiaryEntry.site_id == site_id, SiteDiaryEntry.entry_date == day
            )
        )
    ).scalar_one_or_none()


async def source_rows(session: AsyncSession, site_id: uuid.UUID, day: date) -> list[SourceRow]:
    people = await timesheet_repository.day_person_hours(session, site_id, day)
    rows = [
        SourceRow(
            kind="personnel",
            ref_id=p.id,
            label=p.full_name,
            trade=p.trade,
            source=getattr(p.source, "value", p.source),
            subcontractor_id=p.subcontractor_id,
            subcontractor_name=sub.name if sub else None,
            headcount=None,
            hours=ts.hours,
        )
        for ts, p, sub in people
    ]
    entry = await diary_entry(session, site_id, day)
    if entry is not None:
        rows += await _subcontractor_rows(session, entry.id)
    return rows


async def _subcontractor_rows(session: AsyncSession, entry_id: uuid.UUID) -> list[SourceRow]:
    """Gunlukteki taseron firma satiri (B2.1 cekirdek alanlari: subcontractor_id + hours)."""
    sub_col = getattr(SiteDiaryWorkerCount, "subcontractor_id", None)
    hours_col = getattr(SiteDiaryWorkerCount, "hours", None)
    if sub_col is None or hours_col is None:  # cekirdek alanlari henuz yoksa
        return []
    result = await session.execute(
        select(SiteDiaryWorkerCount, Subcontractor)
        .join(Subcontractor, Subcontractor.id == sub_col)
        .where(SiteDiaryWorkerCount.entry_id == entry_id, hours_col.is_not(None))
        .order_by(Subcontractor.name)
    )
    return [
        SourceRow(
            kind="subcontractor",
            ref_id=sub.id,
            label=sub.name,
            trade=wc.trade,
            source="subcontractor",
            subcontractor_id=sub.id,
            subcontractor_name=sub.name,
            headcount=wc.count,
            hours=Decimal(wc.count) * wc.hours,
        )
        for wc, sub in result.all()
    ]


def duplicate_firm_warnings(rows: Sequence[SourceRow]) -> list[str]:
    """B2-5: ayni firma hem puantajda hem gunluk taseron satirinda → uyari."""
    in_timesheet = {
        r.subcontractor_id for r in rows if r.kind == "personnel" and r.subcontractor_id
    }
    return [
        f"{r.label}: firma hem puantajda hem taşeron satırında — çift sayım olabilir"
        for r in rows
        if r.kind == "subcontractor" and r.ref_id in in_timesheet
    ]


# ------------------------------------------------------------------ dagitim


@dataclass(frozen=True, slots=True)
class SavedDay:
    codes: list[EvDayCode]
    rows: list[EvDayRow]
    cells: list[EvDayCell]
    note: EvDayNote | None


async def load_saved(session: AsyncSession, site_id: uuid.UUID, day: date) -> SavedDay:
    codes = (
        await session.execute(
            select(EvDayCode)
            .where(EvDayCode.site_id == site_id, EvDayCode.day == day)
            .order_by(EvDayCode.sort_order, EvDayCode.node_id)
        )
    ).scalars()
    rows = list(
        (
            await session.execute(
                select(EvDayRow).where(EvDayRow.site_id == site_id, EvDayRow.day == day)
            )
        ).scalars()
    )
    cells = (
        (
            await session.execute(
                select(EvDayCell).where(EvDayCell.row_id.in_([r.id for r in rows]))
            )
        ).scalars()
        if rows
        else []
    )
    note = await session.get(EvDayNote, (site_id, day))
    return SavedDay(list(codes), rows, list(cells), note)


def allocated_hours(saved: SavedDay) -> Decimal:
    return sum((c.hours for c in saved.cells), ZERO)


@dataclass(frozen=True, slots=True)
class CellIn:
    kind: str
    ref_id: uuid.UUID
    node_id: str
    hours: Decimal


def _node_index(tree: BudgetTree) -> dict[str, bool | None]:
    """dugum kimligi → yaprak mi oranli mi (baslik: None)."""
    out: dict[str, bool | None] = {}
    for d in tree.disciplines:
        out[d.id] = None
        for g in d.groups:
            out[g.id] = None
            for i in g.items:
                out[i.id] = None
                for lf in i.leaves:
                    out[lf.id] = lf.is_rated
    return out


async def _assert_writable(session: AsyncSession, site_id: uuid.UUID, day: date) -> None:
    await _assert_site_writable(session, site_id)
    await day_hooks.assert_days_unlocked(session, site_id, [day])


async def save_allocation(
    session: AsyncSession,
    site_id: uuid.UUID,
    day: date,
    actor: User,
    codes: list[tuple[str, str]],
    cells: list[CellIn],
    unallocated_reason: str | None,
    scope: DisciplineScope = UNRESTRICTED,
    reason_provided: bool = True,
) -> None:
    """Gunun dagitimi. KISITSIZ: TAM DEGISTIRME (kodlar + hucreler + gerekce) — bugunku yol.

    KISITLI (DSC-B2): BIRLESTIRME — yalniz kendi gorunur dugumlerine yazar, baska
    disiplinin kod/hucrelerine DOKUNMAZ (ayrintisi `_merge_scoped`). Gorunur kume kilit
    ALINDIKTAN SONRA (sites FOR UPDATE, `_assert_writable`) okunur → gunun ilk kaydinda iki
    disiplin es zamanli yazsa da paylasilan satir tek kalir, kayip yazma olmaz.

    S7: kisitli + kisitsiz eszamanli yazma "son yazan kazanir" (kisitsiz TAM degistirir).
    S3: kisitli kayit da paylasilan satirin `source_hours`ini CANLI degere yeniler (bugun her
    kayit yeniliyor; baska disiplinin gorunmeyen hucreleri eski saate gore girilmis olabilir —
    "puantaj degisti" uyarisinin kaybi yeni bir sinif DEGIL).
    S4: ortak `unallocated_reason` kisitlida govdede YOKSA (`reason_provided=False`) dokunulmaz;
    kisitsizda bugunku "yoksa temizle".
    """
    await _assert_writable(session, site_id, day)
    tree = await active_tree(session, site_id)
    if tree is None:
        raise ConflictError(NO_BASELINE)
    index = _node_index(tree)
    visible: frozenset[str] | None = None
    if scope.is_restricted:
        visible = node_ids(prune_tree(tree, scope, None, True))
    for node_id, rule in codes:
        if rule not in ALLOCATION_RULE_VALUES:
            raise EarnedValueValidationError(f"Geçersiz dağıtım kuralı: {rule}")
        # Ü7: kapsam disi kod, olmayanla AYNI 422 (kimlik sizmaz).
        if node_id not in index or (visible is not None and node_id not in visible):
            raise EarnedValueValidationError(UNKNOWN_CODE.format(node=node_id))
        if index[node_id] is False:
            raise EarnedValueValidationError(UNRATED_CODE.format(node=node_id))
    code_ids = {n for n, _ in codes}
    live = {(r.kind, r.ref_id): r for r in await source_rows(session, site_id, day)}
    for c in cells:
        if (c.kind, c.ref_id) not in live:
            raise EarnedValueValidationError(UNKNOWN_ROW)
        if c.node_id not in code_ids:
            raise EarnedValueValidationError(CELL_CODE_MISSING.format(node=c.node_id))
    if visible is not None:
        await _merge_scoped(
            session,
            site_id,
            day,
            actor,
            codes,
            cells,
            unallocated_reason,
            reason_provided,
            visible,
            live,
        )
        return
    await session.execute(
        delete(EvDayCode).where(EvDayCode.site_id == site_id, EvDayCode.day == day)
    )
    await session.execute(delete(EvDayRow).where(EvDayRow.site_id == site_id, EvDayRow.day == day))
    session.add_all(
        EvDayCode(site_id=site_id, day=day, node_id=n, rule=r, sort_order=k)
        for k, (n, r) in enumerate(codes)
    )
    row_ids: dict[tuple[str, uuid.UUID], uuid.UUID] = {}
    for key, src in live.items():
        row = _new_row(site_id, day, src)
        session.add(row)
        row_ids[key] = row.id
    await session.flush()
    session.add_all(_cell_rows(cells, row_ids, None))
    note = await session.get(EvDayNote, (site_id, day))
    reason = (unallocated_reason or "").strip() or None
    if note is None:
        session.add(
            EvDayNote(
                site_id=site_id, day=day, unallocated_reason=reason, updated_by_user_id=actor.id
            )
        )
    else:
        note.unallocated_reason = reason
        note.updated_by_user_id = actor.id
    await session.flush()


def _new_row(site_id: uuid.UUID, day: date, src: SourceRow) -> EvDayRow:
    return EvDayRow(
        id=uuid.uuid4(),
        site_id=site_id,
        day=day,
        kind=src.kind,
        personnel_id=src.ref_id if src.kind == "personnel" else None,
        subcontractor_id=src.ref_id if src.kind == "subcontractor" else None,
        source_hours=src.hours,
    )


def _cell_rows(
    cells: list[CellIn],
    row_ids: dict[tuple[str, uuid.UUID], uuid.UUID],
    only_nodes: frozenset[str] | None,
) -> list[EvDayCell]:
    """Ayni (satir, kod) hucreleri toplanir; saat 0 olan yazilmaz."""
    merged: dict[tuple[uuid.UUID, str], Decimal] = {}
    for c in cells:
        if only_nodes is not None and c.node_id not in only_nodes:
            continue
        key = (row_ids[(c.kind, c.ref_id)], c.node_id)
        merged[key] = merged.get(key, ZERO) + c.hours
    return [EvDayCell(row_id=r, node_id=n, hours=h) for (r, n), h in merged.items() if h > 0]


async def _merge_scoped(
    session: AsyncSession,
    site_id: uuid.UUID,
    day: date,
    actor: User,
    codes: list[tuple[str, str]],
    cells: list[CellIn],
    unallocated_reason: str | None,
    reason_provided: bool,
    visible: frozenset[str],
    live: dict[tuple[str, uuid.UUID], SourceRow],
) -> None:
    """Kisitli BIRLESTIRME (kilit ALINMIS; kayitli durum kilit SONRASI okunur).

    * KODLAR: gorunur kumede olup govdede olmayan silinir, govdedekiler upsert; gizli kodlar
      dokunulmaz.
    * SATIRLAR (kisi basina paylasilan): canli kaynakta olanin id'si KORUNUR, source_hours
      canliya cekilir (S3); canlida olmayan satir yalniz GIZLI hucresi yoksa silinir.
    * HUCRELER: yalniz gorunur dugumlerdekiler silinip govdeden eklenir; gizliler dokunulmaz.
    * NOT (S4): govdede yoksa dokunulmaz.
    """
    saved = await load_saved(session, site_id, day)
    by_code = {c.node_id: c for c in saved.codes}
    body_ids = {n for n, _ in codes}
    stale = [n for n in by_code if n in visible and n not in body_ids]
    if stale:
        await session.execute(
            delete(EvDayCode).where(
                EvDayCode.site_id == site_id, EvDayCode.day == day, EvDayCode.node_id.in_(stale)
            )
        )
    # Bilinen sonuc: kisitli kodlarin sort_order'i 0..n-1 — gizli kodlarla AYNI deger paylasabilir;
    # kisitsiz gorunumde esitlik node_id ile kirilir (`load_saved` ORDER BY sort_order, node_id).
    for k, (n, r) in enumerate(codes):
        existing = by_code.get(n)
        if existing is None:
            session.add(EvDayCode(site_id=site_id, day=day, node_id=n, rule=r, sort_order=k))
        else:
            existing.rule = r
            existing.sort_order = k
    hidden_rows = {c.row_id for c in saved.cells if c.node_id not in visible}
    row_ids: dict[tuple[str, uuid.UUID], uuid.UUID] = {}
    for row in saved.rows:
        key = (row.kind, row.personnel_id or row.subcontractor_id)
        src = live.get(key)  # type: ignore[arg-type]
        if src is not None:
            row.source_hours = src.hours
            row_ids[key] = row.id  # type: ignore[assignment]
        elif row.id not in hidden_rows:
            await session.execute(delete(EvDayRow).where(EvDayRow.id == row.id))
    for key, src in live.items():
        if key not in row_ids:
            row = _new_row(site_id, day, src)
            session.add(row)
            row_ids[key] = row.id
    await session.flush()
    # Gorunur hucre silmesi TUM kayitli satirlarda (canlidan dusmus satir dahil): kisitlinin
    # kendi eski hucresi canlida olmayan satirda yetim kalmasin; gizli hucreler dokunulmaz.
    all_row_ids = {r.id for r in saved.rows}
    if all_row_ids:
        await session.execute(
            delete(EvDayCell).where(
                EvDayCell.row_id.in_(all_row_ids), EvDayCell.node_id.in_(sorted(visible))
            )
        )
    session.add_all(_cell_rows(cells, row_ids, visible))
    if reason_provided:
        await _set_note(session, site_id, day, actor, unallocated_reason)
    await session.flush()


async def _set_note(
    session: AsyncSession, site_id: uuid.UUID, day: date, actor: User, value: str | None
) -> None:
    note = await session.get(EvDayNote, (site_id, day))
    reason = (value or "").strip() or None
    if note is None:
        session.add(
            EvDayNote(
                site_id=site_id, day=day, unallocated_reason=reason, updated_by_user_id=actor.id
            )
        )
    else:
        note.unallocated_reason = reason
        note.updated_by_user_id = actor.id


async def previous_submitted_day(
    session: AsyncSession, site_id: uuid.UUID, day: date
) -> date | None:
    """B2-7: `day`den ONCEKI, gunlugu GONDERILMIS ve dagitimi olan en son gun."""
    return await session.scalar(
        select(func.max(EvDayRow.day))
        .join(
            SiteDiaryEntry,
            and_(
                SiteDiaryEntry.site_id == EvDayRow.site_id,
                SiteDiaryEntry.entry_date == EvDayRow.day,
            ),
        )
        .where(
            EvDayRow.site_id == site_id,
            EvDayRow.day < day,
            SiteDiaryEntry.status == DiaryStatus.submitted,
        )
    )


# ------------------------------------------------------------------ Gonder on-kosulu


async def _overrun_leaf_keys(
    session: AsyncSession,
    site_id: uuid.UUID,
    entry: SiteDiaryEntry,
    tree: BudgetTree,
    *,
    first_only: bool,
) -> list[tuple[uuid.UUID, uuid.UUID | None]]:
    """Gerekcesiz planli-miktar asimi olan gunluk satirlarinin (kalem, bolum) anahtarlari
    (satir basina bir). `first_only` → ilkinde durur (kisitsiz yolun bugunku erken cikisi)."""
    planned = {(lf.item_id, lf.section_id): lf.planned_qty for *_, lf in tree.leaves()}
    section_col = getattr(SiteDiaryLine, "section_id", None)
    reason_col = getattr(SiteDiaryLine, "overrun_reason", None)
    if section_col is None or reason_col is None:
        return []
    lines = (
        (await session.execute(select(SiteDiaryLine).where(SiteDiaryLine.entry_id == entry.id)))
        .scalars()
        .all()
    )
    out: list[tuple[uuid.UUID, uuid.UUID | None]] = []
    for line in lines:
        key = (line.boq_item_id, line.section_id)
        if line.boq_item_id is None or key not in planned or (line.overrun_reason or "").strip():
            continue
        previous = await session.scalar(
            select(func.coalesce(func.sum(SiteDiaryLine.quantity), 0))
            .join(SiteDiaryEntry, SiteDiaryEntry.id == SiteDiaryLine.entry_id)
            .where(
                SiteDiaryEntry.site_id == site_id,
                SiteDiaryEntry.status == DiaryStatus.submitted,
                SiteDiaryEntry.entry_date < entry.entry_date,
                SiteDiaryLine.boq_item_id == line.boq_item_id,
                section_col.is_(None)
                if line.section_id is None
                else section_col == line.section_id,
            )
        )
        if Decimal(previous) + line.quantity > planned[key]:
            out.append(key)
            if first_only:
                break
    return out


async def _has_line_overrun_without_reason(
    session: AsyncSession, site_id: uuid.UUID, entry: SiteDiaryEntry, tree: BudgetTree
) -> bool:
    return bool(await _overrun_leaf_keys(session, site_id, entry, tree, first_only=True))


def _weather_complete(entry: SiteDiaryEntry) -> bool:
    fields = ("weather", "temp_min_c", "temp_max_c", "wind_ms")
    return all(getattr(entry, f, None) is not None for f in fields)


#: Gonder engeli KODLARI (EV-BORC-2; istemci metne degil koda bakar — sozlesme).
SUBMIT_NO_PERMISSION = "no_planning_permission"
SUBMIT_WEATHER = "weather_incomplete"
SUBMIT_NO_QUANTITY = "no_quantity"
SUBMIT_OVERRUN = "overrun_without_reason"
SUBMIT_UNDISTRIBUTED = "undistributed_hours"
OVERRUN_MESSAGE = "Planlı miktarı aşan satır gerekçesiz"
OVERRUN_OTHER_MESSAGE = "Başka disiplinde {count} satır planlı miktarı aşıyor (gerekçesiz)"


async def _overrun_reasons(
    session: AsyncSession, ctx: SubmitContext, entry: SiteDiaryEntry, tree: BudgetTree
) -> list[SubmitReason]:
    """Asim engeli. KISITSIZ: bugunku bool yolu (tek madde). KISITLI (Ü5/S8): kendi satirinda
    asim varsa mevcut metin; ARDINDAN gorunmeyen (baska disiplin/eslemesiz) gerekceli olmayan
    asim varsa AYNI kod + OPAK madde (kalem adi/kodu YOK, yalniz satir sayisi). Sira: once kendi."""
    scope = await user_scope(session, ctx.actor_id, entry.project_id)  # IZN-B3: proje basina
    if not scope.is_restricted:
        if await _has_line_overrun_without_reason(session, ctx.site_id, entry, tree):
            return [SubmitReason(SUBMIT_OVERRUN, OVERRUN_MESSAGE)]
        return []
    visible = node_ids(prune_tree(tree, scope, None, True))
    keys = await _overrun_leaf_keys(session, ctx.site_id, entry, tree, first_only=False)
    hidden = [k for k in keys if leaf_node_id(*k) not in visible]
    out: list[SubmitReason] = []
    if len(hidden) < len(keys):
        out.append(SubmitReason(SUBMIT_OVERRUN, OVERRUN_MESSAGE))
    if hidden:
        out.append(SubmitReason(SUBMIT_OVERRUN, OVERRUN_OTHER_MESSAGE.format(count=len(hidden))))
    return out


async def submit_blockers(session: AsyncSession, ctx: SubmitContext) -> list[SubmitReason]:
    """`day_hooks.SubmitGuard` uygulamasi (B2-3: yalniz aktif baseline'li santiye)."""
    tree = await active_tree(session, ctx.site_id)
    if tree is None:
        return []
    reasons: list[SubmitReason] = []
    actor = await session.get(User, ctx.actor_id)
    gunluk = await session.get(SiteDiaryEntry, ctx.entry_id)
    # IZN-B3: planlama yazma yetkisi O PROJEDEKİ rolle ölçülür (günlüğün projesi); bağlama yazmaz.
    if actor is None or not await gate_ok(
        session,
        actor,
        PERMISSION_MODULE,
        AccessLevel.draft,
        project_id=gunluk.project_id if gunluk is not None else None,
        record=False,
    ):
        reasons.append(
            SubmitReason(
                SUBMIT_NO_PERMISSION,
                "Günlüğü göndermek planlama yazma yetkisi ister (formen gönderemez)",
            )
        )
    entry = await session.get(SiteDiaryEntry, ctx.entry_id)
    if entry is None:
        return reasons
    if not _weather_complete(entry):
        reasons.append(
            SubmitReason(SUBMIT_WEATHER, "Hava bilgisi eksik (durum, min/max sıcaklık, rüzgâr)")
        )
    line_count = await session.scalar(
        select(func.count()).select_from(SiteDiaryLine).where(SiteDiaryLine.entry_id == entry.id)
    )
    if not line_count:
        reasons.append(SubmitReason(SUBMIT_NO_QUANTITY, "Miktar girilmedi"))
    else:
        reasons.extend(await _overrun_reasons(session, ctx, entry, tree))
    source = sum((r.hours for r in await source_rows(session, ctx.site_id, ctx.entry_date)), ZERO)
    saved = await load_saved(session, ctx.site_id, ctx.entry_date)
    unallocated = source - allocated_hours(saved)
    reason = saved.note.unallocated_reason if saved.note else None
    if unallocated != 0 and not (reason or "").strip():
        reasons.append(
            SubmitReason(SUBMIT_UNDISTRIBUTED, f"{unallocated} a-s dağıtılmamış; gerekçe gerekli")
        )
    return reasons


def register() -> None:
    """Porta kaydol (idempotent). `earned_value.router` import edilince cagrilir."""
    day_hooks.register_day_lock(day_lock, report_date=lock_report_date)
    day_hooks.register_submit_guard(submit_blockers)

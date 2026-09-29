"""Butce revizyon yasam dongusu + taslak yazmalari (§3.9 B1-5, B1-6).

* Ilk yazmada Rev 0 taslagi KENDILIGINDEN dogar — yalniz santiyede HIC revizyon yoksa.
  Aktif revizyon varken taslaksiz yazma 409'dur (once "taslak ac").
* Ayni anda tek taslak (kismi UQ + santiye satiri kilidi). Donmus revizyon DEGISMEZ.
* Taslak CANLI BOQ miktarini okur; yaprak dogrulamasi canli agactan yapilir.

🔴 Eszamanlilik: her yazma `_lock_site` ile santiye satirini `FOR UPDATE` kilitler.
Kilitsiz iki esanli "ilk yazma" iki Rev 0 acmaya calisir; kismi UQ ikincisini
IntegrityError ile keserdi (500). Kilit onu siraya sokar ve ikinci yazma ayni taslagi gorur.
Bekci: `tests/earned_value_budget/test_budget_concurrency.py` (B1-13).

* Tamamlanmis santiyede butce yazmalari SALT OKUNUR (409, §3.11 B1-12). Kural TEK yerde:
  `access.assert_site_writable` (kilit + durum kilit altinda, TEK sorgu). Buradaki giris
  `_writable_site`dir; taslak yolu `_draft_for_write` uzerinden, taslak ac/sil ve dondur ile
  "bosları doldur" dogrudan cagirir. Uclar ayrica `access.completed_site_guard` bagimliligiyla
  409'u govde dogrulamasindan ONCE verir (PLN-B3.0). Okumalar serbesttir.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import UNRESTRICTED, DisciplineScope, visible_item_set
from app.core.errors import ConflictError, EarnedValueValidationError, NotFoundError
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.earned_value import budget_repository as repo
from app.modules.earned_value import guards
from app.modules.earned_value.access import SiteContext, assert_site_writable, is_site_completed
from app.modules.earned_value.budget_scope import item_ids, prune_tree
from app.modules.earned_value.budget_snapshot import frozen_tree
from app.modules.earned_value.budget_tree import BudgetTree, LeafKey, RevisionInputs, build_tree
from app.modules.earned_value.models import (
    DISTRIBUTION_VALUES,
    EvCatalogItem,
    EvDiscipline,
    EvDistribution,
    EvGroupDiscipline,
    EvItemSettings,
    EvLeafSettings,
    EvRevision,
    EvWindow,
    RateSource,
    RevisionStatus,
)
from app.modules.sites.models import Section
from app.modules.users.models import User


@dataclass(frozen=True, slots=True)
class BudgetState:
    revision: EvRevision | None
    editable: bool  # taslak gorunumu (dondurulmus agac degil)
    tree: BudgetTree
    boq_synced_at: datetime | None
    site_completed: bool = False  # `access.is_site_completed` — ekranda editable=false


async def _lock_site(session: AsyncSession, site_id: uuid.UUID) -> None:
    """Santiye satiri kilidi + durum (kilit altinda) — TEK yardimciya devredilir.

    Ayri bir isim olarak kalir: B1-13 pozitif kontrolu (`test_budget_concurrency.py`) kilidi
    tam burada no-op'a cevirir.
    """
    await assert_site_writable(session, site_id, message=guards.SITE_COMPLETED_BUDGET_READ_ONLY)


async def _writable_site(session: AsyncSession, ctx: SiteContext) -> None:
    """Her butce YAZMASININ girisi: santiye satirini kilitler, tamamlanmissa 409 (B1-12).

    Durum `ctx.site`ten DEGIL, kilit altinda veritabanindan okunur (`ctx.site` kilitten once
    yuklendi). Bekci: `test_b30_relock_guard.py` (+ bayat `ctx` pozitif kontrolu).
    """
    await _lock_site(session, ctx.site.id)


async def get_revision(session: AsyncSession, ctx: SiteContext, rev_id: uuid.UUID) -> EvRevision:
    rev = await session.get(EvRevision, rev_id)
    if rev is None or rev.site_id != ctx.site.id:
        raise NotFoundError(guards.REVISION_MISSING)
    return rev


async def current_revision(session: AsyncSession, site_id: uuid.UUID) -> EvRevision | None:
    """Ekranin varsayilan revizyonu: taslak varsa taslak, yoksa aktif, yoksa None."""
    draft = await repo.revision_by_status(session, site_id, RevisionStatus.DRAFT)
    if draft is not None:
        return draft
    return await repo.revision_by_status(session, site_id, RevisionStatus.ACTIVE)


async def load_state(
    session: AsyncSession,
    ctx: SiteContext,
    revision_id: uuid.UUID | None = None,
    scope: DisciplineScope = UNRESTRICTED,
) -> BudgetState:
    """Butce durumu. `scope` kisitliysa agac `budget_scope.prune_tree` ile budanir (K5:
    donmus → `d:` koku; taslak → `d:` ∩ R(site)); kisitsizda agac degismez."""
    rev = (
        await get_revision(session, ctx, revision_id)
        if revision_id
        else await current_revision(session, ctx.site.id)
    )
    calendar = await repo.load_calendar(session, ctx.site.id)
    disciplines = await repo.load_disciplines(session)
    synced = await repo.boq_synced_at(session, ctx.site.id)
    completed = is_site_completed(ctx.site.status)
    if rev is not None and rev.status is not RevisionStatus.DRAFT:
        tree = await frozen_tree(session, rev, disciplines, calendar.is_working_day)
        tree = prune_tree(tree, scope, None, True)
        return BudgetState(rev, False, tree, synced, completed)
    inputs = await repo.load_inputs(session, rev.id) if rev else RevisionInputs()
    boq = await repo.load_boq(session, ctx.site.id)
    tree = build_tree(boq, disciplines, inputs, calendar.is_working_day)
    visible = await visible_item_set(session, scope, item_ids(tree))
    tree = prune_tree(tree, scope, visible, False, calendar.is_working_day)
    return BudgetState(rev, True, tree, synced, completed)


async def _draft_for_write(session: AsyncSession, ctx: SiteContext, actor: User) -> EvRevision:
    await _writable_site(session, ctx)
    draft = await repo.revision_by_status(session, ctx.site.id, RevisionStatus.DRAFT)
    if draft is not None:
        return draft
    if await repo.list_revisions(session, ctx.site.id):
        raise ConflictError(guards.NO_DRAFT)
    return await _new_revision(session, ctx.site.id, 0, actor)


async def _new_revision(
    session: AsyncSession, site_id: uuid.UUID, number: int, actor: User
) -> EvRevision:
    rev = EvRevision(
        site_id=site_id, number=number, status=RevisionStatus.DRAFT, created_by_user_id=actor.id
    )
    session.add(rev)
    await session.flush()
    return rev


async def _touch(session: AsyncSession, rev: EvRevision) -> None:
    """Taslagin `updated_at`i = "son duzenleme" (frontend istegi 5)."""
    rev.updated_at = datetime.now(UTC)
    await session.flush()


# ------------------------------------------------------------------ revizyonlar


async def open_draft(session: AsyncSession, ctx: SiteContext, actor: User) -> EvRevision:
    """ "Taslak revizyon ac": aktifin duzenlenebilir girdileri Rev N+1 taslagina kopyalanir."""
    await _writable_site(session, ctx)
    if await repo.revision_by_status(session, ctx.site.id, RevisionStatus.DRAFT):
        raise ConflictError(guards.DRAFT_EXISTS)
    revisions = await repo.list_revisions(session, ctx.site.id)
    number = max((r.number for r in revisions), default=-1) + 1
    draft = await _new_revision(session, ctx.site.id, number, actor)
    active = next((r for r in revisions if r.status is RevisionStatus.ACTIVE), None)
    if active is not None:
        await _copy_inputs(session, active.id, draft.id)
    return draft


async def _copy_inputs(session: AsyncSession, src: uuid.UUID, dst: uuid.UUID) -> None:
    for model in (EvGroupDiscipline, EvItemSettings, EvDistribution):
        rows = (await session.execute(select(model).where(model.revision_id == src))).scalars()
        for row in rows:
            data = {c.key: getattr(row, c.key) for c in model.__table__.columns}
            data["revision_id"] = dst
            session.add(model(**data))
    for model in (EvLeafSettings, EvWindow):
        rows = (await session.execute(select(model).where(model.revision_id == src))).scalars()
        for row in rows:
            data = {c.key: getattr(row, c.key) for c in model.__table__.columns if c.key != "id"}
            data["revision_id"] = dst
            session.add(model(**data))
    await session.flush()


async def delete_draft(session: AsyncSession, ctx: SiteContext, rev_id: uuid.UUID) -> EvRevision:
    await _writable_site(session, ctx)
    rev = await get_revision(session, ctx, rev_id)
    if rev.status is not RevisionStatus.DRAFT:
        raise ConflictError(guards.NOT_DRAFT)
    await session.delete(rev)
    await session.flush()
    return rev


# ------------------------------------------------------------------ yazmalar


async def _site_ids(session: AsyncSession, model, site_id: uuid.UUID, ids: set[uuid.UUID]):  # noqa: ANN001, ANN202
    if not ids:
        return set()
    rows = await session.execute(
        select(model.id).where(model.site_id == site_id, model.id.in_(ids))
    )
    return set(rows.scalars())


async def _existing_disciplines(session: AsyncSession, ids: set[uuid.UUID]) -> set[uuid.UUID]:
    if not ids:
        return set()
    rows = await session.execute(select(EvDiscipline.id).where(EvDiscipline.id.in_(ids)))
    return set(rows.scalars())


async def _require_disciplines(session: AsyncSession, ids: set[uuid.UUID]) -> None:
    if ids - await _existing_disciplines(session, ids):
        raise NotFoundError(guards.DISCIPLINE_MISSING)


async def set_group_disciplines(
    session: AsyncSession,
    ctx: SiteContext,
    actor: User,
    pairs: list[tuple[uuid.UUID, uuid.UUID | None]],
) -> int:
    """Kismi upsert: listelenen gruplar; `None` eslemeyi kaldirir ("Disiplinsiz")."""
    groups = {g for g, _ in pairs}
    if groups - await _site_ids(session, BoqGroup, ctx.site.id, groups):
        raise EarnedValueValidationError(guards.BOQ_GROUP_FOREIGN)
    await _require_disciplines(session, {d for _, d in pairs if d is not None})
    draft = await _draft_for_write(session, ctx, actor)
    for group_id, disc_id in pairs:
        row = await session.get(EvGroupDiscipline, (draft.id, group_id))
        if disc_id is None:
            if row is not None:
                await session.delete(row)
        elif row is None:
            session.add(
                EvGroupDiscipline(
                    revision_id=draft.id, boq_group_id=group_id, discipline_id=disc_id
                )
            )
        else:
            row.discipline_id = disc_id
    await _touch(session, draft)
    return len(pairs)


async def patch_item(
    session: AsyncSession,
    ctx: SiteContext,
    actor: User,
    item_id: uuid.UUID,
    changes: dict[str, object],
    scope: DisciplineScope = UNRESTRICTED,
) -> BoqItem:
    """Is tipi ayari (K3). `contractor_type=None` → disiplin varsayilanina DON (miras).

    KISITLI (DSC-B2): kalem BUDANMIS agacta aranir; yoksa bugunku "yok" 404 govdesi (Ü7). S1:
    kapsam disi disiplinin katalog kalemi govdede → 404 `CATALOG_ITEM_MISSING` (bugunku "yok"
    yaniti); kisitsizda birebir bugunku."""
    item = await session.get(BoqItem, item_id)
    if item is None or item.site_id != ctx.site.id:
        raise NotFoundError(guards.BOQ_ITEM_FOREIGN)
    if scope.is_restricted:
        state = await load_state(session, ctx, scope=scope)
        if item_id not in set(item_ids(state.tree)):
            raise NotFoundError(guards.BOQ_ITEM_FOREIGN)
    catalog_id = changes.get("catalog_item_id")
    if catalog_id is not None:
        catalog = await session.get(EvCatalogItem, catalog_id)
        if catalog is None or (
            scope.is_restricted and catalog.discipline_id not in (scope.discipline_ids or ())
        ):
            raise NotFoundError(guards.CATALOG_ITEM_MISSING)
    draft = await _draft_for_write(session, ctx, actor)
    row = await session.get(EvItemSettings, (draft.id, item_id))
    if row is None:
        row = EvItemSettings(revision_id=draft.id, boq_item_id=item_id, is_direct=True)
        session.add(row)
    for key, value in changes.items():
        setattr(row, key, value)
    await _touch(session, draft)
    return item


@dataclass(frozen=True, slots=True)
class LeafChange:
    item_id: uuid.UUID
    section_id: uuid.UUID | None
    fields: dict[str, object]  # yalniz GOVDEDE GELEN alanlar (model_fields_set)


def _normalize_rate(fields: dict[str, object]) -> dict[str, object]:
    """Oran ↔ kaynak birlikte: oran geldi kaynak gelmedi → `manual`; oran None → kaynak None."""
    out = dict(fields)
    if "unit_mhr" in out:
        rate = out["unit_mhr"]
        if rate is None:
            out["rate_source"] = None
        else:
            if not isinstance(rate, Decimal) or rate < 0:
                raise EarnedValueValidationError(guards.RATE_NEGATIVE)
            out.setdefault("rate_source", RateSource.MANUAL)
            if out["rate_source"] is None:
                out["rate_source"] = RateSource.MANUAL
    elif "rate_source" in out:
        out.pop("rate_source")  # oransiz kaynak yazilmaz
    return out


async def patch_leaves(
    session: AsyncSession,
    ctx: SiteContext,
    actor: User,
    changes: list[LeafChange],
    scope: DisciplineScope = UNRESTRICTED,
) -> int:
    """Tekil ya da toplu yaprak yazmasi (oran, kaynak, own/subcon ve dogrudan ezmeleri).
    Kisitlida gorunmeyen yaprak → olmayanla ayni 422 `LEAF_MISSING` (Ü7)."""
    state = await load_state(session, ctx, scope=scope)
    live: set[LeafKey] = {(lf.item_id, lf.section_id) for *_, lf in state.tree.leaves()}
    for ch in changes:
        if (ch.item_id, ch.section_id) not in live:
            raise EarnedValueValidationError(guards.LEAF_MISSING)
    draft = await _draft_for_write(session, ctx, actor)
    for ch in changes:
        fields = _normalize_rate(ch.fields)
        row = await _leaf_row(session, draft.id, ch.item_id, ch.section_id)
        if row is None:
            row = EvLeafSettings(
                revision_id=draft.id, boq_item_id=ch.item_id, section_id=ch.section_id
            )
            session.add(row)
        for key, value in fields.items():
            setattr(row, key, value)
    await _touch(session, draft)
    return len(changes)


async def _leaf_row(
    session: AsyncSession, rev_id: uuid.UUID, item_id: uuid.UUID, section_id: uuid.UUID | None
) -> EvLeafSettings | None:
    stmt = select(EvLeafSettings).where(
        EvLeafSettings.revision_id == rev_id,
        EvLeafSettings.boq_item_id == item_id,
        EvLeafSettings.section_id.is_(None)
        if section_id is None
        else EvLeafSettings.section_id == section_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def put_distributions(
    session: AsyncSession, ctx: SiteContext, actor: User, pairs: list[tuple[uuid.UUID, str]]
) -> int:
    if any(dist not in DISTRIBUTION_VALUES for _, dist in pairs):
        raise EarnedValueValidationError("Geçersiz dağılım tipi")
    await _require_disciplines(session, {d for d, _ in pairs})
    draft = await _draft_for_write(session, ctx, actor)
    for disc_id, dist in pairs:
        row = await session.get(EvDistribution, (draft.id, disc_id))
        if row is None:
            session.add(
                EvDistribution(revision_id=draft.id, discipline_id=disc_id, distribution=dist)
            )
        else:
            row.distribution = dist
    await _touch(session, draft)
    return len(pairs)


async def put_windows(
    session: AsyncSession,
    ctx: SiteContext,
    actor: User,
    windows: list[tuple[uuid.UUID, uuid.UUID | None, date, date]],
) -> int:
    """Pencere ezmeleri — TAM DEGISTIRME (govdede olmayan ezme silinir → bolum tarihine doner)."""
    if any(end < start for _, _, start, end in windows):
        raise EarnedValueValidationError(guards.WINDOW_RANGE_INVALID)
    keys = [(d, s) for d, s, _, _ in windows]
    if len(set(keys)) != len(keys):
        raise EarnedValueValidationError("Aynı disiplin × bölüm penceresi iki kez verildi")
    sections = {s for _, s in keys if s is not None}
    if sections - await _site_ids(session, Section, ctx.site.id, sections):
        raise EarnedValueValidationError(guards.SECTION_FOREIGN)
    await _require_disciplines(session, {d for d, _ in keys})
    draft = await _draft_for_write(session, ctx, actor)
    await session.execute(delete(EvWindow).where(EvWindow.revision_id == draft.id))
    for disc_id, section_id, start, end in windows:
        session.add(
            EvWindow(
                revision_id=draft.id,
                discipline_id=disc_id,
                section_id=section_id,
                start_date=start,
                end_date=end,
            )
        )
    await _touch(session, draft)
    return len(windows)

"""Planlama (EV) sirket katalogu + disiplin is kurallari (PLANLAMA-SPEC §3.8 K2/K4, §3.9 B1-9).

Sirket duzeyidir: santiye kapsami YOKTUR (izin kapisi yeter, `access.py`).

## Benzersizlik
Disiplin kodu ve katalogun (disiplin, ad, birim) uclusu once acik SELECT ile
sinanir → `DuplicateError` alanina ozel Turkce metinle (409). DB `UniqueConstraint`
yaris durumu emniyet agi olarak KALIR (`IntegrityError` → 409 genel isleyici). Katalogun
UQ'su normalize anahtar kolonlarindadir (`name_key`/`uom_key`, KATALOG-UQ).

## Silme
Disiplin yalniz HICBIR EV tablosunda kullanilmiyorsa silinir (B1-9); katalog
kalemi silinmez/arsivlenmez (B1-9) — ucu yoktur.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import UNRESTRICTED, DisciplineScope
from app.core.errors import ConflictError, DuplicateError, NotFoundError, RelatedRecordsExistError
from app.modules.catalog.models import EvCatalogItem, EvDiscipline
from app.modules.earned_value import guards
from app.modules.earned_value.contractor_bridge import to_engine_ct
from app.modules.earned_value.labels import normalize_label
from app.modules.earned_value.models import (
    RATE_PRECISION,
    EvBaselineLeaf,
    EvDistribution,
    EvGroupDiscipline,
    EvItemSettings,
    EvRevision,
    EvWindow,
    UserDiscipline,
)
from app.modules.earned_value.schemas_catalog import (
    CatalogActual,
    CatalogActualSite,
    CatalogItemCreate,
    CatalogItemRead,
    CatalogItemUpdate,
    DisciplineCreate,
    DisciplineRef,
    DisciplineUpdate,
)

#: Katalog oran kolonunun kuantumu (Numeric(12,4)) — benimsenen ortalama buna oturur.
_RATE_QUANTUM = Decimal(1).scaleb(-RATE_PRECISION[1])


@dataclass(frozen=True)
class CatalogItemRow:
    """Listeleme/yanit icin katalog kalemi + disiplini + turevleri."""

    item: EvCatalogItem
    discipline: EvDiscipline
    used_by_site_count: int
    actual: CatalogActual


# ------------------------------------------------------------------ disiplin


def _scope_ids(scope: DisciplineScope) -> list[uuid.UUID]:
    return sorted(scope.discipline_ids or (), key=str)


async def list_disciplines(
    session: AsyncSession, scope: DisciplineScope = UNRESTRICTED
) -> list[EvDiscipline]:
    """Disiplinler; kisitlida yalniz KENDI disiplinleri (`IN`; `NOT IN` yok — DSC B1)."""
    stmt = select(EvDiscipline).order_by(EvDiscipline.sort_order, EvDiscipline.code)
    if scope.is_restricted:
        stmt = stmt.where(EvDiscipline.id.in_(_scope_ids(scope)))
    return list((await session.execute(stmt)).scalars())


async def get_discipline(session: AsyncSession, discipline_id: uuid.UUID) -> EvDiscipline:
    discipline = await session.get(EvDiscipline, discipline_id)
    if discipline is None:
        raise NotFoundError(guards.DISCIPLINE_MISSING)
    return discipline


async def _assert_code_free(
    session: AsyncSession, code: str, exclude_id: uuid.UUID | None = None
) -> None:
    stmt = select(EvDiscipline.id).where(EvDiscipline.code == code)
    if exclude_id is not None:
        stmt = stmt.where(EvDiscipline.id != exclude_id)
    if (await session.execute(stmt.limit(1))).first() is not None:
        raise DuplicateError(guards.DISCIPLINE_CODE_TAKEN)


async def create_discipline(session: AsyncSession, data: DisciplineCreate) -> EvDiscipline:
    await _assert_code_free(session, data.code)
    discipline = EvDiscipline(**data.model_dump())
    session.add(discipline)
    await session.flush()
    return discipline


async def update_discipline(
    session: AsyncSession, discipline_id: uuid.UUID, data: DisciplineUpdate
) -> EvDiscipline:
    discipline = await get_discipline(session, discipline_id)
    changes = data.model_dump(exclude_unset=True)
    if "code" in changes and changes["code"] != discipline.code:
        await _assert_code_free(session, changes["code"], exclude_id=discipline.id)
    for field, value in changes.items():
        setattr(discipline, field, value)
    await session.flush()
    return discipline


@dataclass(frozen=True)
class DisciplineUsage:
    item_count: int  # katalog is tipi sayisi
    site_count: int  # disipline BOQ grubu eslenmis (ya da donmus baseline'i olan) santiye
    user_count: int = 0  # disipline atanmis kullanici (`user_disciplines`; DSC-B0)


async def discipline_usage(
    session: AsyncSession, discipline_ids: list[uuid.UUID]
) -> dict[uuid.UUID, DisciplineUsage]:
    """Liste icin TOPLU sayim — uc sorgu, disiplin basina sorgu YOK (N+1 degil).

    Santiye sayisi = grup eslemesi (aktif/taslak/arsiv fark etmez) ∪ donmus baseline yapragi.
    🔴 Baseline'i da saymak zorunlu: BOQ grubu silinince eslemesi CASCADE ile gider ama
    baseline yapragi disipline RESTRICT ile bagli kalir — saymasaydik "0 santiye" deyip
    silmeye izin verir, sonra FK'ya carpardik (genel 409 'Veri butunlugu hatasi').
    """
    if not discipline_ids:
        return {}
    items = dict(
        (
            await session.execute(
                select(EvCatalogItem.discipline_id, func.count())
                .where(EvCatalogItem.discipline_id.in_(discipline_ids))
                .group_by(EvCatalogItem.discipline_id)
            )
        ).all()
    )
    mapped = select(EvGroupDiscipline.discipline_id.label("d"), EvRevision.site_id.label("s")).join(
        EvRevision, EvRevision.id == EvGroupDiscipline.revision_id
    )
    frozen = select(EvBaselineLeaf.discipline_id.label("d"), EvRevision.site_id.label("s")).join(
        EvRevision, EvRevision.id == EvBaselineLeaf.revision_id
    )
    both = mapped.union_all(frozen).subquery()
    sites = dict(
        (
            await session.execute(
                select(both.c.d, func.count(func.distinct(both.c.s)))
                .where(both.c.d.in_(discipline_ids))
                .group_by(both.c.d)
            )
        ).all()
    )
    users = dict(
        (
            await session.execute(
                select(UserDiscipline.discipline_id, func.count())
                .where(UserDiscipline.discipline_id.in_(discipline_ids))
                .group_by(UserDiscipline.discipline_id)
            )
        ).all()
    )
    return {
        d: DisciplineUsage(items.get(d, 0), sites.get(d, 0), users.get(d, 0))
        for d in discipline_ids
    }


async def delete_discipline(session: AsyncSession, discipline_id: uuid.UUID) -> EvDiscipline:
    """Kullanilmayan disiplini siler (kural = sayaclar: is tipi, santiye, atanmis kullanici 0).

    Sayaclar sifirken kalabilecek tek iz, eslemesi kalmamis revizyonlardaki dagilim tipi /
    pencere EZMESIDIR — grubu olmayan disiplin icin anlamsizdir, disiplinle birlikte silinir.
    """
    discipline = await get_discipline(session, discipline_id)
    usage = (await discipline_usage(session, [discipline.id]))[discipline.id]
    if usage.item_count or usage.site_count:
        raise RelatedRecordsExistError(guards.DISCIPLINE_IN_USE)
    if usage.user_count:  # genel 409 'Veri butunlugu' yerine anlamli 409 (DSC-B0)
        raise RelatedRecordsExistError(guards.DISCIPLINE_ASSIGNED_TO_USERS)
    for model in (EvDistribution, EvWindow):
        await session.execute(delete(model).where(model.discipline_id == discipline.id))
    await session.delete(discipline)
    await session.flush()
    return discipline


# ------------------------------------------------------------------- katalog


def _empty_actual() -> CatalogActual:
    return CatalogActual(avg=None, min=None, max=None, site_count=0, sites=[])


async def catalog_actuals(
    session: AsyncSession, item_ids: Iterable[uuid.UUID]
) -> Mapping[uuid.UUID, CatalogActual]:
    """Katalog kalemlerinin GERCEKLESEN birim oranlari (K4) — TEK kaynak.

    Kural (K4): yalniz TAMAMLANMIS santiyeler girer; ortalama MIKTAR AGIRLIKLI
    Σspent / Σqty'dir (santiyeler arasi basit ortalama DEGIL); min/max santiye
    oranlaridir; `sites` ortalamaya giren santiyelerdir.

    B3: `actuals.completed_site_actuals` motoru tamamlanmis santiyelerde kosar (geç import:
    `actuals` → `ev_input` → `settings_service` zinciri bu modulle dongu kurmasin).
    """
    from app.modules.earned_value.actuals import completed_site_actuals

    ids = list(item_ids)
    per_item = await completed_site_actuals(session, ids)
    out: dict[uuid.UUID, CatalogActual] = {}
    for item_id in ids:
        sites = per_item.get(item_id, [])
        if not sites:
            out[item_id] = _empty_actual()
            continue
        qty = sum((s.qty for s in sites), Decimal(0))
        spent = sum((s.spent for s in sites), Decimal(0))
        rates = [s.rate for s in sites]
        out[item_id] = CatalogActual(
            avg=spent / qty,
            min=min(rates),
            max=max(rates),
            site_count=len(sites),
            sites=[
                CatalogActualSite(
                    site_id=s.site_id,
                    site_name=s.site_name,
                    end_date=s.end_date,
                    qty=s.qty,
                    rate=s.rate,
                )
                for s in sorted(sites, key=lambda x: (x.end_date is None, x.end_date), reverse=True)
            ],
        )
    return out


def diff_ratio(actual: CatalogActual, standard: Decimal) -> Decimal | None:
    """(ortalama − standart) ÷ standart; ortalama yoksa `None`. Ham oran, yuvarlamasiz."""
    if actual.avg is None:
        return None
    return (actual.avg - standard) / standard


async def _used_by_site_counts(
    session: AsyncSession, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Kalemi butcesinde baglamis (`ev_item_settings.catalog_item_id`) DISTINCT santiye."""
    if not item_ids:
        return {}
    stmt = (
        select(EvItemSettings.catalog_item_id, func.count(func.distinct(EvRevision.site_id)))
        .join(EvRevision, EvRevision.id == EvItemSettings.revision_id)
        .where(EvItemSettings.catalog_item_id.in_(item_ids))
        .group_by(EvItemSettings.catalog_item_id)
    )
    return {item_id: count for item_id, count in (await session.execute(stmt)).all()}


def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def _rows(
    session: AsyncSession, pairs: list[tuple[EvCatalogItem, EvDiscipline]]
) -> list[CatalogItemRow]:
    ids = [item.id for item, _ in pairs]
    counts = await _used_by_site_counts(session, ids)
    actuals = await catalog_actuals(session, ids)
    return [
        CatalogItemRow(
            item=item,
            discipline=discipline,
            used_by_site_count=counts.get(item.id, 0),
            actual=actuals.get(item.id) or _empty_actual(),
        )
        for item, discipline in pairs
    ]


async def list_catalog(
    session: AsyncSession,
    discipline_id: uuid.UUID | None,
    q: str | None,
    scope: DisciplineScope = UNRESTRICTED,
) -> list[CatalogItemRow]:
    """Katalog; kisitlida yalniz kendi disiplinlerinin kalemleri. Yabanci `discipline_id`
    sorgu parametresi kapsamla KESISIR → `[]` (olmayan disiplinle ayni). `catalog_actuals`
    (proje kapsami) bu dilimde DEGISMEZ (K6)."""
    stmt = select(EvCatalogItem, EvDiscipline).join(
        EvDiscipline, EvDiscipline.id == EvCatalogItem.discipline_id
    )
    if scope.is_restricted:
        stmt = stmt.where(EvCatalogItem.discipline_id.in_(_scope_ids(scope)))
    if discipline_id is not None:
        stmt = stmt.where(EvCatalogItem.discipline_id == discipline_id)
    if q is not None and q.strip():
        stmt = stmt.where(EvCatalogItem.name.ilike(_like_pattern(q.strip()), escape="\\"))
    stmt = stmt.order_by(EvDiscipline.sort_order, EvDiscipline.code, EvCatalogItem.name)
    pairs = [(item, discipline) for item, discipline in (await session.execute(stmt)).all()]
    return await _rows(session, pairs)


async def _catalog_row(session: AsyncSession, item: EvCatalogItem) -> CatalogItemRow:
    discipline = await get_discipline(session, item.discipline_id)
    return (await _rows(session, [(item, discipline)]))[0]


async def get_catalog_item(session: AsyncSession, item_id: uuid.UUID) -> EvCatalogItem:
    item = await session.get(EvCatalogItem, item_id)
    if item is None:
        raise NotFoundError(guards.CATALOG_ITEM_MISSING)
    return item


async def _assert_item_free(
    session: AsyncSession,
    discipline_id: uuid.UUID,
    name: str,
    uom: str,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Tekillik ONERI ESLESMESIYLE AYNI kuralla (`labels.normalize_label`: büyük/küçük harf,
    Türkçe İ/I, üst simge, boşluk) — EV-BORC-5. KATALOG-UQ'dan beri anahtar kolonlarda
    saklidir (`EvCatalogItem._sync_key`) ve DB `uq_ev_catalog_items_disc_name_key_uom_key`
    ile zorlar; bu SELECT yalniz alana ozel Turkce 409 metni icindir."""
    stmt = select(EvCatalogItem.name, EvCatalogItem.uom).where(
        EvCatalogItem.discipline_id == discipline_id,
        EvCatalogItem.name_key == normalize_label(name),
        EvCatalogItem.uom_key == normalize_label(uom),
    )
    if exclude_id is not None:
        stmt = stmt.where(EvCatalogItem.id != exclude_id)
    taken = (await session.execute(stmt.limit(1))).first()
    if taken is not None:
        raise DuplicateError(guards.CATALOG_ITEM_TAKEN_AS.format(name=taken.name, uom=taken.uom))


async def create_catalog_item(session: AsyncSession, data: CatalogItemCreate) -> CatalogItemRow:
    # Govde ici varlik referansi: disiplin yoksa 404 (repo kanonu).
    await get_discipline(session, data.discipline_id)
    await _assert_item_free(session, data.discipline_id, data.name, data.uom)
    item = EvCatalogItem(**data.model_dump(), standard_updated_at=datetime.now(UTC))
    session.add(item)
    await session.flush()
    return await _catalog_row(session, item)


async def update_catalog_item(
    session: AsyncSession, item_id: uuid.UUID, data: CatalogItemUpdate
) -> CatalogItemRow:
    item = await get_catalog_item(session, item_id)
    changes = data.model_dump(exclude_unset=True)
    if "discipline_id" in changes:
        await get_discipline(session, changes["discipline_id"])
    key = (
        changes.get("discipline_id", item.discipline_id),
        changes.get("name", item.name),
        changes.get("uom", item.uom),
    )
    if key != (item.discipline_id, item.name, item.uom):
        await _assert_item_free(session, *key, exclude_id=item.id)
    new_rate = changes.get("standard_unit_mhr")
    if new_rate is not None and new_rate != item.standard_unit_mhr:
        item.standard_updated_at = datetime.now(UTC)
    for field, value in changes.items():
        setattr(item, field, value)
    await session.flush()
    return await _catalog_row(session, item)


@dataclass(frozen=True)
class AdoptResult:
    row: CatalogItemRow
    old: Decimal
    new: Decimal


async def adopt_actual(session: AsyncSession, item_id: uuid.UUID) -> AdoptResult:
    """KAT "Gerceklesen standart yap": standart ← gerceklesen ortalama (K4).

    Gerceklesen yoksa (hic tamamlanmis santiye verisi yok, `catalog_actuals`) 409. Mevcut
    butceler etkilenmez (oran atama aninda kopyalanir, K4).
    """
    item = await get_catalog_item(session, item_id)
    actual = (await catalog_actuals(session, [item.id])).get(item.id)
    if actual is None or actual.avg is None:
        raise ConflictError(guards.CATALOG_NO_ACTUAL)
    old = item.standard_unit_mhr
    new = actual.avg.quantize(_RATE_QUANTUM)
    item.standard_unit_mhr = new
    item.standard_updated_at = datetime.now(UTC)
    await session.flush()
    return AdoptResult(row=await _catalog_row(session, item), old=old, new=new)


def to_read(row: CatalogItemRow) -> CatalogItemRead:
    item = row.item
    return CatalogItemRead(
        id=item.id,
        discipline=DisciplineRef.model_validate(row.discipline),
        name=item.name,
        uom=item.uom,
        standard_unit_mhr=item.standard_unit_mhr,
        default_contractor_type=to_engine_ct(item.default_contractor_type),
        description=item.description,
        standard_updated_at=item.standard_updated_at,
        used_by_site_count=row.used_by_site_count,
        actual=row.actual,
        diff_pct=diff_ratio(row.actual, item.standard_unit_mhr),
    )

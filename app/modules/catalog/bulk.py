"""Katalog TOPLU ekleme / fiyat guncelleme (KAT-B1) — hep-ya-hic, tek transaction.

Bakanlik birim fiyat listesi gibi buyuk listeleri (1..200 kalem/istek) `source_code` ile
eslestirerek ekler; ertesi yil ayni kodlu kalemin yalniz FIYATINI gunceller.

## Sira (kilit kanonu) — TEKIL YOLLARLA AYNI: disiplin → kalem
a. `pg_advisory_xact_lock(SOURCE_IMPORT_LOCK_KEY)` — iki eszamanli TOPLU istek ayni kaynak
   kodunu ayni anda "yok" gorup ikisi de ekleyemesin (UQ yarisi); tekil uclar bu kilidi
   ALMAZ (kaynak kodu yarisi DB UQ → genel 409 ile kapanir; kabul edilmis sinir).
b. KILITSIZ on siniflandirma: hangi satir eslesen kalem (kaynak kodu DB'de var), hangisi yeni.
c. Dokunulan TUM disiplinler (yeni kalem alacaklar + eslesen kalemlerin disiplinleri) `id`
   sirasiyla `service.lock_discipline` ile kilitlenir (sayac kilidi; tekil olusturma ile AYNI
   mekanizma — kopya sayac mantigi YOK).
d. Eslesen kalemler (yalniz `update_price`) `id` sirasiyla `FOR NO KEY UPDATE` + taze okuma.
e. Kilitler alindiktan sonra YENIDEN DOGRULAMA: eslesen kume (kod → kalem id + disiplin)
   on siniflandirmayla ayni mi? Degismisse (arada tekil bir yol kodu/disiplini oynatti)
   TEMIZ 422 (`BULK_CATALOG_CHANGED`, hicbir sey yazilmaz, istek yeniden denenir); yeniden
   siniflandirma YAPILMAZ.
Neden bu sira: `update_discipline` (disiplin → kalemleri UPDATE) ve kalem tasima PATCH'i
(hedef disiplin → kalem) disiplin ONCE, kalem SONRA kilitler; toplu yol kalemi ONCE kilitleseydi
bu iki yolla kilit DONGUSU (40P01) olusurdu (KAT-B1.1 O1, olculdu). Sira tekil yollarla ayni
oldugu icin dongu yok; iki toplu istek zaten advisory ile serilesir.
4. DOGRULA (hepsi toplanir, hicbir sey yazilmadan): disiplin yok · kaynak kodu istek icinde
   tekrar · kaynak kodu DB'de var (`error` kipi) · ad+birim anahtari DB'de var / istek icinde
   tekrar · anahtar kolon sinirini asiyor · fiyat tarihi fiyatsiz (yeni kalem; eslesen kalemde
   fiyat gonderilmediyse KALEMIN fiyatina bakilir). Hata varsa 422 + `errors[]`
   (`loc: ["body","items",i,alan]`), islem geri alinir.
5. YAZ: istek sirasiyla; poz no `service.next_poz_no`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EarnedValueValidationError
from app.core.labels import NAME_KEY_MAX_LEN, UOM_KEY_MAX_LEN, normalize_label
from app.modules.catalog import guards, service
from app.modules.catalog.models import EvCatalogItem, EvDiscipline

#: Toplu kaynak-kodu aktarimlarini seriler (`pg_advisory_xact_lock`, islem sonunda biter).
SOURCE_IMPORT_LOCK_KEY = 0x4B415442  # "KATB"

CREATED = "created"
PRICE_UPDATED = "price_updated"
UNCHANGED = "unchanged"
UPDATE_PRICE = "update_price"


@dataclass(frozen=True)
class BulkRow:
    index: int
    item: EvCatalogItem
    action: str


def _loc(index: int, field: str) -> list[str | int]:
    return ["body", "items", index, field]


class _Errors:
    def __init__(self) -> None:
        self._rows: list[dict[str, object]] = []

    def add(self, index: int, field: str, message: str) -> None:
        self._rows.append({"loc": _loc(index, field), "message": message})

    def raise_if_any(self) -> None:
        if self._rows:
            rows = sorted(self._rows, key=lambda r: r["loc"][2])  # type: ignore[index]
            raise EarnedValueValidationError(guards.BULK_REJECTED.format(count=len(rows)), rows)


async def _existing_by_source(
    session: AsyncSession, codes: Sequence[str], *, lock: bool
) -> dict[str, EvCatalogItem]:
    if not codes:
        return {}
    stmt = (
        select(EvCatalogItem).where(EvCatalogItem.source_code.in_(codes)).order_by(EvCatalogItem.id)
    )
    stmt = stmt.execution_options(populate_existing=True)
    if lock:
        # FOR NO KEY UPDATE: FK KEY SHARE'i (teklif/sozlesme kalemi INSERT'i) bloke etmez
        stmt = stmt.with_for_update(key_share=True)
    rows = (await session.execute(stmt)).scalars()
    return {row.source_code: row for row in rows if row.source_code is not None}


def _classify(
    entries: Sequence[Mapping[str, Any]],
    existing: Mapping[str, EvCatalogItem],
    on_source_conflict: str,
    errors: _Errors,
) -> dict[int, str | None]:
    """Satir → eslesen kalemin kaynak kodu (`update_price`) ya da None (yeni). Hatali satir da
    None sayilir (hata zaten toplandi). Eslesen satirin fiyat/tarih kurali kilit altinda
    (`_check_matched_dates`) bakilir."""
    matched: dict[int, str | None] = {}
    first_seen: dict[str, int] = {}
    for index, entry in enumerate(entries):
        matched[index] = None
        code = entry.get("source_code")
        found = existing.get(code) if code is not None else None
        merges = found is not None and on_source_conflict == UPDATE_PRICE
        if not merges and entry.get("ref_price") is None and entry.get("ref_price_date"):
            errors.add(index, "ref_price_date", guards.REF_PRICE_DATE_NEEDS_PRICE)
        if code is None:
            continue
        if code in first_seen:
            errors.add(
                index, "source_code", guards.BULK_SOURCE_REPEATED.format(first=first_seen[code] + 1)
            )
            continue
        first_seen[code] = index
        if found is None:
            continue
        if merges:
            matched[index] = code
        else:
            errors.add(
                index,
                "source_code",
                guards.BULK_SOURCE_EXISTS.format(
                    poz_no=found.poz_no, name=found.name, uom=found.uom
                ),
            )
    return matched


def _check_matched_dates(
    entries: Sequence[Mapping[str, Any]],
    matched: Mapping[int, EvCatalogItem | None],
    errors: _Errors,
) -> None:
    """Eslesen kalem: fiyat gonderilmeden tarih verildiyse KALEMIN (kilitli) fiyati dolu olmali
    (PATCH ile ayni: yalniz tarih guncellenir; fiyat yoksa kural a → hata)."""
    for index, item in matched.items():
        entry = entries[index]
        if item is None or entry.get("ref_price") is not None:
            continue
        if entry.get("ref_price_date") is not None and item.ref_price is None:
            errors.add(index, "ref_price_date", guards.BULK_DATE_ON_PRICELESS_ITEM)


async def _check_new_items(
    session: AsyncSession,
    entries: Sequence[Mapping[str, Any]],
    new_indexes: Sequence[int],
    errors: _Errors,
) -> None:
    """Yeni kalemler: anahtar uzunlugu + ad/birim cakismasi (DB'dekiyle ve kendi aralarinda)."""
    keyed: dict[int, tuple[uuid.UUID, str, str]] = {}
    for index in new_indexes:
        entry = entries[index]
        name_key, uom_key = normalize_label(entry["name"]), normalize_label(entry["uom"])
        if len(name_key) > NAME_KEY_MAX_LEN or len(uom_key) > UOM_KEY_MAX_LEN:
            errors.add(index, "name", "Ad/Birim: normalize edildikten sonra çok uzun")
            continue
        keyed[index] = (entry["discipline_id"], name_key, uom_key)
    if not keyed:
        return
    rows = await session.execute(
        select(
            EvCatalogItem.discipline_id,
            EvCatalogItem.name_key,
            EvCatalogItem.uom_key,
            EvCatalogItem.name,
            EvCatalogItem.uom,
        ).where(
            EvCatalogItem.discipline_id.in_({k[0] for k in keyed.values()}),
            EvCatalogItem.name_key.in_({k[1] for k in keyed.values()}),
        )
    )
    taken = {(r.discipline_id, r.name_key, r.uom_key): (r.name, r.uom) for r in rows}
    first_seen: dict[tuple[uuid.UUID, str, str], int] = {}
    for index in sorted(keyed):
        key = keyed[index]
        if key in taken:
            name, uom = taken[key]
            errors.add(index, "name", guards.CATALOG_ITEM_TAKEN_AS.format(name=name, uom=uom))
        elif key in first_seen:
            errors.add(index, "name", guards.BULK_ITEM_REPEATED.format(first=first_seen[key] + 1))
        else:
            first_seen[key] = index


def _fingerprint(by_source: Mapping[str, EvCatalogItem]) -> dict[str, tuple[uuid.UUID, uuid.UUID]]:
    """Kod → (kalem id, disiplin id): kilit oncesi/sonrasi siniflandirma ayni mi?"""
    return {code: (item.id, item.discipline_id) for code, item in by_source.items()}


async def _serialize_imports(session: AsyncSession) -> None:
    """Toplu aktarimlari seriler (islem sonunda biter); bkz. modul docstring'i, madde 1."""
    await session.execute(select(func.pg_advisory_xact_lock(SOURCE_IMPORT_LOCK_KEY)))


async def bulk_upsert_items(
    session: AsyncSession, entries: Sequence[Mapping[str, Any]], on_source_conflict: str
) -> list[BulkRow]:
    """Bkz. modul docstring'i. `entries`: `WorkItemCreate.model_dump()` listesi."""
    errors = _Errors()
    await _serialize_imports(session)

    discipline_ids = {entry["discipline_id"] for entry in entries}
    known_ids = set(
        (
            await session.execute(
                select(EvDiscipline.id).where(EvDiscipline.id.in_(discipline_ids))
            )
        ).scalars()
    )
    for index, entry in enumerate(entries):
        if entry["discipline_id"] not in known_ids:
            errors.add(index, "discipline_id", guards.DISCIPLINE_MISSING)

    codes = sorted({e["source_code"] for e in entries if e.get("source_code") is not None})
    update_mode = on_source_conflict == UPDATE_PRICE
    pre = await _existing_by_source(session, codes, lock=False)
    matched_codes = _classify(entries, pre, on_source_conflict, errors)

    new_indexes = [i for i, code in matched_codes.items() if code is None]
    touched = {entries[i]["discipline_id"] for i in new_indexes} & known_ids
    touched |= {pre[c].discipline_id for c in matched_codes.values() if c is not None}
    disciplines: dict[uuid.UUID, EvDiscipline] = {}
    for discipline_id in sorted(touched, key=lambda d: d.int):  # id sirasi
        disciplines[discipline_id] = await service.lock_discipline(session, discipline_id)

    post = await _existing_by_source(session, codes, lock=update_mode)
    if _fingerprint(pre) != _fingerprint(post):
        raise EarnedValueValidationError(guards.BULK_CATALOG_CHANGED)
    matched = {i: (post[c] if c is not None else None) for i, c in matched_codes.items()}
    _check_matched_dates(entries, matched, errors)
    await _check_new_items(
        session,
        entries,
        [i for i in new_indexes if entries[i]["discipline_id"] in known_ids],
        errors,
    )
    errors.raise_if_any()

    now = datetime.now(UTC)
    rows: list[BulkRow] = []
    for index, entry in enumerate(entries):
        found = matched[index]
        if found is None:
            rows.append(BulkRow(index, await _create(session, disciplines, entry, now), CREATED))
        else:
            rows.append(BulkRow(index, found, _update_price(found, entry, now)))
    await session.flush()
    return rows


async def _create(
    session: AsyncSession,
    disciplines: Mapping[uuid.UUID, EvDiscipline],
    entry: Mapping[str, Any],
    now: datetime,
) -> EvCatalogItem:
    item = EvCatalogItem(
        **entry,
        poz_no=await service.next_poz_no(session, disciplines[entry["discipline_id"]]),
        standard_updated_at=now,
        price_updated_at=now if entry.get("ref_price") is not None else None,
    )
    session.add(item)
    return item


def _update_price(item: EvCatalogItem, entry: Mapping[str, Any], now: datetime) -> str:
    """Eslesen kalem: YALNIZ `ref_price` + `ref_price_date` (semantik: `resolve_price`).
    Gonderilmeyen (None) fiyat/tarih 'dokunma' demektir — mevcut fiyat silinmez; fiyat
    degisir + tarih yok → tarih NULL (kural c); yalniz tarih → yalniz tarih (kural d)."""
    changes: dict[str, Any] = {}
    if entry.get("ref_price") is not None:
        changes["ref_price"] = entry["ref_price"]
    if entry.get("ref_price_date") is not None:
        changes["ref_price_date"] = entry["ref_price_date"]  # fiyatsiz gelirse yalniz tarih
    state = service.resolve_price(item.ref_price, item.ref_price_date, changes)
    if not (state.price_changed or state.date_changed):
        return UNCHANGED
    if state.price_changed:
        item.ref_price = state.price
        item.price_updated_at = now
    item.ref_price_date = state.price_date
    return PRICE_UPDATED

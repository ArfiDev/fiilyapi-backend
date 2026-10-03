"""Cekirdek katalog yazma mantigi (TKL-B2): poz no uretimi + kalem/disiplin yazimi.

EV katalog uclari ve (sonraki dilimde) cekirdek `/catalog/items` uclari AYNI fonksiyonlari
cagirir — poz no kurallari tek yerde. Bu modul EV'yi import ETMEZ (bekci:
`tests/modules/earned_value/test_engine_isolation.py`).

## Poz no (T21-T24)
* Biçim: `disiplin kodu + "-" + EN AZ 4 hane` (`MIM-0001`; 9999 ustu `MIM-10000`),
  sirket genelinde tekil (`uq_ev_catalog_items_poz_no`).
* Uretec: disiplin satirinda MONOTON sayac `ev_disciplines.poz_counter` (verilen son sira).
  Disiplin satiri `FOR NO KEY UPDATE` ile KILITLENIR (`lock_discipline`; kod degisiminde
  `FOR UPDATE`), sayac +1, numara yazilir. Numara ASLA yeniden kullanilmaz — `max+1` YASAK:
  kalem baska disipline tasinirsa eski numara BOSA duser ve sayac geri gelmez.
* Kalemin disiplini degisirse yeni disiplinin sayacindan yeni numara (yeni disiplin kilitli).
* Disiplin KODU degisirse o disiplinin TUM kalemleri yeni onekle yeniden yazilir (sayi
  korunur, sayac degismez), AYNI islemde, disiplin kilitli.
* DEGISMEZ: her kalemin `poz_no` oneki = kendi disiplininin GUNCEL `code`u + `-`.

## Kilit sirasi
Kalem yazimi yalniz HEDEF disiplin satirini kilitler (kalem satirini ayrica kilitlemez);
disiplin kodu degisimi yalniz O disiplin satirini kilitler, sonra kalemlerini gunceller.
Tekil yollar en cok BIR disiplin kilidi tutar; toplu yol (`bulk.py`) birden cok disiplini `id`
sirasiyla kilitler ve ONLARDAN SONRA kalemi kilitler (disiplin → kalem, hepsinde ayni sira):
disiplinler arasi dongu `id` sirasiyla, disiplin↔kalem dongusu sabit sirayla onlenir.

## `ref_price` / `price_updated_at` / `ref_price_date` (KAT-B1)
`price_updated_at` YALNIZ `ref_price` DEGISTIGINDE (ilk atama dahil) `now` olur; baska alan
degisince dokunulmaz. `ref_price_date` fiyatin gecerlilik tarihidir (`resolve_price`):
* fiyat DEGISIR + tarih VERILIR → tarih = verilen; fiyat DEGISIR + tarih VERILMEZ → tarih NULL
  (eski tarih yeni fiyata ait olamaz); YALNIZ tarih degisir → yalniz tarih (`price_updated_at`
  dokunulmaz); birlestirilmis durumda fiyat NULL iken tarih dolu → 422.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DuplicateError, EarnedValueValidationError, NotFoundError
from app.core.labels import normalize_label
from app.modules.catalog import guards
from app.modules.catalog.models import EvCatalogItem, EvDiscipline

#: Poz no sayi kisminin EN AZ hane sayisi (`MIM-0001`).
POZ_MIN_DIGITS = 4


def format_poz_no(code: str, seq: int) -> str:
    """`MIM` + 7 → `MIM-0007`; 9999 ustu tasar (`MIM-10000`)."""
    return f"{code}-{seq:0{POZ_MIN_DIGITS}d}"


# ------------------------------------------------------------------ disiplin


async def get_discipline(session: AsyncSession, discipline_id: uuid.UUID) -> EvDiscipline:
    discipline = await session.get(EvDiscipline, discipline_id)
    if discipline is None:
        raise NotFoundError(guards.DISCIPLINE_MISSING)
    return discipline


async def lock_discipline(
    session: AsyncSession, discipline_id: uuid.UUID, *, rewrites_code: bool = False
) -> EvDiscipline:
    """Disiplin satirini kilitler ve TAZE okur (`populate_existing`: oturumun kimlik
    haritasinda bayat kopya varsa sayac/kod kilit ALTINDA yeniden okunur).

    Kilit gucu YAZILACAK kolona gore (R2):
    * `rewrites_code=False` (kalem olusturma, kalemin disiplin degisimi): yalniz `poz_counter`
      yazilir → `FOR NO KEY UPDATE` (`key_share=True`). FK'lerin `KEY SHARE` kilidini
      (`user_disciplines`, grup eslemesi, baseline INSERT'leri) BEKLETMEZ.
    * `rewrites_code=True` (disiplin KODU degisimi): `code` UQ anahtar kolonu yazilir →
      tam `FOR UPDATE`.
    Iki kip birbirini SERILESTIRIR: PG kilit cakisma tablosunda `FOR NO KEY UPDATE` kendisiyle
    ve `FOR UPDATE` ile, `FOR UPDATE` her kiple cakisir; sayac yarisi ve kod degisimi
    arasindaki degismez korunur.
    """
    stmt = (
        select(EvDiscipline)
        .where(EvDiscipline.id == discipline_id)
        .with_for_update(key_share=not rewrites_code)
        .execution_options(populate_existing=True)
    )
    discipline = (await session.execute(stmt)).scalar_one_or_none()
    if discipline is None:
        raise NotFoundError(guards.DISCIPLINE_MISSING)
    return discipline


async def next_poz_no(session: AsyncSession, discipline: EvDiscipline) -> str:
    """Sayaci +1 yapar, yeni numarayi dondurur. `discipline` `lock_discipline` ile KILITLI
    olmali (cagiran sorumlu); sayac hemen flush edilir."""
    discipline.poz_counter = discipline.poz_counter + 1
    await session.flush()
    return format_poz_no(discipline.code, discipline.poz_counter)


async def assert_code_free(
    session: AsyncSession, code: str, exclude_id: uuid.UUID | None = None
) -> None:
    stmt = select(EvDiscipline.id).where(EvDiscipline.code == code)
    if exclude_id is not None:
        stmt = stmt.where(EvDiscipline.id != exclude_id)
    if (await session.execute(stmt.limit(1))).first() is not None:
        raise DuplicateError(guards.DISCIPLINE_CODE_TAKEN)


async def renumber_for_code_change(
    session: AsyncSession, discipline: EvDiscipline, old_code: str, new_code: str
) -> int:
    """Disiplin kodu `old_code` → `new_code`: disiplinin TUM kalemlerinin poz no'su yeni onekle
    yeniden yazilir, SAYI KISMI KORUNUR, sayac degismez. `discipline` KILITLI olmali.

    Sayi kismi eski oneki (`old_code + "-"`) TAM eslesmeyle soyularak alinir (kod `-`
    icerebilir; son `-`ten bolunmez). Onek tutmayan ya da kalani rakam olmayan bir kalem
    DEGISMEZIN bozuldugunu gosterir → acik `RuntimeError` (islem geri alinir).
    Donus: degistirilen kalem sayisi.
    """
    if old_code == new_code:
        return 0
    old_prefix = f"{old_code}-"
    new_prefix = f"{new_code}-"
    rows = (
        await session.execute(
            select(EvCatalogItem.id, EvCatalogItem.poz_no).where(
                EvCatalogItem.discipline_id == discipline.id
            )
        )
    ).all()
    for item_id, poz_no in rows:
        tail = poz_no[len(old_prefix) :]
        if not poz_no.startswith(old_prefix) or not (tail.isascii() and tail.isdigit()):
            raise RuntimeError(
                f"Poz no degismezi bozuk: kalem {item_id} poz_no={poz_no!r}, "
                f"beklenen onek {old_prefix!r} + rakamlar (disiplin {discipline.id})"
            )
    if not rows:
        return 0
    # Tek ifade: `new_prefix || substr(poz_no, len(old_prefix) + 1)`; onek yukarida dogrulandi.
    result = await session.execute(
        update(EvCatalogItem)
        .where(EvCatalogItem.discipline_id == discipline.id)
        .values(poz_no=new_prefix + func.substr(EvCatalogItem.poz_no, len(old_prefix) + 1))
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount or 0


async def update_discipline(
    session: AsyncSession, discipline_id: uuid.UUID, changes: Mapping[str, Any]
) -> EvDiscipline:
    """Disiplin alanlarini yazar. `code` degisiyorsa disiplin KILITLENIR, kod tekilligi
    sinanir ve kalemler AYNI islemde yeniden numaralanir."""
    changing_code = "code" in changes
    discipline = (
        await lock_discipline(session, discipline_id, rewrites_code=True)
        if changing_code
        else await get_discipline(session, discipline_id)
    )
    old_code = discipline.code
    new_code = changes.get("code", old_code)
    if new_code != old_code:
        await assert_code_free(session, new_code, exclude_id=discipline.id)
    for field, value in changes.items():
        setattr(discipline, field, value)
    await session.flush()
    await renumber_for_code_change(session, discipline, old_code, new_code)
    return discipline


# ------------------------------------------------------------------- katalog


async def get_item(session: AsyncSession, item_id: uuid.UUID) -> EvCatalogItem:
    item = await session.get(EvCatalogItem, item_id)
    if item is None:
        raise NotFoundError(guards.CATALOG_ITEM_MISSING)
    return item


async def assert_item_free(
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


async def assert_source_code_free(
    session: AsyncSession, source_code: str, exclude_id: uuid.UUID | None = None
) -> None:
    """Kaynak poz kodu tekil (`uq_ev_catalog_items_source_code`); bu SELECT alana ozel 409
    metni icindir, DB indeksi yarisin son savunmasidir (IntegrityError → genel 409)."""
    stmt = select(EvCatalogItem.poz_no, EvCatalogItem.name, EvCatalogItem.uom).where(
        EvCatalogItem.source_code == source_code
    )
    if exclude_id is not None:
        stmt = stmt.where(EvCatalogItem.id != exclude_id)
    taken = (await session.execute(stmt.limit(1))).first()
    if taken is not None:
        raise DuplicateError(
            guards.SOURCE_CODE_TAKEN_AS.format(poz_no=taken.poz_no, name=taken.name, uom=taken.uom)
        )


@dataclass(frozen=True)
class PriceState:
    """`resolve_price` sonucu: yazilacak fiyat/tarih ve neyin DEGISTIGI."""

    price: Decimal | None
    price_date: date | None
    price_changed: bool
    date_changed: bool


def resolve_price(
    old_price: Decimal | None, old_date: date | None, changes: Mapping[str, Any]
) -> PriceState:
    """`ref_price` + `ref_price_date` kurallarinin TEK kaynagi (tekil uclar + toplu ekleme).

    `changes` yalniz GELEN anahtarlari tasir (`ref_price` / `ref_price_date`). Kurallar: modul
    docstring'i. Birlestirilmis durumda fiyat NULL + tarih dolu → 422 (alan adli, `loc`lu)."""
    price = changes.get("ref_price", old_price)
    price_changed = "ref_price" in changes and price != old_price
    if "ref_price_date" in changes:
        price_date = changes["ref_price_date"]
    elif price_changed:
        price_date = None  # bayat tarih bırakılmaz
    else:
        price_date = old_date
    if price is None and price_date is not None:
        raise EarnedValueValidationError(
            guards.REF_PRICE_DATE_NEEDS_PRICE,
            errors=[
                {"loc": ["body", "ref_price_date"], "message": guards.REF_PRICE_DATE_NEEDS_PRICE}
            ],
        )
    return PriceState(price, price_date, price_changed, price_date != old_date)


async def create_item(session: AsyncSession, fields: Mapping[str, Any]) -> EvCatalogItem:
    """Yeni kalem + SIRADAKI poz no. `fields`: discipline_id, name, uom, standard_unit_mhr,
    default_contractor_type, (description, ref_price). `poz_no` gonderilemez (ValueError)."""
    if "poz_no" in fields or "price_updated_at" in fields:
        raise ValueError("poz_no / price_updated_at istemciden gelemez; sunucu uretir")
    # Govde ici varlik referansi: disiplin yoksa 404 (repo kanonu); AYNI cagri kilitler.
    discipline = await lock_discipline(session, fields["discipline_id"])
    await assert_item_free(session, discipline.id, fields["name"], fields["uom"])
    if fields.get("source_code") is not None:
        await assert_source_code_free(session, fields["source_code"])
    state = resolve_price(None, None, fields)
    now = datetime.now(UTC)
    item = EvCatalogItem(
        **{**fields, "ref_price_date": state.price_date},
        poz_no=await next_poz_no(session, discipline),
        standard_updated_at=now,
        price_updated_at=now if fields.get("ref_price") is not None else None,
    )
    session.add(item)
    await session.flush()
    return item


async def lock_item(session: AsyncSession, item_id: uuid.UUID) -> EvCatalogItem:
    """Kalem satirini `FOR NO KEY UPDATE` ile kilitler ve TAZE okur (`populate_existing`).
    Fiyat/tarih karari (`resolve_price`) YALNIZ kilit altinda okunan degerle verilir: kilitsiz
    okuma bayat fiyatla karar verip (a) degismezini bozar ya da tarihi yanlis fiyata yapistirir
    (KAT-B1.1 Y1). Kilit sirasi her yerde DISIPLIN → KALEM."""
    stmt = (
        select(EvCatalogItem)
        .where(EvCatalogItem.id == item_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    item = (await session.execute(stmt)).scalar_one_or_none()
    if item is None:
        raise NotFoundError(guards.CATALOG_ITEM_MISSING)
    return item


@dataclass(frozen=True)
class ItemUpdate:
    """`apply_item_update` sonucu: guncel kalem + KILIT ALTINDA alinan onceki degerler."""

    item: EvCatalogItem
    before: dict[str, Any]
    changed: list[str]


async def update_item(
    session: AsyncSession, item_id: uuid.UUID, changes: Mapping[str, Any]
) -> EvCatalogItem:
    return (await apply_item_update(session, item_id, changes)).item


async def apply_item_update(
    session: AsyncSession, item_id: uuid.UUID, changes: Mapping[str, Any]
) -> ItemUpdate:
    """Kismi kalem guncelleme. Disiplin DEGISIRSE yeni disiplinin sayacindan yeni poz no
    (eski numara bosa duser); `standard_updated_at` yalniz oran, `price_updated_at` yalniz
    `ref_price` degisince; `ref_price_date` icin `resolve_price`.

    Kilit sirasi: (hedef disiplin) → kalem. Once kilitsiz `get_item` (404 onceligi), sonra
    hedef disiplin kilidi, sonra kalem kilidi ve TAZE okuma; onceki degerler (`before`) de
    kilit altinda alinir (denetim metni bayat degerden uretilmesin)."""
    if "poz_no" in changes or "price_updated_at" in changes:
        raise ValueError("poz_no / price_updated_at istemciden gelemez; sunucu uretir")
    await get_item(session, item_id)
    target: EvDiscipline | None = None
    if "discipline_id" in changes:
        target = await lock_discipline(session, changes["discipline_id"])  # yoksa 404
    item = await lock_item(session, item_id)
    moving = target is not None and target.id != item.discipline_id
    before = snapshot_fields(item, changes)
    new_poz_no: str | None = None
    key = (
        changes.get("discipline_id", item.discipline_id),
        changes.get("name", item.name),
        changes.get("uom", item.uom),
    )
    if key != (item.discipline_id, item.name, item.uom):
        await assert_item_free(session, *key, exclude_id=item.id)
    if moving and target is not None:
        new_poz_no = await next_poz_no(session, target)
    new_rate = changes.get("standard_unit_mhr")
    if new_rate is not None and new_rate != item.standard_unit_mhr:
        item.standard_updated_at = datetime.now(UTC)
    if changes.get("source_code") is not None and changes["source_code"] != item.source_code:
        await assert_source_code_free(session, changes["source_code"], exclude_id=item.id)
    state = resolve_price(item.ref_price, item.ref_price_date, changes)
    if state.price_changed:
        item.price_updated_at = datetime.now(UTC)
    for field, value in changes.items():
        setattr(item, field, value)
    item.ref_price_date = state.price_date
    if new_poz_no is not None:
        item.poz_no = new_poz_no
    await session.flush()
    return ItemUpdate(item, before, changed_fields(before, item))


def snapshot_fields(item: EvCatalogItem, fields: Iterable[str]) -> dict[str, Any]:
    """Guncellemeden ONCE: verilen alanlarin mevcut degerleri (servis ayni nesneyi yerinde
    degistirir; sonradan okumak eski degeri vermez)."""
    return {field: getattr(item, field) for field in fields}


def _comparable(value: Any) -> Any:
    """Enum → degeri (ORM cekirdek enumu ile sema enumu ayni metni tasir); `Decimal` kendi
    sayisal esitligiyle karsilasir (`1250.5 == 1250.50` DEGISIM degildir)."""
    return getattr(value, "value", value)


def changed_fields(before: Mapping[str, Any], item: EvCatalogItem) -> list[str]:
    """`snapshot_fields` ile bu andaki degerleri karsilastirir: FIILEN degisen alanlar."""
    return [f for f, old in before.items() if _comparable(old) != _comparable(getattr(item, f))]

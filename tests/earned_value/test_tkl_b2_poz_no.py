"""TKL-B2 — otomatik poz no: olusturma, monotonluk, disiplin degisimi, KOD degisimi, fiyat damgasi.

EV uclari + cekirdek servis (`app.modules.catalog.service`). DEGISMEZ bekcisi `_invariant`:
her kalemin `poz_no` oneki = kendi disiplininin GUNCEL kodu + `-`, kalan en az 4 rakam; ve
poz no'lar tekil — TEK SQL ile TUM katalog.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog import service as core
from app.modules.catalog.models import EvCatalogItem
from app.modules.earned_value.engine import ContractorType
from tests._poz_no_degismezi import assert_poz_invariant

ITEMS = "/earned-value/catalog"
DISCS = "/earned-value/disciplines"

_invariant = assert_poz_invariant


def _body(discipline_id, name: str, **extra) -> dict:
    return {
        "discipline_id": str(discipline_id),
        "name": name,
        "uom": "m3",
        "standard_unit_mhr": "1.5000",
        "default_contractor_type": "own",
        **extra,
    }


async def _create(client: AsyncClient, admin, discipline_id, name: str) -> dict:
    resp = await client.post(ITEMS, json=_body(discipline_id, name), headers=admin)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _poz_nos(session: AsyncSession, discipline_id) -> dict[str, str]:
    rows = await session.execute(
        text("SELECT name, poz_no FROM ev_catalog_items WHERE discipline_id = :d"),
        {"d": discipline_id},
    )
    return {name: poz_no for name, poz_no in rows}


# ------------------------------------------------------------------ olusturma


async def test_create_gives_next_poz_no_per_discipline(
    client: AsyncClient, admin, disiplin_fabrikasi, seeded_db
) -> None:
    mim = await disiplin_fabrikasi("MIM")
    kab = await disiplin_fabrikasi("KAB")
    first = await _create(client, admin, mim.id, "Kalem A")
    second = await _create(client, admin, mim.id, "Kalem B")
    other = await _create(client, admin, kab.id, "Kalem A")
    assert (first["poz_no"], second["poz_no"], other["poz_no"]) == (
        "MIM-0001",
        "MIM-0002",
        "KAB-0001",
    )
    await seeded_db.refresh(mim)
    assert mim.poz_counter == 2
    await _invariant(seeded_db)


async def test_create_rejects_poz_no_in_body(
    client: AsyncClient, admin, disiplin_fabrikasi
) -> None:
    mim = await disiplin_fabrikasi("MIM")
    resp = await client.post(ITEMS, json=_body(mim.id, "Kalem", poz_no="MIM-0099"), headers=admin)
    assert resp.status_code == 422
    assert "poz_no" in {str(e["loc"][-1]) for e in resp.json()["detail"]}


async def test_update_rejects_poz_no_in_body(
    client: AsyncClient, admin, disiplin_fabrikasi
) -> None:
    mim = await disiplin_fabrikasi("MIM")
    item = await _create(client, admin, mim.id, "Kalem")
    resp = await client.patch(f"{ITEMS}/{item['id']}", json={"poz_no": "X-1"}, headers=admin)
    assert resp.status_code == 422


async def test_list_returns_poz_no(client: AsyncClient, admin, disiplin_fabrikasi) -> None:
    mim = await disiplin_fabrikasi("MIM")
    await _create(client, admin, mim.id, "Kalem")
    rows = (await client.get(ITEMS, headers=admin)).json()
    assert [r["poz_no"] for r in rows] == ["MIM-0001"]


async def test_failed_create_does_not_burn_counter(
    client: AsyncClient, admin, disiplin_fabrikasi, seeded_db
) -> None:
    """Ayni ad+birim 409 → numara alinmadi (tekillik sayactan ONCE sinanir)."""
    mim = await disiplin_fabrikasi("MIM")
    await _create(client, admin, mim.id, "Kalem")
    resp = await client.post(ITEMS, json=_body(mim.id, "kalem"), headers=admin)
    assert resp.status_code == 409
    await seeded_db.refresh(mim)
    assert mim.poz_counter == 1


def test_format_poz_no_pads_to_four_and_overflows() -> None:
    assert core.format_poz_no("MIM", 1) == "MIM-0001"
    assert core.format_poz_no("MIM", 9999) == "MIM-9999"
    assert core.format_poz_no("MIM", 10000) == "MIM-10000"
    assert core.format_poz_no("A-B", 7) == "A-B-0007"


# ----------------------------------------------------------------- monotonluk


async def test_moved_item_frees_old_number_and_it_is_never_reused(
    client: AsyncClient, admin, disiplin_fabrikasi, seeded_db
) -> None:
    mim = await disiplin_fabrikasi("MIM")
    kab = await disiplin_fabrikasi("KAB")
    a = await _create(client, admin, mim.id, "A")
    b = await _create(client, admin, mim.id, "B")
    assert (a["poz_no"], b["poz_no"]) == ("MIM-0001", "MIM-0002")

    moved = await client.patch(
        f"{ITEMS}/{b['id']}", json={"discipline_id": str(kab.id)}, headers=admin
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["poz_no"] == "KAB-0001"

    # MIM-0002 BOSA dustu; sayac geri gelmez → sonraki yeni MIM kalemi MIM-0003
    nxt = await _create(client, admin, mim.id, "C")
    assert nxt["poz_no"] == "MIM-0003"
    assert "MIM-0002" not in (await _poz_nos(seeded_db, mim.id)).values()
    await seeded_db.refresh(mim)
    assert mim.poz_counter == 3
    await _invariant(seeded_db)


async def test_discipline_change_takes_next_number_in_new_discipline(
    client: AsyncClient, admin, disiplin_fabrikasi, seeded_db
) -> None:
    mim = await disiplin_fabrikasi("MIM")
    kab = await disiplin_fabrikasi("KAB")
    await _create(client, admin, kab.id, "K1")
    await _create(client, admin, kab.id, "K2")
    item = await _create(client, admin, mim.id, "M1")
    resp = await client.patch(
        f"{ITEMS}/{item['id']}", json={"discipline_id": str(kab.id)}, headers=admin
    )
    assert resp.json()["poz_no"] == "KAB-0003"
    await seeded_db.refresh(kab)
    assert kab.poz_counter == 3
    await _invariant(seeded_db)


async def test_update_without_discipline_change_keeps_poz_no(
    client: AsyncClient, admin, disiplin_fabrikasi, seeded_db
) -> None:
    mim = await disiplin_fabrikasi("MIM")
    item = await _create(client, admin, mim.id, "M1")
    same = await client.patch(
        f"{ITEMS}/{item['id']}",
        json={"discipline_id": str(mim.id), "name": "M1 yeni"},
        headers=admin,
    )
    assert same.json()["poz_no"] == "MIM-0001"
    await seeded_db.refresh(mim)
    assert mim.poz_counter == 1


# ---------------------------------------------------------------- kod degisimi


async def _patch_code(client: AsyncClient, admin, discipline_id, code: str):
    return await client.patch(f"{DISCS}/{discipline_id}", json={"code": code}, headers=admin)


async def test_code_change_rewrites_prefix_keeps_number_and_counter(
    client: AsyncClient, admin, disiplin_fabrikasi, seeded_db
) -> None:
    mim = await disiplin_fabrikasi("MIM")
    kab = await disiplin_fabrikasi("KAB")
    for name in ("A", "B", "C"):
        await _create(client, admin, mim.id, name)
    await _create(client, admin, kab.id, "A")
    # B'yi tasi → MIM'de sayi 0002 bosa dustu, sayac 3 kaldi
    b_id = next(
        r["id"] for r in (await client.get(ITEMS, headers=admin)).json() if r["name"] == "B"
    )
    await client.patch(f"{ITEMS}/{b_id}", json={"discipline_id": str(kab.id)}, headers=admin)

    resp = await _patch_code(client, admin, mim.id, "MMR")
    assert resp.status_code == 200, resp.text
    assert await _poz_nos(seeded_db, mim.id) == {"A": "MMR-0001", "C": "MMR-0003"}
    assert await _poz_nos(seeded_db, kab.id) == {"A": "KAB-0001", "B": "KAB-0002"}
    await seeded_db.refresh(mim)
    assert mim.poz_counter == 3  # sayac degismez
    await _invariant(seeded_db)


async def test_code_change_with_dash_in_codes_uses_exact_prefix(
    client: AsyncClient, admin, disiplin_fabrikasi, seeded_db
) -> None:
    """Kod `-` icerir: sayi kismi eski onek TAM eslesmesiyle alinir (son `-`ten bolme degil)."""
    d = await disiplin_fabrikasi("A-1")
    await _create(client, admin, d.id, "X")
    assert (await _patch_code(client, admin, d.id, "B-2-3")).status_code == 200
    assert await _poz_nos(seeded_db, d.id) == {"X": "B-2-3-0001"}
    assert (await _patch_code(client, admin, d.id, "Z")).status_code == 200
    assert await _poz_nos(seeded_db, d.id) == {"X": "Z-0001"}
    await _invariant(seeded_db)


async def test_code_swap_between_two_disciplines_without_collision(
    client: AsyncClient, admin, disiplin_fabrikasi, seeded_db
) -> None:
    a = await disiplin_fabrikasi("AAA")
    b = await disiplin_fabrikasi("BBB")
    for name in ("a1", "a2"):
        await _create(client, admin, a.id, name)
    await _create(client, admin, b.id, "b1")
    await _invariant(seeded_db)

    assert (await _patch_code(client, admin, a.id, "TMP")).status_code == 200
    await _invariant(seeded_db)
    assert (await _patch_code(client, admin, b.id, "AAA")).status_code == 200
    await _invariant(seeded_db)
    assert (await _patch_code(client, admin, a.id, "BBB")).status_code == 200
    await _invariant(seeded_db)

    assert await _poz_nos(seeded_db, a.id) == {"a1": "BBB-0001", "a2": "BBB-0002"}
    assert await _poz_nos(seeded_db, b.id) == {"b1": "AAA-0001"}


async def test_reusing_an_old_code_does_not_collide(
    client: AsyncClient, admin, disiplin_fabrikasi, seeded_db
) -> None:
    c = await disiplin_fabrikasi("XYZ")
    d = await disiplin_fabrikasi("DDD")
    await _create(client, admin, c.id, "c1")
    await _create(client, admin, d.id, "d1")
    await _create(client, admin, d.id, "d2")

    assert (await _patch_code(client, admin, c.id, "QQQ")).status_code == 200
    await _invariant(seeded_db)
    assert (await _patch_code(client, admin, d.id, "XYZ")).status_code == 200
    await _invariant(seeded_db)

    assert await _poz_nos(seeded_db, c.id) == {"c1": "QQQ-0001"}
    assert await _poz_nos(seeded_db, d.id) == {"d1": "XYZ-0001", "d2": "XYZ-0002"}
    # eski kodu alan disiplinde yeni kalem: sayac devam eder
    assert (await _create(client, admin, d.id, "d3"))["poz_no"] == "XYZ-0003"


async def test_code_change_taken_code_is_409_and_changes_nothing(
    client: AsyncClient, admin, disiplin_fabrikasi, seeded_db
) -> None:
    a = await disiplin_fabrikasi("AAA")
    await disiplin_fabrikasi("BBB")
    await _create(client, admin, a.id, "a1")
    resp = await _patch_code(client, admin, a.id, "BBB")
    assert resp.status_code == 409
    assert await _poz_nos(seeded_db, a.id) == {"a1": "AAA-0001"}


async def test_non_code_discipline_update_does_not_touch_numbers(
    client: AsyncClient, admin, disiplin_fabrikasi, seeded_db
) -> None:
    a = await disiplin_fabrikasi("AAA")
    await _create(client, admin, a.id, "a1")
    resp = await client.patch(f"{DISCS}/{a.id}", json={"name": "Yeni ad"}, headers=admin)
    assert resp.status_code == 200
    assert await _poz_nos(seeded_db, a.id) == {"a1": "AAA-0001"}


async def test_renumber_raises_when_invariant_is_broken(
    client: AsyncClient, admin, disiplin_fabrikasi, seeded_db
) -> None:
    a = await disiplin_fabrikasi("AAA")
    item = await _create(client, admin, a.id, "a1")
    await seeded_db.execute(
        text("UPDATE ev_catalog_items SET poz_no = 'ZZZ-0001' WHERE id = :i"), {"i": item["id"]}
    )
    with pytest.raises(RuntimeError, match="degismezi bozuk"):
        await core.renumber_for_code_change(seeded_db, a, "AAA", "NEW")


# --------------------------------------------------------------- fiyat damgasi


async def _make_item(session: AsyncSession, discipline) -> EvCatalogItem:
    return await core.create_item(
        session,
        {
            "discipline_id": discipline.id,
            "name": "Fiyatli",
            "uom": "m3",
            "standard_unit_mhr": Decimal("1.5"),
            "default_contractor_type": ContractorType.OWN,
        },
    )


async def test_price_updated_at_follows_ref_price_changes_only(
    seeded_db: AsyncSession, disiplin_fabrikasi
) -> None:
    mim = await disiplin_fabrikasi("MIM")
    item = await _make_item(seeded_db, mim)
    assert item.ref_price is None and item.price_updated_at is None

    # ilk atama → dolu
    await core.update_item(seeded_db, item.id, {"ref_price": Decimal("100.50")})
    first_stamp = item.price_updated_at
    assert first_stamp is not None

    # baska alan degisince DOKUNULMAZ
    await core.update_item(seeded_db, item.id, {"description": "not"})
    assert item.price_updated_at == first_stamp

    # ayni deger tekrar yazilinca DEGISMEZ (Decimal('100.5') == Decimal('100.50'))
    await core.update_item(seeded_db, item.id, {"ref_price": Decimal("100.5")})
    assert item.price_updated_at == first_stamp

    # farkli deger → guncellenir
    await core.update_item(seeded_db, item.id, {"ref_price": Decimal("120.00")})
    assert item.price_updated_at is not None and item.price_updated_at > first_stamp


async def test_create_with_ref_price_stamps_price_updated_at(
    seeded_db: AsyncSession, disiplin_fabrikasi
) -> None:
    mim = await disiplin_fabrikasi("MIM")
    before = datetime.now(UTC)
    item = await core.create_item(
        seeded_db,
        {
            "discipline_id": mim.id,
            "name": "Fiyatli",
            "uom": "m3",
            "standard_unit_mhr": Decimal("1.5"),
            "default_contractor_type": ContractorType.OWN,
            "ref_price": Decimal("10"),
        },
    )
    assert item.price_updated_at is not None and item.price_updated_at >= before


async def test_ref_price_negative_is_rejected_by_check(
    seeded_db: AsyncSession, disiplin_fabrikasi
) -> None:
    from sqlalchemy.exc import IntegrityError

    mim = await disiplin_fabrikasi("MIM")
    item = await _make_item(seeded_db, mim)
    with pytest.raises(IntegrityError, match="ck_ev_catalog_items_ref_price_nonneg"):
        async with seeded_db.begin_nested():
            await core.update_item(seeded_db, item.id, {"ref_price": Decimal("-1")})

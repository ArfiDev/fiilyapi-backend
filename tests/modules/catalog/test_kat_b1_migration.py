"""KAT-B1 — `b4e8d2f6a1c9` migration: `source_code` + kismi UQ + `ref_price_date`.

Tek kullanimlik veritabani (`TEST_DATABASE_URL` veritabani ELLENMEZ, HZ-1 deseni): bir onceki
revizyona cikilir, kalem tohumlanir → upgrade (mevcut satir NULL kalir, kolon/indeks var) →
kismi UQ DB semantigi → downgrade (indeks + kolon yok) → upgrade gidis-donus.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import asyncpg

from tests.modules.treasury.test_hz1_migration import (
    _asyncpg_dsn,
    _create_scratch_database,
    _drop_scratch_database,
    _run_alembic,
)

PREVIOUS = "a9d3b5f7c1e8"
REVISION = "b4e8d2f6a1c9"
UQ = "uq_ev_catalog_items_source_code"
CK = "ck_ev_catalog_items_ref_price_date_requires_price"


async def _ck_var(conn: asyncpg.Connection) -> bool:
    return await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = $1)", CK)


async def _kolonlar(conn: asyncpg.Connection) -> dict[str, tuple[str, str | None]]:
    rows = await conn.fetch(
        "SELECT column_name, is_nullable, data_type, character_maximum_length "
        "FROM information_schema.columns WHERE table_name = 'ev_catalog_items' "
        "AND column_name IN ('source_code', 'ref_price_date')"
    )
    return {
        r["column_name"]: (r["is_nullable"], r["data_type"], r["character_maximum_length"])
        for r in rows
    }


async def _indeks(conn: asyncpg.Connection) -> str | None:
    return await conn.fetchval("SELECT indexdef FROM pg_indexes WHERE indexname = $1", UQ)


async def _tohum(conn: asyncpg.Connection) -> tuple[uuid.UUID, uuid.UUID]:
    disc_id, item_id = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO ev_disciplines (id, code, name, color, default_contractor_type) "
        "VALUES ($1, 'MIG', 'Mig', '#2563EB', 'own')",
        disc_id,
    )
    await _kalem(conn, item_id, disc_id, "k0", "MIG-0001")
    return disc_id, item_id


async def _kalem(conn, item_id, disc_id, ad: str, poz_no: str, source_code=None) -> None:
    sutunlar = "id, discipline_id, name, uom, name_key, uom_key, standard_unit_mhr, "
    sutunlar += "default_contractor_type, poz_no"
    degerler = "$1, $2, $3, 'm', $3, 'm', 1, 'own', $4"
    args = [item_id, disc_id, ad, poz_no]
    if source_code is not None:
        sutunlar += ", source_code"
        degerler += ", $5"
        args.append(source_code)
    await conn.execute(f"INSERT INTO ev_catalog_items ({sutunlar}) VALUES ({degerler})", *args)


async def test_KATB1_migration_kolon_indeks_downgrade_ve_gidis_donus() -> None:
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            disc_id, item_id = await _tohum(conn)
            assert await _kolonlar(conn) == {}
        finally:
            await conn.close()

        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _kolonlar(conn) == {
                "source_code": ("YES", "character varying", 32),
                "ref_price_date": ("YES", "date", None),
            }
            # mevcut satir NULL kalir
            row = await conn.fetchrow(
                "SELECT source_code, ref_price_date FROM ev_catalog_items WHERE id = $1", item_id
            )
            assert row["source_code"] is None and row["ref_price_date"] is None
            definition = await _indeks(conn)
            assert definition is not None
            assert "UNIQUE" in definition and "WHERE (source_code IS NOT NULL)" in definition

            # kismi UQ: iki NULL serbest, ikinci ayni dolu deger CAKISIR, farkli deger serbest
            await _kalem(conn, uuid.uuid4(), disc_id, "k1", "MIG-0002")  # ikinci NULL
            await _kalem(conn, uuid.uuid4(), disc_id, "k2", "MIG-0003", "15.100.1001")
            await _kalem(conn, uuid.uuid4(), disc_id, "k3", "MIG-0004", "15.100.1002")
            try:
                await _kalem(conn, uuid.uuid4(), disc_id, "k4", "MIG-0005", "15.100.1001")
            except asyncpg.UniqueViolationError as exc:
                assert exc.constraint_name == UQ
            else:
                raise AssertionError("ayni dolu kaynak kodu cakismadi — UQ yok")
            # CHECK: fiyatsiz fiyat tarihi DB'de reddedilir; fiyatli tarih serbest
            assert await _ck_var(conn)
            try:
                await conn.execute(
                    "UPDATE ev_catalog_items SET ref_price_date = '2026-01-01' WHERE id = $1",
                    item_id,
                )
            except asyncpg.CheckViolationError as exc:
                assert exc.constraint_name == CK
            else:
                raise AssertionError("fiyatsiz ref_price_date CHECK'e takilmadi")
            await conn.execute(
                "UPDATE ev_catalog_items SET ref_price = 1, ref_price_date = '2026-01-01' "
                "WHERE id = $1",
                item_id,
            )
            await conn.execute(
                "UPDATE ev_catalog_items SET ref_price = NULL, ref_price_date = NULL WHERE id = $1",
                item_id,
            )
            # kolon varsayilani yok: eski konteyner penceresi (kolonsuz INSERT) gecer
            await _kalem(conn, uuid.uuid4(), disc_id, "k5", "MIG-0006")
        finally:
            await conn.close()

        _run_alembic("downgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _kolonlar(conn) == {}
            assert await _indeks(conn) is None
            assert not await _ck_var(conn)
            assert await conn.fetchval("SELECT count(*) FROM ev_catalog_items") == 5
        finally:
            await conn.close()

        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert len(await _kolonlar(conn)) == 2
            assert await _indeks(conn) is not None
            assert await _ck_var(conn)
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


def test_KATB1_migration_kilit_kanonu_lock_timeout_ve_tek_AE_en_basta() -> None:
    """Statik bekci (KARARLAR §2 kilit kanonu): `SET LOCAL lock_timeout` + TEK `LOCK TABLE`,
    yalniz ALTER edilen tablo, ACCESS EXCLUSIVE, her DDL'den ONCE; yukseltme yok."""
    kaynak = (
        Path(__file__).resolve().parents[3]
        / "alembic"
        / "versions"
        / "b4e8d2f6a1c9_kat_b1_kaynak_kod_fiyat_tarihi.py"
    ).read_text(encoding="utf-8")
    kod = "\n".join(satir for satir in kaynak.splitlines() if not satir.lstrip().startswith("#"))
    govde = kod[kod.index("def upgrade()") : kod.index("def downgrade()")]

    assert "SET LOCAL lock_timeout = '10s'" in govde
    assert re.findall(r"LOCK TABLE (\w+) IN ([A-Z ]+) MODE", govde) == [
        ("ev_catalog_items", "ACCESS EXCLUSIVE")
    ]
    assert govde.index("lock_timeout") < govde.index("LOCK TABLE") < govde.index("add_column")
    assert "CONCURRENTLY" not in govde.upper()

"""TKL-B3.1 — `c5d7e9a2b4f6` migration: katalog iz bagi + fiyat degisim damgasi.

Tek kullanimlik veritabani (`TEST_DATABASE_URL` veritabani ELLENMEZ, HZ-1 deseni):
`a2c4e6f81b3d`ye cikilir → bir kalem tohumlanir (`updated_at` bilinen bir an) → upgrade →
mevcut satirin `price_changed_at = updated_at`, `catalog_item_id` NULL; kismi indeks + FK
SET NULL; downgrade/upgrade gidis-donus.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest

from tests.modules.treasury.test_hz1_migration import (
    _asyncpg_dsn,
    _create_scratch_database,
    _drop_scratch_database,
    _run_alembic,
)

PREVIOUS = "a2c4e6f81b3d"
REVISION = "c5d7e9a2b4f6"
UPDATED_AT = datetime(2026, 5, 4, 12, 30, tzinfo=UTC)
IX = "ix_employer_contract_items_catalog_item_id"


async def _seed_contract_item(conn: asyncpg.Connection) -> uuid.UUID:
    project_id, group_id, item_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO projects (id, code, name, budget, progress_pct) "
        "VALUES ($1, 'P-MIG', 'Mig', 0, 0)",
        project_id,
    )
    await conn.execute("INSERT INTO project_contracts (project_id) VALUES ($1)", project_id)
    await conn.execute(
        "INSERT INTO employer_contract_groups (id, project_id, name) VALUES ($1, $2, 'G')",
        group_id,
        project_id,
    )
    await conn.execute(
        "INSERT INTO employer_contract_items (id, project_id, group_id, code, description, "
        "unit, quantity, unit_price, created_at, updated_at) "
        "VALUES ($1, $2, $3, '01', 'Beton', 'm3', 1, 100, $4, $4)",
        item_id,
        project_id,
        group_id,
        UPDATED_AT,
    )
    return item_id


async def _columns(conn: asyncpg.Connection) -> dict[str, str]:
    rows = await conn.fetch(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'employer_contract_items' "
        "AND column_name IN ('catalog_item_id', 'price_changed_at')"
    )
    return {r["column_name"]: r["is_nullable"] for r in rows}


async def _index_def(conn: asyncpg.Connection) -> str | None:
    return await conn.fetchval("SELECT indexdef FROM pg_indexes WHERE indexname = $1", IX)


async def _fk_delete_rule(conn: asyncpg.Connection) -> str | None:
    return await conn.fetchval(
        "SELECT rc.delete_rule FROM information_schema.referential_constraints rc "
        "JOIN information_schema.table_constraints tc "
        "ON tc.constraint_name = rc.constraint_name "
        "WHERE tc.table_name = 'employer_contract_items' "
        "AND tc.constraint_name = 'fk_employer_contract_items_catalog_item_id_ev_catalog_items'"
    )


async def test_TKLB31_migration_backfills_updated_at_and_round_trips() -> None:
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            item_id = await _seed_contract_item(conn)
            assert await _columns(conn) == {}
        finally:
            await conn.close()

        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _columns(conn) == {"catalog_item_id": "YES", "price_changed_at": "NO"}
            row = await conn.fetchrow(
                "SELECT catalog_item_id, price_changed_at, updated_at "
                "FROM employer_contract_items WHERE id = $1",
                item_id,
            )
            assert row["catalog_item_id"] is None
            assert row["price_changed_at"] == row["updated_at"] == UPDATED_AT

            # kismi indeks: yalniz bagli satirlar
            definition = await _index_def(conn)
            assert definition is not None and "WHERE (catalog_item_id IS NOT NULL)" in definition
            assert await _fk_delete_rule(conn) == "SET NULL"

            # eski konteyner penceresi: kolonsuz INSERT server_default ile gecer
            new_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO employer_contract_items (id, project_id, group_id, code, "
                "description, unit, quantity, unit_price) "
                "SELECT $1, project_id, group_id, '02', 'x', 'm', 1, 1 "
                "FROM employer_contract_items WHERE id = $2",
                new_id,
                item_id,
            )
            assert (
                await conn.fetchval(
                    "SELECT price_changed_at IS NOT NULL FROM employer_contract_items "
                    "WHERE id = $1",
                    new_id,
                )
                is True
            )

            # FK ON DELETE SET NULL: katalog satiri silinirse bag NULL'a duser
            disc_id, catalog_id = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO ev_disciplines (id, code, name, color, default_contractor_type) "
                "VALUES ($1, 'MIG', 'Mig', '#2563EB', 'own')",
                disc_id,
            )
            await conn.execute(
                "INSERT INTO ev_catalog_items (id, discipline_id, name, uom, name_key, uom_key, "
                "standard_unit_mhr, default_contractor_type, poz_no) "
                "VALUES ($1, $2, 'k', 'm', 'k', 'm', 1, 'own', 'MIG-0001')",
                catalog_id,
                disc_id,
            )
            await conn.execute(
                "UPDATE employer_contract_items SET catalog_item_id = $1 WHERE id = $2",
                catalog_id,
                item_id,
            )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    "UPDATE employer_contract_items SET catalog_item_id = $1", uuid.uuid4()
                )
            await conn.execute("DELETE FROM ev_catalog_items WHERE id = $1", catalog_id)
            assert (
                await conn.fetchval(
                    "SELECT catalog_item_id FROM employer_contract_items WHERE id = $1", item_id
                )
                is None
            )
        finally:
            await conn.close()

        _run_alembic("downgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _columns(conn) == {}
            assert await _index_def(conn) is None
            assert await _fk_delete_rule(conn) is None
            assert await conn.fetchval("SELECT count(*) FROM employer_contract_items") == 2
        finally:
            await conn.close()

        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _columns(conn) == {"catalog_item_id": "YES", "price_changed_at": "NO"}
            assert await _index_def(conn) is not None
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


def test_TKLB33_migration_kilitleri_ebeveyn_SRE_kalem_AE_ve_lock_timeout() -> None:
    """S1 statik bekçi: kilit YUKSELTMESI/GEREKSIZ KILIT yok. Katalog (yalniz FK hedefi) yalniz
    SHARE ROW EXCLUSIVE, kalem tablosu ACCESS EXCLUSIVE; ebeveyn once; `lock_timeout` var."""
    from pathlib import Path

    kaynak = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "c5d7e9a2b4f6_tkl_b3_sozlesme_kalemi_katalog_bagi.py"
    ).read_text(encoding="utf-8")
    kod = "\n".join(satir for satir in kaynak.splitlines() if not satir.lstrip().startswith("#"))
    govde = kod[kod.index("def upgrade()") : kod.index("def downgrade()")]

    assert "SET LOCAL lock_timeout = '10s'" in govde
    kilitler = re.findall(r"LOCK TABLE (\w+) IN ([A-Z ]+) MODE", govde)
    assert kilitler == [
        ("ev_catalog_items", "SHARE ROW EXCLUSIVE"),
        ("employer_contract_items", "ACCESS EXCLUSIVE"),
    ]
    assert govde.index("lock_timeout") < govde.index("LOCK TABLE")

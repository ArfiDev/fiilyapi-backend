"""TKL-B5.1 — `e4b7c9d1a3f5` migration: sablon tablolari + miktarsiz kalem (SO-21).

Tek kullanimlik veritabani (`TEST_DATABASE_URL` veritabani ELLENMEZ): `d8f1a3c5e7b9`a cikilir →
miktarli kalem tohumlanir → upgrade → NULL miktar kabul / 0 ve negatif RED, sablon tablolari,
kismi UQ (iki varsayilan RED), FK davranislari → downgrade (miktarsiz kalem VARKEN DURUR; yoksa
eski CHECK + NOT NULL geri gelir) → upgrade gidis-donus. Statik kilit bekcisi ayri test.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from pathlib import Path

import asyncpg
import pytest

from tests.modules.treasury.test_hz1_migration import (
    ALEMBIC_CMD,
    BACKEND_DIR,
    _asyncpg_dsn,
    _create_scratch_database,
    _drop_scratch_database,
    _run_alembic,
)

PREVIOUS = "d8f1a3c5e7b9"
REVISION = "e4b7c9d1a3f5"
NEW_TABLES = {"offer_templates", "offer_template_groups", "offer_template_items"}
MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / ("e4b7c9d1a3f5_tkl_b5_sablon_miktar.py")
)


async def _tables(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_name LIKE 'offer_template%'"
    )
    return {r["table_name"] for r in rows}


async def _expect(conn: asyncpg.Connection, constraint: str, sql: str, *args) -> None:
    tr = conn.transaction()
    await tr.start()
    try:
        with pytest.raises((asyncpg.CheckViolationError, asyncpg.UniqueViolationError)) as exc:
            await conn.execute(sql, *args)
        assert exc.value.constraint_name == constraint, (constraint, exc.value)
    finally:
        await tr.rollback()


async def _expect_fk(conn: asyncpg.Connection, sql: str, *args) -> None:
    tr = conn.transaction()
    await tr.start()
    try:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(sql, *args)
    finally:
        await tr.rollback()


class _Zemin:
    """`d8f1a3c5e7b9` semasinda tohumlanan satirlar (miktarli kalem)."""

    employer_id = uuid.uuid4()
    offer_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    group_id = uuid.uuid4()
    catalog_id = uuid.uuid4()
    item_id = uuid.uuid4()


async def _seed_previous(conn: asyncpg.Connection) -> None:
    z = _Zemin
    await conn.execute("INSERT INTO employers (id, name) VALUES ($1, 'Mig İşveren')", z.employer_id)
    await conn.execute(
        "INSERT INTO offers (id, offer_no, employer_id, employer_name, title) "
        "VALUES ($1, 'TKL-2026-0001', $2, 'Mig İşveren', 'Başlık')",
        z.offer_id,
        z.employer_id,
    )
    await conn.execute(
        "INSERT INTO offer_revisions (id, offer_id, rev_no, offer_date, overhead_pct, "
        "profit_pct, vat_pct) VALUES ($1, $2, 0, '2026-10-02', 12, 15, 20)",
        z.revision_id,
        z.offer_id,
    )
    await conn.execute(
        "INSERT INTO offer_groups (id, revision_id, name) VALUES ($1, $2, 'A')",
        z.group_id,
        z.revision_id,
    )
    disc_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO ev_disciplines (id, code, name, color, default_contractor_type) "
        "VALUES ($1, 'MIG', 'Mig', '#2563EB', 'own')",
        disc_id,
    )
    await conn.execute(
        "INSERT INTO ev_catalog_items (id, discipline_id, name, uom, name_key, uom_key, "
        "standard_unit_mhr, default_contractor_type, poz_no) "
        "VALUES ($1, $2, 'k', 'm', 'k', 'm', 1, 'own', 'MIG-0001')",
        z.catalog_id,
        disc_id,
    )
    await conn.execute(
        "INSERT INTO offer_items (id, revision_id, group_id, catalog_item_id, poz_no, "
        "description, unit, quantity, unit_mhr) "
        "VALUES ($1, $2, $3, $4, 'MIG-0001', 'd', 'm', 5, 1)",
        z.item_id,
        z.revision_id,
        z.group_id,
        z.catalog_id,
    )


ITEM_INSERT = (
    "INSERT INTO offer_items (id, revision_id, group_id, catalog_item_id, poz_no, description, "
    "unit, quantity, unit_mhr) VALUES ($1, $2, $3, $4, 'MIG-0001', 'd', 'm', $5, 1)"
)


async def _item(conn: asyncpg.Connection, quantity) -> uuid.UUID:
    z = _Zemin
    item_id = uuid.uuid4()
    await conn.execute(ITEM_INSERT, item_id, z.revision_id, z.group_id, z.catalog_id, quantity)
    return item_id


async def _after_upgrade(conn: asyncpg.Connection) -> None:
    z = _Zemin
    assert await _tables(conn) == NEW_TABLES
    # --- SO-21: NULL miktar kabul; 0 ve negatif RED; eski kalem DURUYOR
    assert await conn.fetchval("SELECT quantity FROM offer_items WHERE id = $1", z.item_id) == 5
    await _item(conn, None)
    for bad in (0, -1):
        await _expect(
            conn,
            "ck_offer_items_quantity_null_or_positive",
            ITEM_INSERT,
            uuid.uuid4(),
            z.revision_id,
            z.group_id,
            z.catalog_id,
            bad,
        )
    assert not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname = 'ck_offer_items_quantity_positive')"
    )
    # --- sablon: tek varsayilan (kismi UQ), ad bos olamaz, oran araligi
    tpl_a, tpl_b = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO offer_templates (id, name, is_default) "
        "VALUES ($1, 'A', true), ($2, 'B', false)",
        tpl_a,
        tpl_b,
    )
    await _expect(
        conn,
        "uq_offer_templates_single_default",
        "INSERT INTO offer_templates (id, name, is_default) VALUES (gen_random_uuid(), 'C', true)",
    )
    await _expect(
        conn,
        "uq_offer_templates_single_default",
        "UPDATE offer_templates SET is_default = true WHERE id = $1",
        tpl_b,
    )
    await conn.execute("INSERT INTO offer_templates (id, name) VALUES (gen_random_uuid(), 'D')")
    await _expect(
        conn,
        "ck_offer_templates_name_not_blank",
        "INSERT INTO offer_templates (id, name) VALUES (gen_random_uuid(), '  ')",
    )
    await _expect(
        conn,
        "ck_offer_templates_profit_range",
        "INSERT INTO offer_templates (id, name, profit_pct) VALUES (gen_random_uuid(), 'E', 1000)",
    )
    # --- grup/kalem: bilesik FK (grup AYNI sablonda), katalog RESTRICT, CASCADE
    group_a, group_b = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO offer_template_groups (id, template_id, name) "
        "VALUES ($1, $2, 'G1'), ($3, $4, 'G2')",
        group_a,
        tpl_a,
        group_b,
        tpl_b,
    )
    ins = (
        "INSERT INTO offer_template_items (id, template_id, group_id, catalog_item_id) "
        "VALUES (gen_random_uuid(), $1, $2, $3)"
    )
    await _expect_fk(conn, ins, tpl_a, group_b, z.catalog_id)  # grup baska sablonun
    await conn.execute(ins, tpl_a, group_a, z.catalog_id)
    await _expect_fk(conn, "DELETE FROM ev_catalog_items WHERE id = $1", z.catalog_id)
    # --- offers.template_id: SET NULL
    await conn.execute("UPDATE offers SET template_id = $1 WHERE id = $2", tpl_a, z.offer_id)
    await _expect_fk(
        conn, "UPDATE offers SET template_id = $1 WHERE id = $2", uuid.uuid4(), z.offer_id
    )
    await conn.execute("DELETE FROM offer_templates WHERE id = $1", tpl_a)
    assert await conn.fetchval("SELECT template_id FROM offers WHERE id = $1", z.offer_id) is None
    assert await conn.fetchval("SELECT count(*) FROM offer_template_items") == 0  # CASCADE


def _alembic_raw(*args: str, database: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": _asyncpg_dsn(database)}
    return subprocess.run(
        [*ALEMBIC_CMD, *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


async def test_TKLB51_migration_miktarsiz_kalem_sablon_ve_gidis_donus() -> None:
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            await _seed_previous(conn)
            # eski semada NULL miktar YOK, 0 YOK
            with pytest.raises(asyncpg.NotNullViolationError):
                await _item(conn, None)
        finally:
            await conn.close()

        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            await _after_upgrade(conn)
        finally:
            await conn.close()

        # miktarsiz kalem VARKEN downgrade DURUR (sessiz miktar uydurulmaz)
        blocked = _alembic_raw("downgrade", PREVIOUS, database=database)
        assert blocked.returncode != 0
        assert "miktarsız teklif kalemi var" in (blocked.stdout + blocked.stderr)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _tables(conn) == NEW_TABLES  # hicbir sey geri alinmadi
            await conn.execute("DELETE FROM offer_items WHERE quantity IS NULL")
        finally:
            await conn.close()

        _run_alembic("downgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _tables(conn) == set()
            assert not await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'offers' AND column_name = 'template_id')"
            )
            # eski CHECK + NOT NULL geri geldi; miktarli kalem korundu
            with pytest.raises(asyncpg.NotNullViolationError):
                await _item(conn, None)
            await _expect(
                conn,
                "ck_offer_items_quantity_positive",
                ITEM_INSERT,
                uuid.uuid4(),
                _Zemin.revision_id,
                _Zemin.group_id,
                _Zemin.catalog_id,
                0,
            )
            assert (
                await conn.fetchval(
                    "SELECT quantity FROM offer_items WHERE id = $1", _Zemin.item_id
                )
                == 5
            )
        finally:
            await conn.close()

        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _tables(conn) == NEW_TABLES
            await _item(conn, None)  # gidis-donus sonrasi NULL miktar yine kabul
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


def _kod(govde: str) -> str:
    return "\n".join(s for s in govde.splitlines() if not s.lstrip().startswith("#"))


def test_TKLB51_migration_kilit_bekcisi() -> None:
    """Statik: `lock_timeout` HER DDL'den once; mevcut tablo ALTER'i TEK ifade (kolon/CHECK
    ayri ayri `op.*` DEGIL); acik kilit yukseltmesi yok; `app` import edilmez; CHECK adi."""
    kaynak = MIGRATION.read_text(encoding="utf-8")
    kod = _kod(kaynak)
    up = kod[kod.index("def upgrade()") : kod.index("def downgrade()")]
    down = kod[kod.index("def downgrade()") :]
    assert "SET LOCAL lock_timeout = '10s'" in up
    ilk_ddl = min(up.index(m) for m in ("create_table", "ALTER TABLE") if m in up)
    assert up.index("lock_timeout") < ilk_ddl
    assert "SET LOCAL lock_timeout" in down
    assert up.count("ALTER TABLE offer_items") == 1  # AE bastan, TEK ifade
    assert up.count("ALTER TABLE offers") == 1
    assert not re.search(r"add_column|alter_column|drop_column|drop_constraint", up + down)
    assert "ACCESS EXCLUSIVE" not in up and "LOCK TABLE" not in up
    assert "ck_offer_items_quantity_null_or_positive" in up
    assert "quantity IS NULL OR quantity > 0" in up
    assert "DROP NOT NULL" in up
    assert not re.search(r"^\s*(from|import) app\b", kod, re.M)

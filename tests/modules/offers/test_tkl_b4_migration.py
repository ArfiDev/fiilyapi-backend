"""TKL-B4.1 — `d8f1a3c5e7b9` migration: teklif cekirdegi tablolari.

Tek kullanimlik veritabani (`TEST_DATABASE_URL` veritabani ELLENMEZ, HZ-1 deseni):
`c5d7e9a2b4f6`ya cikilir → upgrade → tekil ayar tohumu, enum tipleri, CHECK/FK davranisi →
downgrade (YENI enum tipleri duser, PAYLASILAN `price_index_type` KALIR) → upgrade gidis-donus.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import asyncpg
import pytest

from tests.modules.treasury.test_hz1_migration import (
    _asyncpg_dsn,
    _create_scratch_database,
    _drop_scratch_database,
    _run_alembic,
)

PREVIOUS = "c5d7e9a2b4f6"
REVISION = "d8f1a3c5e7b9"
TABLES = {
    "offer_settings",
    "offer_counters",
    "offers",
    "offer_revisions",
    "offer_groups",
    "offer_items",
}
NEW_ENUMS = {"offer_revision_status", "offer_price_escalation"}
PAYMENT = "Ödeme aylık hakedişle, 30 gün vadeli"
MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "d8f1a3c5e7b9_tkl_b4_teklif_cekirdegi.py"
)


async def _tables(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE tablename LIKE 'offer%'")
    return {r["tablename"] for r in rows}


async def _types(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        "SELECT typname FROM pg_type WHERE typname IN "
        "('offer_revision_status', 'offer_price_escalation', 'price_index_type')"
    )
    return {r["typname"] for r in rows}


async def _violates(conn: asyncpg.Connection, constraint: str, sql: str, *args) -> None:
    """`sql` belirtilen kisiti ihlal etmeli (SAVEPOINT: hata baglantiyi bozmasin)."""
    tr = conn.transaction()
    await tr.start()
    try:
        with pytest.raises((asyncpg.CheckViolationError, asyncpg.UniqueViolationError)) as exc:
            await conn.execute(sql, *args)
        assert exc.value.constraint_name == constraint, (constraint, exc.value)
    finally:
        await tr.rollback()


async def _fk_violation(conn: asyncpg.Connection, sql: str, *args) -> None:
    tr = conn.transaction()
    await tr.start()
    try:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(sql, *args)
    finally:
        await tr.rollback()


async def _ok(conn: asyncpg.Connection, sql: str, *args) -> None:
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute(sql, *args)
    finally:
        await tr.rollback()


REV_INSERT = (
    "INSERT INTO offer_revisions (id, offer_id, rev_no, status, offer_date, overhead_pct, "
    "profit_pct, vat_pct, price_escalation, price_index_type, sent_at, won_at, lost_at, "
    "withdrawn_at, lost_reason, winning_amount) "
    "VALUES ($1, $2, 0, $3::offer_revision_status, '2026-10-02', 12, 15, 20, "
    "$4::offer_price_escalation, $5::price_index_type, $6, $7, $8, $9, $10, $11)"
)


def _rev(conn: asyncpg.Connection, offer_id, **kw):
    cols = {
        "status": "draft",
        "esc": "fixed",
        "idx": None,
        "sent": None,
        "won": None,
        "lost": None,
        "wd": None,
        "reason": None,
        "amount": None,
    }
    cols.update(kw)
    return (
        conn,
        REV_INSERT,
        uuid.uuid4(),
        offer_id,
        cols["status"],
        cols["esc"],
        cols["idx"],
        cols["sent"],
        cols["won"],
        cols["lost"],
        cols["wd"],
        cols["reason"],
        cols["amount"],
    )


async def _check_constraints(conn: asyncpg.Connection) -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    employer_id, offer_id = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO employers (id, name) VALUES ($1, 'Migration İşveren')", employer_id
    )
    await conn.execute(
        "INSERT INTO offers (id, offer_no, employer_id, employer_name, title) "
        "VALUES ($1, 'TKL-2026-0001', $2, 'Migration İşveren', 'Başlık')",
        offer_id,
        employer_id,
    )
    # --- ayar tekilligi + aralik
    await _violates(
        conn,
        "offer_settings_only_row_key",
        "INSERT INTO offer_settings (id) VALUES (gen_random_uuid())",
    )
    await _violates(
        conn,
        "ck_offer_settings_validity_range",
        "UPDATE offer_settings SET default_validity_days = 0",
    )
    await _violates(
        conn,
        "ck_offer_settings_validity_range",
        "UPDATE offer_settings SET default_validity_days = 366",
    )
    await _violates(
        conn,
        "ck_offer_settings_profit_range",
        "UPDATE offer_settings SET default_profit_pct = 1000",
    )
    # --- sayac
    await _violates(
        conn, "ck_offer_counters_last_no_positive", "INSERT INTO offer_counters VALUES (2026, 0)"
    )
    # --- teklif no tekil
    await _violates(
        conn,
        "uq_offers_offer_no",
        "INSERT INTO offers (id, offer_no, employer_id, employer_name, title) "
        "VALUES (gen_random_uuid(), 'TKL-2026-0001', $1, 'x', 'y')",
        employer_id,
    )
    # --- fiyat farki <=> endeks turu
    await _violates(
        conn, "ck_offer_revisions_escalation_index", *_rev(conn, offer_id, esc="tuik")[1:]
    )
    await _violates(
        conn, "ck_offer_revisions_escalation_index", *_rev(conn, offer_id, idx="ufe")[1:]
    )
    await _ok(conn, *_rev(conn, offer_id, esc="tuik", idx="tufe")[1:])  # paylasilan tip calisir
    await _ok(conn, *_rev(conn, offer_id)[1:])
    # --- durum damgalari
    await _violates(conn, "ck_offer_revisions_stamp_sent", *_rev(conn, offer_id, status="sent")[1:])
    await _violates(conn, "ck_offer_revisions_stamp_draft", *_rev(conn, offer_id, sent=now)[1:])
    await _violates(
        conn,
        "ck_offer_revisions_stamp_won",
        *_rev(conn, offer_id, status="won", sent=now)[1:],
    )
    await _ok(conn, *_rev(conn, offer_id, status="won", sent=now, won=now)[1:])
    await _violates(
        conn,
        "ck_offer_revisions_stamp_withdrawn",
        *_rev(conn, offer_id, status="sent", sent=now, wd=now)[1:],
    )
    await _ok(conn, *_rev(conn, offer_id, status="withdrawn", wd=now)[1:])  # taslaktan vazgecme
    # --- T37: kayip nedeni / kazanan tutar yalniz `lost` iken
    await _violates(
        conn,
        "ck_offer_revisions_lost_fields_only_lost",
        *_rev(conn, offer_id, reason="Fiyat yüksek")[1:],
    )
    await _violates(
        conn,
        "ck_offer_revisions_lost_fields_only_lost",
        *_rev(conn, offer_id, status="sent", sent=now, amount=100)[1:],
    )
    await _ok(
        conn,
        *_rev(
            conn, offer_id, status="lost", sent=now, lost=now, reason="Fiyat yüksek", amount=1500
        )[1:],
    )
    await _ok(conn, *_rev(conn, offer_id, status="lost", sent=now, lost=now)[1:])  # ikisi opsiyonel
    await _violates(
        conn,
        "ck_offer_revisions_winning_amount_nonneg",
        *_rev(conn, offer_id, status="lost", sent=now, lost=now, amount=-1)[1:],
    )


async def _item_constraints(conn: asyncpg.Connection) -> None:
    """Bilesik FK: kalemin grubu AYNI revizyonda olmali; katalog RESTRICT; CASCADE."""
    employer_id, offer_id, rev_a, rev_b, group_a, group_b = (uuid.uuid4() for _ in range(6))
    disc_id, catalog_id = uuid.uuid4(), uuid.uuid4()
    await conn.execute("INSERT INTO employers (id, name) VALUES ($1, 'İ')", employer_id)
    await conn.execute(
        "INSERT INTO offers (id, offer_no, employer_id, employer_name, title) "
        "VALUES ($1, 'TKL-2026-0002', $2, 'İ', 'T')",
        offer_id,
        employer_id,
    )
    for rid, no in ((rev_a, 0), (rev_b, 1)):
        await conn.execute(
            "INSERT INTO offer_revisions (id, offer_id, rev_no, offer_date, overhead_pct, "
            "profit_pct, vat_pct) VALUES ($1, $2, $3, '2026-10-02', 12, 15, 20)",
            rid,
            offer_id,
            no,
        )
    await conn.execute(
        "INSERT INTO offer_groups (id, revision_id, name) VALUES ($1, $2, 'A'), ($3, $4, 'B')",
        group_a,
        rev_a,
        group_b,
        rev_b,
    )
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
    ins = (
        "INSERT INTO offer_items (id, revision_id, group_id, catalog_item_id, poz_no, description, "
        "unit, quantity, unit_mhr) VALUES ($1, $2, $3, $4, 'MIG-0001', 'd', 'm', $5, $6)"
    )
    # grup baska revizyonun → bilesik FK reddeder
    await _fk_violation(conn, ins, uuid.uuid4(), rev_a, group_b, catalog_id, 1, 1)
    await _violates(
        conn,
        "ck_offer_items_quantity_positive",
        ins,
        uuid.uuid4(),
        rev_a,
        group_a,
        catalog_id,
        0,
        1,
    )
    await _violates(
        conn,
        "ck_offer_items_unit_mhr_positive",
        ins,
        uuid.uuid4(),
        rev_a,
        group_a,
        catalog_id,
        1,
        0,
    )
    # SO-4 / E7: elle teklif B.F. maliyet BOSKEN DB'de reddedilir; maliyetle kabul edilir
    await _violates(
        conn,
        "ck_offer_items_manual_price_needs_cost",
        "INSERT INTO offer_items (id, revision_id, group_id, catalog_item_id, poz_no, "
        "description, unit, quantity, unit_mhr, offer_unit_price) "
        "VALUES ($1, $2, $3, $4, 'MIG-0001', 'd', 'm', 1, 1, 100)",
        uuid.uuid4(),
        rev_a,
        group_a,
        catalog_id,
    )
    await conn.execute(
        "INSERT INTO offer_items (id, revision_id, group_id, catalog_item_id, poz_no, "
        "description, unit, quantity, unit_mhr, cost_unit_price, offer_unit_price) "
        "VALUES ($1, $2, $3, $4, 'MIG-0001', 'd', 'm', 1, 1, 80, 100)",
        uuid.uuid4(),
        rev_a,
        group_a,
        catalog_id,
    )
    item_id = uuid.uuid4()
    await conn.execute(ins, item_id, rev_a, group_a, catalog_id, 1, 1)
    # katalog kalemi referanslidir → silinemez (RESTRICT)
    await _fk_violation(conn, "DELETE FROM ev_catalog_items WHERE id = $1", catalog_id)
    # revizyon silinince grup + kalem CASCADE
    await conn.execute("DELETE FROM offer_revisions WHERE id = $1", rev_a)
    assert await conn.fetchval("SELECT count(*) FROM offer_items WHERE id = $1", item_id) == 0
    assert await conn.fetchval("SELECT count(*) FROM offer_groups WHERE id = $1", group_a) == 0
    # isveren referanslidir → silinemez (RESTRICT)
    await _fk_violation(conn, "DELETE FROM employers WHERE id = $1", employer_id)


async def test_TKLB41_migration_tohum_kisitlar_ve_gidis_donus() -> None:
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _tables(conn) == set()
            assert await _types(conn) == {"price_index_type"}  # projelerden gelir
        finally:
            await conn.close()

        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _tables(conn) == TABLES
            assert await _types(conn) == NEW_ENUMS | {"price_index_type"}

            ayar = await conn.fetch("SELECT * FROM offer_settings")
            assert len(ayar) == 1
            row = ayar[0]
            assert (row["default_overhead_pct"], row["default_profit_pct"]) == (12, 15)
            assert row["default_vat_pct"] == 20
            assert row["default_validity_days"] == 30
            assert row["default_payment_terms"] == PAYMENT
            assert row["only_row"] is True

            # price_index_type PAYLASILIR: kolon projelerin ayni PG tipini kullanir
            assert (
                await conn.fetchval(
                    "SELECT udt_name FROM information_schema.columns "
                    "WHERE table_name = 'offer_revisions' AND column_name = 'price_index_type'"
                )
                == "price_index_type"
            )
            assert await conn.fetchval(
                "SELECT array_agg(e.enumlabel::text ORDER BY e.enumsortorder) FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'offer_revision_status'"
            ) == ["draft", "sent", "won", "lost", "withdrawn"]

            await _check_constraints(conn)
            await _item_constraints(conn)
        finally:
            await conn.close()

        _run_alembic("downgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _tables(conn) == set()
            # yeni tipler DUSTU; PAYLASILAN `price_index_type` DURUYOR (projeler kullanir)
            assert await _types(conn) == {"price_index_type"}
            assert await conn.fetchval("SELECT count(*) FROM project_contracts") == 0
        finally:
            await conn.close()

        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _tables(conn) == TABLES
            assert await conn.fetchval("SELECT count(*) FROM offer_settings") == 1
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


def _kod(govde: str) -> str:
    return "\n".join(s for s in govde.splitlines() if not s.lstrip().startswith("#"))


def test_TKLB41_migration_kilit_bekcisi_lock_timeout_yukseltme_yok() -> None:
    """Statik bekci: `lock_timeout` HER DDL'den once; mevcut tabloya ALTER yok; acik ACCESS
    EXCLUSIVE yok (FK hedefleri `SHARE ROW EXCLUSIVE`i FK ifadesinden zaten alir — docstring'de
    OLCULDU); `price_index_type` yeniden yaratilmaz ve downgrade'de dusurulmez."""
    kaynak = MIGRATION.read_text(encoding="utf-8")
    kod = _kod(kaynak)
    up = kod[kod.index("def upgrade()") : kod.index("def downgrade()")]
    down = kod[kod.index("def downgrade()") :]

    assert "SET LOCAL lock_timeout = '10s'" in up
    ilk_ddl = min(up.index(m) for m in ("create_table", ".create(bind") if m in up)
    assert up.index("lock_timeout") < ilk_ddl
    assert not re.search(r"add_column|alter_column|drop_column|ALTER TABLE", up)
    assert "ACCESS EXCLUSIVE" not in up and "LOCK TABLE" not in up
    assert 'name="price_index_type", create_type=False' in up
    assert "price_index_type" not in down
    assert "offer_revision_status" in kod and "offer_price_escalation" in kod
    assert "SHARE ROW EXCLUSIVE" in kaynak  # kilit olcumu docstring'de yazili
    assert not re.search(r"^\s*(from|import) app\b", kod, re.M)  # `app` import edilmez

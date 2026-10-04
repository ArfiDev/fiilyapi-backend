"""OKT-B1 — ret kaydı migration'ının şema, tur ve veri bekçileri.

Revizyonlara AÇIKÇA çıkılır (`head` / `-1` KULLANILMAZ — `test_ok1a_migration.py`
emsali). Alembic alt süreçte koşar ve test KENDİ TEK KULLANIMLIK veritabanını açar.

⚠️ PG SÜRÜM TUZAĞI: yerel 18, CI 16 — sürüme özgü SQLSTATE iddia edilmez.
"""

import asyncpg
import pytest

from tests.modules.approvals.test_ok1a_migration import (
    _asyncpg_dsn,
    _column_exists,
    _create_scratch_database,
    _current_revision,
    _drop_scratch_database,
    _run_alembic,
)

OKT_REVISION = "b7c3e9a1d5f2"
ONCEKI_REVISION = "e4a8c2d6f1b3"

_ZINCIR_EKLE = (
    "INSERT INTO approval_chains "
    "(id, document_type, document_id, threshold_snapshot, amount_snapshot, created_at, "
    "rejected_at, rejection_reason) "
    "VALUES (gen_random_uuid(), 'purchase_request', $1, 500000, 100, now(), {rej}, {why})"
)


async def _ekle(conn: asyncpg.Connection, document_id, *, reddedilmis: bool) -> None:
    sorgu = _ZINCIR_EKLE.format(
        rej="now()" if reddedilmis else "NULL", why="'gerekce'" if reddedilmis else "NULL"
    )
    await conn.execute(sorgu, document_id)


async def test_upgrade_kolonlari_kisitlari_ve_KISMI_indeksi_kurar() -> None:
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", OKT_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _current_revision(conn) == OKT_REVISION
            for kolon in ("rejected_at", "rejected_by_user_id", "rejection_reason"):
                assert await _column_exists(conn, "approval_chains", kolon), kolon
                assert (
                    await conn.fetchval(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'approval_chains' AND column_name = $1",
                        kolon,
                    )
                    == "YES"
                )
            # Eski tam UNIQUE gitti, yerine KISMI unique indeks geldi.
            assert not await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint "
                "WHERE conname = 'uq_approval_chains_document')"
            )
            tanim = await conn.fetchval(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'uq_approval_chains_open_document'"
            )
            assert tanim is not None and "UNIQUE" in tanim and "rejected_at IS NULL" in tanim, tanim
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint "
                "WHERE conname = 'ck_approval_chains_rejection_pair')"
            )
            # reddeden kullanıcı FK'sı SET NULL: ret izi kullanıcıdan sağ çıkar.
            silme = await conn.fetchval(
                "SELECT confdeltype FROM pg_constraint "
                "WHERE conname = 'approval_chains_rejected_by_user_id_fkey'"
            )
            assert silme in ("n", b"n")
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_kismi_indeks_reddedilmisleri_BIRIKTIRIR_acik_zinciri_TEKLER() -> None:
    import uuid

    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", OKT_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            belge = uuid.uuid4()
            await _ekle(conn, belge, reddedilmis=True)
            await _ekle(conn, belge, reddedilmis=True)
            await _ekle(conn, belge, reddedilmis=False)
            with pytest.raises(asyncpg.UniqueViolationError):
                await _ekle(conn, belge, reddedilmis=False)
            # Gerekçesiz ret damgası DB kısıtına takılır.
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO approval_chains "
                    "(id, document_type, document_id, threshold_snapshot, created_at, rejected_at) "
                    "VALUES (gen_random_uuid(), 'purchase_request', gen_random_uuid(), "
                    "1, now(), now())"
                )
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_downgrade_reddedilmisleri_siler_eski_UNIQUE_gelir_ve_ikinci_upgrade_patlamaz() -> (
    None
):
    import uuid

    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", OKT_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            belge = uuid.uuid4()
            await _ekle(conn, belge, reddedilmis=True)
            await _ekle(conn, belge, reddedilmis=False)
        finally:
            await conn.close()

        _run_alembic("downgrade", ONCEKI_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _current_revision(conn) == ONCEKI_REVISION
            assert not await _column_exists(conn, "approval_chains", "rejected_at")
            assert await conn.fetchval("SELECT count(*) FROM approval_chains") == 1
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint "
                "WHERE conname = 'uq_approval_chains_document')"
            )
        finally:
            await conn.close()

        _run_alembic("upgrade", OKT_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _current_revision(conn) == OKT_REVISION
            assert await conn.fetchval("SELECT count(*) FROM approval_chains") == 1
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

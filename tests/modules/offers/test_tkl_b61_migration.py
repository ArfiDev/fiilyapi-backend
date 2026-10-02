"""TKL-B6.1 — iki migration: `f2a6c8e0b4d7` (enum degeri `'offer'`) ve `a9d3b5f7c1e8`
(`offers` donusturme kolonlari + `ev_contract_item_rates`).

Tek kullanimlik veritabani (`TEST_DATABASE_URL` veritabani ELLENMEZ). Yerel sunucu PG 16'dir
(CI ile ayni ana surum): `ADD VALUE`nun ayni islemde kullanimi GERCEKTEN `unsafe use of new
value` verir — bu yuzden Z1 mutasyonu yerelde de kirmizidir. Statik bekciler ayri testler.
"""

from __future__ import annotations

import ast
import re
import subprocess
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.modules.earned_value.models import RateSource
from tests.modules.treasury.test_hz1_migration import (
    ALEMBIC_CMD,
    BACKEND_DIR,
    _asyncpg_dsn,
    _create_scratch_database,
    _drop_scratch_database,
    _run_alembic,
    _seed_user,
)

BASE = "e4b7c9d1a3f5"
REV_A = "f2a6c8e0b4d7"
REV_B = "a9d3b5f7c1e8"
OLD_LABELS = ["catalog", "history", "manual"]
VERSIONS = Path(__file__).resolve().parents[3] / "alembic" / "versions"
MIG_A = VERSIONS / "f2a6c8e0b4d7_tkl_b6_rate_source_offer.py"
MIG_B = VERSIONS / "a9d3b5f7c1e8_tkl_b6_donusturme.py"


def _alembic_rc(*args: str, database: str) -> subprocess.CompletedProcess[str]:
    """`_run_alembic` gibi ama BASARISIZLIGI testin kendisi degerlendirir (fail-closed testi)."""
    import os

    env = {**os.environ, "DATABASE_URL": _asyncpg_dsn(database)}
    return subprocess.run(
        [*ALEMBIC_CMD, *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


async def _labels(conn: asyncpg.Connection) -> list[str]:
    return await conn.fetchval(
        "SELECT array_agg(e.enumlabel::text ORDER BY e.enumsortorder) FROM pg_enum e "
        "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'ev_rate_source'"
    )


async def _using_columns(conn: asyncpg.Connection) -> set[tuple[str, str]]:
    rows = await conn.fetch(
        "SELECT c.relname, a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "WHERE a.atttypid = 'ev_rate_source'::regtype AND a.attnum > 0 AND NOT a.attisdropped "
        "AND c.relkind = 'r'"
    )
    return {(r["relname"], r["attname"]) for r in rows}


async def _expect_violation(conn, exc, constraint: str | None, sql: str, *args) -> None:
    tr = conn.transaction()
    await tr.start()
    try:
        with pytest.raises(exc) as info:
            await conn.execute(sql, *args)
        if constraint:
            assert info.value.constraint_name == constraint, (constraint, info.value)
    finally:
        await tr.rollback()


# ------------------------------------------------------------------ migration A


async def test_TKLB61A_enum_degeri_eklenir_downgrade_kapisi_ve_tip_yeniden_kurulur() -> None:
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", BASE, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _labels(conn) == OLD_LABELS
            before = await _using_columns(conn)
            assert before == {
                ("ev_leaf_settings", "rate_source"),
                ("ev_baseline_leaves", "rate_source"),
            }
        finally:
            await conn.close()

        _run_alembic("upgrade", REV_A, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _labels(conn) == [*OLD_LABELS, "offer"]
            # DB enum'u ile Python enum'u AYNI kume ve sira (RateSource esitlik bekcisi)
            assert await _labels(conn) == [m.value for m in RateSource]
            # Degeri YAZMAK ayri (autocommit) islemde serbesttir; probe tablo dinamik kesfi olcer.
            await conn.execute("CREATE TABLE tmp_rate_probe (s ev_rate_source)")
            await conn.execute("INSERT INTO tmp_rate_probe VALUES ('offer'), ('manual')")
        finally:
            await conn.close()

        # 🔴 deger kullanilirken downgrade FAIL-CLOSED (probe `pg_attribute` kesfiyle bulunur)
        result = _alembic_rc("downgrade", BASE, database=database)
        assert result.returncode != 0
        cikti = result.stderr + result.stdout
        # DOGRU SEBEP: bizim kapimiz (PG'nin `invalid input value for enum` hatasi DEGIL)
        assert "RuntimeError" in cikti and "downgrade durduruldu" in cikti, cikti[-600:]
        assert "tmp_rate_probe.s" in cikti and "1 satir" in cikti
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _labels(conn) == [*OLD_LABELS, "offer"]  # tip DEGISMEDI (geri alindi)
            assert await conn.fetchval("SELECT count(*) FROM tmp_rate_probe") == 2
            await conn.execute("DELETE FROM tmp_rate_probe WHERE s = 'offer'")
        finally:
            await conn.close()

        # deger kullanilmiyor → tip yeniden kurulur, kolonlar (probe dahil) yeni tipe cevrilir
        _run_alembic("downgrade", BASE, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _labels(conn) == OLD_LABELS
            assert await _using_columns(conn) == before | {("tmp_rate_probe", "s")}
            assert await conn.fetchval("SELECT s::text FROM tmp_rate_probe") == "manual"
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM pg_type WHERE typname = 'ev_rate_source_old'"
                )
                == 0
            )
            await conn.execute("DROP TABLE tmp_rate_probe")
        finally:
            await conn.close()

        _run_alembic("upgrade", REV_A, database=database)  # gidis-donus
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _labels(conn) == [*OLD_LABELS, "offer"]
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


# ------------------------------------------------------------------ migration B


class _Ids:
    def __init__(self) -> None:
        self.employer, self.offer, self.project, self.group, self.item = (
            uuid.uuid4() for _ in range(5)
        )


async def _seed_project_item(conn: asyncpg.Connection, ids: _Ids) -> None:
    await conn.execute(
        "INSERT INTO projects (id, code, name, budget, progress_pct) "
        "VALUES ($1, 'P-B61', 'Mig', 0, 0)",
        ids.project,
    )
    await conn.execute("INSERT INTO project_contracts (project_id) VALUES ($1)", ids.project)
    await conn.execute(
        "INSERT INTO employer_contract_groups (id, project_id, name) VALUES ($1, $2, 'G')",
        ids.group,
        ids.project,
    )
    await conn.execute(
        "INSERT INTO employer_contract_items (id, project_id, group_id, code, description, "
        "unit, quantity, unit_price) VALUES ($1, $2, $3, '01', 'Beton', 'm3', 1, 100)",
        ids.item,
        ids.project,
        ids.group,
    )


OFFER_INSERT = (
    "INSERT INTO offers (id, offer_no, employer_id, employer_name, title, project_id, "
    "converted_at, converted_by_user_id) VALUES ($1, $2, $3, 'İ', 'T', $4, {at}, $5)"
)


async def _check_offers(conn: asyncpg.Connection, ids: _Ids, user_id: uuid.UUID) -> None:
    await conn.execute("INSERT INTO employers (id, name) VALUES ($1, 'İ')", ids.employer)
    # eski yazim (yeni kolonlar yok) → ikisi de NULL, CHECK gecer
    await conn.execute(
        "INSERT INTO offers (id, offer_no, employer_id, employer_name, title) "
        "VALUES ($1, 'TKL-2026-0001', $2, 'İ', 'T')",
        uuid.uuid4(),
        ids.employer,
    )
    # 🔴 Z2: cift tutarliligi — yalniz biri dolu → CHECK
    await _expect_violation(
        conn,
        asyncpg.CheckViolationError,
        "ck_offers_conversion_pair",
        OFFER_INSERT.format(at="NULL"),
        uuid.uuid4(),
        "TKL-2026-0002",
        ids.employer,
        ids.project,
        None,
    )
    await _expect_violation(
        conn,
        asyncpg.CheckViolationError,
        "ck_offers_conversion_pair",
        OFFER_INSERT.format(at="now()"),
        uuid.uuid4(),
        "TKL-2026-0003",
        ids.employer,
        None,
        None,
    )
    # ikisi dolu → gecer
    await conn.execute(
        OFFER_INSERT.format(at="now()"),
        ids.offer,
        "TKL-2026-0004",
        ids.employer,
        ids.project,
        user_id,
    )
    # bir proje TEK tekliften (UQ)
    await _expect_violation(
        conn,
        asyncpg.UniqueViolationError,
        "uq_offers_project_id",
        OFFER_INSERT.format(at="now()"),
        uuid.uuid4(),
        "TKL-2026-0005",
        ids.employer,
        ids.project,
        None,
    )
    # RESTRICT: donusturulmus proje silinemez
    await _expect_violation(
        conn,
        asyncpg.ForeignKeyViolationError,
        None,
        "DELETE FROM projects WHERE id = $1",
        ids.project,
    )
    # SET NULL: kullanici silinince iz kalir, `converted_by_user_id` NULL olur
    await conn.execute("DELETE FROM users WHERE id = $1", user_id)
    row = await conn.fetchrow(
        "SELECT project_id, converted_at, converted_by_user_id FROM offers WHERE id = $1", ids.offer
    )
    assert row["project_id"] == ids.project and row["converted_at"] is not None
    assert row["converted_by_user_id"] is None


async def _check_rates(conn: asyncpg.Connection, ids: _Ids) -> None:
    ins = (
        "INSERT INTO ev_contract_item_rates (contract_item_id, unit_mhr, source) "
        "VALUES ($1, $2, $3)"
    )
    await _expect_violation(
        conn,
        asyncpg.CheckViolationError,
        "ck_ev_contract_item_rates_unit_mhr_positive",
        ins,
        ids.item,
        0,
        "manual",
    )
    await _expect_violation(
        conn,
        asyncpg.NotNullViolationError,
        None,
        "INSERT INTO ev_contract_item_rates (contract_item_id, unit_mhr, source) "
        "VALUES ($1, 1, NULL)",
        ids.item,
    )
    # 'offer' degeri AYRI islemde (conn autocommit) yazilir — migration'dan sonra serbest
    await conn.execute(
        "INSERT INTO ev_contract_item_rates (contract_item_id, unit_mhr, source) "
        "VALUES ($1, 1.2500, 'offer')",
        ids.item,
    )
    row = await conn.fetchrow("SELECT * FROM ev_contract_item_rates")
    assert (str(row["unit_mhr"]), row["source"]) == ("1.2500", "offer")
    assert row["created_at"] is not None and row["updated_at"] is not None
    # PK tekil: kalem basina TEK yuva
    await _expect_violation(
        conn,
        asyncpg.UniqueViolationError,
        "ev_contract_item_rates_pkey",
        ins,
        ids.item,
        1,
        "manual",
    )
    # kalem silinince yuva CASCADE ile gider
    await conn.execute("DELETE FROM employer_contract_items WHERE id = $1", ids.item)
    assert await conn.fetchval("SELECT count(*) FROM ev_contract_item_rates") == 0


async def test_TKLB61B_migration_kisitlar_cascade_ve_gidis_donus() -> None:
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", REV_A, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'offers' AND column_name IN "
                    "('project_id', 'converted_at', 'converted_by_user_id')"
                )
                == 0
            )
            assert await conn.fetchval("SELECT to_regclass('ev_contract_item_rates')") is None
        finally:
            await conn.close()

        _run_alembic("upgrade", REV_B, database=database)
        ids = _Ids()
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert ("ev_contract_item_rates", "source") in await _using_columns(conn)
            await _seed_project_item(conn, ids)
            user_id = await _seed_user(conn)
            await _check_rates(conn, ids)
            await _check_offers(conn, ids, user_id)
        finally:
            await conn.close()

        # donusturulmus teklif VARKEN downgrade DURUR (iz sessizce silinmez)
        result = _alembic_rc("downgrade", REV_A, database=database)
        assert result.returncode != 0 and "donusturulmus" in result.stderr + result.stdout
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            await conn.execute(
                "UPDATE offers SET project_id = NULL, converted_at = NULL "
                "WHERE project_id IS NOT NULL"
            )
        finally:
            await conn.close()

        _run_alembic("downgrade", REV_A, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await conn.fetchval("SELECT to_regclass('ev_contract_item_rates')") is None
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'offers' AND column_name IN "
                    "('project_id', 'converted_at', 'converted_by_user_id')"
                )
                == 0
            )
        finally:
            await conn.close()

        _run_alembic("upgrade", REV_B, database=database)  # gidis-donus
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await conn.fetchval("SELECT to_regclass('ev_contract_item_rates')") is not None
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


# --------------------------------------------------------------- statik bekciler


def _fonksiyon_govdesi(yol: Path, ad: str) -> str:
    """Fonksiyonun KOD govdesi (docstring ve yorumlar HARIC) — `ast` ile."""
    kaynak = yol.read_text(encoding="utf-8")
    agac = ast.parse(kaynak)
    (fn,) = [n for n in agac.body if isinstance(n, ast.FunctionDef) and n.name == ad]
    parcalar = [ast.get_source_segment(kaynak, s) or "" for s in fn.body]
    return "\n".join(p for p in parcalar if not p.lstrip().startswith("#"))


def _revizyon(yol: Path) -> tuple[str, str]:
    kaynak = yol.read_text(encoding="utf-8")
    rev = re.search(r'^revision: str = "(\w+)"', kaynak, re.M)
    down = re.search(r'^down_revision: .*= "(\w+)"', kaynak, re.M)
    assert rev and down
    return rev.group(1), down.group(1)


def test_TKLB61_Z1_add_value_kendi_migration_inde_ve_deger_kullanilmaz() -> None:
    """🔴 PG 12-16: `ADD VALUE` ile eklenen deger AYNI islemde kullanilamaz. A'nin upgrade'i
    YALNIZ `ADD VALUE` (+ `lock_timeout`) icerir; `'offer'` literali B'nin kodunda gecmez;
    zincir BASE → A → B."""
    assert _revizyon(MIG_A) == (REV_A, BASE)
    assert _revizyon(MIG_B) == (REV_B, REV_A)  # B, A'dan SONRA (ayri islem)

    up_a = _fonksiyon_govdesi(MIG_A, "upgrade")
    ifadeler = [s.strip() for s in up_a.splitlines() if s.strip()]
    assert len(ifadeler) == 2, ifadeler
    assert "lock_timeout" in ifadeler[0]
    assert re.fullmatch(
        r"op\.execute\(f\"ALTER TYPE \{ENUM_NAME\} ADD VALUE IF NOT EXISTS '\{NEW_MEMBER\}'\"\)",
        ifadeler[1],
    ), ifadeler[1]

    for ad in ("upgrade", "downgrade"):
        kod_b = _fonksiyon_govdesi(MIG_B, ad)
        assert not re.search(r"'offer'|\"offer\"", kod_b), f"B.{ad}: 'offer' literali kullanilamaz"
        assert "ADD VALUE" not in kod_b
    assert "ADD VALUE" not in MIG_B.read_text(encoding="utf-8").split("def upgrade")[1]


def test_TKLB61_migration_kilit_bekcisi() -> None:
    """`lock_timeout` HER DDL'den once; `offers` ALTER'i TEK ifade; `app` import edilmez."""
    for yol in (MIG_A, MIG_B):
        kaynak = yol.read_text(encoding="utf-8")
        kod = "\n".join(s for s in kaynak.splitlines() if not s.lstrip().startswith("#"))
        assert not re.search(r"^\s*(from|import) app\b", kod, re.M)
        up = _fonksiyon_govdesi(yol, "upgrade")
        assert up.index("SET LOCAL lock_timeout = '10s'") == up.index("lock_timeout") - len(
            "SET LOCAL "
        )
        ilk_ddl = min(up.index(m) for m in ("ALTER TYPE", "ALTER TABLE", "create_table") if m in up)
        assert up.index("lock_timeout") < ilk_ddl
    up_b = _fonksiyon_govdesi(MIG_B, "upgrade")
    assert up_b.count("ALTER TABLE offers") == 1  # AE bastan, tek ifade
    assert "SHARE ROW EXCLUSIVE" in MIG_B.read_text(encoding="utf-8")  # kilit olcumu yazili
    down_a = _fonksiyon_govdesi(MIG_A, "downgrade")
    assert down_a.index("ACCESS EXCLUSIVE") < down_a.index("RuntimeError")  # once kilit, sonra kapi
    assert "RuntimeError" in down_a

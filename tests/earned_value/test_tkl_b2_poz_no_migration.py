"""TKL-B2 — `a2c4e6f81b3d` migration: canli kalemlerin otomatik numaralanmasi + NOT NULL/UQ.

Tek kullanimlik veritabani (`TEST_DATABASE_URL` veritabani ELLENMEZ, HZ-1 deseni):
`7e3b1c9a4f20`ye cikilir → 4 disiplin / 28 kalem tohumlanir (ayni `created_at`liler, farkli
`created_at`liler, ayni `name_key`+`created_at`te `id` ikincil anahtari, `ç`/`ı` ile
KOD NOKTASI sirasi) → upgrade → numaralar ELLE yazilmis sabit listeyle esit.

Siralama kurali: disiplin icinde `created_at ASC, name_key ASC, id ASC` — `name_key` Python
KOD NOKTASI sirasidir (DB harmanlamasi DEGIL): `ç` (U+00E7) tum ASCII harflerden SONRA gelir.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from types import ModuleType

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.modules.catalog.service import format_poz_no
from app.modules.earned_value.labels import normalize_label
from tests.modules.treasury.test_hz1_migration import (
    BACKEND_DIR,
    _asyncpg_dsn,
    _create_scratch_database,
    _drop_scratch_database,
    _run_alembic,
)

PREVIOUS = "7e3b1c9a4f20"
REVISION = "a2c4e6f81b3d"
MIGRATION_PATH = next((BACKEND_DIR / "alembic" / "versions").glob(f"{REVISION}_*.py"))

T0 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
T1 = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
T2 = datetime(2026, 3, 3, 10, 0, tzinfo=UTC)
ID_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
ID_B = uuid.UUID("00000000-0000-0000-0000-000000000002")

#: (disiplin kodu, ad, birim, created_at, sabit id | None)
SEED: list[tuple[str, str, str, datetime, uuid.UUID | None]] = [
    # KAB (12): ayni created_at'te name_key sirasi; ç ASCII'den SONRA
    ("KAB", "Kalıp", "m2", T1, None),
    ("KAB", "Beton", "m3", T1, None),
    ("KAB", "Demir", "ton", T0, None),
    ("KAB", "Zemin", "m2", T2, None),
    ("KAB", "Çelik", "ton", T1, None),
    ("KAB", "Ahşap", "m2", T2, None),
    ("KAB", "Boya", "m2", T0, None),
    ("KAB", "Sıva", "m2", T1, None),
    ("KAB", "Epoksi", "m2", T0, None),
    ("KAB", "Yalıtım", "m2", T2, None),
    ("KAB", "Duvar", "m2", T1, None),
    ("KAB", "Alçı", "m2", T0, None),
    # MIM (10): HEPSI ayni created_at; "Boya" iki birim → id ikincil anahtari
    ("MIM", "Pencere", "ad", T1, None),
    ("MIM", "Boya", "m3", T1, ID_B),
    ("MIM", "Çatı", "m2", T1, None),
    ("MIM", "Kapı", "ad", T1, None),
    ("MIM", "Boya", "m2", T1, ID_A),
    ("MIM", "Merdiven", "ad", T1, None),
    ("MIM", "Asansör", "ad", T1, None),
    ("MIM", "Elektrik", "m", T1, None),
    ("MIM", "Dolap", "ad", T1, None),
    ("MIM", "Cephe", "m2", T1, None),
    # ELK (6): created_at ARTAR ama ad ters alfabetik → created_at oncelikli olmali
    ("ELK", "F", "ad", datetime(2026, 4, 1, 9, 0, tzinfo=UTC), None),
    ("ELK", "E", "ad", datetime(2026, 4, 1, 9, 0, 0, 1, tzinfo=UTC), None),
    ("ELK", "D", "ad", datetime(2026, 4, 2, 9, 0, tzinfo=UTC), None),
    ("ELK", "C", "ad", datetime(2026, 4, 3, 9, 0, tzinfo=UTC), None),
    ("ELK", "B", "ad", datetime(2026, 4, 4, 9, 0, tzinfo=UTC), None),
    ("ELK", "A", "ad", datetime(2026, 4, 5, 9, 0, tzinfo=UTC), None),
]
assert len(SEED) == 28

#: ELLE yazilmis beklenen numaralar: (kod, ad, birim) → poz no.
EXPECTED: dict[tuple[str, str, str], str] = {
    # KAB: T0 {Alçı, Boya, Demir, Epoksi} · T1 {Beton, Duvar, Kalıp, Sıva, Çelik}
    #      · T2 {Ahşap, Yalıtım, Zemin}
    ("KAB", "Alçı", "m2"): "KAB-0001",
    ("KAB", "Boya", "m2"): "KAB-0002",
    ("KAB", "Demir", "ton"): "KAB-0003",
    ("KAB", "Epoksi", "m2"): "KAB-0004",
    ("KAB", "Beton", "m3"): "KAB-0005",
    ("KAB", "Duvar", "m2"): "KAB-0006",
    ("KAB", "Kalıp", "m2"): "KAB-0007",
    ("KAB", "Sıva", "m2"): "KAB-0008",
    ("KAB", "Çelik", "ton"): "KAB-0009",
    ("KAB", "Ahşap", "m2"): "KAB-0010",
    ("KAB", "Yalıtım", "m2"): "KAB-0011",
    ("KAB", "Zemin", "m2"): "KAB-0012",
    # MIM: hepsi T1 → name_key, esitte id (Boya m2 = ...01 once)
    ("MIM", "Asansör", "ad"): "MIM-0001",
    ("MIM", "Boya", "m2"): "MIM-0002",
    ("MIM", "Boya", "m3"): "MIM-0003",
    ("MIM", "Cephe", "m2"): "MIM-0004",
    ("MIM", "Dolap", "ad"): "MIM-0005",
    ("MIM", "Elektrik", "m"): "MIM-0006",
    ("MIM", "Kapı", "ad"): "MIM-0007",
    ("MIM", "Merdiven", "ad"): "MIM-0008",
    ("MIM", "Pencere", "ad"): "MIM-0009",
    ("MIM", "Çatı", "m2"): "MIM-0010",
    # ELK: created_at artan
    ("ELK", "F", "ad"): "ELK-0001",
    ("ELK", "E", "ad"): "ELK-0002",
    ("ELK", "D", "ad"): "ELK-0003",
    ("ELK", "C", "ad"): "ELK-0004",
    ("ELK", "B", "ad"): "ELK-0005",
    ("ELK", "A", "ad"): "ELK-0006",
}
EXPECTED_COUNTERS = {"KAB": 12, "MIM": 10, "ELK": 6, "BOS": 0}


def _load() -> ModuleType:
    name = f"_migration_{MIGRATION_PATH.stem}"
    spec = importlib.util.spec_from_file_location(name, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def _seed(conn: asyncpg.Connection) -> None:
    discs: dict[str, uuid.UUID] = {}
    for code in EXPECTED_COUNTERS:  # BOS kalemsiz disiplin
        discs[code] = uuid.uuid4()
        await conn.execute(
            "INSERT INTO ev_disciplines (id, code, name, color, default_contractor_type) "
            "VALUES ($1, $2, $2, '#2563EB', 'own')",
            discs[code],
            code,
        )
    for code, name, uom, created_at, item_id in SEED:
        await conn.execute(
            "INSERT INTO ev_catalog_items (id, discipline_id, name, uom, name_key, uom_key, "
            "standard_unit_mhr, default_contractor_type, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, 1, 'own', $7)",
            item_id or uuid.uuid4(),
            discs[code],
            name,
            uom,
            normalize_label(name),
            normalize_label(uom),
            created_at,
        )


async def _numbers(conn: asyncpg.Connection) -> dict[tuple[str, str, str], str]:
    rows = await conn.fetch(
        "SELECT d.code, c.name, c.uom, c.poz_no FROM ev_catalog_items c "
        "JOIN ev_disciplines d ON d.id = c.discipline_id"
    )
    return {(r["code"], r["name"], r["uom"]): r["poz_no"] for r in rows}


async def _counters(conn: asyncpg.Connection) -> dict[str, int]:
    return {
        r["code"]: r["poz_counter"]
        for r in await conn.fetch("SELECT code, poz_counter FROM ev_disciplines")
    }


async def _columns(conn: asyncpg.Connection) -> dict[tuple[str, str], str]:
    rows = await conn.fetch(
        "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
        "WHERE (table_name, column_name) IN (('ev_catalog_items','poz_no'),"
        "('ev_catalog_items','ref_price'),('ev_catalog_items','price_updated_at'),"
        "('ev_disciplines','poz_counter'))"
    )
    return {(r["table_name"], r["column_name"]): r["is_nullable"] for r in rows}


async def _constraints(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        "SELECT conname FROM pg_constraint WHERE conname IN "
        "('uq_ev_catalog_items_poz_no','ck_ev_catalog_items_ref_price_nonneg',"
        "'ck_ev_disciplines_poz_counter_nonneg')"
    )
    return {r["conname"] for r in rows}


# ------------------------------------------------------------------ DB'siz


def test_expected_table_is_hand_consistent() -> None:
    """Sabit liste kendi icinde tutarli: her (kod, ad, birim) tohumda var, numara 1..n tam."""
    assert set(EXPECTED) == {(c, n, u) for c, n, u, _t, _i in SEED}
    for code, count in EXPECTED_COUNTERS.items():
        assert sorted(v for (c, _n, _u), v in EXPECTED.items() if c == code) == [
            format_poz_no(code, i) for i in range(1, count + 1)
        ]


def test_frozen_format_equals_app_format() -> None:
    mig = _load()
    for code in ("MIM", "A-1", "KAB"):
        for seq in (1, 9, 10, 999, 1000, 9999, 10000, 123456):
            assert mig._format_poz_no(code, seq) == format_poz_no(code, seq)  # noqa: SLF001


def test_number_items_orders_by_created_at_then_name_key_then_id() -> None:
    mig = _load()
    d = uuid.uuid4()
    i1, i2, i3, i4 = (uuid.UUID(int=n) for n in (1, 2, 3, 4))
    items = [
        (i4, d, T1, "b"),
        (i3, d, T1, "a"),  # ayni created_at, name_key kucuk → once
        (i2, d, T2, "a"),
        (i1, d, T0, "z"),  # created_at en erken → ad buyuk olsa da ilk
    ]
    numbers, counters = mig._number_items([(d, "XXX")], items)  # noqa: SLF001
    assert numbers == {i1: "XXX-0001", i3: "XXX-0002", i4: "XXX-0003", i2: "XXX-0004"}
    assert counters == {d: 4}


def test_number_items_empty_discipline_counter_zero() -> None:
    mig = _load()
    d = uuid.uuid4()
    assert mig._number_items([(d, "BOS")], []) == ({}, {d: 0})  # noqa: SLF001


# ------------------------------------------------------------------ gecici DB


async def test_TKLB2_migration_numbers_live_items_and_round_trips() -> None:
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            await _seed(conn)
            assert await _columns(conn) == {}
        finally:
            await conn.close()

        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _numbers(conn) == EXPECTED
            assert await _counters(conn) == EXPECTED_COUNTERS
            assert await _columns(conn) == {
                ("ev_catalog_items", "poz_no"): "NO",
                ("ev_catalog_items", "ref_price"): "YES",
                ("ev_catalog_items", "price_updated_at"): "YES",
                ("ev_disciplines", "poz_counter"): "NO",
            }
            assert await _constraints(conn) == {
                "uq_ev_catalog_items_poz_no",
                "ck_ev_catalog_items_ref_price_nonneg",
                "ck_ev_disciplines_poz_counter_nonneg",
            }
            assert (
                await conn.fetchval(
                    "SELECT numeric_precision || ',' || numeric_scale "
                    "FROM information_schema.columns "
                    "WHERE table_name='ev_catalog_items' AND column_name='ref_price'"
                )
                == "18,2"
            )
            # UQ gercekten isliyor (sirket geneli tekil)
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "UPDATE ev_catalog_items SET poz_no = 'KAB-0001' WHERE poz_no = 'KAB-0002'"
                )
            with pytest.raises(asyncpg.NotNullViolationError):
                await conn.execute("UPDATE ev_catalog_items SET poz_no = NULL")
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE ev_catalog_items SET ref_price = -1")
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE ev_disciplines SET poz_counter = -1")
        finally:
            await conn.close()

        _run_alembic("downgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _columns(conn) == {}
            assert await _constraints(conn) == set()
            assert await conn.fetchval("SELECT count(*) FROM ev_catalog_items") == 28
        finally:
            await conn.close()

        # tekrar upgrade AYNI numaralari verir (deterministik)
        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _numbers(conn) == EXPECTED
            assert await _counters(conn) == EXPECTED_COUNTERS
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_TKLB2_migration_on_empty_catalog_gives_zero_counters() -> None:
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            await conn.execute(
                "INSERT INTO ev_disciplines (id, code, name, color, default_contractor_type) "
                "VALUES ($1, 'KAB', 'Kaba', '#2563EB', 'own')",
                uuid.uuid4(),
            )
        finally:
            await conn.close()
        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _counters(conn) == {"KAB": 0}
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_TKLB2_fail_closed_check_stops_when_an_item_is_left_unnumbered() -> None:
    """FAIL-CLOSED dali: numarasiz satir kalirsa `_assert_every_item_numbered` ACIK
    RuntimeError ile durur (migration'in NOT NULL adiminden ONCE cagrilir)."""
    mig = _load()
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            disc_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO ev_disciplines (id, code, name, color, default_contractor_type) "
                "VALUES ($1, 'KAB', 'Kaba', '#2563EB', 'own')",
                disc_id,
            )
            await conn.execute("ALTER TABLE ev_catalog_items ALTER COLUMN poz_no DROP NOT NULL")
            await conn.execute(
                "INSERT INTO ev_catalog_items (id, discipline_id, name, uom, name_key, uom_key, "
                "standard_unit_mhr, default_contractor_type) "
                "VALUES ($1, $2, 'x', 'm', 'x', 'm', 1, 'own')",
                uuid.uuid4(),
                disc_id,
            )
        finally:
            await conn.close()

        dsn = settings.test_database_url.rsplit("/", 1)[0] + f"/{database}"
        engine = create_async_engine(dsn)
        try:
            async with engine.connect() as aconn:
                with pytest.raises(RuntimeError, match="TKL-B2 upgrade DURDU"):
                    await aconn.run_sync(mig._assert_every_item_numbered)  # noqa: SLF001
        finally:
            await engine.dispose()
    finally:
        await _drop_scratch_database(database)

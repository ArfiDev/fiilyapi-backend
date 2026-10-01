"""BLF-B1.2 — `7e3b1c9a4f20`: `sections.section_type` PG enum → `section_types` FK.

Neden maliyetli tur donusu testleri: migration CANLI bolum verisini tasir. Eslenmemis tek
bir enum degeri sessizce NULL'a duserse bolum tipini KAYBEDER; downgrade'de tohum-disi
tipe bagli bir bolum sessizce NULL'a cevrilirse yine kayip. Iki bekci (upgrade adim 4,
downgrade adim 1) burada POZITIF KONTROLLE kanitlanir: bekciyi tetikleyen veri gercekten
kurulur, alt surecin dondugu kod ve mesaj ile veritabaninin DEGISMEDIGI ayri ayri olculur.

Test her DB senaryosu icin kendi TEK KULLANIMLIK veritabanini acar ve sonunda dusurur;
`.env` ve `TEST_DATABASE_URL` veritabani ELLENMEZ. Alembic alt surecte kosar (env.py kendi
`asyncio.run()` dongusunu kurar). Revizyonlara ACIKCA cikilir; `head` / `-1` KULLANILMAZ —
sonraki dilimler revizyon ekledikce bu test sessizce yanlis seyi olcerdi.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import asyncpg
import pytest

from app.core.config import settings
from app.modules.earned_value.labels import normalize_label

BACKEND_DIR = Path(__file__).parents[3]
ALEMBIC_CMD = (sys.executable, "-m", "alembic")

PREVIOUS = "d5c0b0a1e7f3"
BLF_B1 = "7e3b1c9a4f20"
MIGRATION_PATH = next((BACKEND_DIR / "alembic" / "versions").glob(f"{BLF_B1}_*.py"))

#: `d4e5f6a7b8c9.SECTION_TYPE_LABELS` — eski enumun degerleri VE sirasi (downgrade bunu
#: birebir geri kurmali).
OLD_ENUM_LABELS = (
    "foundation_infra",
    "structural",
    "finishing",
    "facade_roof",
    "mep",
    "landscape",
    "handover",
)

#: Gorev emrindeki eski enum → ad eslemesi (spec §3 tablosu, mockup etiketleri).
EXPECTED_NAMES = {
    "foundation_infra": "Temel & Altyapı",
    "structural": "Kaba İnşaat",
    "finishing": "İnce İşler",
    "facade_roof": "Cephe & Çatı",
    "mep": "Mekanik / Elektrik",
    "landscape": "Peyzaj",
    "handover": "Teslimat & Kabul",
}

FK_NAME = "fk_sections_section_type_id"
INDEX_NAME = "ix_sections_section_type_id"
UQ_NAME = "uq_section_types_name_key"
CK_NAME = "ck_section_types_name_nonblank"
CK_KEY_NAME = "ck_section_types_name_key_nonblank"


def _load() -> ModuleType:
    name = f"_migration_{MIGRATION_PATH.stem}"
    spec = importlib.util.spec_from_file_location(name, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _asyncpg_dsn(database: str) -> str:
    base = settings.test_database_url.replace("postgresql+asyncpg://", "postgresql://")
    return base.rsplit("/", 1)[0] + f"/{database}"


def _alembic(*args: str, database: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": _asyncpg_dsn(database)}
    return subprocess.run(
        [*ALEMBIC_CMD, *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _run_alembic(*args: str, database: str) -> subprocess.CompletedProcess[str]:
    result = _alembic(*args, database=database)
    if result.returncode != 0:
        pytest.fail(f"alembic {' '.join(args)} basarisiz:\n{result.stdout}\n{result.stderr}")
    return result


async def _create_scratch_database() -> str:
    database = f"sections_blf_b1_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(_asyncpg_dsn("postgres"))
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()
    return database


async def _drop_scratch_database(database: str) -> None:
    admin = await asyncpg.connect(_asyncpg_dsn("postgres"))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    finally:
        await admin.close()


async def _connect(database: str) -> asyncpg.Connection:
    return await asyncpg.connect(_asyncpg_dsn(database))


async def _column_exists(conn: asyncpg.Connection, table: str, column: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = $1 AND column_name = $2)",
        table,
        column,
    )


async def _table_exists(conn: asyncpg.Connection, table: str) -> bool:
    return await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}")


async def _type_exists(conn: asyncpg.Connection, name: str) -> bool:
    return await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_type WHERE typname = $1)", name)


async def _enum_labels(conn: asyncpg.Connection, type_name: str) -> list[str]:
    rows = await conn.fetch(
        "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
        "WHERE t.typname = $1 ORDER BY e.enumsortorder",
        type_name,
    )
    return [row["enumlabel"] for row in rows]


async def _current_revision(conn: asyncpg.Connection) -> str | None:
    return await conn.fetchval("SELECT version_num FROM alembic_version")


async def _make_site(conn: asyncpg.Connection) -> uuid.UUID:
    project_id, site_id = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO projects (id, code, name, status, budget, progress_pct) "
        "VALUES ($1, $2, 'BLF-B1 Proje', 'active', 0, 0)",
        project_id,
        f"P-{uuid.uuid4().hex[:8]}",
    )
    await conn.execute(
        "INSERT INTO sites (id, project_id, code, name, status) "
        "VALUES ($1, $2, $3, 'BLF-B1 Santiye', 'active')",
        site_id,
        project_id,
        f"SNT-{uuid.uuid4().hex[:8]}",
    )
    return site_id


async def _seed_old_sections(conn: asyncpg.Connection) -> dict[uuid.UUID, tuple]:
    """PREVIOUS revizyonunda: 7 enum degerinin HER BIRI + NULL tipli yayinda + NULL tipli
    taslak + tipli taslak. Donus: id → (ad, eski enum degeri | None, budget_amount, is_draft).
    """
    site_id = await _make_site(conn)
    rows: dict[uuid.UUID, tuple] = {}
    specs: list[tuple[str, str | None, Decimal | None, bool]] = [
        (f"Bolum {label}", label, Decimal(f"{1000 + i}.{i:02d}"), False)
        for i, label in enumerate(OLD_ENUM_LABELS)
    ]
    specs += [
        ("Tipsiz yayinda", None, Decimal("0.00"), False),
        ("Tipsiz taslak", None, None, True),
        ("Tipli taslak", "mep", None, True),
    ]
    for name, section_type, budget, is_draft in specs:
        section_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO sections (id, site_id, name, status, section_type, budget_amount, "
            "is_draft) VALUES ($1, $2, $3, 'planned', $4::section_type, $5, $6)",
            section_id,
            site_id,
            name,
            section_type,
            budget,
            is_draft,
        )
        rows[section_id] = (name, section_type, budget, is_draft)
    return rows


# --------------------------------------------------------------------------- #
# DB'siz: sabitler
# --------------------------------------------------------------------------- #


def test_seed_name_keys_equal_normalize_label() -> None:
    """Migration `app`i import etmez; sabit yazilan anahtar BUGUNKU kuralla ayni olmali.
    Kural degisirse bu test kirmizi olur ve yeni bir yeniden-hesaplama migration'i gerekir.
    """
    mig = _load()
    diffs = [
        (name, key, normalize_label(name))
        for _id, _enum, name, key, _order in mig.SEEDS
        if normalize_label(name) != key
    ]
    assert diffs == []


def test_seed_contract_matches_old_enum_and_spec_names() -> None:
    mig = _load()
    assert [seed[1] for seed in mig.SEEDS] == list(OLD_ENUM_LABELS)
    assert {seed[1]: seed[2] for seed in mig.SEEDS} == EXPECTED_NAMES
    assert [seed[4] for seed in mig.SEEDS] == list(range(1, 8))
    ids = [uuid.UUID(seed[0]) for seed in mig.SEEDS]
    assert len(set(ids)) == 7
    assert len({seed[3] for seed in mig.SEEDS}) == 7, "tohum name_key'leri tekil olmali"


def test_test_seed_equals_migration_seed() -> None:
    """`tests/_section_types.SEED_SECTION_TYPES` (create_all tohumu) ile migration `SEEDS`
    (canli tohum) ayri yazilir; ikisi (enum anahtari, ad, sort_order) olarak BIREBIR esit
    olmali — aksi halde testler canlida olmayan bir tohumla yesil kalir."""
    from tests._section_types import SEED_SECTION_TYPES

    mig = _load()
    migration_seed = [(seed[1], seed[2], seed[4]) for seed in mig.SEEDS]
    test_seed = [(key, name, order) for key, (name, order) in SEED_SECTION_TYPES.items()]
    assert test_seed == migration_seed


def test_migration_does_not_import_app() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "from app" not in source and "import app" not in source


def test_alembic_has_single_head() -> None:
    result = subprocess.run(
        [*ALEMBIC_CMD, "heads"], cwd=BACKEND_DIR, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, f"tek head bekleniyordu, cikti:\n{result.stdout}"


# --------------------------------------------------------------------------- #
# Gecici DB: veri tasima + tur donusu
# --------------------------------------------------------------------------- #


async def _assert_upgraded_mapping(
    conn: asyncpg.Connection, seeded: dict[uuid.UUID, tuple], seed_by_enum: dict[str, uuid.UUID]
) -> None:
    assert await conn.fetchval("SELECT count(*) FROM sections") == len(seeded)
    rows = await conn.fetch(
        "SELECT id, name, section_type_id, budget_amount, is_draft FROM sections"
    )
    for row in rows:
        name, old_type, budget, is_draft = seeded[row["id"]]
        expected = seed_by_enum[old_type] if old_type is not None else None
        assert row["section_type_id"] == expected, f"{name}: {old_type} yanlis tohuma baglandi"
        assert row["budget_amount"] == budget, f"{name}: budget_amount degisti"
        assert row["is_draft"] is is_draft
        assert row["name"] == name


async def test_upgrade_maps_every_enum_value_and_round_trips() -> None:
    mig = _load()
    seed_by_enum = {enum_value: uuid.UUID(seed_id) for seed_id, enum_value, *_ in mig.SEEDS}
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn = await _connect(database)
        try:
            seeded = await _seed_old_sections(conn)
        finally:
            await conn.close()

        _run_alembic("upgrade", BLF_B1, database=database)
        conn = await _connect(database)
        try:
            assert await _current_revision(conn) == BLF_B1
            await _assert_upgraded_mapping(conn, seeded, seed_by_enum)
            assert not await _column_exists(conn, "sections", "section_type")
            assert not await _type_exists(conn, "section_type"), "enum tipi upgrade'de kalmis"
            assert await _column_exists(conn, "sections", "budget_amount")
        finally:
            await conn.close()

        _run_alembic("downgrade", PREVIOUS, database=database)
        conn = await _connect(database)
        try:
            assert await _current_revision(conn) == PREVIOUS
            assert await _enum_labels(conn, "section_type") == list(OLD_ENUM_LABELS)
            assert not await _table_exists(conn, "section_types")
            assert not await _column_exists(conn, "sections", "section_type_id")
            rows = await conn.fetch(
                "SELECT id, section_type::text AS t, budget_amount, is_draft FROM sections"
            )
            assert len(rows) == len(seeded)
            round_trip = {r["id"]: (r["t"], r["budget_amount"], r["is_draft"]) for r in rows}
            assert round_trip == {k: (v[1], v[2], v[3]) for k, v in seeded.items()}
        finally:
            await conn.close()

        _run_alembic("upgrade", BLF_B1, database=database)
        conn = await _connect(database)
        try:
            assert await _current_revision(conn) == BLF_B1
            await _assert_upgraded_mapping(conn, seeded, seed_by_enum)
            assert await conn.fetchval("SELECT count(*) FROM section_types") == 7
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_upgrade_fails_closed_on_unmapped_enum_value() -> None:
    """Adim 4 bekcisinin pozitif kontrolu: eski enuma eslemesi OLMAYAN bir deger eklenir
    (canlida bilinmeyen bir deger senaryosu). Upgrade ACIK MESAJLA durmali ve veritabani
    PREVIOUS halinde, veri ile birlikte DEGISMEDEN kalmali."""
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn = await _connect(database)
        try:
            seeded = await _seed_old_sections(conn)
            await conn.execute("ALTER TYPE section_type ADD VALUE 'zz_unmapped'")
            site_id = await _make_site(conn)
            stray_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO sections (id, site_id, name, status, section_type) "
                "VALUES ($1, $2, 'Eslenmeyen', 'planned', 'zz_unmapped')",
                stray_id,
                site_id,
            )
        finally:
            await conn.close()

        result = _alembic("upgrade", BLF_B1, database=database)
        assert result.returncode != 0, "eslenmemis enum degeriyle upgrade GECMEMELIYDI"
        assert "eslenmemis bolum tipi" in result.stderr
        assert "zz_unmapped=1" in result.stderr

        conn = await _connect(database)
        try:
            assert await _current_revision(conn) == PREVIOUS
            assert not await _table_exists(conn, "section_types")
            assert not await _column_exists(conn, "sections", "section_type_id")
            assert (
                await conn.fetchval(
                    "SELECT section_type::text FROM sections WHERE id = $1", stray_id
                )
                == "zz_unmapped"
            )
            assert await conn.fetchval("SELECT count(*) FROM sections") == len(seeded) + 1
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_downgrade_refuses_section_on_non_seed_type() -> None:
    """Downgrade adim 1 bekcisinin pozitif kontrolu: tohum-disi tipe bagli bolum varken
    downgrade HATA vermeli ve veri BOZULMAMALI; bag kaldirilinca downgrade gecmeli ve
    hicbir bolume bagli olmayan tohum-disi tip tabloyla birlikte gitmeli."""
    mig = _load()
    structural_id = next(uuid.UUID(s[0]) for s in mig.SEEDS if s[1] == "structural")
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", BLF_B1, database=database)
        conn = await _connect(database)
        try:
            site_id = await _make_site(conn)
            custom_id, unused_id = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO section_types (id, name, name_key, sort_order) VALUES "
                "($1, 'Altyapı Ek', 'altyapı ek', 8), ($2, 'Kullanılmayan', 'kullanılmayan', 9)",
                custom_id,
                unused_id,
            )
            custom_section, seed_section = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO sections (id, site_id, name, status, section_type_id) VALUES "
                "($1, $3, 'Ozel tipli', 'planned', $4), ($2, $3, 'Kaba', 'planned', $5)",
                custom_section,
                seed_section,
                site_id,
                custom_id,
                structural_id,
            )
        finally:
            await conn.close()

        result = _alembic("downgrade", PREVIOUS, database=database)
        assert result.returncode != 0, "tohum-disi tipe bagli bolumle downgrade GECMEMELIYDI"
        assert "tohum disi" in result.stderr
        assert "'Altyapı Ek'=1" in result.stderr

        conn = await _connect(database)
        try:
            assert await _current_revision(conn) == BLF_B1
            assert await conn.fetchval("SELECT count(*) FROM section_types") == 9
            assert not await _type_exists(conn, "section_type")
            assert not await _column_exists(conn, "sections", "section_type")
            bound = await conn.fetchval(
                "SELECT section_type_id FROM sections WHERE id = $1", custom_section
            )
            assert bound == custom_id, "bekci tetiklendi ama bolumun bagi bozuldu"

            await conn.execute(
                "UPDATE sections SET section_type_id = $1 WHERE id = $2",
                structural_id,
                custom_section,
            )
        finally:
            await conn.close()

        _run_alembic("downgrade", PREVIOUS, database=database)
        conn = await _connect(database)
        try:
            assert await _current_revision(conn) == PREVIOUS
            assert not await _table_exists(conn, "section_types")
            types = await conn.fetch("SELECT section_type::text AS t FROM sections")
            assert [r["t"] for r in types] == ["structural", "structural"]
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


# --------------------------------------------------------------------------- #
# Gecici DB: sema sozlesmesi (MODEL SOZLESMESI ile birebir)
# --------------------------------------------------------------------------- #


async def _columns(conn: asyncpg.Connection, table: str) -> dict[str, tuple]:
    rows = await conn.fetch(
        "SELECT column_name, data_type, character_maximum_length, is_nullable, "
        "column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = $1",
        table,
    )
    return {
        r["column_name"]: (
            r["data_type"],
            r["character_maximum_length"],
            r["is_nullable"],
            r["column_default"],
        )
        for r in rows
    }


async def test_schema_matches_model_contract() -> None:
    mig = _load()
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", BLF_B1, database=database)
        conn = await _connect(database)
        try:
            assert await _columns(conn, "section_types") == {
                "id": ("uuid", None, "NO", None),
                "name": ("character varying", 100, "NO", None),
                "name_key": ("character varying", 400, "NO", None),
                "sort_order": ("integer", None, "NO", "0"),
                "created_at": ("timestamp with time zone", None, "NO", "now()"),
            }
            assert (await _columns(conn, "sections"))["section_type_id"] == (
                "uuid",
                None,
                "YES",
                None,
            )

            constraints = {
                r["conname"]: (r["contype"], r["def"])
                for r in await conn.fetch(
                    "SELECT conname, contype::text, pg_get_constraintdef(oid) AS def "
                    "FROM pg_constraint WHERE conrelid = 'section_types'::regclass"
                )
            }
            assert constraints == {
                "section_types_pkey": ("p", "PRIMARY KEY (id)"),
                UQ_NAME: ("u", "UNIQUE (name_key)"),
                CK_NAME: ("c", "CHECK ((length(btrim((name)::text)) > 0))"),
                CK_KEY_NAME: ("c", "CHECK (((name_key)::text <> ''::text))"),
            }

            fk = await conn.fetchrow(
                "SELECT confrelid::regclass::text AS target, confdeltype::text AS on_delete, "
                "pg_get_constraintdef(oid) AS def FROM pg_constraint "
                "WHERE conname = $1 AND conrelid = 'sections'::regclass AND contype = 'f'",
                FK_NAME,
            )
            assert fk is not None, f"{FK_NAME} yok"
            assert fk["target"] == "section_types"
            assert fk["on_delete"] == "r", "ON DELETE RESTRICT bekleniyordu"
            assert fk["def"] == (
                "FOREIGN KEY (section_type_id) REFERENCES section_types(id) ON DELETE RESTRICT"
            )

            index_def = await conn.fetchval(
                "SELECT indexdef FROM pg_indexes WHERE indexname = $1", INDEX_NAME
            )
            assert index_def == (
                f"CREATE INDEX {INDEX_NAME} ON public.sections USING btree (section_type_id)"
            )

            assert not await _type_exists(conn, "section_type")
            assert not await _column_exists(conn, "sections", "section_type")
            assert await _column_exists(conn, "sections", "budget_amount")

            seeds = await conn.fetch(
                "SELECT id, name, name_key, sort_order FROM section_types ORDER BY sort_order"
            )
            assert [(r["id"], r["name"], r["name_key"], r["sort_order"]) for r in seeds] == [
                (uuid.UUID(seed_id), name, key, order)
                for seed_id, _enum, name, key, order in mig.SEEDS
            ]

            # UQ: ayni name_key ikinci kez yazilamaz (yazim farki kopyasi).
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO section_types (id, name, name_key) "
                    "VALUES ($1, 'ince işler', 'ince işler')",
                    uuid.uuid4(),
                )
            # CHECK: bos/yalniz bosluk ad.
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO section_types (id, name, name_key) VALUES ($1, '   ', 'x')",
                    uuid.uuid4(),
                )
            # CHECK: gorunmez karakterden olusan ad (btrim gecer) -> bos name_key reddedilir.
            with pytest.raises(asyncpg.CheckViolationError, match=CK_KEY_NAME):
                await conn.execute(
                    "INSERT INTO section_types (id, name, name_key) VALUES ($1, $2, '')",
                    uuid.uuid4(),
                    "\u200b",
                )
            # sort_order sunucu varsayilani 0.
            fresh = uuid.uuid4()
            await conn.execute(
                "INSERT INTO section_types (id, name, name_key) VALUES ($1, 'Yeni', 'yeni')",
                fresh,
            )
            assert (
                await conn.fetchval("SELECT sort_order FROM section_types WHERE id = $1", fresh)
                == 0
            )

            # RESTRICT: bolume bagli tip silinemez.
            site_id = await _make_site(conn)
            await conn.execute(
                "INSERT INTO sections (id, site_id, name, status, section_type_id) "
                "VALUES ($1, $2, 'Bagli', 'planned', $3)",
                uuid.uuid4(),
                site_id,
                fresh,
            )
            with pytest.raises((asyncpg.RestrictViolationError, asyncpg.ForeignKeyViolationError)):
                await conn.execute("DELETE FROM section_types WHERE id = $1", fresh)
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

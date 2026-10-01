"""P6 T1 — `sections` yeni kolonlari + `section_status`'a `on_hold`.

Neden ayri ve maliyetli bir tur donusu testi: `ALTER TYPE ... ADD VALUE` GERI
ALINAMAZ, dolayisiyla `downgrade` tipi yeniden yaratmak ZORUNDADIR; takas
tipini (`section_status_old`) veya `section_type` tipini `DROP TYPE` etmeyi
unutan bir downgrade IKINCI upgrade'i "type already exists" ile patlatir ve bu
yalniz canlida gorulurdu. Ayni sekilde yanlislikla `NOT NULL` konan tek bir kolon
TASLAK kaydini imkansiz kilar.

Test kendi TEK KULLANIMLIK veritabanini acar ve sonunda dusurur; `.env` ve
`TEST_DATABASE_URL` veritabani ELLENMEZ. Alembic alt surecte kosturulur cunku
`alembic/env.py` kendi `asyncio.run()` dongusunu kurar ve calisan bir
pytest-asyncio dongusunun icinden cagrilamaz.
"""

import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.modules.sites.models import Section, SectionStatus, Site
from tests._section_types import SEED_SECTION_TYPES, SEED_TYPE_IDS, seed_section_types

BACKEND_DIR = Path(__file__).parents[3]
# `python -m alembic`: yerelde `.venv/bin/python`, CI'da sistem Python'u. Sabit
# `.venv/bin/alembic` yolu CI'da YOKTUR ve testi yalniz orada kirardi.
ALEMBIC_CMD = (sys.executable, "-m", "alembic")

# Revizyonlara ACIKCA cikilir; `head` / `-1` KULLANILMAZ — sonraki dilimler
# revizyon ekledikce bu test sessizce yanlis seyi olcerdi.
# RE-PARENT (2026-08-02): ebeveyn artik `c3d4e5f6a7b8` (P3.1) DEGIL, `f2a3b4c5d6e7`
# (P8 unite satisi). P8 ayni ebeveyne baglanip ONCE canliya cikinca zincir
# catallanmisti; iki head `Dockerfile:22`deki `alembic upgrade head && uvicorn`i
# kirar ve canli uygulama HIC acilmaz.
PARENT_REVISION = "a3b4c5d6e7f8"  # TH taseron hakedisi — zincirin yeni ucu (IKINCI re-parent)
P6_REVISION = "d4e5f6a7b8c9"

NEW_SECTION_COLUMNS = (
    "section_type",
    "description",
    "deputy_manager_user_id",
    "deputy_manager_name",
    "planned_worker_count",
    "budget_amount",
    "is_draft",
)

CHECK_CONSTRAINTS = ("ck_sections_planned_worker_count", "ck_sections_budget_amount")
DEPUTY_FK = "fk_sections_deputy_manager_user_id"
DEPUTY_INDEX = "ix_sections_deputy_manager_user_id"

SECTION_STATUS_AFTER = ("planned", "active", "on_hold", "completed")
SECTION_STATUS_BEFORE = ("planned", "active", "completed")
SECTION_TYPE_LABELS = (
    "foundation_infra",
    "structural",
    "finishing",
    "facade_roof",
    "mep",
    "landscape",
    "handover",
)


def _asyncpg_dsn(database: str) -> str:
    base = settings.test_database_url.replace("postgresql+asyncpg://", "postgresql://")
    return base.rsplit("/", 1)[0] + f"/{database}"


def _run_alembic(*args: str, database: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": _asyncpg_dsn(database)}
    result = subprocess.run(
        [*ALEMBIC_CMD, *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic {' '.join(args)} basarisiz:\n{result.stdout}\n{result.stderr}")
    return result


async def _column_exists(conn: asyncpg.Connection, table: str, column: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = $1 AND column_name = $2)",
        table,
        column,
    )


async def _constraint_exists(conn: asyncpg.Connection, name: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = $1)", name
    )


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


async def _create_scratch_database() -> str:
    database = f"sections_p6_{uuid.uuid4().hex[:8]}"
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


# --------------------------------------------------------------------------- #
# Model katmani
# --------------------------------------------------------------------------- #


def test_section_status_has_on_hold_in_form_order():
    """Form 71 sirasi: Planlandi · Aktif · Beklemede. `completed` KALIR."""
    assert [s.value for s in SectionStatus] == list(SECTION_STATUS_AFTER)


def test_section_type_labels_match_spec():
    """BLF-B1: `SectionType` artik enum DEGIL (tablo); bu test eski PG enum etiketlerini
    SABIT demetle tutar ve test tohumunun (`tests/_section_types.py`) ayni yedi anahtari
    ayni sirayla tasidigini cakar."""
    assert list(SEED_SECTION_TYPES) == list(SECTION_TYPE_LABELS)


def test_only_is_draft_is_not_nullable_among_new_columns():
    """Taslak destegi (spec §3): `is_draft` disinda yeni kolonlarin HEPSI nullable.
    Tek bir `NOT NULL` yarim doldurulmus formun kaydini imkansiz kilardi."""
    columns = Section.__table__.columns
    # BLF-B1: `section_type` kolonu `section_type_id` oldu (model); eski sema sabitleri
    # (`NEW_SECTION_COLUMNS`) migration testleri icin DEGISMEDEN kalir.
    for name in ("section_type_id" if n == "section_type" else n for n in NEW_SECTION_COLUMNS):
        if name == "is_draft":
            assert not columns[name].nullable
            assert "false" in str(columns[name].server_default.arg)
        else:
            assert columns[name].nullable, f"sections.{name} nullable degil"


async def _make_site(db_session, project_factory, code: str) -> Site:
    project = await project_factory(code)
    site = Site(project_id=project.id, code=code, name=f"{code} Santiye")
    db_session.add(site)
    await db_session.flush()
    return site


async def test_section_persists_new_columns(db_session, project_factory):
    site = await _make_site(db_session, project_factory, "P-P6")
    await seed_section_types(db_session)
    section = Section(
        site_id=site.id,
        name="Kaba Insaat",
        status=SectionStatus.on_hold,
        section_type_id=SEED_TYPE_IDS["structural"],
        description="Kalip, demir, beton isleri",
        deputy_manager_name="Ali Veli",
        planned_worker_count=42,
        budget_amount=Decimal("1234.56"),
        is_draft=True,
    )
    db_session.add(section)
    await db_session.flush()
    db_session.expunge_all()

    loaded = (
        await db_session.execute(select(Section).where(Section.id == section.id))
    ).scalar_one()
    assert loaded.status is SectionStatus.on_hold
    assert loaded.section_type_id == SEED_TYPE_IDS["structural"]
    assert loaded.description == "Kalip, demir, beton isleri"
    assert loaded.deputy_manager_name == "Ali Veli"
    assert loaded.planned_worker_count == 42
    assert loaded.budget_amount == Decimal("1234.56")
    assert loaded.is_draft is True


async def test_section_draft_can_be_saved_empty(db_session, project_factory):
    """Yarim doldurulmus taslak: yeni kolonlarin hicbiri verilmez."""
    site = await _make_site(db_session, project_factory, "P-P6D")
    section = Section(site_id=site.id, name="Taslak Bolum", is_draft=True)
    db_session.add(section)
    await db_session.flush()
    db_session.expunge_all()

    loaded = (
        await db_session.execute(select(Section).where(Section.id == section.id))
    ).scalar_one()
    assert loaded.section_type is None
    assert loaded.budget_amount is None
    assert loaded.planned_worker_count is None
    assert loaded.status is SectionStatus.planned


# --------------------------------------------------------------------------- #
# Migration tur donusu
# --------------------------------------------------------------------------- #


def test_alembic_has_single_head():
    """Iki head = canlida deploy kilitlenmesi."""
    result = subprocess.run(
        [*ALEMBIC_CMD, "heads"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, f"tek head bekleniyordu, cikti:\n{result.stdout}"


async def test_upgrade_downgrade_upgrade_round_trip():
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", P6_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for column in NEW_SECTION_COLUMNS:
                assert await _column_exists(conn, "sections", column), (
                    f"sections.{column} upgrade sonrasi yok"
                )
            for name in (*CHECK_CONSTRAINTS, DEPUTY_FK):
                assert await _constraint_exists(conn, name), f"{name} upgrade sonrasi yok"
            assert await _enum_labels(conn, "section_status") == list(SECTION_STATUS_AFTER)
            assert await _enum_labels(conn, "section_type") == list(SECTION_TYPE_LABELS)
            assert await _current_revision(conn) == P6_REVISION
        finally:
            await conn.close()

        _run_alembic("downgrade", PARENT_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for column in NEW_SECTION_COLUMNS:
                assert not await _column_exists(conn, "sections", column), (
                    f"sections.{column} downgrade sonrasi duruyor"
                )
            for name in (*CHECK_CONSTRAINTS, DEPUTY_FK):
                assert not await _constraint_exists(conn, name), f"{name} downgrade sonrasi duruyor"
            assert await _enum_labels(conn, "section_status") == list(SECTION_STATUS_BEFORE)
            # Tip artiklari IKINCI upgrade'i patlatir — hicbiri kalmamali.
            assert not await _type_exists(conn, "section_type")
            assert not await _type_exists(conn, "section_status_old")
            assert await _current_revision(conn) == PARENT_REVISION
        finally:
            await conn.close()

        _run_alembic("upgrade", P6_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for column in NEW_SECTION_COLUMNS:
                assert await _column_exists(conn, "sections", column), (
                    f"sections.{column} ikinci upgrade sonrasi yok"
                )
            assert await _enum_labels(conn, "section_status") == list(SECTION_STATUS_AFTER)
            assert await _current_revision(conn) == P6_REVISION
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_existing_rows_survive_upgrade_and_on_hold_downgrade():
    """Upgrade ONCESI yazilmis satir korunur; downgrade'de `on_hold` satiri
    KAYBOLMAZ, `planned`'a tasinir (`f1b2c3d4e5a6` dersi)."""
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PARENT_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            project_id, site_id, section_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO projects (id, code, name, status, budget, progress_pct) "
                "VALUES ($1, 'P-P6RT', 'P6 Tur Donusu', 'active', 0, 0)",
                project_id,
            )
            await conn.execute(
                "INSERT INTO sites (id, project_id, code, name) "
                "VALUES ($1, $2, 'SNT-P6RT', 'P6 Santiye')",
                site_id,
                project_id,
            )
            await conn.execute(
                "INSERT INTO sections (id, site_id, name, status) "
                "VALUES ($1, $2, 'Eski Bolum', 'planned')",
                section_id,
                site_id,
            )
        finally:
            await conn.close()

        _run_alembic("upgrade", P6_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            row = await conn.fetchrow("SELECT * FROM sections WHERE id = $1", section_id)
            assert row is not None, "mevcut bolum satiri upgrade'de kayboldu"
            assert row["name"] == "Eski Bolum"
            assert row["is_draft"] is False, "mevcut satirlar yayinda sayilir"
            assert row["section_type"] is None
            assert row["budget_amount"] is None
            # Yeni enum degeri fiilen yazilabiliyor mu?
            await conn.execute("UPDATE sections SET status = 'on_hold' WHERE id = $1", section_id)
        finally:
            await conn.close()

        _run_alembic("downgrade", PARENT_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            status = await conn.fetchval(
                "SELECT status::text FROM sections WHERE id = $1", section_id
            )
            assert status == "planned", "on_hold satiri downgrade'de kayboldu veya bozuldu"
            assert await conn.fetchval("SELECT count(*) FROM sections") == 1
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_negative_values_rejected_by_check_constraints():
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", P6_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            project_id, site_id = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO projects (id, code, name, status, budget, progress_pct) "
                "VALUES ($1, 'P-P6CK', 'P6 Check', 'active', 0, 0)",
                project_id,
            )
            await conn.execute(
                "INSERT INTO sites (id, project_id, code, name) "
                "VALUES ($1, $2, 'SNT-P6CK', 'P6 Santiye')",
                site_id,
                project_id,
            )
            negatives: tuple[tuple[str, object], ...] = (
                ("planned_worker_count", -1),
                ("budget_amount", Decimal("-1")),
            )
            for column, value in negatives:
                with pytest.raises(asyncpg.exceptions.CheckViolationError):
                    await conn.execute(
                        f"INSERT INTO sections (id, site_id, name, {column}) "
                        "VALUES ($1, $2, 'Negatif', $3)",
                        uuid.uuid4(),
                        site_id,
                        value,
                    )
            # Indeks de olusmus olmali (deputy FK sorgulari icin).
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = $1)", DEPUTY_INDEX
            )
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

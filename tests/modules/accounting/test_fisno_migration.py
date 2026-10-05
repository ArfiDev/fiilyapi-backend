"""FIS-NO T2 — `entry_no` + `journal_entry_counters` migration turu ve BACKFILL.

## 🔴 NEDEN AYRI BIR BACKFILL TESTI — bu olmadan backfill KANITSIZ SIPLENIR

Test veritabani her kosuda SIFIRDAN kurulur (`tests/conftest.py`
`drop_all`/`create_all`), yani `journal_entries` **BOSTUR** ve migration'in
backfill'i **HIC KOSMAZ**. Butun takim yesilken backfill tamamen yanlis
olabilirdi ve bu YALNIZ CANLIDA, veri olan bir veritabaninda gorulurdu.

Bu dosya bu yuzden AYRI bir veritabani acar, migration'i **bir onceki
revizyona** kadar kosturur, oraya **elle fis tohumlar** ve ancak ondan sonra bu
dilimin migration'ini kosturur.

Tohum BILEREK ayni `entry_date`ten BIRDEN FAZLA tasir: backfill'in ikinci
siralama anahtari (`id`) ancak boyle sinanir. Tek anahtarli bir `row_number()`
penceresinde esit tarihlerin duzeni TANIMSIZDIR ve ayni migration iki
veritabaninda FARKLI numaralar uretebilirdi.

## Ucuncu iddia: SONRAKI GERCEK FIS CAKISMAZ

Sayac tablosu tohumlanmazsa ilk yeni fis `next_no = 1` ile dogar,
`YEV-2026-0001` ise backfill'de ZATEN dagitilmistir ve uc
`uq_journal_entries_entry_no` ihlaliyle patlar. Bu yuzden test backfill'den
sonra GERCEK ureticiyi cagirir ve fisi GERCEKTEN yazar.

Revizyonlara ACIKCA cikilir; `head` / `-1` KULLANILMAZ — sonraki dilimler
revizyon ekledikce bu test sessizce YANLIS seyi olcerdi.

Alembic alt surecte kosturulur (`alembic/env.py` kendi `asyncio.run()`
dongusunu kurar — MU-1/MU-2/MT-1 deseni). Test kendi TEK KULLANIMLIK
veritabanini acar ve sonunda BASARISIZLIKTA BILE dusurur; `.env` ve
`TEST_DATABASE_URL` veritabani ELLENMEZ.

⚠️ PG SURUM TUZAGI: yerel 18, CI 16 — surume ozgu SQLSTATE iddia edilmez.
"""

import os
import subprocess
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.modules.accounting import numbering
from app.modules.accounting.models import JournalEntry, JournalEntryStatus
from app.modules.users.models import User

BACKEND_DIR = Path(__file__).parents[3]
ALEMBIC_CMD = (sys.executable, "-m", "alembic")

# Revizyonlara ACIKCA cikilir (modul docstring'i).
FIN1_REVISION = "d8e9f0a1b2c3"
FISNO_REVISION = "f150c0117e42"

TABLE = "journal_entries"
COLUMN = "entry_no"
COUNTER_TABLE = "journal_entry_counters"
UNIQUE_CONSTRAINT = "uq_journal_entries_entry_no"


def _asyncpg_dsn(database: str) -> str:
    base = settings.test_database_url.replace("postgresql+asyncpg://", "postgresql://")
    return base.rsplit("/", 1)[0] + f"/{database}"


def _sqlalchemy_dsn(database: str) -> str:
    return settings.test_database_url.rsplit("/", 1)[0] + f"/{database}"


def _run_alembic(*args: str, database: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": _asyncpg_dsn(database)}
    result = subprocess.run(
        [*ALEMBIC_CMD, *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic {' '.join(args)} basarisiz:\n{result.stdout}\n{result.stderr}")
    return result


async def _create_scratch_database() -> str:
    database = f"fisno_mig_{uuid.uuid4().hex[:8]}"
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


async def _column_exists(conn: asyncpg.Connection, table: str, column: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name = $1 AND column_name = $2)",
        table,
        column,
    )


async def _table_exists(conn: asyncpg.Connection, table: str) -> bool:
    return await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}")


async def _current_revision(conn: asyncpg.Connection) -> str | None:
    return await conn.fetchval("SELECT version_num FROM alembic_version")


# --------------------------------------------------------------------------- #
# Tohum — FIS-NO ONCESI (ebeveyn revizyonundaki) fisler
# --------------------------------------------------------------------------- #

#: 🔴 Ayni `entry_date`ten BIRDEN FAZLA: ikinci siralama anahtarinin (`id`)
#: sinandigi yer burasidir. Yil ICINDE beklenen sira `(entry_date, id)`dir.
TOHUM: dict[int, list[date]] = {
    2025: [date(2025, 3, 10), date(2025, 1, 5), date(2025, 3, 10)],
    2026: [date(2026, 7, 17), date(2026, 7, 17), date(2026, 2, 1), date(2026, 7, 17)],
}


async def _aktor_yarat(database: str) -> uuid.UUID:
    """`journal_entries.created_by_id` NOT NULL + RESTRICT — FK zemini sart.

    HAM SQL: bu revizyonda `users.all_projects` (izn_b3) HENÜZ YOKTUR ve bugünkü `User` modeli
    onu taşır; ORM insert'i eski şemada patlar.
    """
    conn = await asyncpg.connect(_asyncpg_dsn(database))
    try:
        role_id = await conn.fetchval(
            "INSERT INTO roles (id, key, name, emoji, description, is_system) "
            "VALUES (gen_random_uuid(), 'fisno_backfill', 'FIS-NO Backfill Rolu', '', '', false) "
            "RETURNING id"
        )
        return await conn.fetchval(
            "INSERT INTO users (id, email, password_hash, full_name, title, role_id, status, "
            "token_version) VALUES (gen_random_uuid(), 'fisno-backfill@muhasebe.co', 'x', "
            "'FIS-NO Backfill', '', $1, 'active', 0) RETURNING id",
            role_id,
        )
    finally:
        await conn.close()


async def _tohumla(database: str) -> dict[int, list[tuple[str, str]]]:
    """Fisleri HAM SQL ile yazar ve yil basina BEKLENEN numara dizisini doner.

    ORM kullanilamaz: ebeveyn revizyonunda `journal_entries.entry_no` kolonu
    HENUZ YOKTUR ve bugunku `JournalEntry` modeli onu tasir. Ham SQL ayni
    zamanda daha durustur — canlidaki mevcut veri de bu kolonsuz dogmustur.
    """
    actor_id = await _aktor_yarat(database)

    # yil -> [(id, beklenen numara)] — sira testin KENDI olcusune gore kurulur.
    beklenen: dict[int, list[tuple[str, str]]] = {}
    conn = await asyncpg.connect(_asyncpg_dsn(database))
    try:
        for yil, tarihler in TOHUM.items():
            # `id` uretilip SIRALANIR: beklenti testin icinde, uygulamanin
            # kuralindan BAGIMSIZ olarak kurulur (`entry_date`, sonra `id`).
            satirlar = [(uuid.uuid4(), tarih) for tarih in tarihler]
            for entry_id, tarih in satirlar:
                await conn.execute(
                    "INSERT INTO journal_entries "
                    "(id, entry_date, period_year, period_month, description, status, "
                    " total_debit, total_credit, created_by_id) "
                    "VALUES ($1, $2, $3, $4, $5, 'posted', 100, 100, $6)",
                    entry_id,
                    tarih,
                    yil,
                    tarih.month,
                    f"Tohum fis {tarih}",
                    actor_id,
                )
            # 🔴 Beklenen sira UYGULAMANIN kuralindan degil, testin KENDI
            # siralamasindan kurulur: `entry_date`, esitlikte `id`.
            sirali = sorted(satirlar, key=lambda satir: (satir[1], str(satir[0])))
            beklenen[yil] = [
                (str(entry_id), numbering.format_entry_no(yil, sira))
                for sira, (entry_id, _) in enumerate(sirali, start=1)
            ]
    finally:
        await conn.close()
    return beklenen


# --------------------------------------------------------------------------- #
# Migration hijyeni
# --------------------------------------------------------------------------- #


def test_alembic_has_single_head():
    """Iki head = canlida deploy kilitlenmesi (`alembic upgrade head` patlar).

    🔴 `DATABASE_URL` override'i BURADA DA verilir: `.env` UZAK Railway'i
    gosterir ve alembic'in bir alt komutunun ileride motor kurmasi, hicbir uyari
    vermeden canliya baglanmak demektir (MU-1'de bir kez yasandi). Kural tek
    cumledir: **hicbir alembic komutu override'siz kosmaz.**
    """
    result = subprocess.run(
        [*ALEMBIC_CMD, "heads"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": _asyncpg_dsn("postgres")},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, f"tek head bekleniyordu, cikti:\n{result.stdout}"


def test_migration_parent_is_the_expected_revision():
    """🔴 Ebeveyn `d8e9f0a1b2c3` (FIN-1). Arada baska bir dilim merge edilirse
    re-parent SART — bu sabit ve migration BIRLIKTE guncellenir (P8/TH dersi)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini")))
    revision = script.get_revision(FISNO_REVISION)
    assert revision.down_revision == FIN1_REVISION


async def test_upgrade_downgrade_upgrade_round_trip():
    """🔴 Downgrade KOLONU **ve** TABLOYU dusurur.

    Biri kalirsa ikinci `upgrade` "already exists" ile patlar ve bu YALNIZ
    CANLIDA gorulur: konteyner acilista `alembic upgrade head && uvicorn …`
    kosar, patlarsa `&&` kisa devre yapar ve uvicorn HIC BASLAMAZ.
    """
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", FISNO_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _column_exists(conn, TABLE, COLUMN)
            assert await _table_exists(conn, COUNTER_TABLE)
            assert await _current_revision(conn) == FISNO_REVISION
        finally:
            await conn.close()

        _run_alembic("downgrade", FIN1_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert not await _column_exists(conn, TABLE, COLUMN)
            assert not await _table_exists(conn, COUNTER_TABLE), (
                'sayac tablosu downgrade\'de kalmis — ikinci upgrade "already exists" ile patlardi'
            )
            # Komsu tablolar AYAKTA: bu dilim yalniz `journal_entries`e dokunur.
            for komsu in ("journal_lines", "chart_of_accounts", "accounting_periods", "users"):
                assert await _table_exists(conn, komsu), komsu
            assert await _current_revision(conn) == FIN1_REVISION
        finally:
            await conn.close()

        # 🔴 ASIL IDDIA: ikinci upgrade PATLAMADAN gecer.
        _run_alembic("upgrade", FISNO_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _column_exists(conn, TABLE, COLUMN)
            assert await _table_exists(conn, COUNTER_TABLE)
            assert await _current_revision(conn) == FISNO_REVISION
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_entry_no_NOT_NULL_ve_UNIQUE_DB_duzeyinde():
    """Kisitlar SEMADA gercekten var mi — ham SQL ile sinanir.

    Model katmanindaki `nullable=False` / `UniqueConstraint` migration'a
    YANSIMAMIS olabilir (ikisi ayri dosyalardir ve autogenerate KULLANILMIYOR).
    """
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", FISNO_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            nullable = await conn.fetchval(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = $2",
                TABLE,
                COLUMN,
            )
            assert nullable == "NO"
            uzunluk = await conn.fetchval(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = $2",
                TABLE,
                COLUMN,
            )
            assert uzunluk == 20
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = $1)",
                UNIQUE_CONSTRAINT,
            ), "numara SIRKET GENELINDE tekil olmaliydi"
            # Sayac 1'in altina INEMEZ (karar 2'nin tek fiili ihlal yolu).
            await conn.execute(
                "INSERT INTO journal_entry_counters (year, next_no) VALUES (2026, 5)"
            )
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(
                    "UPDATE journal_entry_counters SET next_no = 0 WHERE year = 2026"
                )
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


# --------------------------------------------------------------------------- #
# 🔴 BACKFILL — bu dosyanin asil sebebi
# --------------------------------------------------------------------------- #


async def test_BACKFILL_deterministiktir_sayac_tohumlanir_ve_sonraki_fis_CAKISMAZ():
    """🔴 Uc iddia TEK turda: (a) sira · (b) sayac tohumu · (c) cakisma yok.

    (a) Numaralar yil ICINDE `ORDER BY entry_date, id` ile eslesir. Tohumda ayni
        tarihten UC fis vardir; ikinci anahtar olmasaydi duzen TANIMSIZ olurdu.
    (b) Her yilin sayaci o yilin son sirasindan devam eder (`son + 1`).
    (c) Backfill'den SONRA gercek uretici cagrilir ve fis GERCEKTEN yazilir —
        sayac tohumlanmasaydi burasi `uq_journal_entries_entry_no` ihlaliyle
        patlardi.
    """
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", FIN1_REVISION, database=database)
        beklenen = await _tohumla(database)
        _run_alembic("upgrade", FISNO_REVISION, database=database)

        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            # (a) SIRA — beklenti testin kendi `(entry_date, id)` siralamasindan
            #     kurulmustur, uygulamanin kuralindan DEGIL.
            for yil in TOHUM:
                satirlar = await conn.fetch(
                    "SELECT id, entry_no FROM journal_entries "
                    "WHERE period_year = $1 ORDER BY entry_date, id",
                    yil,
                )
                olculen = [(str(satir["id"]), satir["entry_no"]) for satir in satirlar]
                assert olculen == beklenen[yil], yil

            # Numara TEKILDIR ve HICBIRI bos degildir.
            assert (
                await conn.fetchval("SELECT count(*) FROM journal_entries WHERE entry_no IS NULL")
                == 0
            )
            assert await conn.fetchval(
                "SELECT count(*) = count(DISTINCT entry_no) FROM journal_entries"
            )

            # (b) SAYAC TOHUMU — yil basina `son sira + 1`.
            sayaclar = {
                satir["year"]: satir["next_no"]
                for satir in await conn.fetch("SELECT year, next_no FROM journal_entry_counters")
            }
            assert sayaclar == {yil: len(tarihler) + 1 for yil, tarihler in TOHUM.items()}
        finally:
            await conn.close()

        # 🔴 (c) ORM ile yazar, yani `JournalEntry` MODELININ BUGUNKU kolon
        #     kumesini kullanir — ama veritabani `FISNO_REVISION`da duruyor.
        #     Sonraki bir dilim `journal_entries`e kolon ekledigi anda
        #     (MU-3A: `source_type`/`source_id`) bu insert `UndefinedColumnError`
        #     verirdi. Iddia (a) ve (b) YUKARIDA, DOGRU revizyonda zaten olculdu;
        #     (c)'nin olctugu sey SAYAC TOHUMUDUR ve sonraki migration'lar sayaci
        #     ELLEMEZ. Sema HEAD'e cikarilir ki bu test model ile revizyonun
        #     ayrisimindan DEGIL, yalnizca tohumdan kirilsin.
        _run_alembic("upgrade", "head", database=database)

        # (c) CAKISMA YOK — gercek uretici + gercek yazim.
        engine = create_async_engine(_sqlalchemy_dsn(database))
        try:
            session_factory = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            async with session_factory() as session:
                actor = (await session.execute(select(User))).scalars().one()
                yeni_no = await numbering.generate_entry_no(session, year=2026)
                assert yeni_no == numbering.format_entry_no(2026, len(TOHUM[2026]) + 1)
                session.add(
                    JournalEntry(
                        entry_no=yeni_no,
                        entry_date=date(2026, 8, 1),
                        period_year=2026,
                        period_month=8,
                        description="Backfill sonrasi ilk gercek fis",
                        status=JournalEntryStatus.draft,
                        total_debit=Decimal("100.00"),
                        total_credit=Decimal("100.00"),
                        created_by_id=actor.id,
                    )
                )
                # Patlarsa sayac tohumlanmamis demektir.
                await session.commit()
        finally:
            await engine.dispose()
    finally:
        await _drop_scratch_database(database)


async def test_backfill_9999u_ASAR_ve_numara_BUDANMAZ():
    """🔴 `lpad` TUZAGI GERCEKTEN kosturulur: 10.001 fis tohumlanir.

    `lpad('10000', 4, '0')` -> `'1000'`. Postgres'in `lpad`i metni genislige
    **BUDAR**; naif bir `lpad(sira::text, 4, '0')` yazimi 10.000. fise
    `YEV-2030-1000` verirdi ve bu, 1.000. fisin numarasiyla CAKISIRDI.

    ⚠️ Bu test bilerek migration'in KENDI SQL'ini kosturur. Onceki hâli ayni
    ifadeyi teste ELLE KOPYALAYIP Python ile karsilastiriyordu ve mutasyon
    denemesinde KOR CIKTI: kopya dogruydu, migration yanlisti, test yesil
    kaldi. "Girdi var" ile "bekcilik ediyor" AYNI SEY DEGILDIR.

    Budama hâlinde iki sey birden olur: `uq_journal_entries_entry_no` kurulamaz
    (migration PATLAR) ve numara kumesi eksilir. Iddia ikisini de kapsar.

    Tohum TEK ifadedir (`generate_series`) — 10.001 satirlik Python dongusu
    dakikalar surerdi. Tarih hepsinde AYNIDIR: hangi `id`nin hangi sirayi
    aldigi burada ILGISIZDIR, iddia numara KUMESI uzerinedir (sira bekcisi bir
    onceki testtir).
    """
    yil = 2030
    adet = 10_001
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", FIN1_REVISION, database=database)
        actor_id = await _aktor_yarat(database)

        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            await conn.execute(
                "INSERT INTO journal_entries "
                "(id, entry_date, period_year, period_month, description, status, "
                " total_debit, total_credit, created_by_id) "
                "SELECT gen_random_uuid(), DATE '2030-06-15', $1, 6, "
                "       'Tohum ' || n, 'posted', 100, 100, $2 "
                "FROM generate_series(1, $3) AS n",
                yil,
                actor_id,
                adet,
            )
        finally:
            await conn.close()

        # 🔴 Budama olsaydi UNIQUE kurulamaz ve BURASI patlardi.
        _run_alembic("upgrade", FISNO_REVISION, database=database)

        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            numaralar = {
                satir["entry_no"]
                for satir in await conn.fetch(
                    "SELECT entry_no FROM journal_entries WHERE period_year = $1", yil
                )
            }
            assert numaralar == {
                numbering.format_entry_no(yil, sira) for sira in range(1, adet + 1)
            }
            # Sinirin iki yaninda ACIK iddia: dort haneden bese gecis BUDAMAZ.
            assert f"YEV-{yil}-9999" in numaralar
            assert f"YEV-{yil}-10000" in numaralar
            assert f"YEV-{yil}-10001" in numaralar
            assert (
                await conn.fetchval(
                    "SELECT next_no FROM journal_entry_counters WHERE year = $1", yil
                )
                == adet + 1
            )
        finally:
            await conn.close()

        # Sonraki GERCEK fis de bes haneli numarayi BUDAMADAN alir.
        engine = create_async_engine(_sqlalchemy_dsn(database))
        try:
            session_factory = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            async with session_factory() as session:
                assert await numbering.generate_entry_no(session, year=yil) == f"YEV-{yil}-10002"
        finally:
            await engine.dispose()
    finally:
        await _drop_scratch_database(database)

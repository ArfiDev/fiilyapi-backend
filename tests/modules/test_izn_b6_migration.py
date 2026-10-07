"""IZN-B6c — `izn_b6_eski_izin_tablolari_drop`: DROP, WARNING sayımı, birebir downgrade.

Gerçek PG'de B5a (`d4b8f1a6c3e9`) → B6c → downgrade → B6c. Çakılanlar:
* UPGRADE: 4 eski tablo + 3 enum tipi YOK; satır sayıları WARNING olarak basılır, akış durmaz.
* DOWNGRADE: tablolar/tipler BOŞ ve B5a şemasıyla BİREBİR (kolon, kısıt, indeks, enum etiket
  sırası) geri gelir; ikinci upgrade aynı durumu kurar.
* Enum tipi başka bir kolonda hâlâ kullanılıyorsa migration RuntimeError ile DURUR, hiçbir şey
  düşmez.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg

from tests.modules.approvals.test_ok1a_migration import (
    _create_scratch_database,
    _current_revision,
    _drop_scratch_database,
    _table_exists,
    _type_exists,
)
from tests.modules.test_izn_b1_migration import _alembic, _baglan, _cikti, _upgrade

B5_REVISION = "d4b8f1a6c3e9"
B6_REVISION = "e6b1d9f3a7c2"
VERSIONS_DIR = Path(__file__).parents[2] / "alembic" / "versions"
MIGRATION_PATH = next(VERSIONS_DIR.glob("*_izn_b6_eski_izin_tablolari_drop.py"))
ESKI_TABLOLAR = ("modules", "role_permissions", "user_project_access", "user_disciplines")
ESKI_TIPLER = ("scope", "access_level", "module_group")


def test_zincir_b5anin_ustunde() -> None:
    metin = MIGRATION_PATH.read_text(encoding="utf-8")
    assert f'down_revision: str | Sequence[str] | None = "{B5_REVISION}"' in metin
    assert f'revision: str = "{B6_REVISION}"' in metin


def test_migration_app_import_etmez() -> None:
    metin = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "from app" not in metin and "import app" not in metin


async def _sema_dokumu(conn: asyncpg.Connection) -> dict[str, list[tuple]]:
    """4 tablo + 3 tipin şeması: kolon, kısıt tanımı, indeks tanımı, enum etiket sırası."""
    tablolar = list(ESKI_TABLOLAR)
    kolonlar = await conn.fetch(
        "SELECT table_name, ordinal_position, column_name, data_type, udt_name, is_nullable, "
        "column_default, character_maximum_length FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = ANY($1) ORDER BY 1, 2",
        tablolar,
    )
    kisitlar = await conn.fetch(
        "SELECT conrelid::regclass::text AS tablo, conname, contype::text, "
        "pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid::regclass::text = ANY($1) ORDER BY 1, 2",
        tablolar,
    )
    indeksler = await conn.fetch(
        "SELECT tablename, indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = ANY($1) ORDER BY 1, 2",
        tablolar,
    )
    etiketler = await conn.fetch(
        "SELECT t.typname, e.enumsortorder::int, e.enumlabel FROM pg_enum e "
        "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = ANY($1) ORDER BY 1, 2",
        list(ESKI_TIPLER),
    )
    return {
        "kolonlar": [tuple(r) for r in kolonlar],
        "kisitlar": [tuple(r) for r in kisitlar],
        "indeksler": [tuple(r) for r in indeksler],
        "etiketler": [tuple(r) for r in etiketler],
    }


async def _hepsi_var(conn: asyncpg.Connection) -> bool:
    tablolar = [await _table_exists(conn, t) for t in ESKI_TABLOLAR]
    tipler = [await _type_exists(conn, t) for t in ESKI_TIPLER]
    return all(tablolar) and all(tipler)


async def _hicbiri_yok(conn: asyncpg.Connection) -> bool:
    tablolar = [await _table_exists(conn, t) for t in ESKI_TABLOLAR]
    tipler = [await _type_exists(conn, t) for t in ESKI_TIPLER]
    return not any(tablolar) and not any(tipler)


async def test_upgrade_downgrade_birebir_ve_ikinci_upgrade() -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(B5_REVISION, database)
        conn = await _baglan(database)
        try:
            assert await _hepsi_var(conn)
            once = await _sema_dokumu(conn)
            moduller = await conn.fetchval("SELECT count(*) FROM modules")
            izinler = await conn.fetchval("SELECT count(*) FROM role_permissions")
            assert moduller > 0 and izinler > 0  # seed verisi var: WARNING sayıları sıfır değil
        finally:
            await conn.close()

        sonuc = _upgrade(B6_REVISION, database)
        cikti = _cikti(sonuc)
        assert f"role_permissions DROP edilecek, {izinler} satir" in cikti
        assert f"modules DROP edilecek, {moduller} satir" in cikti
        assert "user_project_access DROP edilecek, 0 satir" in cikti
        assert "user_disciplines DROP edilecek, 0 satir" in cikti

        conn = await _baglan(database)
        try:
            assert await _current_revision(conn) == B6_REVISION
            assert await _hicbiri_yok(conn)
        finally:
            await conn.close()

        sonuc = _alembic("downgrade", "-1", database=database)
        assert sonuc.returncode == 0, _cikti(sonuc)
        conn = await _baglan(database)
        try:
            assert await _current_revision(conn) == B5_REVISION
            assert await _hepsi_var(conn)
            for tablo in ESKI_TABLOLAR:  # veri geri GELMEZ
                assert await conn.fetchval(f"SELECT count(*) FROM {tablo}") == 0  # noqa: S608
            sonra = await _sema_dokumu(conn)
        finally:
            await conn.close()
        assert sonra == once  # birebir şema

        _upgrade(B6_REVISION, database)
        conn = await _baglan(database)
        try:
            assert await _hicbiri_yok(conn)
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_tip_baska_kolonda_kullaniliyorsa_durur() -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(B5_REVISION, database)
        conn = await _baglan(database)
        try:
            await conn.execute("CREATE TABLE sentetik_kullanan (id int, kapsam scope NOT NULL)")
        finally:
            await conn.close()

        sonuc = _alembic("upgrade", B6_REVISION, database=database)
        assert sonuc.returncode != 0
        cikti = _cikti(sonuc)
        assert "RuntimeError" in cikti
        assert "sentetik_kullanan.kapsam (scope)" in cikti

        conn = await _baglan(database)
        try:
            # İşlem geri alındı: revizyon ve 4 tablo + 3 tip yerinde.
            assert await _current_revision(conn) == B5_REVISION
            assert await _hepsi_var(conn)
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

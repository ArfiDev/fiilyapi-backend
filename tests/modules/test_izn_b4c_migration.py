"""IZN-B4c madde 20 — `izn_b4c_eski_rol_gizli_alanlari` migration'ının veri bekçileri (gerçek PG).

Her test KENDİ TEK KULLANIMLIK veritabanını açar (`test_izn_b1_migration.py` emsali); revizyonlara
AÇIKÇA çıkılır. `.env` ve `TEST_DATABASE_URL` veritabanı ELLENMEZ.

Kural: yalnız 4 eski rol, kümesi TAM {tum_tutarlar} iken, rol adı geçen gizli alan denetim kaydı
yokken değişir; ekrandan değiştirilmiş küme DOKUNULMAZ; downgrade yalnız onaylı kümeyi geri çevirir.
"""

import asyncpg

from app.modules.roles import seed_data
from tests.modules.approvals.test_ok1a_migration import (
    _create_scratch_database,
    _drop_scratch_database,
)
from tests.modules.test_izn_b1_migration import _alembic, _baglan, _hidden, _upgrade

ONCEKI_REVISION = "e8b4c2d6f9a1"
B4C_REVISION = "a1d6e4b8c2f7"
TUM = {"tum_tutarlar"}
DORT_ROL = ["hr_manager", "site_chief", "field_engineer", "procurement"]
ONAYLI = {k: {c.value for c in v} for k, v in seed_data.ESKI_ROL_GIZLI_ALANLAR.items()}
DOKUNULMAYAN = ["viewer", "warehouse_keeper", "planning_engineer", "cost_engineer", "patron"]


async def _tum_kumeler(conn: asyncpg.Connection) -> dict[str, set[str]]:
    keys = [r[0] for r in await conn.fetch("SELECT key FROM roles")]
    return {key: await _hidden(conn, key) for key in keys}


async def _kur_onceki(database: str) -> asyncpg.Connection:
    _upgrade(ONCEKI_REVISION, database)
    conn = await _baglan(database)
    for key in DORT_ROL:
        assert await _hidden(conn, key) == TUM, key  # B1'in türettiği durum
    return conn


def test_migration_zinciri_e8b4in_ustunde() -> None:
    from pathlib import Path

    yol = next(
        (Path(__file__).parents[2] / "alembic" / "versions").glob(
            "*_izn_b4c_eski_rol_gizli_alanlari.py"
        )
    )
    metin = yol.read_text(encoding="utf-8")
    assert f'down_revision: str | Sequence[str] | None = "{ONCEKI_REVISION}"' in metin
    assert f'revision: str = "{B4C_REVISION}"' in metin


def test_migration_sabitleri_seed_ile_ayni() -> None:
    import importlib.util
    import sys
    from pathlib import Path

    yol = next(
        (Path(__file__).parents[2] / "alembic" / "versions").glob(
            "*_izn_b4c_eski_rol_gizli_alanlari.py"
        )
    )
    spec = importlib.util.spec_from_file_location("_migration_izn_b4c", yol)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    assert {k: set(v) for k, v in modul.APPROVED_SETS.items()} == ONAYLI
    assert set(modul.APPROVED_SETS) == set(DORT_ROL)
    assert modul.OLD_SET == TUM


async def test_dort_rol_onayli_kumeye_gecer_digerleri_dokunulmaz() -> None:
    database = await _create_scratch_database()
    try:
        conn = await _kur_onceki(database)
        try:
            once = await _tum_kumeler(conn)
            _upgrade(B4C_REVISION, database)
            sonra = await _tum_kumeler(conn)
        finally:
            await conn.close()
        for key in DORT_ROL:
            assert sonra[key] == ONAYLI[key], key
        assert "tum_tutarlar" not in sonra["hr_manager"]
        assert "maas_kisisel" not in sonra["hr_manager"]
        assert {"maliyet_kar", "maas_kisisel"}.isdisjoint(sonra["procurement"])
        degismeyen = {k: v for k, v in once.items() if k not in DORT_ROL}
        assert {k: v for k, v in sonra.items() if k not in DORT_ROL} == degismeyen
        assert sonra["viewer"] == {"tum_tutarlar", "maas_kisisel"}
        assert sonra["warehouse_keeper"] == TUM
        # Seed (create_all) durumuyla aynı gerçek: migration zinciri HEAD = seed.
        for key, kume in seed_data.HIDDEN_FIELDS.items():
            assert sonra[key] == {c.value for c in kume}, key
    finally:
        await _drop_scratch_database(database)


async def test_ekrandan_degistirilmis_rol_dokunulmaz() -> None:
    database = await _create_scratch_database()
    try:
        conn = await _kur_onceki(database)
        try:
            await conn.execute(
                "INSERT INTO role_hidden_fields (role_id, category) "
                "SELECT id, 'maas_kisisel' FROM roles WHERE key = 'hr_manager'"
            )
            await conn.execute(
                "DELETE FROM role_hidden_fields WHERE category = 'tum_tutarlar' AND role_id = "
                "(SELECT id FROM roles WHERE key = 'procurement')"
            )
            _upgrade(B4C_REVISION, database)
            assert await _hidden(conn, "hr_manager") == {"tum_tutarlar", "maas_kisisel"}
            assert await _hidden(conn, "procurement") == set()
            assert await _hidden(conn, "site_chief") == ONAYLI["site_chief"]
            assert await _hidden(conn, "field_engineer") == ONAYLI["field_engineer"]
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_gizli_alan_denetim_kaydi_olan_rol_dokunulmaz() -> None:
    database = await _create_scratch_database()
    try:
        conn = await _kur_onceki(database)
        try:
            ad = await conn.fetchval("SELECT name FROM roles WHERE key = 'site_chief'")
            await conn.execute(
                "INSERT INTO audit_log (id, action, detail) "
                "VALUES (gen_random_uuid(), 'update', $1)",
                f"Gizli alanlar değişti: {ad} · gizli: Tüm tutarlar",
            )
            # Başka bir rolün adı için kayıt YOK; ad öneki farklı roller karışmaz.
            _upgrade(B4C_REVISION, database)
            assert await _hidden(conn, "site_chief") == TUM
            assert await _hidden(conn, "hr_manager") == ONAYLI["hr_manager"]
            assert await _hidden(conn, "procurement") == ONAYLI["procurement"]
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_downgrade_onayli_kumeyi_tum_tutarlara_geri_cevirir() -> None:
    database = await _create_scratch_database()
    try:
        conn = await _kur_onceki(database)
        try:
            once = await _tum_kumeler(conn)
            _upgrade(B4C_REVISION, database)
            sonuc = _alembic("downgrade", ONCEKI_REVISION, database=database)
            assert sonuc.returncode == 0, sonuc.stdout + sonuc.stderr
            assert await _tum_kumeler(conn) == once
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_downgrade_onayli_olmayan_kumeye_dokunmaz() -> None:
    database = await _create_scratch_database()
    try:
        conn = await _kur_onceki(database)
        try:
            _upgrade(B4C_REVISION, database)
            # Ekrandan: hr_manager artık maas_kisisel'i de gizliyor → onaylı küme DEĞİL.
            await conn.execute(
                "INSERT INTO role_hidden_fields (role_id, category) "
                "SELECT id, 'maas_kisisel' FROM roles WHERE key = 'hr_manager'"
            )
            sonuc = _alembic("downgrade", ONCEKI_REVISION, database=database)
            assert sonuc.returncode == 0, sonuc.stdout + sonuc.stderr
            assert await _hidden(conn, "hr_manager") == ONAYLI["hr_manager"] | {"maas_kisisel"}
            for key in ("site_chief", "field_engineer", "procurement"):
                assert await _hidden(conn, key) == TUM, key
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

"""IZN-B5a madde 13 — `izn_b5a_onay_kutusu_onay_biti` migration'ının veri bekçisi (gerçek PG).

Her test KENDİ TEK KULLANIMLIK veritabanını açar (`test_izn_b4c_migration.py` emsali).
Kural: `genel.onay_kutusu` hücrelerinde `can_approve` false olur, düzey ve diğer sayfalar
DEĞİŞMEZ; downgrade işlemsizdir.
"""

from app.core.sayfalar import SAYFA_BY_KEY
from tests.modules.approvals.test_ok1a_migration import (
    _create_scratch_database,
    _drop_scratch_database,
)
from tests.modules.test_izn_b1_migration import _alembic, _baglan, _upgrade

ONCEKI_REVISION = "a1d6e4b8c2f7"
B5A_REVISION = "c7e2a9d4b1f3"
ONAY_KUTUSU = "genel.onay_kutusu"

_HUCRELER = (
    "SELECT r.key, p.page_key, p.level::text, p.can_approve FROM role_page_permissions p "
    "JOIN roles r ON r.id = p.role_id ORDER BY r.key, p.page_key"
)


def test_katalogda_onay_kutusunun_onay_eylemi_yok() -> None:
    assert SAYFA_BY_KEY[ONAY_KUTUSU].onay_var is False


async def test_onay_kutusu_onay_bitleri_kapanir_digerleri_degismez() -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(ONCEKI_REVISION, database)
        conn = await _baglan(database)
        try:
            # Eski katalogda ekrandan açılabilen bit (patron seed'i dışında bir rol de).
            await conn.execute(
                "UPDATE role_page_permissions SET can_approve = true, level = 'view' "
                "WHERE page_key = $1 AND role_id = (SELECT id FROM roles WHERE key = 'viewer')",
                ONAY_KUTUSU,
            )
            once = await conn.fetch(_HUCRELER)
            assert any(r["page_key"] == ONAY_KUTUSU and r["can_approve"] for r in once)
            _upgrade(B5A_REVISION, database)
            sonra = await conn.fetch(_HUCRELER)
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

    assert not any(r["page_key"] == ONAY_KUTUSU and r["can_approve"] for r in sonra)
    beklenen = [
        (r["key"], r["page_key"], r["level"], False if r["page_key"] == ONAY_KUTUSU else r[3])
        for r in once
    ]
    assert [(r["key"], r["page_key"], r["level"], r[3]) for r in sonra] == beklenen


# --- madde 3: satış alt sayfaları Görür (d4b8f1a6c3e9) ---

SATIS_REVISION = "d4b8f1a6c3e9"
SATIS = ("mali.satis_blok", "mali.satis_unite", "mali.satis_excel", "mali.satis_paylasim")
B5A_SAYFALARI = (*SATIS, ONAY_KUTUSU)


def _satis_migration():
    import importlib.util
    import sys
    from pathlib import Path

    yol = next(
        (Path(__file__).parents[2] / "alembic" / "versions").glob(
            "*_izn_b5a_satis_alt_sayfa_gorur.py"
        )
    )
    spec = importlib.util.spec_from_file_location("_migration_izn_b5a_satis", yol)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


def test_satis_migration_sabitleri_kodla_ayni() -> None:
    from app.core.access import AccessLevel
    from app.core.page_gate import gate_flags

    modul = _satis_migration()
    assert modul.down_revision == B5A_REVISION
    assert modul.SATIS_SAYFALARI == SATIS
    simdi = {page for page, flag in gate_flags("projects", AccessLevel.view) if flag == "view"}
    assert set(modul.SATIS_SAYFALARI) <= simdi
    assert set(modul.ESKI_PROJE_GORME) == simdi - set(SATIS)


async def _sayfa_hucreleri(conn) -> dict[str, dict[str, tuple[str, bool]]]:
    rows = await conn.fetch(_HUCRELER)
    sonuc: dict[str, dict[str, tuple[str, bool]]] = {}
    for r in rows:
        sonuc.setdefault(r["key"], {})[r["page_key"]] = (r["level"], r[3])
    return sonuc


async def test_head_b5a_sayfalari_seed_ile_ayni() -> None:
    """seed = migrate edilmiş DB (CEO kararı a): B5a'nın değiştirdiği 5 sayfada her rol."""
    from app.modules.roles import seed_data

    database = await _create_scratch_database()
    try:
        _upgrade(SATIS_REVISION, database)
        conn = await _baglan(database)
        try:
            db = await _sayfa_hucreleri(conn)
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

    for role_key, cells in seed_data.PAGE_MATRIX.items():
        for page in B5A_SAYFALARI:
            level, approve = cells.get(page, (seed_data.PageLevel.none, False))
            assert db[role_key].get(page, ("none", False)) == (level.value, approve), (
                role_key,
                page,
            )


async def test_satis_downgrade_yalniz_acilanlari_kapatir_ekrandan_degiseni_korur() -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(B5A_REVISION, database)
        conn = await _baglan(database)
        try:
            once = await _sayfa_hucreleri(conn)
            _upgrade(SATIS_REVISION, database)
            acik = await _sayfa_hucreleri(conn)
            assert acik["viewer"]["mali.satis_blok"] == ("view", False)
            # Ekrandan kaydedilmiş rol (denetim kaydı) downgrade'de korunur.
            await conn.execute(
                "INSERT INTO audit_log (id, occurred_at, action, detail) VALUES "
                "(gen_random_uuid(), now(), 'update', "
                "'Sayfa izinleri değişti: ' || (SELECT name FROM roles WHERE key = 'viewer') "
                "|| ' (1 sayfa) · x')"
            )
            sonuc = _alembic("downgrade", B5A_REVISION, database=database)
            assert sonuc.returncode == 0, sonuc.stderr
            geri = await _sayfa_hucreleri(conn)
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

    for role_key, cells in once.items():
        beklenen = acik[role_key] if role_key == "viewer" else cells
        assert geri[role_key] == beklenen, role_key
    assert any(once[k] != acik[k] for k in once if k != "viewer")

"""IZN-B2 — `izn_b2` migration'ı: Onaylar eşiği düzeltmesi + Patron `is_system=false`.

Üç şey çakılır:
1. DONMUŞ migration kopyası ↔ katalog: `AFFECTED_PAGES` eski/yeni eşikleri `core/sayfalar`
   (`_ESIKLER` + `ESIK_SPEC_B1_FARKLARI`) ile, 6 yeni rolün iki modül düzeyi `seed_data.IZN_MATRIX`
   ile eşit (migration `app` import etmez; elle kopya).
2. Gerçek PG'de upgrade (B1 → B2): tüm roller için sayfa hücreleri `seed_data.PAGE_MATRIX`
   (B2 eşikleri) ile BİREBİR; yalnız `can_approve` değişir; CANLI satırdan türetme (ekrandan
   değiştirilmiş hücre seed'e dönmez); Patron `is_system=false`.
3. Downgrade: B1 hücrelerine ve `patron.is_system=true`ya döner.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from app.core.access import AccessLevel
from app.core.sayfalar import ESIK_SPEC_B1_FARKLARI, SAYFA_BY_KEY, SAYFALAR, esik_spec
from app.modules.roles import seed_data
from tests._izn_b1_esikleri import ESIK_SPEC_B5A_FARKLARI, b1_rows
from tests.modules.approvals.test_ok1a_migration import (
    _create_scratch_database,
    _drop_scratch_database,
)
from tests.modules.test_izn_b1_migration import (
    IZN_REVISION as B1_REVISION,
)
from tests.modules.test_izn_b1_migration import (
    _baglan,
    _custom_role,
    _live_cell_set,
    _page_cells,
    _upgrade,
)

B2_REVISION = "671436c0b42a"
MIGRATION_PATH = next(
    (Path(__file__).parents[2] / "alembic" / "versions").glob("*_izn_b2_esik_duzeltmesi.py")
)
ETKILENEN_SAYFALAR = [
    "stok.satinalma_talepleri",
    "mali.hakedis_isveren",
    "mali.hakedis_taseron",
    "proje.isveren_hakedis",
    "proje.taseron_hakedis",
    "santiye.hakedisler",
]


@pytest.fixture(scope="module")
def migration():
    spec = importlib.util.spec_from_file_location(
        f"_migration_{MIGRATION_PATH.stem}", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- 1) migration kopyası ↔ katalog (DB gerektirmez) ----------------------------------------


def test_zincir_b1in_ustunde_ve_tek_head() -> None:
    metin = MIGRATION_PATH.read_text(encoding="utf-8")
    assert f'down_revision: str | Sequence[str] | None = "{B1_REVISION}"' in metin


def test_etkilenen_sayfalar_katalogdaki_eski_ve_yeni_esiklerle_AYNI(migration) -> None:
    assert list(migration.AFFECTED_PAGES) == ETKILENEN_SAYFALAR
    assert {s.key for s in SAYFALAR if s.envanter_no in ESIK_SPEC_B1_FARKLARI} == set(
        ETKILENEN_SAYFALAR
    )
    letter = {"r": "request", "a": "approve", "x": "admin"}
    for page_key, (module, eski, yeni) in migration.AFFECTED_PAGES.items():
        sayfa = SAYFA_BY_KEY[page_key]
        assert sayfa.eski_modul == module
        eski_onay = ESIK_SPEC_B1_FARKLARI[sayfa.envanter_no].split("|")[2]
        yeni_onay = esik_spec(sayfa.envanter_no, sayfa.eski_modul).split("|")[2]
        assert letter[eski_onay] == eski, page_key
        assert letter[yeni_onay] == yeni, page_key
        # Görme/yazma eşikleri DEĞİŞMEDİ (migration yalnız `can_approve`a dokunur).
        assert (
            ESIK_SPEC_B1_FARKLARI[sayfa.envanter_no].split("|")[:2]
            == esik_spec(sayfa.envanter_no, sayfa.eski_modul).split("|")[:2]
        )


def test_yeni_rol_modul_duzeyleri_seed_ile_AYNI(migration) -> None:
    assert list(migration.IZN_ROLE_ORDER) == list(seed_data.IZN_ROLE_ORDER)
    for module, levels in migration.IZN_MODULE_LEVELS.items():
        assert levels == [a.value for a, _ in seed_data.IZN_MATRIX[module]], module
    modules = {module for module, _e, _y in migration.AFFECTED_PAGES.values()}
    assert set(migration.IZN_MODULE_LEVELS) == modules
    assert set(migration.LEVEL_RANK) == {a.value for a in AccessLevel}


def test_seed_patron_is_system_false() -> None:
    patron = next(r for r in seed_data.ROLES if r["key"] == "patron")
    assert patron["is_system"] is False


# --- 2) gerçek PG: B1 → B2 --------------------------------------------------------------------


def _beklenen_b2(role_key: str) -> dict[str, tuple[str, bool]]:
    """B2 migration'ının ürettiği hücreler = bugünkü `PAGE_MATRIX` — B5a'nın değiştirdiği sayfalar
    HARİÇ: onlar B2 çıktısında B1 eşikleriyle türemiş değerde kalır (B5a hücre taşıması ayrı
    veri migration'ıdır; bilinçli fark, IZN-B5a maddeler 3 ve 5)."""
    beklenen = {k: (lv.value, ap) for k, (lv, ap) in seed_data.PAGE_MATRIX[role_key].items()}
    matris = seed_data.MATRIX if role_key in seed_data.ROLE_ORDER else seed_data.IZN_MATRIX
    eski = b1_rows(matris, role_key)
    for sayfa in SAYFALAR:
        if sayfa.envanter_no in ESIK_SPEC_B5A_FARKLARI:
            beklenen[sayfa.key] = eski[sayfa.key]
    return beklenen


async def test_upgrade_tum_rollerin_hucreleri_PAGE_MATRIX_ile_birebir_yalniz_can_approve_degisir(
    migration,
) -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(B1_REVISION, database)
        conn = await _baglan(database)
        try:
            once = {k: await _page_cells(conn, k) for k in seed_data.PAGE_MATRIX}
            assert await conn.fetchval("SELECT is_system FROM roles WHERE key = 'patron'") is True
        finally:
            await conn.close()
        for role_key, hucreler in once.items():  # B1 çıktısı sanity
            eski = b1_rows(seed_data.MATRIX, role_key) if role_key in seed_data.ROLE_ORDER else None
            if eski is not None:
                assert hucreler == eski, role_key

        sonuc = _upgrade(B2_REVISION, database)
        conn = await _baglan(database)
        try:
            for role_key in seed_data.PAGE_MATRIX:
                sonra = await _page_cells(conn, role_key)
                assert sonra == _beklenen_b2(role_key), role_key
                # Yalnız etkilenen sayfaların `can_approve`ı değişebilir; `level` ASLA.
                for page_key, (level, approve) in sonra.items():
                    assert level == once[role_key][page_key][0], (role_key, page_key)
                    if page_key not in ETKILENEN_SAYFALAR:
                        assert approve == once[role_key][page_key][1], (role_key, page_key)
            assert await conn.fetchval("SELECT is_system FROM roles WHERE key = 'patron'") is False
            # Sistem Yöneticisi hücre taşımaz (dokunulmadı).
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM role_page_permissions p JOIN roles r ON r.id = p.role_id "
                    "WHERE r.key = 'system_admin'"
                )
                == 0
            )
            muhasebe = await _page_cells(conn, "accounting")
            assert muhasebe["mali.hakedis_isveren"] == ("edit", True)  # DARALMA kapandı
            pm = await _page_cells(conn, "project_manager")
            assert pm["stok.satinalma_talepleri"] == ("edit", True)
            sef = await _page_cells(conn, "site_chief")
            assert sef["mali.hakedis_isveren"] == ("edit", False)  # draft: onaylamaz
            assert sef["stok.satinalma_talepleri"] == ("edit", False)  # request: yalnız gönderir
        finally:
            await conn.close()
        assert "IZN-B2" in sonuc.stdout + sonuc.stderr
    finally:
        await _drop_scratch_database(database)


async def test_ekrandan_degistirilmis_hucre_CANLI_degeriyle_turetilir_ozel_rol_dahil(
    migration,
) -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(B1_REVISION, database)
        conn = await _baglan(database)
        try:
            # Şef: canlıda hakedişte `approve` verilmiş (seed `draft`) → B1 `approve=False` idi.
            await _live_cell_set(conn, "site_chief", "progress_payments", "approve", "all")
            # Muhasebe: canlıda hakedişi `view`a çekilmiş → Düzenler/Onaylar yok kalmalı.
            await _live_cell_set(conn, "accounting", "progress_payments", "view", "all")
            await _custom_role(conn, "ozel_bos", {})  # B1 sonrası açılmış satırsız rol: DOKUNULMAZ
        finally:
            await conn.close()
        _upgrade(B2_REVISION, database)
        conn = await _baglan(database)
        try:
            sef = await _page_cells(conn, "site_chief")
            assert sef["mali.hakedis_isveren"] == ("edit", True)  # canlı `approve` taşındı
            muhasebe = await _page_cells(conn, "accounting")
            assert muhasebe["mali.hakedis_isveren"][1] is False  # canlı `view`: onay yok
            bos = await _page_cells(conn, "ozel_bos")
            assert bos == {}  # B1 sonrası açılan satırsız rolün hücresi yok: migration eklemez
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_downgrade_B1_hucrelerine_ve_patron_is_system_true(migration) -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(B2_REVISION, database)
        from tests.modules.test_izn_b1_migration import _alembic

        sonuc = _alembic("downgrade", B1_REVISION, database=database)
        assert sonuc.returncode == 0, sonuc.stdout + sonuc.stderr
        conn = await _baglan(database)
        try:
            for role_key in seed_data.ROLE_ORDER:
                if role_key == "system_admin":
                    continue
                assert await _page_cells(conn, role_key) == b1_rows(seed_data.MATRIX, role_key), (
                    role_key
                )
            assert await conn.fetchval("SELECT is_system FROM roles WHERE key = 'patron'") is True
        finally:
            await conn.close()
        # Yeniden upgrade aynı B2 durumunu kurar (idempotent çift yön).
        _upgrade(B2_REVISION, database)
        conn = await _baglan(database)
        try:
            assert await _page_cells(conn, "accounting") == _beklenen_b2("accounting")
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

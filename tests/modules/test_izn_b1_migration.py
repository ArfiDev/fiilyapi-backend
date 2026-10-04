"""IZN-B1 — `izn_b1` migration'ının şema, DERİVASYON ve tur bekçileri (gerçek PG).

Revizyonlara AÇIKÇA çıkılır (`head` / `-1` KULLANILMAZ — `test_okt_b1_migration.py` emsali).
Alembic alt süreçte koşar ve her test KENDİ TEK KULLANIMLIK veritabanını açar; `.env` ve
`TEST_DATABASE_URL` veritabanı ELLENMEZ.

🔴 EN ÖNEMLİ İDDİA (CEO kararı): sayfa hücreleri KOD SABİTİNDEN DEĞİL, DB'deki canlı
`role_permissions` satırlarından türetilir. Canlıda hücreler ekrandan değiştirilmiş olabilir;
seed'e dönen bir migration o değişikliği sessizce SİLERDİ. Bunu şu testler kilitler:
`test_ekrandan_degistirilmis_hucre_CANLI_degeriyle_tasinir`,
`test_satiri_silinmis_hucre_seed_hucresine_duser`, `test_ozel_rol_tasinir`.

⚠️ PG SÜRÜM TUZAĞI: yerel 18, CI 16 — sürüme özgü SQLSTATE iddia edilmez.
"""

import asyncio
import os
import re
import subprocess
import time
import uuid

import asyncpg

from app.core.sayfalar import SAYFA_ANAHTARLARI
from app.modules.roles import seed_data
from tests._izn_b1_esikleri import b1_rows
from tests.modules.approvals.test_ok1a_migration import (
    ALEMBIC_CMD,
    BACKEND_DIR,
    _asyncpg_dsn,
    _create_scratch_database,
    _current_revision,
    _drop_scratch_database,
    _table_exists,
    _type_exists,
)

IZN_REVISION = "c5e9a3b7d1f4"
ONCEKI_REVISION = "b7c3e9a1d5f2"

YENI_ROL_ANAHTARLARI = sorted(seed_data.IZN_ROLE_ORDER)
SAYFA_SAYISI = 100
SISYON = "system_admin"
NON_ADMIN_ESKI_ROLLER = [r for r in seed_data.ROLE_ORDER if r != SISYON]


def _alembic(*args: str, database: str) -> subprocess.CompletedProcess[str]:
    """`_run_alembic` gibi ama BAŞARISIZLIĞI testin kendisi değerlendirir (fail-closed testleri)."""
    env = {**os.environ, "DATABASE_URL": _asyncpg_dsn(database)}
    return subprocess.run(
        [*ALEMBIC_CMD, *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _upgrade(revision: str, database: str) -> subprocess.CompletedProcess[str]:
    sonuc = _alembic("upgrade", revision, database=database)
    assert sonuc.returncode == 0, f"alembic upgrade {revision}:\n{sonuc.stdout}\n{sonuc.stderr}"
    return sonuc


def _cikti(sonuc: subprocess.CompletedProcess[str]) -> str:
    return sonuc.stdout + sonuc.stderr


async def _baglan(database: str) -> asyncpg.Connection:
    return await asyncpg.connect(_asyncpg_dsn(database))


async def _live_cell_set(conn, role_key: str, module_key: str, level: str, scope: str) -> None:
    sonuc = await conn.execute(
        "UPDATE role_permissions SET access_level = $3::access_level, scope = $4::scope "
        "WHERE role_id = (SELECT id FROM roles WHERE key = $1) "
        "AND module_id = (SELECT id FROM modules WHERE key = $2)",
        role_key,
        module_key,
        level,
        scope,
    )
    assert sonuc == "UPDATE 1", (role_key, module_key, sonuc)


async def _live_cell_delete(conn, role_key: str, module_key: str) -> None:
    sonuc = await conn.execute(
        "DELETE FROM role_permissions "
        "WHERE role_id = (SELECT id FROM roles WHERE key = $1) "
        "AND module_id = (SELECT id FROM modules WHERE key = $2)",
        role_key,
        module_key,
    )
    assert sonuc == "DELETE 1", (role_key, module_key, sonuc)


async def _custom_role(conn, key: str, cells: dict[str, tuple[str, str]]) -> uuid.UUID:
    """Ekrandan açılmış özel rol: yalnız verilen modüllerde satırı olur (eski rol deseni)."""
    role_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO roles (id, key, name, emoji, description, is_system) "
        "VALUES ($1, $2, $3, '', '', false)",
        role_id,
        key,
        key.upper(),
    )
    for module_key, (level, scope) in cells.items():
        await conn.execute(
            "INSERT INTO role_permissions (id, role_id, module_id, access_level, scope) "
            "VALUES (gen_random_uuid(), $1, (SELECT id FROM modules WHERE key = $2), "
            "$3::access_level, $4::scope)",
            role_id,
            module_key,
            level,
            scope,
        )
    return role_id


async def _page_cells(conn, role_key: str) -> dict[str, tuple[str, bool]]:
    rows = await conn.fetch(
        "SELECT p.page_key, p.level::text, p.can_approve FROM role_page_permissions p "
        "JOIN roles r ON r.id = p.role_id WHERE r.key = $1",
        role_key,
    )
    return {row[0]: (row[1], row[2]) for row in rows}


async def _hidden(conn, role_key: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT h.category::text FROM role_hidden_fields h "
        "JOIN roles r ON r.id = h.role_id WHERE r.key = $1",
        role_key,
    )
    return {row[0] for row in rows}


def _expected_pages(role_key: str) -> dict[str, tuple[str, bool]]:
    """B1 migration'ının ÜRETTİĞİ hücreler (B1 eşikleriyle; B2 düzeltmesi `izn_b2`de)."""
    if role_key in seed_data.IZN_ROLE_ORDER:
        rows = b1_rows(seed_data.IZN_MATRIX, role_key)
        rows.update(
            {
                k: (lv.value, ap)
                for k, (lv, ap) in seed_data.IZN_SAYFA_ISTISNALARI.get(role_key, {}).items()
            }
        )
        return rows
    return b1_rows(seed_data.MATRIX, role_key)


# ---------------------------------------------------------------------------
# 1) Şema + seed
# ---------------------------------------------------------------------------


async def test_upgrade_tablolari_enumlari_ve_100_sayfalik_hucreleri_kurar() -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(ONCEKI_REVISION, database)
        sonuc = _upgrade(IZN_REVISION, database)
        conn = await _baglan(database)
        try:
            assert await _current_revision(conn) == IZN_REVISION
            for tablo in ("role_page_permissions", "role_hidden_fields"):
                assert await _table_exists(conn, tablo), tablo
            for tip in ("page_level", "hidden_category"):
                assert await _type_exists(conn, tip), tip

            # 7 eski + 6 yeni = 13 rol × 100 sayfa; Sistem Yöneticisi hücre TAŞIMAZ.
            roller = await conn.fetchval("SELECT count(*) FROM roles")
            assert roller == 14
            assert await conn.fetchval("SELECT count(*) FROM role_page_permissions") == 13 * 100
            sayim = dict(
                await conn.fetch(
                    "SELECT r.key, count(*) FROM role_page_permissions p "
                    "JOIN roles r ON r.id = p.role_id GROUP BY r.key"
                )
            )
            assert SISYON not in sayim
            assert set(sayim) == set(NON_ADMIN_ESKI_ROLLER) | set(YENI_ROL_ANAHTARLARI)
            assert set(sayim.values()) == {SAYFA_SAYISI}

            # Eski tablo DOKUNULMAZ.
            assert await conn.fetchval("SELECT count(*) FROM role_permissions") == 184

            # Anahtar kümesi katalogla birebir (DB seed ↔ kod sabiti bekçisi).
            anahtarlar = {
                r[0]
                for r in await conn.fetch("SELECT DISTINCT page_key FROM role_page_permissions")
            }
            assert anahtarlar == set(SAYFA_ANAHTARLARI)

            # Seed durumunda her rolün hücreleri ve gizli alanları PAGE_MATRIX/HIDDEN_FIELDS'tir.
            for role_key in NON_ADMIN_ESKI_ROLLER + YENI_ROL_ANAHTARLARI:
                assert await _page_cells(conn, role_key) == _expected_pages(role_key), role_key
                beklenen = {c.value for c in seed_data.HIDDEN_FIELDS[role_key]}
                assert await _hidden(conn, role_key) == beklenen, role_key

            # Seed durumunda sapma YOK; ama eşikli eşleme eski sade eşlemeyi KISAR (genişleme yok):
            # sayı ve satırlar WARNING'e basılır.
            cikti = _cikti(sonuc)
            assert "SAPAN canli hucre sayisi=0" in cikti
            assert "seed-rol (rol,modul) cifti=0" in cikti
            assert "Gormez'e dusen (rol,modul) cifti=0" in cikti
            assert re.search(r"KISILAN genisleme .* hucre sayisi=[1-9]\d*", cikti), cikti
            for satir in (
                "patron:teklif.teklif_hazirlama eski=edit/True yeni=edit/False",
                "accounting:mali.donem_kapanisi eski=edit/True yeni=edit/False",
                "patron:genel.projeler eski=edit/False yeni=view/False",
            ):
                assert satir in cikti, satir
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_yeni_roller_is_system_false_ve_role_permissions_satiri_YOK() -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(IZN_REVISION, database)
        conn = await _baglan(database)
        try:
            for row in seed_data.IZN_ROLES:
                kayit = await conn.fetchrow(
                    "SELECT id, name, emoji, description, is_system FROM roles WHERE key = $1",
                    row["key"],
                )
                assert kayit is not None, row["key"]
                assert (kayit["name"], kayit["emoji"], kayit["description"]) == (
                    row["name"],
                    row["emoji"],
                    row["description"],
                )
                assert kayit["is_system"] is False
                # Eski kapı onları 403 ile dışarıda tutar (fail-closed).
                assert (
                    await conn.fetchval(
                        "SELECT count(*) FROM role_permissions WHERE role_id = $1", kayit["id"]
                    )
                    == 0
                )
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_kisitlar_PK_CHECK_ve_FK_cascade_calisir() -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(IZN_REVISION, database)
        conn = await _baglan(database)
        try:
            role_id = await conn.fetchval("SELECT id FROM roles WHERE key = 'viewer'")
            # CHECK: onay, görünmeyen sayfada anlamsızdır.
            try:
                await conn.execute(
                    "INSERT INTO role_page_permissions (role_id, page_key, level, can_approve) "
                    "VALUES ($1, 'x.yok', 'none', true)",
                    role_id,
                )
                raise AssertionError("CHECK (onay ⇒ düzey ≠ none) ihlali kabul edildi")
            except asyncpg.CheckViolationError:
                pass
            # PK: aynı (rol, sayfa) iki kez yazılamaz.
            try:
                await conn.execute(
                    "INSERT INTO role_page_permissions (role_id, page_key, level, can_approve) "
                    "VALUES ($1, 'genel.raporlar', 'view', false)",
                    role_id,
                )
                raise AssertionError("PK ihlali kabul edildi")
            except asyncpg.UniqueViolationError:
                pass
            # FK + CASCADE: rol silinince hücreler ve gizli alanlar gider.
            await conn.execute("DELETE FROM roles WHERE id = $1", role_id)
            for tablo in ("role_page_permissions", "role_hidden_fields"):
                assert (
                    await conn.fetchval(f"SELECT count(*) FROM {tablo} WHERE role_id = $1", role_id)
                    == 0
                )
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


# ---------------------------------------------------------------------------
# 2) 🔴 CANLI satırlardan türetme
# ---------------------------------------------------------------------------


async def test_ekrandan_degistirilmis_hucre_CANLI_degeriyle_tasinir() -> None:
    """Canlıda ekrandan değiştirilmiş hücre seed'e DÖNMEZ; canlı değer taşınır."""
    database = await _create_scratch_database()
    try:
        _upgrade(ONCEKI_REVISION, database)
        conn = await _baglan(database)
        try:
            # Şef muhasebeyi `full` yapmış (seed `none`), İK bordroyu `view`e çekmiş (seed `full`),
            # muhasebe hazineyi kapatmış (seed `full`).
            await _live_cell_set(conn, "site_chief", "accounting", "full", "all")
            await _live_cell_set(conn, "hr_manager", "payroll", "view", "all")
            await _live_cell_set(conn, "accounting", "treasury", "none", "all")
            # `limited` bayrağı: saha müh. üç hücrede `all`a çekilmiş → bayrak DÜŞER;
            # muhasebe panelini `limited` yapmış → bayrak DOĞAR.
            for modul in ("dashboard", "projects", "sites"):
                await _live_cell_set(conn, "field_engineer", modul, "view", "all")
            await _live_cell_set(conn, "accounting", "dashboard", "view", "limited")
        finally:
            await conn.close()

        sonuc = _upgrade(IZN_REVISION, database)

        conn = await _baglan(database)
        try:
            sef = await _page_cells(conn, "site_chief")
            assert sef["mali.yevmiye"] == ("edit", True), "seed `none` iken canlı `full` taşınmalı"
            assert sef["mali.mizan"] == ("view", False)  # Mizan'da yazma eylemi yok → Görür
            assert sef["mali.yevmiye"] != _expected_pages("site_chief")["mali.yevmiye"]

            ik = await _page_cells(conn, "hr_manager")
            assert ik["mali.bordro"] == ("view", False), "seed `full` iken canlı `view` taşınmalı"
            assert ik["mali.bordro"] != _expected_pages("hr_manager")["mali.bordro"]

            muhasebe = await _page_cells(conn, "accounting")
            assert muhasebe["mali.hazine"] == ("none", False)
            assert muhasebe["mali.cek_odeme"] == ("none", False)

            assert await _hidden(conn, "field_engineer") == set(), "limited kalkınca bayrak düşer"
            assert await _hidden(conn, "accounting") == {"tum_tutarlar"}
            assert await _hidden(conn, "site_chief") == {"tum_tutarlar"}  # dokunulmayan rol

            # Dokunulmayan hücreler seed ile aynı kalır.
            assert (await _page_cells(conn, "patron")) == _expected_pages("patron")
        finally:
            await conn.close()

        cikti = _cikti(sonuc)
        assert "seed'den SAPAN canli hucre sayisi=7" in cikti
        for parca in (
            "site_chief:accounting canli=full/all seed=none/all",
            "hr_manager:payroll canli=view/all seed=full/all",
            "accounting:treasury canli=none/all seed=full/all",
        ):
            assert parca in cikti, parca
    finally:
        await _drop_scratch_database(database)


async def test_satiri_silinmis_hucre_seed_hucresine_duser() -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(ONCEKI_REVISION, database)
        conn = await _baglan(database)
        try:
            await _live_cell_delete(conn, "accounting", "accounting")
            await _live_cell_delete(conn, "procurement", "inventory")
        finally:
            await conn.close()

        sonuc = _upgrade(IZN_REVISION, database)

        conn = await _baglan(database)
        try:
            # Seed: muhasebe × muhasebe = full → Düzenler + Onaylar; satınalma × stok = full.
            muhasebe = await _page_cells(conn, "accounting")
            assert muhasebe["mali.yevmiye"] == ("edit", True)
            assert muhasebe["mali.mizan"] == ("view", False)
            satinalma = await _page_cells(conn, "procurement")
            assert satinalma["stok.stok_depo"] == ("edit", False)
            # Sonuç tüm seed rolleri için PAGE_MATRIX ile AYNI (silinen satır seed'e düştü).
            for role_key in NON_ADMIN_ESKI_ROLLER:
                assert await _page_cells(conn, role_key) == _expected_pages(role_key), role_key
            assert await conn.fetchval("SELECT count(*) FROM role_page_permissions") == 1300
        finally:
            await conn.close()

        cikti = _cikti(sonuc)
        assert "seed-rol (rol,modul) cifti=2" in cikti
        assert "SAPAN canli hucre sayisi=0" in cikti
    finally:
        await _drop_scratch_database(database)


async def test_ozel_rol_tasinir() -> None:
    """Ekrandan açılmış (ROLE_ORDER'da olmayan) rol de 100 hücre alır; satırsız modül Görmez."""
    database = await _create_scratch_database()
    try:
        _upgrade(ONCEKI_REVISION, database)
        conn = await _baglan(database)
        try:
            await _custom_role(
                conn,
                "ozel_taseron",
                {"inventory": ("full", "all"), "dashboard": ("view", "limited")},
            )
        finally:
            await conn.close()

        sonuc = _upgrade(IZN_REVISION, database)

        conn = await _baglan(database)
        try:
            hucreler = await _page_cells(conn, "ozel_taseron")
            assert len(hucreler) == SAYFA_SAYISI
            assert hucreler["stok.stok_depo"] == ("edit", False)
            assert hucreler["santiye.stok"] == ("edit", False)
            assert hucreler["bolum.malzeme"] == ("view", False)  # sekme: yazma eylemi yok
            assert hucreler["genel.gosterge_paneli"] == ("view", False)
            # Satırı olmayan modüllerin sayfaları Görmez (özel rolde seed yedeği = Görmez).
            assert hucreler["mali.yevmiye"] == ("none", False)
            assert hucreler["saha.puantaj"] == ("none", False)
            # Modülsüz yedi sayfa başlangıç düzeyini alır.
            assert hucreler["genel.raporlar"] == ("view", False)
            assert hucreler["ayarlar.gelistirme"] == ("none", False)
            assert await _hidden(conn, "ozel_taseron") == {"tum_tutarlar"}
            assert await conn.fetchval("SELECT count(*) FROM role_page_permissions") == 14 * 100
        finally:
            await conn.close()
        assert "ozel_taseron" in _cikti(sonuc)
    finally:
        await _drop_scratch_database(database)


async def test_yazma_surerken_migration_kilidi_BEKLER_ve_commit_edilen_degeri_okur() -> None:
    """Okuma, kilit ALINDIKTAN SONRA yapılır: açık bir yazma işlemi varken migration bekler
    (NOWAIT + yeniden deneme) ve o işlem COMMIT edince yeni değeri taşır."""
    database = await _create_scratch_database()
    try:
        _upgrade(ONCEKI_REVISION, database)
        yazar = await _baglan(database)
        try:
            tx = yazar.transaction()
            await tx.start()
            await _live_cell_set(yazar, "site_chief", "accounting", "full", "all")

            env = {**os.environ, "DATABASE_URL": _asyncpg_dsn(database)}
            surec = subprocess.Popen(
                [*ALEMBIC_CMD, "upgrade", IZN_REVISION],
                cwd=BACKEND_DIR,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                # Migration kilidi alamıyor → kısa süre sonra HÂLÂ koşuyor olmalı.
                await asyncio.sleep(2.5)
                assert surec.poll() is None, "yazma açıkken migration beklemeden geçti"
                await tx.commit()
                cikti, hata = await asyncio.to_thread(surec.communicate, None, 120)
                assert surec.returncode == 0, cikti + hata
            finally:
                if surec.poll() is None:
                    surec.wait(timeout=60)
        finally:
            await yazar.close()

        conn = await _baglan(database)
        try:
            sef = await _page_cells(conn, "site_chief")
            assert sef["mali.yevmiye"] == ("edit", True)
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


# ---------------------------------------------------------------------------
# 3) Ön koşul bekçisi (çakışan anahtar) + downgrade
# ---------------------------------------------------------------------------


async def test_ayni_anahtarli_elle_acilmis_rol_RAISE_etmez_canli_satirindan_turetilir() -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(ONCEKI_REVISION, database)
        conn = await _baglan(database)
        try:
            await _custom_role(conn, "viewer", {"inventory": ("view", "all")})
        finally:
            await conn.close()

        sonuc = _upgrade(IZN_REVISION, database)  # RAISE etmez

        conn = await _baglan(database)
        try:
            # Yinelenmedi: 'viewer' tek; öteki 5 yeni rol eklendi.
            assert await conn.fetchval("SELECT count(*) FROM roles WHERE key = 'viewer'") == 1
            assert await conn.fetchval("SELECT count(*) FROM roles") == 8 + 1 + 5
            hucreler = await _page_cells(conn, "viewer")
            # Seed 'viewer'ı DEĞİL, canlı satırı: yalnız stok Görür; gerisi Görmez (+modülsüz).
            assert hucreler["stok.stok_depo"] == ("view", False)
            assert hucreler["genel.gosterge_paneli"] == ("none", False)
            assert hucreler != _expected_pages("viewer")
        finally:
            await conn.close()
        assert "mevcut cakisan yeni-rol anahtarlari=['viewer']" in _cikti(sonuc)

        # Downgrade çakışan rolü SİLMEZ (eski hücreleri var); eklenen 5 rolü siler.
        sonuc = _alembic("downgrade", ONCEKI_REVISION, database=database)
        assert sonuc.returncode == 0, _cikti(sonuc)
        conn = await _baglan(database)
        try:
            kalan = {r[0] for r in await conn.fetch("SELECT key FROM roles")}
            assert "viewer" in kalan
            assert kalan.isdisjoint(set(YENI_ROL_ANAHTARLARI) - {"viewer"})
            assert await conn.fetchval("SELECT count(*) FROM role_permissions") == 184 + 1
        finally:
            await conn.close()
        assert "KORUNDU" in _cikti(sonuc)
    finally:
        await _drop_scratch_database(database)


async def test_downgrade_yeni_rol_atanmissa_FAIL_CLOSED_durur_sonra_tur_temiz_gecer() -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(IZN_REVISION, database)
        conn = await _baglan(database)
        try:
            rol_id = await conn.fetchval("SELECT id FROM roles WHERE key = 'cost_engineer'")
            await conn.execute(
                "INSERT INTO users (id, email, password_hash, full_name, title, role_id, status, "
                "token_version) VALUES (gen_random_uuid(), 'maliyet@izn.co', 'x', 'M', '', $1, "
                "'active', 0)",
                rol_id,
            )
        finally:
            await conn.close()

        # Atanmış kullanıcı varken geri alma DURUR (açık mesaj), hiçbir şey silinmez.
        sonuc = _alembic("downgrade", ONCEKI_REVISION, database=database)
        assert sonuc.returncode != 0
        assert "cost_engineer=1 kullanici" in _cikti(sonuc)
        conn = await _baglan(database)
        try:
            assert await _current_revision(conn) == IZN_REVISION
            assert await _table_exists(conn, "role_page_permissions")
            assert await conn.fetchval("SELECT count(*) FROM roles") == 14
            assert await conn.fetchval("SELECT count(*) FROM role_page_permissions") == 1300
            await conn.execute("DELETE FROM users WHERE email = 'maliyet@izn.co'")
        finally:
            await conn.close()

        # Kullanıcı kalkınca downgrade temiz: tablolar, enumlar, 6 rol gider.
        sonuc = _alembic("downgrade", ONCEKI_REVISION, database=database)
        assert sonuc.returncode == 0, _cikti(sonuc)
        conn = await _baglan(database)
        try:
            assert await _current_revision(conn) == ONCEKI_REVISION
            for tablo in ("role_page_permissions", "role_hidden_fields"):
                assert not await _table_exists(conn, tablo), tablo
            for tip in ("page_level", "hidden_category"):
                assert not await _type_exists(conn, tip), tip
            assert await conn.fetchval("SELECT count(*) FROM roles") == 8
            assert await conn.fetchval("SELECT count(*) FROM role_permissions") == 184
        finally:
            await conn.close()

        # İkinci upgrade patlamaz (enum DROP TYPE kanonu) ve aynı durumu kurar.
        _upgrade(IZN_REVISION, database)
        conn = await _baglan(database)
        try:
            assert await _current_revision(conn) == IZN_REVISION
            assert await conn.fetchval("SELECT count(*) FROM role_page_permissions") == 1300
            for role_key in NON_ADMIN_ESKI_ROLLER + YENI_ROL_ANAHTARLARI:
                assert await _page_cells(conn, role_key) == _expected_pages(role_key), role_key
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_kilit_tavani_asilirsa_migration_fail_closed_duser() -> None:
    """Okuma kilidini engelleyen uzun bir yazma: migration 10 sn sonra TEMİZ düşer, yarım kalmaz."""
    database = await _create_scratch_database()
    try:
        _upgrade(ONCEKI_REVISION, database)
        yazar = await _baglan(database)
        try:
            tx = yazar.transaction()
            await tx.start()
            await _live_cell_set(yazar, "site_chief", "accounting", "full", "all")
            baslangic = time.monotonic()
            sonuc = await asyncio.to_thread(_alembic, "upgrade", IZN_REVISION, database=database)
            assert time.monotonic() - baslangic >= 9.0
            assert sonuc.returncode != 0
            assert "LockCeilingExceededError" in _cikti(sonuc)
            await tx.rollback()
        finally:
            await yazar.close()
        conn = await _baglan(database)
        try:
            assert await _current_revision(conn) == ONCEKI_REVISION
            assert not await _table_exists(conn, "role_page_permissions")
            assert not await _type_exists(conn, "page_level")
            assert await conn.fetchval("SELECT count(*) FROM roles") == 8
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

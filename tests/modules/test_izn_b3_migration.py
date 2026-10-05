"""IZN-B3 — `izn_b3` migration'ı: `users.all_projects`, `project_members`, geçiş, downgrade turu.

Gerçek PG'de B2 → B3 → B2 → B3. Çakılanlar:
* ŞEMA: kolon + iki tablo + kısıtlar (UQ user+project, rol ve disiplin RESTRICT, üye/kullanıcı/proje
  CASCADE).
* GEÇİŞ (bugünkü verilerden): `all_projects` bayrağı, proje satırları → `project_members` (rol =
  ana rol; eski tablodaki yinelenen satır TEKİLLEŞİR; `all_projects` kişide proje satırı
  YAZILMAZ), global disiplin → kişinin HER ekip satırına aynı küme; hedefsiz kalan iki durum
  (all_projects kişi, ekip satırı olmayan kişi) WARNING olarak SAYILIR ve eski satırlar KALIR.
* DOWNGRADE: eski tablolar yeni gerçeğe göre yeniden kurulur; kayıp (proje başına rol / ayrı
  disiplin) WARNING; ikinci upgrade aynı durumu kurar.
* Boş canlı veri (5 kullanıcı, proje yok) hatasız geçer.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import asyncpg

from tests.modules.approvals.test_ok1a_migration import (
    _column_exists,
    _create_scratch_database,
    _current_revision,
    _drop_scratch_database,
    _table_exists,
)
from tests.modules.test_izn_b1_migration import _alembic, _baglan, _cikti, _upgrade

B2_REVISION = "671436c0b42a"
B3_REVISION = "d3a7f1c9b5e2"
MIGRATION_PATH = next(
    (Path(__file__).parents[2] / "alembic" / "versions").glob("*_izn_b3_proje_ekibi.py")
)


def test_zincir_b2nin_ustunde() -> None:
    metin = MIGRATION_PATH.read_text(encoding="utf-8")
    assert f'down_revision: str | Sequence[str] | None = "{B2_REVISION}"' in metin
    assert f'revision: str = "{B3_REVISION}"' in metin


def test_migration_app_import_etmez() -> None:
    metin = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "from app" not in metin and "import app" not in metin


async def _kullanici(conn: asyncpg.Connection, email: str, role_key: str) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO users (id, email, password_hash, full_name, title, role_id, status, "
        "token_version) VALUES (gen_random_uuid(), $1, 'x', $1, '', "
        "(SELECT id FROM roles WHERE key = $2), 'active', 0) RETURNING id",
        email,
        role_key,
    )


async def _proje(conn: asyncpg.Connection, code: str) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO projects (id, code, name, budget, progress_pct) "
        "VALUES (gen_random_uuid(), $1, $1, 0, 0) RETURNING id",
        code,
    )


async def _disiplin(conn: asyncpg.Connection, code: str) -> uuid.UUID:
    enum_degeri = await conn.fetchval(
        "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
        "WHERE t.typname = (SELECT udt_name FROM information_schema.columns "
        "WHERE table_name = 'ev_disciplines' AND column_name = 'default_contractor_type') "
        "ORDER BY e.enumsortorder LIMIT 1"
    )
    tip = await conn.fetchval(
        "SELECT udt_name FROM information_schema.columns "
        "WHERE table_name = 'ev_disciplines' AND column_name = 'default_contractor_type'"
    )
    return await conn.fetchval(
        "INSERT INTO ev_disciplines (id, code, name, color, default_contractor_type) "
        f"VALUES (gen_random_uuid(), $1, $1, '#2563EB', $2::{tip}) RETURNING id",
        code,
        enum_degeri,
    )


async def _eski_veri(conn: asyncpg.Connection) -> dict[str, uuid.UUID]:
    """Bugünkü (B2) veri: tüm-projeler kişisi, yinelenen satırlı ekip kişisi, hedefsiz disiplin."""
    ids: dict[str, uuid.UUID] = {}
    for ad, rol in (
        ("admin", "system_admin"),
        ("tum", "patron"),
        ("ekip", "site_chief"),
        ("yetim", "field_engineer"),
        ("bos", "accounting"),
    ):
        ids[ad] = await _kullanici(conn, f"{ad}@izn-b3-mig.co", rol)
    for ad in ("p1", "p2", "p3"):
        ids[ad] = await _proje(conn, ad.upper())
    ids["d1"] = await _disiplin(conn, "D1")
    ids["d2"] = await _disiplin(conn, "D2")

    async def erisim(user: str, project: str | None, tum: bool) -> None:
        await conn.execute(
            "INSERT INTO user_project_access (id, user_id, project_id, all_projects) "
            "VALUES (gen_random_uuid(), $1, $2, $3)",
            ids[user],
            ids[project] if project else None,
            tum,
        )

    await erisim("admin", None, True)
    await erisim("tum", None, True)
    await erisim("tum", "p1", False)  # all_projects kişide proje satırı: yazılmaz
    await erisim("ekip", "p1", False)
    await erisim("ekip", "p1", False)  # eski tabloda UNIQUE yoktu: yinelenen satır
    await erisim("ekip", "p2", False)

    async def disiplin(user: str, *discs: str) -> None:
        for d in discs:
            await conn.execute(
                "INSERT INTO user_disciplines (user_id, discipline_id) VALUES ($1, $2)",
                ids[user],
                ids[d],
            )

    await disiplin("ekip", "d1", "d2")
    await disiplin("tum", "d1")  # all_projects: hedefsiz
    await disiplin("yetim", "d1")  # ekip satırı yok: hedefsiz
    return ids


async def _kisit_ihlali(conn: asyncpg.Connection, kisit: str, sql: str, *args: object) -> None:
    """`sql` belirtilen kısıtı ihlal etmeli (başka bir kısıt değil)."""
    try:
        await conn.execute(sql, *args)
    except asyncpg.IntegrityConstraintViolationError as exc:
        assert kisit in str(exc), (kisit, str(exc))
        return
    raise AssertionError(f"kısıt çalışmadı ({kisit}): {sql}")


async def _uyeler(conn: asyncpg.Connection) -> dict[tuple[str, str], tuple[str, set[str]]]:
    """(kullanıcı e-postası, proje kodu) → (rol anahtarı, disiplin kodları)."""
    rows = await conn.fetch(
        "SELECT u.email, p.code, r.key, "
        "coalesce(array_agg(d.code) FILTER (WHERE d.code IS NOT NULL), '{}') AS discs "
        "FROM project_members pm JOIN users u ON u.id = pm.user_id "
        "JOIN projects p ON p.id = pm.project_id JOIN roles r ON r.id = pm.role_id "
        "LEFT JOIN project_member_disciplines pmd ON pmd.member_id = pm.id "
        "LEFT JOIN ev_disciplines d ON d.id = pmd.discipline_id "
        "GROUP BY u.email, p.code, r.key"
    )
    return {(r["email"].split("@")[0], r["code"]): (r["key"], set(r["discs"])) for r in rows}


async def test_upgrade_gecis_downgrade_turu_ve_ikinci_upgrade() -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(B2_REVISION, database)
        conn = await _baglan(database)
        try:
            await _eski_veri(conn)
            assert not await _column_exists(conn, "users", "all_projects")
        finally:
            await conn.close()

        sonuc = _upgrade(B3_REVISION, database)
        cikti = _cikti(sonuc)
        # WARNING sayımları: 6 eski satır → 2 tüm-projeler kişisi + 2 ekip satırı; 1 yinelenen
        # satır tekilleşti; 1 proje satırı (tüm-projeler kişisinde) yazılmadı.
        assert "user_project_access 6 satir -> 2 tum-projeler kullanicisi, 2 ekip satiri" in cikti
        assert "1 yinelenen satir tekillesti, 1 proje satiri tum-projeler kullanicisinda" in cikti
        assert "1 tum-projeler kullanicisinin global disiplini HEDEFSIZ" in cikti
        assert "1 kullanicinin global disiplini HEDEFSIZ" in cikti

        conn = await _baglan(database)
        try:
            assert await _current_revision(conn) == B3_REVISION
            assert await _column_exists(conn, "users", "all_projects")
            tum_projeler = {
                r["email"].split("@")[0]
                for r in await conn.fetch("SELECT email FROM users WHERE all_projects")
            }
            assert tum_projeler == {"admin", "tum"}
            # Ekip satırları: yinelenen tekilleşti, rol = ANA rol, disiplin = global küme.
            assert await _uyeler(conn) == {
                ("ekip", "P1"): ("site_chief", {"D1", "D2"}),
                ("ekip", "P2"): ("site_chief", {"D1", "D2"}),
            }
            # Eski tablolar KALIR ve DOKUNULMAZ (B6'ya kadar).
            assert await conn.fetchval("SELECT count(*) FROM user_project_access") == 6
            assert await conn.fetchval("SELECT count(*) FROM user_disciplines") == 4
        finally:
            await conn.close()

        # Kısıtlar: UQ, rol RESTRICT, disiplin RESTRICT, CASCADE'ler.
        conn = await _baglan(database)
        try:
            ekip = await conn.fetchval("SELECT id FROM users WHERE email = 'ekip@izn-b3-mig.co'")
            p1 = await conn.fetchval("SELECT id FROM projects WHERE code = 'P1'")
            p3 = await conn.fetchval("SELECT id FROM projects WHERE code = 'P3'")
            sef = await conn.fetchval("SELECT id FROM roles WHERE key = 'site_chief'")
            satinalma = await conn.fetchval("SELECT id FROM roles WHERE key = 'procurement'")
            uye_sql = (
                "INSERT INTO project_members (id, user_id, project_id, role_id) "
                "VALUES (gen_random_uuid(), $1, $2, $3)"
            )
            await _kisit_ihlali(conn, "uq_project_members_user_project", uye_sql, ekip, p1, sef)
            await conn.execute(uye_sql, ekip, p3, satinalma)  # hiçbir kullanıcının ana rolü DEĞİL
            await _kisit_ihlali(
                conn, "project_members_role_id_fkey", "DELETE FROM roles WHERE key = 'procurement'"
            )
            # Eski global satırlar kalkınca disiplini yalnız YENİ tablo tutar.
            await conn.execute("DELETE FROM user_disciplines")
            await _kisit_ihlali(
                conn,
                "project_member_disciplines_discipline_id_fkey",
                "DELETE FROM ev_disciplines WHERE code = 'D1'",
            )
            await conn.execute("DELETE FROM project_members WHERE project_id = $1", p3)
            await conn.execute("DELETE FROM projects WHERE code = 'P2'")  # CASCADE: üye + disiplin
            assert set(await _uyeler(conn)) == {("ekip", "P1")}
            assert await conn.fetchval("SELECT count(*) FROM project_member_disciplines") == 2
            await conn.execute("DELETE FROM users WHERE email = 'ekip@izn-b3-mig.co'")
            assert await conn.fetchval("SELECT count(*) FROM project_members") == 0
            assert await conn.fetchval("SELECT count(*) FROM project_member_disciplines") == 0
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_downgrade_eski_tablolari_yeni_gerceklikten_kurar_ikinci_upgrade_turetir() -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(B2_REVISION, database)
        conn = await _baglan(database)
        try:
            await _eski_veri(conn)
        finally:
            await conn.close()
        _upgrade(B3_REVISION, database)

        # Yeni dünyada bir değişiklik: ekip kişisi P2'de FARKLI rolle ve FARKLI disiplinle.
        conn = await _baglan(database)
        try:
            await conn.execute(
                "UPDATE project_members SET role_id = (SELECT id FROM roles WHERE key = "
                "'field_engineer') WHERE project_id = (SELECT id FROM projects WHERE code = 'P2')"
            )
            await conn.execute(
                "DELETE FROM project_member_disciplines WHERE member_id = (SELECT id FROM "
                "project_members WHERE project_id = (SELECT id FROM projects WHERE code='P2')) "
                "AND discipline_id = (SELECT id FROM ev_disciplines WHERE code = 'D2')"
            )
            await conn.execute(
                "UPDATE users SET all_projects = true WHERE email = 'bos@izn-b3-mig.co'"
            )
            ilk_durum = await _uyeler(conn)
        finally:
            await conn.close()

        sonuc = _alembic("downgrade", B2_REVISION, database=database)
        assert sonuc.returncode == 0, _cikti(sonuc)
        cikti = _cikti(sonuc)
        assert (
            "1 ekip satirinin proje rolu" in cikti and "1 kullanicinin proje basina AYRI" in cikti
        )
        conn = await _baglan(database)
        try:
            assert await _current_revision(conn) == B2_REVISION
            assert not await _column_exists(conn, "users", "all_projects")
            for tablo in ("project_members", "project_member_disciplines"):
                assert not await _table_exists(conn, tablo), tablo
            eski = {
                (r["email"].split("@")[0], r["code"], r["all_projects"])
                for r in await conn.fetch(
                    "SELECT u.email, p.code, a.all_projects FROM user_project_access a "
                    "JOIN users u ON u.id = a.user_id LEFT JOIN projects p ON p.id = a.project_id"
                )
            }
            assert eski == {
                ("admin", None, True),
                ("tum", None, True),
                ("bos", None, True),  # yeni dünyada işaretlendi
                ("ekip", "P1", False),
                ("ekip", "P2", False),  # yinelenen satır geri GELMEDİ (tekil)
            }
            disiplinler = {
                (r["email"].split("@")[0], r["code"])
                for r in await conn.fetch(
                    "SELECT u.email, d.code FROM user_disciplines ud "
                    "JOIN users u ON u.id = ud.user_id JOIN ev_disciplines d "
                    "ON d.id = ud.discipline_id"
                )
            }
            # ekip: projelerdeki disiplinlerin BİRLEŞİMİ (P1: D1,D2 ∪ P2: D1); tum/yetim: aynen.
            assert disiplinler == {("ekip", "D1"), ("ekip", "D2"), ("tum", "D1"), ("yetim", "D1")}
        finally:
            await conn.close()

        _upgrade(B3_REVISION, database)
        conn = await _baglan(database)
        try:
            assert await _current_revision(conn) == B3_REVISION
            # İkinci upgrade eski tablolardan yeniden türetir: P2 rolü ana role döner.
            assert await _uyeler(conn) == {
                ("ekip", "P1"): ("site_chief", {"D1", "D2"}),
                ("ekip", "P2"): ("site_chief", {"D1", "D2"}),
            }
            assert ilk_durum != await _uyeler(conn)
            assert {
                r["email"].split("@")[0]
                for r in await conn.fetch("SELECT email FROM users WHERE all_projects")
            } == {"admin", "tum", "bos"}
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_bos_canli_veri_5_kullanici_proje_yok_hatasiz_gecer_ve_geri_doner() -> None:
    """Canlı Postgres-v3: 5 kullanıcı, proje YOK, yalnız admin'in `all_projects` satırı."""
    database = await _create_scratch_database()
    try:
        _upgrade(B2_REVISION, database)
        conn = await _baglan(database)
        try:
            ids = {}
            for ad, rol in (
                ("admin", "system_admin"),
                ("patron", "patron"),
                ("muh", "accounting"),
                ("pm", "project_manager"),
                ("sef", "site_chief"),
            ):
                ids[ad] = await _kullanici(conn, f"{ad}@izn-b3-bos.co", rol)
            await conn.execute(
                "INSERT INTO user_project_access (id, user_id, project_id, all_projects) "
                "VALUES (gen_random_uuid(), $1, NULL, true)",
                ids["admin"],
            )
        finally:
            await conn.close()

        _upgrade(B3_REVISION, database)
        conn = await _baglan(database)
        try:
            assert await conn.fetchval("SELECT count(*) FROM project_members") == 0
            assert await conn.fetchval("SELECT count(*) FROM project_member_disciplines") == 0
            assert await conn.fetchval("SELECT email FROM users WHERE all_projects") == (
                "admin@izn-b3-bos.co"
            )
        finally:
            await conn.close()
        sonuc = _alembic("downgrade", B2_REVISION, database=database)
        assert sonuc.returncode == 0, _cikti(sonuc)
        conn = await _baglan(database)
        try:
            assert await conn.fetchval("SELECT count(*) FROM user_project_access") == 1
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

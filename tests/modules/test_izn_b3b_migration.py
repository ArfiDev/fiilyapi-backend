"""IZN-B3b — `izn_b3b` migration'ı: `user_approval_roles` söküm, WARNING sayımı, downgrade.

Gerçek PG'de B3 → B3b → B3 → B3b:
* UPGRADE: atamalar SAYILIR (toplam · ana rolle uyuşmayan · "Tüm projeler" olmayan sahip) ve
  WARNING basılır; tablo DÜŞER; `approval_role` enum tipi KALIR (`approval_steps` kullanır);
  zincir adımları dokunulmaz.
* DOWNGRADE: tablo yeniden açılır, her kullanıcıya ANA rol anahtarı enum değerlerindense o satır
  yazılır (yaklaşık geri dönüş); satır ALMAYAN kullanıcı sayısı WARNING.
* Boş canlı veri (5 kullanıcı, atama yok) hatasız geçer.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import asyncpg

from tests.modules.approvals.test_ok1a_migration import (
    _create_scratch_database,
    _current_revision,
    _drop_scratch_database,
    _table_exists,
)
from tests.modules.test_izn_b1_migration import _alembic, _baglan, _cikti, _upgrade

B3_REVISION = "d3a7f1c9b5e2"
B3B_REVISION = "e8b4c2d6f9a1"
MIGRATION_PATH = next(
    (Path(__file__).parents[2] / "alembic" / "versions").glob("*_izn_b3b_onay_rolu_*.py")
)


def test_zincir_b3un_ustunde() -> None:
    metin = MIGRATION_PATH.read_text(encoding="utf-8")
    assert f'down_revision: str | Sequence[str] | None = "{B3_REVISION}"' in metin
    assert f'revision: str = "{B3B_REVISION}"' in metin


def test_migration_app_import_etmez() -> None:
    metin = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "from app" not in metin and "import app" not in metin


async def _kullanici(
    conn: asyncpg.Connection, email: str, role_key: str, *, tum: bool = False
) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO users (id, email, password_hash, full_name, title, role_id, status, "
        "token_version, all_projects) VALUES (gen_random_uuid(), $1, 'x', $1, '', "
        "(SELECT id FROM roles WHERE key = $2), 'active', 0, $3) RETURNING id",
        email,
        role_key,
        tum,
    )


async def _ata(conn: asyncpg.Connection, user: uuid.UUID, rol: str) -> None:
    await conn.execute(
        "INSERT INTO user_approval_roles (id, user_id, approval_role) "
        "VALUES (gen_random_uuid(), $1, $2::approval_role)",
        user,
        rol,
    )


async def _eski_veri(conn: asyncpg.Connection) -> dict[str, uuid.UUID]:
    """B3 dünyası: ana rolüyle uyuşan / uyuşmayan atamalar, tüm-projeler olan / olmayan sahip."""
    ids = {
        "muh": await _kullanici(conn, "muh@izn-b3b-mig.co", "accounting", tum=True),
        "sef": await _kullanici(conn, "sef@izn-b3b-mig.co", "site_chief"),
        "karma": await _kullanici(conn, "karma@izn-b3b-mig.co", "field_engineer"),
        "yok": await _kullanici(conn, "yok@izn-b3b-mig.co", "viewer"),
    }
    await _ata(conn, ids["muh"], "accounting")  # uyuşur, tüm-projeler
    await _ata(conn, ids["sef"], "site_chief")  # uyuşur, tüm-projeler DEĞİL
    await _ata(conn, ids["karma"], "procurement")  # uyuşmaz, tüm-projeler DEĞİL
    await _ata(conn, ids["karma"], "patron")  # uyuşmaz, tüm-projeler DEĞİL
    return ids


async def test_upgrade_sayar_uyarir_tabloyu_dusurur_enum_kalir_downgrade_yaklasik_doner() -> None:
    database = await _create_scratch_database()
    try:
        _upgrade(B3_REVISION, database)
        conn = await _baglan(database)
        try:
            await _eski_veri(conn)
            assert await _table_exists(conn, "user_approval_roles")
        finally:
            await conn.close()

        sonuc = _upgrade(B3B_REVISION, database)
        cikti = _cikti(sonuc)
        # 4 atama · 3'ü ana rolle uyuşmuyor? muh/sef uyuşur → yalnız karma'nın 2'si uyuşmaz.
        assert "4 atama dusuyor; 2 atama kullanicinin ANA rolu ile uyusmuyor" in cikti
        assert "3 atamanin sahibi 'Tum projeler' DEGIL" in cikti

        conn = await _baglan(database)
        try:
            assert await _current_revision(conn) == B3B_REVISION
            assert not await _table_exists(conn, "user_approval_roles")
            # Enum tipi KALIR: `approval_steps.approval_role` onu kullanır.
            assert await conn.fetchval("SELECT 1 FROM pg_type WHERE typname = 'approval_role'")
            assert await _table_exists(conn, "approval_steps")
        finally:
            await conn.close()

        sonuc = _alembic("downgrade", B3_REVISION, database=database)
        assert sonuc.returncode == 0, _cikti(sonuc)
        cikti = _cikti(sonuc)
        # Ana rol anahtarı ∈ enum: accounting + site_chief (+ seed kullanıcıları yok) = 2 satır;
        # karma (field_engineer) ve yok (viewer) satır ALMAZ.
        assert "2 atama yazildi; 2 kullanici satir ALMADI" in cikti
        conn = await _baglan(database)
        try:
            assert await _current_revision(conn) == B3_REVISION
            satirlar = {
                (r["email"].split("@")[0], r["approval_role"])
                for r in await conn.fetch(
                    "SELECT u.email, a.approval_role::text FROM user_approval_roles a "
                    "JOIN users u ON u.id = a.user_id"
                )
            }
            assert satirlar == {("muh", "accounting"), ("sef", "site_chief")}
            # Tablo kısıtları geri geldi: UQ + FK CASCADE.
            muh = await conn.fetchval("SELECT id FROM users WHERE email = 'muh@izn-b3b-mig.co'")
            try:
                await _ata(conn, muh, "accounting")
            except asyncpg.UniqueViolationError as exc:
                assert "uq_user_approval_roles_user_role" in str(exc)
            else:
                raise AssertionError("UQ geri gelmedi")
            await conn.execute("DELETE FROM users WHERE id = $1", muh)
            assert await conn.fetchval("SELECT count(*) FROM user_approval_roles") == 1
        finally:
            await conn.close()

        _upgrade(B3B_REVISION, database)
        conn = await _baglan(database)
        try:
            assert await _current_revision(conn) == B3B_REVISION
            assert not await _table_exists(conn, "user_approval_roles")
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_bos_canli_veri_atama_yok_hatasiz_gecer_ve_geri_doner() -> None:
    """Canlı Postgres-v3: kullanıcılar var, atama ve proje YOK."""
    database = await _create_scratch_database()
    try:
        _upgrade(B3_REVISION, database)
        conn = await _baglan(database)
        try:
            for ad, rol in (("a", "system_admin"), ("b", "patron"), ("c", "accounting")):
                await _kullanici(conn, f"{ad}@izn-b3b-bos.co", rol)
        finally:
            await conn.close()

        sonuc = _upgrade(B3B_REVISION, database)
        assert "0 atama dusuyor; 0 atama kullanicinin ANA rolu ile uyusmuyor" in _cikti(sonuc)
        sonuc = _alembic("downgrade", B3_REVISION, database=database)
        assert sonuc.returncode == 0, _cikti(sonuc)
        # patron + accounting ana rolü enum değeri → 2 satır; system_admin satır almaz.
        assert "2 atama yazildi; 1 kullanici satir ALMADI" in _cikti(sonuc)
    finally:
        await _drop_scratch_database(database)

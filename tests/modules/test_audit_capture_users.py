"""Denetim yakalama: auth login + users uclari (plan Task 2).

Her test "islem sonrasi TAM OLARAK bir denetim satiri" kuralini dogrular; negatif
testler (basarisiz login, GET uclari, 403) satir olusmadigini kilitler.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditAction, AuditLog
from app.modules.roles.models import Role

_IP = "203.0.113.42"
_HEADERS = {"x-forwarded-for": _IP}


async def _login(client, user_factory, role_key: str) -> str:
    await user_factory(email=f"{role_key}@t.co", password="parola1234", role_key=role_key)
    resp = await client.post(
        "/auth/login",
        json={"email": f"{role_key}@t.co", "password": "parola1234"},
        headers=_HEADERS,
    )
    return resp.json()["access_token"]


async def _auth(client, user_factory, role_key: str) -> dict[str, str]:
    token = await _login(client, user_factory, role_key)
    return {"Authorization": f"Bearer {token}", **_HEADERS}


async def _rows(session: AsyncSession, action: AuditAction | None = None) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.occurred_at)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list((await session.execute(stmt)).scalars().all())


async def _role_id(session: AsyncSession, key: str) -> str:
    return str((await session.execute(select(Role).where(Role.key == key))).scalar_one().id)


async def test_basarili_login_denetim_satiri_yazar(client, user_factory, seeded_db):
    user = await user_factory(email="giris@t.co", password="parola1234", role_key="accounting")

    resp = await client.post(
        "/auth/login",
        json={"email": "giris@t.co", "password": "parola1234"},
        headers=_HEADERS,
    )
    assert resp.status_code == 200

    rows = await _rows(seeded_db)
    assert len(rows) == 1
    assert rows[0].action is AuditAction.login
    assert rows[0].detail == "Sisteme giriş yapıldı"
    assert rows[0].actor_user_id == user.id
    assert str(rows[0].ip_address) == _IP


async def test_basarisiz_login_denetim_satiri_yazmaz(client, user_factory, seeded_db):
    await user_factory(email="giris@t.co", password="parola1234", role_key="accounting")

    resp = await client.post(
        "/auth/login",
        json={"email": "giris@t.co", "password": "yanlisParola"},
        headers=_HEADERS,
    )
    assert resp.status_code == 401

    assert await _rows(seeded_db) == []


async def test_kullanici_olusturma_denetim_satiri_yazar(client, user_factory, seeded_db):
    headers = await _auth(client, user_factory, "system_admin")
    rid = await _role_id(seeded_db, "accounting")

    resp = await client.post(
        "/users",
        json={
            "email": "yeni@t.co",
            "password": "parola1234",
            "full_name": "Yeni Kullanıcı",
            "role_id": rid,
        },
        headers=headers,
    )
    assert resp.status_code == 201

    rows = await _rows(seeded_db, AuditAction.create)
    assert len(rows) == 1
    assert rows[0].detail == "Kullanıcı oluşturuldu: Yeni Kullanıcı · Muhasebe"
    assert str(rows[0].ip_address) == _IP
    assert "parola1234" not in rows[0].detail


async def test_kullanici_guncelleme_denetim_satiri_yazar(client, user_factory, seeded_db):
    headers = await _auth(client, user_factory, "system_admin")
    target = await user_factory(
        email="hedef@t.co", password="parola1234", role_key="accounting", full_name="Eski Ad"
    )

    resp = await client.patch(
        f"/users/{target.id}",
        json={"full_name": "Yeni Ad"},
        headers=headers,
    )
    assert resp.status_code == 200

    rows = await _rows(seeded_db, AuditAction.update)
    assert len(rows) == 1
    assert rows[0].detail == "Kullanıcı güncellendi: Yeni Ad"


async def test_parola_sifirlama_denetim_satiri_parolayi_icermez(client, user_factory, seeded_db):
    headers = await _auth(client, user_factory, "system_admin")
    target = await user_factory(
        email="hedef@t.co", password="parola1234", role_key="accounting", full_name="Hedef Kişi"
    )
    new_password = "cokGizliParola9"

    resp = await client.patch(
        f"/users/{target.id}/password",
        json={"new_password": new_password},
        headers=headers,
    )
    assert resp.status_code == 204

    rows = await _rows(seeded_db, AuditAction.update)
    assert len(rows) == 1
    assert rows[0].detail == "Kullanıcı parolası sıfırlandı: Hedef Kişi"
    # Gizli deger sizintisi yasagi (plan §Yanit govdesi) — acik regresyon kilidi.
    assert new_password not in rows[0].detail
    assert rows[0].actor_user_id is not None


async def test_kullanici_silme_denetim_satiri_silinen_adi_icerir(client, user_factory, seeded_db):
    headers = await _auth(client, user_factory, "system_admin")
    target = await user_factory(
        email="silinecek@t.co",
        password="parola1234",
        role_key="accounting",
        full_name="Silinecek Kişi",
    )

    resp = await client.delete(f"/users/{target.id}", headers=headers)
    assert resp.status_code == 204

    rows = await _rows(seeded_db, AuditAction.delete)
    assert len(rows) == 1
    assert rows[0].detail == "Kullanıcı silindi: Silinecek Kişi"


async def test_erisim_guncelleme_denetim_satiri_yazar(
    client, user_factory, seeded_db, project_factory
):
    headers = await _auth(client, user_factory, "system_admin")
    target = await user_factory(
        email="hedef@t.co", password="parola1234", role_key="accounting", full_name="Erişim Kişi"
    )
    project = await project_factory(code="PRJ-AUDIT-1")
    rid = await _role_id(seeded_db, "accounting")

    resp = await client.put(
        f"/users/{target.id}/access",
        json={
            "role_id": rid,
            "all_projects": False,
            "projects": [{"project_id": str(project.id), "role_id": rid, "discipline_ids": []}],
        },
        headers=headers,
    )
    assert resp.status_code == 200

    rows = await _rows(seeded_db, AuditAction.update)
    assert len(rows) == 1
    assert rows[0].detail == (
        "Kullanıcı erişimi güncellendi: Erişim Kişi · ana rol Muhasebe · 1 projede ekip"
    )


async def test_get_uclari_denetim_satiri_yazmaz(client, user_factory, seeded_db):
    headers = await _auth(client, user_factory, "system_admin")

    assert (await client.get("/users", headers=headers)).status_code == 200
    assert (await client.get("/auth/me", headers=headers)).status_code == 200

    # Yalnizca yukaridaki login satiri kalmali; okuma uclari denetim uretmez.
    rows = await _rows(seeded_db)
    assert [r.action for r in rows] == [AuditAction.login]


async def test_yetkisiz_istek_denetim_satiri_yazmaz(client, user_factory, seeded_db):
    headers = await _auth(client, user_factory, "patron")  # user_management = none
    rid = await _role_id(seeded_db, "accounting")

    resp = await client.post(
        "/users",
        json={"email": "z@t.co", "password": "parola1234", "full_name": "Z", "role_id": rid},
        headers=headers,
    )
    assert resp.status_code == 403

    assert await _rows(seeded_db, AuditAction.create) == []

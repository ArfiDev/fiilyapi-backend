from sqlalchemy import select

from app.core.access import AccessLevel
from app.modules.roles.models import Module, Role, RolePermission
from tests._legacy_permission_yardimcisi import sync_page_cells


async def _login(client, user_factory, role_key: str) -> str:
    await user_factory(email=f"{role_key}@t.co", password="parola1234", role_key=role_key)
    resp = await client.post(
        "/auth/login", json={"email": f"{role_key}@t.co", "password": "parola1234"}
    )
    return resp.json()["access_token"]


async def _role_id(session, key: str):
    return str((await session.execute(select(Role).where(Role.key == key))).scalar_one().id)


async def test_create_and_list_user_as_admin(client, user_factory, seeded_db):
    token = await _login(client, user_factory, "system_admin")
    rid = await _role_id(seeded_db, "accounting")
    resp = await client.post(
        "/users",
        json={
            "email": "yeni@t.co",
            "password": "parola1234",
            "full_name": "Yeni Kullanıcı",
            "role_id": rid,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "yeni@t.co"
    assert "password" not in body and "password_hash" not in body
    assert "last_login_at" in body

    listing = await client.get("/users", headers={"Authorization": f"Bearer {token}"})
    assert listing.status_code == 200
    assert any(u["email"] == "yeni@t.co" for u in listing.json()["items"])
    assert all("last_login_at" in u for u in listing.json()["items"])


async def test_create_user_forbidden_for_non_admin(client, user_factory, seeded_db):
    token = await _login(client, user_factory, "patron")  # user_management=none
    rid = await _role_id(seeded_db, "accounting")
    resp = await client.post(
        "/users",
        json={"email": "z@t.co", "password": "parola1234", "full_name": "Z", "role_id": rid},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_reset_password_admin_only(client, user_factory, seeded_db):
    admin_token = await _login(client, user_factory, "system_admin")
    target = await user_factory(email="t@t.co", password="parola1234", role_key="site_chief")
    resp = await client.patch(
        f"/users/{target.id}/password",
        json={"new_password": "yeniParola9"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 204


async def test_delete_user_admin_only(client, user_factory):
    admin_token = await _login(client, user_factory, "system_admin")
    target = await user_factory(email="d@t.co", password="parola1234", role_key="accounting")
    resp = await client.delete(
        f"/users/{target.id}", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 204


# --- 🔴 `admin_only` ADININ İKİNCİ YARISI (kayıt 451) ------------------------
#
# Yukarıdaki iki test `…_admin_only` diye ADLANDIRILMIŞ ama YALNIZ 204 yolunu
# doğruluyordu: admin OLMAYAN bir aktörle 403 alındığına dair hiçbir iddia
# yoktu. Yani kapı (`require_permission("user_management", AccessLevel.admin)`,
# router.py:119 ve :143) sökülse bile ikisi de YEŞİL kalırdı — adın vaat ettiği
# bekçi hiç koşmuyordu. Sahte-yeşilin ders kitabı hâli: TEST ADI BEKÇİ DEĞİLDİR.
#
# `patron` bilinçli seçildi: matriste `user_management` YALNIZ `system_admin`de
# `_A`dır, kalan yedi rolde `_N` (seed_data.py:206).


async def test_reset_password_admin_OLMAYANA_403(client, user_factory, seeded_db):
    token = await _login(client, user_factory, "patron")  # user_management=none
    target = await user_factory(email="t403@t.co", password="parola1234", role_key="site_chief")
    resp = await client.patch(
        f"/users/{target.id}/password",
        json={"new_password": "yeniParola9"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_delete_user_admin_OLMAYANA_403(client, user_factory, seeded_db):
    token = await _login(client, user_factory, "patron")  # user_management=none
    target = await user_factory(email="d403@t.co", password="parola1234", role_key="accounting")
    resp = await client.delete(f"/users/{target.id}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


# 🔴 Yukarıdaki iki 403 testi KAPININ VARLIĞINI kanıtlar ama SEVİYESİNİ değil.
# ÖLÇÜLDÜ: kapı `admin`den `view`a düşürülünce ikisi de YEŞİL kalıyor, çünkü
# `patron`un seviyesi `none`dur ve HER eşikte reddedilir. Yani `admin_only`
# adının vaadi hâlâ hak edilmemiş olurdu. Aşağıdaki ikili tam da o aralığı
# hedefler: seviyesi `full` olan — yani yazma yetkisi OLAN ama yönetici
# OLMAYAN — bir aktör de reddedilmelidir. Seed matrisinde `user_management`
# yalnız `_A` ya da `_N` taşır (seed_data.py:206), bu yüzden ara seviye
# testte AÇIKÇA kurulur (equipment IDOR deseni).


async def _seviye_ver(session, role_key: str, level: AccessLevel) -> None:
    role_id = (await session.execute(select(Role.id).where(Role.key == role_key))).scalar_one()
    module_id = (
        await session.execute(select(Module.id).where(Module.key == "user_management"))
    ).scalar_one()
    permission = (
        await session.execute(
            select(RolePermission).where(
                RolePermission.role_id == role_id, RolePermission.module_id == module_id
            )
        )
    ).scalar_one()
    permission.access_level = level
    await session.flush()
    await sync_page_cells(session, permission.role_id)


async def test_reset_password_FULL_seviyesine_de_403(client, user_factory, seeded_db):
    await _seviye_ver(seeded_db, "patron", AccessLevel.full)
    token = await _login(client, user_factory, "patron")
    target = await user_factory(email="tfull@t.co", password="parola1234", role_key="site_chief")
    resp = await client.patch(
        f"/users/{target.id}/password",
        json={"new_password": "yeniParola9"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, "kapı `admin` DEĞİL daha düşük bir eşikte olabilir"


async def test_delete_user_FULL_seviyesine_de_403(client, user_factory, seeded_db):
    await _seviye_ver(seeded_db, "patron", AccessLevel.full)
    token = await _login(client, user_factory, "patron")
    target = await user_factory(email="dfull@t.co", password="parola1234", role_key="accounting")
    resp = await client.delete(f"/users/{target.id}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403, "kapı `admin` DEĞİL daha düşük bir eşikte olabilir"


async def test_list_users_pagination(client, user_factory, seeded_db):
    token = await _login(client, user_factory, "system_admin")
    resp = await client.get("/users?limit=1&offset=0", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 1 and body["offset"] == 0
    assert len(body["items"]) <= 1


def test_openapi_documents_error_responses():
    from app.main import app

    schema = app.openapi()
    responses = schema["paths"]["/users"]["get"]["responses"]
    assert "403" in responses

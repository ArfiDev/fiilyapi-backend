from sqlalchemy import select

from app.modules.roles.models import Role


async def _login(client, user_factory, role_key: str) -> str:
    await user_factory(email=f"{role_key}@t.co", password="parola1234", role_key=role_key)
    resp = await client.post(
        "/auth/login", json={"email": f"{role_key}@t.co", "password": "parola1234"}
    )
    return resp.json()["access_token"]


async def _rid(session, key):
    return str((await session.execute(select(Role).where(Role.key == key))).scalar_one().id)


async def test_list_roles_and_modules(client, user_factory):
    token = await _login(client, user_factory, "system_admin")
    h = {"Authorization": f"Bearer {token}"}
    roles = await client.get("/roles", headers=h)
    assert roles.status_code == 200 and len(roles.json()) == 8
    modules = await client.get("/modules", headers=h)
    assert modules.status_code == 200 and len(modules.json()) == 23  # PLN-B1


async def test_create_and_delete_custom_role(client, user_factory):
    token = await _login(client, user_factory, "system_admin")
    h = {"Authorization": f"Bearer {token}"}
    created = await client.post(
        "/roles",
        json={"key": "saha_amiri", "name": "Saha Amiri", "emoji": "🚧", "description": ""},
        headers=h,
    )
    assert created.status_code == 201
    new_id = created.json()["id"]
    deleted = await client.delete(f"/roles/{new_id}", headers=h)
    assert deleted.status_code == 204


async def test_roles_forbidden_for_non_admin(client, user_factory):
    token = await _login(client, user_factory, "patron")
    resp = await client.get("/roles", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


# IZN-B2: `PUT /roles/{id}/permissions/{module}` 410 oldu; modül hücresi yazma kuralları
# (kapsam, kablolu modül, maskeleyen kapsam + yazan seviye) servis düzeyinde
# `tests/modules/test_role_service.py` ile çakılır;
# 410 davranışı `tests/modules/test_izn_b2_roles_api.py` içindedir.

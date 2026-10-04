from sqlalchemy import select

from app.core.access import AccessLevel, Scope
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.projects.models import Employer
from app.modules.roles.models import Module, Role, RolePermission
from tests._legacy_permission_yardimcisi import sync_page_cells


async def _set_permission(
    session, role_key: str, module_key: str, level: AccessLevel, scope: Scope = Scope.all
) -> None:
    role_id = (await session.execute(select(Role.id).where(Role.key == role_key))).scalar_one()
    module_id = (
        await session.execute(select(Module.id).where(Module.key == module_key))
    ).scalar_one()
    permission = (
        await session.execute(
            select(RolePermission).where(
                RolePermission.role_id == role_id, RolePermission.module_id == module_id
            )
        )
    ).scalar_one()
    permission.access_level = level
    permission.scope = scope
    await session.flush()
    await sync_page_cells(session, permission.role_id)


async def _login(client, user_factory, role_key: str) -> str:
    await user_factory(email=f"{role_key}@t.co", password="parola1234", role_key=role_key)
    resp = await client.post(
        "/auth/login", json={"email": f"{role_key}@t.co", "password": "parola1234"}
    )
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_list_employers_unauthenticated(client):
    assert (await client.get("/employers")).status_code == 401


async def test_list_employers_forbidden_without_projects_view(client, user_factory):
    """procurement seed'de projects=none tasir -> GET /employers 403."""
    token = await _login(client, user_factory, "procurement")
    assert (await client.get("/employers", headers=_auth(token))).status_code == 403


async def test_create_employer_201(client, db_session, user_factory):
    token = await _login(client, user_factory, "system_admin")
    resp = await client.post(
        "/employers",
        json={"name": "Ankara Yapı A.Ş.", "tax_number": "1234567890", "contact_person": "A. Veli"},
        headers=_auth(token),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Ankara Yapı A.Ş."
    assert body["tax_number"] == "1234567890"
    assert body["is_active"] is True

    audit_rows = (
        (await db_session.execute(select(AuditLog).where(AuditLog.action == AuditAction.create)))
        .scalars()
        .all()
    )
    assert any("Ankara Yapı" in row.detail for row in audit_rows)


async def test_create_employer_duplicate_tax_number_409(client, db_session, user_factory):
    db_session.add(Employer(name="Var Olan", tax_number="1112223334"))
    await db_session.flush()
    token = await _login(client, user_factory, "system_admin")

    resp = await client.post(
        "/employers",
        json={"name": "Kopya", "tax_number": "1112223334"},
        headers=_auth(token),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Bu VKN ile kayıtlı bir işveren zaten var."


async def test_create_multiple_null_tax_numbers_allowed(client, user_factory):
    token = await _login(client, user_factory, "system_admin")
    first = await client.post("/employers", json={"name": "Firma A"}, headers=_auth(token))
    second = await client.post("/employers", json={"name": "Firma B"}, headers=_auth(token))
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["tax_number"] is None


async def test_create_employer_invalid_tax_number_422(client, user_factory):
    token = await _login(client, user_factory, "system_admin")
    resp = await client.post(
        "/employers",
        json={"name": "Firma", "tax_number": "12"},
        headers=_auth(token),
    )
    assert resp.status_code == 422
    assert "VKN 10 veya 11 haneli rakam olmalıdır." in resp.text


async def test_create_employer_forbidden_for_full_not_admin(client, db_session, user_factory):
    """POST /employers admin ister (POST /projects ile ayni seviye). full YETMEZ."""
    await _set_permission(db_session, "patron", "projects", AccessLevel.full)
    full_token = await _login(client, user_factory, "patron")
    forbidden = await client.post("/employers", json={"name": "X"}, headers=_auth(full_token))
    assert forbidden.status_code == 403

    admin_token = await _login(client, user_factory, "system_admin")
    allowed = await client.post("/employers", json={"name": "Y"}, headers=_auth(admin_token))
    assert allowed.status_code == 201


async def test_list_employers_q_and_active_only(client, db_session, user_factory):
    db_session.add(Employer(name="Ankara Yapı"))
    db_session.add(Employer(name="İzmir İnşaat"))
    db_session.add(Employer(name="Pasif Firma", is_active=False))
    await db_session.flush()
    token = await _login(client, user_factory, "system_admin")

    # active_only varsayilan true -> pasif gizli, ORDER BY name.
    all_active = await client.get("/employers", headers=_auth(token))
    names = [e["name"] for e in all_active.json()["items"]]
    assert names == ["Ankara Yapı", "İzmir İnşaat"]

    # q ada gore ILIKE.
    filtered = await client.get("/employers?q=izmir", headers=_auth(token))
    assert [e["name"] for e in filtered.json()["items"]] == ["İzmir İnşaat"]

    # active_only=false -> pasif de gelir.
    with_inactive = await client.get("/employers?active_only=false", headers=_auth(token))
    assert "Pasif Firma" in [e["name"] for e in with_inactive.json()["items"]]

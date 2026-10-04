import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.db import get_db
from app.core.permissions import require_pages
from app.modules.roles.models import Role


@pytest.fixture
def guarded_app(db_session):
    """require_permission ile korunan tek uçlu bir test uygulaması."""
    test_app = FastAPI()

    # IZN-B2: onay eylemi uçları sayfa ONAYLAR bayrağından geçer (`require_permission(…,
    # approve)` hiçbir bayrağa bağlı değildir). Muhasebe/PM geçer, Şef (draft) geçemez.
    @test_app.get(
        "/korumali",
        dependencies=[
            require_pages(
                ("mali.hakedis_isveren", "proje.isveren_hakedis", "santiye.hakedisler"), "approve"
            )
        ],
    )
    async def korumali() -> dict[str, bool]:
        return {"ok": True}

    async def _override_get_db():
        yield db_session

    test_app.dependency_overrides[get_db] = _override_get_db
    return test_app


@pytest.fixture
async def guarded_client(guarded_app):
    transport = ASGITransport(app=guarded_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def _token_for(client, email: str) -> str:
    login = await client.post("/auth/login", json={"email": email, "password": "parola"})
    return login.json()["access_token"]


async def test_role_with_sufficient_level_is_allowed(
    guarded_client, client, seeded_db, user_factory
):
    """Muhasebe hakedişte approve seviyesinde — geçmeli."""
    await user_factory(email="muhasebe@fiil.com", password="parola", role_key="accounting")
    token = await _token_for(client, "muhasebe@fiil.com")

    response = await guarded_client.get("/korumali", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


async def test_site_chief_cannot_approve_progress_payments(
    guarded_client, client, seeded_db, user_factory
):
    """NEGATİF: Şantiye Şefi hakedişte yalnızca draft seviyesinde — onaylayamaz."""
    await user_factory(email="sef@fiil.com", password="parola", role_key="site_chief")
    token = await _token_for(client, "sef@fiil.com")

    response = await guarded_client.get("/korumali", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403


async def test_hr_manager_has_no_access_to_progress_payments(
    guarded_client, client, seeded_db, user_factory
):
    """NEGATİF: İK Müdürü hakedişe hiç giremez."""
    await user_factory(email="ik@fiil.com", password="parola", role_key="hr_manager")
    token = await _token_for(client, "ik@fiil.com")

    response = await guarded_client.get("/korumali", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403


async def test_unauthenticated_request_is_rejected(guarded_client):
    response = await guarded_client.get("/korumali")
    assert response.status_code == 401


async def test_system_admin_passes_every_gate(guarded_client, client, seeded_db, user_factory):
    await user_factory(email="admin@fiil.com", password="parola", role_key="system_admin")
    token = await _token_for(client, "admin@fiil.com")

    response = await guarded_client.get("/korumali", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


async def test_role_with_no_permission_row_is_denied(
    guarded_client, client, seeded_db, user_factory
):
    """NEGATİF: (rol, modül) için hiç RolePermission satırı yoksa erişim reddedilmeli.

    `seeded_db` yalnızca 8 kanonik rol için 14x8=112 hücrelik matrisi doldurur; bu test için
    kasıtlı olarak matrisin DIŞINDA yeni bir rol oluşturuyoruz, dolayısıyla `progress_payments`
    modülü için hiçbir izin satırı yok. `require_permission` "varsayılan kapalı" olmalı: satır
    yoksa `permission is None` dalı 403 üretmeli — `permission.access_level`'e hiç erişmeden.
    Bu, seviyesi `none` olan bir satırın reddedilmesini test eden
    `test_hr_manager_has_no_access_to_progress_payments`'tan farklı bir kod yolu.
    """
    no_permission_role = Role(
        key="test_no_permissions",
        name="Izinsiz Rol",
        emoji="",
        description="Test icin: hicbir modulde izin satiri olmayan rol.",
        is_system=False,
    )
    seeded_db.add(no_permission_role)
    await seeded_db.flush()

    await user_factory(email="izinsiz@fiil.com", password="parola", role_key="test_no_permissions")
    token = await _token_for(client, "izinsiz@fiil.com")

    response = await guarded_client.get("/korumali", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403

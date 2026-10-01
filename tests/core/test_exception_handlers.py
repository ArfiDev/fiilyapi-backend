import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import DomainError, PermissionLockedError
from app.core.exception_handlers import register_exception_handlers


@pytest.fixture
def handler_app():
    """DomainError alt sınıflarını fırlatan iki uçlu, korkuluklu bir test uygulaması."""
    test_app = FastAPI()
    register_exception_handlers(test_app)

    @test_app.get("/kilitli")
    async def kilitli() -> None:
        raise PermissionLockedError("Sistem Yöneticisi rolünün izinleri değiştirilemez")

    @test_app.get("/alan-hatasi")
    async def alan_hatasi() -> None:
        raise DomainError("Alan kuralı ihlal edildi")

    return test_app


@pytest.fixture
async def handler_client(handler_app):
    transport = ASGITransport(app=handler_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def test_permission_locked_error_returns_403_with_message(handler_client):
    response = await handler_client.get("/kilitli")

    assert response.status_code == 403
    assert response.json() == {"detail": "Sistem Yöneticisi rolünün izinleri değiştirilemez"}


async def test_domain_error_returns_400_with_message(handler_client):
    response = await handler_client.get("/alan-hatasi")

    assert response.status_code == 400
    assert response.json() == {"detail": "Alan kuralı ihlal edildi"}


async def test_section_type_taken_error_conflict_body_has_existing_not_duplicate_handler():
    """Alt sinif `DuplicateError` handler'ina DUSMEZ: govdede `existing` olmali."""
    import uuid

    from app.core.errors import DuplicateError, SectionTypeTakenError

    existing_id = uuid.uuid4()
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/tip")
    async def tip():
        raise SectionTypeTakenError(
            "Bu bölüm tipi zaten var: Peyzaj", existing_id=existing_id, existing_name="Peyzaj"
        )

    @app.get("/duz")
    async def duz():
        raise DuplicateError("düz tekrar")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        taken = await c.get("/tip")
        plain = await c.get("/duz")

    assert taken.status_code == 409
    assert taken.json() == {
        "detail": "Bu bölüm tipi zaten var: Peyzaj",
        "existing": {"id": str(existing_id), "name": "Peyzaj"},
    }
    assert plain.status_code == 409
    assert plain.json() == {"detail": "düz tekrar"}  # düz DuplicateError `existing` taşımaz


async def test_integrity_error_maps_to_409():
    from sqlalchemy.exc import IntegrityError

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise IntegrityError("stmt", {}, Exception("fk"))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/boom")
    assert resp.status_code == 409

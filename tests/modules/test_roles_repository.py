import uuid

import pytest
from sqlalchemy import func, select

from app.core.errors import DomainError, NotFoundError, PermissionLockedError
from app.modules.roles import service
from app.modules.roles.models import Role, RolePagePermission
from app.modules.roles.schemas import RoleCreate


async def test_create_custom_role_100_sayfa_hucresi_acar(seeded_db):
    """Özel rol 100 sayfa hücresiyle doğar."""
    role = await service.create_custom_role(
        seeded_db, RoleCreate(key="saha_amiri", name="Saha Amiri", emoji="🚧", description="")
    )
    assert role.is_system is False
    sayfa = (
        await seeded_db.execute(
            select(func.count())
            .select_from(RolePagePermission)
            .where(RolePagePermission.role_id == role.id)
        )
    ).scalar_one()
    assert sayfa == 100


async def test_create_custom_role_duplicate_key_raises(seeded_db):
    with pytest.raises(DomainError):
        await service.create_custom_role(
            seeded_db, RoleCreate(key="patron", name="X", emoji="", description="")
        )


async def test_delete_system_role_locked(seeded_db):
    sysadmin = (
        await seeded_db.execute(select(Role).where(Role.key == "system_admin"))
    ).scalar_one()
    with pytest.raises(PermissionLockedError):
        await service.delete_role(seeded_db, sysadmin.id)


async def test_delete_unknown_role_raises(seeded_db):
    with pytest.raises(NotFoundError):
        await service.delete_role(seeded_db, uuid.uuid4())


async def test_delete_role_in_use_rejected(seeded_db, user_factory):
    role = await service.create_custom_role(
        seeded_db, RoleCreate(key="gecici_rol", name="Geçici", emoji="", description="")
    )
    await user_factory(email="ru@t.co", password="parola1234", role_key="gecici_rol")
    with pytest.raises(DomainError):
        await service.delete_role(seeded_db, role.id)

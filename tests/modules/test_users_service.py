import uuid

import pytest
from sqlalchemy import select

from app.core.errors import DomainError, NotFoundError, PermissionLockedError
from app.core.security import hash_password, verify_password
from app.modules.roles.models import Role
from app.modules.users import service
from app.modules.users.models import User, UserStatus
from app.modules.users.schemas import UserCreate, UserUpdate


async def _role_id(session, key):
    return (await session.execute(select(Role).where(Role.key == key))).scalar_one().id


async def _system_admin_actor(session):
    role = (await session.execute(select(Role).where(Role.key == "system_admin"))).scalar_one()
    actor = User(
        email="actor-admin@t.co",
        password_hash=hash_password("parola1234"),
        full_name="Actor",
        role_id=role.id,
    )
    session.add(actor)
    await session.flush()
    return actor


async def test_create_user_hashes_password(seeded_db):
    actor = await _system_admin_actor(seeded_db)
    data = UserCreate(
        email="a@t.co",
        password="parola1234",
        full_name="Ahmet",
        role_id=await _role_id(seeded_db, "patron"),
    )
    user = await service.create_user(seeded_db, actor, data)
    assert user.password_hash != "parola1234"
    assert verify_password("parola1234", user.password_hash)


async def test_create_user_duplicate_email_raises(seeded_db):
    actor = await _system_admin_actor(seeded_db)
    rid = await _role_id(seeded_db, "patron")
    await service.create_user(
        seeded_db,
        actor,
        UserCreate(email="d@t.co", password="parola1234", full_name="A", role_id=rid),
    )
    with pytest.raises(DomainError):
        await service.create_user(
            seeded_db,
            actor,
            UserCreate(email="d@t.co", password="parola1234", full_name="B", role_id=rid),
        )


async def test_create_user_unknown_role_raises(seeded_db):
    actor = await _system_admin_actor(seeded_db)
    with pytest.raises(NotFoundError):
        await service.create_user(
            seeded_db,
            actor,
            UserCreate(email="x@t.co", password="parola1234", full_name="X", role_id=uuid.uuid4()),
        )


async def test_update_user_changes_status(seeded_db, user_factory):
    actor = await _system_admin_actor(seeded_db)
    user = await user_factory(email="u@t.co", password="parola1234", role_key="site_chief")
    updated = await service.update_user(
        seeded_db, actor, user.id, UserUpdate(status=UserStatus.passive)
    )
    assert updated.status is UserStatus.passive


async def test_set_password(seeded_db, user_factory):
    user = await user_factory(email="p@t.co", password="parola1234", role_key="patron")
    await service.set_user_password(seeded_db, user.id, "yeniParola9")
    refreshed = (await seeded_db.execute(select(User).where(User.id == user.id))).scalar_one()
    assert verify_password("yeniParola9", refreshed.password_hash)


async def test_set_password_bumps_token_version(seeded_db, user_factory):
    user = await user_factory(email="tv@t.co", password="parola1234", role_key="patron")
    assert user.token_version == 0
    await service.set_user_password(seeded_db, user.id, "yeniParola9")
    assert user.token_version == 1


async def test_delete_user(seeded_db, user_factory):
    user = await user_factory(email="del@t.co", password="parola1234", role_key="accounting")
    await service.delete_user(seeded_db, user.id)
    result = await seeded_db.execute(select(User).where(User.id == user.id))
    assert result.scalar_one_or_none() is None


async def test_delete_unknown_user_raises(seeded_db):
    with pytest.raises(NotFoundError):
        await service.delete_user(seeded_db, uuid.uuid4())


async def test_full_actor_cannot_assign_system_role(seeded_db):
    from app.core.access import AccessLevel, Scope
    from app.modules.roles.schemas import RoleCreate
    from app.modules.roles.service import create_custom_role
    from tests._legacy_permission_yardimcisi import update_role_permission

    mgr_role = await create_custom_role(
        seeded_db, RoleCreate(key="kul_yonetici", name="Kul", emoji="", description="")
    )
    await update_role_permission(
        seeded_db, mgr_role.id, "user_management", AccessLevel.full, Scope.all
    )
    actor = User(
        email="mgr@t.co",
        password_hash=hash_password("parola1234"),
        full_name="Mgr",
        role_id=mgr_role.id,
    )
    seeded_db.add(actor)
    await seeded_db.flush()
    sysadmin = (
        await seeded_db.execute(select(Role).where(Role.key == "system_admin"))
    ).scalar_one()
    with pytest.raises(PermissionLockedError):
        await service.create_user(
            seeded_db,
            actor,
            UserCreate(
                email="hedef@t.co", password="parola1234", full_name="H", role_id=sysadmin.id
            ),
        )


async def test_admin_actor_can_assign_system_role(seeded_db):
    actor = await _system_admin_actor(seeded_db)
    sysadmin = (
        await seeded_db.execute(select(Role).where(Role.key == "system_admin"))
    ).scalar_one()
    user = await service.create_user(
        seeded_db,
        actor,
        UserCreate(
            email="yeni-admin@t.co", password="parola1234", full_name="A", role_id=sysadmin.id
        ),
    )
    assert user.role_id == sysadmin.id


async def test_cannot_delete_last_system_admin(seeded_db):
    actor = await _system_admin_actor(seeded_db)  # tek aktif system_admin
    with pytest.raises(DomainError):
        await service.delete_user(seeded_db, actor.id)


async def _user_manager_actor(seeded_db, email: str = "yetkili@t.co"):
    """user_management=full, diğer TÜM hücreleri none olan özel rolde bir aktör."""
    from app.core.access import AccessLevel, Scope
    from app.modules.roles.schemas import RoleCreate
    from app.modules.roles.service import create_custom_role
    from tests._legacy_permission_yardimcisi import update_role_permission

    mgr_role = await create_custom_role(
        seeded_db, RoleCreate(key="kul_yon_2", name="Kul2", emoji="", description="")
    )
    await update_role_permission(
        seeded_db, mgr_role.id, "user_management", AccessLevel.full, Scope.all
    )
    actor = User(
        email=email,
        password_hash=hash_password("parola1234"),
        full_name="Yetkili",
        role_id=mgr_role.id,
    )
    seeded_db.add(actor)
    await seeded_db.flush()
    return actor


async def test_full_actor_cannot_assign_role_stronger_than_own(seeded_db):
    """Aktör, kendi sahip OLMADIĞI yetkileri içeren (is_system=False) bir rolü atayamaz."""
    actor = await _user_manager_actor(seeded_db)
    accounting_id = await _role_id(seeded_db, "accounting")  # treasury/payroll/... = full
    with pytest.raises(PermissionLockedError):
        await service.create_user(
            seeded_db,
            actor,
            UserCreate(
                email="mint@t.co", password="parola1234", full_name="M", role_id=accounting_id
            ),
        )


async def test_full_actor_cannot_escalate_self_via_update(seeded_db):
    """PATCH /users/{kendi_id} ile kendine güçlü bir rol yazılamaz."""
    actor = await _user_manager_actor(seeded_db, email="kendi@t.co")
    accounting_id = await _role_id(seeded_db, "accounting")
    with pytest.raises(PermissionLockedError):
        await service.update_user(seeded_db, actor, actor.id, UserUpdate(role_id=accounting_id))


async def test_full_actor_can_assign_role_within_own_level(seeded_db):
    """POZİTİF KONTROL: aktörün seviyesini hiçbir modülde aşmayan rol atanabilir."""
    from app.modules.roles.schemas import RoleCreate
    from app.modules.roles.service import create_custom_role

    actor = await _user_manager_actor(seeded_db, email="pozitif@t.co")
    bos_rol = await create_custom_role(
        seeded_db, RoleCreate(key="bos_rol", name="Boş", emoji="", description="")
    )
    user = await service.create_user(
        seeded_db,
        actor,
        UserCreate(email="bos@t.co", password="parola1234", full_name="B", role_id=bos_rol.id),
    )
    assert user.role_id == bos_rol.id

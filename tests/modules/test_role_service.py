import uuid

import pytest
from sqlalchemy import select

from app.core.access import AccessLevel
from app.core.errors import NotFoundError, PermissionLockedError
from app.core.sayfalar import PageLevel, sayfa_matrisi
from app.modules.roles.models import Role, RolePagePermission
from app.modules.roles.service import rename_role
from tests._modul_duzeyi_yardimcisi import _baslangic_haritasi, modul_duzeyi_yaz


async def _role(session, key: str) -> Role:
    return (await session.execute(select(Role).where(Role.key == key))).scalar_one()


async def _hucre_seviyeleri(session, role_id) -> dict[str, PageLevel]:
    rows = await session.execute(
        select(RolePagePermission.page_key, RolePagePermission.level).where(
            RolePagePermission.role_id == role_id
        )
    )
    return {page_key: level for page_key, level in rows.all()}


def _beklenen_hucreler(role_key: str, modul: str, level: AccessLevel) -> dict[str, PageLevel]:
    harita = {**_baslangic_haritasi(role_key), modul: level}
    return {key: lvl for key, (lvl, _) in sayfa_matrisi(harita).items()}


async def test_permission_can_be_raised_for_normal_role(seeded_db):
    role = await _role(seeded_db, "site_chief")
    await modul_duzeyi_yaz(seeded_db, role.id, "progress_payments", AccessLevel.approve)
    beklenen = _beklenen_hucreler("site_chief", "progress_payments", AccessLevel.approve)
    assert await _hucre_seviyeleri(seeded_db, role.id) == beklenen


async def test_permission_can_be_lowered_for_normal_role(seeded_db):
    role = await _role(seeded_db, "patron")
    await modul_duzeyi_yaz(seeded_db, role.id, "payroll", AccessLevel.view)
    beklenen = _beklenen_hucreler("patron", "payroll", AccessLevel.view)
    assert await _hucre_seviyeleri(seeded_db, role.id) == beklenen


async def test_system_admin_permissions_are_locked(seeded_db):
    """Kilitlenme koruması: system_admin izin satırları hiç kimse tarafından değiştirilemez."""
    role = await _role(seeded_db, "system_admin")
    with pytest.raises(PermissionLockedError):
        await modul_duzeyi_yaz(seeded_db, role.id, "settings", AccessLevel.none)


async def test_system_admin_can_still_be_renamed(seeded_db):
    """Ad/emoji/açıklama düzenlenebilir; kilitli olan yalnızca izinlerdir."""
    role = await _role(seeded_db, "system_admin")
    renamed = await rename_role(
        seeded_db, role.id, name="Süper Yönetici", emoji="⚡", description=""
    )
    assert renamed.name == "Süper Yönetici"
    assert renamed.key == "system_admin"


async def test_renaming_never_changes_key(seeded_db):
    """Yetki kontrolü key'e dayanır; ad değişince yetkiler kaymamalı."""
    role = await _role(seeded_db, "field_engineer")
    renamed = await rename_role(seeded_db, role.id, name="Teknik Ofis", emoji="📐", description="")
    assert renamed.key == "field_engineer"


async def test_rename_unknown_role_raises_not_found(seeded_db):
    with pytest.raises(NotFoundError):
        await rename_role(seeded_db, uuid.uuid4(), "X", "", "")


async def test_update_permission_missing_row_raises_not_found(seeded_db):
    patron = await _role(seeded_db, "patron")
    with pytest.raises(NotFoundError):
        await modul_duzeyi_yaz(seeded_db, patron.id, "olmayan_modul", AccessLevel.view)


async def test_update_permission_unknown_role_raises_not_found(seeded_db):
    with pytest.raises(NotFoundError):
        await modul_duzeyi_yaz(seeded_db, uuid.uuid4(), "dashboard", AccessLevel.view)


async def test_update_permission_system_admin_still_locked(seeded_db):
    sysadmin = await _role(seeded_db, "system_admin")
    with pytest.raises(PermissionLockedError):
        await modul_duzeyi_yaz(seeded_db, sysadmin.id, "dashboard", AccessLevel.view)

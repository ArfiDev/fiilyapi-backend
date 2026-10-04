import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel, Scope
from app.core.sayfalar import HiddenCategory
from app.modules.roles.models import (
    IZN_ROLE_KEYS,
    Module,
    Role,
    RoleHiddenField,
    RolePagePermission,
    RolePermission,
)


async def get_permission(
    session: AsyncSession, role_id: uuid.UUID, module_key: str
) -> RolePermission | None:
    stmt = (
        select(RolePermission)
        .join(Module, Module.id == RolePermission.module_id)
        .where(RolePermission.role_id == role_id, Module.key == module_key)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_roles(session: AsyncSession) -> list[Role]:
    result = await session.execute(select(Role).order_by(Role.is_system.desc(), Role.name))
    return list(result.scalars().all())


async def get_role(session: AsyncSession, role_id: uuid.UUID) -> Role | None:
    return await session.get(Role, role_id)


async def get_module(session: AsyncSession, module_key: str) -> Module | None:
    stmt = select(Module).where(Module.key == module_key)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_modules(session: AsyncSession) -> list[Module]:
    result = await session.execute(select(Module).order_by(Module.sort_order))
    return list(result.scalars().all())


async def get_role_matrix(
    session: AsyncSession, role_id: uuid.UUID
) -> list[tuple[Module, RolePermission]]:
    """Rolün matrisi — HER modül için bir hücre, izin satırı olmasa bile.

    🔴 Bu fonksiyon eskiden INNER JOIN'di ve `modules`ı değil `role_permissions`ı
    sürüyordu. Satırı olmayan modül matristen TAMAMEN DÜŞÜYORDU; belirti Ayarlar
    ekranı değil `/auth/me` idi — `permissions` haritasında anahtar HİÇ bulunmuyordu.

    Delik yapısaldır: `create_custom_role` yalnız o anda var olan modüller için
    hücre açar, uzantı migration'ları ise izin satırlarını `WHERE r.key = :role_key`
    süzgeciyle ve sabit `ROLE_ORDER` üzerinde yazar (tek istisna `ai`). Yani
    migration'dan ÖNCE açılmış her özel rol, sonradan inen her modülün dışında kalır.

    Artık sürücü `modules`tır (LEFT OUTER JOIN) ve eksik hücre yerine VARSAYILAN
    KAPALI bir hücre üretilir. Üretilen nesne `session.add` EDİLMEZ: okuma yolu
    yazmaz, yoksa `uq_role_module` yarışında çift satır doğardı. Kalıcı hücreyi
    yalnız `service.update_role_permission` açar.
    """
    stmt = (
        select(Module, RolePermission)
        .outerjoin(
            RolePermission,
            (RolePermission.module_id == Module.id) & (RolePermission.role_id == role_id),
        )
        .order_by(Module.sort_order)
    )
    result = await session.execute(stmt)
    return [
        (module, permission or varsayilan_hucre(role_id, module))
        for module, permission in result.all()
    ]


def varsayilan_hucre(role_id: uuid.UUID, module: Module) -> RolePermission:
    """İzin satırı olmayan (rol, modül) çifti için GEÇİCİ, kalıcılaşmayan hücre.

    Varsayılan `none`dur — `core/permissions.py` zaten satır yokken reddediyordu,
    bu hücre o davranışı AYNEN temsil eder; matrise görünürlük ekler, yetki eklemez.
    """
    return RolePermission(
        role_id=role_id,
        module_id=module.id,
        access_level=AccessLevel.none,
        scope=Scope.all,
    )


async def list_role_page_cells(
    session: AsyncSession, role_id: uuid.UUID
) -> list[RolePagePermission]:
    """Rolün sayfa hücreleri — TEK sorgu (`/auth/me` yolu; rol başına ≤100 satır)."""
    result = await session.execute(
        select(RolePagePermission)
        .where(RolePagePermission.role_id == role_id)
        .order_by(RolePagePermission.page_key)
    )
    return list(result.scalars().all())


async def list_role_hidden_categories(
    session: AsyncSession, role_id: uuid.UUID
) -> list[HiddenCategory]:
    """Rolün gizlediği hassas alan kategorileri — TEK sorgu."""
    result = await session.execute(
        select(RoleHiddenField.category).where(RoleHiddenField.role_id == role_id)
    )
    return sorted(result.scalars().all(), key=lambda c: c.value)


def role_assignable(role_key: str, has_legacy_cells: bool) -> bool:
    """Rol bir kullanıcıya atanabilir mi? (TEK kural: `users/service` ve `GET /roles` bunu kullanır)

    IZN-B1'in 6 yeni rolü eski modül kapısında hücre taşımaz → atanan kullanıcı HER uçta 403
    alırdı; B2 kapı köprüsü canlıya çıkana dek atanamazlar. Aynı anahtarla elle açılmış ve eski
    hücreleri OLAN rol bu kilide takılmaz (eski kapı onu zaten doğru yönetir).
    """
    return role_key not in IZN_ROLE_KEYS or has_legacy_cells


async def has_legacy_cells(session: AsyncSession, role_id: uuid.UUID) -> bool:
    count = (
        await session.execute(
            select(func.count())
            .select_from(RolePermission)
            .where(RolePermission.role_id == role_id)
        )
    ).scalar_one()
    return count > 0


async def legacy_cell_role_ids(session: AsyncSession, role_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Verilen roller içinde eski modül hücresi (`role_permissions` satırı) olanlar — TEK sorgu."""
    if not role_ids:
        return set()
    result = await session.execute(
        select(RolePermission.role_id).where(RolePermission.role_id.in_(role_ids)).distinct()
    )
    return set(result.scalars().all())

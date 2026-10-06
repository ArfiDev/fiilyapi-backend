import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sayfalar import HiddenCategory
from app.modules.roles.models import Role, RoleHiddenField, RolePagePermission


async def list_roles(session: AsyncSession) -> list[Role]:
    result = await session.execute(select(Role).order_by(Role.is_system.desc(), Role.name))
    return list(result.scalars().all())


async def get_role(session: AsyncSession, role_id: uuid.UUID) -> Role | None:
    return await session.get(Role, role_id)


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


async def role_user_counts(
    session: AsyncSession, role_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Roller için FARKLI kullanıcı sayısı — TEK sorgu (IZN-B3).

    Bir kullanıcı bir role ANA rolü olarak YA DA bir proje ekibi satırındaki rolü olarak bağlıysa
    o rolde sayılır; iki yoldan da bağlı (ya da birkaç projede aynı rolde) kişi TEK kez sayılır
    (`UNION` tekilleştirir). `0` ⇔ rol silinebilir; `project_members.role_id` RESTRICT olduğu için
    bu sayı FK'nin de aynasıdır.
    """
    if not role_ids:
        return {}
    from app.modules.users.models import ProjectMember, User  # roles ↔ users döngüsünü önler

    pairs = (
        select(User.role_id.label("role_id"), User.id.label("user_id"))
        .where(User.role_id.in_(role_ids))
        .union(
            select(ProjectMember.role_id, ProjectMember.user_id).where(
                ProjectMember.role_id.in_(role_ids)
            )
        )
        .subquery()
    )
    result = await session.execute(select(pairs.c.role_id, func.count()).group_by(pairs.c.role_id))
    return {role_id: count for role_id, count in result.all()}

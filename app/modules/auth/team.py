"""`/auth/me` proje ekibi yükü (IZN-B3, IZN-PLAN §5): proje başına rol + ekip rollerinin sayfaları.

İki ayrı kümedir ve bilerek öyledir: `projects[]` HAFİFTİR (proje + rol anahtarı + disiplin
kimlikleri); sayfa haritaları rol başına BİR kez `role_pages`te durur — aynı rolü 30 projede
taşıyan kişi 30 kez 3 KB taşımaz. Sorgu sayısı sabittir (4): ekip, disiplinler, hücreler,
gizli alanlar; kullanıcının rolü kaç farklı olursa olsun.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.schemas import MeProject, MeRolePages
from app.modules.pages.grants import grants_from_cells
from app.modules.roles.models import Role, RoleHiddenField, RolePagePermission
from app.modules.users.models import ProjectMember, ProjectMemberDiscipline


async def load_team(session: AsyncSession, user_id: uuid.UUID) -> list[tuple[ProjectMember, str]]:
    """Kullanıcının ekip satırları + rol anahtarı (proje kimliğiyle sıralı → kararlı yanıt)."""
    rows = await session.execute(
        select(ProjectMember, Role.key)
        .join(Role, Role.id == ProjectMember.role_id)
        .where(ProjectMember.user_id == user_id)
        .order_by(ProjectMember.project_id)
    )
    return [(member, role_key) for member, role_key in rows.all()]


async def team_projects(
    session: AsyncSession, team: list[tuple[ProjectMember, str]]
) -> list[MeProject]:
    disciplines: dict[uuid.UUID, list[uuid.UUID]] = {}
    if team:
        rows = await session.execute(
            select(ProjectMemberDiscipline.member_id, ProjectMemberDiscipline.discipline_id)
            .where(ProjectMemberDiscipline.member_id.in_([m.id for m, _ in team]))
            .order_by(ProjectMemberDiscipline.discipline_id)
        )
        for member_id, discipline_id in rows.all():
            disciplines.setdefault(member_id, []).append(discipline_id)
    return [
        MeProject(
            project_id=member.project_id,
            role_key=role_key,
            discipline_ids=disciplines.get(member.id, []),
        )
        for member, role_key in team
    ]


async def team_role_pages(
    session: AsyncSession, team: list[tuple[ProjectMember, str]], main_role_key: str
) -> dict[str, MeRolePages]:
    """Ekipte kullanılan ve ANA rolden FARKLI her rolün sayfa hücreleri + gizli kategorileri.

    Ana rolle aynı anahtarlı ekip rolü buraya girmez (istemci ana `pages`i kullanır). Rolün
    satırı olmayan sayfa katalogdan `none` ile doldurulur (`/auth/me.pages` ile aynı kural).
    """
    role_ids = {m.role_id: key for m, key in team if key != main_role_key}
    if not role_ids:
        return {}
    rows_by_role: dict[uuid.UUID, list[RolePagePermission]] = {rid: [] for rid in role_ids}
    cells = await session.execute(
        select(RolePagePermission)
        .where(RolePagePermission.role_id.in_(role_ids))
        .order_by(RolePagePermission.page_key)
    )
    for cell in cells.scalars():
        rows_by_role[cell.role_id].append(cell)
    hidden: dict[uuid.UUID, list] = {rid: [] for rid in role_ids}
    for role_id, category in (
        await session.execute(
            select(RoleHiddenField.role_id, RoleHiddenField.category).where(
                RoleHiddenField.role_id.in_(role_ids)
            )
        )
    ).all():
        hidden[role_id].append(category)
    return {
        key: MeRolePages(
            pages=grants_from_cells(rows_by_role[role_id]),
            hidden_fields=sorted(hidden[role_id], key=lambda c: c.value),
        )
        for role_id, key in role_ids.items()
    }

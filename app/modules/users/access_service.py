"""Kullanıcı erişimi (IZN-B3): ana rol + "Tüm projeler" + proje ekibi (rol + disiplin), ATOMİK.

`GET`/`PUT /users/{id}/access` iki yüzü: okuma ve TAM-DEĞİŞTİRME. Eski `project-access` ve
global `disciplines` uçlarının yerini alır (KARARLAR §1.7).

🔴 TAM-DEĞİŞTİRME KİLİT ALTINDA koşar: kullanıcı satırı `FOR UPDATE` ile okunur; aynı kişinin
erişimini yazan iki istek serileşir (`project_members` UQ'su ikinciyi yine 409'a düşürürdü, ama
karışık küme bırakmasın diye kilit baştan alınır).

🔴 ATOMİKLİK: tüm doğrulama YAZMADAN ÖNCE koşar; yazma tek istek transaction'ındadır (`get_db`
hata → rollback). Ana rol, "Tüm projeler" işareti, ekip satırları ve disiplinler HEP YA DA HİÇ.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.discipline_ref import DisciplineRef
from app.core.errors import DomainError, NotFoundError, UserAccessValidationError
from app.modules.catalog.models import EvDiscipline
from app.modules.projects.models import Project
from app.modules.roles.models import SYSTEM_ADMIN_KEY, Role
from app.modules.users import repository
from app.modules.users.models import ProjectMember, ProjectMemberDiscipline, User
from app.modules.users.schemas import (
    ProjectMemberInput,
    ProjectMemberResponse,
    UserAccessInput,
    UserAccessResponse,
)
from app.modules.users.service import (
    is_last_active_system_admin,
    require_assignable_role,
)

USER_MISSING = "Kullanıcı bulunamadı"

ALL_PROJECTS_WITH_TEAM = (
    "Tüm projelere erişimi olan kullanıcıya proje ekibi satırı eklenemez; "
    "önce 'Tüm projeler' işaretini kaldırın ya da proje listesini boşaltın"
)
DUPLICATE_PROJECT = "Aynı proje birden fazla kez eklenmiş"
SYSTEM_ADMIN_AS_PROJECT_ROLE = (
    "Sistem Yöneticisi rolü proje rolü olarak atanamaz; yalnız ana rol olarak atanır"
)
LAST_ADMIN_DEMOTION = "Son aktif Sistem Yöneticisi düşürülemez"


@dataclass(frozen=True, slots=True)
class AccessChange:
    """PUT sonucu: yanıt + denetim satırı için özet."""

    user: User
    response: UserAccessResponse
    main_role_name: str


def _listed(ids: set[uuid.UUID]) -> str:
    return ", ".join(sorted(str(i) for i in ids))


async def get_access(session: AsyncSession, user_id: uuid.UUID) -> UserAccessResponse:
    user = await repository.get_user(session, user_id)
    if user is None:
        raise NotFoundError(USER_MISSING)
    return await _build_response(session, user)


async def _build_response(session: AsyncSession, user: User) -> UserAccessResponse:
    """İki sorgu: ekip satırları (+ proje adı), disiplinler (+ ref). Sıra: proje adı, kimlik."""
    # "Tüm projeler" kişide ekip satırı YOK SAYILIR (bayat satır olsa bile `projects` boş döner).
    members = (
        []
        if user.all_projects
        else (
            await session.execute(
                select(ProjectMember, Project.name)
                .join(Project, Project.id == ProjectMember.project_id)
                .where(ProjectMember.user_id == user.id)
                .order_by(Project.name, Project.id)
            )
        ).all()
    )
    refs_by_member: dict[uuid.UUID, list[DisciplineRef]] = {}
    if members:
        rows = (
            await session.execute(
                select(ProjectMemberDiscipline.member_id, EvDiscipline)
                .join(EvDiscipline, EvDiscipline.id == ProjectMemberDiscipline.discipline_id)
                .where(ProjectMemberDiscipline.member_id.in_([m.id for m, _ in members]))
                .order_by(EvDiscipline.code)
            )
        ).all()
        for member_id, discipline in rows:
            refs_by_member.setdefault(member_id, []).append(
                DisciplineRef.model_validate(discipline)
            )
    return UserAccessResponse(
        role_id=user.role_id,
        all_projects=user.all_projects,
        projects=[
            ProjectMemberResponse(
                project_id=member.project_id,
                project_name=project_name,
                role_id=member.role_id,
                disciplines=refs_by_member.get(member.id, []),
            )
            for member, project_name in members
        ],
    )


def _validate_shape(data: UserAccessInput) -> list[ProjectMemberInput]:
    """Gövde içi kurallar (DB'ye gitmeden): all_projects ⇒ ekip boş; proje yinelenmez."""
    if data.all_projects and data.projects:
        raise UserAccessValidationError(ALL_PROJECTS_WITH_TEAM)
    project_ids = [p.project_id for p in data.projects]
    if len(set(project_ids)) != len(project_ids):
        raise UserAccessValidationError(DUPLICATE_PROJECT)
    return list(data.projects)


async def _existing_ids(
    session: AsyncSession, column: InstrumentedAttribute[uuid.UUID], ids: set[uuid.UUID]
) -> set[uuid.UUID]:
    if not ids:
        return set()
    return set((await session.execute(select(column).where(column.in_(ids)))).scalars())


async def _validate_references(
    session: AsyncSession, data: UserAccessInput, projects: list[ProjectMemberInput]
) -> dict[uuid.UUID, Role]:
    """Bilinmeyen proje / rol / disiplin → 422 (kimlikler mesajda). Döner: rol kimliği → rol."""
    role_ids = {data.role_id} | {p.role_id for p in projects}
    roles = {
        role.id: role
        for role in (await session.execute(select(Role).where(Role.id.in_(role_ids)))).scalars()
    }
    if missing := role_ids - roles.keys():
        raise UserAccessValidationError(f"Bilinmeyen rol: {_listed(missing)}")
    project_ids = {p.project_id for p in projects}
    if missing := project_ids - await _existing_ids(session, Project.id, project_ids):
        raise UserAccessValidationError(f"Bilinmeyen proje: {_listed(missing)}")
    discipline_ids = {d for p in projects for d in p.discipline_ids}
    if missing := discipline_ids - await _existing_ids(session, EvDiscipline.id, discipline_ids):
        raise UserAccessValidationError(f"Bilinmeyen disiplin: {_listed(missing)}")
    for entry in projects:
        if roles[entry.role_id].key == SYSTEM_ADMIN_KEY:
            raise UserAccessValidationError(SYSTEM_ADMIN_AS_PROJECT_ROLE)
    return roles


async def replace_access(
    session: AsyncSession, actor: User, user_id: uuid.UUID, data: UserAccessInput
) -> AccessChange:
    """Ana rol + `all_projects` + ekip satırları + disiplinler: TAM DEĞİŞTİRME, tek transaction.

    Sıra: 404 (kullanıcı) → 422 (gövde/başvuru) → 403 (rol atama yetkisi) → 400 (son Sistem
    Yöneticisi) → yazma. Yetki karşılaştırması (`require_assignable_role`) yalnız YENİ verilen
    rollere uygulanır: değişmeyen rol bir yetki VERMEZ; yoksa yetkisi daha yüksek bir rolü olan
    kişinin disiplinini bile düzenleyemezdiniz.
    """
    user = await repository.get_user_locked(session, user_id)
    if user is None:
        raise NotFoundError(USER_MISSING)
    projects = _validate_shape(data)
    roles = await _validate_references(session, data, projects)

    existing = {
        member.project_id: member
        for member in (
            await session.execute(select(ProjectMember).where(ProjectMember.user_id == user.id))
        ).scalars()
    }
    new_roles: dict[uuid.UUID, Role] = {}
    if data.role_id != user.role_id:
        new_roles[data.role_id] = roles[data.role_id]
    for entry in projects:
        current = existing.get(entry.project_id)
        if current is None or current.role_id != entry.role_id:
            new_roles[entry.role_id] = roles[entry.role_id]
    for role_id in new_roles:
        await require_assignable_role(session, actor, role_id)

    main_role = roles[data.role_id]
    if data.role_id != user.role_id and await is_last_active_system_admin(session, user):
        raise DomainError(LAST_ADMIN_DEMOTION)

    user.role_id = data.role_id
    user.all_projects = data.all_projects
    await _apply_team(session, user.id, existing, projects)
    await session.flush()
    return AccessChange(user, await _build_response(session, user), main_role.name)


async def _apply_team(
    session: AsyncSession,
    user_id: uuid.UUID,
    existing: dict[uuid.UUID, ProjectMember],
    projects: list[ProjectMemberInput],
) -> None:
    """Farkı uygular: çıkan üye silinir (disiplinler CASCADE), yeni eklenir, kalan güncellenir."""
    wanted = {p.project_id for p in projects}
    removed = [m.id for pid, m in existing.items() if pid not in wanted]
    if removed:
        await session.execute(delete(ProjectMember).where(ProjectMember.id.in_(removed)))

    current_disciplines: dict[uuid.UUID, set[uuid.UUID]] = {}
    kept = [m.id for pid, m in existing.items() if pid in wanted]
    if kept:
        rows = await session.execute(
            select(ProjectMemberDiscipline.member_id, ProjectMemberDiscipline.discipline_id).where(
                ProjectMemberDiscipline.member_id.in_(kept)
            )
        )
        for member_id, discipline_id in rows.all():
            current_disciplines.setdefault(member_id, set()).add(discipline_id)

    for entry in projects:
        want = set(entry.discipline_ids)
        member = existing.get(entry.project_id)
        if member is None:
            member = ProjectMember(
                id=uuid.uuid4(),
                user_id=user_id,
                project_id=entry.project_id,
                role_id=entry.role_id,
            )
            session.add(member)
            await session.flush()  # üye satırı disiplin FK'sinden ÖNCE yazılır
        elif member.role_id != entry.role_id:
            member.role_id = entry.role_id
        have = current_disciplines.get(member.id, set())
        if have - want:
            await session.execute(
                delete(ProjectMemberDiscipline).where(
                    ProjectMemberDiscipline.member_id == member.id,
                    ProjectMemberDiscipline.discipline_id.in_(have - want),
                )
            )
        session.add_all(
            ProjectMemberDiscipline(member_id=member.id, discipline_id=d) for d in want - have
        )

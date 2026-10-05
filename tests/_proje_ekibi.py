"""TEST YARDIMCISI — proje ekibi kurulumu (IZN-B3).

Eski `UserProjectAccess` / global `UserDiscipline` yerine testler ekibi bu yardımcılarla kurar.
Üretimde YOKTUR: üretim yazma yolu `PUT /users/{id}/access`tir (`users/access_service.py`).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sayfalar import SAYFALAR, PageLevel
from app.modules.roles.models import RolePagePermission
from app.modules.users.models import ProjectMember, ProjectMemberDiscipline, User


async def ekibe_ekle(
    session: AsyncSession, user: User, project_id: uuid.UUID, role_id: uuid.UUID | None = None
) -> ProjectMember:
    """Kişiyi projenin ekibine yazar (zaten üyeyse satırı döner; rol verilirse GÜNCELLER).

    `role_id` verilmezse kişinin ANA rolü yazılır (göç kuralı).
    """
    uye = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.user_id == user.id, ProjectMember.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if uye is None:
        uye = ProjectMember(user_id=user.id, project_id=project_id, role_id=role_id or user.role_id)
        session.add(uye)
    elif role_id is not None:
        uye.role_id = role_id
    await session.flush()
    return uye


async def disiplin_ata(
    session: AsyncSession, user: User, project_id: uuid.UUID, discipline_id: uuid.UUID
) -> None:
    """Kişiye O PROJEDE disiplin atar (ekipte değilse önce ana rolüyle ekibe yazar)."""
    uye = await ekibe_ekle(session, user, project_id)
    mevcut = await session.get(ProjectMemberDiscipline, (uye.id, discipline_id))
    if mevcut is None:
        session.add(ProjectMemberDiscipline(member_id=uye.id, discipline_id=discipline_id))
        await session.flush()


async def tum_projeler(session: AsyncSession, user: User) -> None:
    """`users.all_projects` işareti (ekip satırı yazmaz)."""
    user.all_projects = True
    await session.flush()


async def modul_sayfa_hucreleri(
    session: AsyncSession, role_id: uuid.UUID, module_key: str, level: PageLevel = PageLevel.edit
) -> None:
    """Rolün `module_key` modülünü besleyen TÜM sayfalara (kök + proje içi ikizler) hücre yazar.

    Elle kurulan test rolleri (gerçek commit'li yarış kurulumları) yalnız kök sayfayı verirdi; ekip
    rolü PROJE İÇİ sayfalarla karar verdiği için ikizler de gerekir (seed rollerinde migration
    ikisini de üretir).
    """
    for sayfa in SAYFALAR:
        if sayfa.eski_modul != module_key:
            continue
        mevcut = await session.get(RolePagePermission, (role_id, sayfa.key))
        if mevcut is None:
            session.add(
                RolePagePermission(
                    role_id=role_id, page_key=sayfa.key, level=level, can_approve=False
                )
            )
        else:
            mevcut.level = level
    await session.flush()


async def baska_projede_disiplinli(
    session: AsyncSession, user_id: uuid.UUID, discipline_id: uuid.UUID
) -> None:
    """Kişiyi bir projede disiplinle kısıtlar (IZN-B3: şirket geneli uçlar disiplinden etkilenmez).
    Kısıt PROJE BAŞINADIR; şirket geneli uçları etkilemez."""
    from app.modules.projects.models import Project  # test yardımcısı: yerel import

    user = await session.get(User, user_id)
    proje = Project(code=f"KIS-{uuid.uuid4().hex[:6]}", name="Kisit Projesi")
    session.add(proje)
    await session.flush()
    await disiplin_ata(session, user, proje.id, discipline_id)

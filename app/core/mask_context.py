"""ETKİN ROLÜN gizli kategorileri (IZN-B4): bir isteğin `MaskeKumeleri`ni çözer.

Kural (IZN-PLAN §3 + B3 notu; kapı kararıyla AYNI çözücüler):

* Sistem Yöneticisi → hiçbir şey gizli değil.
* Proje bağlamlı uç (`projects.context.request_project`): kişi o projenin EKİBİNDE ise O PROJEDEKİ
  rolün bayrakları, değilse ana rolün.
* `all_projects` kişi → ana rol (ekip satırı yok sayılır, `page_gate.team_roles` boş döner).
* Şirket geneli uç (proje çözülmez) → ana rol.
* Çok proje LİSTESİ: `project_id` taşıyan satır KENDİ projesindeki rolle maskelenir
  (`MaskeKumeleri.proje_basina`); `project_id` taşımayan satır isteğin varsayılanıyla.

Bu dosya `permissions`/`page_gate` ile AYNI çözücüleri kullanır: kapı ile maske arasında iki ayrı
"etkin rol" tanımı DOĞAMAZ. Yerel import döngüyü önler (`projects.context` modeller çeker).
"""

from __future__ import annotations

import uuid

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.field_mask import BOS_KUMELER, MaskeKumeleri
from app.core.page_gate import is_admin_role, team_roles
from app.core.sayfalar import HiddenCategory
from app.modules.roles.models import RoleHiddenField


async def role_hidden_sets(
    session: AsyncSession, role_ids: set[uuid.UUID]
) -> dict[uuid.UUID, frozenset[HiddenCategory]]:
    """Rolların gizli kategorileri — TEK sorgu (satırı olmayan rol: boş küme = hiçbiri gizli)."""
    out: dict[uuid.UUID, set[HiddenCategory]] = {role_id: set() for role_id in role_ids}
    if not role_ids:
        return {}
    rows = await session.execute(
        select(RoleHiddenField.role_id, RoleHiddenField.category).where(
            RoleHiddenField.role_id.in_(role_ids)
        )
    )
    for role_id, category in rows.all():
        out[role_id].add(category)
    return {role_id: frozenset(categories) for role_id, categories in out.items()}


async def kumeleri_coz(
    session: AsyncSession, user: object, request: Request | None
) -> MaskeKumeleri:
    """Kullanıcı + istek için maske kümeleri. Sorgu sayısı sabit (≤ 3), rol sayısından bağımsız."""
    if await is_admin_role(session, user):
        return BOS_KUMELER
    from app.modules.projects.context import request_project  # döngüyü önler

    main_id = user.role_id  # type: ignore[attr-defined]
    team = await team_roles(session, user)  # "Tüm projeler" kişide BOŞ
    sets = await role_hidden_sets(session, {main_id, *team.values()})
    main = sets[main_id]
    per_project = {project_id: sets[role_id] for project_id, role_id in team.items()}
    project_id = await request_project(session, request) if request is not None else None
    default = per_project.get(project_id, main) if project_id is not None else main
    return MaskeKumeleri(varsayilan=default, proje_basina=per_project)

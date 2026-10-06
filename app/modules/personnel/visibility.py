"""Personel okuma yollarında PROJE GÖRÜNÜRLÜĞÜ (IZN-B5a, 23a okuma tarafı).

Atama yazması görünmeyen projeyi 404 ile kapatır; aynı bilgi okumadan da sızmamalıdır
(`assigned_project_id`, `?project_id=` süzgeci, özet uçtaki proje adı). Kural: aktörün
GÖREMEDİĞİ projeye ait atama alanları `null` döner; görünmeyen projeyle süzmek BOŞ sonuç verir
(404 değil: var/yok sızmaz). Sistem Yöneticisi ve "Tüm projeler" kişisi `visible_projects`
gereği hepsini görür. İstek başına TEK `visible_projects` çağrısı yapılır (N+1 yok).
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.personnel.schemas import PersonnelResponse
from app.modules.projects import service as projects_service
from app.modules.users.models import User

VisibleIds = frozenset[uuid.UUID]


async def visible_project_ids(session: AsyncSession, actor: User) -> VisibleIds:
    return frozenset(p.id for p in await projects_service.visible_projects(session, actor))


def filter_hidden(project_id: uuid.UUID | None, visible: VisibleIds) -> bool:
    """`True` = istenen proje süzgeci aktöre görünmüyor → sonuç BOŞ olmalı."""
    return project_id is not None and project_id not in visible


def hide_assignment(item: PersonnelResponse, visible: VisibleIds) -> PersonnelResponse:
    """Görünmeyen projeye atamayı (proje + bölüm) `null`a çeker; YENİ nesne döner."""
    if item.assigned_project_id is None or item.assigned_project_id in visible:
        return item
    return item.model_copy(update={"assigned_project_id": None, "assigned_section_id": None})

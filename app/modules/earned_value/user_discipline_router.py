"""Kullanici disiplin atamasi uclari (DSC-B0, spec Ü9): `/users/{user_id}/disciplines`.

Yol `users` altindadir ama modul PLANLAMA (EV) olur — cekirdek `users` EV'yi import etmez
(§2.7). Izin kapisi kullanici yonetimidir (`project-access` emsali): GET `view`, PUT `full`.
Bu uclar disipline DUYARLI DEGILDIR (kullanici yonetimi) — rota bekcisi disinda tutulur.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.core.ratelimit import client_ip
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.earned_value import audit_messages, discipline_adapter, user_discipline_service
from app.modules.earned_value.schemas_catalog import UserDisciplinesInput, UserDisciplinesRead
from app.modules.earned_value.user_discipline_service import DisciplineAssignment
from app.modules.users.models import User

# Disiplin kapsami portuna kayit — `catalog_router`in kaydina ek guvence (idempotent).
discipline_adapter.register()

router = APIRouter(
    prefix="/users", tags=["earned-value", "users"], responses=COMMON_ERROR_RESPONSES
)


def _read(assignment: DisciplineAssignment) -> UserDisciplinesRead:
    return UserDisciplinesRead(
        discipline_ids=assignment.discipline_ids, disciplines=assignment.disciplines
    )


@router.get(
    "/{user_id}/disciplines",
    response_model=UserDisciplinesRead,
    dependencies=[require_permission("user_management", AccessLevel.view)],
)
async def get_user_disciplines_endpoint(
    user_id: uuid.UUID,
    session: DbSession,
) -> UserDisciplinesRead:
    return _read(await user_discipline_service.get_assignment(session, user_id))


@router.put(
    "/{user_id}/disciplines",
    response_model=UserDisciplinesRead,
    dependencies=[require_permission("user_management", AccessLevel.full)],
)
async def set_user_disciplines_endpoint(
    request: Request,
    user_id: uuid.UUID,
    data: UserDisciplinesInput,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UserDisciplinesRead:
    assignment = await user_discipline_service.replace_assignment(
        session, user_id, data.discipline_ids
    )
    if assignment.changed:  # ayni kume tekrar gelirse denetim gurultusu yazilmaz
        await record_audit(
            session,
            action=AuditAction.update,
            detail=audit_messages.user_disciplines_updated(
                assignment.user.full_name, assignment.codes
            ),
            actor_user_id=current_user.id,
            ip_address=client_ip(request),
        )
    return _read(assignment)

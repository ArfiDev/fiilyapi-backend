"""BLF-B1 — sirket geneli bolum tipi uclari (`GET`/`POST /section-types`).

Okuma `sites:view`. Ekleme (IZN-B5c) sayfa kapisidir: `santiye.bolumler` VEYA `bolum.detay`
Duzenler'i (= bolum formunun iki hali: olusturma + duzenleme; tip secici ikisinde de vardir).
Proje baglami YOKTUR (sirket geneli). Silme / yeniden adlandirma ucu YOKTUR.

Hassas alan maskesi (`MaskeRotasi`) `router.py` ile AYNIDIR; yanit hassas alan tasimaz.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_pages, require_permission
from app.core.ratelimit import client_ip
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.sites.schemas import SectionTypeConflict, SectionTypeCreate, SectionTypeRead
from app.modules.sites.service import section_types as service
from app.modules.users.models import User

router = APIRouter(
    tags=["sites"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=MaskeRotasi,
)

_VIEW = require_permission("sites", AccessLevel.view)
_YAZ = require_pages(("santiye.bolumler", "bolum.detay"), "edit")


@router.get("/section-types", response_model=list[SectionTypeRead], dependencies=[_VIEW])
async def list_section_types_endpoint(session: DbSession) -> list[SectionTypeRead]:
    rows = await service.list_section_types(session)
    return [SectionTypeRead.model_validate(row) for row in rows]


@router.post(
    "/section-types",
    response_model=SectionTypeRead,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_409_CONFLICT: {"model": SectionTypeConflict}},
    dependencies=[_YAZ],
)
async def create_section_type_endpoint(
    request: Request,
    data: SectionTypeCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SectionTypeRead:
    section_type = await service.create_section_type(session, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.section_type_created(section_type.name),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    return SectionTypeRead.model_validate(section_type)

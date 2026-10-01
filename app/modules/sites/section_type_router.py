"""BLF-B1 — sirket geneli bolum tipi uclari (`GET`/`POST /section-types`).

Izin modulu `sites`tir, AYRI modul ACILMAZ (izin isleri en son). Okuma `view`,
ekleme bolum OLUSTURMA ucuyla (`POST /sites/{id}/sections`) AYNI kapidir (`full`):
bolum olusturabilen tip ekleyebilir. Silme / yeniden adlandirma ucu YOKTUR.

Kapsam maskesi cifti (`route_class` + `kapsam_kapisi`) `router.py` ile AYNIDIR;
yanit para alani tasimaz ama `tests/core/test_kapsam_baglantisi.py` kisitli izinle
korunan HER routerin baglanmasini sart kosar.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import kapsam_kapisi, require_permission
from app.core.ratelimit import client_ip
from app.core.scoped_route import kapsam_rotasi, kapsamdan_oku
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.sites.schemas import SectionTypeConflict, SectionTypeCreate, SectionTypeRead
from app.modules.sites.service import section_types as service
from app.modules.users.models import User

router = APIRouter(
    tags=["sites"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=kapsam_rotasi("sites", kapsamdan_oku),
    dependencies=[kapsam_kapisi("sites")],
)

_VIEW = require_permission("sites", AccessLevel.view)
# `router.py::create_section_endpoint` kapisinin (`_FULL`) AYNISI — tek kaynak o.
_FULL = require_permission("sites", AccessLevel.full)


@router.get("/section-types", response_model=list[SectionTypeRead], dependencies=[_VIEW])
async def list_section_types_endpoint(session: DbSession) -> list[SectionTypeRead]:
    rows = await service.list_section_types(session)
    return [SectionTypeRead.model_validate(row) for row in rows]


@router.post(
    "/section-types",
    response_model=SectionTypeRead,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_409_CONFLICT: {"model": SectionTypeConflict}},
    dependencies=[_FULL],
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

"""Teklif → proje DONUSTURME ucu (TKL-B6.2).

`POST /offers/{offer_id}/convert` — kazanilan tekliften TEK islemde proje + sozlesme + (istege
bagli) santiye + tam dagitim + adam-saat tohumu. Is mantigi `convert_service.py`dedir.

## Neden AYRI router, kapsam maskesi YOK
Uc iki KISITLI izni birlestirir (`projects:admin` + `contracts:full`): tek `route_class` tek kapsam
anahtari tasir, yani bu router maskeye BAGLANAMAZ. Kapsam bekcisinin (`test_kapsam_baglantisi`)
sarti bu durumda sudur: router maskelenecek alan DONDURMEMELIDIR. Bu yuzden YANIT PARASIZDIR
(kimlikler, sayac, uyarilar) — bir gun yanita `Decimal` eklenirse bekci kirmizi verir.

## Izin (SO-42 — GORUNUR NOT)
`projects:admin` (yeni projeyi gorebilmenin teknik on kosulu; proje olusturma emsali) +
`contracts:full` + `RequireUnrestricted` (toplu/yapisal islem). Bugun seed matrisinde `projects:
admin` YALNIZ `system_admin`dedir → donusturmeyi yalniz sistem yoneticisi yapabilir; patron 403.
Izin matrisine DOKUNULMADI (izin turu).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_deps import RequireUnrestricted
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.core.ratelimit import client_ip
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.offers import convert_service
from app.modules.offers.convert_schemas import (
    ConvertRequest,
    ConvertResponse,
    ConvertValidationErrorOut,
)
from app.modules.users.models import User

router = APIRouter(tags=["offers"], responses=COMMON_ERROR_RESPONSES)

_PERMISSIONS = [
    require_permission("projects", AccessLevel.admin),
    require_permission("contracts", AccessLevel.full),
    RequireUnrestricted,
]


@router.post(
    "/offers/{offer_id}/convert",
    response_model=ConvertResponse,
    dependencies=_PERMISSIONS,
    responses={
        409: {"description": "Zaten dönüştürüldü / kazanılmamış / proje kodu kullanılıyor"},
        422: {
            "model": ConvertValidationErrorOut,
            "description": "Doğrulama hatası; servis doğrulamasında `errors[].loc` yapısaldır",
        },
    },
)
async def convert_offer_endpoint(
    request: Request,
    offer_id: Annotated[uuid.UUID, Path()],
    data: ConvertRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ConvertResponse:
    """Kazanilan (son revizyonu `won`) teklifi projeye donusturur; ikinci cagri 409."""
    result = await convert_service.convert_offer(session, user, offer_id, data)
    ip = client_ip(request)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.project_created(result.project.name),
        actor_user_id=user.id,
        ip_address=ip,
    )
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.offer_converted(
            result.offer.offer_no,
            result.project.code,
            result.item_count,
            result.site.code if result.site is not None else None,
        ),
        actor_user_id=user.id,
        ip_address=ip,
    )
    return ConvertResponse(
        project_id=result.project.id,
        project_slug=result.project.slug,
        project_code=result.project.code,
        site_id=result.site.id if result.site is not None else None,
        contract_item_count=result.item_count,
        warnings=result.warnings,
    )

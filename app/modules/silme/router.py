import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import SYSTEM_ADMIN_ONLY_DETAIL, require_system_admin
from app.core.ratelimit import client_ip
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.silme import service
from app.modules.silme.params import DELETE_WITH_PREVIEW_RESPONSES, PreviewTokenQuery
from app.modules.silme.schemas import DeleteKind, DeletePreviewResponse
from app.modules.users.models import User

router = APIRouter(
    route_class=MaskeRotasi,
    prefix="/admin/silme",
    tags=["silme"],
    responses={
        **COMMON_ERROR_RESPONSES,
        403: {"description": SYSTEM_ADMIN_ONLY_DETAIL},
    },
    dependencies=[require_system_admin()],
)


@router.get(
    "/{kind}/{record_id}/onizleme",
    response_model=DeletePreviewResponse,
    summary="Silme önizlemesi: kayıtla birlikte silinecek bağlı kayıtlar",
    responses={404: {"description": "Kayıt bulunamadı (ör. `Şantiye bulunamadı`)"}},
)
async def preview_delete_endpoint(
    kind: DeleteKind, record_id: uuid.UUID, session: DbSession
) -> DeletePreviewResponse:
    """Yalnız Sistem Yöneticisi. SİLMEZ; yazma yapmaz.

    Önizleme ile `DELETE` AYNI çözücüyü kullanır: burada sayılan ağaç, silinecek ağaçtır.
    `groups` boşsa kaydın bağlı kaydı yoktur (yine de onay istenir).
    """
    return await service.onizle(session, kind, record_id)


@router.delete(
    "/{kind}/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Kaydı bağlı kayıtlarıyla birlikte sil",
    responses={
        404: {"description": "Kayıt bulunamadı (ör. `Şantiye bulunamadı`)"},
        **DELETE_WITH_PREVIEW_RESPONSES,
    },
)
async def delete_with_dependents_endpoint(
    request: Request,
    kind: DeleteKind,
    record_id: uuid.UUID,
    actor: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    preview_token: PreviewTokenQuery = None,
) -> None:
    """Yalnız Sistem Yöneticisi, HER KOŞULDA. Kök kaydı ve bağlı ağacını TEK işlemde siler.

    Kök `FOR UPDATE` kilitlenir, ağaç yeniden hesaplanır; karması `preview_token`la
    uyuşmazsa HİÇBİR ŞEY silinmez ve 409 `preview_stale` döner. Denetim günlüğüne tek satır
    yazılır (kim, ne, kaç bağlı kayıt).
    """
    detail = await service.sil(session, kind, record_id, preview_token)
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=detail,
        actor_user_id=actor.id,
        ip_address=client_ip(request),
    )

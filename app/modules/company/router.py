from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)

from app.core.access import AccessLevel
from app.core.config import settings
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.errors import NotFoundError
from app.core.openapi import COMMON_ERROR_RESPONSES, DELETE_403_YANITI
from app.core.permissions import require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.company import service
from app.modules.company.schemas import CompanyRead, CompanyUpdate
from app.modules.users.models import User

router = APIRouter(prefix="/company", tags=["company"], responses=COMMON_ERROR_RESPONSES)


@router.get("", response_model=CompanyRead)
async def get_company_endpoint(
    _user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> CompanyRead:
    company = await service.get_company(session)
    return CompanyRead.from_model(company)


@router.put(
    "",
    response_model=CompanyRead,
    dependencies=[require_permission("settings", AccessLevel.full)],
)
async def update_company_endpoint(
    request: Request,
    data: CompanyUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> CompanyRead:
    company = await service.update_company(session, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.COMPANY_UPDATED,
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    return CompanyRead.from_model(company)


@router.post(
    "/logo",
    response_model=CompanyRead,
    responses={
        413: {"description": "Logo boyutu cok buyuk"},
        422: {"description": "Gecersiz logo bicimi veya icerigi"},
    },
    dependencies=[require_permission("settings", AccessLevel.full)],
)
async def upload_logo_endpoint(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    file: Annotated[UploadFile, File(...)],
) -> CompanyRead:
    if file.content_type not in settings.allowed_logo_content_type_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Desteklenmeyen logo bicimi (izinli: PNG, JPEG, SVG, WEBP)",
        )
    max_bytes = settings.logo_max_bytes
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(65536):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Logo boyutu cok buyuk (en fazla 1 MB)",
            )
        chunks.append(chunk)
    content = b"".join(chunks)
    if not service.logo_signature_matches(file.content_type, content):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Logo icerigi bildirilen bicimle uyusmuyor",
        )
    company = await service.set_logo(session, file.content_type, file.filename, content)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.COMPANY_LOGO_UPDATED,
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    return CompanyRead.from_model(company)


@router.get("/logo")
async def get_logo_endpoint(
    _user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> Response:
    company = await service.get_company(session)
    if company.logo_data is None:
        raise NotFoundError("Logo yuklenmemis")
    safe_filename = service.safe_logo_filename(company.logo_filename)
    return Response(
        content=company.logo_data,
        media_type=company.logo_content_type or "application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
        },
    )


@router.delete(
    "/logo",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**DELETE_403_YANITI},
    dependencies=[require_system_admin()],
)
async def delete_logo_endpoint(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Şirket logosunu kaldırır. YALNIZ Sistem Yöneticisi."""
    await service.clear_logo(session)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.COMPANY_LOGO_REMOVED,
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )

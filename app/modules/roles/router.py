import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.errors import NotFoundError
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_page, require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.roles import repository, service
from app.modules.roles.schemas import (
    ModuleResponse,
    PermissionCell,
    PermissionUpdate,
    RoleCopy,
    RoleCreate,
    RolePagesResponse,
    RolePagesUpdate,
    RoleRename,
    RoleResponse,
)
from app.modules.users.models import User

router = APIRouter(tags=["roles"], responses=COMMON_ERROR_RESPONSES)


@router.get(
    "/roles",
    response_model=list[RoleResponse],
    dependencies=[require_permission("user_management", AccessLevel.view)],
)
async def list_roles_endpoint(
    session: DbSession,
) -> list[RoleResponse]:
    return await service.role_responses(session, await repository.list_roles(session))


@router.get(
    "/modules",
    response_model=list[ModuleResponse],
    dependencies=[require_permission("user_management", AccessLevel.view)],
)
async def list_modules_endpoint(
    session: DbSession,
) -> list[ModuleResponse]:
    return [ModuleResponse.model_validate(m) for m in await repository.list_modules(session)]


@router.get(
    "/roles/{role_id}/permissions",
    response_model=list[PermissionCell],
    dependencies=[require_permission("user_management", AccessLevel.view)],
)
async def get_role_permissions_endpoint(
    role_id: uuid.UUID,
    session: DbSession,
) -> list[PermissionCell]:
    """KALDIRILACAK (B6): modül düzeyi SAYFA HÜCRELERİNDEN türetilmiş salt-okur görünümdür."""
    role = await repository.get_role(session, role_id)
    if role is None:
        raise NotFoundError("Rol bulunamadı")
    matrix = await repository.derived_role_matrix(session, role_id, role.key)
    return [
        PermissionCell(module_key=module.key, access_level=level, scope=scope)
        for module, level, scope in matrix
    ]


@router.post(
    "/roles",
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_page("ayarlar.rol_yonetimi", "edit")],
)
async def create_role_endpoint(
    request: Request,
    data: RoleCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> RoleResponse:
    role = await service.create_custom_role(session, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.role_created(role.name),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    return (await service.role_responses(session, [role]))[0]


@router.patch(
    "/roles/{role_id}",
    response_model=RoleResponse,
    dependencies=[require_page("ayarlar.rol_yonetimi", "edit")],
)
async def rename_role_endpoint(
    request: Request,
    role_id: uuid.UUID,
    data: RoleRename,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> RoleResponse:
    # Eski ad yeniden adlandirmadan ONCE okunmali; sonra okunursa yeni ad iki kez yazilir.
    existing = await repository.get_role(session, role_id)
    old_name = existing.name if existing is not None else ""
    role = await service.rename_role(session, role_id, data.name, data.emoji, data.description)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.role_renamed(old_name, role.name),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    return (await service.role_responses(session, [role]))[0]


#: 410 gövdesi: eski modül hücresi yazma ucu kalktı (IZN-B2). Mesaj yeni ekranı işaret eder.
PERMISSION_WRITE_GONE_DETAIL = (
    "Modül bazlı izin matrisi kaldırıldı. İzinleri Ayarlar > Sayfa İzinleri ekranından "
    "(PUT /roles/{id}/pages) düzenleyin."
)


@router.put(
    "/roles/{role_id}/permissions/{module_key}",
    deprecated=True,
    status_code=status.HTTP_410_GONE,
    response_model=None,
    responses={
        status.HTTP_410_GONE: {
            "description": "Uç kaldırıldı: izinler artık sayfa bazlı (PUT /roles/{id}/pages)"
        }
    },
    dependencies=[require_permission("user_management", AccessLevel.admin)],
)
async def update_permission_endpoint(
    role_id: uuid.UUID,
    module_key: str,
    data: PermissionUpdate,
) -> None:
    """KALDIRILDI (IZN-B2): her çağrı 410 döner, hiçbir şey yazılmaz.

    Eski modül hücreleri DONDURULDU; kapılar sayfa hücrelerinden karar verir. Gövde şeması
    yalnız istemci tiplerinin kırılmaması için durur (B6'da uç ve şema birlikte sökülür).
    """
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=PERMISSION_WRITE_GONE_DETAIL)


@router.get(
    "/roles/{role_id}/pages",
    response_model=RolePagesResponse,
    dependencies=[require_permission("user_management", AccessLevel.view)],
)
async def get_role_pages_endpoint(
    role_id: uuid.UUID,
    session: DbSession,
) -> RolePagesResponse:
    """Bir rolün sayfa izinleri + gizli alanları (Sayfa İzinleri ekranı). Rol yoksa 404."""
    return await service.get_role_pages(session, role_id)


@router.put(
    "/roles/{role_id}/pages",
    response_model=RolePagesResponse,
    dependencies=[require_page("ayarlar.sayfa_izinleri", "edit")],
)
async def update_role_pages_endpoint(
    request: Request,
    role_id: uuid.UUID,
    data: RolePagesUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> RolePagesResponse:
    """Sayfa İzinleri "Kaydet": TAM matris (100 sayfa) + gizli alan kümesi, TEK transaction.

    403: Sistem Yöneticisi rolü kilitli · 404: rol yok · 422: eksik sayfa, bilinmeyen sayfa
    anahtarı ya da kategori, `approve=true` onay eylemi olmayan sayfada ya da `level=none`
    iken. Reddedilen istek hiçbir şey yazmaz ve denetim satırı ÜRETMEZ; değişmeyen kısım için
    da satır üretilmez (sayfa değişikliği ve gizli alan değişikliği ayrı satırlardır).
    """
    change = await service.update_role_pages(session, role_id, data.pages, data.hidden_fields)
    if change.page_changes:
        await record_audit(
            session,
            action=AuditAction.update,
            detail=messages.role_pages_updated(change.role.name, change.page_changes),
            actor_user_id=current_user.id,
            ip_address=client_ip(request),
        )
    if change.hidden_changed:
        await record_audit(
            session,
            action=AuditAction.update,
            detail=messages.role_hidden_fields_updated(
                change.role.name, [messages.HIDDEN_CATEGORY_LABELS[c] for c in change.hidden_fields]
            ),
            actor_user_id=current_user.id,
            ip_address=client_ip(request),
        )
    return await service.get_role_pages(session, role_id)


@router.post(
    "/roles/{role_id}/copy",
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_page("ayarlar.rol_yonetimi", "edit")],
)
async def copy_role_endpoint(
    request: Request,
    role_id: uuid.UUID,
    data: RoleCopy,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> RoleResponse:
    """Kaynak rolün sayfa hücreleri + gizli alanlarıyla yeni rol (anahtar addan türetilir).

    404: kaynak rol yok. Kilit ve silinemezlik kopyalanmaz (yeni rol `is_system=false`).
    """
    source = await repository.get_role(session, role_id)
    source_name = source.name if source is not None else ""
    role = await service.copy_role(session, role_id, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.role_copied(source_name, role.name),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    return (await service.role_responses(session, [role]))[0]


@router.delete(
    "/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_system_admin()],
)
async def delete_role_endpoint(
    request: Request,
    role_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    # Ad silmeden ONCE okunmali; sonra okunursa satir yoktur.
    existing = await repository.get_role(session, role_id)
    deleted_name = existing.name if existing is not None else ""
    await service.delete_role(session, role_id)  # rol yoksa/kilitliyse istisna firlatir
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=messages.role_deleted(deleted_name),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )

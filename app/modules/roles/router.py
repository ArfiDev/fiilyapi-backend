import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.errors import NotFoundError
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.core.ratelimit import client_ip
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.roles import repository, service
from app.modules.roles.schemas import (
    ModuleResponse,
    PermissionCell,
    PermissionUpdate,
    RoleCreate,
    RoleRename,
    RoleResponse,
)
from app.modules.roles.service import update_role_permission
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
    return [RoleResponse.model_validate(r) for r in await repository.list_roles(session)]


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
    if await repository.get_role(session, role_id) is None:
        raise NotFoundError("Rol bulunamadı")
    matrix = await repository.get_role_matrix(session, role_id)
    return [
        PermissionCell(module_key=module.key, access_level=perm.access_level, scope=perm.scope)
        for module, perm in matrix
    ]


@router.post(
    "/roles",
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_permission("user_management", AccessLevel.admin)],
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
    return RoleResponse.model_validate(role)


@router.patch(
    "/roles/{role_id}",
    response_model=RoleResponse,
    dependencies=[require_permission("user_management", AccessLevel.admin)],
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
    return RoleResponse.model_validate(role)


@router.put(
    "/roles/{role_id}/permissions/{module_key}",
    response_model=PermissionCell,
    dependencies=[require_permission("user_management", AccessLevel.admin)],
)
async def update_permission_endpoint(
    request: Request,
    role_id: uuid.UUID,
    module_key: str,
    data: PermissionUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PermissionCell:
    # system_admin -> PermissionLockedError(403); satir/rol yok -> NotFoundError(404)
    # Reddedilen degisiklik denetim satiri URETMEZ: istisna asagidaki koda hic ulasmaz.
    perm = await update_role_permission(session, role_id, module_key, data.access_level, data.scope)
    # Adlar islem sirasinda degismedigi icin sonrasinda okunmalari guvenli.
    role = await repository.get_role(session, role_id)
    module = await repository.get_module(session, module_key)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.permission_changed(
            role.name if role is not None else "",
            module.name if module is not None else module_key,
            perm.access_level,
        ),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    return PermissionCell(module_key=module_key, access_level=perm.access_level, scope=perm.scope)


@router.delete(
    "/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_permission("user_management", AccessLevel.admin)],
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

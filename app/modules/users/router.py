import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.errors import NotFoundError
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.roles.models import Role
from app.modules.users import repository, service
from app.modules.users.models import User
from app.modules.users.schemas import (
    PasswordReset,
    ProjectAccessInput,
    ProjectAccessResponse,
    UserCreate,
    UserListResponse,
    UserResponse,
    UserUpdate,
)

router = APIRouter(prefix="/users", tags=["users"], responses=COMMON_ERROR_RESPONSES)


@router.get(
    "",
    response_model=UserListResponse,
    dependencies=[require_permission("user_management", AccessLevel.view)],
)
async def list_users_endpoint(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UserListResponse:
    users = await repository.list_users(session, limit=limit, offset=offset)
    total = await repository.count_users(session)
    return UserListResponse(
        items=[UserResponse.model_validate(u) for u in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    dependencies=[require_permission("user_management", AccessLevel.view)],
)
async def get_user_endpoint(
    user_id: uuid.UUID,
    session: DbSession,
) -> UserResponse:
    user = await repository.get_user(session, user_id)
    if user is None:
        raise NotFoundError("Kullanıcı bulunamadı")
    return UserResponse.model_validate(user)


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_permission("user_management", AccessLevel.full)],
)
async def create_user_endpoint(
    request: Request,
    data: UserCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UserResponse:
    user = await service.create_user(session, current_user, data)
    # Rol servis katmaninda dogrulanirken kimlik haritasina girdigi icin ek sorgu cikmaz.
    role = await session.get(Role, user.role_id)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.user_created(user.full_name, role.name if role else ""),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    return UserResponse.model_validate(user)


@router.patch(
    "/{user_id}",
    response_model=UserResponse,
    dependencies=[require_permission("user_management", AccessLevel.full)],
)
async def update_user_endpoint(
    request: Request,
    user_id: uuid.UUID,
    data: UserUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UserResponse:
    user = await service.update_user(session, current_user, user_id, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.user_updated(user.full_name),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    return UserResponse.model_validate(user)


@router.patch(
    "/{user_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_permission("user_management", AccessLevel.admin)],
)
async def reset_password_endpoint(
    request: Request,
    user_id: uuid.UUID,
    data: PasswordReset,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    await service.set_user_password(session, user_id, data.new_password)
    target = await repository.get_user(session, user_id)
    await record_audit(
        session,
        action=AuditAction.update,
        # Yeni parola metne ASLA girmez (plan §Yanit govdesi).
        detail=messages.password_reset(target.full_name if target else ""),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_system_admin()],
)
async def delete_user_endpoint(
    request: Request,
    user_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    # Ad silmeden ONCE okunmali; sonra okunursa satir yoktur.
    target = await repository.get_user(session, user_id)
    deleted_name = target.full_name if target is not None else ""
    await service.delete_user(session, user_id)  # kullanici yoksa 404 firlatir
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=messages.user_deleted(deleted_name),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )


@router.put(
    "/{user_id}/project-access",
    response_model=ProjectAccessResponse,
    dependencies=[require_permission("user_management", AccessLevel.full)],
)
async def set_project_access_endpoint(
    request: Request,
    user_id: uuid.UUID,
    data: ProjectAccessInput,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ProjectAccessResponse:
    rows = await service.set_project_access(session, user_id, data)
    target = await repository.get_user(session, user_id)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.project_access_updated(target.full_name if target else ""),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    all_projects = any(r.all_projects for r in rows)
    project_ids = [r.project_id for r in rows if r.project_id is not None]
    return ProjectAccessResponse(all_projects=all_projects, project_ids=project_ids)


@router.get(
    "/{user_id}/project-access",
    response_model=ProjectAccessResponse,
    dependencies=[require_permission("user_management", AccessLevel.view)],
)
async def get_project_access_endpoint(
    user_id: uuid.UUID,
    session: DbSession,
) -> ProjectAccessResponse:
    rows = await repository.get_project_access(session, user_id)
    all_projects = any(r.all_projects for r in rows)
    project_ids = [r.project_id for r in rows if r.project_id is not None]
    return ProjectAccessResponse(all_projects=all_projects, project_ids=project_ids)

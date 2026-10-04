import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.errors import NotFoundError
from app.core.openapi import COMMON_ERROR_RESPONSES, DELETE_403_YANITI
from app.core.permissions import require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.roles.models import Role
from app.modules.users import access_service, repository, service
from app.modules.users.models import User
from app.modules.users.schemas import (
    PasswordReset,
    UserAccessInput,
    UserAccessResponse,
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
    q: Annotated[
        str | None,
        Query(
            max_length=100,
            description="Ad, e-posta ya da ana rol adında ara (büyük/küçük harf ve Türkçe "
            "karakter duyarsız; `İ ı I i` aynı sayılır). Boş = süzgeç yok.",
        ),
    ] = None,
) -> UserListResponse:
    users = await repository.list_users(session, limit=limit, offset=offset, q=q)
    total = await repository.count_users(session, q=q)
    return UserListResponse(
        items=await service.user_responses(session, users),
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
    return (await service.user_responses(session, [user]))[0]


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
    return (await service.user_responses(session, [user]))[0]


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
    return (await service.user_responses(session, [user]))[0]


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
    responses={
        **DELETE_403_YANITI,
        400: {"description": "Son aktif Sistem Yöneticisi silinemez"},
        409: {"description": "İz bırakmış kullanıcı silinemez (veri bütünlüğü; `code` yok)"},
    },
    dependencies=[require_system_admin()],
)
async def delete_user_endpoint(
    request: Request,
    user_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    # Ad silmeden ONCE okunmali; sonra okunursa satir yoktur.
    """Kullanıcıyı siler. YALNIZ Sistem Yöneticisi.

    Son aktif Sistem Yöneticisi silinemez (**400**). İz bırakmış kullanıcı (12 tabloda RESTRICT) DB
    kısıtı nedeniyle **409** `Veri bütünlüğü hatası` alır; anonimleştirme yolu SIL-B3'tedir.
    """
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


#: 410 gövdesi: eski proje erişimi uçları kalktı (IZN-B3). Mesaj yeni ucu işaret eder.
PROJECT_ACCESS_GONE_DETAIL = (
    "Proje erişimi artık proje ekibi olarak yönetilir. Ayarlar > Kullanıcılar ekranından "
    "(GET/PUT /users/{id}/access) düzenleyin."
)

_PROJECT_ACCESS_GONE_RESPONSES = {
    status.HTTP_410_GONE: {
        "description": "Uç kaldırıldı: erişim artık ana rol + proje ekibi (`/users/{id}/access`)"
    }
}


@router.put(
    "/{user_id}/project-access",
    deprecated=True,
    status_code=status.HTTP_410_GONE,
    response_model=None,
    responses=_PROJECT_ACCESS_GONE_RESPONSES,
    dependencies=[require_permission("user_management", AccessLevel.full)],
)
async def set_project_access_endpoint(user_id: uuid.UUID) -> None:
    """KALDIRILDI (IZN-B3): her çağrı 410 döner, hiçbir şey yazılmaz."""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=PROJECT_ACCESS_GONE_DETAIL)


@router.get(
    "/{user_id}/project-access",
    deprecated=True,
    status_code=status.HTTP_410_GONE,
    response_model=None,
    responses=_PROJECT_ACCESS_GONE_RESPONSES,
    dependencies=[require_permission("user_management", AccessLevel.view)],
)
async def get_project_access_endpoint(user_id: uuid.UUID) -> None:
    """KALDIRILDI (IZN-B3): her çağrı 410 döner."""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=PROJECT_ACCESS_GONE_DETAIL)


@router.get(
    "/{user_id}/access",
    response_model=UserAccessResponse,
    dependencies=[require_permission("user_management", AccessLevel.view)],
)
async def get_user_access_endpoint(user_id: uuid.UUID, session: DbSession) -> UserAccessResponse:
    """Kullanıcının ana rolü, "Tüm projeler" işareti ve proje ekibi (rol + disiplin).

    `all_projects=true` kişide `projects` boştur. Kapı: Kullanıcılar sayfası Görür.
    """
    return await access_service.get_access(session, user_id)


@router.put(
    "/{user_id}/access",
    response_model=UserAccessResponse,
    responses={
        400: {"description": "Son aktif Sistem Yöneticisi düşürülemez"},
        403: {
            "description": "Atanan rol aktörün yetkilerini aşıyor / Sistem Yöneticisi rolünü "
            "yalnız Sistem Yöneticisi atar"
        },
        404: {"description": "Kullanıcı ya da rol bulunamadı"},
        422: {
            "description": "`all_projects=true` iken ekip dolu · aynı proje iki kez · bilinmeyen "
            "proje/rol/disiplin · proje rolü Sistem Yöneticisi"
        },
    },
    dependencies=[require_permission("user_management", AccessLevel.full)],
)
async def set_user_access_endpoint(
    request: Request,
    user_id: uuid.UUID,
    data: UserAccessInput,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UserAccessResponse:
    """Ana rol + "Tüm projeler" + proje ekibini TEK transaction'da TAM DEĞİŞTİRİR (atomik).

    Kullanıcı satırı `FOR UPDATE` ile kilitlenir; hata → hiçbir şey değişmez. Yanıt GET ile aynı.
    Kapı: Kullanıcılar sayfası Düzenler.
    """
    change = await access_service.replace_access(session, current_user, user_id, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.user_access_updated(
            change.user.full_name,
            change.main_role_name,
            change.response.all_projects,
            len(change.response.projects),
        ),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    return change.response

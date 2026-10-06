import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES, DELETE_403_YANITI
from app.core.page_gate import decide
from app.core.permissions import (
    _DENIED,
    require_page,
    require_pages,
    require_system_admin,
)
from app.core.ratelimit import client_ip
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.roles import repository, service
from app.modules.roles.schemas import (
    RoleCopy,
    RoleCreate,
    RolePagesResponse,
    RolePagesUpdate,
    RoleRename,
    RoleResponse,
)
from app.modules.users.models import User

router = APIRouter(route_class=MaskeRotasi, tags=["roles"], responses=COMMON_ERROR_RESPONSES)

#: IZN-B5a (madde 15): rol uçlarını `ayarlar.kullanicilar` GÖRÜR bitinin açması bir sızıntıydı
#: (rol ekranlarına bağımsız). Rol ayrıntı ucu (`/roles/{id}/pages`) yalnız rol ekranlarının
#: Görür'üyle açılır.
_ROL_EKRANLARI = ("ayarlar.rol_yonetimi", "ayarlar.sayfa_izinleri")
_ROL_EKRANI_GORUR = require_pages(_ROL_EKRANLARI, "view")


async def _rol_listesi_kapisi(
    user: Annotated[User, Depends(get_current_user)], session: DbSession
) -> None:
    """`GET /roles`: rol ekranları Görür VEYA `ayarlar.kullanicilar` DÜZENLER (kullanıcıya rol
    atamak rol listesini gerektirir; salt Görür bunu yapamaz). 403 gövdesi diğer kapılarla aynı."""
    pairs = (*((key, "view") for key in _ROL_EKRANLARI), ("ayarlar.kullanicilar", "edit"))
    if not await decide(session, user, pairs):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DENIED)


@router.get(
    "/roles",
    response_model=list[RoleResponse],
    dependencies=[Depends(_rol_listesi_kapisi)],
)
async def list_roles_endpoint(
    session: DbSession,
) -> list[RoleResponse]:
    return await service.role_responses(session, await repository.list_roles(session))


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


@router.get(
    "/roles/{role_id}/pages",
    response_model=RolePagesResponse,
    dependencies=[_ROL_EKRANI_GORUR],
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
    responses={
        **DELETE_403_YANITI,
        403: {
            "description": "Yalnız Sistem Yöneticisi silebilir; Sistem Yöneticisi rolü silinemez"
        },
        409: {"description": "Role atanmış kullanıcılar var"},
    },
    dependencies=[require_system_admin()],
)
async def delete_role_endpoint(
    request: Request,
    role_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    # Ad silmeden ONCE okunmali; sonra okunursa satir yoktur.
    """Rolü siler. YALNIZ Sistem Yöneticisi.

    Sistem Yöneticisi rolü silinemez (**403**, `Sistem Yöneticisi rolü silinemez`); kullanıcısı olan
    rol **409** (`Bu role atanmış kullanıcılar var; önce onları başka role taşıyın`).
    """
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

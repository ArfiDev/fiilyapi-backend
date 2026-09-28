"""Onay motorunun BES ucu (sozlesme Y5).

```
GET  /approvals                    — onay kutusu
GET  /approvals/settings           — esigi oku
PUT  /approvals/settings           — esigi yaz     [approvals: admin]
GET  /approvals/roles              — tum atamalar  [approvals: admin]
PUT  /approvals/roles/{user_id}    — atama yaz     [approvals: admin]
```

🔴 YENI IZIN MODULU ACILMADI: `approvals` ("Onay Kutusu", ModuleGroup.GENEL)
seed'de ZATEN vardir (`roles/seed_data.py:74,176`) ve matris satiri da mevcuttur.

🔴 ROTA SIRASI TUZAGI DEGERLENDIRILDI ve BU KOKTE YOKTUR: `/approvals/{id}`
BICIMINDE HICBIR ROTA ACILMAMISTIR, dolayisiyla `/approvals/settings` ve
`/approvals/roles` sabit yollarinin UUID sanilmasi YAPISAL OLARAK IMKANSIZDIR.
Kural bir bekci testiyle kilitlidir (`test_modulun_ROTA_KUMESI_tam_olarak_bes_yoldur`).

Zincirin ONAY/RET uclari BURADA DEGILDIR: onlar evraklarin KENDI `/approve`
`/reject` uclarindan gecer (T3) ve o uclarin YOLU KORUNUR — motor yalnizca
anlamlarini devralir.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.errors import NotFoundError
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.core.ratelimit import client_ip
from app.modules.approvals import guards, service
from app.modules.approvals.schemas import (
    ApprovalInboxItem,
    ApprovalInboxResponse,
    ApprovalRoleAssignmentListResponse,
    ApprovalRoleAssignmentRead,
    ApprovalRoleAssignmentUpdate,
    ApprovalSettingsRead,
    ApprovalSettingsUpdate,
)
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.users.models import User

router = APIRouter(prefix="/approvals", tags=["approvals"], responses=COMMON_ERROR_RESPONSES)

_ADMIN = require_permission("approvals", AccessLevel.admin)


@router.get("", response_model=ApprovalInboxResponse)
async def list_my_approvals_endpoint(
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApprovalInboxResponse:
    """Kullaniciya DUSEN siradaki onay adimlari.

    Ayri bir yetki kapisi YOKTUR ve olmamalidir: donen kume zaten "bu adim
    SANA dustu" olgusuyla sinirlidir; `approvals` izni dusuk olan bir rol de
    kendine dusen imzayi gormek zorundadir (matriste sef/saha/IK = `_OWN`).
    """
    views, total, roller = await service.pending_for_user(
        session, current_user, limit=limit, offset=offset
    )
    return ApprovalInboxResponse(
        items=[ApprovalInboxItem.from_view(view) for view in views],
        total=total,
        limit=limit,
        offset=offset,
        my_approval_roles=roller,
    )


@router.get("/settings", response_model=ApprovalSettingsRead)
async def get_approval_settings_endpoint(
    _user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ApprovalSettingsRead:
    """Esik OKUMASI kapisizdir (`GET /company` emsali): ekran, zincirin neden
    Patron adimi tasidigini aciklamak icin esigi bilmek zorundadir."""
    return ApprovalSettingsRead(approval_threshold_try=await service.get_threshold(session))


@router.put("/settings", response_model=ApprovalSettingsRead, dependencies=[_ADMIN])
async def update_approval_settings_endpoint(
    request: Request,
    data: ApprovalSettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ApprovalSettingsRead:
    """Esigi YALNIZ `admin` degistirir (K3).

    🔴 Degisiklik ACIK zincirleri ETKILEMEZ: her zincir kuruldugu andaki esigi
    (ve tutari) KENDI satirinda dondurur (MK-2 kanonu).
    """
    yeni = await service.set_threshold(session, data.approval_threshold_try)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.APPROVAL_THRESHOLD_UPDATED,
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    return ApprovalSettingsRead(approval_threshold_try=yeni)


@router.get("/roles", response_model=ApprovalRoleAssignmentListResponse, dependencies=[_ADMIN])
async def list_approval_role_assignments_endpoint(
    _user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApprovalRoleAssignmentListResponse:
    """EN AZ BIR onay rolu tasiyan kullanicilar. Rolu OLMAYANLAR burada DONMEZ:
    bu uc atamalarin listesidir, kullanici katalogu `GET /users`tur."""
    users, total, atamalar = await service.assignment_page(session, limit=limit, offset=offset)
    return ApprovalRoleAssignmentListResponse(
        items=[
            ApprovalRoleAssignmentRead(
                user_id=user.id,
                full_name=user.full_name,
                email=user.email,
                approval_roles=atamalar.get(user.id, []),
            )
            for user in users
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.put("/roles/{user_id}", response_model=ApprovalRoleAssignmentRead, dependencies=[_ADMIN])
async def set_approval_roles_endpoint(
    request: Request,
    user_id: uuid.UUID,
    data: ApprovalRoleAssignmentUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ApprovalRoleAssignmentRead:
    """Bir kullanicinin onay rollerini TAM KUME olarak yazar (K1).

    Onay rolu HICBIR IZIN VERMEZ: yalnizca zincirde imza adayligidir. Bu yuzden
    burada izin matrisine DOKUNULMAZ ve yeni bir rol/modul acilmaz.
    """
    hedef = await session.get(User, user_id)
    if hedef is None:
        raise NotFoundError(guards.UNKNOWN_USER)
    roller = await service.replace_user_roles(session, user_id, data.approval_roles)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.approval_roles_assigned(hedef.full_name, [rol.value for rol in roller]),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    return ApprovalRoleAssignmentRead(
        user_id=hedef.id,
        full_name=hedef.full_name,
        email=hedef.email,
        approval_roles=roller,
    )

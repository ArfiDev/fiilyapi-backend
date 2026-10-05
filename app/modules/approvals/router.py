"""Onay motorunun yollari (sozlesme Y5 + OKT-B1 gecmis ucu; IZN-B3b rol atama uclari 410).

```
GET  /approvals                    — onay kutusu            (satir basina `can_decide`)
GET  /approvals/history            — onay gecmisi (onaylanan / reddedilen)  (`can_decide`)
GET  /approvals/settings           — esigi oku
PUT  /approvals/settings           — esigi yaz     [ayarlar.onay_rolleri: Duzenler]
GET  /approvals/roles              — 410 (KALDIRILDI)  [ayarlar.onay_rolleri: Duzenler]
PUT  /approvals/roles/{user_id}    — 410 (KALDIRILDI)  [ayarlar.onay_rolleri: Duzenler]
```

🔴 IZN-B3b (K1): onay rolleri PROJE ROLUNDEN gelir. Adim rolu rol adiyla kalir; adimi, belgenin
projesinde o role atanmis kisi (ya da "Tum projeler" + ana rol) onaylar. `user_approval_roles`
tablosu SOKULDU; `GET /approvals/roles` ve `PUT /approvals/roles/{user_id}` B2/B3 emsaliyle 410 +
deprecated YERINDE kalir (eski kapi ayni; B6'da topluca sokulur). Atama Ayarlar > Kullanicilar'da
(`PUT /users/{id}/access`). Yanittaki `my_approval_roles` yerine satir basina `can_decide` gelir.

🔴 YENI IZIN MODULU ACILMADI: `approvals` ("Onay Kutusu", ModuleGroup.GENEL)
seed'de ZATEN vardir (`roles/seed_data.py:74,176`) ve matris satiri da mevcuttur.

🔴 ROTA SIRASI TUZAGI DEGERLENDIRILDI ve BU KOKTE YOKTUR: `/approvals/{id}`
BICIMINDE HICBIR ROTA ACILMAMISTIR, dolayisiyla `/approvals/settings`,
`/approvals/history` ve `/approvals/roles` sabit yollarinin UUID sanilmasi YAPISAL OLARAK
IMKANSIZDIR. Kural bir bekci testiyle kilitlidir
(`test_modulun_ROTA_KUMESI_tam_olarak_alti_yoldur`).

Zincirin ONAY/RET uclari BURADA DEGILDIR: onlar evraklarin KENDI `/approve`
`/reject` uclarindan gecer (T3) ve o uclarin YOLU KORUNUR — motor yalnizca
anlamlarini devralir.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_page
from app.core.ratelimit import client_ip
from app.modules.approvals import service
from app.modules.approvals.definitions import HistoryFilter
from app.modules.approvals.schemas import (
    ApprovalHistoryItem,
    ApprovalHistoryResponse,
    ApprovalInboxItem,
    ApprovalInboxResponse,
    ApprovalSettingsRead,
    ApprovalSettingsUpdate,
)
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.users.models import User

#: 410 gövdesi: onay rolü atama uçları kalktı (IZN-B3b). Mesaj yeni ucu işaret eder.
APPROVAL_ROLES_GONE_DETAIL = (
    "Onay rolleri artık proje rolünden gelir. Kimin hangi adımı onaylayacağını "
    "Ayarlar > Kullanıcılar ekranından (PUT /users/{id}/access) düzenleyin."
)

_APPROVAL_ROLES_GONE_RESPONSES = {
    status.HTTP_410_GONE: {
        "description": "Uç kaldırıldı: onay rolü artık proje rolü (`PUT /users/{id}/access`)"
    }
}

router = APIRouter(
    route_class=MaskeRotasi,
    prefix="/approvals",
    tags=["approvals"],
    responses=COMMON_ERROR_RESPONSES,
)

#: IZN-B2 §2.4: onay eşiği = "Onay Eşiği" (`ayarlar.onay_rolleri`) sayfası DÜZENLER.
_ADMIN = require_page("ayarlar.onay_rolleri", "edit")


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
    views, total = await service.pending_for_user(session, current_user, limit=limit, offset=offset)
    return ApprovalInboxResponse(
        items=[ApprovalInboxItem.from_view(view) for view in views],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/history", response_model=ApprovalHistoryResponse)
async def list_approval_history_endpoint(
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    decision: Annotated[HistoryFilter, Query()] = HistoryFilter.all,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApprovalHistoryResponse:
    """Sonuclanmis zincirler: `decision=approved|rejected|all` (varsayilan `all`).

    Ayri bir yetki kapisi YOKTUR (bekleyen kutusu gibi): gorunurluk "adimlarindan
    birinin onay rolu bende + evragin projesini goruyorum" olgusuyla sinirlidir.
    Ret kaydi zincir SILINMEDIGI icin vardir (OKT-B1); eski (silinmis) retler yoktur.
    """
    views, total = await service.history_for_user(
        session, current_user, decision=decision, limit=limit, offset=offset
    )
    return ApprovalHistoryResponse(
        items=[ApprovalHistoryItem.from_history_view(view) for view in views],
        total=total,
        limit=limit,
        offset=offset,
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


@router.get(
    "/roles",
    deprecated=True,
    status_code=status.HTTP_410_GONE,
    response_model=None,
    responses=_APPROVAL_ROLES_GONE_RESPONSES,
    dependencies=[_ADMIN],
)
async def list_approval_role_assignments_endpoint() -> None:
    """KALDIRILDI (IZN-B3b): her çağrı 410 döner. Onay rolü = proje rolü (K1)."""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=APPROVAL_ROLES_GONE_DETAIL)


@router.put(
    "/roles/{user_id}",
    deprecated=True,
    status_code=status.HTTP_410_GONE,
    response_model=None,
    responses=_APPROVAL_ROLES_GONE_RESPONSES,
    dependencies=[_ADMIN],
)
async def set_approval_roles_endpoint(user_id: uuid.UUID) -> None:
    """KALDIRILDI (IZN-B3b): her çağrı 410 döner, hiçbir şey yazılmaz."""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=APPROVAL_ROLES_GONE_DETAIL)

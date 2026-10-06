"""Teklif SABLONU uclari (TKL-B5.1). Prefix `/offers/templates`.

| uc | kapi |
|----|------|
| okumalar (liste, detay) | `contracts:view` |
| TUM yazmalar | `contracts:full` |

(B4 `router.py` ile ayni gecici T25 deseni ve ayni kapsam cifti. IZN-B3: `RequireUnrestricted`
kalkti — disiplin proje basina, sablonlar sirket geneli.)

🔴 ROTA SIRASI: `/offers/templates` LITERALDIR ve `/offers/{offer_id}` (UUID) ile CAKISIR
(`GET /offers/templates` once dinamik yola dusse `offer_id` ayristirma hatasi → 422 verirdi).
Bu router `router_registry.ROUTERS`ta `offers_router`dan ONCE kayitlidir; sirayi
`tests/modules/offers/test_templates_api.py` bekcisi olcer.

Yazimlar denetim satiri YAZAR (olustur / guncelle / icerik / varsayilan / sil / tekliften /
kopya); `PATCH` hicbir sey fiilen degismediyse yazmaz.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, status

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES, DELETE_403_YANITI
from app.core.permissions import (
    require_page,
    require_permission,
    require_system_admin,
)
from app.core.ratelimit import client_ip
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.offers import template_service
from app.modules.offers.template_schemas import (
    TemplateContentReplace,
    TemplateCopy,
    TemplateCreate,
    TemplateDetailRead,
    TemplateFromOffer,
    TemplateListResponse,
    TemplateUpdate,
)
from app.modules.users.models import User

# HASSAS ALAN MASKESİ (IZN-B4): `MaskeRotasi` tek parça (bağlamı kendisi ekler).
router = APIRouter(
    tags=["offers"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=MaskeRotasi,
)

_VIEW = require_permission("contracts", AccessLevel.view)
# IZN-B5b madde 7: şablon yazmaları 'Teklif Şablonları' sayfasının Düzenler bayrağı.
_WRITE = [require_page("teklif.sablonlar", "edit")]
_User = Annotated[User, Depends(get_current_user)]
_TemplateId = Annotated[uuid.UUID, Path()]
_BASE = "/offers/templates"


@router.get(_BASE, response_model=TemplateListResponse, dependencies=[_VIEW])
async def list_templates_endpoint(session: DbSession) -> TemplateListResponse:
    """Sablonlar: varsayilan once, sonra ada gore; kullanim sayisi + son guncelleme."""
    return await template_service.list_templates(session)


@router.post(
    _BASE,
    response_model=TemplateDetailRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=_WRITE,
)
async def create_template_endpoint(
    request: Request, data: TemplateCreate, user: _User, session: DbSession
) -> TemplateDetailRead:
    """BOS sablon (icerik `PUT {id}/content`)."""
    template = await template_service.create_template(session, user, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.offer_template_created(template.name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await template_service.get_template_detail(session, template.id)


@router.post(
    _BASE + "/from-offer",
    response_model=TemplateDetailRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=_WRITE,
)
async def create_template_from_offer_endpoint(
    request: Request, data: TemplateFromOffer, user: _User, session: DbSession
) -> TemplateDetailRead:
    """Tekliften sablon: gruplar + kalemlerin katalog baglari + revizyon GG/kar %'si.
    Fiyat ve miktar KOPYALANMAZ."""
    template, offer = await template_service.create_from_offer(
        session,
        user,
        offer_id=data.offer_id,
        rev_no=data.rev_no,
        name=data.name,
        description=data.description,
    )
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.offer_template_from_offer(template.name, offer.offer_no, data.rev_no),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await template_service.get_template_detail(session, template.id)


@router.get(_BASE + "/{template_id}", response_model=TemplateDetailRead, dependencies=[_VIEW])
async def get_template_endpoint(template_id: _TemplateId, session: DbSession) -> TemplateDetailRead:
    return await template_service.get_template_detail(session, template_id)


@router.patch(_BASE + "/{template_id}", response_model=TemplateDetailRead, dependencies=_WRITE)
async def update_template_endpoint(
    request: Request,
    template_id: _TemplateId,
    data: TemplateUpdate,
    user: _User,
    session: DbSession,
) -> TemplateDetailRead:
    """Ad / aciklama / GG-kar % (`null` = temizle) / `is_default` (true = varsayilan yap)."""
    template, changed, became_default = await template_service.update_template(
        session, user, template_id, data, data.expected_updated_at
    )
    if changed:
        await record_audit(
            session,
            action=AuditAction.update,
            detail=messages.offer_template_updated(template.name),
            actor_user_id=user.id,
            ip_address=client_ip(request),
        )
    if became_default:
        await record_audit(
            session,
            action=AuditAction.update,
            detail=messages.offer_template_default_set(template.name),
            actor_user_id=user.id,
            ip_address=client_ip(request),
        )
    return await template_service.get_template_detail(session, template.id)


@router.put(
    _BASE + "/{template_id}/content", response_model=TemplateDetailRead, dependencies=_WRITE
)
async def replace_template_content_endpoint(
    request: Request,
    template_id: _TemplateId,
    data: TemplateContentReplace,
    user: _User,
    session: DbSession,
) -> TemplateDetailRead:
    """TUM gruplar + kalemler TAM degistirilir (govdedeki sira = sablon sirasi). Tek uc:
    ekran gruplari/kalemleri surukleyip tek kaydeder; ince taneli ekle/cikar/sirala uclari
    gerekmez (KISS)."""
    template, group_count, item_count = await template_service.replace_content(
        session, user, template_id, data
    )
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.offer_template_content_replaced(template.name, group_count, item_count),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await template_service.get_template_detail(session, template.id)


@router.post(
    _BASE + "/{template_id}/default", response_model=TemplateDetailRead, dependencies=_WRITE
)
async def set_default_template_endpoint(
    request: Request, template_id: _TemplateId, user: _User, session: DbSession
) -> TemplateDetailRead:
    """Varsayilan yap; eski varsayilan AYNI islemde duser (tek varsayilan)."""
    template, changed = await template_service.set_default(session, user, template_id)
    if changed:
        await record_audit(
            session,
            action=AuditAction.update,
            detail=messages.offer_template_default_set(template.name),
            actor_user_id=user.id,
            ip_address=client_ip(request),
        )
    return await template_service.get_template_detail(session, template.id)


@router.post(
    _BASE + "/{template_id}/copy",
    response_model=TemplateDetailRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=_WRITE,
)
async def copy_template_endpoint(
    request: Request,
    template_id: _TemplateId,
    user: _User,
    session: DbSession,
    data: TemplateCopy | None = None,
) -> TemplateDetailRead:
    """Sablondan sablon kopyasi (varsayilan DEGIL)."""
    template, source = await template_service.copy_template(
        session, user, template_id, data.name if data is not None else None
    )
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.offer_template_copied(template.name, source.name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await template_service.get_template_detail(session, template.id)


@router.delete(
    _BASE + "/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**DELETE_403_YANITI},
    dependencies=[require_system_admin()],
)
async def delete_template_endpoint(
    request: Request, template_id: _TemplateId, user: _User, session: DbSession
) -> None:
    """Teklif şablonunu siler. YALNIZ Sistem Yöneticisi. Bağlı teklifler korunur (`template_id` NULL
    olur).
    """
    template = await template_service.delete_template(session, template_id)
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=messages.offer_template_deleted(template.name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )

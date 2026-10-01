"""Teklif Hazirlama uclari (TKL-B4.1 ayar + TKL-B4.2 teklif/revizyon/grup/kalem).

| uc | kapi |
|----|------|
| okumalar (`GET` ayar / liste / detay / revizyon) | `contracts:view` |
| TUM yazmalar (ayar PUT, teklif, revizyon, gecis, grup, kalem) | `contracts:full` + kisitsiz |

# GECICI IZIN (T25 deseni, izin turune kadar): teklif ayri bir izin modulu DEGILDIR,
# `contracts` iznine baglanir (katalog ile ayni). Izin modulu TOHUMLANMAZ.
# `site_chief`/`field_engineer` (contracts=none) 403 alir.

🔴 ROTA SIRASI: `/offers/settings` LITERALDIR; `/offers/{offer_id}`den ONCE tanimli kalmali
(ayni router icinde, ustte).

Para alanlari `Gorunurluk.para` (kapsami `limited` olan rol gormez). Servisler commit etmez;
denetim satirlari BURADA yazilir. Kalem/grup TEKIL duzenlemeleri bilincli olarak denetim
satiri YAZMAZ (gurultu); yapisal olaylar yazar.
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, status

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_deps import RequireUnrestricted
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import kapsam_kapisi, require_permission
from app.core.ratelimit import client_ip
from app.core.scoped_route import kapsam_rotasi, kapsamdan_oku
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.offers import (
    item_service,
    last_price_provider,
    offer_queries,
    offer_service,
    service,
)
from app.modules.offers.models import OfferRevisionStatus
from app.modules.offers.offer_read_schemas import (
    OfferDetailRead,
    OfferGroupBasicRead,
    OfferItemRead,
    OfferItemsBulkResponse,
    OfferListResponse,
    OfferRevisionRead,
)
from app.modules.offers.offer_schemas import (
    OfferCreate,
    OfferGroupCreate,
    OfferGroupUpdate,
    OfferItemCreate,
    OfferItemsBulkCreate,
    OfferItemUpdate,
    OfferLoseRequest,
    OfferRevisionUpdate,
    OfferUpdate,
)
from app.modules.offers.offer_service import OfferAction
from app.modules.offers.offer_views import read_item
from app.modules.offers.schemas import OfferSettingsRead, OfferSettingsUpdate
from app.modules.offers.settings_audit import conditions_audit_detail, settings_audit_detail
from app.modules.users.models import User

last_price_provider.register()

# 🔴 KAPSAM MASKESI — IKI PARCA DA GEREKLI (bkz. `catalog/router.py`); cifti
#    `tests/core/test_kapsam_baglantisi.py` cakar.
router = APIRouter(
    tags=["offers"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=kapsam_rotasi("contracts", kapsamdan_oku),
    dependencies=[kapsam_kapisi("contracts")],
)

_VIEW = require_permission("contracts", AccessLevel.view)
_FULL = require_permission("contracts", AccessLevel.full)

_User = Annotated[User, Depends(get_current_user)]
_OfferId = Annotated[uuid.UUID, Path()]
_RevNo = Annotated[int, Path(ge=0, le=100_000)]
_WRITE = [_FULL, RequireUnrestricted]


@router.get("/offers/settings", response_model=OfferSettingsRead, dependencies=[_VIEW])
async def get_offer_settings_endpoint(session: DbSession) -> OfferSettingsRead:
    """Teklif varsayilanlari: GG / kar / KDV yuzdesi, gecerlilik gunu, odeme metni."""
    return OfferSettingsRead.model_validate(await service.get_settings(session))


@router.put(
    "/offers/settings",
    response_model=OfferSettingsRead,
    dependencies=[_FULL, RequireUnrestricted],
)
async def update_offer_settings_endpoint(
    request: Request, data: OfferSettingsUpdate, user: _User, session: DbSession
) -> OfferSettingsRead:
    """Varsayilanlari TAM degistirir. Yeni revizyonlar bu degerleri KOPYALAR; mevcut teklifler
    degismez."""
    row, before = await service.update_settings(session, data)
    detail = settings_audit_detail(before, row)
    if detail is not None:  # hicbir alan degismediyse denetim satiri YAZILMAZ
        await record_audit(
            session,
            action=AuditAction.update,
            detail=detail,
            actor_user_id=user.id,
            ip_address=client_ip(request),
        )
    return OfferSettingsRead.model_validate(row)


# ------------------------------------------------------------------------ teklif


@router.get("/offers", response_model=OfferListResponse, dependencies=[_VIEW])
async def list_offers_endpoint(
    session: DbSession,
    status_filter: Annotated[OfferRevisionStatus | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    employer_id: Annotated[uuid.UUID | None, Query()] = None,
    offer_date_from: Annotated[date | None, Query()] = None,
    offer_date_to: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OfferListResponse:
    """Teklif listesi (son revizyonun durumu/tutari). `q`: no / is adi / isveren;
    `offer_date_from`/`offer_date_to`: son revizyonun teklif tarihi (dahil-dahil). Zarfta durum
    basina adet + KDV haric toplam, suresi gecmis adedi ve kazanma orani (`status`
    filtresinden bagimsiz)."""
    return await offer_queries.list_offers(
        session,
        status=status_filter,
        q=q,
        employer_id=employer_id,
        offer_date_from=offer_date_from,
        offer_date_to=offer_date_to,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/offers",
    response_model=OfferDetailRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=_WRITE,
)
async def create_offer_endpoint(
    request: Request, data: OfferCreate, user: _User, session: DbSession
) -> OfferDetailRead:
    """Teklif + Rev.0 taslak. Kosullar gonderilmezse `offer_settings`ten kopyalanir."""
    offer = await offer_service.create_offer(session, user, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.offer_created(offer.offer_no, offer.title, offer.employer_name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await offer_queries.build_offer_detail(session, offer)


@router.get("/offers/{offer_id}", response_model=OfferDetailRead, dependencies=[_VIEW])
async def get_offer_endpoint(offer_id: _OfferId, session: DbSession) -> OfferDetailRead:
    """Kunye + revizyon ozetleri + revizyon gecmisi olaylari."""
    return await offer_queries.get_offer_detail(session, offer_id)


@router.patch("/offers/{offer_id}", response_model=OfferDetailRead, dependencies=_WRITE)
async def update_offer_endpoint(
    request: Request, offer_id: _OfferId, data: OfferUpdate, user: _User, session: DbSession
) -> OfferDetailRead:
    """Kunye (isveren, is adi, kapsam ozeti) — yalniz son revizyon taslak iken."""
    offer, changed = await offer_service.update_offer(session, user, offer_id, data)
    if changed:  # hicbir alan degismediyse denetim satiri YAZILMAZ
        await record_audit(
            session,
            action=AuditAction.update,
            detail=messages.offer_updated(offer.offer_no, offer.title, offer.employer_name),
            actor_user_id=user.id,
            ip_address=client_ip(request),
        )
    return await offer_queries.build_offer_detail(session, offer)


@router.delete("/offers/{offer_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_WRITE)
async def delete_offer_endpoint(
    request: Request, offer_id: _OfferId, user: _User, session: DbSession
) -> None:
    """Yalniz tek revizyonlu ve taslak teklif. Numara geri kullanilmaz."""
    offer = await offer_service.delete_offer(session, user, offer_id)
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=messages.offer_deleted(offer.offer_no, offer.title),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


# --------------------------------------------------------------------- revizyon


@router.post(
    "/offers/{offer_id}/revisions",
    response_model=OfferRevisionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=_WRITE,
)
async def create_revision_endpoint(
    request: Request, offer_id: _OfferId, user: _User, session: DbSession
) -> OfferRevisionRead:
    """Yeni revizyon (onceki revizyonun kopyasi) — yalniz son revizyon gonderilmis/kaybedilmis."""
    offer, revision = await offer_service.create_revision(session, user, offer_id)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.offer_revision_created(offer.offer_no, revision.rev_no),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await offer_queries.read_revision(session, offer, revision)


@router.get(
    "/offers/{offer_id}/revisions/{rev_no}",
    response_model=OfferRevisionRead,
    dependencies=[_VIEW],
)
async def get_revision_endpoint(
    offer_id: _OfferId, rev_no: _RevNo, session: DbSession
) -> OfferRevisionRead:
    """Kosullar + gruplar + kalemler (hesapli) + toplamlar (musteri / ic ayri)."""
    return await offer_queries.get_revision_read(session, offer_id, rev_no)


@router.patch(
    "/offers/{offer_id}/revisions/{rev_no}",
    response_model=OfferRevisionRead,
    dependencies=_WRITE,
)
async def update_revision_endpoint(
    request: Request,
    offer_id: _OfferId,
    rev_no: _RevNo,
    data: OfferRevisionUpdate,
    user: _User,
    session: DbSession,
) -> OfferRevisionRead:
    """Kosullar (tarih, gecerlilik, GG/kar/KDV %, odeme, fiyat farki) — son revizyon taslak."""
    offer, revision, before = await offer_service.update_revision(
        session, user, offer_id, rev_no, data
    )
    detail = conditions_audit_detail(offer.offer_no, before, revision)
    if detail is not None:  # hicbir kosul fiilen degismediyse denetim satiri YAZILMAZ
        await record_audit(
            session,
            action=AuditAction.update,
            detail=detail,
            actor_user_id=user.id,
            ip_address=client_ip(request),
        )
    return await offer_queries.read_revision(session, offer, revision)


async def _transition(
    request: Request,
    session: DbSession,
    user: User,
    offer_id: uuid.UUID,
    rev_no: int,
    action: OfferAction,
    lose: OfferLoseRequest | None = None,
) -> OfferDetailRead:
    offer, revision = await offer_service.transition(session, user, offer_id, rev_no, action, lose)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.offer_status_changed(
            offer.offer_no, revision.rev_no, offer_service.TRANSITIONS[action][2]
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await offer_queries.build_offer_detail(session, offer)


@router.post(
    "/offers/{offer_id}/revisions/{rev_no}/send",
    response_model=OfferDetailRead,
    dependencies=_WRITE,
)
async def send_revision_endpoint(
    request: Request, offer_id: _OfferId, rev_no: _RevNo, user: _User, session: DbSession
) -> OfferDetailRead:
    """`draft → sent`."""
    return await _transition(request, session, user, offer_id, rev_no, OfferAction.send)


@router.post(
    "/offers/{offer_id}/revisions/{rev_no}/win",
    response_model=OfferDetailRead,
    dependencies=_WRITE,
)
async def win_revision_endpoint(
    request: Request, offer_id: _OfferId, rev_no: _RevNo, user: _User, session: DbSession
) -> OfferDetailRead:
    """`sent → won` (taslaktan dogrudan kazanma YOK)."""
    return await _transition(request, session, user, offer_id, rev_no, OfferAction.win)


@router.post(
    "/offers/{offer_id}/revisions/{rev_no}/lose",
    response_model=OfferDetailRead,
    dependencies=_WRITE,
)
async def lose_revision_endpoint(
    request: Request,
    offer_id: _OfferId,
    rev_no: _RevNo,
    user: _User,
    session: DbSession,
    data: OfferLoseRequest | None = None,
) -> OfferDetailRead:
    """`sent → lost`; govde istege bagli: `lost_reason`, `winning_amount` (T37)."""
    return await _transition(request, session, user, offer_id, rev_no, OfferAction.lose, data)


@router.post(
    "/offers/{offer_id}/revisions/{rev_no}/withdraw",
    response_model=OfferDetailRead,
    dependencies=_WRITE,
)
async def withdraw_revision_endpoint(
    request: Request, offer_id: _OfferId, rev_no: _RevNo, user: _User, session: DbSession
) -> OfferDetailRead:
    """`draft | sent → withdrawn` (SON durum)."""
    return await _transition(request, session, user, offer_id, rev_no, OfferAction.withdraw)


# ------------------------------------------------------------------------- grup

_GroupId = Annotated[uuid.UUID, Path()]
_ItemId = Annotated[uuid.UUID, Path()]
_REV = "/offers/{offer_id}/revisions/{rev_no}"


@router.post(
    _REV + "/groups",
    response_model=OfferGroupBasicRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=_WRITE,
)
async def create_group_endpoint(
    offer_id: _OfferId, rev_no: _RevNo, data: OfferGroupCreate, session: DbSession
) -> OfferGroupBasicRead:
    group = await item_service.create_group(session, offer_id, rev_no, data)
    return OfferGroupBasicRead(id=group.id, name=group.name, sort_order=group.sort_order)


@router.patch(_REV + "/groups/{group_id}", response_model=OfferGroupBasicRead, dependencies=_WRITE)
async def update_group_endpoint(
    offer_id: _OfferId,
    rev_no: _RevNo,
    group_id: _GroupId,
    data: OfferGroupUpdate,
    session: DbSession,
) -> OfferGroupBasicRead:
    group = await item_service.update_group(session, offer_id, rev_no, group_id, data)
    return OfferGroupBasicRead(id=group.id, name=group.name, sort_order=group.sort_order)


@router.delete(
    _REV + "/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_WRITE
)
async def delete_group_endpoint(
    request: Request,
    offer_id: _OfferId,
    rev_no: _RevNo,
    group_id: _GroupId,
    user: _User,
    session: DbSession,
) -> None:
    """Grubu icindeki kalemlerle birlikte siler (yalniz taslak)."""
    # Icinde kalem olan grubun silinmesi TEK denetim satiri yazar; bos grup yazmaz (R2c).
    offer, revision, name, item_count = await item_service.delete_group(
        session, offer_id, rev_no, group_id
    )
    if item_count > 0:
        await record_audit(
            session,
            action=AuditAction.delete,
            detail=messages.offer_group_deleted(offer.offer_no, revision.rev_no, name, item_count),
            actor_user_id=user.id,
            ip_address=client_ip(request),
        )


# ------------------------------------------------------------------------ kalem


@router.post(
    _REV + "/items",
    response_model=OfferItemRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=_WRITE,
)
async def create_item_endpoint(
    offer_id: _OfferId, rev_no: _RevNo, data: OfferItemCreate, session: DbSession
) -> OfferItemRead:
    """Katalogdan kalem ekler (poz no / ad / birim / adam-saat KOPYALANIR). `cost_unit_price`
    gonderilmezse maliyet = son fiyat → referans → bos (SO-6); acik `null` = bos."""
    _offer, revision, items = await item_service.add_items(session, offer_id, rev_no, [data])
    return read_item(items[0], revision)


@router.post(
    _REV + "/items/bulk",
    response_model=OfferItemsBulkResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_WRITE,
)
async def create_items_bulk_endpoint(
    request: Request,
    offer_id: _OfferId,
    rev_no: _RevNo,
    data: OfferItemsBulkCreate,
    user: _User,
    session: DbSession,
) -> OfferItemsBulkResponse:
    """Coklu katalog secicisi: 1..200 kalem, hep-ya-hic, TEK denetim satiri."""
    offer, revision, items = await item_service.add_items(
        session, offer_id, rev_no, data.items, bulk=True
    )
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.offer_items_bulk_created(
            offer.offer_no, revision.rev_no, [item.poz_no for item in items]
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return OfferItemsBulkResponse(items=[read_item(item, revision) for item in items])


@router.patch(_REV + "/items/{item_id}", response_model=OfferItemRead, dependencies=_WRITE)
async def update_item_endpoint(
    offer_id: _OfferId,
    rev_no: _RevNo,
    item_id: _ItemId,
    data: OfferItemUpdate,
    session: DbSession,
) -> OfferItemRead:
    """Miktar, maliyet, oranlar (`null` = revizyon geneli), elle B.F. (`null` = kilidi kaldir),
    adam-saat, grup, sira. Katalog bagi ve kopya alanlar degistirilemez (422)."""
    _offer, revision, item = await item_service.update_item(
        session, offer_id, rev_no, item_id, data
    )
    return read_item(item, revision)


@router.delete(
    _REV + "/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_WRITE
)
async def delete_item_endpoint(
    offer_id: _OfferId, rev_no: _RevNo, item_id: _ItemId, session: DbSession
) -> None:
    await item_service.delete_item(session, offer_id, rev_no, item_id)

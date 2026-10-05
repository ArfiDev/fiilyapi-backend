"""Cekirdek is kalemi katalogu uclari — `/catalog/items` (TKL-B2.2).

| uc | kapi |
|----|------|
| kalem listesi / disiplin listesi | `contracts:view` |
| olustur / guncelle / toplu ekle (`/bulk`) | `contracts:full` |

# GECICI IZIN (kullanici karari; izin turuna kadar): katalog ayri bir izin modulu degildir,
# `contracts` iznine baglanir. `earned_value:view` olup `contracts` yetkisi olmayan roller
# (site_chief, field_engineer) bu uclara GIRMEZ (403) — EV KAT ucu (`/earned-value/catalog`)
# onlar icin acik kalir ve `ref_price` DONMEZ.

`ref_price` para alanidir: kapsami `limited` olan rol gormez (alan maskesi). Poz no sunucu
uretir; yazma mantigi EV ile ORTAK (`catalog.service`): 404/409/422 metinleri ayni.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_scope import UNRESTRICTED
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.core.ratelimit import client_ip
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.catalog import bulk, queries, service
from app.modules.catalog.schemas import (
    WorkDisciplineListResponse,
    WorkItemBulkResultRow,
    WorkItemCreate,
    WorkItemListResponse,
    WorkItemRead,
    WorkItemsBulkCreate,
    WorkItemsBulkResponse,
    WorkItemUpdate,
)
from app.modules.users.models import User

# HASSAS ALAN MASKESİ (IZN-B4): `MaskeRotasi` tek parça (bağlamı kendisi ekler).
router = APIRouter(
    tags=["catalog"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=MaskeRotasi,
)

_VIEW = require_permission("contracts", AccessLevel.view)
_FULL = require_permission("contracts", AccessLevel.full)

_User = Annotated[User, Depends(get_current_user)]


@router.get("/catalog/disciplines", response_model=WorkDisciplineListResponse, dependencies=[_VIEW])
async def list_work_disciplines_endpoint(session: DbSession) -> WorkDisciplineListResponse:
    """Disiplin seçici listesi (salt okunur; CRUD EV'de kalır) — `sort_order`, `code` sırası."""
    return WorkDisciplineListResponse(items=await queries.list_disciplines(session, UNRESTRICTED))


@router.get("/catalog/items", response_model=WorkItemListResponse, dependencies=[_VIEW])
async def list_work_items_endpoint(
    session: DbSession,
    q: Annotated[str | None, Query(max_length=200)] = None,
    discipline_id: Annotated[uuid.UUID | None, Query()] = None,
) -> WorkItemListResponse:
    """İş kalemi kataloğu — poz no sırasıyla. `q` ad veya poz no içinde, kaynak poz no'da
    ÖNEKLE arar."""
    items = await queries.list_items(session, UNRESTRICTED, q=q, discipline_id=discipline_id)
    return WorkItemListResponse(items=items)


@router.post(
    "/catalog/items",
    response_model=WorkItemRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL],
)
async def create_work_item_endpoint(
    request: Request, data: WorkItemCreate, user: _User, session: DbSession
) -> WorkItemRead:
    """Kalem ekler; poz no sunucuda otomatik üretilir (gövdede gönderilemez)."""
    item = await service.create_item(session, data.model_dump())
    detail = messages.work_item_created(item.poz_no, item.name, item.uom)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await queries.read_item(session, item)


@router.post(
    "/catalog/items/bulk",
    response_model=WorkItemsBulkResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL],
)
async def bulk_work_items_endpoint(
    request: Request, data: WorkItemsBulkCreate, user: _User, session: DbSession
) -> WorkItemsBulkResponse:
    """Toplu kalem ekleme / fiyat güncelleme (1..200, hep-ya-hiç, TEK denetim satırı).

    Poz no sunucuda üretilir. `source_code` DB'de zaten varsa: `on_source_conflict="error"`
    (varsayılan) → 422; `"update_price"` → yalnız `ref_price` + `ref_price_date` güncellenir.
    Hatalar `errors[]` içinde `loc: ["body","items",i,alan]` ile döner; hiçbir şey yazılmaz.
    """
    rows = await bulk.bulk_upsert_items(
        session, [e.model_dump() for e in data.items], data.on_source_conflict
    )
    counts = {
        a: sum(1 for r in rows if r.action == a)
        for a in (bulk.CREATED, bulk.PRICE_UPDATED, bulk.UNCHANGED)
    }
    codes = await queries.discipline_codes(session, {r.item.discipline_id for r in rows})
    await record_audit(
        session,
        action=AuditAction.create if counts[bulk.CREATED] else AuditAction.update,
        detail=messages.work_items_bulk_imported(
            counts[bulk.CREATED], counts[bulk.PRICE_UPDATED], counts[bulk.UNCHANGED], codes
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return WorkItemsBulkResponse(
        created=counts[bulk.CREATED],
        updated=counts[bulk.PRICE_UPDATED],
        unchanged=counts[bulk.UNCHANGED],
        items=[
            WorkItemBulkResultRow(
                index=r.index, id=r.item.id, poz_no=r.item.poz_no, action=r.action
            )
            for r in rows
        ],
    )


@router.patch(
    "/catalog/items/{item_id}",
    response_model=WorkItemRead,
    dependencies=[_FULL],
)
async def update_work_item_endpoint(
    request: Request,
    item_id: uuid.UUID,
    data: WorkItemUpdate,
    user: _User,
    session: DbSession,
) -> WorkItemRead:
    """Kısmi güncelleme; geçmeyen alan dokunulmaz, `description`/`ref_price` `null` = temizle.

    Denetim satırı YALNIZ bir alan FİİLEN değiştiyse yazılır (boş `{}` PATCH ya da aynı
    değerlerle PATCH satır yazmaz; 200 + mevcut kayıt döner). `ref_price` değiştiyse metne
    `eski → yeni` fiyat girer.
    """
    changes = data.model_dump(exclude_unset=True)
    result = await service.apply_item_update(session, item_id, changes)
    item, changed = result.item, result.changed
    old_price = result.before.get("ref_price")
    if changed:
        detail = (
            messages.work_item_price_updated(
                item.poz_no, item.name, item.uom, old_price, item.ref_price
            )
            if "ref_price" in changed
            else messages.work_item_updated(item.poz_no, item.name, item.uom)
        )
        await record_audit(
            session,
            action=AuditAction.update,
            detail=detail,
            actor_user_id=user.id,
            ip_address=client_ip(request),
        )
    return await queries.read_item(session, item)

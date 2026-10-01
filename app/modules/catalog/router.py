"""Cekirdek is kalemi katalogu uclari — `/catalog/items` (TKL-B2.2).

| uc | kapi |
|----|------|
| kalem listesi / disiplin listesi | `contracts:view` (+ disiplin kapsami) |
| olustur / guncelle | `contracts:full` + `RequireUnrestricted` |

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
from app.core.discipline_deps import DisciplineScoped, RequireUnrestricted
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import kapsam_kapisi, require_permission
from app.core.ratelimit import client_ip
from app.core.scoped_route import kapsam_rotasi, kapsamdan_oku
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.catalog import queries, service
from app.modules.catalog.schemas import (
    WorkDisciplineListResponse,
    WorkItemCreate,
    WorkItemListResponse,
    WorkItemRead,
    WorkItemUpdate,
)
from app.modules.users.models import User

# 🔴 KAPSAM MASKESİ — İKİ PARÇA DA GEREKLİ (bkz. `contracts/router.py`); çifti
#    `tests/core/test_kapsam_baglantisi.py` çakar.
router = APIRouter(
    tags=["catalog"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=kapsam_rotasi("contracts", kapsamdan_oku),
    dependencies=[kapsam_kapisi("contracts")],
)

_VIEW = require_permission("contracts", AccessLevel.view)
_FULL = require_permission("contracts", AccessLevel.full)

_User = Annotated[User, Depends(get_current_user)]


@router.get("/catalog/disciplines", response_model=WorkDisciplineListResponse, dependencies=[_VIEW])
async def list_work_disciplines_endpoint(
    session: DbSession, scope: DisciplineScoped
) -> WorkDisciplineListResponse:
    """Disiplin seçici listesi (salt okunur; CRUD EV'de kalır) — `sort_order`, `code` sırası."""
    return WorkDisciplineListResponse(items=await queries.list_disciplines(session, scope))


@router.get("/catalog/items", response_model=WorkItemListResponse, dependencies=[_VIEW])
async def list_work_items_endpoint(
    session: DbSession,
    scope: DisciplineScoped,
    q: Annotated[str | None, Query(max_length=200)] = None,
    discipline_id: Annotated[uuid.UUID | None, Query()] = None,
) -> WorkItemListResponse:
    """İş kalemi kataloğu — poz no sırasıyla. `q` ad veya poz no içinde arar."""
    items = await queries.list_items(session, scope, q=q, discipline_id=discipline_id)
    return WorkItemListResponse(items=items)


@router.post(
    "/catalog/items",
    response_model=WorkItemRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL, RequireUnrestricted],
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


@router.patch(
    "/catalog/items/{item_id}",
    response_model=WorkItemRead,
    dependencies=[_FULL, RequireUnrestricted],
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
    before = service.snapshot_fields(await service.get_item(session, item_id), changes)
    old_price = before.get("ref_price")
    item = await service.update_item(session, item_id, changes)
    changed = service.changed_fields(before, item)
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

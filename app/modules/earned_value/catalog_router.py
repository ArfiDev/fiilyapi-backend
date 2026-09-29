"""Planlama (EV) uclari — catalog_router. Kapilar ve kapsam: `access.py`.

SIRKET duzeyi (K2, K4): disiplin listesi + birim oran katalogu (KAT ekrani).
Santiye kapsami YOKTUR — izin kapisi yeter.

| uc | kapi |
|----|------|
| disiplin listesi okuma | `VIEW` VEYA `user_management:view` (B0b) |
| katalog okuma | `VIEW` |
| disiplin/katalog yazma, "gerceklesen standart yap" | `CATALOG` (full) |
| disiplin silme | `ADMIN` (B1-9) |

`GET` `record_audit` CAGIRMAZ; her yazma ucu TEK denetim satiri yazar
(`audit_messages`). Katalog kalemi silme/arsiv ucu YOKTUR (B1-9).
"""

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_deps import DisciplineScoped, RequireUnrestricted
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_any_permission
from app.core.ratelimit import client_ip
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.earned_value import audit_messages, catalog_service, discipline_adapter
from app.modules.earned_value.access import ADMIN, CATALOG, PERMISSION_MODULE, VIEW
from app.modules.earned_value.schemas_catalog import (
    CatalogItemCreate,
    CatalogItemRead,
    CatalogItemUpdate,
    DisciplineCreate,
    DisciplineRead,
    DisciplineUpdate,
)
from app.modules.users.models import User

# Disiplin kapsami portuna kayit (DSC-B0) — import yan etkisi; `day_router` emsali.
discipline_adapter.register()

router = APIRouter(tags=["earned-value"], responses=COMMON_ERROR_RESPONSES)

_User = Annotated[User, Depends(get_current_user)]
_Session = DbSession


async def _audit(
    session: AsyncSession, request: Request, user: User, action: AuditAction, detail: str
) -> None:
    await record_audit(
        session,
        action=action,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


# ------------------------------------------------------------------ disiplin

# DSC-B0b: kullanici yonetimi ekrani (atama secicisi) disiplin katalogunu okur ama EV izni
# gerektirmez → `earned_value:view` VEYA `user_management:view`. Izin MATRISI degismez; yalniz
# bu ucun kapisi "herhangi biri yeter". Kisitli kullanici yine yalniz kendi disiplinlerini gorur
# (`DisciplineScoped`, B1). Gerekce `#` yorumdur: docstring openapi aciklamasina sizar.
_DISCIPLINE_LIST_GATE = require_any_permission(
    (PERMISSION_MODULE, AccessLevel.view), ("user_management", AccessLevel.view)
)


def _discipline_read(row, usage: catalog_service.DisciplineUsage) -> DisciplineRead:  # noqa: ANN001
    return DisciplineRead.model_validate(row).model_copy(
        update={
            "used_by_item_count": usage.item_count,
            "used_by_site_count": usage.site_count,
            "user_count": usage.user_count,
        }
    )


@router.get(
    "/earned-value/disciplines",
    response_model=list[DisciplineRead],
    dependencies=[_DISCIPLINE_LIST_GATE],
)
async def list_disciplines_endpoint(
    session: _Session, scope: DisciplineScoped
) -> list[DisciplineRead]:
    """Sirket disiplinleri (K2) — `sort_order`, sonra `code` sirasiyla."""
    # Kisitli kullanici yalniz kendi disiplinlerini gorur (Ü8) — `list_disciplines(scope)`.
    rows = await catalog_service.list_disciplines(session, scope)
    usage = await catalog_service.discipline_usage(session, [r.id for r in rows])
    return [_discipline_read(row, usage[row.id]) for row in rows]


@router.post(
    "/earned-value/disciplines",
    response_model=DisciplineRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[CATALOG, RequireUnrestricted],
)
async def create_discipline_endpoint(
    request: Request, data: DisciplineCreate, user: _User, session: _Session
) -> DisciplineRead:
    """Disiplin ekler. Kod benzersizdir → 409 `DISCIPLINE_CODE_TAKEN`."""
    discipline = await catalog_service.create_discipline(session, data)
    detail = audit_messages.discipline_created(discipline.code, discipline.name)
    await _audit(session, request, user, AuditAction.create, detail)
    return DisciplineRead.model_validate(discipline)


@router.patch(
    "/earned-value/disciplines/{discipline_id}",
    response_model=DisciplineRead,
    dependencies=[CATALOG, RequireUnrestricted],
)
async def update_discipline_endpoint(
    request: Request,
    discipline_id: uuid.UUID,
    data: DisciplineUpdate,
    user: _User,
    session: _Session,
) -> DisciplineRead:
    """Kismi guncelleme; gecmeyen alan dokunulmaz, acik `null` 422 (alan adli)."""
    discipline = await catalog_service.update_discipline(session, discipline_id, data)
    detail = audit_messages.discipline_updated(discipline.code, discipline.name)
    await _audit(session, request, user, AuditAction.update, detail)
    usage = await catalog_service.discipline_usage(session, [discipline.id])
    return _discipline_read(discipline, usage[discipline.id])


@router.delete(
    "/earned-value/disciplines/{discipline_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[ADMIN, RequireUnrestricted],
)
async def delete_discipline_endpoint(
    request: Request, discipline_id: uuid.UUID, user: _User, session: _Session
) -> Response:
    """Disiplini siler — yalniz HICBIR EV kaydinda kullanilmiyorsa (B1-9), yoksa 409."""
    discipline = await catalog_service.delete_discipline(session, discipline_id)
    detail = audit_messages.discipline_deleted(discipline.code, discipline.name)
    await _audit(session, request, user, AuditAction.delete, detail)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------- katalog


@router.get("/earned-value/catalog", response_model=list[CatalogItemRead], dependencies=[VIEW])
async def list_catalog_endpoint(
    session: _Session,
    scope: DisciplineScoped,
    discipline_id: Annotated[uuid.UUID | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> list[CatalogItemRead]:
    """Birim oran katalogu (KAT). `q` is tipi adinda harf duyarsiz arar.

    `actual` (gerceklesen) ve `diff_pct` K4 kuralindadir: TAMAMLANMIS santiyelerin miktar
    agirlikli ortalamasi (`catalog_service.catalog_actuals`); tamamlanmis santiye verisi
    yoksa bostur.
    """
    rows = await catalog_service.list_catalog(session, discipline_id, q, scope)
    return [catalog_service.to_read(row) for row in rows]


@router.post(
    "/earned-value/catalog",
    response_model=CatalogItemRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[CATALOG, RequireUnrestricted],
)
async def create_catalog_item_endpoint(
    request: Request, data: CatalogItemCreate, user: _User, session: _Session
) -> CatalogItemRead:
    """Katalog is tipi ekler. (disiplin, ad, birim) benzersiz — ad/birim NORMALIZE
    karsilastirilir (buyuk/kucuk harf, İ/I, bosluk) → 409 `CATALOG_ITEM_TAKEN_AS`."""
    row = await catalog_service.create_catalog_item(session, data)
    detail = audit_messages.catalog_item_created(row.item.name, row.item.uom)
    await _audit(session, request, user, AuditAction.create, detail)
    return catalog_service.to_read(row)


@router.patch(
    "/earned-value/catalog/{item_id}",
    response_model=CatalogItemRead,
    dependencies=[CATALOG, RequireUnrestricted],
)
async def update_catalog_item_endpoint(
    request: Request,
    item_id: uuid.UUID,
    data: CatalogItemUpdate,
    user: _User,
    session: _Session,
) -> CatalogItemRead:
    """Kismi guncelleme. Standart oran DEGISIRSE `standard_updated_at` yenilenir."""
    row = await catalog_service.update_catalog_item(session, item_id, data)
    detail = audit_messages.catalog_item_updated(row.item.name, row.item.uom)
    await _audit(session, request, user, AuditAction.update, detail)
    return catalog_service.to_read(row)


@router.post(
    "/earned-value/catalog/{item_id}/adopt-actual",
    response_model=CatalogItemRead,
    dependencies=[CATALOG, RequireUnrestricted],
)
async def adopt_actual_endpoint(
    request: Request, item_id: uuid.UUID, user: _User, session: _Session
) -> CatalogItemRead:
    """KAT "Gerceklesen standart yap": gerceklesen ortalama yoksa 409 `CATALOG_NO_ACTUAL`.

    Gerceklesen = TAMAMLANMIS santiyelerin miktar agirlikli ortalamasi (K4; PLN-B3'ten beri
    `actuals.completed_site_actuals`); hic tamamlanmis santiye verisi yoksa 409.
    """
    result = await catalog_service.adopt_actual(session, item_id)
    item = result.row.item
    detail = audit_messages.catalog_standard_adopted(
        item.name, item.uom, _fmt(result.old), _fmt(result.new)
    )
    await _audit(session, request, user, AuditAction.update, detail)
    return catalog_service.to_read(result.row)


def _fmt(value: Decimal) -> str:
    """Denetim metni icin sade ondalik: sondaki sifirlar atilir, Turkce virgul."""
    text = format(value.normalize(), "f")
    return text.replace(".", ",")

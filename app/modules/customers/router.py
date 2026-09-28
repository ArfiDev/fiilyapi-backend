"""Alıcı (müşteri) kartoteksi uçları — P8 T2 (spec §4).

`contracts/router.py`nin `/subcontractors` bloğunun birebiri: kapı sabitleri
modül düzeyinde tanımlanır, denetim metinleri `audit/messages.py`den gelir.

**`visible_projects` süzgeci BİLİNÇLİ OLARAK yok** (spec §6): `customers`
proje-bağımsızdır, tabloda `project_id` kolonu bile yoktur. Erişim yalnız `sales`
izin seviyesindedir. IDOR unutulmuş DEĞİLDİR — sonraki okuyucu bunu "eksik"
sanıp proje süzgeci EKLEMESİN.

**DELETE ucu AÇILMAZ** (spec §4): alıcıya bağlı satış kaydı bulunabilir
(`unit_sales.customer_id` RESTRICT). Kartoteksten çıkarma ihtiyacı doğarsa
çözüm bir aktiflik bayrağıdır, silme değil.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import kapsam_kapisi, require_permission
from app.core.ratelimit import client_ip
from app.core.scoped_route import kapsam_rotasi, kapsamdan_oku
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.customers import service
from app.modules.customers.schemas import (
    CustomerCreate,
    CustomerListResponse,
    CustomerResponse,
    CustomerUpdate,
)
from app.modules.users.models import User

# 🔴 KAPSAM MASKESI — IKI PARCA DA GEREKLI (kullanici karari 2026-09-19).
#    Anahtar `customers` DEGIL `sales`: `customers` bir izin modulu degildir,
#    uclari `sales` seviyeleriyle korunur (yukaridaki docstring) ve kapsam da
#    o satirdan okunur.
#
#    BUGUN HICBIR ALANI MASKELENMEZ (olculdu: `CustomerResponse` tamami kimlik
#    alani — ad, TCKN/VKN, telefon, adres; tek bir `Decimal` yok). Kopru YINE DE
#    kurulur ve bu ihtiyati DEGILDIR: kartoteks yarin bir bakiye/alacak alani
#    kazanirsa maske KENDILIGINDEN calisir. Kopruyu "simdilik gereksiz" diye
#    atlamak, o alani ekleyen kisinin buraya bakmasini gerektirirdi — yani tam
#    olarak `units` routerini 15 uc boyunca maskesiz birakan hatanin kendisi.
router = APIRouter(
    tags=["customers"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=kapsam_rotasi("sales", kapsamdan_oku),
    dependencies=[kapsam_kapisi("sales")],
)

_VIEW = require_permission("sales", AccessLevel.view)
_FULL = require_permission("sales", AccessLevel.full)


@router.get("/customers", response_model=CustomerListResponse, dependencies=[_VIEW])
async def list_customers_endpoint(
    session: DbSession,
    q: str | None = None,
) -> CustomerListResponse:
    """`q` ad / TCKN / VKN üzerinde kısmi arar (spec §4)."""
    items = await service.list_customers(session, q)
    return CustomerListResponse(items=[CustomerResponse.model_validate(c) for c in items])


@router.post(
    "/customers",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL],
)
async def create_customer_endpoint(
    request: Request,
    data: CustomerCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> CustomerResponse:
    customer = await service.create_customer(session, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.customer_created(customer.name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return CustomerResponse.model_validate(customer)


@router.get("/customers/{customer_id}", response_model=CustomerResponse, dependencies=[_VIEW])
async def get_customer_endpoint(
    customer_id: uuid.UUID,
    session: DbSession,
) -> CustomerResponse:
    customer = await service.get_customer(session, customer_id)
    return CustomerResponse.model_validate(customer)


@router.patch("/customers/{customer_id}", response_model=CustomerResponse, dependencies=[_FULL])
async def update_customer_endpoint(
    request: Request,
    customer_id: uuid.UUID,
    data: CustomerUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> CustomerResponse:
    customer = await service.update_customer(session, customer_id, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.customer_updated(customer.name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return CustomerResponse.model_validate(customer)

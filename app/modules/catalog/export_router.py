"""Fiyatli Is Kalemi Katalogu Excel ucu (TKL-B5.2) — `/catalog/items` okumasiyla AYNI kapi:
`contracts:view` + kapsam cifti + `DisciplineScoped`. Okuma ucu: denetim satiri YAZMAZ.

Veri `queries.list_items` ile (tek toplu `last_price.latest`, N+1 yok). Maske ELLE uygulanir
(`kapsamla_maskele`): `limited` rolde Referans Fiyat / Fiyat Guncelleme / Son Fiyat / Kaynak /
Belge / Tarih / Fiyat Tarihi (`ref_price_date`, KAT-B1: fiyat gizliyken tarihi de gizli) BOS;
poz, ad, birim, adam-saat, yuklenici, Kaynak Poz No gorunur (BOQ export emsali).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.core import http
from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.discipline_deps import DisciplineScoped
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import kapsam_kapisi, require_permission
from app.core.scoped_route import kapsam_rotasi, kapsamdan_oku, kapsamla_maskele
from app.modules.catalog import queries
from app.modules.catalog.export import FILENAME, build_catalog_workbook
from app.modules.catalog.schemas import WorkItemListResponse

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# 🔴 KAPSAM MASKESI — IKI PARCA DA GEREKLI (bkz. `catalog/router.py`).
router = APIRouter(
    tags=["catalog"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=kapsam_rotasi("contracts", kapsamdan_oku),
    dependencies=[kapsam_kapisi("contracts")],
)

_VIEW = require_permission("contracts", AccessLevel.view)


@router.get(
    "/catalog/items/export",
    dependencies=[_VIEW],
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Excel dosyasi"}},
)
async def export_catalog_endpoint(
    session: DbSession,
    scope: DisciplineScoped,
    q: Annotated[str | None, Query(max_length=200)] = None,
    discipline_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Response:
    """Fiyatli katalog xlsx — liste ucuyla AYNI suzgecler (`q`, `discipline_id`) ve kapsam."""
    items = await queries.list_items(session, scope, q=q, discipline_id=discipline_id)
    masked = kapsamla_maskele(WorkItemListResponse(items=items), "contracts")
    return Response(
        content=build_catalog_workbook(masked.items).getvalue(),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": http.content_disposition(FILENAME)},
    )

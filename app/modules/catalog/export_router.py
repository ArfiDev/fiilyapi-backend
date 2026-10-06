"""Fiyatli Is Kalemi Katalogu Excel ucu (TKL-B5.2) — `/catalog/items` okumasiyla AYNI kapi:
`contracts:view` + kapsam cifti. Okuma ucu: denetim satiri YAZMAZ.

Veri `queries.list_items` ile (tek toplu `last_price.latest`, N+1 yok).
Maske ELLE uygulanir (`maskele_baglamli`):
`limited` rolde Referans Fiyat / Fiyat Guncelleme / Son Fiyat / Kaynak /
Belge / Tarih / Fiyat Tarihi (`ref_price_date`, KAT-B1: fiyat gizliyken tarihi de gizli) BOS;
poz, ad, birim, adam-saat, yuklenici, Kaynak Poz No gorunur (BOQ export emsali).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.core import http
from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.discipline_scope import UNRESTRICTED
from app.core.mask_route import MaskeRotasi, maskele_baglamli
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.modules.catalog import queries
from app.modules.catalog.export import FILENAME, build_catalog_workbook
from app.modules.catalog.schemas import WorkItemListResponse

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# HASSAS ALAN MASKESİ (IZN-B4): `MaskeRotasi` tek parça (bağlamı kendisi ekler).
router = APIRouter(
    tags=["catalog"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=MaskeRotasi,
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
    q: Annotated[str | None, Query(max_length=200)] = None,
    discipline_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Response:
    """Fiyatli katalog xlsx — liste ucuyla AYNI suzgecler (`q`, `discipline_id`)."""
    items = await queries.list_items(session, UNRESTRICTED, q=q, discipline_id=discipline_id)
    masked = await maskele_baglamli(WorkItemListResponse(items=items))
    return Response(
        content=build_catalog_workbook(masked.items).getvalue(),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": http.content_disposition(FILENAME)},
    )

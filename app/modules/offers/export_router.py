"""Teklif revizyonu Excel ucu (TKL-B5.2) — okuma ucudur, denetim satiri YAZMAZ.

Kapi `offers/router.py` okumalariyla BIREBIR: `contracts:view` + kapsam cifti (route sinifi +
`kapsam_kapisi`) + `RequireUnrestricted` (TKL-B4.6, R5/SO-19: disiplin atanmis kullanici teklif
modulunu hic goremez). Ayri dosya: B4 router'ina dokunmadan eklenir.

🔴 Maske ELLE uygulanir (`kapsamla_maskele`): rota sarmalayicisi `Response` govdesinin icine
bakamaz (bkz. `boq/router.py` kacak-uc notu). Zarf kitaba girmeden ONCE maskelenir.

`limited` kullanici HER IKI gorunumu de indirebilir; para hucreleri BOS basilir (BOQ emsali,
"kapsam kisiti gizleme kararidir, is durdurma karari degil"): ekranda gorunen kume ile dosyadaki
kume ayni kalir, iki gorunum arasinda 403/200 ayrisimi uretilmez. Adam-saat ve girdi
yuzdeleri API'de de gorunur oldugu icin iç gorunumde dolu kalir.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response

from app.core import http
from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.discipline_deps import RequireUnrestricted
from app.core.errors import NotFoundError
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import kapsam_kapisi, require_permission
from app.core.scoped_route import kapsam_rotasi, kapsamdan_oku, kapsamla_maskele
from app.modules.offers import offer_queries
from app.modules.offers.export import FILE_SUFFIX, ExportView, build_offer_workbook
from app.modules.offers.locking import OFFER_MISSING
from app.modules.offers.models import Offer

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# 🔴 KAPSAM MASKESI — IKI PARCA DA GEREKLI (bkz. `offers/router.py`).
router = APIRouter(
    tags=["offers"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=kapsam_rotasi("contracts", kapsamdan_oku),
    dependencies=[kapsam_kapisi("contracts"), RequireUnrestricted],  # TKL-B4.6 (R5)
)

_VIEW = require_permission("contracts", AccessLevel.view)


@router.get(
    "/offers/{offer_id}/revisions/{rev_no}/export",
    dependencies=[_VIEW],
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Excel dosyasi"}},
)
async def export_revision_endpoint(
    offer_id: Annotated[uuid.UUID, Path()],
    rev_no: Annotated[int, Path(ge=0, le=100_000)],
    session: DbSession,
    view: Annotated[
        ExportView,
        Query(description="`employer` (varsayilan): isveren ciktisi; `internal`: ic cikti."),
    ] = ExportView.employer,
) -> Response:
    """Revizyonu xlsx olarak indirir. Veri teklif okuma yolundandir (ikinci hesap yok)."""
    revision = await offer_queries.get_revision_read(session, offer_id, rev_no)
    offer = await session.get(Offer, offer_id)  # `get_revision_read` yukledi: kimlik haritasi
    if offer is None:  # pragma: no cover - get_revision_read 404 verirdi
        raise NotFoundError(OFFER_MISSING)
    buffer = build_offer_workbook(offer, kapsamla_maskele(revision, "contracts"), view)
    filename = f"{revision.offer_no}-Rev{rev_no}-{FILE_SUFFIX[view]}.xlsx"
    return Response(
        content=buffer.getvalue(),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": http.content_disposition(filename)},
    )

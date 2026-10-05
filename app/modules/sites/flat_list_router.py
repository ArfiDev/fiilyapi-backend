"""Duz `GET /sites` ucu — BOR-TEMIZ T4 (SITE-1a).

Olculen bosluk: santiye listeleyen TEK yol `/projects/{project_id}/sites`ti;
oteki tum `/sites/...` yollari tekil `{site_id}` altindaydi. Proje SECMEDEN
santiye listeleyen bir uc YOKTU — mockup'larin "santiye sec" dropdown'lari
bunu istiyor. Migration YOKTUR: hicbir sema degisikligi getirmez.

## 🔴 Nicin AYRI, INCE bir router

`sites/service.py` dosya tavaninin (800 satir) USTUNDE — o dosyaya SATIR
EKLENMEZ. Bu router `projects.service.visible_projects`i cagirip
`sites.repository`nin iki ince sorgusunu kullanir (servis katmani atlanir —
`personnel/document_type_router.py` ve `equipment/document_router.py`nin ayni
deseni).

## 🔴 Gorunurluk: `/projects/{id}/sites` ile AYNI KAYNAK

`sites/service.py:_visible_project` da, bu router da
`app.modules.projects.service.visible_projects(session, actor)` cagirir. Kopya
gorunurluk mantigi YAZILMAZ (P2 spec §5.2: "tek kaynak burasidir").
Listede gorunmeyen proje santiyeleri SESSIZCE suzulur (404 uretilmez — liste
ucu tekil kayit sormaz); tekil erisimde kanon degismeden 404 kalir.
`total` de ayni suzgecten gecmis kumeyi sayar.

## Rota sirasi

`GET /sites` TEK segmentlidir; `sites_router`in `GET /sites/{site_id}` yolu
IKI segmentlidir. FastAPI SEGMENT SAYISINA gore ayirdigi icin bu ikisi
CAKISMAZ — `personnel/document-types` vs `personnel/{id}` tuzagi (iki segment
vs iki segment) burada YOKTUR; olculdu. Yine de `main.py`de bu router
`sites_router`dan ONCE kaydedilir: maliyeti sifir, ilerideki bir
`/sites/{...}` genislemesine karsi ucuz sigorta.

## Yetki + sayfalama

Kapi `sites` iznidir, duzey `view` — mevcut santiye okuma uclariyla ayni.
Sayfalama K7 standardi: varsayilan 50, tavan 200, asim 422 (kirpma YOK).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import kapsam_kapisi, require_permission
from app.core.scoped_route import kapsam_rotasi, kapsamdan_oku
from app.modules.projects.service import visible_projects
from app.modules.sites import repository
from app.modules.sites.schemas import SiteOptionListResponse, SiteOptionResponse
from app.modules.users.models import User

# 🔴 KAPSAM MASKESİ — İKİ PARÇA DA GEREKLİ (kullanıcı kararı 2026-09-19).
#    Gerekçe `router.py`deki kardeşindedir; çifti
#    `tests/core/test_kapsam_baglantisi.py` çakar.
router = APIRouter(
    tags=["sites"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=kapsam_rotasi("sites", kapsamdan_oku),
    dependencies=[kapsam_kapisi("sites")],
)

_VIEW = require_permission("sites", AccessLevel.view)
# IZN-B3: çok proje LİSTE uçları: ana rol VEYA ekip rolü açar, satırlar `visible_projects`te proje
# başına o projedeki rolle süzülür.
_VIEW_LISTE = require_permission("sites", AccessLevel.view, multi_project=True)

# K7 sayfalama standardi (`accounting`/`invoicing` router'lariyla birebir):
# tavan asimi sessizce KIRPILMAZ, 422 doner.
_LIMIT = Annotated[int, Query(ge=1, le=200)]
_OFFSET = Annotated[int, Query(ge=0)]


@router.get("/sites", response_model=SiteOptionListResponse, dependencies=[_VIEW_LISTE])
async def list_site_options_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    limit: _LIMIT = 50,
    offset: _OFFSET = 0,
) -> SiteOptionListResponse:
    """Proje bagimsiz santiye secenekleri (yalin sema, K3)."""
    project_ids = [project.id for project in await visible_projects(session, user)]
    total = await repository.count_site_options(session, project_ids)
    rows = await repository.list_site_options(session, project_ids, limit, offset)
    return SiteOptionListResponse(
        items=[
            SiteOptionResponse(
                id=site_id,
                code=code,
                name=name,
                project_id=project_id,
                project_name=project_name,
            )
            for site_id, code, name, project_id, project_name in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )

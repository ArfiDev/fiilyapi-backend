import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_deps import DisciplineScoped
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_page, require_pages, require_permission
from app.core.ratelimit import client_ip
from app.core.slug import parse_ref
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.projects import cost_summary, land_share, service, timeline
from app.modules.projects.land_share_schemas import (
    LandShareSummaryResponse,
    LandShareUnitListResponse,
)
from app.modules.projects.models import ProjectStatus, ProjectType
from app.modules.projects.schemas import (
    EmployerCreate,
    EmployerListResponse,
    EmployerResponse,
    ProjectCostsResponse,
    ProjectCreate,
    ProjectDetailResponse,
    ProjectListResponse,
    ProjectTimelineResponse,
    ProjectUpdate,
)
from app.modules.units.schemas import UnitOwnerSideFilter
from app.modules.users.models import User

# HASSAS ALAN MASKESİ (IZN-B4): `MaskeRotasi` tek parça (bağlamı kendisi ekler).
router = APIRouter(
    prefix="/projects",
    tags=["projects"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=MaskeRotasi,
)

# K7 sayfalama standardi (`accounting`/`invoicing`/duz `GET /sites` ile birebir):
# varsayilan 50, tavan 200; tavan asimi SESSIZCE KIRPILMAZ → 422.
_LIMIT = Annotated[int, Query(ge=1, le=200)]
_OFFSET = Annotated[int, Query(ge=0)]

# İşveren kartoteksi YENİ İZİN MODÜLÜ AÇMAZ (spec §2.5/§7.6): `projects`
# view/admin ile korunur. Ayrı bir router yalnız yol farkı içindir (/employers).
employers_router = APIRouter(
    prefix="/employers",
    tags=["employers"],
    responses=COMMON_ERROR_RESPONSES,
    # Kartoteks YENİ izin modülü açmaz → kapsamı da `projects`tendir.
    route_class=MaskeRotasi,
)


@employers_router.get(
    "",
    response_model=EmployerListResponse,
    dependencies=[require_permission("projects", AccessLevel.view)],
)
async def list_employers_endpoint(
    session: DbSession,
    q: str | None = None,
    active_only: bool = True,
) -> EmployerListResponse:
    employers = await service.list_employers(session, q, active_only)
    return EmployerListResponse(items=[EmployerResponse.model_validate(e) for e in employers])


@employers_router.post(
    "",
    response_model=EmployerResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_page("genel.projeler", "edit")],
)
async def create_employer_endpoint(
    request: Request,
    data: EmployerCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> EmployerResponse:
    employer = await service.create_employer(session, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.employer_created(employer.name),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    return EmployerResponse.model_validate(employer)


@router.get(
    "",
    response_model=ProjectListResponse,
    dependencies=[require_permission("projects", AccessLevel.view, multi_project=True)],
)
async def list_projects_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
    type: ProjectType | None = None,
    status_filter: Annotated[ProjectStatus | None, Query(alias="status")] = None,
    limit: _LIMIT = 50,
    offset: _OFFSET = 0,
) -> ProjectListResponse:
    """Proje listesi (SITE-1b sonrası sayfalı).

    `counts` süzgeçten de sayfadan da ETKİLENMEZ (sekme rakamları);
    `total` SÜZGEÇLENMİŞ kümenin boyutudur (sayfa çubuğu). Ayrıntı:
    `ProjectListResponse` docstring'i.
    """
    return await service.list_projects_overview(
        session, user, type, status_filter, scope, limit, offset
    )


# DIKKAT — ROTA SIRASI: bu STATIK yol, `/{project_id}` parametreli yolundan
# ONCE tanimlanmak ZORUNDA. Sonra tanimlanirsa FastAPI "timeline"i bir proje
# kimligi sanar ve uc hic calismadan 422 (uuid_parsing) doner.
@router.get(
    "/timeline",
    response_model=ProjectTimelineResponse,
    # OKUMA ucu: `view` yeter (spec §3). Yeni izin modulu ACILMAZ. Audit
    # YAZILMAZ — turev okuma hicbir sey degistirmez (costs ucuyla ayni karar).
    dependencies=[require_permission("projects", AccessLevel.view, multi_project=True)],
)
async def get_projects_timeline_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ProjectTimelineResponse:
    """Portfoy Gantt'i (P11). HAM veri — ay/zoom parametresi YOKTUR (spec §6 S4)."""
    return await timeline.get_timeline(session, user)


@router.get(
    "/{project_id}",
    response_model=ProjectDetailResponse,
    dependencies=[require_permission("projects", AccessLevel.view)],
)
async def get_project_endpoint(
    project_id: str,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> ProjectDetailResponse:
    """URL-2 — yol parametresi UUID **ya da** slug kabul eder (karar 2).

    🔴 YOL ADI `project_id` OLARAK KALIR: yolun sablonu (`/projects/{project_id}`)
    degismezse uretilmis istemcinin yol anahtari de degismez. Degisen tek sey
    parametrenin TIPIDIR (`uuid` -> `string`) — sozlesme farkinda gorulecek
    sey budur.

    🔴 YAN ETKI: eskiden UUID olmayan bir deger 422 (`uuid_parsing`) alirdi,
    artik 404 alir. Bu KACINILMAZDIR — slug uzayi tam olarak "UUID olmayan
    metinler"dir; ikisi ayni yol parametresinde birlikte yasayamaz.

    PATCH ucu BILEREK `uuid.UUID` KALIR: karar 2 "OKUMA uclari" der. Yazmanin
    kimligi, okumanin dondurdugu `id`den gelir; yazma yuzeyini tahmin edilebilir
    bir anahtara acmak icin sebep YOKTUR.
    """
    return await service.get_project_detail(session, user, parse_ref(project_id), scope)


@router.get(
    "/{project_id}/costs",
    response_model=ProjectCostsResponse,
    # OKUMA ucu: `view` yeter (P10 spec §3). Audit YAZILMAZ — türev okuma hiçbir
    # şey değiştirmez, denetim günlüğünü kart açılışlarıyla şişirmek anlamsızdır.
    # IZN-B5b Ek/6: tüketici proje özeti; satış alt sayfalarının Görür'ü bu ucu AÇMAZ.
    dependencies=[
        require_pages(
            (
                "genel.projeler",
                "genel.proje_takvimi",
                "proje.ozet",
                "proje.paylasim_tablosu",
            ),
            "view",
        )
    ],
)
async def get_project_costs_endpoint(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ProjectCostsResponse:
    return await cost_summary.get_project_costs(session, user, project_id)


# --- P-KK: kat karşılığı paylaşım (OKUMA) ---
#
# Yol `/{project_id}/land-share/...`tır ve ayrı bir router AÇILMAZ: iki uç da
# proje bağlamındadır ve `projects` izinleriyle korunur — yeni izin modülü
# açmak (`roles/seed_data.py`) migration doğururdu (K9).


@router.get(
    "/{project_id}/land-share/summary",
    response_model=LandShareSummaryResponse,
    # OKUMA ucu: `view` yeter; audit YAZILMAZ (`/costs` ucuyla aynı karar —
    # türev okuma hiçbir şey değiştirmez).
    dependencies=[require_permission("projects", AccessLevel.view)],
)
async def get_land_share_summary_endpoint(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> LandShareSummaryResponse:
    return await land_share.get_summary(session, user, project_id)


@router.get(
    "/{project_id}/land-share/units",
    response_model=LandShareUnitListResponse,
    dependencies=[require_permission("projects", AccessLevel.view)],
)
async def list_land_share_units_endpoint(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    owner_side: UnitOwnerSideFilter | None = None,
    block_id: uuid.UUID | None = None,
    q: str | None = None,
    limit: _LIMIT = 50,
    offset: _OFFSET = 0,
) -> LandShareUnitListResponse:
    return await land_share.list_units(
        session,
        user,
        project_id,
        owner_side=owner_side,
        block_id=block_id,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=ProjectDetailResponse,
    status_code=status.HTTP_201_CREATED,
    # Proje olusturma "Projeler" sayfasi Duzenler'dir (IZN-B2: eski admin esigi). IZN-B3:
    # olusturana ANA rolüyle ekip satiri yazilir (`add_creator_membership`), boylece olusturan
    # kendi yarattigi projeyi gorur; Sistem Yoneticisi / "Tum projeler" kisisi zaten gorur.
    dependencies=[require_page("genel.projeler", "edit")],
)
async def create_project_endpoint(
    request: Request,
    data: ProjectCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> ProjectDetailResponse:
    project = await service.create_project(session, data)
    await service.add_creator_membership(session, current_user, project)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.project_created(project.name),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    return await service.build_project_detail(session, project, current_user, scope)


@router.patch(
    "/{project_id}",
    response_model=ProjectDetailResponse,
    # IZN-B5b madde 1 (CEO A): oluştur + düzenle tek sayfada (`genel.projeler`); eskiden
    # `projects:full` = satış sekmelerinin Düzenler'i (patron/PM daralması bilinçli kabul).
    dependencies=[require_page("genel.projeler", "edit")],
)
async def update_project_endpoint(
    request: Request,
    project_id: uuid.UUID,
    data: ProjectUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> ProjectDetailResponse:
    project = await service.update_project(session, current_user, project_id, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.project_updated(project.name),
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    return await service.build_project_detail(session, project, current_user, scope)

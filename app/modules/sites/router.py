import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_deps import DisciplineScoped
from app.core.discipline_scope import DisciplineScope
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.core.slug import parse_ref
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.silme import service as silme_service
from app.modules.silme.params import DELETE_WITH_PREVIEW_RESPONSES, PreviewTokenQuery
from app.modules.sites import repository, service
from app.modules.sites.models import Section, Site
from app.modules.sites.schemas import (
    SectionCreate,
    SectionDetailResponse,
    SectionListResponse,
    SectionUpdate,
    SiteCreate,
    SiteDetailResponse,
    SiteListResponse,
    SiteUpdate,
)
from app.modules.users.models import User

# Uclar uc ayri kok altina dagildigi icin (/projects/../sites, /sites, /sections)
# router prefix TASIMAZ; yollar tam yazilir. Bolum uclari da "sites" iznine
# baglidir — bolum santiyenin ic kirilimidir, ayri modul degildir (spec §4).
# HASSAS ALAN MASKESİ (IZN-B4): `MaskeRotasi` yanıtı etkin rolün `hidden_fields`ına göre maskeler
#    ve bağlam bağımlılığını kendisi ekler (tek parça).
router = APIRouter(
    tags=["sites"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=MaskeRotasi,
)

_VIEW = require_permission("sites", AccessLevel.view)
_FULL = require_permission("sites", AccessLevel.full)
# SILME uclari `require_system_admin` ile kapilidir (SIL-B1): modul seviyesi degil rol ANAHTARI.


async def _audit(
    request: Request,
    session: AsyncSession,
    user: User,
    action: AuditAction,
    detail: str,
) -> None:
    """Denetim satiri (B5 deseni, `units/router.py` ile ayni imza).

    Metin PARAMETREDIR, burada kurulmaz: silme ve yayina alma metinleri servis
    katmaninda, satir yok olmadan / eski durum kaybolmadan ONCE kurulur.
    """
    await record_audit(
        session,
        action=action,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


async def _detail_of(
    session: AsyncSession, site: Site, actor: User, scope: DisciplineScope
) -> SiteDetailResponse:
    """Yazma uclarinin yaniti da okuma ucuyla ayni zarfi tasir.

    🔴 `actor` ILR-1'de EKLENDI ve varsayilani YOKTUR: bolum yuzdesi izne
    duyarlidir, izni olcmeden yanit uretmek fail-open bir yol acardi.
    """
    await session.refresh(site, attribute_names=["sections", "project"])
    return await service.build_site_detail(session, site, actor, site.project, scope)


@router.get("/projects/{project_id}/sites", response_model=SiteListResponse, dependencies=[_VIEW])
async def list_sites_endpoint(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> SiteListResponse:
    return await service.list_sites_overview(session, user, project_id, scope)


@router.post(
    "/projects/{project_id}/sites",
    response_model=SiteDetailResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL],
)
async def create_site_endpoint(
    request: Request,
    project_id: uuid.UUID,
    data: SiteCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> SiteDetailResponse:
    site = await service.create_site(session, current_user, project_id, data)
    # Taslak ve yayin AYRI metinlerdir (spec §10): denetim ekraninda "gercekten
    # santiye acildi mi" sorusu metinden cevaplanabilmelidir.
    created = (
        messages.site_draft_created(site.name)
        if data.is_draft
        else messages.site_created(site.name)
    )
    await _audit(request, session, current_user, AuditAction.create, created)
    # Bolumler icin TEK OZET satir (`units_bulk_created` deseni): bolum basina
    # satir yazilsaydi 5 bolumlu bir form 6 denetim satiri uretirdi.
    if data.sections:
        await _audit(
            request,
            session,
            current_user,
            AuditAction.create,
            messages.site_sections_created(site.name, len(data.sections)),
        )
    return await _detail_of(session, site, current_user, scope)


@router.get("/sites/{site_id}", response_model=SiteDetailResponse, dependencies=[_VIEW])
async def get_site_endpoint(
    site_id: str,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
    project: Annotated[str | None, Query()] = None,
) -> SiteDetailResponse:
    """URL-2 — yol parametresi UUID **ya da** slug (karar 2).

    `project` KAPSAM suzgecidir ve yalniz slug yolunda anlamlidir: `sites.slug`
    PROJE ICINDE tekildir, bu uc ise DUZDUR (yolda proje yok). Frontend URL'i
    (`/projeler/<p>/santiyeler/<s>`) nested oldugu icin bunu her zaman
    verebilir. Verilmezse cozumleme gorunur kume icinde TEK ADAY sartina
    baglidir — belirsizlik 404'tur (fail-closed), rastgele secim YOKTUR.
    Ayrinti: `_visible_site` docstring'i.
    """
    return await service.get_site_detail(
        session,
        user,
        parse_ref(site_id),
        scope,
        project_ref=parse_ref(project) if project is not None else None,
    )


@router.patch("/sites/{site_id}", response_model=SiteDetailResponse, dependencies=[_FULL])
async def update_site_endpoint(
    request: Request,
    site_id: uuid.UUID,
    data: SiteUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> SiteDetailResponse:
    # Metin SERVISTEN gelir: `is_draft: true -> false` gecisi ("yayına alındı")
    # duz guncellemeden ayirt edilebilsin diye — onceki `is_draft` degeri yalniz
    # orada gorunur.
    site, detail = await service.update_site(session, current_user, site_id, data)
    await _audit(request, session, current_user, AuditAction.update, detail)
    return await _detail_of(session, site, current_user, scope)


@router.delete(
    "/sites/{site_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=DELETE_WITH_PREVIEW_RESPONSES,
    dependencies=[require_system_admin()],
)
async def delete_site_endpoint(
    request: Request,
    site_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    preview_token: PreviewTokenQuery = None,
) -> None:
    """Santiyeyi bagli kayitlariyla birlikte siler. YALNIZ Sistem Yoneticisi; ONIZLEME ZORUNLU.

    Bolum, poz, blok, unite, puantaj, gunluk, belge, plan, sozlesme ve diger bagli kayitlar
    birlikte silinir. Once `GET /admin/silme/site/{id}/onizleme`, onay, sonra bu uc `preview_token`
    ile cagrilir: eksikse 428 `preview_required`; agac degistiyse 409 `preview_stale`; agacta
    mali kayit varsa 409 `financial_pending` (mali silme sonraki surumde acilacak).

    Gorunmeyen ve var olmayan santiye ayni yaniti verir. Yanit `204 No Content`, govdesiz. Denetim
    satirina silinen ve bagi kopan kayitlarin tam dokumu yazilir.
    """
    detail = await silme_service.sil(session, "site", site_id, preview_token)
    await _audit(request, session, current_user, AuditAction.delete, detail)


@router.get("/sites/{site_id}/sections", response_model=SectionListResponse, dependencies=[_VIEW])
async def list_sections_endpoint(
    site_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> SectionListResponse:
    return await service.list_sections_for_site(session, user, site_id, scope)


async def _owning_site_name(session: AsyncSession, section: Section) -> str:
    """Denetim metni icin santiye adi. Santiye yetki kontrolu sirasinda zaten
    yuklendigi icin bu cagri kimlik haritasindan doner, ek sorgu uretmez."""
    site = await repository.get_site(session, section.site_id)
    return site.name if site is not None else ""


@router.post(
    "/sites/{site_id}/sections",
    response_model=SectionDetailResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL],
)
async def create_section_endpoint(
    request: Request,
    site_id: uuid.UUID,
    data: SectionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> SectionDetailResponse:
    section = await service.create_section(session, current_user, site_id, data)
    await _audit(
        request,
        session,
        current_user,
        AuditAction.create,
        messages.section_created(await _owning_site_name(session, section), section.name),
    )
    return await service.build_section_detail(session, section, current_user, scope)


@router.get("/sections/{section_id}", response_model=SectionDetailResponse, dependencies=[_VIEW])
async def get_section_endpoint(
    section_id: str,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
    site: Annotated[str | None, Query()] = None,
    project: Annotated[str | None, Query()] = None,
) -> SectionDetailResponse:
    """P6 §5 — Bolum Detay ekraninin veri ucu.

    Izin modulu `sites`tir, AYRI bir modul acilmaz: bolum santiyenin ic
    kirilimidir (bkz. router docstring'i). Gorunurluk servistedir
    (`_visible_section`) — gorunmeyen bolum 404 doner ve govdesi var olmayan bir
    UUID'ninkiyle BIREBIR AYNIDIR.
    """
    return await service.get_section_detail(
        session,
        user,
        parse_ref(section_id),
        scope,
        site_ref=parse_ref(site) if site is not None else None,
        project_ref=parse_ref(project) if project is not None else None,
    )


@router.delete(
    "/sections/{section_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=DELETE_WITH_PREVIEW_RESPONSES,
    dependencies=[require_system_admin()],
)
async def delete_section_endpoint(
    request: Request,
    section_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    preview_token: PreviewTokenQuery = None,
) -> None:
    """Bolumu bagli kayitlariyla birlikte siler. YALNIZ Sistem Yoneticisi; ONIZLEME ZORUNLU.

    Kilometre tasi, dagitim, belge ve bolume yazilmis gunluk miktar satirlari birlikte silinir.
    Bagi kopan kayitlar (personel, puantaj, satinalma talebi…) SILINMEZ, yalniz bolum bagi
    kopar; onizlemede `detached` olarak gorunur. Once `GET /admin/silme/section/{id}/onizleme`,
    sonra bu uc `preview_token` ile: eksikse 428 `preview_required`; agac degistiyse 409
    `preview_stale`; mali kayit varsa 409 `financial_pending`. Kalan bolumlerin `sort_order`
    degerleri yeniden numaralanmaz. Yanit `204 No Content`, govdesiz.
    """
    detail = await silme_service.sil(session, "section", section_id, preview_token)
    await _audit(request, session, current_user, AuditAction.delete, detail)


@router.patch("/sections/{section_id}", response_model=SectionDetailResponse, dependencies=[_FULL])
async def update_section_endpoint(
    request: Request,
    section_id: uuid.UUID,
    data: SectionUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> SectionDetailResponse:
    # Metin SERVISTEN gelir (`update_site` deseni): `is_draft: true -> false`
    # gecisi ("yayına alındı") duz guncellemeden ayirt edilebilsin diye — onceki
    # `is_draft` degeri yalniz orada gorunur.
    section, detail = await service.update_section(session, current_user, section_id, data)
    await _audit(request, session, current_user, AuditAction.update, detail)
    return await service.build_section_detail(session, section, current_user, scope)

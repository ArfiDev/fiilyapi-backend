import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_deps import DisciplineScoped
from app.core.discipline_scope import DisciplineScope
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import kapsam_kapisi, require_permission
from app.core.ratelimit import client_ip
from app.core.scoped_route import kapsam_rotasi, kapsamdan_oku
from app.core.slug import parse_ref
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
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
# 🔴 KAPSAM MASKESİ — İKİ PARÇA DA GEREKLİ (kullanıcı kararı 2026-09-19):
#    `route_class` dönen modeli maskeler, `dependencies` aktörün kapsamını
#    köprüye yazar. Biri eksikse maske SESSİZCE `all` görür ve hiçbir şey
#    gizlemez. Çifti `tests/core/test_kapsam_baglantisi.py` çakar.
router = APIRouter(
    tags=["sites"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=kapsam_rotasi("sites", kapsamdan_oku),
    dependencies=[kapsam_kapisi("sites")],
)

_VIEW = require_permission("sites", AccessLevel.view)
_FULL = require_permission("sites", AccessLevel.full)
# KULLANICI KARARI 2026-07-30 ("silme = sistem yoneticisi"): SILME uclari yazma
# uclarindan BIR SEVIYE YUKARIDADIR. Neden `_FULL` DEGIL: `app/core/access.py`
# "full yazmayi kapsar, SILMEYI KAPSAMAZ" der; `units`/`blocks`/`boq` DELETE
# uclariyla birebir ayni desen (`units/router.py:181,206`).
#
# BILINEN SONUC (kabul edildi): seed matrisinde `sites:admin` yalniz
# `system_admin`'dedir — proje muduru dahil kimse santiye/bolum silemez.
_ADMIN = require_permission("sites", AccessLevel.admin)


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


@router.delete("/sites/{site_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_ADMIN])
async def delete_site_endpoint(
    request: Request,
    site_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Spec §7.1. CASCADE KORKULUGU servistedir — bolum/poz/blok varsa 409.

    Yetki kapisi korkuluktan ONCE calisir: yetkisiz aktor 403 alir ve santiyenin
    bagli kayit tasiyip tasimadigini OGRENEMEZ. Gorunmeyen santiye 404 doner ve
    govdesi var olmayan UUID'ninkiyle BIREBIR AYNIDIR.

    Yanit `204 No Content`, GOVDESIZ. Denetim metni servis icinde, satir yok
    olmadan ONCE kurulur; engellenen silme (409) istisna attigi icin buraya hic
    gelmez ve gunluge satir dusmez.
    """
    detail = await service.delete_site(session, current_user, site_id)
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
    "/sections/{section_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_ADMIN]
)
async def delete_section_endpoint(
    request: Request,
    section_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Spec §7.1. 🔴 **BU CUMLE BAYATTI VE DUZELTILDI (BC-3, 2026-09-05).**

    Eski metin *"`sections.id`'yi hedefleyen FK yok"* diyordu; `deletes.py`nin
    servis docstring'i bunu zaten curutmustu ama router'daki kopya duruyordu.
    ÖLÇÜLDÜ (`Base.metadata` uzerinden, kelime aramasiyla DEGIL): `sections.id`yi
    **ON BIR** FK hedefliyor — ikisi CASCADE (`boq_item_section_allocations`,
    `section_milestones` ve BC-3'un `section_documents`i), kalani SET NULL
    (`personnel`, `purchase_requests`, `sections.depends_on_section_id`,
    `site_diary_entries`, `site_plan_rows`, `stock_entry_lines`,
    `subcontractor_progress_payments`, `timesheet_entries`).
    Silme yine de kosulsuzdur cunku hicbiri RESTRICT DEGIL; kosulsuzlugun
    gerekcesi "FK yok" DEGIL, "engelleyen FK yok"tur.

    Kapi `_ADMIN`'dir — bolum santiyenin ic kirilimi oldugu icin `sites`
    modulunun seviyeleri kullanilir, AYRI izin modulu acilmaz.

    Yanit `204 No Content`, GOVDESIZ. Denetim metni servis icinde, satir yok
    olmadan ONCE kurulur.
    """
    detail = await service.delete_section(session, current_user, section_id)
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

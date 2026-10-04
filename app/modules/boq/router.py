import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.core import http
from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_deps import DisciplineScoped, RequireUnrestricted
from app.core.openapi import COMMON_ERROR_RESPONSES, DELETE_403_YANITI
from app.core.permissions import kapsam_kapisi, require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.core.scoped_route import kapsam_rotasi, kapsamdan_oku, kapsamla_maskele
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.boq import section_distribution, service
from app.modules.boq.export import build_boq_workbook
from app.modules.boq.schemas import (
    BoqGroupCreate,
    BoqGroupResponse,
    BoqGroupUpdate,
    BoqItemAllocationsReplace,
    BoqItemAllocationsResponse,
    BoqItemCreate,
    BoqItemResponse,
    BoqItemUpdate,
    BoqListResponse,
    SectionDistributionResponse,
    SectionDistributionSave,
)
from app.modules.users.models import User

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Spec §4 karari: BOQ okuma/yazma uclari "sites" degil kendi "boq" iznine
# baglidir — site_chief/field_engineer'i ayirmanin tek yolu budur. Yazma
# uclari (T5/T6) "_FULL" kullanir (view yetmez).
# Uc kokleri bilincli karisiktir (plan §Frontend notu): GET + POST'lar
# `/sites/...` altinda, PATCH'lar `/boq/...` kokunde (dolayli kimlik
# cozumlemesi kullandiklari icin yol parametreleri farkli).
# 🔴 KAPSAM MASKESİ — İKİ PARÇA DA GEREKLİ (kullanıcı kararı 2026-09-19):
#    `route_class` dönen modeli maskeler, `dependencies` aktörün kapsamını
#    köprüye yazar. Biri eksikse maske SESSİZCE `all` görür ve hiçbir şey
#    gizlemez. Çifti `tests/core/test_kapsam_baglantisi.py` çakar.
router = APIRouter(
    tags=["boq"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=kapsam_rotasi("boq", kapsamdan_oku),
    dependencies=[kapsam_kapisi("boq")],
)

_VIEW = require_permission("boq", AccessLevel.view)
_FULL = require_permission("boq", AccessLevel.full)
# KULLANICI KARARI 2026-07-30: silme YALNIZ sistem yoneticisindedir. `full`
# yazmayi kapsar, SILMEYI KAPSAMAZ (`app/core/access.py` §5.0).
_ADMIN = require_permission("boq", AccessLevel.admin)


#: BOQ-SEC K5 — bolum suzgeci. Uc EKLENMEZ, mevcut uca parametre eklenir:
#: iki okuma yolu iki farkli hesap uretir ve zamanla ayrisirdi.
_SECTION_FILTER = Annotated[
    uuid.UUID | None,
    Query(
        description=(
            "Bolum suzgeci. Verilirse yalniz o bolume tahsisi olan kalemler doner ve "
            "`quantity` o bolume tahsis edilen miktardir (poz kotasi degil)."
        )
    ),
]


@router.get("/sites/{site_id}/boq", response_model=BoqListResponse, dependencies=[_VIEW])
async def get_boq_endpoint(
    site_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
    section_id: _SECTION_FILTER = None,
) -> BoqListResponse:
    """`section_id` YOKSA davranis birebir eskisidir (BOQ-SEC K5).

    Baska santiyenin bolum kimligi BOS LISTE degil **404** alir
    (`service.visible_section_in_site` gerekcesi).
    """
    return await service.get_boq_for_site(session, user, site_id, section_id, scope)


@router.get(
    "/sites/{site_id}/boq/export",
    dependencies=[_VIEW],
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Excel dosyasi"}},
)
async def export_boq_endpoint(
    site_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
    section_id: _SECTION_FILTER = None,
) -> Response:
    """Spec §5.3: BOQ'yu xlsx olarak indirir. Okuma ucudur — `record_audit`
    cagirmaz (T7 kurali: okumalar denetim gunlugune yazmaz).

    BOQ-SEC K5: `section_id` ekran ucuyla AYNI cagriyi besler
    (`get_boq_export_for_site`) — ikinci bir suzme kodu yazilmaz, yoksa Excel
    ile ekran zamanla ayrisirdi.

    🔴 **MASKE BURADA ELLE UYGULANIR** (2026-09-19 kacak-uc onarimi). Rota
    sarmalayicisi yalnizca `BaseModel` donuslerini maskeler ve bu uc `Response`
    (xlsx baytlari) doner — yani sarmalayici onu AYNEN geciriyordu. Sonuc:
    `boq = view/limited` olan rol (santiye sefi, satinalma) ekranda `—` gordugu
    birim fiyati ve tutari AYNI KAPIDAN (`boq:view`) dosya olarak tam degeriyle
    indiriyordu. Maskenin en buyuk tek deligi buydu.

    🔴 **Neden 403 DEGIL, MASKE.** Iki secenek de kacagi kapatirdi; olculdu ve
    maske secildi:
      * Dosyanin ISI kisitli rol icin de gecerlidir: `limited` rolde metraj ve
        poz kimligi GORUNURDUR (kova tablosu), yani santiye sefinin sahada
        kullandigi metraj listesi maskeden sonra da calisir. 403 vermek onu
        bugun yapabildigi isi yapamaz hale getirirdi — kapsam kisiti bir
        GIZLEME karari, bir IS DURDURMA karari degildir.
      * Ekran ile dosya AYNI zarftan uretilir (yukaridaki K5 gerekcesi); ekranda
        gorunen kume ile dosyada gorunen kume de boylece AYNI kalir. 403,
        "ekranda var ama indiremiyorum" diye aciklanamaz bir ayrisma yaratirdi.

    Zarf **build_boq_workbook'a girmeden ONCE** maskelenir: kitaba ham deger
    yazip sonra hucre silmek iki ayri gizleme kuralı uretir ve zamanla ayrisirdi.
    Bekcisi `tests/core/test_kapsam_kacak_uclar.py`.
    """
    site, boq = await service.get_boq_export_for_site(session, user, site_id, section_id, scope)
    buffer = build_boq_workbook(kapsamla_maskele(boq, "boq"))
    filename = f"is-kalemleri-{site.code}.xlsx"
    return Response(
        content=buffer.getvalue(),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": http.content_disposition(filename)},
    )


@router.post(
    "/sites/{site_id}/boq/groups",
    response_model=BoqGroupResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL, RequireUnrestricted],
)
async def create_boq_group_endpoint(
    request: Request,
    site_id: uuid.UUID,
    data: BoqGroupCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> BoqGroupResponse:
    # DSC-B2 (Ü6): yeni grup acmak yapisal islemdir → kisitliya 403 (`RequireUnrestricted`).
    group = await service.create_group(session, user, site_id, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.boq_group_created(group.name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await service.group_response(session, group)


@router.post(
    "/sites/{site_id}/boq/items",
    response_model=BoqItemResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL],
)
async def create_boq_item_endpoint(
    request: Request,
    site_id: uuid.UUID,
    data: BoqItemCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> BoqItemResponse:
    # DSC-B2: kisitli kullanici yalniz KENDI disiplinindeki gruba kalem ekler; gorunmeyen
    # grup olmayanla AYNI 422'yi alir (Ü7).
    item = await service.create_item(session, user, site_id, data, scope)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.boq_item_created(item.code, item.description),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await service.item_response(session, item, user)


@router.patch("/boq/groups/{group_id}", response_model=BoqGroupResponse, dependencies=[_FULL])
async def update_boq_group_endpoint(
    request: Request,
    group_id: uuid.UUID,
    data: BoqGroupUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> BoqGroupResponse:
    # DSC-B2 (S6): kisitli kendi grubunu gunceller; gorunmeyen grup 404.
    group = await service.update_group(session, user, group_id, data, scope)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.boq_group_updated(group.name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await service.group_response(session, group)


@router.patch("/boq/items/{item_id}", response_model=BoqItemResponse, dependencies=[_FULL])
async def update_boq_item_endpoint(
    request: Request,
    item_id: uuid.UUID,
    data: BoqItemUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> BoqItemResponse:
    # DSC-B2: gorunmeyen kalem 404; tasinacak grup gorunur degilse 422.
    item = await service.update_item(session, user, item_id, data, scope)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.boq_item_updated(item.code, item.description),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await service.item_response(session, item, user)


@router.get(
    "/boq/items/{item_id}/allocations",
    response_model=BoqItemAllocationsResponse,
    dependencies=[_VIEW],
)
async def get_boq_item_allocations_endpoint(
    item_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> BoqItemAllocationsResponse:
    """BOQ-ALLOC — pozun bolum tahsislerinin TAMAMI, TEK cagrida.

    🔴 Bu uc olmadan `PUT .../allocations` yazmaya ACILAMAZ: PUT tam kume
    degistirmedir (K4) ve kismi gorusu olan bir ekran gormedigi bolumlerin
    paylarini sessizce siler. Bolum ekrani yalniz KENDI payini gorur.

    Kapi `_VIEW`dir (K1), PUT'un `_FULL`u DEGIL: okuma ucudur ve izin matrisi
    DEGISMEZ. `record_audit` CAGIRILMAZ (K3, T7 kurali — `export_boq_endpoint`
    emsali). Gorunmeyen kalem **404** alir, 403 degil (K2).
    """
    return await service.get_allocations(session, user, item_id, scope)


@router.put(
    "/boq/items/{item_id}/allocations",
    response_model=BoqItemAllocationsResponse,
    dependencies=[_FULL],
)
async def replace_boq_item_allocations_endpoint(
    request: Request,
    item_id: uuid.UUID,
    data: BoqItemAllocationsReplace,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> BoqItemAllocationsResponse:
    """BOQ-SEC K4 — pozun bolum tahsislerini TAM KUME olarak degistirir.

    Kapi `_FULL`dur (K8): tahsis YAZMADIR, mevcut BOQ yazma uclariyla BIREBIR
    ayni izin. Yeni izin modulu ACILMAZ, izin matrisi DEGISMEZ.

    Govdedeki `allocations` alani ZORUNLUDUR: gonderilmezse 422. Bos dizi `[]`
    tum tahsisleri kaldirir — "dokunma" anlami YOKTUR (K4).
    """
    result = await service.replace_allocations(session, user, item_id, data, scope)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.boq_item_allocations_replaced(result.item.code, len(result.allocations)),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return result


@router.get(
    "/sites/{site_id}/boq/section-distribution",
    response_model=SectionDistributionResponse,
    dependencies=[_VIEW],
)
async def get_section_distribution_endpoint(
    site_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> SectionDistributionResponse:
    """BDG-B1 — santiyenin kalem x bolum dagilim matrisi (okuma ucu).

    `record_audit` CAGIRILMAZ (okumalar denetim gunlugune yazmaz). Gorunmeyen
    santiye **404**; kisitli kullanicida yalniz gorunur kalemler ve onlardan
    tureyen sayaclar doner (`boq/section_distribution.py`).
    """
    return await section_distribution.build_section_distribution(session, user, site_id, scope)


@router.put(
    "/sites/{site_id}/boq/section-distribution",
    response_model=SectionDistributionResponse,
    dependencies=[_FULL],
)
async def save_section_distribution_endpoint(
    request: Request,
    site_id: uuid.UUID,
    data: SectionDistributionSave,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> SectionDistributionResponse:
    """BDG-B1 — matris yazma, BIRLESTIRME semantigi (govdede gecmeyen hucre korunur).

    Kapi `PUT /boq/items/{id}/allocations` ile AYNI (`_FULL`). Denetim: GERCEKTEN
    DEGISEN her kalem icin `replace_allocations` ile ayni kayit turu; degismeyen
    hucre kayit yazmaz.
    """
    result = await section_distribution.save_section_distribution(
        session, user, site_id, data, scope
    )
    for changed in result.changed:
        await record_audit(
            session,
            action=AuditAction.update,
            detail=messages.boq_item_allocations_replaced(changed.code, changed.section_count),
            actor_user_id=user.id,
            ip_address=client_ip(request),
        )
    return result.matrix


@router.delete(
    "/boq/groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **DELETE_403_YANITI,
        409: {"description": "Grupta iş kalemi var; önce kalemleri silin"},
    },
    dependencies=[require_system_admin()],
)
async def delete_boq_group_endpoint(
    request: Request,
    group_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """İş kalemi grubunu siler. YALNIZ Sistem Yöneticisi.

    Yalnız BOŞ grup silinir; kalemi olan grup **409** (iş kuralı).
    """
    # SIL-B1: grup silme YALNIZ Sistem Yoneticisi'nindir; disiplin kisiti DELETE'te uygulanmaz.
    name = await service.delete_group(session, user, group_id)
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=messages.boq_group_deleted(name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


@router.delete(
    "/boq/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **DELETE_403_YANITI,
        409: {"description": "Kaleme yazılmış günlük kayıt satırı var; önce günlüklerden çıkarın"},
    },
    dependencies=[require_system_admin()],
)
async def delete_boq_item_endpoint(
    request: Request,
    item_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> None:
    """İş kalemini (poz) siler. YALNIZ Sistem Yöneticisi.

    Kaleme yazılmış günlük satırı varsa **409** (iş kuralı, Sistem Yöneticisi'ni de durdurur).
    Frontend F13 (kalem silme) bu uca bağlıdır.
    """
    code, description = await service.delete_item(session, user, item_id, scope)
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=messages.boq_item_deleted(code, description),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )

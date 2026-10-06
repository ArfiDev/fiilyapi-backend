"""Sözleşmeler (P5) uçları — task C5 yalnız birleşik liste ucunu açar.

`boq/router.py` deseninin aynısı: kapı sabitleri modül düzeyinde tanımlanır,
sonraki task'lar (C6-C12) `_VIEW`/`_FULL`/`_ADMIN`'i buradan import eder.
"""

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_deps import RequireUnrestricted
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES, DELETE_403_YANITI
from app.core.permissions import (
    require_page,
    require_pages,
    require_permission,
    require_system_admin,
)
from app.core.ratelimit import client_ip
from app.core.slug import parse_ref
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.contracts import (
    distribution,
    last_price_provider,
    service,
    subcontractors,
    subcontracts,
)
from app.modules.contracts.models import ContractStatus
from app.modules.contracts.schemas import (
    ContractDistributionResponse,
    ContractDistributionSave,
    ContractListResponse,
    ContractType,
    EmployerContractDetail,
    EmployerContractGroupCreate,
    EmployerContractGroupResponse,
    EmployerContractGroupUpdate,
    EmployerContractItemCreate,
    EmployerContractItemResponse,
    EmployerContractItemsBulkCreate,
    EmployerContractItemsBulkResponse,
    EmployerContractItemsResponse,
    EmployerContractItemUpdate,
    SubcontractorContractCreate,
    SubcontractorContractDetail,
    SubcontractorContractItemCreate,
    SubcontractorContractItemResponse,
    SubcontractorContractItemsLoadResponse,
    SubcontractorContractItemUpdate,
    SubcontractorContractListResponse,
    SubcontractorContractUpdate,
    SubcontractorCreate,
    SubcontractorListResponse,
    SubcontractorResponse,
    SubcontractorUpdate,
)
from app.modules.subcontractor_progress_payments import lines as subcontractor_payment_lines
from app.modules.users.models import User

# HASSAS ALAN MASKESİ (IZN-B4): `MaskeRotasi` tek parça (bağlamı kendisi ekler).
router = APIRouter(
    tags=["contracts"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=MaskeRotasi,
)

# TKL-B3.2: son fiyat portuna kayit (import yan etkisi; bekci testi uygulama acilisinda ister).
last_price_provider.register()

_VIEW = require_permission("contracts", AccessLevel.view)
# IZN-B3: çok proje LİSTE uçları (parametresiz proje verisi): ana rol VEYA ekip rolü açar, satırlar
# `visible_projects`te proje başına o projedeki rolle süzülür.
_VIEW_LISTE = require_permission("contracts", AccessLevel.view, multi_project=True)
# IZN-B5b madde 7: sözleşme yazmaları sayfa başına (CEO kararı 3 + 5).
_DAGILIM_EDIT = require_pages(("teklif.poz_dagilimi", "proje.is_kalemleri"), "edit")
_ISVEREN_EDIT = require_pages(("teklif.isveren_sozlesme", "proje.is_kalemleri"), "edit")
_TASERON_FIRMA_EKLE = require_pages(("teklif.taseron_firmalar", "teklif.sozlesmeler"), "edit")
_TASERON_FIRMA_EDIT = require_page("teklif.taseron_firmalar", "edit")
_TASERON_SOZ_OLUSTUR = require_pages(("teklif.sozlesmeler", "teklif.taseron_sozlesme"), "edit")
_TASERON_SOZ_EDIT = require_page("teklif.taseron_sozlesme", "edit")
# KULLANICI KARARI 2026-07-30 (kalıcı karar 2, `boq/router.py` deseninin aynısı):
# silme YALNIZ sistem yöneticisindedir — `full` yazmayı kapsar, SİLMEYİ KAPSAMAZ.
_ADMIN = require_permission("contracts", AccessLevel.admin)


@router.get("/contracts", response_model=ContractListResponse, dependencies=[_VIEW_LISTE])
async def list_contracts_endpoint(
    contract_type: Annotated[ContractType, Query(alias="type")],
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    project_id: uuid.UUID | None = None,
    status_filter: Annotated[ContractStatus | None, Query(alias="status")] = None,
    q: str | None = None,
) -> ContractListResponse:
    return await service.list_contracts(session, user, contract_type, project_id, status_filter, q)


# --- İşveren sözleşmesi: okuma + poz grup/kalem yazma (task C6, spec §6.2) ---
#
# Uç kökleri `boq/router.py` deseninin aynısı: GET/POST alt kaynakta
# `/projects/{project_id}/contract/...` altında, PATCH düz kökte
# `/contracts/employer/...` (dolaylı kimlik çözümlemesi kullanır).
#
# Denetim günlüğü mesajları `app/modules/audit/messages.py`de merkezileşir
# (spec §8, task C13).


# --- Taşeron kartoteksi (task C9, spec §3.4/§6.4) ---


@router.get(
    "/projects/{project_id}/contract",
    response_model=EmployerContractDetail,
    dependencies=[_VIEW],
)
async def get_employer_contract_endpoint(
    project_id: str,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> EmployerContractDetail:
    """URL-4 — proje anahtarı UUID **ya da** PROJE SLUG'ı olabilir
    (`/sozlesmeler/isveren/kopru-guclendirme`).

    🔴 Ayrı bir sözleşme slug'ı AÇILMADI ve `project_contracts` tablosuna kolon
    EKLENMEDİ: bu ucun kimliği zaten PROJEDİR (`project_contracts` PK'sı
    `project_id`) ve projenin slug'ı URL-2'de zaten üretilmiştir. İkinci bir
    slug aynı kaydı iki adla anılır kılardı.
    """
    return await service.get_employer_contract_detail(session, user, parse_ref(project_id))


@router.get(
    "/projects/{project_id}/contract/items",
    response_model=EmployerContractItemsResponse,
    dependencies=[_VIEW],
)
async def get_employer_contract_items_endpoint(
    project_id: str,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> EmployerContractItemsResponse:
    """URL-4 — proje anahtarı UUID ya da proje slug'ı (ekranın ikinci isteği)."""
    return await service.get_employer_contract_items(session, user, parse_ref(project_id))


@router.get(
    "/projects/{project_id}/contract/distribution",
    response_model=ContractDistributionResponse,
    dependencies=[_VIEW, RequireUnrestricted],
)
async def get_contract_distribution_endpoint(
    project_id: str,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ContractDistributionResponse:
    """URL-4 — proje anahtarı UUID ya da proje slug'ı (ekranın üçüncü isteği).

    🔴 PUT ikizi (`save_contract_distribution`) `uuid.UUID` KALIR — URL-2
    kararı 3: yalnız OKUMA uçları anahtar kabul eder.
    """
    return await distribution.build_distribution(session, user, parse_ref(project_id))


@router.put(
    "/projects/{project_id}/contract/distribution",
    response_model=ContractDistributionResponse,
    dependencies=[_DAGILIM_EDIT, RequireUnrestricted],
)
async def save_contract_distribution_endpoint(
    request: Request,
    project_id: uuid.UUID,
    data: ContractDistributionSave,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ContractDistributionResponse:
    """`POZ` 24 "Dağılımı Kaydet" — ekranın tamamı tek atomik istekte."""
    result = await distribution.save_distribution(session, user, project_id, data)
    project = await service._visible_project(session, user, project_id)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.contract_distribution_saved(project.name, len(data.allocations)),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return result


@router.post(
    "/projects/{project_id}/contract/groups",
    response_model=EmployerContractGroupResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_ISVEREN_EDIT],
)
async def create_employer_contract_group_endpoint(
    request: Request,
    project_id: uuid.UUID,
    data: EmployerContractGroupCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> EmployerContractGroupResponse:
    group, project = await service.create_employer_group(session, user, project_id, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.employer_contract_group_created(project.name, group.name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return EmployerContractGroupResponse(id=group.id, name=group.name, sort_order=group.sort_order)


@router.patch(
    "/contracts/employer/groups/{group_id}",
    response_model=EmployerContractGroupResponse,
    dependencies=[_ISVEREN_EDIT],
)
async def update_employer_contract_group_endpoint(
    request: Request,
    group_id: uuid.UUID,
    data: EmployerContractGroupUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> EmployerContractGroupResponse:
    group, project = await service.update_employer_group(session, user, group_id, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.employer_contract_group_updated(project.name, group.name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return EmployerContractGroupResponse(id=group.id, name=group.name, sort_order=group.sort_order)


@router.delete(
    "/contracts/employer/groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**DELETE_403_YANITI, 409: {"description": "Grupta poz var; önce pozları silin"}},
    dependencies=[require_system_admin()],
)
async def delete_employer_contract_group_endpoint(
    request: Request,
    group_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """İşveren sözleşme grubunu siler. YALNIZ Sistem Yöneticisi.

    Pozu olan grup **409** (iş kuralı).
    """
    project_name, group_name = await service.delete_employer_group(session, user, group_id)
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=messages.employer_contract_group_deleted(project_name, group_name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


@router.post(
    "/projects/{project_id}/contract/items",
    response_model=EmployerContractItemResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_ISVEREN_EDIT],
)
async def create_employer_contract_item_endpoint(
    request: Request,
    project_id: uuid.UUID,
    data: EmployerContractItemCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> EmployerContractItemResponse:
    item, project = await service.create_employer_item(session, user, project_id, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.employer_contract_item_created(project.name, item.code, item.description),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    # Yeni oluşturulan kaleme henüz hiçbir BOQ satırı bağlı OLAMAZ — dağıtım
    # sıfırdır, ek sorgu atmaya gerek yok.
    return service.to_item_response(item, Decimal("0"))


@router.post(
    "/projects/{project_id}/contract/items/bulk",
    response_model=EmployerContractItemsBulkResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_ISVEREN_EDIT],
)
async def create_employer_contract_items_bulk_endpoint(
    request: Request,
    project_id: uuid.UUID,
    data: EmployerContractItemsBulkCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> EmployerContractItemsBulkResponse:
    """TKL-B3.1: çoklu katalog seçicisi için toplu poz ekleme (tek işlem, hep-ya-hiç).

    Kapı tekil `+ Poz Ekle` ucuyla AYNIDIR; denetim günlüğüne TEK satır yazılır.
    """
    items, project = await service.create_employer_items_bulk(session, user, project_id, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.employer_contract_items_bulk_created(
            project.name, [item.code for item in items]
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return EmployerContractItemsBulkResponse(
        items=[service.to_item_response(item, Decimal("0")) for item in items]
    )


@router.patch(
    "/contracts/employer/items/{item_id}",
    response_model=EmployerContractItemResponse,
    dependencies=[_ISVEREN_EDIT],
)
async def update_employer_contract_item_endpoint(
    request: Request,
    item_id: uuid.UUID,
    data: EmployerContractItemUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> EmployerContractItemResponse:
    item, project, refreshed_boq_count = await service.update_employer_item(
        session, user, item_id, data
    )
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.employer_contract_item_updated(
            project.name, item.code, item.description, refreshed_boq_count
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await service.to_item_response_single(session, project.id, item)


@router.delete(
    "/contracts/employer/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**DELETE_403_YANITI},
    dependencies=[require_system_admin()],
)
async def delete_employer_contract_item_endpoint(
    request: Request,
    item_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """İşveren sözleşme kalemini (poz) siler. YALNIZ Sistem Yöneticisi.

    Engel yok: bağlı `boq_items.contract_item_id` DB'de `ON DELETE SET NULL` ile serbest kalır,
    satır SİLİNMEZ.
    """
    project_name, code, description = await service.delete_employer_item(session, user, item_id)
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=messages.employer_contract_item_deleted(project_name, code, description),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


# --- Taşeron kartoteksi (task C9, spec §6.4) ---
#
# `employers_router` (`app/modules/projects/router.py`) deseninin birebiri.
# DELETE bu task'ta AÇILMAZ — C12'nin işi (409 `SUBCONTRACTOR_HAS_CONTRACTS`).
# `visible_projects` süzgeci BİLİNÇLİ OLARAK yok: kartoteks proje-bağımsızdır
# (`Employer` de aynı şekilde geçmiyor).


@router.get(
    "/subcontractors",
    response_model=SubcontractorListResponse,
    dependencies=[_VIEW],
)
async def list_subcontractors_endpoint(
    session: DbSession,
    q: str | None = None,
    active_only: bool = True,
) -> SubcontractorListResponse:
    items = await subcontractors.list_subcontractors(session, q, active_only)
    return SubcontractorListResponse(items=[SubcontractorResponse.model_validate(s) for s in items])


@router.post(
    "/subcontractors",
    response_model=SubcontractorResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_TASERON_FIRMA_EKLE],
)
async def create_subcontractor_endpoint(
    request: Request,
    data: SubcontractorCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorResponse:
    subcontractor = await subcontractors.create_subcontractor(session, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.subcontractor_created(subcontractor.name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return SubcontractorResponse.model_validate(subcontractor)


@router.patch(
    "/subcontractors/{subcontractor_id}",
    response_model=SubcontractorResponse,
    dependencies=[_TASERON_FIRMA_EDIT],
)
async def update_subcontractor_endpoint(
    request: Request,
    subcontractor_id: uuid.UUID,
    data: SubcontractorUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorResponse:
    subcontractor = await subcontractors.update_subcontractor(session, subcontractor_id, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.subcontractor_updated(subcontractor.name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return SubcontractorResponse.model_validate(subcontractor)


@router.delete(
    "/subcontractors/{subcontractor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **DELETE_403_YANITI,
        409: {"description": "Taşeronun sözleşmesi ya da günlük işçi kaydı var; önce onları silin"},
    },
    dependencies=[require_system_admin()],
)
async def delete_subcontractor_endpoint(
    request: Request,
    subcontractor_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Taşeron firmasını siler. YALNIZ Sistem Yöneticisi.

    Sözleşmesi (ya da günlük işçi sayısı kaydı) olan taşeron **409** (iş kuralı).
    """
    name = await subcontractors.delete_subcontractor(session, subcontractor_id)
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=messages.subcontractor_deleted(name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


# --- Taşeron sözleşmesi POST/GET/PATCH (task C10, spec §6.5) ---
#
# DELETE, kalem uçları ve `load-from-employer` bu task'ta AÇILMAZ — C11/C12'nin
# işi. Denetim mesajları `app/modules/audit/messages.py`de merkezileşir (spec
# §8, task C13); taslak→yayın geçişi `subcontract_published` ile AYRI bir
# metin üretir (`sites.update_site`/`messages.site_published` deseninin
# aynısı) — router `is_draft`in ÖNCEKİ değerini göremez, ayrımı servis
# `is_publishing` olarak döner.


@router.post(
    "/projects/{project_id}/subcontractor-contracts",
    response_model=SubcontractorContractDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_TASERON_SOZ_OLUSTUR],
)
async def create_subcontractor_contract_endpoint(
    request: Request,
    project_id: uuid.UUID,
    data: SubcontractorContractCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorContractDetail:
    contract, project = await subcontracts.create_subcontractor_contract(
        session, user, project_id, data
    )
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.subcontract_created(
            project.name,
            messages.subcontract_label(contract.contract_no, contract.subcontractor_name),
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await subcontracts.to_subcontract_detail(session, contract)


# ⚠️ ROTA SIRASI: statik `/subcontractor-contracts` yolu, ŞABLONLU
# `/subcontractor-contracts/{contract_id}` yolundan ÖNCE tanımlanır
# (`subcontractor_progress_payments` `/summary` ucundaki kuralın aynısı).
# Bekçi testi: `tests/contracts/test_subcontract_list.py::
# test_liste_yolu_detay_ucuyla_carpismaz`.
@router.get(
    "/subcontractor-contracts",
    response_model=SubcontractorContractListResponse,
    dependencies=[_VIEW_LISTE],
)
async def list_subcontractor_contracts_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    project_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    status_filter: Annotated[ContractStatus | None, Query(alias="status")] = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SubcontractorContractListResponse:
    """TB2 U1 (spec §1): hakediş açma akışının seçim adımı bu uçtan beslenir —

    hakedişlerden türetme, hiç hakedişi olmayan sözleşmeyi göremiyordu.
    Sıralama `contract_no`+`id`. TB3 T2: `subcontractor_progress_payments`
    liste ucunun sayfalama deseni (`total`/`limit`/`offset`) — parametresiz
    çağrı varsayılan limiti uygular ama `total`ı döndürür, böylece istemci
    kırpılmayı GÖREBİLİR.
    """
    return await subcontracts.list_subcontractor_contracts(
        session,
        user,
        project_id=project_id,
        site_id=site_id,
        status_filter=status_filter,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/subcontractor-contracts/{contract_id}",
    response_model=SubcontractorContractDetail,
    dependencies=[_VIEW],
)
async def get_subcontractor_contract_endpoint(
    contract_id: str,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorContractDetail:
    """URL-4 — yol parametresi UUID **ya da** sözleşme slug'ı kabul eder
    (`/sozlesmeler/taseron/tsz-2026-004`). Yol adı `contract_id` KALIR.

    Görünmeyen projedeki sözleşmenin slug'ı da **404** alır: `_visible_contract`
    çözümden SONRA `_visible_project`e uğrar ve gövde var olmayan kaydınkiyle
    BİREBİR aynıdır (IDOR — slug TAHMİN EDİLEBİLİR, UUID değil).
    """
    contract = await subcontracts.get_subcontractor_contract(session, user, parse_ref(contract_id))
    return await subcontracts.to_subcontract_detail(session, contract)


@router.patch(
    "/subcontractor-contracts/{contract_id}",
    response_model=SubcontractorContractDetail,
    dependencies=[_TASERON_SOZ_EDIT],
)
async def update_subcontractor_contract_endpoint(
    request: Request,
    contract_id: uuid.UUID,
    data: SubcontractorContractUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorContractDetail:
    (
        contract,
        project,
        is_publishing,
        site_changed,
    ) = await subcontracts.update_subcontractor_contract(session, user, contract_id, data)
    # TB4 T6 (karar S9/2): şantiye değişti = günlük köprüsü değişti. TASLAK
    # hakedişlerin `diary` damgası yeni şantiyenin günlüğüyle yeniden sınanır;
    # onaylı/ödenmiş evrak DONMUŞTUR. Tetikleme burada durur, `subcontracts`
    # servisinde DEĞİL: hakediş paketi `contracts`a bağlıdır, tersi olsaydı
    # paketler arası import çemberi doğardı. Router hiçbir modül tarafından
    # import EDİLMEZ — kompozisyon katmanı olarak ikisini de tanıyabilir.
    if site_changed:
        await subcontractor_payment_lines.restamp_draft_payments(session, contract)
    label = messages.subcontract_label(contract.contract_no, contract.subcontractor_name)
    detail = (
        messages.subcontract_published(project.name, label)
        if is_publishing
        else messages.subcontract_updated(project.name, label)
    )
    await record_audit(
        session,
        action=AuditAction.update,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await subcontracts.to_subcontract_detail(session, contract)


@router.delete(
    "/subcontractor-contracts/{contract_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **DELETE_403_YANITI,
        409: {"description": "Faturaya bağlı hakedişi olan sözleşme silinemez (veri bütünlüğü)"},
    },
    dependencies=[require_system_admin()],
)
async def delete_subcontractor_contract_endpoint(
    request: Request,
    contract_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Taşeron sözleşmesini siler. YALNIZ Sistem Yöneticisi.

    Kalemleri ve hakedişleri CASCADE ile birlikte gider. Hakedişi faturaya bağlıysa DB kısıtı
    nedeniyle **409** (`Veri bütünlüğü hatası`, `code` yok).
    """
    (
        project_name,
        contract_no,
        subcontractor_name,
    ) = await subcontracts.delete_subcontractor_contract(session, user, contract_id)
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=messages.subcontract_deleted(
            project_name, messages.subcontract_label(contract_no, subcontractor_name)
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


# --- Taşeron sözleşmesi kalemleri + `load-from-employer` (task C11, spec §6.5) ---
#
# DELETE bu task'ta AÇILMAZ — C12'nin işi. Denetim mesajları `audit/
# messages.py`de merkezileşir (spec §8, task C13).


@router.post(
    "/subcontractor-contracts/{contract_id}/items",
    response_model=SubcontractorContractItemResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_TASERON_SOZ_EDIT],
)
async def create_subcontract_item_endpoint(
    request: Request,
    contract_id: uuid.UUID,
    data: SubcontractorContractItemCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorContractItemResponse:
    item, contract, _ = await subcontracts.create_subcontract_item(session, user, contract_id, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.subcontract_item_created(contract.contract_no, item.code),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await subcontracts.to_subcontract_item_response(session, item)


@router.patch(
    "/subcontractor-contracts/items/{item_id}",
    response_model=SubcontractorContractItemResponse,
    dependencies=[_TASERON_SOZ_EDIT],
)
async def update_subcontract_item_endpoint(
    request: Request,
    item_id: uuid.UUID,
    data: SubcontractorContractItemUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorContractItemResponse:
    item, contract, _ = await subcontracts.update_subcontract_item(session, user, item_id, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.subcontract_item_updated(contract.contract_no, item.code),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await subcontracts.to_subcontract_item_response(session, item)


@router.delete(
    "/subcontractor-contracts/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**DELETE_403_YANITI},
    dependencies=[require_system_admin()],
)
async def delete_subcontract_item_endpoint(
    request: Request,
    item_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Taşeron sözleşme kalemini siler. YALNIZ Sistem Yöneticisi. Engel yok."""
    contract_no, code = await subcontracts.delete_subcontract_item(session, user, item_id)
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=messages.subcontract_item_deleted(contract_no, code),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


@router.post(
    "/subcontractor-contracts/{contract_id}/items/load-from-employer",
    response_model=SubcontractorContractItemsLoadResponse,
    dependencies=[_TASERON_SOZ_EDIT],
)
async def load_subcontract_items_from_employer_endpoint(
    request: Request,
    contract_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorContractItemsLoadResponse:
    created_count, skipped_count, contract, _ = await subcontracts.load_items_from_employer(
        session, user, contract_id
    )
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.subcontract_items_loaded(contract.contract_no, created_count),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return SubcontractorContractItemsLoadResponse(
        created_count=created_count, skipped_count=skipped_count
    )

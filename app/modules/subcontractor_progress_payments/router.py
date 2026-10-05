"""Taşeron hakedişi uçları (T2) — oluşturma / liste / detay / PATCH / DELETE.

Kapılar `progress_payments` iznidir: iki hakediş ailesi AYNI ekran ailesidir,
**yeni izin modülü AÇILMAZ** (spec §1). Denetim günlüğü (`record_audit`) TÜM
yazma uçlarına bağlıdır; mesajlar `app/modules/audit/messages.py`de merkezîdir.

T3'te eklendi: `PUT …/lines` (değiştirme semantiği + kota) ve `refresh-prices`.
Kapsam DIŞI (T4/T5): durum aksiyonları, `summary`.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_deps import RequireUnrestricted
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.core.slug import parse_ref
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.silme import service as silme_service
from app.modules.silme.params import DELETE_WITH_PREVIEW_RESPONSES, PreviewTokenQuery
from app.modules.subcontractor_progress_payments import read, service, summary
from app.modules.subcontractor_progress_payments.models import SubcontractorPaymentStatus
from app.modules.subcontractor_progress_payments.router_transitions import (
    router as transitions_router,
)
from app.modules.subcontractor_progress_payments.schemas import (
    SubcontractorProgressPaymentCreate,
    SubcontractorProgressPaymentDetail,
    SubcontractorProgressPaymentLinesSave,
    SubcontractorProgressPaymentListResponse,
    SubcontractorProgressPaymentSummary,
    SubcontractorProgressPaymentUpdate,
    SubcontractorRefreshPricesResponse,
)
from app.modules.users.models import User

# Ü2 (DSC-B5): hakedis ticari/proje duzeyi → kisitli kullaniciya router duzeyinde 403.
router = APIRouter(
    tags=["subcontractor-progress-payments"],
    responses=COMMON_ERROR_RESPONSES,
    dependencies=[RequireUnrestricted],
)

_VIEW = require_permission("progress_payments", AccessLevel.view)
# IZN-B3: çok proje LİSTE uçları: ana rol VEYA ekip rolü açar, satırlar `visible_projects`te proje
# başına o projedeki rolle süzülür.
_VIEW_LISTE = require_permission("progress_payments", AccessLevel.view, multi_project=True)
_DRAFT = require_permission("progress_payments", AccessLevel.draft)


@router.get(
    "/subcontractor-progress-payments",
    response_model=SubcontractorProgressPaymentListResponse,
    dependencies=[_VIEW_LISTE],
)
async def list_subcontractor_progress_payments_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    project_id: uuid.UUID | None = None,
    site_id: Annotated[
        uuid.UUID | None,
        Query(
            description=(
                "Bu şantiyeyi KAPSAYAN hakedişler: sözleşmesi bu şantiyeye bağlı olanlar "
                "**ve** proje geneli (`site_id IS NULL`) sözleşmelerin hakedişleri. "
                "Satırın hangisi olduğu `contract_site_id` alanından okunur — proje geneli "
                "satırlar her şantiyede tekrar döner, şantiye toplamına EKLENMEZ."
            )
        ),
    ] = None,
    period_year: int | None = None,
    period_month: Annotated[int | None, Query(ge=1, le=12)] = None,
    status_filter: Annotated[SubcontractorPaymentStatus | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(description="Taşeron adı veya sözleşme no araması")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SubcontractorProgressPaymentListResponse:
    """L83-101 filtreleri + `audit`/`users` sayfalama deseni (`total`/`limit`/`offset`).

    `site_id` (TB2/U2) SÖZLEŞME üzerinden süzer — hakedişin şantiye kolonu yoktur.
    HAK-NULL: süzgeç EŞİTLİK değil KAPSAMA sorar, proje geneli (`site_id IS
    NULL`) sözleşmelerin hakedişleri DE döner ve `contract_site_id` ile
    işaretlenir (gerekçe: `repository._list_stmt`).
    Kapsamı GENİŞLETMEZ: `visible_projects` süzgeci her hâlükârda üstte kalır.
    """
    return await read.list_payments(
        session,
        user,
        project_id=project_id,
        site_id=site_id,
        period_year=period_year,
        period_month=period_month,
        status_filter=status_filter,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/subcontractor-progress-payments/summary",
    response_model=SubcontractorProgressPaymentSummary,
    dependencies=[_VIEW_LISTE],
)
async def subcontractor_progress_payment_summary_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    project_id: uuid.UUID | None = None,
    site_id: Annotated[
        uuid.UUID | None,
        Query(
            description=(
                "Bu şantiyeyi KAPSAYAN hakedişler: sözleşmesi bu şantiyeye bağlı olanlar "
                "**ve** proje geneli (`site_id IS NULL`) sözleşmelerin hakedişleri. "
                "Satırın hangisi olduğu `contract_site_id` alanından okunur — proje geneli "
                "satırlar her şantiyede tekrar döner, şantiye toplamına EKLENMEZ."
            )
        ),
    ] = None,
    period_year: int | None = None,
    period_month: Annotated[int | None, Query(ge=1, le=12)] = None,
    status_filter: Annotated[SubcontractorPaymentStatus | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(description="Taşeron adı veya sözleşme no araması")] = None,
) -> SubcontractorProgressPaymentSummary:
    """L105-122 KPI şeridi (4 kart). Kapı `_VIEW`: özet yalnız OKUMA'dır.

    ⚠️ Bu tanım `/{payment_id}` ŞABLONUNDAN ÖNCE durmak ZORUNDADIR: FastAPI
    rotaları TANIM SIRASINA göre eşler, sonra tanımlansaydı `summary` bir UUID
    sanılır ve uç 422 ile ulaşılamaz olurdu.

    Süzgeçler liste ucuyla BİREBİR aynıdır (aynı `_list_stmt` gövdesi) — KPI
    şeridi ile altındaki tablo aynı kümeyi göstermek zorundadır.
    """
    return await summary.get_summary(
        session,
        user,
        project_id=project_id,
        site_id=site_id,
        period_year=period_year,
        period_month=period_month,
        status_filter=status_filter,
        q=q,
    )


@router.get(
    "/subcontractor-progress-payments/{payment_id}",
    response_model=SubcontractorProgressPaymentDetail,
    dependencies=[_VIEW],
)
async def get_subcontractor_progress_payment_endpoint(
    payment_id: str,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorProgressPaymentDetail:
    """URL-4 — yol parametresi UUID **ya da** `<sözleşme-slug>-<sıra>` slug'ı
    kabul eder (`/hakedisler/taseron/tsz-2025-001-48`). Mockup ölçüldü:
    `Taşeron Hakediş Oluştur` breadcrumb'ı `Akın İnşaat TSZ-2025-001 / Hakediş #48`.
    Yazma uçları `uuid.UUID` KALIR (URL-2 kararı 3).
    """
    return await read.get_detail(session, user, parse_ref(payment_id))


@router.post(
    "/subcontractor-contracts/{contract_id}/progress-payments",
    response_model=SubcontractorProgressPaymentDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_DRAFT],
)
async def create_subcontractor_progress_payment_endpoint(
    request: Request,
    contract_id: uuid.UUID,
    data: SubcontractorProgressPaymentCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorProgressPaymentDetail:
    """O66: satırlar sözleşme kalemlerinden OTOMATİK yüklenir; gövdede satır YOK.

    Fiyatsız kalem 422, aynı sözleşmede açık hakediş 409 (spec §2/§5).
    Yanıt `read.build_detail`den gelir — `get_detail` çağrılsaydı kapsam
    sorgusu istek başına İKİ KEZ koşardı (işveren H4 denetimi O3).
    """
    context = await service.create(session, user, contract_id, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.subcontractor_progress_payment_created(
            context.project.name, context.contract.subcontractor_name, context.payment.sequence_no
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await read.build_detail(session, context)


@router.patch(
    "/subcontractor-progress-payments/{payment_id}",
    response_model=SubcontractorProgressPaymentDetail,
    dependencies=[_DRAFT],
)
async def update_subcontractor_progress_payment_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    data: SubcontractorProgressPaymentUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorProgressPaymentDetail:
    """Yalnız `status=draft` (spec §5); aksi 409 `INVALID_STATUS_TRANSITION`."""
    context = await service.update(session, user, payment_id, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.subcontractor_progress_payment_updated(
            context.project.name, context.contract.subcontractor_name, context.payment.sequence_no
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await read.build_detail(session, context)


@router.put(
    "/subcontractor-progress-payments/{payment_id}/lines",
    response_model=SubcontractorProgressPaymentDetail,
    dependencies=[_DRAFT],
)
async def save_subcontractor_progress_payment_lines_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    data: SubcontractorProgressPaymentLinesSave,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorProgressPaymentDetail:
    """O formunun tek "Taslak Kaydet" gövdesi — **DEĞİŞTİRME** semantiği.

    ⚠️ Gövdede geçmeyen satır SİLİNİR (`PUT …/contract/distribution`
    BİRLEŞTİRMESİNİN tersi). Yalnız `status=draft` (409); kalem sahipliği, fiyat
    guard'ı ve kota tavanı (spec §4) her yazımda koşar.

    Kalemi silinmiş satırlar gövdeden adreslenemediği için düşer; sayıları
    yanıtın `dropped_orphan_count` alanında BİLDİRİLİR (sessiz atlama yok).
    Detay `build_detail`den KAPSAM SORGUSU TEKRARLANMADAN kurulur.
    """
    context, dropped_orphan_count = await service.save_lines(session, user, payment_id, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.subcontractor_progress_payment_lines_saved(
            context.project.name,
            context.contract.subcontractor_name,
            context.payment.sequence_no,
            len(context.payment.lines),
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    detail = await read.build_detail(session, context)
    return detail.model_copy(update={"dropped_orphan_count": dropped_orphan_count})


@router.post(
    "/subcontractor-progress-payments/{payment_id}/refresh-prices",
    response_model=SubcontractorRefreshPricesResponse,
    dependencies=[_DRAFT],
)
async def refresh_subcontractor_progress_payment_prices_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorRefreshPricesResponse:
    """Yalnız `draft`ta snapshot beşlisini + yüzde üçlüsünü bilinçli tazeler.

    Yanıt YALNIZ `{refreshed_count}`tur (işveren deseni): güncel ekran ayrı bir
    `GET` ile okunur, tek gövdede sayaç + tam detay BİRLEŞTİRİLMEZ.
    """
    context, refreshed_count = await service.refresh_prices(session, user, payment_id)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.subcontractor_progress_payment_prices_refreshed(
            context.project.name,
            context.contract.subcontractor_name,
            context.payment.sequence_no,
            refreshed_count,
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return SubcontractorRefreshPricesResponse(refreshed_count=refreshed_count)


@router.delete(
    "/subcontractor-progress-payments/{payment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **DELETE_WITH_PREVIEW_RESPONSES,
        404: {"description": "Hakediş bulunamadı"},
    },
    dependencies=[require_system_admin()],
)
async def delete_subcontractor_progress_payment_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    preview_token: PreviewTokenQuery = None,
) -> None:
    """Taşeron hakedişini bağlı kayıtlarıyla birlikte siler. Yalnız Sistem Yöneticisi.

    ÖNİZLEME ZORUNLU.
    Durumdan bağımsız: onaylı/ödenmiş hakediş de silinir. Satırlar, onay zinciri, hakediş faturası
    ve ödemeleri, onların fişleri ve stornoları birlikte gider (K2; kapalı dönem durdurmaz). Önce
    `GET /admin/silme/subcontractor_progress_payment/{id}/onizleme`, sonra bu uç `preview_token`
    ile: eksikse 428 `preview_required`; ağaç değiştiyse 409 `preview_stale`. Yanıt `204`.
    """
    detail = await silme_service.sil(
        session, "subcontractor_progress_payment", payment_id, preview_token
    )
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


# Durum aksiyonları (T4) AYRI dosyadadır (`router_transitions.py`, dosya başına
# ~400 satır kuralı) ama modül dışına TEK router çıkar. `include_router` en SONDA
# durur: `/{payment_id}/…` yolları `/{payment_id}` şablonuyla çakışmaz (segment
# sayısı farklı), buna karşılık `/summary` SABİTİ `/{payment_id}` şablonundan
# ÖNCE tanımlanmak ZORUNDADIR — yukarıdaki sıra bilinçlidir.
router.include_router(transitions_router)

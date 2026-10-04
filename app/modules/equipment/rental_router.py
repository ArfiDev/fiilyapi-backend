"""Kira hakedişi uçları (MK-2 spec §4) — M5.

Kapı **`equipment`** iznidir (MK-1'de açıldı; MK-2'de YENİ MODÜL AÇILMAZ ve izin
migration'ı YOKTUR): okuma `view`, yazmanın tamamı `full`. Görünmeyen kayıt
404'tür.

## 🔴 Niçin AYRI bir router — ve niçin ÖNCE kaydedilir

`router.py`nin (MK-1) `/equipment/{equipment_id}` yolu bir UUID yol
parametresidir ve `/equipment/rental-invoices`ı ONDAN SONRA gelen her tanım
yakalanamaz hâle gelir: FastAPI yolları KAYIT SIRASINA göre eşler ve
`rental-invoices` dizgesini UUID'ye çevirmeye çalışıp 422 döndürürdü. Bu yüzden
bu router `main.py`de `equipment_router`dan **ÖNCE** `include_router` edilir ve
kural bir BEKÇİ TESTİYLE kilitlenmiştir
(`test_rota_sirasi_rental_invoices_UUID_SANILMAZ`).

İkinci kök (`/equipment/rental-invoice-lines`) spec §4'ün SAYDIĞI yoldur: satır
kimliği faturasından bağımsızdır ve iç içe bir yol (`…/{invoice_id}/lines/{id}`)
istemciyi her satır isteğinde faturayı da taşımaya zorlardı.

`GET` uçları `record_audit` ÇAĞIRMAZ (WORKFLOW kuralı — okumalar denetlenmez);
yazma uçlarının hepsi tek denetim satırı yazar ve metin SERVİS katmanında kurulur.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_pages, require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.core.slug import parse_ref
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.equipment import rental_service
from app.modules.equipment.models import RentalInvoiceStatus
from app.modules.equipment.rental_schemas import (
    RentalInvoiceCreate,
    RentalInvoiceDetailResponse,
    RentalInvoiceLineResponse,
    RentalInvoiceLineUpdate,
    RentalInvoiceListResponse,
    RentalInvoiceResponse,
    RentalInvoiceUpdate,
)
from app.modules.users.models import User

router = APIRouter(prefix="/equipment", tags=["equipment"], responses=COMMON_ERROR_RESPONSES)

_VIEW = require_permission(rental_service.PERMISSION_MODULE, AccessLevel.view)
_FULL = require_permission(rental_service.PERMISSION_MODULE, AccessLevel.full)
#: IZN-B2: kira Onayla / Öde / Reddet = Kira Hakedişi sayfası ONAYLAR (eşik eski `full`).
_RENTAL_APPROVE = require_pages(("saha.makine_kira",), "approve")

_STATUS_RESPONSES = {
    404: {"description": "Kira hakedişi bulunamadı (görünmeyen dahil)"},
    409: {"description": "Durum makinesi bu geçişe izin vermiyor (K5)"},
}


async def _audit(
    request: Request,
    session: AsyncSession,
    user: User,
    action: AuditAction,
    detail: str,
) -> None:
    """Denetim satırı (B5 deseni). Metin PARAMETREDİR, burada kurulmaz."""
    await record_audit(
        session,
        action=action,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


@router.get("/rental-invoices", response_model=RentalInvoiceListResponse, dependencies=[_VIEW])
async def list_rental_invoices_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    supplier_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    #: 🔴 MK-4 — Ekipman Detay ekranının "bu makinenin hakedişleri" bloğu.
    #: Süzgeç SATIR düzeyindedir (`equipment_id` başlıkta yoktur): bir fatura,
    #: bu ekipmana ait EN AZ BİR satırı varsa listeye girer.
    equipment_id: uuid.UUID | None = None,
    status_filter: Annotated[RentalInvoiceStatus | None, Query(alias="status")] = None,
    period_year: Annotated[int | None, Query(ge=2000, le=2200)] = None,
    period_month: Annotated[int | None, Query(ge=1, le=12)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RentalInvoiceListResponse:
    """Hakediş listesi — süzgeçler AND'lidir, kapsam (K9) HER ZAMAN üsttedir.

    Satır toplamları burada YOKTUR (şema gerekçesi): `our_total` satırlardan
    türer ve liste satırı başına tüm satırları taramak gerekirdi.
    """
    items, total = await rental_service.list_invoices(
        session,
        user,
        supplier_id=supplier_id,
        site_id=site_id,
        equipment_id=equipment_id,
        status=status_filter,
        period_year=period_year,
        period_month=period_month,
        limit=limit,
        offset=offset,
    )
    return RentalInvoiceListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post(
    "/rental-invoices",
    response_model=RentalInvoiceDetailResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Kiralama firması ya da şantiye bulunamadı (görünmeyen dahil)"},
        409: {"description": "Bu firma için aynı fatura numarası zaten kayıtlı"},
    },
    dependencies=[_FULL],
)
async def create_rental_invoice_endpoint(
    request: Request,
    data: RentalInvoiceCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> RentalInvoiceDetailResponse:
    """M5 üst formu. 🔴 Satırlar GÖVDEDE YOKTUR: sunucu onları çalışma kaydından
    KURAR (K2 snapshot'ı, M5:83 "Çalışma kaydından otomatik yüklendi")."""
    detay, detail = await rental_service.create_invoice(session, user, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return detay


@router.get(
    "/rental-invoices/{invoice_id}",
    response_model=RentalInvoiceDetailResponse,
    dependencies=[_VIEW],
)
async def get_rental_invoice_endpoint(
    invoice_id: str,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> RentalInvoiceDetailResponse:
    """M5'in TAMAMI: tablo + tfoot + proje dağılımı (spec §4).

    🔴 K2: hiçbir sayı çalışma kaydından CANLI okunmaz — satırların KENDİ
    kolonlarından türer.

    URL-4 — yol parametresi UUID **ya da** fatura no slug'ı kabul eder
    (`/makine/kira/lt2026080211`). Yazma uçları `uuid.UUID` KALIR.
    """
    invoice = await rental_service.visible_invoice(session, user, parse_ref(invoice_id))
    return await rental_service.invoice_detail(session, invoice)


@router.patch(
    "/rental-invoices/{invoice_id}",
    response_model=RentalInvoiceDetailResponse,
    responses={
        409: {"description": "Onaylanmış/ödenmiş hakediş düzenlenemez (K5)"},
        422: {"description": "Kiralık satırlar seçilen firmaya ait değil (K8)"},
    },
    dependencies=[_FULL],
)
async def update_rental_invoice_endpoint(
    request: Request,
    invoice_id: uuid.UUID,
    data: RentalInvoiceUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> RentalInvoiceDetailResponse:
    """Kısmi güncelleme — `draft` + `pending_verification`; ötesi 409.

    Dönem/şantiye değişikliği satırları KENDİLİĞİNDEN tazelemez (K2): tazeleme
    `POST …/reload` ile AÇIKÇA yapılır.
    """
    detay, detail = await rental_service.update_invoice(session, user, invoice_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return detay


@router.post(
    "/rental-invoices/{invoice_id}/reload",
    response_model=RentalInvoiceDetailResponse,
    responses=_STATUS_RESPONSES,
    dependencies=[_FULL],
)
async def reload_rental_invoice_endpoint(
    request: Request,
    invoice_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> RentalInvoiceDetailResponse:
    """🔴 K2'nin AÇIK tazeleme eylemi — YALNIZ `draft` (ötesi 409)."""
    detay, detail = await rental_service.reload_invoice(session, user, invoice_id)
    await _audit(request, session, user, AuditAction.update, detail)
    return detay


@router.post(
    "/rental-invoices/{invoice_id}/approve",
    response_model=RentalInvoiceResponse,
    responses=_STATUS_RESPONSES,
    dependencies=[_RENTAL_APPROVE],
)
async def approve_rental_invoice_endpoint(
    request: Request,
    invoice_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> RentalInvoiceResponse:
    """**"Onayla ve Ödemeye Gönder"** (ONAYLI SAPMA — M5:27 "Kiracıya Gönder"
    diyor ama akış yönüyle çelişiyor: gelen faturayı BİZ ödüyoruz).

    Zinciri TEK ADIM ilerletir; ödeme damgası bu uçtan BASILMAZ.
    """
    baslik, detail = await rental_service.approve_invoice(session, user, invoice_id)
    await _audit(request, session, user, AuditAction.update, detail)
    return baslik


@router.post(
    "/rental-invoices/{invoice_id}/pay",
    response_model=RentalInvoiceResponse,
    responses=_STATUS_RESPONSES,
    dependencies=[_RENTAL_APPROVE],
)
async def pay_rental_invoice_endpoint(
    request: Request,
    invoice_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> RentalInvoiceResponse:
    """🔴 ÖDENDİ damgası. `paid` bir UÇ DURUMDUR: ikinci çağrı 409.

    EŞİK = KİLİT: fatura DENETİMDEN ÖNCE kilitlenir (çift ödeme yarışı).
    """
    baslik, detail = await rental_service.pay_invoice(session, user, invoice_id)
    await _audit(request, session, user, AuditAction.update, detail)
    return baslik


@router.post(
    "/rental-invoices/{invoice_id}/reject",
    response_model=RentalInvoiceResponse,
    responses=_STATUS_RESPONSES,
    dependencies=[_RENTAL_APPROVE],
)
async def reject_rental_invoice_endpoint(
    request: Request,
    invoice_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> RentalInvoiceResponse:
    """Onayın GERİ ALINMASI (`approved → pending_verification`).

    Ayrı bir `rejected` durumu YOKTUR (K5): fatura "doğrulama bekleyen"
    listesine geri döner ve yeniden düzenlenebilir hâle gelir.
    """
    baslik, detail = await rental_service.reject_invoice(session, user, invoice_id)
    await _audit(request, session, user, AuditAction.update, detail)
    return baslik


@router.patch(
    "/rental-invoice-lines/{line_id}",
    response_model=RentalInvoiceLineResponse,
    responses={
        404: {"description": "Satır bulunamadı (faturası görünmeyen dahil)"},
        409: {"description": "Onaylanmış/ödenmiş hakedişin satırı düzenlenemez (K5)"},
    },
    dependencies=[_FULL],
)
async def update_rental_invoice_line_endpoint(
    request: Request,
    line_id: uuid.UUID,
    data: RentalInvoiceLineUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> RentalInvoiceLineResponse:
    """M5'in İKİ input'u: `rate_amount` (Kira B.F.) + `invoiced_hours` (Fatura
    Saati). Başka alan taşıyan gövde 422'dir — `worked_hours` gövdeden
    yazılabilseydi K2 snapshot'ı bir PATCH ile delinirdi."""
    satir, detail = await rental_service.update_line(session, user, line_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return satir


@router.delete(
    "/rental-invoice-lines/{line_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: {"description": "Satır yalnız taslak hakedişte silinebilir"}},
    dependencies=[require_system_admin()],
)
async def delete_rental_invoice_line_endpoint(
    request: Request,
    line_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> Response:
    """YALNIZ `draft` (spec §4): doğrulama aşamasında bir satırın yok olması,
    firmanın faturasıyla karşılaştırılan kümeyi sessizce küçültürdü."""
    detail = await rental_service.delete_line(session, user, line_id)
    await _audit(request, session, user, AuditAction.delete, detail)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

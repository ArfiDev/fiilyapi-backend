"""Taşeron hakedişi durum aksiyonları (T4, spec §5) — beş uç.

`router.py`den AYRI dosyadadır (dosya başına ~400 satır kuralı); tek bir
`APIRouter` olarak kalması için `router.py` sonunda `include_router` ile
BAĞLANIR — yön tek taraflıdır (`router` → `router_transitions`), ters import
açılmaz ve modül dışına tek bir router çıkar.

Beş uç da TEK yoldan (`transitions.perform`) geçer; geçiş tablosu, kilit ve
kotanın onay anındaki yeniden doğrulaması ORADA tek kopyadır. Router'ın tek işi
KAPIYI seçmek ve denetim satırını yazmaktır — durum kontrolü BURADA
TEKRARLANMAZ (işveren `progress_payments/router.py` deseninin birebiri).

Kapı seviyeleri işverenle AYNIDIR (aynı izin modülü, aynı ekran ailesi):
`submit` → `_DRAFT` · `mark-paid` → `_APPROVE` · `unapprove` → `_ADMIN`.
`approve`/`reject` de AYNI seviyeyi ister ama kapıları `_CHAIN_APPROVE`dir
(OK-1C): seviye yetmediğinde zincirin SIRADAKİ adımının sahibi
(evrakın projesinde adım rolü olan kişi) modül kapısını İKAME EDER.
Diğer üç uç DEĞİŞMEDİ — genişleme DARDIR.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_deps import RequireUnrestricted
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_pages, require_permission
from app.core.ratelimit import client_ip
from app.modules.approvals import service as approvals_service
from app.modules.approvals.gate import require_pages_or_chain_step
from app.modules.approvals.models import ApprovalDocumentType
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.subcontractor_progress_payments import read, transitions
from app.modules.subcontractor_progress_payments.schemas import (
    SubcontractorProgressPaymentDetail,
    SubcontractorRejectBody,
)
from app.modules.subcontractor_progress_payments.service import PaymentContext
from app.modules.users.models import User

# Ü2 (DSC-B5): hakedis ticari/proje duzeyi → kisitli kullaniciya router duzeyinde 403.
router = APIRouter(
    tags=["subcontractor-progress-payments"],
    responses=COMMON_ERROR_RESPONSES,
    dependencies=[RequireUnrestricted],
    route_class=MaskeRotasi,
)

#: IZN-B5b: taşeron hakediş ailesi (`santiye.hakedisler` işveren ailesidir; çıkarıldı, CEO k.2).
HAKEDIS_TASERON = ("mali.hakedis_taseron", "proje.taseron_hakedis")
_DRAFT = require_pages(HAKEDIS_TASERON, "edit")
#: IZN-B2: Onayla/Reddet/Ödendi = taşeron hakediş sayfaları ONAYLAR (+ proje sekmesi).
_APPROVE_PAGES = HAKEDIS_TASERON
_APPROVE = require_pages(_APPROVE_PAGES, "approve")
_ADMIN = require_permission("progress_payments", AccessLevel.admin)
#: OK-1C — `approve`/`reject`in kapısı. Modül seviyesi AYNEN `approve`tır;
#: yalnız YEDEK bir dal eklenir (`approvals/gate.py`).
_CHAIN_APPROVE = require_pages_or_chain_step(
    _APPROVE_PAGES,
    module_key="progress_payments",
    document_type=ApprovalDocumentType.subcontractor_progress_payment,
    document_id_param="payment_id",
)

_PATH = "/subcontractor-progress-payments/{payment_id}"


def _context(result: transitions.TransitionResult) -> PaymentContext:
    """Yanıt `read.build_detail`den KAPSAM SORGUSU TEKRARLANMADAN kurulur:
    `perform` kapsam kararını çoktan vermiş ve üçlüyü döndürmüştür (işveren
    ucunun `get_detail` çağrısı kapsamı istek başına İKİ KEZ sorgular)."""
    return PaymentContext(payment=result.payment, contract=result.contract, project=result.project)


@router.post(
    f"{_PATH}/submit", response_model=SubcontractorProgressPaymentDetail, dependencies=[_DRAFT]
)
async def submit_subcontractor_progress_payment_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorProgressPaymentDetail:
    """`draft → pending_approval`. Zorunluluk kuralları (dönem + Σmiktar>0)
    YALNIZ burada koşar: taslak eksik veriyle serbestçe saklanır.

    "Revize Gerekli" damgası (`rejected_at`/`rejection_reason`) burada TEMİZLENİR.
    """
    result = await transitions.perform(session, user, payment_id, transitions.PaymentAction.submit)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.subcontractor_progress_payment_submitted(
            result.project.name, result.contract.subcontractor_name, result.payment.sequence_no
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await read.build_detail(session, _context(result))


@router.post(
    f"{_PATH}/approve",
    response_model=SubcontractorProgressPaymentDetail,
    dependencies=[_CHAIN_APPROVE],
)
async def approve_subcontractor_progress_payment_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorProgressPaymentDetail:
    """🔴 **OK-1A T3: YOL ve KAPI KORUNDU, ANLAM DEĞİŞTİ.**

    Uç artık onay ZİNCİRİNİN sıradaki adımını ilerletir (mockup zinciri
    `Onay Kutusu.dc.html:120-144`: Şantiye Şefi → Proje Müdürü → Muhasebe,
    eşik üstünde + Patron). Evrak ancak SON adımda `approved` olur; ara
    adımlarda `pending_approval`da KALIR. Zincirsiz ESKİ kayıtlarda bugünkü
    tek adımlı davranış sürer.

    Kota KİLİT ALTINDA sırasız TAM küme üzerinden YENİDEN doğrulanır (spec §4)
    — aşım 422, onay GERÇEKLEŞMEZ."""
    result = await transitions.perform(session, user, payment_id, transitions.PaymentAction.approve)
    # `AuditAction.approve` TAM BU UÇ için ayrılmıştır; diğer geçişler `update`.
    # ADIM onayı da `approve`dır (Y6: yeni `AuditAction` üyesi AÇILMAZ).
    await record_audit(
        session,
        action=AuditAction.approve,
        detail=approvals_service.audit_detail(
            messages.subcontractor_progress_payment_approved(
                result.project.name, result.contract.subcontractor_name, result.payment.sequence_no
            ),
            result.chain_step,
            document_label=messages.subcontractor_payment_label(
                result.project.name, result.contract.subcontractor_name, result.payment.sequence_no
            ),
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await read.build_detail(session, _context(result))


@router.post(
    f"{_PATH}/reject",
    response_model=SubcontractorProgressPaymentDetail,
    dependencies=[_CHAIN_APPROVE],
)
async def reject_subcontractor_progress_payment_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    data: SubcontractorRejectBody,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorProgressPaymentDetail:
    """`pending_approval → draft` — ret BEŞİNCİ durum DEĞİLDİR (spec §5).

    İşverenden AYRILAN nokta: gövde ZORUNLUDUR ve gerekçe `rejection_reason`
    KOLONUNA yazılır; `rejected_at` ile birlikte L177 "Revize Gerekli" rozetinin
    (`is_revision_required` türevi) kaynağıdır.
    """
    result = await transitions.perform(
        session, user, payment_id, transitions.PaymentAction.reject, reason=data.reason
    )
    await record_audit(
        session,
        action=AuditAction.update,
        detail=approvals_service.rejection_audit_detail(
            messages.subcontractor_progress_payment_rejected(
                result.project.name,
                result.contract.subcontractor_name,
                result.payment.sequence_no,
                result.payment.rejection_reason or data.reason,
            ),
            result.chain_step,
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await read.build_detail(session, _context(result))


@router.post(
    f"{_PATH}/mark-paid", response_model=SubcontractorProgressPaymentDetail, dependencies=[_APPROVE]
)
async def mark_paid_subcontractor_progress_payment_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorProgressPaymentDetail:
    """`approved → paid`. Ödeme detayı formu mockup'ta YOK → tek tıkla
    işaretleme, yalnız `paid_at` damgalanır (fatura/ödeme bağı mali dilimlere)."""
    result = await transitions.perform(
        session, user, payment_id, transitions.PaymentAction.mark_paid
    )
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.subcontractor_progress_payment_paid(
            result.project.name, result.contract.subcontractor_name, result.payment.sequence_no
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await read.build_detail(session, _context(result))


@router.post(
    f"{_PATH}/unapprove", response_model=SubcontractorProgressPaymentDetail, dependencies=[_ADMIN]
)
async def unapprove_subcontractor_progress_payment_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SubcontractorProgressPaymentDetail:
    """`approved → pending_approval` (geri çek) — YALNIZ `admin`.

    🔴 **OK-1A Y4:** uç artık zincirin SON karara bağlanmış adımını da GERİ
    SARAR; zincir SİLİNMEZ (ret'ten farkı budur). Sözleşme Y4 bu davranışı
    yalnız işveren hakedişi için yazıyordu — ÖLÇÜM taşerona da ZORUNLU olduğunu
    gösterdi: geri sarmayan bir `unapprove`, tamamlanmış zincirli bir evrağı
    `pending_approval`a döndürür ve sonraki onay `CHAIN_COMPLETED` 409'una
    çarpardı, yani evrak KİLİTLENİRDİ.

    `paid` kaynak DEĞİLDİR: ödenmiş hakedişin geri dönüşü yoktur, denemesi 409.
    Denetim mesajı ESKİ onaylayanı taşır — `transitions.perform` bu ikisini
    damgalar NULL'lanmadan ÖNCE yakalar, router yeniden sorgulayamaz.
    """
    result = await transitions.perform(
        session, user, payment_id, transitions.PaymentAction.unapprove
    )
    await record_audit(
        session,
        action=AuditAction.update,
        detail=approvals_service.rewind_audit_detail(
            messages.subcontractor_progress_payment_unapproved(
                result.project.name,
                result.contract.subcontractor_name,
                result.payment.sequence_no,
                result.previous_approver_name,
                result.previous_approved_at,
            ),
            result.chain_rewind,
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await read.build_detail(session, _context(result))

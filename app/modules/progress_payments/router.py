"""İşveren hakedişi (P7) uçları — CRUD (H4) + satır/tazeleme (H5/H7) + durum
geçişleri (H6) + silme (H8) + denetim günlüğü (H10, spec §11).

`contracts/router.py` deseninin aynısı: kapı sabitleri modül düzeyinde tanımlanır.
Denetim günlüğü (`record_audit`) TÜM yazma uçlarına bağlıdır; mesaj aileleri
`app/modules/audit/messages.py`de merkezileşir (plan Task H10).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_deps import RequireUnrestricted
from app.core.openapi import COMMON_ERROR_RESPONSES, DELETE_403_YANITI
from app.core.permissions import require_pages, require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.core.slug import parse_ref
from app.modules.approvals import service as approvals_service
from app.modules.approvals.gate import require_pages_or_chain_step
from app.modules.approvals.models import ApprovalDocumentType
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.progress_payments import last_price_provider, service, summary, transitions
from app.modules.progress_payments.models import ProgressPaymentStatus
from app.modules.progress_payments.schemas import (
    ProgressPaymentCreate,
    ProgressPaymentDetail,
    ProgressPaymentLinesSave,
    ProgressPaymentListResponse,
    ProgressPaymentSummary,
    ProgressPaymentUpdate,
    RefreshPricesResponse,
    RejectBody,
)
from app.modules.users.models import User

# Ü2 (DSC-B5): hakedis ticari/proje duzeyi → kisitli kullaniciya router duzeyinde 403; yeni
# eklenen her rota otomatik kapali.
router = APIRouter(
    tags=["progress-payments"],
    responses=COMMON_ERROR_RESPONSES,
    dependencies=[RequireUnrestricted],
)

# TKL-B3.2: son fiyat portuna kayit (import yan etkisi; bekci testi uygulama acilisinda ister).
last_price_provider.register()

_VIEW = require_permission("progress_payments", AccessLevel.view)
# IZN-B3: çok proje LİSTE uçları: ana rol VEYA ekip rolü açar, satırlar `visible_projects`te proje
# başına o projedeki rolle süzülür.
_VIEW_LISTE = require_permission("progress_payments", AccessLevel.view, multi_project=True)
_DRAFT = require_permission("progress_payments", AccessLevel.draft)
#: IZN-B2: Onayla/Reddet/Ödendi = hakediş sayfaları ONAYLAR (işveren + proje sekmesi + şantiye).
_APPROVE_PAGES = ("mali.hakedis_isveren", "proje.isveren_hakedis", "santiye.hakedisler")
_APPROVE = require_pages(_APPROVE_PAGES, "approve")
_ADMIN = require_permission("progress_payments", AccessLevel.admin)
#: OK-1C — `approve`/`reject`in kapısı. Modül seviyesi AYNEN `approve`tır;
#: seviye yetmediğinde zincirin SIRADAKİ adımının sahibi
#: (evrakın projesinde adım rolü olan kişi) onu İKAME EDER (`approvals/gate.py`).
#: `mark-paid`/`unapprove` DEĞİŞMEDİ — kapsam DAR.
_CHAIN_APPROVE = require_pages_or_chain_step(
    _APPROVE_PAGES,
    module_key="progress_payments",
    document_type=ApprovalDocumentType.progress_payment,
    document_id_param="payment_id",
)


@router.get(
    "/progress-payments",
    response_model=ProgressPaymentListResponse,
    dependencies=[_VIEW_LISTE],
)
async def list_progress_payments_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    project_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    status_filter: Annotated[ProgressPaymentStatus | None, Query(alias="status")] = None,
) -> ProgressPaymentListResponse:
    return await service.list_payments(
        session, user, project_id=project_id, site_id=site_id, status_filter=status_filter
    )


@router.get(
    "/projects/{project_id}/progress-payments/summary",
    response_model=ProgressPaymentSummary,
    dependencies=[_VIEW],
)
async def get_progress_payment_summary_endpoint(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ProgressPaymentSummary:
    """E14 127-147 "Hakediş Özeti" kartı + SHK 82-84 şantiye kartları (spec §9.6).

    Kapı `_VIEW`: özet yalnız OKUMA'dır. Aynı gövde `contracts` modülünün E14
    detayına da gömülür (`EmployerContractDetail.progress_payment_summary`) —
    izin matrisinde `contracts ≥ view` olan HER rolün `progress_payments ≥ view`
    olduğu doğrulanmıştır (`seed_data.py:169/187`), bu yüzden gömme bir izin
    arka kapısı açmaz.
    """
    return await summary.get_summary(session, user, project_id)


@router.get(
    "/progress-payments/{payment_id}",
    response_model=ProgressPaymentDetail,
    dependencies=[_VIEW],
)
async def get_progress_payment_endpoint(
    payment_id: str,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ProgressPaymentDetail:
    """URL-4 — yol parametresi UUID **ya da** `<proje-slug>-<sıra>` slug'ı
    kabul eder (`/hakedisler/kopru-guclendirme-5`).

    Bileşik anahtar (`project_id`, `sequence_no`) AYRIŞTIRILMAZ: slug
    oluşturulurken ÜRETİLİP SAKLANIR, böylece yol şablonu `/{payment_id}`
    DEĞİŞMEZ (URL-2 kararı 1) ve `parse_ref` de değişmez.
    Durum geçişleri ve PATCH/DELETE `uuid.UUID` KALIR (kararı 3).
    """
    return await service.get_detail(session, user, parse_ref(payment_id))


@router.post(
    "/projects/{project_id}/progress-payments",
    response_model=ProgressPaymentDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_DRAFT],
)
async def create_progress_payment_endpoint(
    request: Request,
    project_id: uuid.UUID,
    data: ProgressPaymentCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ProgressPaymentDetail:
    """D8/K9: sözleşmede açık hakediş varsa 409; sözleşme yoksa 422 (spec §9.2).

    Yanıt hesap türevleri (`calculation`/`progress`/`groups`) taşıdığı için
    TEK detay inşa yolundan (`service.build_detail`) geçer — iki kopya hesap
    mantığı riski taşınmaz. `get_detail` DEĞİL `build_detail` çağrılır (H4
    denetimi O3): `create` kapsam süzgecini zaten koşturmuş ve `(payment,
    project)` çiftini çözmüştür; `get_detail` ikinci bir `visible_projects`
    sorgusu daha koştururdu.
    """
    payment, project = await service.create(session, user, project_id, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.progress_payment_created(project.name, payment.sequence_no),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await service.build_detail(session, payment, project)


@router.patch(
    "/progress-payments/{payment_id}",
    response_model=ProgressPaymentDetail,
    dependencies=[_DRAFT],
)
async def update_progress_payment_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    data: ProgressPaymentUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ProgressPaymentDetail:
    """Yalnız `status=draft` (spec §7); aksi 409 `INVALID_STATUS_TRANSITION`."""
    payment, project = await service.update(session, user, payment_id, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.progress_payment_updated(project.name, payment.sequence_no),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await service.build_detail(session, payment, project)


@router.put(
    "/progress-payments/{payment_id}/lines",
    response_model=ProgressPaymentDetail,
    dependencies=[_DRAFT],
)
async def save_progress_payment_lines_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    data: ProgressPaymentLinesSave,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ProgressPaymentDetail:
    """OLU formunun tek "Taslak Kaydet" gövdesi — **DEĞİŞTİRME** semantiği.

    ⚠️ Gövdede geçmeyen satır SİLİNİR. P5'in `PUT …/contract/distribution`
    **BİRLEŞTİRME** ucunun TERSİDİR (orada gövdede geçmeyen hücre KORUNUR) —
    frontend'de ikisi yan yana kullanılacağı için karıştırılmamalıdır (spec §10/2).

    Yalnız `status=draft` (409 `INVALID_STATUS_TRANSITION`); §6.5 korkulukları
    (dağıtım ön şartı, kota tavanı, sahiplik, FF kilidi) her yazımda koşar.

    Kalemi silinmiş satırlar gövdeden adreslenemediği için düşer; sayıları
    yanıtın `dropped_orphan_count` alanında BİLDİRİLİR (spec §10/7, sessiz
    atlama yok). `get_detail` bu bilgiyi bilemez — `model_copy` ile üzerine
    yazılır (mutasyon yok, yeni nesne).

    Denetim mesajı `payment.lines` (kaydedilmiş NİHAİ durum) uzunluğunu taşır
    (spec §11 `progress_payment_lines_saved(…, count)`) — gövdedeki `len(data.
    lines)` DEĞİL, çünkü ikisi düşen bağı-kopmuş satırlar YOKSA zaten eşittir
    ama tutarlılık için TEK doğruluk kaynağı (kalıcı hâl) tercih edilir.
    """
    payment, project, dropped_orphan_count = await service.save_lines(
        session, user, payment_id, data
    )
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.progress_payment_lines_saved(
            project.name, payment.sequence_no, len(payment.lines)
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    detail = await service.get_detail(session, user, payment.id)
    return detail.model_copy(update={"dropped_orphan_count": dropped_orphan_count})


@router.post(
    "/progress-payments/{payment_id}/refresh-prices",
    response_model=RefreshPricesResponse,
    dependencies=[_DRAFT],
)
async def refresh_progress_payment_prices_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> RefreshPricesResponse:
    """§5.1/§9.3: yalnız `draft`'ta bağı kopmamış satırların snapshot beşlisini
    + hakedişin yüzde üçlüsünü kalemden/sözleşmeden bilinçli tazeler.

    Yanıt YALNIZ `{refreshed_count}`'tur (plan Adım 1'in test şeması) — güncel
    ekran ayrı bir `GET /progress-payments/{id}` ile okunur; `PUT …/lines`'ın
    aksine burada tek gövdede iki bilgi (sayaç + tam detay) BİRLEŞTİRİLMEZ,
    çünkü tazeleme sonrası frontend zaten ekranı yeniden çizmek için detayı
    ayrıca çeker (spec §9.3, `RefreshPricesResponse`)."""
    payment, project, refreshed_count = await service.refresh_prices(session, user, payment_id)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.progress_payment_prices_refreshed(
            project.name, payment.sequence_no, refreshed_count
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return RefreshPricesResponse(refreshed_count=refreshed_count)


# --- Durum geçişleri (spec §7, §9.4) ---
#
# Beş uç da TEK yoldan (`transitions.perform`) geçer; geçiş tablosu ve kilit
# orada TEK kopyadır. Router'ın tek işi KAPIYI seçmektir (§7 tablosunun "asgari
# seviye" kolonu) — durum kontrolü BURADA TEKRARLANMAZ.


@router.post(
    "/progress-payments/{payment_id}/submit",
    response_model=ProgressPaymentDetail,
    dependencies=[_DRAFT],
)
async def submit_progress_payment_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ProgressPaymentDetail:
    """E15 71 / OLU 25 "Onaya Gönder" — `draft → pending_approval`.

    Zorunluluk kuralları (dönem, satır/Σ>0, sözleşme bedeli) YALNIZ burada koşar
    (§7): taslak eksik veriyle serbestçe saklanır (kalıcı karar 4).
    """
    result = await transitions.perform(session, user, payment_id, transitions.PaymentAction.submit)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.progress_payment_submitted(result.project.name, result.payment.sequence_no),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await service.get_detail(session, user, result.payment.id)


@router.post(
    "/progress-payments/{payment_id}/approve",
    response_model=ProgressPaymentDetail,
    dependencies=[_CHAIN_APPROVE],
)
async def approve_progress_payment_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ProgressPaymentDetail:
    """🔴 **OK-1A T3: YOL ve KAPI KORUNDU, ANLAM DEĞİŞTİ.**

    Uç artık onay ZİNCİRİNİN sıradaki adımını ilerletir. Evrak ancak SON adım
    onaylanınca `pending_approval → approved` geçişini yapar; ara adımlarda
    `pending_approval`da KALIR (durum makinesi DEĞİŞMEDİ, ona giden yol
    uzadı). Zincirsiz ESKİ kayıtlarda bugünkü tek adımlı davranış sürer.

    Kota kilit altında YENİDEN doğrulanır — HER adımda, yalnız sonuncusunda
    değil: aşmış bir hakedişin ara imzalarını toplaması, aşımı ancak son anda
    görülen bir sürprize çevirirdi.
    """
    result = await transitions.perform(session, user, payment_id, transitions.PaymentAction.approve)
    # `AuditAction.approve` (`audit/models.py` docstring'i) TAM BU UÇ için
    # ayrılmıştı — diğer geçişler `AuditAction.update`dir. ADIM onayı da
    # `approve`dır (sözleşme Y6: yeni `AuditAction` üyesi AÇILMAZ).
    await record_audit(
        session,
        action=AuditAction.approve,
        detail=approvals_service.audit_detail(
            messages.progress_payment_approved(result.project.name, result.payment.sequence_no),
            result.chain_step,
            document_label=messages.progress_payment_label(
                result.project.name, result.payment.sequence_no
            ),
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await service.get_detail(session, user, result.payment.id)


@router.post(
    "/progress-payments/{payment_id}/reject",
    response_model=ProgressPaymentDetail,
    dependencies=[_CHAIN_APPROVE],
)
async def reject_progress_payment_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    data: RejectBody,
) -> ProgressPaymentDetail:
    """`pending_approval → draft` — ret sonrası taslak yeniden düzenlenebilir.

    🔴 **KIRICI (OK-1A K2):** gövde ve gerekçe artık ZORUNLUDUR; eskiden
    `RejectBody | None` idi. `reason` yine hiçbir kolona yazılmaz — TEK kalıcı
    izi denetim günlüğüdür (spec §11) ve K2 bunu değiştirmez: kullanıcı kararı
    gerekçenin ZORUNLULUĞUNU bağladı, DEPOLANDIĞI yeri değil.

    Ret zinciri de BİTİRİR: `approval_chains` satırı DAMGALANIR, SİLİNMEZ (OKT-B1)
    ve yeniden gönderim ADIM 1'den, YENİ eşik snapshot'ıyla başlar.
    """
    result = await transitions.perform(
        session, user, payment_id, transitions.PaymentAction.reject, reason=data.reason
    )
    await record_audit(
        session,
        action=AuditAction.update,
        detail=approvals_service.rejection_audit_detail(
            messages.progress_payment_rejected(
                result.project.name, result.payment.sequence_no, data.reason
            ),
            result.chain_step,
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await service.get_detail(session, user, result.payment.id)


@router.post(
    "/progress-payments/{payment_id}/mark-paid",
    response_model=ProgressPaymentDetail,
    dependencies=[_APPROVE],
)
async def mark_paid_progress_payment_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ProgressPaymentDetail:
    """`approved → paid` (K11: onay seviyesi). Ödeme detayı formu mockup'ta YOK
    → tek tıkla işaretleme, yalnız `paid_at` damgalanır."""
    result = await transitions.perform(
        session, user, payment_id, transitions.PaymentAction.mark_paid
    )
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.progress_payment_paid(result.project.name, result.payment.sequence_no),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await service.get_detail(session, user, result.payment.id)


@router.post(
    "/progress-payments/{payment_id}/unapprove",
    response_model=ProgressPaymentDetail,
    dependencies=[_ADMIN],
)
async def unapprove_progress_payment_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ProgressPaymentDetail:
    """`approved → pending_approval` (geri çek) — YALNIZ `admin` (§7 tablosu).

    🔴 **OK-1A Y4: YOL, `_ADMIN` KAPISI ve GEÇİŞ TABLOSU KORUNDU; anlam eklendi.**
    Uç artık zincirin SON karara bağlanmış adımını da GERİ SARAR (`decided_by`/
    `decided_at` NULL'lanır) — zincir SİLİNMEZ (ret'ten farkı budur) ve o adım
    yeniden sıradaki adım olur. Geri sarmasaydı tamamlanmış zincirli bir evrak
    `pending_approval`a döner ve sonraki onay "zincir tamamlanmış" 409'una
    çarpardı: evrak KİLİTLENİRDİ.

    `paid` kaynak DEĞİLDİR (K7): ödenmiş hakedişin geri dönüşü yoktur, denemesi
    409'dur.

    H6'dan devredilen ZORUNLULUK (plan H10, spec §11): bugüne kadar `unapprove`
    geriye HİÇBİR iz bırakmıyordu — `transitions.perform` eski `approved_by`/
    `approved_at`'ı damgalar NULL'lanmadan ÖNCE yakalar (`TransitionResult`),
    mesaj bu ikisini taşır.
    """
    result = await transitions.perform(
        session, user, payment_id, transitions.PaymentAction.unapprove
    )
    await record_audit(
        session,
        action=AuditAction.update,
        detail=approvals_service.rewind_audit_detail(
            messages.progress_payment_unapproved(
                result.project.name,
                result.payment.sequence_no,
                result.previous_approver_name,
                result.previous_approved_at,
            ),
            result.chain_rewind,
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await service.get_detail(session, user, result.payment.id)


# --- Silme (spec §7.1, §9.5, task H8) ---


@router.delete(
    "/progress-payments/{payment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **DELETE_403_YANITI,
        409: {"description": "Onaylanmış veya ödenmiş hakediş silinemez"},
    },
    dependencies=[require_system_admin()],
)
async def delete_progress_payment_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """İşveren hakedişini siler. YALNIZ Sistem Yöneticisi.

    `approved`/`paid` hakediş **409** (iş kuralı, Sistem Yöneticisi'ni de durdurur): önce
    `unapprove` ile geri çekilir. Taslağı açan kişi kendi taslağını da silemez. Satırlar birlikte
    gider. Disiplin kısıtı DELETE'te uygulanmaz.
    """
    summary = await service.delete_payment(session, user, payment_id)
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=messages.progress_payment_deleted(
            summary.project_name, summary.sequence_no, summary.status_label, summary.amount
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )

"""Taşeron hakedişi OKUMA yolu (T2) — detay ve liste yanıtlarının kurulumu.

`service.py`den ayrı durur (işveren modülünün `summary.py`/`lines.py` ayrımının
aynı gerekçesi): yazma yolu (kapsam + kilit + kurallar) ile okuma yolu (yanıt
inşası) farklı hızda değişir ve T3 hesap bloklarını BURAYA ekleyecektir.

Yön TEK taraflıdır: bu modül `service`in kapsam yardımcılarını çağırır, `service`
buradan hiçbir şey İMPORT ETMEZ — döngüsel import doğmaz.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import restricted_project_ids
from app.modules.progress_payments import calculations
from app.modules.projects.service import visible_projects
from app.modules.subcontractor_progress_payments import amounts, repository
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
    SubcontractorProgressPaymentLine,
)
from app.modules.subcontractor_progress_payments.schemas import (
    SubcontractorProgressPaymentDetail,
    SubcontractorProgressPaymentLineRead,
    SubcontractorProgressPaymentListItem,
    SubcontractorProgressPaymentListResponse,
)
from app.modules.subcontractor_progress_payments.service import PaymentContext, visible_payment
from app.modules.users.models import User


def is_revision_required(payment: SubcontractorProgressPayment) -> bool:
    """L177 "Revize Gerekli" rozetinin TEK kopya türevi (T4, spec §5).

    Rozet BEŞİNCİ bir durum DEĞİLDİR: `reject` kaydı `draft`a döndürür ve
    `rejected_at`'i damgalar; `submit` damgayı temizler. `draft` şartı
    ZORUNLUDUR — damgası duran ama yeniden onaya gönderilip onaylanmış bir kayıt
    rozet ALMAZ.

    Liste ve detay uçları AYNI fonksiyondan okur: iki kopya türev, ekranın iki
    yerinde farklı rozet göstermenin en kısa yoludur.
    """
    return payment.status == SubcontractorPaymentStatus.draft and payment.rejected_at is not None


def _line_read(line: SubcontractorProgressPaymentLine) -> SubcontractorProgressPaymentLineRead:
    """Satır türevleri (`adjusted_unit_price`/`line_total`) `calculations.py`nin
    saf fonksiyonlarından okunur — formül BURADA TEKRARLANMAZ (T3)."""
    return SubcontractorProgressPaymentLineRead(
        id=line.id,
        adjusted_unit_price=calculations.adjusted_unit_price(
            line.contract_unit_price, line.coefficient
        ),
        line_total=calculations.line_total(
            line.contract_unit_price, line.coefficient, line.quantity
        ),
        contract_item_id=line.contract_item_id,
        code=line.code,
        source_code=line.source_code,
        description=line.description,
        unit=line.unit,
        contract_unit_price=line.contract_unit_price,
        coefficient=line.coefficient,
        quantity=line.quantity,
        group_name=line.group_name,
        sort_order=line.sort_order,
        quantity_source=line.quantity_source,
    )


async def build_detail(
    session: AsyncSession, context: PaymentContext
) -> SubcontractorProgressPaymentDetail:
    """GÖRÜNÜRLÜK KONTROLÜ YAPMAZ — çağıranın kapsam kararını çoktan vermiş
    olması ŞARTTIR (işveren `build_detail` ayrımının aynı gerekçesi: `POST`/
    `PATCH` uçları `visible_projects` sorgusunu ikinci kez koşturmasın).

    T3'te `async` oldu: hesap bloğunun avans tavanı sözleşme bedeline ve ÖNCEKİ
    tamamlanmış hakedişlere bağlıdır (spec §3), ikisi de DB'den okunur.
    """
    payment, contract, project = context
    calculation = await amounts.calculation_for(session, payment)
    return SubcontractorProgressPaymentDetail(
        id=payment.id,
        slug=payment.slug,
        contract_id=payment.contract_id,
        project_id=payment.project_id,
        project_name=project.name,
        subcontractor_name=contract.subcontractor_name,
        contract_no=contract.contract_no,
        work_category=contract.work_category,
        sequence_no=payment.sequence_no,
        period_year=payment.period_year,
        period_month=payment.period_month,
        description=payment.description,
        status=payment.status,
        vat_pct=payment.vat_pct,
        advance_pct=payment.advance_pct,
        retainage_pct=payment.retainage_pct,
        default_coefficient=payment.default_coefficient,
        section_id=payment.section_id,
        submitted_at=payment.submitted_at,
        approved_at=payment.approved_at,
        approved_by=payment.approved_by,
        paid_at=payment.paid_at,
        rejected_at=payment.rejected_at,
        rejection_reason=payment.rejection_reason,
        is_revision_required=is_revision_required(payment),
        created_by=payment.created_by,
        created_at=payment.created_at,
        updated_at=payment.updated_at,
        lines=[_line_read(line) for line in payment.lines],
        calculation=calculation,
    )


async def get_detail(
    session: AsyncSession, actor: User, payment_ref: uuid.UUID | str
) -> SubcontractorProgressPaymentDetail:
    return await build_detail(session, await visible_payment(session, actor, payment_ref))


async def list_payments(
    session: AsyncSession,
    actor: User,
    *,
    project_id: uuid.UUID | None,
    site_id: uuid.UUID | None,
    period_year: int | None,
    period_month: int | None,
    status_filter: SubcontractorPaymentStatus | None,
    q: str | None,
    limit: int,
    offset: int,
) -> SubcontractorProgressPaymentListResponse:
    """Kapsam SQL'de kalır: süzgeç `visible_projects`ten türeyen kimlik listesidir,
    ikinci bir görünürlük kararı VERİLMEZ (spec §9.0)."""
    # IZN-B3: disiplinle KISITLI projelerin hakedisleri listeden DISARIDA kalir (Ü2).
    kisitli = await restricted_project_ids(session, actor.id)
    visible_ids = [p.id for p in await visible_projects(session, actor) if p.id not in kisitli]
    filters = {
        "project_id": project_id,
        "site_id": site_id,
        "period_year": period_year,
        "period_month": period_month,
        "status_filter": status_filter,
        "q": q,
    }
    rows = await repository.list_payments(
        session, visible_ids, limit=limit, offset=offset, **filters
    )
    total = await repository.count_payments(session, visible_ids, **filters)
    # Hesap TOPLU çekilir (işveren liste ucunun N+1 dersi): hakediş başına
    # sözleşme bedeli + geçmiş sorgusu koşulsaydı 50 satırlık sayfa 100 sorgu ederdi.
    blocks = await amounts.bulk_calculations(session, [payment for payment, _, _ in rows])
    return SubcontractorProgressPaymentListResponse(
        items=[
            SubcontractorProgressPaymentListItem(
                id=payment.id,
                # 🔴 LİSTEYE DE GİRER — `hakedisler/taseron/[paymentId]`
                # bağlantısını üreten şema budur.
                slug=payment.slug,
                contract_id=payment.contract_id,
                project_id=payment.project_id,
                project_name=project.name,
                subcontractor_name=contract.subcontractor_name,
                contract_no=contract.contract_no,
                work_category=contract.work_category,
                sequence_no=payment.sequence_no,
                period_year=payment.period_year,
                period_month=payment.period_month,
                description=payment.description,
                status=payment.status,
                section_id=payment.section_id,
                # HAK-NULL: satırın kapsamı. Sözleşme JOIN'i ZATEN kurulu
                # (`_list_stmt`) — ek istek YOK.
                contract_site_id=contract.site_id,
                created_at=payment.created_at,
                gross_total=blocks[payment.id].gross,
                net_total=blocks[payment.id].net,
                is_revision_required=is_revision_required(payment),
            )
            for payment, contract, project in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )

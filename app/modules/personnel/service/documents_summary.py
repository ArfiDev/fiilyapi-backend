"""IK-1 T4 — belge takibi OZETI (spec §2, §3 — BT mockup birebir)."""

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import timezone
from app.modules.personnel import repository, status
from app.modules.personnel.schemas import (
    HrDocumentsSummaryResponse,
    HrDocumentTypeBreakdown,
    HrExpiredDocument,
    HrExpiringDocument,
)
from app.modules.personnel.service.core import SUMMARY_LIST_LIMIT
from app.modules.personnel.visibility import visible_project_ids
from app.modules.users.models import User


async def build_hr_documents_summary(
    session: AsyncSession, *, today: date | None = None, actor: User | None = None
) -> HrDocumentsSummaryResponse:
    """BT özeti: 5 KPI + tip dağılımı + iki liste — SABİT sorgu sayısı (N+1 yok).

    Üç AGGREGA sorgu (katalog tipleri · aktif+yayın personel sayısı · aktif+yayın
    personelin tüm belgeleri) çekilir; durum türevi, dağılım kırılımı, `missing`
    sayımı ve iki listenin sıralama+kırpması Python'da bu satırlar üzerinden
    yapılır. Sorgu sayısı VERİ BÜYÜKLÜĞÜNDEN BAĞIMSIZDIR (`test_n_plus_1_sabit_sorgu`).

    `today` ENJEKTE EDİLİR (servis sınırı `timezone.today()` verir, test sabit tarih):
    sınır günleri deterministik olsun. Durum `status.derive_document_status` TEK
    KAYNAĞINDAN gelir — eşik (30 gün) burada TEKRARLANMAZ.

    Kapsam: yalnız AKTİF (`is_active=true`) + YAYINDA (`is_draft=false`) personel.
    `missing` KPI toplamı YALNIZ zorunlu (`is_mandatory=true`) tipler üzerinden;
    opsiyonel tipler dağılımda gösterilir ama KPI'ya girmez.
    """
    today = today or timezone.today()

    types = await repository.list_document_types(session)
    active_published_count = await repository.count_active_published_personnel(session)
    rows = await repository.list_active_published_document_rows(session)
    # IZN-B5a: görünmeyen projenin ADI sızmaz (`actor=None` yalnız HTTP dışı doğrudan servis).
    visible = await visible_project_ids(session, actor) if actor is not None else None

    total_documents = len(rows)
    valid = expiring = expired = 0
    # Tip başına belge durum sayaçları (kırılım) + o tipte kaydı olan personel kümesi.
    per_type_counts: dict[uuid.UUID, dict[str, int]] = {
        t.id: {"valid": 0, "expiring": 0, "expired": 0} for t in types
    }
    personnel_with_type: dict[uuid.UUID, set[uuid.UUID]] = {t.id: set() for t in types}
    expired_rows: list[HrExpiredDocument] = []
    expiring_rows: list[HrExpiringDocument] = []

    for row in rows:
        (
            doc_id,
            personnel_id,
            type_id,
            free_label,
            valid_until,
            full_name,
            type_name,
            _is_mandatory,
            validity_months,
            project_name,
            assigned_project_id,
        ) = row
        if visible is not None and assigned_project_id not in visible:
            project_name = None
        state = status.derive_document_status(valid_until, validity_months, today=today)
        if state == status.STATUS_VALID:
            valid += 1
        elif state == status.STATUS_EXPIRING:
            expiring += 1
        elif state == status.STATUS_EXPIRED:
            expired += 1

        if type_id is not None and type_id in per_type_counts:
            per_type_counts[type_id][state] += 1
            personnel_with_type[type_id].add(personnel_id)

        label = type_name if type_name is not None else (free_label or "belge")
        if state == status.STATUS_EXPIRED:
            expired_rows.append(
                HrExpiredDocument(
                    id=doc_id,
                    personnel_id=personnel_id,
                    personnel_name=full_name,
                    document_label=label,
                    project_name=project_name,
                    valid_until=valid_until,
                    days_overdue=(today - valid_until).days,
                )
            )
        elif state == status.STATUS_EXPIRING:
            expiring_rows.append(
                HrExpiringDocument(
                    id=doc_id,
                    personnel_id=personnel_id,
                    personnel_name=full_name,
                    document_label=label,
                    project_name=project_name,
                    valid_until=valid_until,
                    days_left=(valid_until - today).days,
                )
            )

    by_type: list[HrDocumentTypeBreakdown] = []
    missing_total = 0
    for t in types:
        counts = per_type_counts[t.id]
        have = len(personnel_with_type[t.id])
        # Bu tipte kaydı OLMAYAN aktif+yayın personel = eksik (kişi tabanı).
        missing_for_type = max(active_published_count - have, 0)
        if t.is_mandatory:
            missing_total += missing_for_type
        by_type.append(
            HrDocumentTypeBreakdown(
                type_id=t.id,
                type_name=t.name,
                is_mandatory=t.is_mandatory,
                validity_months=t.validity_months,
                total_documents=counts["valid"] + counts["expiring"] + counts["expired"],
                valid=counts["valid"],
                expiring=counts["expiring"],
                expired=counts["expired"],
                missing=missing_for_type,
            )
        )

    # En çok geciken önce (valid_until en eski) · en yakın biten önce (days_left en küçük).
    expired_rows.sort(key=lambda r: r.days_overdue, reverse=True)
    expiring_rows.sort(key=lambda r: r.days_left)

    return HrDocumentsSummaryResponse(
        total_documents=total_documents,
        valid=valid,
        expiring=expiring,
        expired=expired,
        missing=missing_total,
        by_type=by_type,
        expired_documents=expired_rows[:SUMMARY_LIST_LIMIT],
        expiring_documents=expiring_rows[:SUMMARY_LIST_LIMIT],
    )

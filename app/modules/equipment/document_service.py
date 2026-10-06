"""Ekipman belgesi iş kuralları (MK-2 T4, spec §2.3/§4) — M2:134-159 slotları.

`service.py`nin (MK-1 çekirdeği) `visible_equipment` kapısını İTHAL EDER,
YENİDEN YAZMAZ: K9/K20 görünürlüğü ekipmanın kendisiyle AYNI tanımdır — bir
ekipmanın belgesi, ekipmanın kendisi görünmeyen bir kullanıcıya da 404 verir
(IDOR deseni, ST/P2 kanonu).

## Dosya doğrulama BC/İK-1'in YOLUNU izler, İCAT ETMEZ

`app.modules.documents.files` saf fonksiyonları (`normalize_filename` /
`assert_allowed_extension` / `mime_for_filename` / `content_disposition`) VE
`settings.document_max_bytes` tavanı DOĞRUDAN kullanılır — beyaz liste, uzantı
kararı, dosya adı temizliği ikinci bir yerde TEKRARLANMAZ.

## K7 tarih eşikleri

`expiring_soon`: `today <= valid_until <= today + 30 gün` (sınır günler DAHİL).
`expired`: `valid_until < today`. Eşik burada TEK YERDEDİR — `build_summary`
dışında hiçbir yerde tekrarlanmaz.
"""

import uuid
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import timezone
from app.core.errors import EquipmentValidationError, NotFoundError
from app.modules.documents import files
from app.modules.equipment import document_repository as repository
from app.modules.equipment.document_schemas import (
    EquipmentDocumentsSummaryResponse,
    EquipmentDocumentUpdate,
    EquipmentExpiredDocument,
    EquipmentExpiringDocument,
)
from app.modules.equipment.models import Equipment, EquipmentDocument, EquipmentDocumentType
from app.modules.equipment.service import visible_equipment
from app.modules.equipment.service.core import _visible_project_ids
from app.modules.users.models import User

PERMISSION_MODULE = "equipment"

EXPIRING_SOON_DAYS = 30
"""K7 — İK-1'in `derive_document_status` eşiğiyle AYNI pencere (30 gün)."""

DOCUMENT_MISSING = "Ekipman belgesi bulunamadı."
"""Görünmeyen VE var olmayan kaydın TEK cümlesi — ikisi ayırt EDİLEMEZ."""

DOCUMENT_TYPE_MISSING = "Seçilen belge tipi bulunamadı."


async def list_document_types(session: AsyncSession) -> list[EquipmentDocumentType]:
    """`GET /equipment/document-types` — okuma `view` yeter, CRUD ucu YOK."""
    return await repository.list_document_types(session)


async def list_documents(session: AsyncSession, actor: User, equipment_id: uuid.UUID) -> list:
    """`GET /equipment/{id}/documents` — görünmeyen ekipman 404 (K9/K20)."""
    await visible_equipment(session, actor, equipment_id)
    return await repository.list_documents_for_equipment(session, equipment_id)


async def _resolve_type(session: AsyncSession, type_id: uuid.UUID) -> EquipmentDocumentType:
    doc_type = await repository.get_document_type(session, type_id)
    if doc_type is None:
        raise EquipmentValidationError(DOCUMENT_TYPE_MISSING)
    return doc_type


async def create_document(
    session: AsyncSession,
    actor: User,
    equipment_id: uuid.UUID,
    *,
    type_id: uuid.UUID,
    filename: str,
    content: bytes,
    valid_until: date | None,
    document_no: str | None = None,
    issued_at: date | None = None,
    note: str | None = None,
) -> tuple[EquipmentDocument, EquipmentDocumentType, str]:
    """Multipart yükleme — kapı SIRASI SABİTTİR (`documents` T3 deseni):

    1. görünürlük (K9/K20) → 404
    2. belge tipi VAR MI → 422
    3. dosya adı normalize + uzantı beyaz listesi → 422 (baytlar OKUNMADAN önce
       çağıran router'da denetlenir; bu fonksiyon zaten okunmuş `content` alır)
    """
    equipment = await visible_equipment(session, actor, equipment_id)
    doc_type = await _resolve_type(session, type_id)

    document = EquipmentDocument(
        equipment_id=equipment.id,
        type_id=doc_type.id,
        filename=filename,
        mime_type=files.mime_for_filename(filename),
        size_bytes=len(content),
        content=content,
        document_no=document_no,
        issued_at=issued_at,
        note=note,
        valid_until=valid_until,
    )
    document = await repository.create_document(session, document)
    detail = f"Ekipman belgesi yüklendi: {equipment.name} · {doc_type.name}"
    return document, doc_type, detail


async def update_document(
    session: AsyncSession,
    actor: User,
    document_id: uuid.UUID,
    data: EquipmentDocumentUpdate,
) -> tuple[EquipmentDocument, EquipmentDocumentType, str]:
    """Kısmi künye güncellemesi (K2) — KAPSAM DÖRT ALAN.

    Görünmeyen/var olmayan belge → 404 (AYNI cümle, `_visible_document` kapısı;
    403 DEĞİL: kaydın VARLIĞI sızdırılmaz). Dosya baytlarına, adına, mime
    tipine ve belge tipine DOKUNULMAZ — şema zaten o alanları taşımaz.

    `exclude_unset`: gönderilmeyen alan atlanır, açıkça `null` gönderilen alan
    temizlenir (`update_personnel_document` emsalinin birebiri).
    """
    document, equipment = await _visible_document(session, actor, document_id)

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(document, field, value)
    await session.flush()
    await session.refresh(document)

    doc_type = await _resolve_type(session, document.type_id)
    detail = f"Ekipman belgesi güncellendi: {equipment.name} · {doc_type.name}"
    return document, doc_type, detail


async def _visible_document(
    session: AsyncSession, actor: User, document_id: uuid.UUID
) -> tuple[EquipmentDocument, Equipment]:
    """Belgenin görünürlüğü EKİPMANININKİYLE AYNIDIR — ayrı bir kapı YOKTUR."""
    document = await repository.get_document(session, document_id)
    if document is None:
        raise NotFoundError(DOCUMENT_MISSING)
    try:
        equipment = await visible_equipment(session, actor, document.equipment_id)
    except NotFoundError as exc:
        # Ekipman görünmüyorsa belge de görünmüyor — AYNI cümle (IDOR deseni).
        raise NotFoundError(DOCUMENT_MISSING) from exc
    return document, equipment


async def get_document_for_download(
    session: AsyncSession, actor: User, document_id: uuid.UUID
) -> EquipmentDocument:
    document, _equipment = await _visible_document(session, actor, document_id)
    return document


async def delete_document(session: AsyncSession, actor: User, document_id: uuid.UUID) -> str:
    document, equipment = await _visible_document(session, actor, document_id)
    detail = f"Ekipman belgesi silindi: {equipment.name} · {document.filename}"
    await repository.delete_document(session, document)
    return detail


async def build_summary(
    session: AsyncSession, actor: User, *, today: date | None = None
) -> EquipmentDocumentsSummaryResponse:
    """K7 özeti — SABİT sorgu sayısı (İK-1 `build_hr_documents_summary` deseni).

    `today` ENJEKTE EDİLİR (servis sınırı `timezone.today()` verir, test sabit
    tarih kullanır): sınır günleri (bugün / +30 / +31 / dün) deterministik olsun.

    🔴 `actor` K20 KAPSAMI İÇİNDİR ve imzada sonradan açıldı: bu fonksiyon
    modülün kapsam süzgecinden geçmeyen TEK okuma yoluydu. Öteki her yol
    (`list_documents`:57, `_visible_document`:137) `visible_equipment`ten geçer.
    Sızan şey sayaçtan ibaret değildi — `expiring_documents`/`expired_documents`
    dizileri `equipment_name` ve `type_name` taşır, yani görünmeyen projenin
    makine adları doğrudan basılıyordu.

    Depo istisnası (`site_id IS NULL`) `repository.scope` içinde korunur:
    atanmamış makine HERKESE görünür, aksi hâlde hiç kimse göremezdi.
    """
    today = today or timezone.today()
    horizon = today + timedelta(days=EXPIRING_SOON_DAYS)

    project_ids = await _visible_project_ids(session, actor, liste=True)
    rows = await repository.list_active_document_rows_for_summary(session, project_ids)
    required_types = [t for t in await repository.list_document_types(session) if t.is_required]
    present_pairs = await repository.list_active_equipment_type_pairs(session, project_ids)
    active_equipment_ids = await repository.list_active_equipment_ids(session, project_ids)

    expiring_soon = 0
    expired = 0
    expiring_rows: list[EquipmentExpiringDocument] = []
    expired_rows: list[EquipmentExpiredDocument] = []

    for row in rows:
        if row.valid_until is None:
            continue
        if row.valid_until < today:
            expired += 1
            expired_rows.append(
                EquipmentExpiredDocument(
                    id=row.id,
                    equipment_id=row.equipment_id,
                    equipment_name=row.equipment_name,
                    type_name=row.type_name,
                    valid_until=row.valid_until,
                    days_overdue=(today - row.valid_until).days,
                )
            )
        elif today <= row.valid_until <= horizon:
            expiring_soon += 1
            expiring_rows.append(
                EquipmentExpiringDocument(
                    id=row.id,
                    equipment_id=row.equipment_id,
                    equipment_name=row.equipment_name,
                    type_name=row.type_name,
                    valid_until=row.valid_until,
                    days_left=(row.valid_until - today).days,
                )
            )

    # K7/İK-1 `missing`: bu tipte kaydı OLMAYAN aktif ekipman = eksik.
    missing = sum(
        1
        for equipment_id in active_equipment_ids
        for t in required_types
        if (equipment_id, t.id) not in present_pairs
    )

    expired_rows.sort(key=lambda r: r.days_overdue, reverse=True)
    expiring_rows.sort(key=lambda r: r.days_left)

    return EquipmentDocumentsSummaryResponse(
        expiring_soon=expiring_soon,
        expired=expired,
        missing=missing,
        expiring_documents=expiring_rows,
        expired_documents=expired_rows,
    )

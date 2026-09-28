"""Ekipman belgesi uçları (MK-2 T4, spec §2.3/§4) — M2:134-159 slotları.

Kapı **`equipment`** iznidir (MK-1'de açıldı; MK-2'de YENİ MODÜL AÇILMAZ, izin
migration'ı YOKTUR): okuma `view`, yazmanın tamamı `full`. Görünmeyen ekipmanın
belgesi 404'tür (K9/K20).

## 🔴 Niçin AYRI bir router — ve niçin `equipment_router`dan ÖNCE kaydedilir

`GET /equipment/document-types` İKİ segmentlidir (`equipment` / `document-types`)
ve `router.py`nin (MK-1) `/equipment/{equipment_id}` yolu da İKİ segmentlidir.
FastAPI yolları KAYIT SIRASINA göre eşler: `{equipment_id}` route'u ÖNCE
kaydedilirse `document-types` dizgesi bir UUID sanılır ve 422 döner
(`equipment_rental_router`ın kurduğu tuzağın AYNISI). Bu yüzden bu router da
`main.py`de `equipment_router`dan ÖNCE `include_router` edilir; kural bir
BEKÇİ TESTİYLE kilitlidir (`test_rota_sirasi_document_types_UUID_SANILMAZ`).

`/equipment/{equipment_id}/documents` (liste/yükleme) ÜÇ segmentlidir ve
`/equipment/documents/{document_id}` (indirme/silme) da ÜÇ-DÖRT segmentlidir;
ikisi de `{equipment_id}` (2 segment) ile ASLA çakışmaz — yalnız `document-types`
sıraya duyarlıdır.

## Dosya yükleme — BC/İK-1'in yolunu BİREBİR izler

Kapı sırası (`documents/router.upload_document_endpoint` deseni):
1. dosya adı normalize edilir (yol/başlık enjeksiyonu temizlenir) → 422
2. uzantı beyaz listeden geçer → 422 — **baytlar OKUNMADAN ÖNCE**
3. gövde parçalı okunur, tavan aşılırsa → 413
4. görünürlük (K9/K20) ve belge tipi denetimi → 404 / 422

`GET` uçları `record_audit` ÇAĞIRMAZ (WORKFLOW kuralı — okumalar denetlenmez).
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.config import settings
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.core.ratelimit import client_ip
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.documents import files
from app.modules.documents.guards import DOCUMENT_TOO_LARGE
from app.modules.equipment import document_service as service
from app.modules.equipment.document_schemas import (
    DOCUMENT_NO_MAX_LENGTH,
    EquipmentDocumentListResponse,
    EquipmentDocumentResponse,
    EquipmentDocumentsSummaryResponse,
    EquipmentDocumentTypeListResponse,
    EquipmentDocumentTypeResponse,
    EquipmentDocumentUpdate,
)
from app.modules.users.models import User

router = APIRouter(prefix="/equipment", tags=["equipment"], responses=COMMON_ERROR_RESPONSES)

_VIEW = require_permission(service.PERMISSION_MODULE, AccessLevel.view)
_FULL = require_permission(service.PERMISSION_MODULE, AccessLevel.full)

_UPLOAD_CHUNK_BYTES = 65536
"""`documents/router.py`nin AYNI sabiti — tavanı aşan istek en fazla bu kadar
fazla bayt okumuş olur (belleğe almadan reddetme sözü)."""


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


async def _read_within_limit(file: UploadFile) -> bytes:
    """`documents/router._read_within_limit`in AYNISI — PARÇA PARÇA okur, tavanı
    aşan ANDA 413 ile keser (2 GB'lık bir gövde tamamen belleğe alınmaz)."""
    max_bytes = settings.document_max_bytes
    parcalar: list[bytes] = []
    toplam = 0
    while parca := await file.read(_UPLOAD_CHUNK_BYTES):
        toplam += len(parca)
        if toplam > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=DOCUMENT_TOO_LARGE,
            )
        parcalar.append(parca)
    return b"".join(parcalar)


def _to_type_response(doc_type) -> EquipmentDocumentTypeResponse:
    return EquipmentDocumentTypeResponse.model_validate(doc_type)


def _to_document_response(row) -> EquipmentDocumentResponse:
    return EquipmentDocumentResponse(
        id=row.id,
        equipment_id=row.equipment_id,
        type_id=row.type_id,
        type_code=row.type_code,
        type_name=row.type_name,
        filename=row.filename,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        document_no=row.document_no,
        issued_at=row.issued_at,
        valid_until=row.valid_until,
        note=row.note,
        created_at=row.created_at,
    )


def _document_payload(document, doc_type) -> EquipmentDocumentResponse:
    """ORM kaydı + tip künyesinden yanıt (POST/PATCH ORTAK) — liste ucunun
    `Row` yolundan AYRI ama alan kümesi AYNI; iki yerde tekrarlanmaz."""
    return EquipmentDocumentResponse(
        id=document.id,
        equipment_id=document.equipment_id,
        type_id=document.type_id,
        type_code=doc_type.code,
        type_name=doc_type.name,
        filename=document.filename,
        mime_type=document.mime_type,
        size_bytes=document.size_bytes,
        document_no=document.document_no,
        issued_at=document.issued_at,
        valid_until=document.valid_until,
        note=document.note,
        created_at=document.created_at,
    )


@router.get(
    "/document-types",
    response_model=EquipmentDocumentTypeListResponse,
    dependencies=[_VIEW],
)
async def list_equipment_document_types_endpoint(
    session: DbSession,
) -> EquipmentDocumentTypeListResponse:
    """Altı sabit slot (M2:134-159). CRUD ucu YOK — yönetimi ayarlar dilimine
    ertelenmiştir (İK-1 emsali)."""
    types = await service.list_document_types(session)
    return EquipmentDocumentTypeListResponse(items=[_to_type_response(t) for t in types])


@router.get(
    "/documents/summary",
    response_model=EquipmentDocumentsSummaryResponse,
    dependencies=[_VIEW],
)
async def equipment_documents_summary_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> EquipmentDocumentsSummaryResponse:
    """K7 özeti: `expiring_soon` (30 gün) + `expired` + `missing` (zorunlu tip
    eksikleri, yalnız AKTİF ekipman) — hepsi K20 kapsamından GEÇER."""
    return await service.build_summary(session, user)


@router.get(
    "/{equipment_id}/documents",
    response_model=EquipmentDocumentListResponse,
    dependencies=[_VIEW],
)
async def list_equipment_documents_endpoint(
    equipment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> EquipmentDocumentListResponse:
    """Görünmeyen ekipman → 404 (K9/K20, IDOR deseni)."""
    rows = await service.list_documents(session, user, equipment_id)
    return EquipmentDocumentListResponse(items=[_to_document_response(r) for r in rows])


@router.post(
    "/{equipment_id}/documents",
    response_model=EquipmentDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Ekipman bulunamadı (görünmeyen dahil)"},
        413: {"description": "Dosya boyutu tavanı aşıyor"},
        422: {"description": "Desteklenmeyen dosya türü ya da geçersiz belge tipi"},
    },
    dependencies=[_FULL],
)
async def create_equipment_document_endpoint(
    request: Request,
    equipment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    file: Annotated[UploadFile, File(...)],
    type_id: Annotated[uuid.UUID, Form()],
    valid_until: Annotated[date | None, Form()] = None,
    document_no: Annotated[str | None, Form(max_length=DOCUMENT_NO_MAX_LENGTH)] = None,
    issued_at: Annotated[date | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
) -> EquipmentDocumentResponse:
    """Multipart yükleme (M2:134-159). Kapı sırası modül docstring'inde.

    FRM-1'in üç künye alanı (`document_no`/`issued_at`/`note`) OPSİYONELDİR:
    hiç gönderilmezse NULL kalır ve önceki davranış birebir korunur.
    """
    filename = files.normalize_filename(file.filename)
    files.assert_allowed_extension(filename)
    content = await _read_within_limit(file)

    document, doc_type, detail = await service.create_document(
        session,
        user,
        equipment_id,
        type_id=type_id,
        filename=filename,
        content=content,
        valid_until=valid_until,
        document_no=document_no,
        issued_at=issued_at,
        note=note,
    )
    await _audit(request, session, user, AuditAction.create, detail)
    return _document_payload(document, doc_type)


@router.patch(
    "/documents/{document_id}",
    response_model=EquipmentDocumentResponse,
    responses={404: {"description": "Belge bulunamadı (görünmeyen ekipmanın belgesi dahil)"}},
    dependencies=[_FULL],
)
async def update_equipment_document_endpoint(
    request: Request,
    document_id: uuid.UUID,
    data: EquipmentDocumentUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> EquipmentDocumentResponse:
    """Kısmi künye güncellemesi (K2) — DÖRT alan: `document_no` · `issued_at` ·
    `note` · `valid_until`.

    🔴 Dosyanın kendisi (içerik/ad/mime tipi) ve belge TİPİ bu uçtan
    DEĞİŞTİRİLEMEZ; yanlış dosya silinip yeniden yüklenir. Yetki DELETE/POST
    ile aynı (`full`), görünmeyen kayıt 404'tür (403 DEĞİL — varlık sızmaz).
    """
    document, doc_type, detail = await service.update_document(session, user, document_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return _document_payload(document, doc_type)


@router.get(
    "/documents/{document_id}/download",
    dependencies=[_VIEW],
    responses={
        200: {"content": {"application/octet-stream": {}}},
        404: {"description": "Belge bulunamadı (görünmeyen ekipmanın belgesi dahil)"},
    },
)
async def download_equipment_document_endpoint(
    document_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> StreamingResponse:
    """`documents/router.download_document_endpoint`in AYNI başlık deseni:
    `Content-Type` künyeden, `Content-Length` `size_bytes`ten, `Content-Disposition`
    RFC 5987 ile ve `X-Content-Type-Options: nosniff` — arşivdeki bir dosyanın
    tarayıcıda ÇALIŞTIRILMASI engellenir."""
    document = await service.get_document_for_download(session, user, document_id)

    # Akış gövdesi function_stack kapandıktan sonra üretilir: ORM nesnesine değil,
    # önceden okunmuş yerel değere bağlanılır.
    icerik = document.content

    async def _stream():
        yield icerik

    return StreamingResponse(
        _stream(),
        media_type=document.mime_type,
        headers={
            "Content-Length": str(document.size_bytes),
            "Content-Disposition": files.content_disposition(document.filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "Belge bulunamadı (görünmeyen ekipmanın belgesi dahil)"}},
    dependencies=[_FULL],
)
async def delete_equipment_document_endpoint(
    request: Request,
    document_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    detail = await service.delete_document(session, user, document_id)
    await _audit(request, session, user, AuditAction.delete, detail)

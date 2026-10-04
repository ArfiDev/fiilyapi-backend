"""Klasör uçları (T2) — spec §3 birinci satırı.

Kapı `documents` iznidir (spec §7 S2, 20. modül): okuma `view`, klasör açma ve
adlandırma `full`, silme `admin`. Üç seviye üç ayrı bağımlılıktır ve BURADA
durur; servis katmanı yetkiye değil KAPSAMA (`visible_projects`) bakar.

`GET` `record_audit` ÇAĞIRMAZ (WORKFLOW kuralı — okumalar denetlenmez); üç yazma
ucunun üçü de tek denetim satırı yazar ve metni servis katmanında, kayıt
değişmeden/yok olmadan ÖNCE kurulur.

Router prefix TAŞIMAZ: uçlar üç ayrı kök altına dağılır (`/projects/{id}/
document-folders`, `/document-folders/{id}` ve `/documents`), `sites/router.py`
deseninin birebiri.

## Belge uçları (T3)

| Uç | Yetki |
|---|---|
| `POST /documents` (multipart) | `full` |
| `GET /documents` | `view` |
| `GET /documents/{id}/download` | `view` |
| `PATCH /documents/{id}` | `full` |
| `DELETE /documents/{id}` | `admin` |

⚠️ **`DELETE /documents/{id}` UCU AÇILIR AMA EKRANDA BASILMAZ** — spec §3'ün
bilinçli kararı: mockup'ta belge silme aksiyonu YOKTUR (E12 ve SB'nin belge
kartlarında yalnız "İndir" vardır). Uç, yanlış yüklenen bir dosyayı sistem
yöneticisinin temizleyebilmesi için vardır; frontend dilimi bunu bir düğmeye
BAĞLAMAYACAK. Mockup'a silme aksiyonu gelirse buton o zaman çizilir.
"""

import uuid
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.config import settings
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES, DELETE_403_YANITI
from app.core.permissions import require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.documents import files, guards, service
from app.modules.documents.deps import get_storage_backend, get_streaming_storage_backend
from app.modules.documents.schemas import (
    DESCRIPTION_MAX_LENGTH,
    DocumentFolderCreate,
    DocumentFolderListResponse,
    DocumentFolderRead,
    DocumentFolderUpdate,
    DocumentListResponse,
    DocumentRead,
    DocumentUpdate,
)
from app.modules.documents.storage import StorageBackend
from app.modules.users.models import User

router = APIRouter(tags=["documents"], responses=COMMON_ERROR_RESPONSES)

_VIEW = require_permission(service.PERMISSION_MODULE, AccessLevel.view)
_FULL = require_permission(service.PERMISSION_MODULE, AccessLevel.full)
# SILME uclari `require_system_admin` ile kapilidir (SIL-B1): modul seviyesi degil rol ANAHTARI.


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


@router.get(
    "/projects/{project_id}/document-folders",
    response_model=DocumentFolderListResponse,
    dependencies=[_VIEW],
)
async def list_document_folders_endpoint(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    site_id: Annotated[uuid.UUID | None, Query()] = None,
) -> DocumentFolderListResponse:
    """Bir KÖKÜN klasörleri — düz liste, hiyerarşiyi `parent_id` taşır.

    `site_id` bir SÜZGEÇTİR: verilmezse yalnız PROJE DÜZEYİ klasörler döner,
    verilirse yalnız o şantiyeninkiler. Gerekçe `service.list_folders`tadır
    (E12 kökü her an tek bir proje/şantiye ikilisidir).

    Görünmeyen proje 404 döner ve gövdesi var olmayan kimliğinkiyle AYNIDIR.

    Belge SAYACI YOKTUR — gerekçe `schemas` başlığındadır.
    """
    folders = await service.list_folders(session, user, project_id, site_id)
    return DocumentFolderListResponse(
        folders=[DocumentFolderRead.model_validate(folder) for folder in folders]
    )


@router.post(
    "/projects/{project_id}/document-folders",
    response_model=DocumentFolderRead,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "Bu kapsamda aynı adlı klasör var"}},
    dependencies=[_FULL],
)
async def create_document_folder_endpoint(
    request: Request,
    project_id: uuid.UUID,
    data: DocumentFolderCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> DocumentFolderRead:
    """Yeni klasör. Kategori seti SERBESTTİR (spec §7 S3) — otomatik seed YOKTUR.

    * ad çakışması → 409 (kontrol UYGULAMA katmanındadır; T1 bulgusu: NULL'lı
      kapsamda DB kısıtı işlemez)
    * `site_id` başka projenin şantiyesi → 422
    * `parent_id` başka kapsamın klasörü → 422
    """
    folder, detail = await service.create_folder(session, user, project_id, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return DocumentFolderRead.model_validate(folder)


@router.patch(
    "/document-folders/{folder_id}",
    response_model=DocumentFolderRead,
    responses={409: {"description": "Bu kapsamda aynı adlı klasör var"}},
    dependencies=[_FULL],
)
async def rename_document_folder_endpoint(
    request: Request,
    folder_id: uuid.UUID,
    data: DocumentFolderUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> DocumentFolderRead:
    """YALNIZ ad değişir. Klasör TAŞIMA ucu yoktur (gerekçe `schemas`ta)."""
    context = await service.visible_folder(session, user, folder_id)
    folder, detail = await service.rename_folder(session, context, data.name)
    await _audit(request, session, user, AuditAction.update, detail)
    return DocumentFolderRead.model_validate(folder)


@router.delete(
    "/document-folders/{folder_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**DELETE_403_YANITI, 409: {"description": "Klasörde belge ya da alt klasör var"}},
    dependencies=[require_system_admin()],
)
async def delete_document_folder_endpoint(
    request: Request,
    folder_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Belge klasörünü siler. YALNIZ Sistem Yöneticisi.

    YALNIZ BOŞ klasör silinir; belge ya da alt klasör varsa **409** (iş kuralı). Yanıt
    `204 No Content`, gövdesizdir.
    """
    context = await service.visible_folder(session, user, folder_id)
    detail = await service.delete_folder(session, context)
    await _audit(request, session, user, AuditAction.delete, detail)


# --- Belge uçları (T3) ---

_UPLOAD_CHUNK_BYTES = 65536
"""Yükleme gövdesinin tek seferde okunan parçası (64 KB, company logo deseni).

Tavanı aşan istek EN FAZLA bu kadar fazla bayt okumuş olur; sabit büyütülürse
"belleğe almadan reddetme" sözü o oranda zayıflar.
"""


async def _read_within_limit(file: UploadFile) -> bytes:
    """Gövdeyi PARÇA PARÇA okur ve tavanı aşan ilk anda 413 ile keser.

    ⚠️ Bu döngü "önce oku, sonra bak"ın yerine geçer ve sebebi bir DoS
    yüzeyidir: 2 GB'lık bir gövdeyi tamamen okuyup ardından reddetmek, isteği
    reddederken bile 2 GB bellek harcamak demektir. Burada okuma, toplam tavanı
    aştığı ANDA durur — sonraki baytlar hiç istenmez (kanıt:
    `test_boyut_sinirinda_govde_tamamen_bellege_alinmaz`).

    Tavan `settings.document_max_bytes`tır (spec §4, varsayılan 50 MB) ve
    HARDCODE DEĞİLDİR — sınır büyütülecekse env yeter, kod değişmez.

    `company` logo ucunun deseninin kardeşidir; parça boyu da oradan gelir.
    """
    max_bytes = settings.document_max_bytes
    parcalar: list[bytes] = []
    toplam = 0
    while parca := await file.read(_UPLOAD_CHUNK_BYTES):
        toplam += len(parca)
        if toplam > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=guards.DOCUMENT_TOO_LARGE,
            )
        parcalar.append(parca)
    return b"".join(parcalar)


@router.post(
    "/documents",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        413: {"description": "Dosya boyutu tavanı aşıyor"},
        422: {"description": "Desteklenmeyen dosya türü ya da kapsam dışı klasör/şantiye"},
    },
    dependencies=[_FULL],
)
async def upload_document_endpoint(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
    file: Annotated[UploadFile, File(...)],
    project_id: Annotated[uuid.UUID, Form()],
    site_id: Annotated[uuid.UUID | None, Form()] = None,
    folder_id: Annotated[uuid.UUID | None, Form()] = None,
    description: Annotated[str | None, Form(max_length=DESCRIPTION_MAX_LENGTH)] = None,
    filename: Annotated[str | None, Form(min_length=1, max_length=255)] = None,
) -> DocumentRead:
    """Multipart yükleme (spec §3/§4).

    KAPI SIRASI SABİTTİR ve ucuzdan pahalıya gider:
    1. dosya adı normalize edilir (yol/başlık enjeksiyonu temizlenir) → 422
    2. uzantı beyaz listeden geçer → 422 — **baytlar OKUNMADAN ÖNCE**
    3. gövde parçalı okunur, tavan aşılırsa → 413
    4. kapsam korkulukları (şantiye/klasör) → 422, görünmeyen proje → 404

    Uzantı kontrolünün okumadan önce olması bilinçlidir: yasak uzantılı 50 MB'lık
    bir gövdeyi sonuna kadar okumanın hiçbir karşılığı yoktur.

    `filename` OPSİYONELDİR (T1). Verilmezse yüklenen dosyanın adı kullanılır
    (mevcut davranış). Verilirse künyeye YAZILACAK NİHAİ ad odur ve `file.filename`
    hiç kullanılmaz — kaynak ne olursa olsun (form alanı ya da yüklenen dosyanın
    adı) aynı iki kapıdan (normalize + beyaz liste) geçer, biri diğerini
    ATLATAMAZ. `max_length=255` `DocumentUpdate.filename`in (PATCH) aynı
    kuralıdır — iki giriş noktası ayrı ayrı yazılsaydı biri güncellenip diğeri
    unutulur, biri diğerinden gevşek kalırdı.

    Künye + baytlar TEK transaction'da yazılır; `put` patlarsa künye de yazılmaz.
    """
    kaynak_ad = filename if filename is not None else file.filename
    nihai_ad = files.normalize_filename(kaynak_ad)
    files.assert_allowed_extension(nihai_ad)
    filename = nihai_ad
    content = await _read_within_limit(file)

    document, detail = await service.upload_document(
        session,
        storage,
        user,
        project_id=project_id,
        site_id=site_id,
        folder_id=folder_id,
        description=description,
        filename=filename,
        content=content,
    )
    await _audit(request, session, user, AuditAction.create, detail)
    return DocumentRead.model_validate(document)


@router.get("/documents", response_model=DocumentListResponse, dependencies=[_VIEW])
async def list_documents_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    project_id: Annotated[uuid.UUID, Query()],
    site_id: Annotated[uuid.UUID | None, Query()] = None,
    folder_id: Annotated[uuid.UUID | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=150)] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> DocumentListResponse:
    """Künye listesi — BAYTLARA DOKUNMADAN (spec §2; SQL düzeyinde test edilir).

    `project_id` ZORUNLUDUR; `site_id` T2'nin klasör listesiyle aynı semantiktedir
    (verilmezse proje düzeyi); `folder_id` verilmezse kapsamın tamamı döner
    (SB kökü "Tüm Belgeler"). Gerekçeler `service.list_documents`tadır.

    SIRALAMA SEÇİLEBİLİR DEĞİLDİR (`created_at` azalan) ve SAYFALAMA YOKTUR —
    mockup'ta ikisi de yoktur, icat edilmez. `limit` yalnız "Son Eklenenler"
    panelini kısaltmak içindir (spec §3).
    """
    documents = await service.list_documents(
        session, user, project_id, site_id, folder_id, q, limit
    )
    return DocumentListResponse(documents=[DocumentRead.model_validate(d) for d in documents])


@router.get(
    "/documents/{document_id}/download",
    dependencies=[_VIEW],
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def download_document_endpoint(
    document_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    storage: Annotated[StorageBackend, Depends(get_streaming_storage_backend)],
) -> StreamingResponse:
    """İçeriği PARÇALI akıtır — 48 MB'lık bir ZIP tam-bellek OKUNMAZ (spec §3).

    Başlıklar:
      * `Content-Type` künyedeki tiptir (uzantıdan türetilmiştir; istemcinin
        yükleme sırasındaki beyanı DEĞİL).
      * `Content-Length` künyedeki `size_bytes`tır — akış hâlinde tarayıcı
        yüzdelik ilerleme gösterebilsin diye açıkça verilir.
      * `Content-Disposition` Türkçe karakterli adı RFC 5987 ile taşır ve
        `attachment`tır; `nosniff` ile birlikte arşivdeki bir dosyanın
        tarayıcıda ÇALIŞTIRILMASINI engeller.

    İlk parça yanıt başlamadan önce alınır: içeriği olmayan künye yarım bir 200
    değil düzgün bir 404 döner (`service.start_document_stream`).
    """
    context = await service.visible_document(session, user, document_id)
    document = context.document
    akis = await service.start_document_stream(storage, document.id)
    return StreamingResponse(
        akis,
        media_type=document.mime_type,
        headers={
            "Content-Length": str(document.size_bytes),
            "Content-Disposition": files.content_disposition(document.filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.patch(
    "/documents/{document_id}",
    response_model=DocumentRead,
    responses={422: {"description": "Desteklenmeyen dosya türü ya da kapsam dışı klasör"}},
    dependencies=[_FULL],
)
async def update_document_endpoint(
    request: Request,
    document_id: uuid.UUID,
    data: DocumentUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> DocumentRead:
    """Ad / açıklama / klasör taşıma (spec §3). Kapsam (proje/şantiye) DEĞİŞMEZ.

    Taşımada hedef klasörün kapsamı belgeninkiyle aynı olmalıdır (422);
    `folder_id: null` belgeyi kapsamın köküne taşır ve İZİNLİDİR.
    """
    context = await service.visible_document(session, user, document_id)
    document, detail = await service.update_document(session, context, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return DocumentRead.model_validate(document)


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**DELETE_403_YANITI},
    dependencies=[require_system_admin()],
)
async def delete_document_endpoint(
    request: Request,
    document_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
) -> None:
    """Belgeyi (künye + baytlar) siler. YALNIZ Sistem Yöneticisi.

    ⚠️ BU UÇ EKRANDA BASILMAZ (modül docstring'i): mockup'ta belge silme aksiyonu yoktur; uç yalnız
    yanlış yüklenen dosyayı temizlemek içindir. Yanıt `204 No Content`, gövdesizdir.
    """
    context = await service.visible_document(session, user, document_id)
    detail = await service.delete_document(session, storage, context)
    await _audit(request, session, user, AuditAction.delete, detail)

"""Personel belge tipi katalogu ucu — BOR-TEMIZ T3 (Boşluk #4).

`PersonnelDocumentType` modeli ve `repository.list_document_types` ZATEN
VARDI; eksik olan yalnız `GET /personnel/document-types` HTTP ucuydu. Migration
YOKTUR, CRUD ucu YOKTUR (katalog yönetimi ayarlar dilimine ertelenmiştir —
İK-1/MK-2 kararının aynısı).

## 🔴 Niçin AYRI, İNCE bir router (K5)

`personnel/service.py` dosya tavanının (800 satır) üstünde — o dosyaya SATIR
EKLENMEZ. `personnel/router.py` da sıkışık; yeni ucu oraya eklemek
yerine bu dosya doğrudan `repository.list_document_types(session)`i çağırır
(servis katmanı atlanır — `equipment/document_router.py`nin
`list_equipment_document_types_endpoint`inin birebiri).

## 🔴 Niçin `personnel_router`dan ÖNCE kaydedilir

`GET /personnel/document-types` İKİ segmentlidir ve `personnel/router.py`nin
`GET /personnel/{personnel_id}` yolu da İKİ segmentlidir. FastAPI yolları
KAYIT SIRASINA göre eşler: `{personnel_id}` route'u ÖNCE kaydedilirse
`document-types` dizgesi UUID sanılır ve 422 döner (ölçüldü — RED adımında
`test_yetkili_200_...` bu hatayla düştü: `uuid_parsing ... input: document-types`).
`equipment_document_router`ın `equipment_router`dan önce kaydedilme kuralının
BİREBİRİ; `main.py`de bu router `personnel_router`dan ÖNCE `include_router`
edilir.

## Yetki

Kapı `personnel` iznidir, düzey `view` — mevcut personel okuma uçlarıyla
TUTARLI (`personnel/router.py`deki `_VIEW`); yeni bir izin düzeyi İCAT EDİLMEDİ.

## Sıralama + `is_active`

`repository.list_document_types` zaten `sort_order, name` ile sıralar —
ekipman emsaliyle tutarlı. `is_active` süzgeci UYGULANMAZ: pasif tip de
listeye girer (ölçülen gerçek davranış, `test_pasif_tip_listede_gorunur...`
ile kilitlenmiştir). Davranış burada SESSİZCE DEĞİŞTİRİLMEDİ.

## K7 — "kırık" değil İSRAF

Frontend'in belge tiplerini `by_type[]`ten türetmesi EKSİKSİZDİR (kayıtsız tip
de geliyordu — `personnel/service.py:423,488-509`). Kusur doğruluk değil
MALİYETTİ: bir dropdown için tüm personel×belge özet sorgusu koşuyordu. Bu uç
o israfı giderir.
"""

from fastapi import APIRouter

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.modules.personnel import repository, service
from app.modules.personnel.schemas import (
    PersonnelDocumentTypeListResponse,
    PersonnelDocumentTypeResponse,
)

router = APIRouter(tags=["personnel"], responses=COMMON_ERROR_RESPONSES)

_VIEW = require_permission(service.PERMISSION_MODULE, AccessLevel.view)


@router.get(
    "/personnel/document-types",
    response_model=PersonnelDocumentTypeListResponse,
    dependencies=[_VIEW],
)
async def list_personnel_document_types_endpoint(
    session: DbSession,
) -> PersonnelDocumentTypeListResponse:
    """Katalog tipleri, `sort_order` sırasıyla. CRUD ucu YOK (K5 notu)."""
    types = await repository.list_document_types(session)
    return PersonnelDocumentTypeListResponse(
        items=[PersonnelDocumentTypeResponse.model_validate(t) for t in types]
    )

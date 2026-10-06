"""Personel kartoteksi uçları — puantaj T2 (spec §3).

`customers/router.py`nin birebiri: kapı sabitleri modül düzeyinde tanımlanır,
denetim metinleri `audit/messages.py`den gelir.

Kapılar `personnel` iznidir (seed'de HAZIR, matris DEĞİŞMEZ): okuma `view`,
yazma `full`. Bu ayrım **şantiye şefini SALT OKUR yapar** (matriste
`personnel=_V`) — işçiyi İK ekler (spec §5 bilinçli sınır).

**`visible_projects` süzgeci yok, ama `?project_id=` süzgeci VAR** (İK-1 spec §5
K4): `personnel` yine şirket-geneli bir İK varlığıdır ve tüm projelerde görünür.
Puantaj diliminin "proje süzgeci EKLEMESİN" notu `assigned_project_id` atama
kolonu YOKKEN geçerliydi; §5 K4 kararı bunu güncelledi — kolon açıldığından
`?project_id=` meşru bir DARALTMA süzgecidir, yetki genişletmez (IDOR açığı
DEĞİLDİR: kapsam denetimi yine `personnel` iznidir).

**DELETE ucu AÇILMAZ** (spec §3): puantaj kayıtları personele RESTRICT ile
bağlıdır; kartoteksten çıkarma `PATCH {"is_active": false}` ile yapılır.
"""

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import http
from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.mask_route import MaskeRotasi, maskele_baglamli
from app.core.openapi import COMMON_ERROR_RESPONSES, DELETE_403_YANITI
from app.core.permissions import require_pages, require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.core.slug import parse_ref
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.personnel import export, repository, service
from app.modules.personnel.export import XLSX_MEDIA_TYPE, build_personnel_workbook
from app.modules.personnel.models import LeaveStatus, Personnel
from app.modules.personnel.schemas import (
    HrDocumentsSummaryResponse,
    HrLeavesSummaryResponse,
    LeaveApproveRequest,
    LeaveBalanceResponse,
    LeaveBalanceUpdate,
    LeaveRejectRequest,
    LeaveRequestCreate,
    LeaveRequestListResponse,
    LeaveRequestResponse,
    LeaveRequestUpdate,
    LeaveTypeResponse,
    PersonnelCreate,
    PersonnelDocumentCreate,
    PersonnelDocumentResponse,
    PersonnelDocumentUpdate,
    PersonnelListResponse,
    PersonnelResponse,
    PersonnelUpdate,
    SelfLeaveRequestCreate,
)
from app.modules.personnel.visibility import filter_hidden, hide_assignment, visible_project_ids
from app.modules.personnel.write_gate import reject_hidden_unique_fields
from app.modules.site_diary.models import WorkerSource
from app.modules.users.models import User

router = APIRouter(tags=["personnel"], responses=COMMON_ERROR_RESPONSES, route_class=MaskeRotasi)

_VIEW = require_permission(service.PERMISSION_MODULE, AccessLevel.view)
_FULL = require_permission(service.PERMISSION_MODULE, AccessLevel.full)
#: IZN-B2: İzin Onayla/Reddet = İzin Yönetimi sayfası ONAYLAR (eşik eski `full`).
_LEAVE_APPROVE = require_pages(("ik.izin_yonetimi",), "approve")
# SILME uclari `require_system_admin` ile kapilidir (SIL-B1): modul seviyesi degil rol ANAHTARI.


@dataclass
class PersonnelFilters:
    """Liste ve dışa aktarma uçlarının PAYLAŞTIĞI süzgeç kümesi — tek yerde tanımlı.

    `audit/router.py:AuditFilters` emsali. İki uç aynı `dataclass`ı `Depends()`
    ile alır: sorgu parametrelerinin adları, tipleri ve varsayılanları TEK
    yerdedir. Kopyalansaydı Excel bir gün ekrandan BAŞKA bir kümeyi indirmeye
    başlar ve fark hiçbir testte görünmezdi.
    """

    q: Annotated[str | None, Query(description="Ad içinde kısmi arama (spec §3)")] = None
    source: Annotated[WorkerSource | None, Query()] = None
    subcontractor_id: Annotated[uuid.UUID | None, Query()] = None
    is_active: Annotated[bool | None, Query()] = None
    project_id: Annotated[uuid.UUID | None, Query()] = None
    is_draft: Annotated[bool | None, Query()] = None

    def as_kwargs(self) -> dict[str, object]:
        return {
            "q": self.q,
            "source": self.source,
            "subcontractor_id": self.subcontractor_id,
            "is_active": self.is_active,
            "project_id": self.project_id,
            "is_draft": self.is_draft,
        }


async def personnel_items(
    session: AsyncSession,
    filters: PersonnelFilters,
    *,
    limit: int | None,
    offset: int = 0,
    actor: User | None = None,
) -> list[PersonnelResponse]:
    """Ekranı da Excel'i de BESLEYEN tek okuma yolu (EXPORT-XLSX §1).

    `limit=None` süzgece uyan TÜM kayıtları getirir (`repository.list_personnel`
    docstring'i): dışa aktarma sessizce kırpılmaz.
    """
    visible = await visible_project_ids(session, actor) if actor is not None else None
    if visible is not None and filter_hidden(filters.project_id, visible):
        return []  # görünmeyen proje süzgeci: var/yok sızmaz (IZN-B5a)
    rows = await repository.list_personnel(
        session, limit=limit, offset=offset, **filters.as_kwargs()
    )
    items = [PersonnelResponse.model_validate(row) for row in rows]
    return items if visible is None else [hide_assignment(i, visible) for i in items]


async def _response_for(
    session: AsyncSession, user: User, personnel: Personnel
) -> PersonnelResponse:
    """Tek personel yanıtı: görünmeyen projeye atama `null` (IZN-B5a)."""
    return hide_assignment(
        PersonnelResponse.model_validate(personnel), await visible_project_ids(session, user)
    )


@router.get("/personnel", response_model=PersonnelListResponse, dependencies=[_VIEW])
async def list_personnel_endpoint(
    session: DbSession,
    user: Annotated[User, Depends(get_current_user)],
    filters: Annotated[PersonnelFilters, Depends()],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PersonnelListResponse:
    """`q` YALNIZ ada kısmi bakar (spec §3); süzgeçler AND'lidir.

    `is_active` GÖNDERİLMEZSE süzgeç uygulanmaz — pasif personel sessizce
    gizlenmez; ekran hangi kümeyi istediğini açıkça söyler. `project_id`
    (İK-1 §5 K4) `assigned_project_id`e göre DARALTIR — yetki genişletmez;
    `is_draft` taslakları ayıklamak için opsiyoneldir.

    Sayfa tavanı **200'de KALIR**: `limit=None` yalnız `export.xlsx` ucunun
    hakkıdır (orada sayfalama kavramı yoktur).
    """
    items = await personnel_items(session, filters, limit=limit, offset=offset, actor=user)
    hidden = filter_hidden(filters.project_id, await visible_project_ids(session, user))
    total = 0 if hidden else await repository.count_personnel(session, **filters.as_kwargs())
    return PersonnelListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/personnel/export.xlsx",
    dependencies=[_VIEW],
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Excel dosyasi"}},
)
async def personnel_export_endpoint(
    session: DbSession,
    user: Annotated[User, Depends(get_current_user)],
    filters: Annotated[PersonnelFilters, Depends()],
) -> Response:
    """PE "Dışa Aktar": kartoteksin Excel çıktısı — EKRANLA AYNI KÜME.

    Satırlar liste ucuyla AYNI çağrıdan (`personnel_items`) gelir: aynı süzgeç
    kümesi, aynı sıralama (`ORDER BY full_name`), aynı kapı (`_VIEW`). İkinci
    bir sorgu/süzgeç yolu AÇILMAZ.

    `limit`/`offset` YOKTUR — eşleşen TÜM kayıtlar yazılır (sessiz kırpma yok).
    Kapsam ekrandan geniş DEĞİLDİR; sütun kümesi de ekranı aşmaz (TCKN/IBAN/
    telefon/adres DOSYAYA GİRMEZ, bkz. `export.py` docstring'i).

    Okuma ucudur — `_audit` ÇAĞIRMAZ ve `Request` parametresi bile ALMAZ
    (`units/router.py` P4 T7 kuralı).
    """
    items = await personnel_items(session, filters, limit=None, actor=user)
    # IZN-B4c: rolün gizlediği hücre (SGK, ücret) `None` olur → hücre BOŞ kalır (`export._text`).
    masked = [await maskele_baglamli(item) for item in items]
    return Response(
        content=build_personnel_workbook(masked).getvalue(),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": http.content_disposition(export.filename())},
    )


@router.get(
    "/hr/documents/summary",
    response_model=HrDocumentsSummaryResponse,
    dependencies=[_VIEW],
)
async def hr_documents_summary_endpoint(
    session: DbSession,
    user: Annotated[User, Depends(get_current_user)],
) -> HrDocumentsSummaryResponse:
    """BT özet ucu: 5 KPI + belge tipi dağılımı + süresi-dolan/yaklaşan listeleri.

    Okuma (`view`) yeter — `personnel` şirket-geneli İK varlığıdır (liste ucu
    deseni). Sayılar yalnız AKTİF + YAYINDA personeli kapsar; durum türevi
    `status.py` tek kaynağından, sorgu sayısı veri büyüklüğünden bağımsızdır.
    """
    return await service.build_hr_documents_summary(session, actor=user)


@router.post(
    "/personnel",
    response_model=PersonnelResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL],
)
async def create_personnel_endpoint(
    request: Request,
    data: PersonnelCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PersonnelResponse:
    await reject_hidden_unique_fields(session, user, request, data)
    personnel = await service.create_personnel(session, data, actor=user)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.personnel_created(personnel.full_name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await _response_for(session, user, personnel)


@router.get("/personnel/{personnel_id}", response_model=PersonnelResponse, dependencies=[_VIEW])
async def get_personnel_endpoint(
    personnel_id: str,
    session: DbSession,
    user: Annotated[User, Depends(get_current_user)],
) -> PersonnelResponse:
    """URL-4 — yol parametresi UUID **ya da** ad slug'ı kabul eder.

    🔴 Bu modülde PROJE KAPSAMLI bir görünürlük süzgeci YOKTUR (ölçüldü:
    `service/core.py`de `visible_*` kapısı yok) — personel kartı şirket
    genelinde `personnel:view` iznine bağlıdır. Slug bu yüzden var olmayan bir
    süzgeci DELMEZ; izinsiz kullanıcı slug'la da UUID'yle de aynı 403'ü alır.
    """
    personnel = await service.get_personnel(session, parse_ref(personnel_id))
    return await _response_for(session, user, personnel)


@router.patch("/personnel/{personnel_id}", response_model=PersonnelResponse, dependencies=[_FULL])
async def update_personnel_endpoint(
    request: Request,
    personnel_id: uuid.UUID,
    data: PersonnelUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PersonnelResponse:
    """Pasifleştirme de BURADAN geçer (`{"is_active": false}`) — DELETE ucu yoktur."""
    personnel = await service.update_personnel(session, personnel_id, data, actor=user)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.personnel_updated(personnel.full_name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await _response_for(session, user, personnel)


# --- İK-1 T3: belge alt-kaynağı (spec §3) ------------------------------------
#
# Rota kökleri BİLİNÇLİ olarak İKİYE AYRILIR (`documents` deseni): liste/ekleme
# personele bağlıdır (`/personnel/{id}/documents`), güncelleme/silme belgenin
# kendi kimliğiyledir (`/personnel/documents/{doc_id}`) — belgeyi düzenlemek için
# personel kimliğini de taşımak gereksizdir ve iki kimlikli yol çelişki riski açar.


@router.get(
    "/personnel/{personnel_id}/documents",
    response_model=list[PersonnelDocumentResponse],
    dependencies=[_VIEW],
)
async def list_personnel_documents_endpoint(
    personnel_id: uuid.UUID,
    session: DbSession,
) -> list[PersonnelDocumentResponse]:
    """O personelin belgeleri (tip künyeli, N+1 yok). Personel yok → 404.

    `status`/`days_left` TÜREVdir (`status.py` tek kaynağı); GET denetlenmez.
    """
    return await service.list_personnel_documents(session, personnel_id)


@router.post(
    "/personnel/{personnel_id}/documents",
    response_model=PersonnelDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL],
)
async def create_personnel_document_endpoint(
    request: Request,
    personnel_id: uuid.UUID,
    data: PersonnelDocumentCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PersonnelDocumentResponse:
    """Belge kaydı. `type_id` XOR `free_label`; pasif tip → 422, yok → 404;

    görünmez/var olmayan BC belgesi (`document_id`) → 404 (IDOR korkuluğu).
    """
    response, detail = await service.create_personnel_document(session, user, personnel_id, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return response


@router.patch(
    "/personnel/documents/{document_id}",
    response_model=PersonnelDocumentResponse,
    dependencies=[_FULL],
)
async def update_personnel_document_endpoint(
    request: Request,
    document_id: uuid.UUID,
    data: PersonnelDocumentUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PersonnelDocumentResponse:
    """Kısmi güncelleme. Belge yok → 404; `document_id` değişimi aynı BC görünürlük
    denetiminden geçer."""
    response, detail = await service.update_personnel_document(session, user, document_id, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return response


@router.delete(
    "/personnel/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**DELETE_403_YANITI},
    dependencies=[require_system_admin()],
)
async def delete_personnel_document_endpoint(
    request: Request,
    document_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """İK takip kaydını (personel belgesi) siler. YALNIZ Sistem Yöneticisi.

    SET NULL: bağlı BC arşiv künyesi DURUR (dosya arşivde kalır). Yanıt 204, gövdesiz.
    """
    detail = await service.delete_personnel_document(session, document_id)
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


# --- İK-2 T2: izin talebi (spec §3, İZ mockup) -------------------------------
#
# Rota kökü BİLİNÇLİ olarak `/personnel/...` ALTINDA DEĞİLDİR: İZ ekranı talepleri
# ŞİRKET GENELİNDE listeler (personel seçmeden), yani liste ucunun doğal kimliği
# personel değildir. Personel bazlı görünüm `?personnel_id=` süzgecidir.
#
# **`/leave-types` SALT OKUMADIR** (spec §1): katalog CRUD'u AÇILMAZ, talep
# formunun tip listesine ihtiyacı olduğu için yalnız GET vardır.
#
# approve/reject uçları ve bakiye T3'ün işidir — BURADA YOKTUR.


@router.get(
    "/leave-types",
    response_model=list[LeaveTypeResponse],
    dependencies=[Depends(get_current_user)],
)
async def list_leave_types_endpoint(
    session: DbSession,
) -> list[LeaveTypeResponse]:
    """Aktif izin tipleri (`sort_order`). Yazma ucu YOKTUR — katalog ayarlar dilimidir.

    🔴 **Kapı İK-2.1'de `personnel=view`den KİMLİK DOĞRULAMASINA indirildi** ve
    bu, self-servis talep ucunun ÇALIŞABİLMESİ için zorunludur: matriste
    `personnel=none` olan `procurement` rolündeki bir çalışan kendi talebini
    açabiliyor ama tip listesini okuyamasaydı **formu dolduramazdı**.

    Veri sızıntısı DEĞİLDİR: bu uç bir **referans kataloğudur** — izin tipinin
    adı, rengi, sırası ve "yıllıktan düşer mi / belge ister mi" bayrakları. Ne
    kişi, ne kayıt, ne tutar, ne de proje bilgisi taşır; şirkete özgü hiçbir
    gizli değer yoktur ve satırları `leave_types` SEED'i belirler. Kapı yine de
    ANONİM DEĞİLDİR (`get_current_user`): dışarıya açılmadı, yalnız oturum açmış
    her role açıldı.

    Genişleme BURADA BİTER: `/personnel*`, `/leave-requests` (klasik liste),
    `approve`/`reject` ve bakiye uçlarının kapıları AYNEN durur."""
    types = await service.list_leave_types(session)
    return [LeaveTypeResponse.model_validate(t) for t in types]


@router.get("/leave-requests", response_model=LeaveRequestListResponse, dependencies=[_VIEW])
async def list_leave_requests_endpoint(
    session: DbSession,
    user: Annotated[User, Depends(get_current_user)],
    status_filter: Annotated[LeaveStatus | None, Query(alias="status")] = None,
    personnel_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LeaveRequestListResponse:
    """İZ talep tablosu. Süzgeçler AND'lidir; `project_id` PERSONELİN projesi
    üzerinden DARALTIR (talebin kendi proje kolonu yoktur).

    `limit` tavanı 200'dür (TB3 korkuluğu): tavanı aşan istek SESSİZCE KIRPILMAZ,
    422 olur — ekran eksik listeyi tam sanmasın.
    """
    if filter_hidden(project_id, await visible_project_ids(session, user)):
        return LeaveRequestListResponse(items=[], total=0, limit=limit, offset=offset)
    items, total = await service.list_leave_requests(
        session,
        status=status_filter,
        personnel_id=personnel_id,
        project_id=project_id,
        limit=limit,
        offset=offset,
    )
    return LeaveRequestListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post(
    "/leave-requests",
    response_model=LeaveRequestResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL],
)
async def create_leave_request_endpoint(
    request: Request,
    data: LeaveRequestCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> LeaveRequestResponse:
    """`days` SUNUCU hesabıdır ve `status` `pending` başlar (spec §5 K2); ikisi de
    gövdeden alınmaz — gönderilirse 422 (şema `extra="forbid"`).

    Personel yok → 404 · izin tipi yok → 404, pasif → 422 · ters tarih → 422 ·
    görünmez BC belgesi (`document_id`) → 404 (IDOR korkuluğu).
    """
    response, detail = await service.create_leave_request(session, user, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return response


# --- İK-2.1: self-servis izin talebi (kullanıcı kararı 2026-08-19) ----------
#
# 🔴 **Bu iki uç `/leave-requests/{request_id}` rotasından ÖNCE tanımlanmak
# ZORUNDADIR** (MK-2 rota sırası tuzağı): FastAPI ilk eşleşeni seçer, sonra
# tanımlansaydı "self" bir UUID sanılır ve 422 gelirdi.
#
# 🔴 **Kapıda `_VIEW`/`_FULL` YOKTUR ve bu bilinçlidir.** Yetkinin kaynağı
# `personnel` modül izni DEĞİL, kaydın SAHİPLİĞİdir: talep açan kişi kendi
# `user_id` köprüsüyle eşleşen TEK personel kaydına yazar. `personnel` kapısı
# konsaydı matriste `personnel=none` olan `procurement` rolündeki bir çalışan
# kendi iznini talep edemezdi — yüzeyin var oluş nedeni tam olarak budur.
# Genişleme buraya kadardır: onay/red/düzenleme/silme kapıları DEĞİŞMEDİ ve
# gövde `personnel_id` KABUL ETMEZ (başkasının adına talep yapısal olarak yok).


@router.get("/leave-requests/self", response_model=LeaveRequestListResponse)
async def list_self_leave_requests_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    status_filter: Annotated[LeaveStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LeaveRequestListResponse:
    """Aktörün KENDİ izin talepleri (K6).

    `personnel_id` süzgeci YOKTUR — sunucu koyar; bu uç başka bir personelin
    listesine çevrilemez. Bağlı personel kaydı yok → 404 · birden fazla → 409.
    """
    items, total = await service.list_self_leave_requests(
        session, user, status=status_filter, limit=limit, offset=offset
    )
    return LeaveRequestListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post(
    "/leave-requests/self",
    response_model=LeaveRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_self_leave_request_endpoint(
    request: Request,
    data: SelfLeaveRequestCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> LeaveRequestResponse:
    """Personelin KENDİ izin talebi — onay akışı DEĞİŞMEZ, İK'da kalır.

    Hedef personel gövdeden alınmaz, `user_id` köprüsünden çözülür; gövdeye
    `personnel_id` konması 422'dir ve cevap hedefin var olup olmadığına göre
    DEĞİŞMEZ. Bağlı kayıt yok → 404 (K3) · birden fazla kayıt → 409 (K4) ·
    izin tipi yok → 404, pasif → 422 · ters tarih → 422 · görünmez BC belgesi
    → 404.
    """
    response, detail = await service.create_self_leave_request(session, user, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return response


@router.get(
    "/leave-requests/{request_id}", response_model=LeaveRequestResponse, dependencies=[_VIEW]
)
async def get_leave_request_endpoint(
    request_id: uuid.UUID,
    session: DbSession,
) -> LeaveRequestResponse:
    return await service.get_leave_request(session, request_id)


@router.patch(
    "/leave-requests/{request_id}", response_model=LeaveRequestResponse, dependencies=[_FULL]
)
async def update_leave_request_endpoint(
    request: Request,
    request_id: uuid.UUID,
    data: LeaveRequestUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> LeaveRequestResponse:
    """YALNIZ `pending` kayıt düzenlenebilir (karara bağlanmış → 409). Tarih
    değişirse `days` YENİDEN sunucu hesabıdır."""
    response, detail = await service.update_leave_request(session, user, request_id, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return response


@router.delete(
    "/leave-requests/{request_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **DELETE_403_YANITI,
        409: {"description": "Yalnız bekleyen izin talebi silinebilir"},
    },
    dependencies=[require_system_admin()],
)
async def delete_leave_request_endpoint(
    request: Request,
    request_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Bekleyen izin talebini siler. YALNIZ Sistem Yöneticisi.

    Karara bağlanmış (onaylı/reddedilmiş) talep **409** (iş kuralı). Talebi açan kişi talebi
    SİLEMEZ; vazgeçmenin yolu silme değil GERİ ÇEKMEdir (`POST /leave-requests/{id}/withdraw`).
    """
    detail = await service.delete_leave_request(session, user, request_id)
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


# --- İK-2 T3: onay/red + bakiye (spec §3, §5 K4/K5) --------------------------
#
# **Onay TEK ADIMDIR** (K4): çok-aşamalı onay MOTORU açılmaz (SA onay-motoru
# kararının emsali). İZ 57'deki "şef → İK" akışı METİNdir; satır-içi tek ✓ tek
# onay adımıdır ve kapısı `personnel` **full+**tir.
#
# Uçlar POST'tur, PATCH DEĞİL: karar bir DURUM GEÇİŞİDİR (`pending -> approved`),
# alan güncellemesi değil. PATCH `/leave-requests/{id}` ile karıştırılmaları da
# tehlikeli olurdu — o uç yalnız `pending` kaydı düzenler ve karar alanlarını
# HİÇ kabul etmez.


@router.post(
    "/leave-requests/{request_id}/approve",
    response_model=LeaveRequestResponse,
    dependencies=[_LEAVE_APPROVE],
)
async def approve_leave_request_endpoint(
    request: Request,
    request_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    data: LeaveApproveRequest | None = None,
) -> LeaveRequestResponse:
    """Talebi onaylar (TEK adım). Karar alanları SUNUCU damgasıdır — gövde ALAN
    KABUL ETMEZ (gönderilirse 422).

    Talep yok → 404 · `pending` değil → 409 · 🔴 **KENDİ talebi → 403** (OK-1A T5,
    kullanıcı kararı 2026-08-21; TEK istisna `admin` ve o da denetime "vekâleten"
    işaretiyle geçer) · çakışan ONAYLI izin → 409 (K3) · hak aşımı → 409 (K5) ·
    **kalan hak hesaplanamıyor → 409** (🔴 fail-closed: kıdem 1 yılı doldurmadı
    ya da `hire_date` boş). RED bu kapılardan HİÇBİRİNDEN etkilenmez — 403 dâhil.
    """
    response, detail = await service.approve_leave_request(session, user, request_id)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return response


@router.post(
    "/leave-requests/{request_id}/reject",
    response_model=LeaveRequestResponse,
    dependencies=[_LEAVE_APPROVE],
)
async def reject_leave_request_endpoint(
    request: Request,
    request_id: uuid.UUID,
    data: LeaveRejectRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> LeaveRequestResponse:
    """Talebi reddeder — `reason` ZORUNLU (boş/boşluk → 422).

    **Red HER ZAMAN serbesttir:** hak aşımı ya da çakışma yüzünden onaylanamayan
    talep REDDEDİLEBİLİR (İZ 98-99: ✓ pasif, ✗ aktif). Talep yok → 404 ·
    `pending` değil → 409.
    """
    response, detail = await service.reject_leave_request(session, user, request_id, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return response


@router.post("/leave-requests/{request_id}/withdraw", response_model=LeaveRequestResponse)
async def withdraw_leave_request_endpoint(
    request: Request,
    request_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> LeaveRequestResponse:
    """Talebi SAHİBİ geri çeker (İK-2.2, kullanıcı kararı 2026-08-22).

    🔴 **Kapıda `_VIEW`/`_FULL` YOKTUR ve bu ZORUNLUDUR** — `/leave-requests/self`
    ile aynı gerekçe: yetkinin kaynağı `personnel` modül izni DEĞİL kaydın
    SAHİPLİĞİdir. Matriste `personnel=none` olan `procurement` rolündeki bir
    çalışan kendi talebini AÇABİLİYOR; `_VIEW` konsaydı onu GERİ ÇEKEMEZ ve bu
    dilim hiçbir şey çözmezdi.

    🔴 **`admin` istisnası YOKTUR**: admin başkasının talebini geri çekemez (404).
    Vazgeçme yetki yükseltmesi değildir — admin zaten `reject`/`DELETE` edebilir.

    Talep yok **ya da sahibi değilsin** → 404 (ayırt edilemez) · `pending` değil
    → 409. Kayıt SİLİNMEZ: `pending -> withdrawn` durum geçişidir.
    """
    response, detail = await service.withdraw_leave_request(session, user, request_id)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return response


# `year` sınırı: bakiye takvim yılıdır ve serbest bir tam sayı olarak bırakılırsa
# `9999` gibi bir değer anlamsız bir kıdem penceresi hesaplatırdı. Aralık İZ'in
# gerçekçi kullanım ömrüdür.
_YEAR_PATH = Path(ge=2000, le=2100)


@router.get(
    "/leave-balances/{personnel_id}/{year}",
    response_model=LeaveBalanceResponse,
    dependencies=[_VIEW],
)
async def get_leave_balance_endpoint(
    personnel_id: uuid.UUID,
    session: DbSession,
    year: Annotated[int, _YEAR_PATH],
) -> LeaveBalanceResponse:
    """İZ bakiye satırı: hak / devreden / kullanılan / kalan / kullanım yüzdesi.

    Hepsi TÜREVdir (`annual_entitlement` KOLON DEĞİL, spec §5 K1). Bakiye SATIRI
    olmayan personel için de 200 döner (devreden 0) — satır yalnız MANUEL devreden
    içindir, yokluğu veri eksikliği değildir. Personel yok → 404.

    Hak/kalan/yüzde **null** olabilir: kıdem 1 yılı doldurmadıysa ya da `hire_date`
    boşsa hak hesaplanamaz (İZ 163 "1 yıl dolunca hak kazanır") — ekran 0 değil
    "Hak yok" basar.
    """
    return await service.get_leave_balance(session, personnel_id, year)


@router.put(
    "/leave-balances/{personnel_id}/{year}",
    response_model=LeaveBalanceResponse,
    dependencies=[_FULL],
)
async def upsert_leave_balance_endpoint(
    request: Request,
    personnel_id: uuid.UUID,
    data: LeaveBalanceUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: Annotated[int, _YEAR_PATH],
) -> LeaveBalanceResponse:
    """Devreden günü yazar (UPSERT) — YALNIZ `carried_over` (İZ 137).

    Türev alan (`annual_entitlement`/`used`/`remaining`) gönderilirse 422: hiçbiri
    kolon değildir ve sessizce yutulsalardı istemci hakkı değiştirdiğini sanırdı.
    Personel yok → 404. Aynı isteği iki kez göndermek ikinci satır AÇMAZ.
    """
    response, detail = await service.upsert_leave_balance(session, personnel_id, year, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return response


# `?year=` süzgeci bakiye ucunun `_YEAR_PATH` sınırının BİREBİR aynısıdır (İZ 120
# yıl seçici): iki uç aynı yıl penceresini anlatır, sınırlarının ayrışması
# ekranda seçilebilen ama özette 422 dönen bir yıl bırakırdı.
_YEAR_QUERY = Query(ge=2000, le=2100)


@router.get(
    "/hr/leaves/summary",
    response_model=HrLeavesSummaryResponse,
    dependencies=[_VIEW],
)
async def hr_leaves_summary_endpoint(
    session: DbSession,
    year: Annotated[int | None, _YEAR_QUERY] = None,
) -> HrLeavesSummaryResponse:
    """İZ özet ucu: 5 KPI + personel bazlı izin bakiyesi tablosu.

    Okuma (`view`) yeter — `personnel` şirket-geneli İK varlığıdır (liste ucu ve
    `/hr/documents/summary` deseni); proje görünürlüğü süzgeci UYGULANMAZ.

    `year` verilmezse içinde bulunulan yıl. Yıl YALNIZ bakiye eksenini kaydırır:
    "Bekleyen Talep"/"Bugün İzinli"/"Bu Ay Kullanılan" BUGÜNE bağlıdır.

    Hak/kalan/yüzde **null** olabilir (kıdem<1 ya da `hire_date` yok, İZ 163) ve
    bu satırlar borç toplamına 0 olarak karışmaz — `unknown_entitlement_personnel`
    ile AÇIKÇA sayılır (🔴 fail-closed). Sorgu sayısı veri büyüklüğünden bağımsızdır.
    """
    return await service.build_hr_leaves_summary(session, year=year)

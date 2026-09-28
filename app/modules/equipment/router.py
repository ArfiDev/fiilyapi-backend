"""Makine & ekipman uçları (MK-1 spec §4).

Kapı `equipment` iznidir (21. modül, spec §6): okuma `view`, yazmanın tamamı
`full`. Görünmeyen kayıt 404'tür.

`inventory`/`documents` router'larının aksine bu router PREFIX TAŞIR (`/equipment`):
spec §4'ün SAYDIĞI uçların hepsi tek kökün altındadır (`/equipment`,
`/equipment/summary`, `/equipment/work-logs`, `/equipment/work-summary`,
`/equipment/fuel-logs`, `/equipment/fuel-summary`) — ikinci bir kök yoktur.

## AÇILMAYAN uç (spec §4, icat yasağı)

**`DELETE /equipment/{id}` YOKTUR.** Kullanımdan kaldırma
`PATCH {"is_active": false}` iledir; kaydı olan ekipman zaten `RESTRICT`
yüzünden DB seviyesinde de silinemez. Yol tanımlı olmadığı için FastAPI 405
döner ve bu bir BEKÇİ TESTİYLE kilitlenmiştir (`test_silme_ucu_yoktur_405`).
Kira hakedişi (M5) ve ekipman belgeleri MK-2'nindir (spec §9) — bu router'da
HİÇBİRİ açılmaz.

**`DELETE /equipment/work-logs/{id}` ve `DELETE /equipment/fuel-logs/{id}` ise
VARDIR** ve bu bir çelişki değildir: çalışma/yakıt kaydı MALİ İZ DEĞİLDİR —
maliyet/`amount` her okumada TÜREVDİR (K18 · yakıt eşi `cost.fuel_amount`) ve
kayıt hatası düzeltilebilmelidir. Silinemeyen şey ekipmanın KENDİSİDİR.

`GET` uçları `record_audit` ÇAĞIRMAZ (WORKFLOW kuralı — okumalar denetlenmez);
yazma uçlarının hepsi tek denetim satırı yazar ve metin servis katmanında
kurulur.

## Yol SIRASI önemlidir

`/equipment/summary`, `/equipment/work-logs`, `/equipment/work-summary`,
`/equipment/fuel-logs` ve `/equipment/fuel-summary` `/equipment/{equipment_id}`den
ÖNCE tanımlanır: sonra gelselerdi FastAPI onları birer UUID sanıp yolu 422'ye
düşürürdü. `/equipment/work-logs/{log_id}` ve `/equipment/fuel-logs/{log_id}` de
aynı sebeple `/{equipment_id}`nin üstündedir.
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import http
from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.core.ratelimit import client_ip
from app.core.slug import parse_ref
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.equipment import detail_service, service, work_summary_export
from app.modules.equipment.detail_schemas import EquipmentDetailResponse
from app.modules.equipment.models import (
    EquipmentCategory,
    EquipmentOwnership,
    EquipmentStatus,
    WorkLogType,
)
from app.modules.equipment.schemas import (
    EquipmentCreate,
    EquipmentListResponse,
    EquipmentResponse,
    EquipmentSummaryResponse,
    EquipmentUpdate,
    FuelLogCreate,
    FuelLogListResponse,
    FuelLogResponse,
    FuelLogUpdate,
    FuelSummaryResponse,
    WorkLogCreate,
    WorkLogListResponse,
    WorkLogResponse,
    WorkLogUpdate,
    WorkSummaryResponse,
)
from app.modules.users.models import User

router = APIRouter(prefix="/equipment", tags=["equipment"], responses=COMMON_ERROR_RESPONSES)

_VIEW = require_permission(service.PERMISSION_MODULE, AccessLevel.view)
_FULL = require_permission(service.PERMISSION_MODULE, AccessLevel.full)


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


@router.get("", response_model=EquipmentListResponse, dependencies=[_VIEW])
async def list_equipment_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    status_filter: Annotated[EquipmentStatus | None, Query(alias="status")] = None,
    category: EquipmentCategory | None = None,
    site_id: uuid.UUID | None = None,
    ownership: EquipmentOwnership | None = None,
    q: str | None = None,
    is_active: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EquipmentListResponse:
    """M1 kart listesi — süzgeçler AND'lidir, kapsam (K20) HER ZAMAN üsttedir.

    `q` ad + marka + model + plaka + seri üzerinde kısmi arar: M1 kartı bu
    alanları üst üste basar ve tek alanda aramak kullanıcıyı "yok" sanısına
    düşürürdü.

    `is_active` spec §4'ün SAYDIĞI süzgeçlerden değildir ama listenin varsayılanı
    "hepsi"dir; pasifleri ayıklamak isteyen ekran onu açıkça verir. Varsayılan
    `false` yapılsaydı hurdaya ayrılan makine hiçbir listede bulunamazdı.
    """
    items, total = await service.list_equipment(
        session,
        user,
        status=status_filter,
        category=category,
        site_id=site_id,
        ownership=ownership,
        q=q,
        is_active=is_active,
        limit=limit,
        offset=offset,
    )
    return EquipmentListResponse(
        items=[EquipmentResponse.model_validate(e) for e in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/summary", response_model=EquipmentSummaryResponse, dependencies=[_VIEW])
async def equipment_summary_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> EquipmentSummaryResponse:
    """M1 KPI'ları: DÖRT durum sayacı (K21) + cari ay çalışma maliyeti.

    Sayaçlar ve maliyet AYNI kapsam süzgecinden geçer — sayaç sızıntısı da bir
    sızıntıdır (görünmeyen projenin filo büyüklüğünü ele verir).
    """
    return EquipmentSummaryResponse.model_validate(await service.summarize(session, user))


@router.post(
    "",
    response_model=EquipmentResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Seçilen şantiye, operatör ya da tedarikçi bulunamadı"},
        422: {"description": "Sahip olunan ekipmanda alış bedeli zorunludur (K2)"},
    },
    dependencies=[_FULL],
)
async def create_equipment_endpoint(
    request: Request,
    data: EquipmentCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> EquipmentResponse:
    """M2 formunun kaydı. `site_id` verilmezse makine DEPODADIR (K4)."""
    equipment, detail = await service.create_equipment(session, user, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return EquipmentResponse.model_validate(equipment)


@router.get("/work-logs", response_model=WorkLogListResponse, dependencies=[_VIEW])
async def list_work_logs_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    equipment_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    record_type: WorkLogType | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WorkLogListResponse:
    """M3 "Son Kayıtlar" listesi — EN YENİ önce.

    `site_id` süzgeci KAYDIN kendi şantiyesine bakar (K9), makinenin bugünkü
    atamasına değil.
    """
    items, total = await service.list_work_logs(
        session,
        user,
        equipment_id=equipment_id,
        site_id=site_id,
        date_from=date_from,
        date_to=date_to,
        record_type=record_type,
        limit=limit,
        offset=offset,
    )
    return WorkLogListResponse(
        items=[WorkLogResponse.model_validate(k) for k in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/work-summary", response_model=WorkSummaryResponse, dependencies=[_VIEW])
async def work_summary_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: Annotated[int, Query(ge=2000, le=2200)],
    month: Annotated[int, Query(ge=1, le=12)],
    site_id: uuid.UUID | None = None,
) -> WorkSummaryResponse:
    """M3 ana tablosu + tfoot + haftalık mini grafik.

    🔴 Toplamlar HER ZAMAN satırlardan türer (K15); mockup'ın tfoot'u kendi
    satırlarıyla tutarsızdır ve kopyalanmaz.
    """
    return await service.work_summary(session, user, year=year, month=month, site_id=site_id)


@router.get(
    "/work-summary/export.xlsx",
    dependencies=[_VIEW],
    response_class=Response,
    responses={
        200: {
            "content": {work_summary_export.XLSX_MEDIA_TYPE: {}},
            "description": "Excel dosyasi",
        }
    },
)
async def work_summary_export_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: Annotated[int, Query(ge=2000, le=2200)],
    month: Annotated[int, Query(ge=1, le=12)],
    site_id: uuid.UUID | None = None,
) -> Response:
    """M3 "Excel İndir": çalışma kaydının Excel çıktısı — EKRANLA AYNI KÜME.

    Zarf ekran ucuyla AYNI çağrıdan (`service.work_summary`) gelir: aynı
    görünürlük kapsamı (`_visible_project_ids`), aynı ay sınırları, aynı
    süzgeç, aynı toplam kaynağı. İkinci bir sorgu/hesap yolu AÇILMAZ.

    Yüzey ZATEN sayfalanmamıştır — kırpma kavramı yoktur, `limit` almaz.

    Okuma ucudur — `_audit` ÇAĞIRMAZ ve `Request` parametresi bile ALMAZ
    (`units/router.py` P4 T7 kuralı).
    """
    ozet = await service.work_summary(session, user, year=year, month=month, site_id=site_id)
    return Response(
        content=work_summary_export.build_work_summary_workbook(ozet).getvalue(),
        media_type=work_summary_export.XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": http.content_disposition(
                work_summary_export.filename(year, month)
            )
        },
    )


@router.post(
    "/work-logs",
    response_model=WorkLogResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Ekipman, şantiye ya da operatör bulunamadı (görünmeyen dahil)"},
        422: {"description": "K11 saat kuralları ya da K12 günlük 24 saat tavanı"},
    },
    dependencies=[_FULL],
)
async def create_work_log_endpoint(
    request: Request,
    data: WorkLogCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> WorkLogResponse:
    """M3 kaydı. `hours` SUNUCU hesabıdır (K11); günlük tavan KİLİTLİDİR (K12)."""
    log, detail = await service.create_work_log(session, user, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return WorkLogResponse.model_validate(log)


@router.get("/work-logs/{log_id}", response_model=WorkLogResponse, dependencies=[_VIEW])
async def get_work_log_endpoint(
    log_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> WorkLogResponse:
    """Görünmeyen kayıt var olmayanla AYNI 404'ü döner."""
    return WorkLogResponse.model_validate(await service.visible_work_log(session, user, log_id))


@router.patch(
    "/work-logs/{log_id}",
    response_model=WorkLogResponse,
    responses={422: {"description": "K11 saat kuralları ya da K12 günlük 24 saat tavanı"}},
    dependencies=[_FULL],
)
async def update_work_log_endpoint(
    request: Request,
    log_id: uuid.UUID,
    data: WorkLogUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> WorkLogResponse:
    """Kayıt hatası düzeltilebilir; K11/K12 BİRLEŞİK değerler üzerinde koşar."""
    log = await service.visible_work_log(session, user, log_id)
    log, detail = await service.update_work_log(session, user, log, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return WorkLogResponse.model_validate(log)


@router.delete("/work-logs/{log_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_FULL])
async def delete_work_log_endpoint(
    request: Request,
    log_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> Response:
    """🔴 Çalışma kaydı MALİ İZ DEĞİLDİR (maliyet ondan türev) — silinebilir.

    Ekipmanın KENDİSİ silinemez: orada iz `RESTRICT`lidir ve DELETE ucu yoktur.
    """
    detail = await service.delete_work_log(session, user, log_id)
    await _audit(request, session, user, AuditAction.delete, detail)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/fuel-logs", response_model=FuelLogListResponse, dependencies=[_VIEW])
async def list_fuel_logs_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    equipment_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FuelLogListResponse:
    """M4 kayıt listesi — `site_id` süzgeci KAYDIN kendi şantiyesine bakar (K4)."""
    items, total = await service.list_fuel_logs(
        session,
        user,
        equipment_id=equipment_id,
        site_id=site_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return FuelLogListResponse(
        items=[FuelLogResponse.model_validate(k) for k in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/fuel-summary", response_model=FuelSummaryResponse, dependencies=[_VIEW])
async def fuel_summary_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: Annotated[int, Query(ge=2000, le=2200)],
    month: Annotated[int, Query(ge=1, le=12)],
    equipment_id: uuid.UUID | None = None,
) -> FuelSummaryResponse:
    """M4 üst blok + tablo.

    🔴 `lt_per_hour_avg` paydası dönemin ÇALIŞMA KAYDI saat toplamıdır
    (modüller arası bağ, M4:39); rozet (`consumption_status`) SUNUCUDAN gelir.
    """
    return await service.fuel_summary(
        session, user, year=year, month=month, equipment_id=equipment_id
    )


@router.post(
    "/fuel-logs",
    response_model=FuelLogResponse,
    status_code=status.HTTP_201_CREATED,
    responses={404: {"description": "Ekipman ya da şantiye bulunamadı (görünmeyen dahil)"}},
    dependencies=[_FULL],
)
async def create_fuel_log_endpoint(
    request: Request,
    data: FuelLogCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> FuelLogResponse:
    """M4 kaydı. `entered_by_id` oturum kullanıcısından DAMGALANIR (K14)."""
    log, detail = await service.create_fuel_log(session, user, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return FuelLogResponse.model_validate(log)


@router.get("/fuel-logs/{log_id}", response_model=FuelLogResponse, dependencies=[_VIEW])
async def get_fuel_log_endpoint(
    log_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> FuelLogResponse:
    """Görünmeyen kayıt var olmayanla AYNI 404'ü döner."""
    return FuelLogResponse.model_validate(await service.visible_fuel_log(session, user, log_id))


@router.patch("/fuel-logs/{log_id}", response_model=FuelLogResponse, dependencies=[_FULL])
async def update_fuel_log_endpoint(
    request: Request,
    log_id: uuid.UUID,
    data: FuelLogUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> FuelLogResponse:
    """Kayıt hatası düzeltilebilir."""
    log = await service.visible_fuel_log(session, user, log_id)
    log, detail = await service.update_fuel_log(session, user, log, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return FuelLogResponse.model_validate(log)


@router.delete("/fuel-logs/{log_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_FULL])
async def delete_fuel_log_endpoint(
    request: Request,
    log_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> Response:
    """🔴 Yakıt kaydı MALİ İZ DEĞİLDİR (maliyet ondan türev) — silinebilir."""
    detail = await service.delete_fuel_log(session, user, log_id)
    await _audit(request, session, user, AuditAction.delete, detail)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{equipment_id}", response_model=EquipmentResponse, dependencies=[_VIEW])
async def get_equipment_endpoint(
    equipment_id: str,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> EquipmentResponse:
    """Görünmeyen kayıt var olmayanla AYNI 404'ü döner (spec §4).

    URL-4 — yol parametresi UUID **ya da** ad slug'ı kabul eder
    (`/makine/beko-loder`). Yol adı `equipment_id` KALIR (URL-2 kararı 1);
    PATCH ucu `uuid.UUID` KALIR (kararı 3).
    """
    return EquipmentResponse.model_validate(
        await service.visible_equipment(session, user, parse_ref(equipment_id))
    )


@router.get(
    "/{equipment_id}/detail",
    response_model=EquipmentDetailResponse,
    dependencies=[_VIEW],
)
async def get_equipment_detail_endpoint(
    equipment_id: str,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    as_of: date | None = None,
) -> EquipmentDetailResponse:
    """MK-4 — Ekipman Detay ekranının künye + İKİ TÜREV bloğu.

    🔴 `GET /{equipment_id}` DEĞİŞMEDİ: türevler oraya konsaydı LİSTE ucu da
    (aynı şemayı paylaşır) her çizilişte hareket tablosunu tarardı. Sözleşme
    ADDITIVE genişler.

    `as_of` **tahmini bakım tarihinin dayanak günüdür** ve verilmezse TR
    takviminde bugündür (`core.timezone.today`). Parametre olması, yanıtın
    hangi güne göre üretildiğini istemcinin de testin de SABİTLEYEBİLMESİ
    içindir — sunucunun kendi günü hesabın derinine gömülseydi ikisi de
    imkânsızdı.

    Görünmeyen kayıt var olmayanla AYNI 404'ü döner (K20).
    """
    return await detail_service.equipment_detail(
        session, user, parse_ref(equipment_id), as_of=as_of
    )


@router.patch(
    "/{equipment_id}",
    response_model=EquipmentResponse,
    responses={422: {"description": "Sahip olunan ekipmanda alış bedeli zorunludur (K2)"}},
    dependencies=[_FULL],
)
async def update_equipment_endpoint(
    request: Request,
    equipment_id: uuid.UUID,
    data: EquipmentUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> EquipmentResponse:
    """Kısmi güncelleme. **Kullanımdan kaldırma da buradan geçer**
    (`{"is_active": false}`) — DELETE ucu yoktur (modül docstring'i).

    K2 burada da koşar ve MEVCUT SATIR + GÖVDE birleşimine bakar."""
    equipment = await service.visible_equipment(session, user, equipment_id)
    equipment, detail = await service.update_equipment(session, equipment, user, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return EquipmentResponse.model_validate(equipment)

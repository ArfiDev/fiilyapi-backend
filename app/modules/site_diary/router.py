"""Şantiye günlüğü uçları (T2) — oluşturma / liste / detay / PATCH / DELETE.

Kapılar `site_diary` iznidir (seed'de HAZIR, matris DEĞİŞMEZ): okuma `view`,
yazma `full`. Bu ayrım PM'i (matriste `site_diary=_V`) SALT OKUR yapar — yazma
uçlarında 403 alır; şef ve saha mühendisi (`_F`) tam yetkilidir.

Denetim günlüğü (`record_audit`) TÜM yazma uçlarına bağlıdır; mesajlar
`app/modules/audit/messages.py`de merkezîdir.

T4'te eklenenler: `GET …/diary/summary` (aşağıda) ve durum aksiyonları
(`submit`/`reopen` — `router_transitions.py`de, dosya sonunda `include_router`
ile BAĞLANIR; modül dışına tek bir router çıkar).

Kapsam DIŞI: `diary-suggestion` (T5).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.errors import SiteValidationError
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.core.ratelimit import client_ip
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.site_diary import (
    guards,
    read,
    router_suggestion,
    router_transitions,
    service,
    summary,
)
from app.modules.site_diary.schemas import (
    SiteDiaryEntryCreate,
    SiteDiaryEntryDetail,
    SiteDiaryEntryListResponse,
    SiteDiaryEntryUpdate,
    SiteDiaryLinesSave,
    SiteDiarySummary,
)
from app.modules.users.models import User

router = APIRouter(tags=["site-diary"], responses=COMMON_ERROR_RESPONSES)

_VIEW = require_permission(service.PERMISSION_MODULE, AccessLevel.view)
_FULL = require_permission(service.PERMISSION_MODULE, AccessLevel.full)


@router.get(
    "/sites/{site_id}/diary",
    response_model=SiteDiaryEntryListResponse,
    dependencies=[_VIEW],
)
async def list_site_diary_entries_endpoint(
    site_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: Annotated[int | None, Query(ge=guards.MIN_YEAR, le=guards.MAX_YEAR)] = None,
    month: Annotated[int | None, Query(ge=1, le=12)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    section_id: Annotated[uuid.UUID | None, Query()] = None,
) -> SiteDiaryEntryListResponse:
    """GK "Son Kayıtlar" — durum, işçi toplamı ve satır ₺ toplamı TÜREVDİR.

    DET-1.B `section_id`: Bölüm Detay › Günlük Kayıt — Kural A (başlığı bu bölüm OLAN gün ∪
    bu bölüme miktar satırı yazılmış gün; kayıt tekrarlanmaz). `total` süzülmüş kümedir;
    başka şantiyenin / olmayan bölüm 422. Verilmezse davranış aynen.

    `month` YALNIZ `year` ile anlamlıdır ("her yılın temmuzu" bir dönem
    değildir); tek başına gönderilirse 422 — sessizce yok saymak, kullanıcının
    filtrelediğini sandığı bir listeyi filtresiz göstermek olurdu.
    """
    if month is not None and year is None:
        raise SiteValidationError(guards.YEAR_REQUIRED_FOR_MONTH)
    return await read.list_entries(
        session,
        user,
        site_id,
        year=year,
        month=month,
        limit=limit,
        offset=offset,
        section_id=section_id,
    )


@router.get(
    "/sites/{site_id}/diary/summary",
    response_model=SiteDiarySummary,
    dependencies=[_VIEW],
)
async def get_site_diary_summary_endpoint(
    site_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: Annotated[int | None, Query(ge=guards.MIN_YEAR, le=guards.MAX_YEAR)] = None,
    month: Annotated[int | None, Query(ge=1, le=12)] = None,
) -> SiteDiarySummary:
    """Hakediş Özeti ekranının veri kaynağı — YALNIZ `submitted` günler (spec §3).

    Dönem kuralı liste ucuyla BİREBİR AYNIDIR (`month` tek başına 422): iki uç
    farklı süzgeç kabul etseydi aynı ekranın iki bölümü farklı dönemi gösterirdi.
    Kesin kararlar `summary.get_summary`dedir.
    """
    if month is not None and year is None:
        raise SiteValidationError(guards.YEAR_REQUIRED_FOR_MONTH)
    return await summary.get_summary(session, user, site_id, year=year, month=month)


@router.get("/diary/{entry_id}", response_model=SiteDiaryEntryDetail, dependencies=[_VIEW])
async def get_site_diary_entry_endpoint(
    entry_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    section_id: Annotated[uuid.UUID | None, Query()] = None,
) -> SiteDiaryEntryDetail:
    """DET-1.B `section_id`: `prev_id`/`next_id` bu bölümün Kural A kümesinde; verilmezse
    şantiye bağlamında. Başka şantiyenin / olmayan bölüm 422."""
    return await read.get_detail(session, user, entry_id, section_id=section_id)


@router.post(
    "/sites/{site_id}/diary",
    response_model=SiteDiaryEntryDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL],
)
async def create_site_diary_entry_endpoint(
    request: Request,
    site_id: uuid.UUID,
    data: SiteDiaryEntryCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SiteDiaryEntryDetail:
    """Satır iskeleti şantiyenin BOQ pozlarından OTOMATİK üretilir; gövdede satır YOK.

    Aynı şantiye + aynı gün için ikinci kayıt 409'dur (UQ ön kontrolü, net mesaj).
    Yanıt `read.build_detail`den gelir — `get_detail` çağrılsaydı kapsam sorgusu
    istek başına İKİ KEZ koşardı.
    """
    context = await service.create(session, user, site_id, data)
    await record_audit(
        session,
        action=AuditAction.create,
        detail=messages.site_diary_entry_created(
            context.project.name,
            context.site.name,
            context.entry.entry_date,
            len(context.entry.lines),
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await read.build_detail(session, context)


@router.patch("/diary/{entry_id}", response_model=SiteDiaryEntryDetail, dependencies=[_FULL])
async def update_site_diary_entry_endpoint(
    request: Request,
    entry_id: uuid.UUID,
    data: SiteDiaryEntryUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SiteDiaryEntryDetail:
    """Yalnız `status=draft`; gönderilmiş kayda YAZMA YASAK (409). Kesin karar
    `service.update`tedir — kural burada TEKRARLANMAZ."""
    context = await service.update(session, user, entry_id, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.site_diary_entry_updated(
            context.project.name, context.site.name, context.entry.entry_date
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await read.build_detail(session, context)


@router.put("/diary/{entry_id}/lines", response_model=SiteDiaryEntryDetail, dependencies=[_FULL])
async def save_site_diary_lines_endpoint(
    request: Request,
    entry_id: uuid.UUID,
    data: SiteDiaryLinesSave,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SiteDiaryEntryDetail:
    """GK'nin miktar girişi — **DEĞİŞTİRME** semantiği.

    ⚠️ Gövdede geçmeyen satır SİLİNİR. Yalnız `status=draft` (409); poz sahipliği
    (poz günlüğün ŞANTİYESİNİN BOQ'suna ait olmalı) her yazımda koşar. Snapshot
    dörtlüsü gövdede YOKTUR — fiyat BOQ'dan gelir, istemciden değil.

    Pozu silinmiş satırlar gövdeden adreslenemediği için düşer; sayıları yanıtın
    `dropped_orphan_count` alanında BİLDİRİLİR (sessiz atlama yok). Kesin
    kararlar `service.save_lines` + `lines.apply_lines`tadır.
    """
    context, dropped_orphan_count = await service.save_lines(session, user, entry_id, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.site_diary_lines_saved(
            context.project.name,
            context.site.name,
            context.entry.entry_date,
            len(context.entry.lines),
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    detail = await read.build_detail(session, context)
    return detail.model_copy(update={"dropped_orphan_count": dropped_orphan_count})


@router.delete("/diary/{entry_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_FULL])
async def delete_site_diary_entry_endpoint(
    request: Request,
    entry_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Kapı `_FULL`dur, `_ADMIN` DEĞİL (taşeron silme ucunun aynı gerekçesi):
    admin kapısı olsaydı taslağı üreten şef/saha rollerinin KENDİ taslağını
    silme istisnası (`can_delete`) ölü kural olurdu. Kesin karar
    `service.delete_entry`tedir."""
    summary = await service.delete_entry(session, user, entry_id)
    await record_audit(
        session,
        action=AuditAction.delete,
        detail=messages.site_diary_entry_deleted(
            summary.project_name,
            summary.site_name,
            summary.entry_date,
            summary.status_label,
            summary.line_count,
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


# Durum aksiyonları (T4) ve hakediş öneri uçları (T5) AYRI dosyalardadır ama
# modül dışına TEK router çıkar — `app/main.py` yalnız bu nesneyi bağlar
# (taşeron `router.py` deseninin aynısı).
router.include_router(router_transitions.router)
router.include_router(router_suggestion.router)

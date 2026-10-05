"""Şantiye planlama uçları (T2 okuma + T3 yazma + T4 gün özeti).

Kapı **`site_diary`** iznidir (spec §6 S1; yeni izin modülü AÇILMAZ): okuma
`view`, yazma (T3) `full`. Bu ayrım proje müdürünü (matriste `site_diary=_V`)
SALT OKUR yapar — planı görür, T3'ün `PUT`larında 403 alır.

`GET` `record_audit` ÇAĞIRMAZ (WORKFLOW kuralı — okumalar denetlenmez); dört
`PUT`un dördü de uç başına TEK dönem-özeti olayı yazar.

Dört yazma ucunun dördü de **DEĞİŞTİRME** semantiğindedir: mockup'ta tek "Kaydet"
düğmesi vardır (P97), taslak/onay akışı YOKTUR (spec §3). Kesin kararlar
`write.py`dedir ve burada TEKRARLANMAZ.

T4 gün özeti de bir OKUMADIR: `view` yeter, audit yazmaz. Kapsam DIŞI: malzeme
planı (spec §5) · GK bloğunun yazma karşılığı (spec §4 ONAYLI SAPMA).
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.core.ratelimit import client_ip
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.site_planning import read, service, write
from app.modules.site_planning.schemas import (
    SitePlanCellsSave,
    SitePlanDaySummaryRange,
    SitePlanGoalsSave,
    SitePlanRowSaved,
    SitePlanRowsResult,
    SitePlanRowsSave,
    SitePlanSprintRead,
    SitePlanSprintSave,
    SitePlanWeek,
)
from app.modules.users.models import User

router = APIRouter(
    route_class=MaskeRotasi, tags=["site-planning"], responses=COMMON_ERROR_RESPONSES
)

_VIEW = require_permission(service.PERMISSION_MODULE, AccessLevel.view)
_FULL = require_permission(service.PERMISSION_MODULE, AccessLevel.full)

# Hafta ZORUNLUDUR (puantajın `year`/`month`u gibi): haftasız bir ızgaranın sütun
# iskeleti bile yoktur. `assert_week_start` (Pazartesi şartı) hem okuma hem yazma
# yolunda KAPSAM KARARINDAN ÖNCE koşar — kaydırılmış bir hafta, kullanıcının
# görmediği bir haftayı DEĞİŞTİRME semantiğiyle süpürürdü.
_WEEK_START = Annotated[date, Query()]


@router.get("/sites/{site_id}/plan", response_model=SitePlanWeek, dependencies=[_VIEW])
async def get_site_plan_endpoint(
    site_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    week_start: Annotated[date, Query()],
) -> SitePlanWeek:
    """P (Planlama) ızgarasının bir haftası: gruplar + hücreler + hedefler + sprint.

    `week_start` ZORUNLUDUR (puantajın `year`/`month`u gibi): haftasız bir
    ızgaranın sütun iskeleti bile yoktur. Pazartesi değilse 422 — sessiz kaydırma
    ekranın başka bir haftayı gösterdiğini fark etmesini engellerdi.
    """
    return await read.get_week(session, user, site_id, week_start)


@router.get(
    "/sites/{site_id}/plan/day-summary",
    response_model=SitePlanDaySummaryRange,
    dependencies=[_VIEW],
)
async def get_site_plan_day_summary_endpoint(
    site_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    start: Annotated[date, Query()],
    days: Annotated[int, Query(ge=1, le=service.MAX_SUMMARY_DAYS)] = 5,
) -> SitePlanDaySummaryRange:
    """F-SD'nin GK gömülü "Önümüzdeki 5 Gün" bloğu (mockup 321-348) — SALT OKUNUR.

    Izgaradan TÜRETİLİR (spec §4): gün başına hücre metinlerinin birleşimi +
    `crew` satırlarının işçi toplamı + bölüm etiketleri. Planı olmayan gün
    pencereden DÜŞMEZ, `has_plan: false` ile işaretlenir — blok beş kutu çizer.

    ⚠️ Bu ucun YAZMA karşılığı YOKTUR ve açılmayacaktır (spec §4 ONAYLI SAPMA):
    mockup'taki textarea/number/select girişleri basılmaz, ızgara TEK kaynaktır.

    `start` HERHANGİ bir gün olabilir — Pazartesi şartı YOKTUR (`GET …/plan`ten
    bilinçli fark): bu haftalık ızgara değil, kayan bir penceredir.

    `days` varsayılanı 5'tir (GK bloğunun kutu sayısı) ve `MAX_SUMMARY_DAYS` ile
    tavanlanır — gerekçe `service.MAX_SUMMARY_DAYS` yanındadır.
    """
    return await read.get_day_summary(session, user, site_id, start, days)


@router.put("/sites/{site_id}/plan/rows", response_model=SitePlanRowsResult, dependencies=[_FULL])
async def save_site_plan_rows_endpoint(
    request: Request,
    site_id: uuid.UUID,
    data: SitePlanRowsSave,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SitePlanRowsResult:
    """Izgaranın satır listesi — **DEĞİŞTİRME** semantiği.

    ⚠️ Gövde şantiyenin satır kümesinin TAM kümesidir: gövdede geçmeyen satır
    SİLİNİR ve o satırın TÜM haftalardaki hücreleri FK CASCADE ile gider. Başka
    şantiyenin satırlarına DOKUNULMAZ (kesin karar `write.save_rows`).

    `week_start` YOKTUR: satır ızgaranın kaynağıdır, hücre haftaya aittir.
    """
    context = await service.visible_site(session, user, site_id)
    rows = await write.save_rows(session, context, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.site_plan_rows_saved(context.project.name, context.site.name, len(rows)),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return SitePlanRowsResult(
        rows=[
            SitePlanRowSaved(
                id=row.id,
                kind=row.kind,
                section_id=row.section_id,
                label=row.label,
                planned_worker_count=row.planned_worker_count,
                sort_order=row.sort_order,
            )
            for row in rows
        ]
    )


@router.put("/sites/{site_id}/plan/cells", response_model=SitePlanWeek, dependencies=[_FULL])
async def save_site_plan_cells_endpoint(
    request: Request,
    site_id: uuid.UUID,
    data: SitePlanCellsSave,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    week_start: _WEEK_START,
) -> SitePlanWeek:
    """Izgaranın hücreleri — **YALNIZ `week_start` haftası**, DEĞİŞTİRME semantiği.

    ⚠️ Gövde hafta + şantiye kapsamının TAM kümesidir: gövdede geçmeyen hücre
    SİLİNİR. Başka HAFTANIN ve başka ŞANTİYENİN hücrelerine DOKUNULMAZ (kesin
    karar `write.save_cells`; kapsamın iki parçası ayrı ayrı test edilir).

    Metni boş hücre plana YAZILMAZ — "hücre yokluğu = plan yok" (spec §2).

    Yanıt GÜNCEL ızgaradır: ekran kaydettiğinin tamamını geri görmelidir.
    """
    week_start = service.assert_week_start(week_start)
    context = await service.visible_site(session, user, site_id)
    cell_count = await write.save_cells(session, context, data, week_start)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.site_plan_cells_saved(
            context.project.name, context.site.name, week_start, cell_count
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await read.build_week(session, context, week_start)


@router.put("/sites/{site_id}/plan/goals", response_model=SitePlanWeek, dependencies=[_FULL])
async def save_site_plan_goals_endpoint(
    request: Request,
    site_id: uuid.UUID,
    data: SitePlanGoalsSave,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    week_start: _WEEK_START,
) -> SitePlanWeek:
    """P203-227 haftalık hedefler — DEĞİŞTİRME semantiği.

    ⚠️ Gövde o HAFTANIN hedef kümesidir: geçmeyen hedef SİLİNİR, başka haftanın
    hedeflerine DOKUNULMAZ. Başka haftanın hedef kimliği 422'dir — kabul
    edilseydi hedef sessizce hafta değiştirirdi (kesin karar `write.save_goals`).
    """
    week_start = service.assert_week_start(week_start)
    context = await service.visible_site(session, user, site_id)
    goal_count = await write.save_goals(session, context, data, week_start)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.site_plan_goals_saved(
            context.project.name, context.site.name, week_start, goal_count
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await read.build_week(session, context, week_start)


@router.put(
    "/sites/{site_id}/plan/sprint", response_model=SitePlanSprintRead | None, dependencies=[_FULL]
)
async def save_site_plan_sprint_endpoint(
    request: Request,
    site_id: uuid.UUID,
    data: SitePlanSprintSave,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SitePlanSprintRead | None:
    """P107 "Aktif Sprint" şeridi. Boş/`null` ad aktif sprinti KAPATIR (`null` döner).

    Şantiye başına TEK aktif sprint vardır (kısmi UQ); uç mevcut aktif satırı
    yeniden kullanır, ikinci bir aktif satır AÇMAZ. Tarih alanı ve görünüm kipi
    (Hafta/Ay/Sprint) backend'e AÇILMAZ — mockup göstermiyor (spec §2, §6 S4).
    """
    context = await service.visible_site(session, user, site_id)
    sprint = await write.save_sprint(session, context, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.site_plan_sprint_saved(
            context.project.name, context.site.name, None if sprint is None else sprint.name
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return None if sprint is None else SitePlanSprintRead(id=sprint.id, name=sprint.name)

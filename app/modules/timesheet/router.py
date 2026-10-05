"""Puantaj matris uçları — T3 (spec §3).

`site_diary/router.py` deseninin kardeşi: kapı sabitleri modül düzeyinde
tanımlanır, denetim metinleri `audit/messages.py`den gelir, kesin kararlar
`service.py` + `matrix.py`dedir ve burada TEKRARLANMAZ.

Kapılar `timesheet` iznidir (seed satır 171, matris DEĞİŞMEZ): okuma `view`,
yazma `full`. Bu ayrım **saha mühendisini SALT OKUR** yapar (`timesheet=_V`) —
matrisi şantiye şefi (`_F`) doldurur. Proje müdürü (`_N`) okuyamaz bile.

## Aylık okuma + HAFTALIK yazma (PUAN-SAAT)

Ekran (E5 `Ekran 5 - Puantaj.dc.html`) **haftalıktır** — giriş, kaydetme ve
Normal/FM türevleri hafta kapsamındadır. Aylık matris OKUMA olarak KALIR: Excel
çıktısı ve arşiv görünümü aydır (dosya adı da dönem taşır), ama **aylık YAZMA
UCU KALDIRILDI** — bir haftayı kaydetmenin ayın geri kalanını silmesi mümkün
olan tek yol oydu.

🔴 **KIRICI DEĞİŞİKLİK:** `PUT /sites/{id}/timesheet?year=&month=` artık YOKTUR;
yerine `PUT /sites/{id}/timesheet/week?iso_year=&iso_week=` geldi.

**"Önceki Haftayı Kopyala" (E5 75) AYRI UÇ DEĞİLDİR** — ölçüldü ve karar verildi:
istemci aynı `GET …/timesheet/week` ucunu bir önceki ISO haftası için çağırır ve
formu onunla tohumlar. Sunucu tarafı bir kopyalama ucu, kullanıcı ızgarayı
görmeden yazardı (mockup'ta düğme formu DOLDURUR, kaydetmez — kaydetme ayrı
düğmedir, E5 76) ve kişi-gün tekliği (UQ) ihlallerini kullanıcının göremediği
bir anda 409'a çevirirdi.

`export.xlsx` (T4) da AYNI `_VIEW` kapısındadır: indirme bir OKUMADIR, matrisi
görebilen indirebilir. Denetim günlüğüne YAZMAZ (T7 kuralı: okumalar denetlenmez).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from app.core import http
from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.errors import SiteValidationError
from app.core.mask_route import MaskeRotasi, maskele_baglamli
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.core.ratelimit import client_ip
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.timesheet import export, guards, matrix, repository, service, week
from app.modules.timesheet.schemas import TimesheetMatrix, TimesheetWeek, TimesheetWeekSave
from app.modules.users.models import User

router = APIRouter(tags=["timesheet"], responses=COMMON_ERROR_RESPONSES, route_class=MaskeRotasi)

_VIEW = require_permission(service.PERMISSION_MODULE, AccessLevel.view)
_FULL = require_permission(service.PERMISSION_MODULE, AccessLevel.full)

# Donem ZORUNLUDUR (`site_diary`nin opsiyonel `year`/`month`undan bilincli fark):
# gunluk kayit LISTELENIR, puantaj bir AY MATRISIDIR ve mockup'ta ay secici her
# zaman doludur (SP 96 "Temmuz 2026"). Donemsiz matrisin sutun iskeleti bile yok.
_YEAR = Annotated[int, Query(ge=2000, le=2100)]
_MONTH = Annotated[int, Query(ge=1, le=12)]

# ISO hafta: 1..53. 53 GERCEKTEN vardir (orn. 2026 ISO yili 53 haftalidir) ve
# ust siniri 52 koymak yilda bir haftayi erisilmez yapardi. Var OLMAYAN hafta
# (orn. 2027-W53) `date.fromisocalendar`ta ValueError'dur -> 422 (asagida).
_ISO_YEAR = Annotated[int, Query(ge=2000, le=2100)]
_ISO_WEEK = Annotated[int, Query(ge=1, le=53)]


@router.get("/sites/{site_id}/timesheet", response_model=TimesheetMatrix, dependencies=[_VIEW])
async def get_site_timesheet_endpoint(
    site_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: _YEAR,
    month: _MONTH,
    section_id: uuid.UUID | None = None,
) -> TimesheetMatrix:
    """ŞP/E5 puantaj matrisi: kişi satırları + gün hücreleri + türev toplamlar.

    `section_id` ŞP 99'un "Tüm Bölümler / Kat 6–10" seçicisidir. Başka şantiyenin
    bölümü boş matris DEĞİL 404 alır (kesin karar `service.visible_section`).
    """
    site, project = await service.visible_site(session, user, site_id)
    section = await service.visible_section(session, site, section_id)
    return await matrix.build(session, site, project, section, year=year, month=month)


@router.get(
    "/sites/{site_id}/timesheet/export.xlsx",
    dependencies=[_VIEW],
    response_class=Response,
    responses={200: {"content": {export.XLSX_MEDIA_TYPE: {}}, "description": "Excel dosyasi"}},
)
async def export_site_timesheet_endpoint(
    site_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: _YEAR,
    month: _MONTH,
    section_id: uuid.UUID | None = None,
) -> Response:
    """ŞP matrisinin Excel çıktısı (spec §3).

    Matris `matrix.build` ile AYNI çağrıdan gelir — kapsam süzgeci, bölüm 404'ü
    ve TÜM toplamlar okuma ucuyla birebir aynıdır. Okuma ucudur: `record_audit`
    ÇAĞIRMAZ.
    """
    site, project = await service.visible_site(session, user, site_id)
    section = await service.visible_section(session, site, section_id)
    built = await matrix.build(session, site, project, section, year=year, month=month)
    # IZN-B4c: puantaj alanları `Hassas.yok` (saat) → maske no-op; uç yine de ortak yoldan geçer
    # ki modüle ileride etiketli alan eklenirse dosya ekranla AYNI maskeyi taşısın.
    built = await maskele_baglamli(built)
    buffer = export.build_timesheet_workbook(built)
    name = export.filename(site.code, year, month)
    return Response(
        content=buffer.getvalue(),
        media_type=export.XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": http.content_disposition(name)},
    )


def _assert_week_exists(iso_year: int, iso_week: int) -> None:
    """Var olmayan ISO haftasi (orn. 53 haftasi olmayan bir yilin 53. haftasi).

    `date.fromisocalendar` ValueError atar; yakalanmazsa 500 olurdu. Kullanicinin
    duzeltebilecegi bir GIRDI hatasidir -> 422.
    """
    try:
        repository.week_bounds(iso_year, iso_week)
    except ValueError as exc:  # pragma: no cover - mesaj asagidaki testte olculur
        raise SiteValidationError(guards.format_week_missing(iso_year, iso_week)) from exc


@router.get("/sites/{site_id}/timesheet/week", response_model=TimesheetWeek, dependencies=[_VIEW])
async def get_site_timesheet_week_endpoint(
    site_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    iso_year: _ISO_YEAR,
    iso_week: _ISO_WEEK,
    section_id: uuid.UUID | None = None,
) -> TimesheetWeek:
    """E5 haftalık ekranı: 7 günlük ızgara + Normal/FM türevleri + ay şeridi.

    "Önceki Haftayı Kopyala" (E5 75) İSTEMCİ tarafındadır: aynı uç bir önceki
    ISO haftası için çağrılır (gerekçe modül docstring'inde).
    """
    _assert_week_exists(iso_year, iso_week)
    site, project = await service.visible_site(session, user, site_id)
    section = await service.visible_section(session, site, section_id)
    return await week.build(session, site, project, section, iso_year=iso_year, iso_week=iso_week)


@router.put("/sites/{site_id}/timesheet/week", response_model=TimesheetWeek, dependencies=[_FULL])
async def save_site_timesheet_week_endpoint(
    request: Request,
    site_id: uuid.UUID,
    data: TimesheetWeekSave,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    iso_year: _ISO_YEAR,
    iso_week: _ISO_WEEK,
    section_id: uuid.UUID | None = None,
) -> TimesheetWeek:
    """E5 76 "Haftayı Kaydet" — **DEĞİŞTİRME** semantiği.

    ⚠️ Gövde kaydedilen KAPSAMIN TAM kümesidir: kapsam içinde gövdede geçmeyen
    hücre SİLİNİR. Aynı ayın BAŞKA haftalarına ve başka şantiyeye DOKUNULMAZ
    (kesin karar `service.save_week`).

    🔴 `section_id` GET ile AYNI süzgeçtir ve KAPSAMI DARALTIR: verildiğinde
    yalnız o bölümün hücreleri kilitlenir, güncellenir ve silinir. Süzgeç
    yazmada da tanınmasaydı, bölüm süzgeçli bir ızgarayı gönderen istemci aynı
    haftanın DİĞER bölümlerini geri alınamaz biçimde silerdi. Süzgeç varken
    gövdedeki her hücre o bölüme ait olmalıdır (422), başka şantiyenin bölümü
    okumadaki ile aynı 404'tür.

    Denetim TEK hafta-özeti olayıdır; hücre başına olay yazmak 7×48'lik bir
    kaydetmede denetim günlüğünü kullanılamaz hâle getirirdi (spec §3).

    Yanıt GÜNCEL haftadır ve KAYDEDİLEN kapsamı gösterir: süzgeçsiz istekte
    şantiyenin tamamı, süzgeçli istekte o bölüm — ekran kaydettiğinin tamamını,
    fazlasını DEĞİL, geri görmelidir.
    """
    _assert_week_exists(iso_year, iso_week)
    context = await service.visible_site(session, user, site_id)
    section = await service.visible_section(session, context.site, section_id)
    cell_count = await service.save_week(
        session, user, context, data, iso_year=iso_year, iso_week=iso_week, section=section
    )
    start, end = repository.week_bounds(iso_year, iso_week)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.timesheet_week_saved(
            context.project.name, context.site.name, iso_year, iso_week, start, end, cell_count
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await week.build(
        session, context.site, context.project, section, iso_year=iso_year, iso_week=iso_week
    )

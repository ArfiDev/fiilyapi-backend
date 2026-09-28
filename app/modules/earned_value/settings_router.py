"""Planlama (EV) uclari — settings_router. Kapilar ve kapsam: `access.py`.

Santiye EV ayarlari (AYP ekrani; PLANLAMA-SPEC §3.8 K1/K5/K6/K19/K25): tek GET +
tek PUT. Kapi: okuma `VIEW`, yazma `WRITE` (draft — saha muhendisi dahil, B1-8).
Kapsam: `access.visible_site` — gorunmeyen santiye ile olmayan santiye AYNI 404. PUT'ta
kapsam + "tamamlanmis santiye salt okunur" `access.completed_site_guard` bagimliligidir
(sira 404 → 409 → 422 govde; kilitli yetkili denetim `settings_service.save_settings`te).

`GET` `record_audit` CAGIRMAZ; PUT TEK `settings_saved` satiri yazar.
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.ratelimit import client_ip
from app.core.timezone import today
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.earned_value import audit_messages, settings_preview, settings_service
from app.modules.earned_value.access import (
    VIEW,
    WRITE,
    SiteContext,
    completed_site_guard,
    visible_site,
)
from app.modules.earned_value.guards import SITE_COMPLETED_READ_ONLY
from app.modules.earned_value.models import CompositeMeasure
from app.modules.earned_value.schemas_reports import CompositeValueOut, SettingsPreview
from app.modules.earned_value.schemas_settings import SettingsRead, SettingsSave
from app.modules.users.models import User

router = APIRouter(tags=["earned-value"], responses=COMMON_ERROR_RESPONSES)

_User = Annotated[User, Depends(get_current_user)]
_Session = DbSession
_Writable = Annotated[SiteContext, Depends(completed_site_guard(SITE_COMPLETED_READ_ONLY))]


@router.get(
    "/sites/{site_id}/earned-value/settings", response_model=SettingsRead, dependencies=[VIEW]
)
async def get_settings_endpoint(site_id: uuid.UUID, user: _User, session: _Session) -> SettingsRead:
    """Santiyenin EV ayarlari. Satir yoksa sabit varsayilanlar, `is_default` = true (K1)."""
    await visible_site(session, user, site_id)
    return await settings_service.get_settings(session, site_id)


@router.put(
    "/sites/{site_id}/earned-value/settings", response_model=SettingsRead, dependencies=[WRITE]
)
async def save_settings_endpoint(
    request: Request,
    site_id: uuid.UUID,
    context: _Writable,
    data: SettingsSave,
    user: _User,
    session: _Session,
) -> SettingsRead:
    """TAM DEGISTIRME: govde santiyenin ayar kumesinin TAMAMIDIR.

    ⚠️ Govdede gecmeyen tatil ve pacal metrik SILINIR. Baslangic/bitis tarihi alan
    DEGILDIR (K7); "her n. haftanin X gunu" kurali YOKTUR (S5). Yanit guncel GET govdesi.
    """
    # §3.10 F0-8: tamamlanmis santiyenin ayarlari SALT OKUNUR (409) — `_Writable` bagimliligi
    # govdeden once, servis kilit altinda.
    result = await settings_service.save_settings(session, site_id, data, user)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=audit_messages.settings_saved(context.project.name, context.site.name),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return result


_Day = Annotated[date | None, Query(alias="date", description="Varsayilan: bugun (TR)")]


@router.get(
    "/sites/{site_id}/earned-value/settings/preview",
    response_model=SettingsPreview,
    dependencies=[VIEW],
)
async def get_settings_preview(
    site_id: uuid.UUID, user: _User, session: _Session, day: _Day = None
) -> SettingsPreview:
    """AYP canli degerleri: bugunun sapmasi (K27 puan) · gunluk/haftalik PF · hafta no ·
    kayitli pacal metriklerin gerceklesen/planlisi (B3-3). Kayitli ayarla hesaplanir."""
    await visible_site(session, user, site_id)
    return await settings_preview.preview(session, site_id, day or today())


@router.get(
    "/sites/{site_id}/earned-value/settings/preview/composite",
    response_model=CompositeValueOut,
    dependencies=[VIEW],
)
async def get_composite_preview(
    site_id: uuid.UUID,
    user: _User,
    session: _Session,
    measure: CompositeMeasure,
    numerator_item_id: Annotated[list[uuid.UUID], Query(min_length=1, max_length=200)],
    denominator_item_id: uuid.UUID,
    day: _Day = None,
) -> CompositeValueOut:
    """Duzenlenen (kaydedilmemis) pacal metrigin canli degeri — kayitli metriklerle AYNI formul."""
    await visible_site(session, user, site_id)
    return await settings_preview.composite_preview(
        session, site_id, day or today(), measure, numerator_item_id, denominator_item_id
    )

"""Şantiye günlüğü durum aksiyonları (T4) — iki uç.

`router.py`den AYRI dosyadadır (dosya başına ~400 satır kuralı, taşeron
`router_transitions.py` deseninin aynısı); tek bir `APIRouter` olarak kalması
için `router.py` sonunda `include_router` ile BAĞLANIR — yön tek taraflıdır ve
modül dışına tek bir router çıkar.

İki uç da TEK yoldan (`transitions.perform`) geçer: geçiş tablosu, kilit ve damga
ORADA tek kopyadır. Router'ın işi KAPIYI seçmek ve denetim satırını yazmaktır;
durum kontrolü BURADA TEKRARLANMAZ.

Kapılar: `submit` → `_FULL` (şef/saha/patron; PM `view` olduğu için 403) ·
`reopen` → `_ADMIN` (gerekçe `transitions` modül docstring'inde).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.core.ratelimit import client_ip
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.site_diary import read, service, transitions
from app.modules.site_diary.schemas import SiteDiaryEntryDetail
from app.modules.users.models import User

router = APIRouter(tags=["site-diary"], responses=COMMON_ERROR_RESPONSES)

_FULL = require_permission(service.PERMISSION_MODULE, AccessLevel.full)
_ADMIN = require_permission(service.PERMISSION_MODULE, AccessLevel.admin)


@router.post("/diary/{entry_id}/submit", response_model=SiteDiaryEntryDetail, dependencies=[_FULL])
async def submit_site_diary_entry_endpoint(
    request: Request,
    entry_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SiteDiaryEntryDetail:
    """`draft → submitted` + `submitted_at` damgası (E7'nin "Gönder" butonu).

    Gönderim kaydı `summary`nin saydığı kümeye SOKAR (spec §3) ve yazma kapısını
    kapatır; geri almanın tek yolu `reopen`dır. İkinci `submit` 409'dur —
    sessiz/idempotent geçiş, ilk damgayı üzerine yazmak olurdu.
    """
    context = await transitions.perform(session, user, entry_id, transitions.DiaryAction.submit)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.site_diary_entry_submitted(
            context.project.name, context.site.name, context.entry.entry_date
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await read.build_detail(session, context)


@router.post("/diary/{entry_id}/reopen", response_model=SiteDiaryEntryDetail, dependencies=[_ADMIN])
async def reopen_site_diary_entry_endpoint(
    request: Request,
    entry_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SiteDiaryEntryDetail:
    """`submitted → draft` (yanlış gönderim düzeltmesi) — YALNIZ `admin`.

    Kapı `_FULL` DEĞİLDİR: kaydı giren rol kendi gönderimini geri açabilseydi,
    `summary`nin saydığı hakediş rakamı denetimsiz değiştirilebilirdi. `draft`
    kaynak DEĞİLDİR (409): geri alınacak bir gönderim yoktur.
    """
    context = await transitions.perform(session, user, entry_id, transitions.DiaryAction.reopen)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.site_diary_entry_reopened(
            context.project.name, context.site.name, context.entry.entry_date
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await read.build_detail(session, context)

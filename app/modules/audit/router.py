"""Denetim gunlugu okuma ve disa aktarim uclari (plan Task 4-5).

Yalnizca okuma uclari tanimlanir; audit satirlari `record_audit` ile yazma uclarinda
uretilir. Bu tablo icin UPDATE/DELETE ucu YOKTUR.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.core import http
from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.modules.audit import repository
from app.modules.audit.export import build_audit_workbook
from app.modules.audit.gizli_tutar import (
    TutarOlayi,
    detay_maskele,
    gizli_kategoriler,
    gizli_olaylar,
)
from app.modules.audit.models import AuditAction
from app.modules.audit.repository import AuditRow
from app.modules.audit.schemas import AuditActorRead, AuditItem, AuditListResponse

router = APIRouter(
    route_class=MaskeRotasi, prefix="/audit-log", tags=["audit"], responses=COMMON_ERROR_RESPONSES
)

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSX_FILENAME = "denetim-gunlugu.xlsx"


@dataclass
class AuditFilters:
    """Liste ve disa aktarim uclarinin PAYLASTIGI filtre kumesi — tek yerde tanimli."""

    actor_user_id: Annotated[uuid.UUID | None, Query()] = None
    action: Annotated[AuditAction | None, Query()] = None
    date_from: Annotated[
        date | None,
        Query(description="YYYY-MM-DD — o gunun 00:00'indan itibaren (Europe/Istanbul, dahil)"),
    ] = None
    date_to: Annotated[
        date | None,
        Query(description="YYYY-MM-DD — o gunun sonuna kadar (Europe/Istanbul, dahil)"),
    ] = None
    q: Annotated[str | None, Query(description="Detay metni veya aktor adinda kismi arama")] = None

    def as_kwargs(self) -> dict[str, object]:
        return {
            "actor_user_id": self.actor_user_id,
            "action": self.action,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "q": self.q,
        }


def _to_item(row: AuditRow, olaylar: tuple[TutarOlayi, ...]) -> AuditItem:
    entry, actor, role = row
    return AuditItem(
        id=entry.id,
        occurred_at=entry.occurred_at,
        action=entry.action,
        # Politika A (IZN-B5a): okuyucunun kategorisi gizliyse tutarli olayin metni sabit ifade.
        detail=detay_maskele(entry.detail, olaylar),
        # asyncpg INET'i IPv4Address/IPv6Address dondurur — metne cevrilmeden serilestirilemez.
        ip_address=str(entry.ip_address) if entry.ip_address is not None else None,
        actor=(
            AuditActorRead(
                id=actor.id, full_name=actor.full_name, role_name=role.name if role else ""
            )
            if actor is not None
            else None
        ),
    )


@router.get(
    "",
    response_model=AuditListResponse,
    dependencies=[require_permission("settings", AccessLevel.view)],
)
async def list_audit_log_endpoint(
    session: DbSession,
    filters: Annotated[AuditFilters, Depends()],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditListResponse:
    """Filtrelenebilir/sayfalanabilir denetim gunlugu listesi (`occurred_at DESC`)."""
    olaylar = gizli_olaylar(await gizli_kategoriler())
    kwargs = {**filters.as_kwargs(), "gizli_olaylar": olaylar}
    rows = await repository.list_audit_entries(session, limit=limit, offset=offset, **kwargs)
    total = await repository.count_audit_entries(session, **kwargs)
    return AuditListResponse(
        items=[_to_item(row, olaylar) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get(
    "/export.xlsx",
    dependencies=[require_permission("settings", AccessLevel.view)],
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Excel dosyasi"}},
)
async def export_audit_log_endpoint(
    session: DbSession,
    filters: Annotated[AuditFilters, Depends()],
) -> Response:
    """Filtrelenmis denetim gunlugunu Excel dosyasi olarak doner.

    Liste ucuyle AYNI filtreler gecerlidir; `limit`/`offset` YOKTUR — eslesen tum
    kayitlar yazilir (sessiz kirpma yapilmaz, bkz. plan Task 5 sinir notu).
    """
    olaylar = gizli_olaylar(await gizli_kategoriler())
    rows = await repository.list_audit_entries(
        session, limit=None, **filters.as_kwargs(), gizli_olaylar=olaylar
    )
    buffer = build_audit_workbook(rows, gizli_olaylar=olaylar)
    return Response(
        content=buffer.getvalue(),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": http.content_disposition(XLSX_FILENAME)},
    )

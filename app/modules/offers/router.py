"""Teklif Hazirlama uclari (TKL-B4.1: yalniz `/offers/settings`; teklif uclari B4.2).

| uc | kapi |
|----|------|
| `GET /offers/settings` | `contracts:view` |
| `PUT /offers/settings` | `contracts:full` + `RequireUnrestricted` |

# GECICI IZIN (T25 deseni, izin turune kadar): teklif ayri bir izin modulu DEGILDIR,
# `contracts` iznine baglanir (katalog ile ayni). Izin modulu TOHUMLANMAZ.
# `site_chief`/`field_engineer` (contracts=none) 403 alir.

🔴 ROTA SIRASI (B4.2 icin): `/offers/settings` LITERALDIR; B4.2'de `GET /offers/{offer_id}`
eklendiginde literal yol parametreliden ONCE tanimlanmali (ayni router icinde, ustte).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_deps import RequireUnrestricted
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import kapsam_kapisi, require_permission
from app.core.ratelimit import client_ip
from app.core.scoped_route import kapsam_rotasi, kapsamdan_oku
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.offers import service
from app.modules.offers.schemas import OfferSettingsRead, OfferSettingsUpdate
from app.modules.users.models import User

# 🔴 KAPSAM MASKESI — IKI PARCA DA GEREKLI (bkz. `catalog/router.py`); cifti
#    `tests/core/test_kapsam_baglantisi.py` cakar.
router = APIRouter(
    tags=["offers"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=kapsam_rotasi("contracts", kapsamdan_oku),
    dependencies=[kapsam_kapisi("contracts")],
)

_VIEW = require_permission("contracts", AccessLevel.view)
_FULL = require_permission("contracts", AccessLevel.full)

_User = Annotated[User, Depends(get_current_user)]


@router.get("/offers/settings", response_model=OfferSettingsRead, dependencies=[_VIEW])
async def get_offer_settings_endpoint(session: DbSession) -> OfferSettingsRead:
    """Teklif varsayilanlari: GG / kar / KDV yuzdesi, gecerlilik gunu, odeme metni."""
    return OfferSettingsRead.model_validate(await service.get_settings(session))


@router.put(
    "/offers/settings",
    response_model=OfferSettingsRead,
    dependencies=[_FULL, RequireUnrestricted],
)
async def update_offer_settings_endpoint(
    request: Request, data: OfferSettingsUpdate, user: _User, session: DbSession
) -> OfferSettingsRead:
    """Varsayilanlari TAM degistirir. Yeni revizyonlar bu degerleri KOPYALAR; mevcut teklifler
    degismez."""
    row = await service.update_settings(session, data)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=messages.offer_settings_updated(
            row.default_overhead_pct,
            row.default_profit_pct,
            row.default_vat_pct,
            row.default_validity_days,
        ),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return OfferSettingsRead.model_validate(row)

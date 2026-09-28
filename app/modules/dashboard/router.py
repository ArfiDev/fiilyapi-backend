from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import kapsam_kapisi, require_permission
from app.core.scoped_route import kapsam_rotasi, kapsamdan_oku
from app.modules.dashboard.schemas import DashboardSummaryResponse
from app.modules.dashboard.service import build_summary
from app.modules.users.models import User

# 🔴 KAPSAM MASKESİ — İKİ PARÇA DA GEREKLİ (kullanıcı kararı 2026-09-19):
#    `route_class` dönen modeli maskeler, `dependencies` aktörün kapsamını
#    köprüye yazar. Biri eksikse maske SESSİZCE `all` görür ve hiçbir şey
#    gizlemez. Çifti `tests/core/test_kapsam_baglantisi.py` çakar.
router = APIRouter(
    prefix="/dashboard",
    tags=["dashboard"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=kapsam_rotasi("dashboard", kapsamdan_oku),
    dependencies=[kapsam_kapisi("dashboard")],
)


@router.get(
    "/summary",
    response_model=DashboardSummaryResponse,
    dependencies=[require_permission("dashboard", AccessLevel.view)],
)
async def get_dashboard_summary_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> DashboardSummaryResponse:
    return await build_summary(session, user)

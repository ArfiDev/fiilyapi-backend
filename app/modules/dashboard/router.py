from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_deps import DisciplineScoped
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.modules.dashboard.schemas import DashboardSummaryResponse
from app.modules.dashboard.service import build_summary
from app.modules.users.models import User

# HASSAS ALAN MASKESİ (IZN-B4): `MaskeRotasi` tek parça (bağlamı kendisi ekler).
router = APIRouter(
    prefix="/dashboard",
    tags=["dashboard"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=MaskeRotasi,
)


@router.get(
    "/summary",
    response_model=DashboardSummaryResponse,
    dependencies=[require_permission("dashboard", AccessLevel.view)],
)
async def get_dashboard_summary_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    scope: DisciplineScoped,
) -> DashboardSummaryResponse:
    return await build_summary(session, user, scope)

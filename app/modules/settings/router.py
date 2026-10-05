from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.modules.settings import service
from app.modules.settings.schemas import (
    NotificationPrefItem,
    NotificationPrefsUpdate,
    PreferencesRead,
    PreferencesUpdate,
)
from app.modules.users.models import User

router = APIRouter(
    route_class=MaskeRotasi, prefix="/settings", tags=["settings"], responses=COMMON_ERROR_RESPONSES
)


@router.get("/preferences", response_model=PreferencesRead)
async def get_preferences_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PreferencesRead:
    return await service.get_preferences(session, user)


@router.put("/preferences", response_model=PreferencesRead)
async def update_preferences_endpoint(
    data: PreferencesUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PreferencesRead:
    return await service.update_preferences(session, user, data)


@router.get("/notifications", response_model=list[NotificationPrefItem])
async def get_notifications_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> list[NotificationPrefItem]:
    return await service.get_notifications(session, user)


@router.put("/notifications", response_model=list[NotificationPrefItem])
async def update_notifications_endpoint(
    data: NotificationPrefsUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> list[NotificationPrefItem]:
    return await service.update_notifications(session, user, data)

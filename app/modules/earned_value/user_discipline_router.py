"""KALDIRILDI (IZN-B3): `/users/{user_id}/disciplines` — disiplin artık PROJE BAŞINA.

Eski global kullanıcı → disiplin ataması (DSC-B0) proje ekibine taşındı: disiplinler
`PUT /users/{id}/access` gövdesinde `projects[].discipline_ids` olarak, o projedeki rolle birlikte
atanır (KARARLAR §1.7). İki uç da 410 döner (IZN-B2 `PUT /roles/{id}/permissions/{module}` emsali);
kapıları eskisiyle AYNIDIR, yani yetkisiz aktör 403 görmeye devam eder.
Bu uçlar disipline DUYARLI DEĞİLDİR (kullanıcı yönetimi) — rota bekçisi dışında tutulur.
"""

import uuid

from fastapi import APIRouter, HTTPException, status

from app.core.access import AccessLevel
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.modules.earned_value import discipline_adapter

# Disiplin kapsami portuna kayit — `catalog_router`in kaydina ek guvence (idempotent).
discipline_adapter.register()

router = APIRouter(
    prefix="/users", tags=["earned-value", "users"], responses=COMMON_ERROR_RESPONSES
)

#: 410 gövdesi: global disiplin ataması kalktı (IZN-B3). Mesaj yeni ucu işaret eder.
USER_DISCIPLINES_GONE_DETAIL = (
    "Disiplin ataması artık proje başına yapılır. Ayarlar > Kullanıcılar ekranından "
    "(PUT /users/{id}/access, projects[].discipline_ids) düzenleyin."
)

_GONE_RESPONSES = {
    status.HTTP_410_GONE: {
        "description": "Uç kaldırıldı: disiplin artık proje ekibinde (`/users/{id}/access`)"
    }
}


@router.get(
    "/{user_id}/disciplines",
    deprecated=True,
    status_code=status.HTTP_410_GONE,
    response_model=None,
    responses=_GONE_RESPONSES,
    dependencies=[require_permission("user_management", AccessLevel.view)],
)
async def get_user_disciplines_endpoint(user_id: uuid.UUID) -> None:
    """KALDIRILDI (IZN-B3): her çağrı 410 döner."""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=USER_DISCIPLINES_GONE_DETAIL)


@router.put(
    "/{user_id}/disciplines",
    deprecated=True,
    status_code=status.HTTP_410_GONE,
    response_model=None,
    responses=_GONE_RESPONSES,
    dependencies=[require_permission("user_management", AccessLevel.full)],
)
async def set_user_disciplines_endpoint(user_id: uuid.UUID) -> None:
    """KALDIRILDI (IZN-B3): her çağrı 410 döner, hiçbir şey yazılmaz."""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=USER_DISCIPLINES_GONE_DETAIL)

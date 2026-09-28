from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel, Scope, satisfies
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.scoped_route import kapsam_bagimligi_kur
from app.modules.roles.repository import get_permission
from app.modules.users.models import User


def require_permission(module_key: str, min_level: AccessLevel):
    """Uç için asgari yetki kapısı.

    Kullanımı:
        @router.post(
            "/x", dependencies=[require_permission("progress_payments", AccessLevel.draft)]
        )

    İzin satırı yoksa erişim reddedilir (varsayılan kapalı).
    """

    async def _check(
        user: Annotated[User, Depends(get_current_user)],
        session: DbSession,
    ) -> None:
        permission = await get_permission(session, user.role_id, module_key)
        if permission is None or not satisfies(permission.access_level, min_level):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Bu işlem için yetkiniz yok"
            )

    return Depends(_check)


async def can_read(session: AsyncSession, user: User, module_key: str) -> bool:
    """ILR-1/2 — bir ROLUN o modulu OKUYUP okuyamadigi (uc kapisi DEGIL, ALAN kapisi).

    🔴 `require_permission` bir UCU kapatir; bu ise TUREV BIR ALANI kapatir.
    Ikisi ayri sorunlardir: `boq` ucunu okuyabilen `procurement`, gunlukten
    turemis bir yuzdeyi gormemelidir — yoksa `site_diary`nin kapisi hic
    calismadan o veri BOQ ekranindan sizar (K4).

    Varsayilan KAPALI: izin satiri yoksa `False`.
    """
    permission = await get_permission(session, user.role_id, module_key)
    return permission is not None and satisfies(permission.access_level, AccessLevel.view)


async def actor_scope(session: AsyncSession, user: User, module_key: str) -> Scope:
    """Aktörün O MODÜLDEKİ veri kapsamı (`RolePermission.scope`).

    🔴 `require_permission` bir UCU, `can_read` bir ALANI kapatır; bu ise alan
    kapısını KAPSAMA bağlar (`core.field_scope` üç kovayı anlatır).

    İzin satırı yoksa `Scope.all` döner ve bu bilinçli bir fail-OPEN'dır: satır
    yoksa `require_permission` ucu ZATEN 403 ile kapatmıştır, yani buraya
    ulaşılmışsa erişim vardır. Burada `limited` varsaymak, kapsamı yapılandırılmamış
    her rolün ekranını sessizce boşaltırdı.
    """
    permission = await get_permission(session, user.role_id, module_key)
    return Scope.all if permission is None else permission.scope


def kapsam_kapisi(module_key: str):
    """Routerın `dependencies=[...]`ine eklenen KÖPRÜ.

    Bunu kuran router, `route_class=kapsam_rotasi(module_key, kapsamdan_oku)`
    ile birlikte kullanılmalıdır; ikisinden biri eksikse maske sessizce
    `all` görür. `tests/core/test_kapsam_baglantisi.py` bu çifti çakar.
    """

    async def _cozucu(
        user: Annotated[User, Depends(get_current_user)],
        session: DbSession,
    ) -> Scope:
        return await actor_scope(session, user, module_key)

    # Kapsam bağımlılığı async generator'dır; function kapsamlı `get_db`ye
    # bağlanabilmesi için kendisinin de function olması şart (yoksa import'ta
    # DependencyScopeError). Maske ve serileştirme function_stack kapanmadan önce
    # koştuğundan davranış değişmez.
    return Depends(kapsam_bagimligi_kur(_cozucu), scope="function")

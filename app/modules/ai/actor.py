"""`ActorContext` çözümü — **her dispatch'te TAZE** (S19).

Uzun bir tur boyunca yetki DONMAZ: `token_version` iptali, `status=passive`e
düşürme ve izin matrisinin çalışma anında düzenlenmesi bu depoda gerçektir.
Bu yüzden aktör bağlamı önbelleğe alınmaz.

🔴 Bu dosya `ai/tools/**` altında **DEĞİLDİR** — bilerek. B14 import sınırı
araçların `repository`ye dokunmasını yasaklar; aktör çözümü ise araç değil,
huninin girdisidir ve `roles.repository.get_role_matrix`i kullanmak zorundadır
(aynı matrisi ikinci kez yazmak, `/auth/me` ile sessizce ayrışmak demekti).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import user_scope
from app.modules.ai.registry import ActorContext
from app.modules.roles.models import Role
from app.modules.roles.repository import get_role_matrix
from app.modules.users.models import User


async def aktor_baglami(session: AsyncSession, user: User) -> ActorContext:
    """Aktörün rolünü ve izin haritasını **taze** okur.

    ⚠️ `get_role_matrix` bir **INNER JOIN**'dir: izin satırı olmayan modülün
    anahtarı haritada HİÇ bulunmaz. `kapilar_gecti` bunu fail-closed okur
    (`permissions.get(modul, AccessLevel.none)`), yani eksik satır "yetki yok"
    anlamına gelir — `require_permission`ın davranışıyla birebir aynı.
    """
    # 🔴 `user.role` ÜZERİNDEN OKUNMAZ: `User.role` `lazy="raise"`tır ve yalnız
    # `get_current_user`ın `joinedload`u sayesinde doludur. Aktör bağlamı, User
    # nesnesinin NASIL yüklendiğine bağlı olamaz — `session.get` kimlik
    # haritasını kullanır, rol zaten yüklüyse ek sorgu KOŞMAZ.
    rol = await session.get(Role, user.role_id)
    if rol is None:  # pragma: no cover - FK bunu imkânsız kılar
        raise ValueError("Aktörün rolü bulunamadı")
    matris = await get_role_matrix(session, user.role_id)
    return ActorContext(
        user_id=user.id,
        role_key=rol.key,
        role_is_system=bool(rol.is_system),
        permissions={modul.key: izin.access_level for modul, izin in matris},
        disiplin_kisitli=(await user_scope(session, user.id)).is_restricted,
    )

"""`ActorContext` çözümü — **her dispatch'te TAZE** (S19).

Uzun bir tur boyunca yetki DONMAZ: `token_version` iptali, `status=passive`e
düşürme ve izin matrisinin çalışma anında düzenlenmesi bu depoda gerçektir.
Bu yüzden aktör bağlamı önbelleğe alınmaz.

🔴 Bu dosya `ai/tools/**` altında **DEĞİLDİR** — bilerek. B14 import sınırı
araçların `repository`ye dokunmasını yasaklar; aktör çözümü ise araç değil,
huninin girdisidir ve `roles.repository.derived_role_matrix`i kullanmak zorundadır
(aynı matrisi ikinci kez yazmak, `/auth/me` ile sessizce ayrışmak demekti).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import user_scope
from app.modules.ai.registry import ActorContext
from app.modules.roles.models import Role
from app.modules.roles.repository import derived_role_matrix
from app.modules.users.models import User


async def aktor_baglami(session: AsyncSession, user: User) -> ActorContext:
    """Aktörün rolünü ve izin haritasını **taze** okur.

    IZN-B2: izin haritası SAYFA HÜCRELERİNDEN türetilir (`derived_role_matrix`); kapılar
    `require_permission` ile aynı kaynaktan karar verir. Haritada olmayan modül fail-closed
    okunur (`permissions.get(modul, AccessLevel.none)`).
    """
    # 🔴 `user.role` ÜZERİNDEN OKUNMAZ: `User.role` `lazy="raise"`tır ve yalnız
    # `get_current_user`ın `joinedload`u sayesinde doludur. Aktör bağlamı, User
    # nesnesinin NASIL yüklendiğine bağlı olamaz — `session.get` kimlik
    # haritasını kullanır, rol zaten yüklüyse ek sorgu KOŞMAZ.
    rol = await session.get(Role, user.role_id)
    if rol is None:  # pragma: no cover - FK bunu imkânsız kılar
        raise ValueError("Aktörün rolü bulunamadı")
    matris = await derived_role_matrix(session, user.role_id, rol.key)
    return ActorContext(
        user_id=user.id,
        role_key=rol.key,
        role_is_system=bool(rol.is_system),
        permissions={modul.key: seviye for modul, seviye, _kapsam in matris},
        disiplin_kisitli=(await user_scope(session, user.id)).is_restricted,
    )

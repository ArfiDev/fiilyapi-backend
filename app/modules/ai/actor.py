"""`ActorContext` çözümü — **her dispatch'te TAZE** (S19).

Uzun bir tur boyunca yetki DONMAZ: `token_version` iptali, `status=passive`e
düşürme ve izin matrisinin çalışma anında düzenlenmesi bu depoda gerçektir.
Bu yüzden aktör bağlamı önbelleğe alınmaz.

🔴 Bu dosya `ai/tools/**` altında **DEĞİLDİR** — bilerek. B14 import sınırı
araçların `repository`ye dokunmasını yasaklar; aktör çözümü ise araç değil,
huninin girdisidir ve kapı kararını `core.page_gate` ile AYNI kaynaktan (sayfa hücreleri)
verir.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.discipline_scope import user_scope
from app.core.page_gate import gate_ok_from_cells, gosterge_duzeyi, load_cells
from app.core.sayfalar import MODUL_ANAHTARLARI as _MODULLER
from app.modules.ai.registry import ActorContext
from app.modules.roles.models import SYSTEM_ADMIN_KEY, Role
from app.modules.users.models import User

#: Kapı eşiklerinden türeyen modül anahtarları (IZN-B6b R2); TEK kaynak
#: `sayfalar.MODUL_ANAHTARLARI`.
MODUL_ANAHTARLARI: Final[frozenset[str]] = frozenset(_MODULLER)


async def aktor_baglami(session: AsyncSession, user: User) -> ActorContext:
    """Aktörün rolünü ve izin haritasını **taze** okur.

    IZN-B6b: `permissions` (`/ai/context` göstergesi) kapı düzeyidir (`level_from_cells`); sayfa
    Onaylar biti yansımaz — onay yetkisi `yetkilerim.sayfalar`dan okunur.
    GERÇEK kapıdan türer: modül başına `page_gate`in GEÇTİĞİ en yüksek düzey (Sistem
    Yöneticisi `admin`). Haritada olmayan modül fail-closed okunur
    (`permissions.get(modul, AccessLevel.none)`).

    IZN-B5e: katalog kapısı `gecen_kapilar`dır — ana rolün `page_gate` kapılarından GERÇEKTEN
    geçtiği `(modül, düzey)` çiftleri; `decide`in proje bağlamsız ana-rol dalıyla aynı kaynak
    (`gate_ok_from_cells`). Ekip rolü katalogu GENİŞLETMEZ (`decide`: ekip rolü tek başına
    kapıyı açmaz).
    """
    # 🔴 `user.role` ÜZERİNDEN OKUNMAZ: `User.role` `lazy="raise"`tır ve yalnız
    # `get_current_user`ın `joinedload`u sayesinde doludur. Aktör bağlamı, User
    # nesnesinin NASIL yüklendiğine bağlı olamaz — `session.get` kimlik
    # haritasını kullanır, rol zaten yüklüyse ek sorgu KOŞMAZ.
    rol = await session.get(Role, user.role_id)
    if rol is None:  # pragma: no cover - FK bunu imkânsız kılar
        raise ValueError("Aktörün rolü bulunamadı")
    sistem_yoneticisi = rol.key == SYSTEM_ADMIN_KEY
    hucreler = {} if sistem_yoneticisi else await load_cells(session, user.role_id)
    return ActorContext(
        user_id=user.id,
        role_key=rol.key,
        role_is_system=bool(rol.is_system),
        permissions={
            modul: gosterge_duzeyi(hucreler, modul, sistem_yoneticisi=sistem_yoneticisi)
            for modul in sorted(MODUL_ANAHTARLARI)
        },
        gecen_kapilar=frozenset(
            (modul, seviye)
            for modul in MODUL_ANAHTARLARI
            for seviye in AccessLevel
            if sistem_yoneticisi or gate_ok_from_cells(hucreler, modul, seviye)
        ),
        disiplin_kisitli=(await user_scope(session, user.id)).is_restricted,
    )

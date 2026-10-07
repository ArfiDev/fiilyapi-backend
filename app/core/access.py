import enum
from typing import Protocol


class AccessLevel(str, enum.Enum):
    """Erişim seviyesi. Sıralıdır: none < view < draft < request < approve < full < admin.

    full silmeyi KAPSAMAZ — silme yalnızca admin seviyesindedir (spec §5.0).
    """

    none = "none"
    view = "view"
    draft = "draft"
    request = "request"
    approve = "approve"
    full = "full"
    admin = "admin"


_LEVEL_ORDER: dict[AccessLevel, int] = {
    AccessLevel.none: 0,
    AccessLevel.view: 1,
    AccessLevel.draft: 2,
    AccessLevel.request: 3,
    AccessLevel.approve: 4,
    AccessLevel.full: 5,
    AccessLevel.admin: 6,
}


def satisfies(actual: AccessLevel, required: AccessLevel) -> bool:
    """actual seviyesi, required seviyesini karşılıyor mu?"""
    return _LEVEL_ORDER[actual] >= _LEVEL_ORDER[required]


#: `roles.models.SYSTEM_ADMIN_KEY` ile AYNI değer: `app/core` bir ürün modülünü ithal etmez, bu
#: yüzden literal burada yaşar; eşitliği `tests/core/test_is_system_admin.py` çakar.
SYSTEM_ADMIN_ROLE_KEY = "system_admin"


class _HasRoleKey(Protocol):
    key: str


class _HasRole(Protocol):
    role: _HasRoleKey


def is_system_admin(user: _HasRole) -> bool:
    """Kullanıcı Sistem Yöneticisi mi? Rol ANAHTARINA bakar (IZN-PLAN §1.1).

    `Role.is_system` bu soruyu cevaplamaz: Patron'da da `True`'dur (IZN-OLCUM §9). Silme,
    kilit ve "her yere evet" kuralı yalnız bu anahtarla tanınır. `user.role` yüklenmiş olmalıdır
    (`User.role` `lazy="raise"`dır; `get_current_user` onu yükler).
    """
    return user.role.key == SYSTEM_ADMIN_ROLE_KEY

import enum
import uuid
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


class Scope(str, enum.Enum):
    """Veri kapsamı — aktörün modül içinde hangi kayıtları görebildiği."""

    all = "all"
    own = "own"
    project = "project"
    finance = "finance"
    stock = "stock"
    limited = "limited"


#: 🔴 MATRİSTEN DÜŞEN KAPSAMLAR (kullanıcı kararı 2026-09-19).
#:
#: Bu üçü uygulanmıyordu ve uygulanmaları ya KUSUR ya FAZLALIK olurdu:
#:   `own`     — `GET /approvals` bilerek kapısızdır ve `_pending_filter`in
#:               "Bekçi 5"i senin AÇTIĞIN zinciri onay kutundan ZATEN çıkarır;
#:               `created_by == aktör` süzgeci o bekçiyi TERS ÇEVİRİRDİ.
#:   `project` — proje kapsamı `UserProjectAccess`ten sürülür ve ilgili iki
#:               modül onu ZATEN uygular; ikinci bir kopya bir gün ayrışırdı.
#:   `stock`   — onay kutusu zaten kişiseldir.
#:
#: Enum ÜYELERİ dokunulmadan bırakıldı (PG tipini daraltmak OpenAPI kırar ve
#: tek başına fayda üretmez); kapı bunun yerine YAZMA yolunda fail-closed
#: kapatılır. Canlı satırları `b2c3d4e5f8a1` migration'ı `all`a çeker.
#: Bekçileri: `tests/modules/test_izin_kapsami_bekcisi.py` ve
#: `tests/modules/test_role_service.py::test_DUSEN_kapsam_MEVCUT_OLSA_BILE_*`.
DROPPED_SCOPES: frozenset[Scope] = frozenset({Scope.own, Scope.project, Scope.stock})


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


class Deletable(Protocol):
    """Silinebilirliği değerlendirilecek kaydın taşıması gereken asgari alanlar."""

    created_by: uuid.UUID
    is_draft: bool


def can_delete(actor_id: uuid.UUID, level: AccessLevel, record: Deletable) -> bool:
    """Spec §5.0 silme kuralı.

    admin seviyesi her şeyi siler. Bunun dışında yalnızca taslak istisnası geçerlidir:
    kaydı aktör oluşturmuş + kayıt hâlâ taslak + aktörün en az draft seviyesi var.
    """
    if satisfies(level, AccessLevel.admin):
        return True

    return record.created_by == actor_id and record.is_draft and satisfies(level, AccessLevel.draft)


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

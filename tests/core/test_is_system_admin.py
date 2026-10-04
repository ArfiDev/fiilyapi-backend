"""IZN-B1 — `is_system_admin`: Sistem Yöneticisi rol ANAHTARINA bakar, `Role.is_system`e değil."""

from types import SimpleNamespace

from app.core.access import SYSTEM_ADMIN_ROLE_KEY, is_system_admin
from app.modules.roles.models import SYSTEM_ADMIN_KEY


def _kullanici(key: str, *, is_system: bool) -> SimpleNamespace:
    return SimpleNamespace(role=SimpleNamespace(key=key, is_system=is_system))


def test_core_literali_roles_sabitiyle_ayni() -> None:
    assert SYSTEM_ADMIN_ROLE_KEY == SYSTEM_ADMIN_KEY


def test_yalniz_system_admin_anahtari_sistem_yoneticisidir() -> None:
    assert is_system_admin(_kullanici("system_admin", is_system=True)) is True
    # Patron'da `is_system=True`dur (IZN-OLCUM §9) ama Sistem Yöneticisi DEĞİLDİR.
    assert is_system_admin(_kullanici("patron", is_system=True)) is False
    # Anahtar tek başına belirler; bayrak değil.
    assert is_system_admin(_kullanici("system_admin", is_system=False)) is True
    assert is_system_admin(_kullanici("site_chief", is_system=False)) is False

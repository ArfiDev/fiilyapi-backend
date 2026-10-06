"""TEST YARDIMCISI — modül düzeyi dilinden SAYFA hücresi yazar (IZN-B6b-T).

`_legacy_permission_yardimcisi`nin (`update_role_permission`, `sync_page_cells`, 15 kopya
`_set_permission`) yerine geçer ve eski tablo `role_permissions`a HİÇ yazmaz, HİÇ okumaz.

Çalışma şekli:
- Rolün "modül düzeyi haritası" (modül -> `AccessLevel`) `session.info` içinde tutulur
  (oturum = test; test bitince kendiliğinden gider).
- İLK dokunuşta başlangıç haritası: seed rolde `seed_data.MATRIX` / `IZN_MATRIX` düzeyleri
  (rol anahtarıyla), özel rolde 23 modülün hepsi `none`.
- Üstüne yazılır; `core/sayfalar.sayfa_matrisi` ile 100 sayfa hücresi YENİDEN üretilir
  (eski `sync_page_cells` ile aynı dönüşüm).
- `tum_tutarlar`: `None` = dokunma, `True/False` = `role_hidden_fields`te aç/kapat.
  (Kapsam kavramı (Scope) kalktı; bayrak AÇIK parametredir.)

Eski yoldan bilinçli farklar:
1. DB'deki eski satır OKUNMAZ: eski yol `get_role_matrix` ile rol_permissions'ı okurdu; testin
   o satırı ORM ile elle değiştirmesi artık hücreye yansımaz (geçişte `modul_duzeyi_yaz`a çevrilir).
2. Seed rolün başlangıç `Scope.limited` hücreleri bayrağı kendiliğinden TÜRETMEZ (eski yol
   ilk sync'te `tum_tutarlar`ı açardı, ör. site_chief); gerekirse `tum_tutarlar=True` yazılır.
3. Yeni (IZN) rollere de yazılır (eski `update_role_permission` "legacy hücre yok" diye reddederdi);
   `IZN_SAYFA_ISTISNALARI` yeniden türetmede kaybolur (eski `sync_page_cells` ile aynı).
"""

import uuid
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.errors import NotFoundError, PermissionLockedError
from app.core.sayfalar import ESKI_MODULLER, HiddenCategory, sayfa_matrisi
from app.modules.roles.models import (
    SYSTEM_ADMIN_KEY,
    Role,
    RoleHiddenField,
    RolePagePermission,
)
from app.modules.roles.seed_data import IZN_MATRIX, IZN_ROLE_ORDER, MATRIX, ROLE_ORDER

_INFO_ANAHTARI = "modul_duzeyi_haritalari"

RolRef = uuid.UUID | str | Role


async def _rol_coz(session: AsyncSession, role: RolRef) -> Role:
    if isinstance(role, Role):
        return role
    if isinstance(role, uuid.UUID):
        found = await session.get(Role, role)
    else:
        found = (await session.execute(select(Role).where(Role.key == role))).scalar_one_or_none()
    if found is None:
        raise NotFoundError("Rol bulunamadı")
    return found


def _baslangic_haritasi(role_key: str) -> dict[str, AccessLevel]:
    harita = dict.fromkeys(ESKI_MODULLER, AccessLevel.none)
    for matrix, order in ((MATRIX, ROLE_ORDER), (IZN_MATRIX, IZN_ROLE_ORDER)):
        if role_key in order:
            index = order.index(role_key)
            harita.update({modul: cells[index] for modul, cells in matrix.items()})
    return harita


def _harita(session: AsyncSession, role: Role) -> dict[str, AccessLevel]:
    haritalar: dict[uuid.UUID, dict[str, AccessLevel]] = session.info.setdefault(_INFO_ANAHTARI, {})
    if role.id not in haritalar:
        haritalar[role.id] = _baslangic_haritasi(role.key)
    return haritalar[role.id]


async def _sayfa_hucrelerini_uret(
    session: AsyncSession, role_id: uuid.UUID, harita: Mapping[str, AccessLevel]
) -> None:
    existing = {
        row.page_key: row
        for row in (
            await session.execute(
                select(RolePagePermission).where(RolePagePermission.role_id == role_id)
            )
        )
        .scalars()
        .all()
    }
    for page_key, (level, approve) in sayfa_matrisi(harita).items():
        row = existing.get(page_key)
        if row is None:
            session.add(
                RolePagePermission(
                    role_id=role_id, page_key=page_key, level=level, can_approve=approve
                )
            )
        elif row.level is not level or row.can_approve != approve:
            row.level = level
            row.can_approve = approve


async def _tum_tutarlar_ayarla(session: AsyncSession, role_id: uuid.UUID, wanted: bool) -> None:
    hidden = await session.get(RoleHiddenField, (role_id, HiddenCategory.tum_tutarlar))
    if wanted and hidden is None:
        session.add(RoleHiddenField(role_id=role_id, category=HiddenCategory.tum_tutarlar))
    elif not wanted and hidden is not None:
        await session.delete(hidden)


async def modul_duzeyleri_yaz(
    session: AsyncSession,
    role: RolRef,
    duzeyler: Mapping[str, AccessLevel],
    *,
    tum_tutarlar: bool | None = None,
) -> None:
    """Rolün birden çok modül düzeyini yazar; 100 sayfa hücresini TEK kez yeniden üretir."""
    rol = await _rol_coz(session, role)
    if rol.key == SYSTEM_ADMIN_KEY:
        raise PermissionLockedError("Sistem Yöneticisi rolünün izinleri değiştirilemez")
    bilinmeyen = set(duzeyler) - ESKI_MODULLER
    if bilinmeyen:
        raise NotFoundError(f"İzin satırı bulunamadı: {sorted(bilinmeyen)}")

    harita = _harita(session, rol)
    harita.update(duzeyler)
    await _sayfa_hucrelerini_uret(session, rol.id, harita)
    if tum_tutarlar is not None:
        await _tum_tutarlar_ayarla(session, rol.id, tum_tutarlar)
    await session.flush()


async def modul_duzeyi_yaz(
    session: AsyncSession,
    role: RolRef,
    module_key: str,
    level: AccessLevel,
    *,
    tum_tutarlar: bool | None = None,
) -> None:
    """Rolün tek modül düzeyini yazar (eski `update_role_permission` + `sync_page_cells`)."""
    await modul_duzeyleri_yaz(session, role, {module_key: level}, tum_tutarlar=tum_tutarlar)

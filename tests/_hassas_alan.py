"""TEST YARDIMCISI — rol başına hassas alan bayrakları (IZN-B4).

Üretimde yazma yolu `PUT /roles/{id}/hidden-fields`tir; testler satırı doğrudan yazar.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sayfalar import HiddenCategory, PageLevel
from app.modules.roles.models import Role, RoleHiddenField
from tests._ekip_dunyasi import rol_kur


async def gizli_alanlar_ayarla(
    session: AsyncSession, role: Role, kategoriler: Iterable[HiddenCategory]
) -> None:
    """Rolün gizli kategori kümesini TAM değiştirir (boş = hiçbiri gizli değil)."""
    await session.execute(delete(RoleHiddenField).where(RoleHiddenField.role_id == role.id))
    for kategori in set(kategoriler):
        session.add(RoleHiddenField(role_id=role.id, category=kategori))
    await session.flush()


async def rol_gizli(
    session: AsyncSession,
    key: str,
    kategoriler: Iterable[HiddenCategory],
    level: PageLevel = PageLevel.edit,
) -> Role:
    """Her sayfası `level` olan özel rol + verilen gizli kategoriler."""
    rol = await rol_kur(session, key, level)
    await gizli_alanlar_ayarla(session, rol, kategoriler)
    return rol


async def rol_gizle(session: AsyncSession, role_key: str, *kategoriler: HiddenCategory) -> None:
    """Anahtarıyla verilen (seed) rolün gizli kategori kümesini TAM değiştirir."""
    rol = (await session.execute(select(Role).where(Role.key == role_key))).scalar_one()
    await gizli_alanlar_ayarla(session, rol, kategoriler)

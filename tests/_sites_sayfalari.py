"""TEST YARDIMCISI — "sites erişimi yok" rolü kurulumu (IZN-B5c).

B5c'den sonra `GET /sites/{id}`, `/sections/{id}`, `/sites/{id}/sections` yalnız `sites`
modülünün iki sayfasıyla değil santiye.* / bolum.* iç sayfalarının Görür'üyle de açılır. Eski
testler "sites:none" rolünü yalnız modül hücresini düşürerek kuruyordu; diğer modüller (stok,
puantaj, ...) iç sayfaları açık bıraktığı için artık bu uçlar açılır. `sites_sayfalarini_kapat`
bu üç ucun TÜM sayfalarını `none` yapar (davranış iddiası gevşetilmez, yalnız rol kurulumu
tamamlanır).
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sayfalar import PageLevel
from app.modules.roles.models import Role, RolePagePermission

SITES_UC_GET_SAYFALARI: tuple[str, ...] = (
    "proje.santiyeler",
    "santiye.bolumler",
    "santiye.is_kalemleri",
    "santiye.puantaj",
    "santiye.stok",
    "santiye.hakedisler",
    "santiye.gunluk_kayit",
    "santiye.belgeler",
    "santiye.bolum_dagilimi",
    "santiye.gunluk_ozet",
    "santiye.gunluk_planlama",
    "santiye.adam_saat_butcesi",
    "santiye.planlama_paneli",
    "santiye.gunluk_ilerleme_raporu",
    "santiye.haftalik_qurr",
    "bolum.detay",
    "bolum.is_kalemleri",
    "bolum.puantaj",
    "bolum.malzeme",
    "bolum.hakedis",
    "bolum.gunluk_kayit",
    "bolum.gunluk_kayit_detay",
)


async def sites_sayfalarini_kapat(session: AsyncSession, role_key: str) -> None:
    role_id = (await session.execute(select(Role.id).where(Role.key == role_key))).scalar_one()
    await session.execute(
        update(RolePagePermission)
        .where(
            RolePagePermission.role_id == role_id,
            RolePagePermission.page_key.in_(SITES_UC_GET_SAYFALARI),
        )
        .values(level=PageLevel.none, can_approve=False)
    )
    await session.flush()

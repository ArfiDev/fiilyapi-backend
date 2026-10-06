"""IZN-B6a-me — seed bekçisi: sistem dışı HER rolde hücre kümesi == sayfa kataloğu.

Yeni sayfa kataloğa eklenirse eski rollere (üretimde) BACKFILL migration şarttır; hücre
eksikse `/auth/me` katalogdan `none` ile tamamlar ama rol ekranı ve denetim eksik hücreyi
yansıtmaz. Bu bekçi testlerin seed'inin katalogla senkron kaldığını kanıtlar.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sayfalar import SAYFA_ANAHTARLARI, SAYFALAR
from app.modules.roles import seed_data
from app.modules.roles.models import SYSTEM_ADMIN_KEY, Role, RolePagePermission


async def test_seed_sonrasi_her_rolde_hucre_anahtarlari_katalogla_birebir(
    seeded_db: AsyncSession,
) -> None:
    await seed_data.seed_izn_reference_data(seeded_db)
    roller = (await seeded_db.execute(select(Role).where(Role.key != SYSTEM_ADMIN_KEY))).scalars()
    roller = list(roller)
    assert roller  # boş küme sahte-yeşil üretmesin
    beklenen = set(SAYFA_ANAHTARLARI)
    for rol in roller:
        anahtarlar = list(
            (
                await seeded_db.execute(
                    select(RolePagePermission.page_key).where(RolePagePermission.role_id == rol.id)
                )
            ).scalars()
        )
        assert len(anahtarlar) == len(SAYFALAR), (
            f"{rol.key}: {len(anahtarlar)} hücre != katalog {len(SAYFALAR)}. "
            "Yeni sayfa eklendiyse seed + eski rollere BACKFILL migration gerekir."
        )
        assert set(anahtarlar) == beklenen, f"{rol.key}: hücre anahtarları katalogla ayrışıyor"

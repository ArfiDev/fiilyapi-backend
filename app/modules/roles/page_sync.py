"""IZN-B1 WRITE-THROUGH: eski modül hücresi değişince sayfa hücrelerini yeniden türetir.

B2 (kapı köprüsü) gelene dek eski İzin Matrisi ekranı TEK yazma yeridir ve uç kapıları hâlâ
`role_permissions`ı okur; sayfa hücreleri (`/auth/me.pages` → menü) bunu izlemezse iki model
ayrışır. Bu yüzden `update_role_permission` her yazıdan sonra bunu çağırır: rolün TÜM modül
hücrelerinden (eşikler başka modüle de bakabilir: Teklif Hazırlama "Onaylar" = `projects:admin`)
100 sayfa hücresi ve `tum_tutarlar` bayrağı yeniden türetilir — migration'la AYNI kural
(`core/sayfalar.sayfa_matrisi` / `gizli_alanlar`; migration kopyası eşitlik bekçisiyle çakılı).

Yalnız `tum_tutarlar` bayrağına dokunulur (öteki gizli kategoriler elle yönetilir).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sayfalar import HiddenCategory, gizli_alanlar, sayfa_matrisi
from app.modules.roles.models import RoleHiddenField, RolePagePermission
from app.modules.roles.repository import get_role_matrix


async def sync_page_cells(session: AsyncSession, role_id: uuid.UUID) -> None:
    matrix = await get_role_matrix(session, role_id)
    cells = {module.key: (perm.access_level, perm.scope) for module, perm in matrix}

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
    for page_key, (level, approve) in sayfa_matrisi(cells).items():
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

    wanted = HiddenCategory.tum_tutarlar in gizli_alanlar(cells)
    hidden = await session.get(RoleHiddenField, (role_id, HiddenCategory.tum_tutarlar))
    if wanted and hidden is None:
        session.add(RoleHiddenField(role_id=role_id, category=HiddenCategory.tum_tutarlar))
    elif not wanted and hidden is not None:
        await session.delete(hidden)
    await session.flush()

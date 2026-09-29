"""Disiplin kapsami BAGIMLILIGI — "bu rota disipline gore suzulur" ISARETI (DSC-B0).

Isaret mekanizmasi olarak `Depends(...)` secildi (route attribute / `openapi_extra`
DEGIL): (1) bagimlilik agaci FastAPI'nin kendi verisidir, `include_router` ile
`_IncludedRouter` sarmalarindan gecen EFEKTIF baglamda (`ctx.dependant`) da gorunur;
(2) sihirsizdir — isaret, ucun gercekten kapsami COZDUGU bagimlilikla AYNI seydir, yani
"isaretli ama suzmeyen" bir rota yazmak icin kapsami hic almamis olmak gerekir;
(3) `openapi_extra` sozlesmeyi (openapi baseline) kirletirdi, attribute ise `include_router`
kopyalamasinda sessizce kaybolabilirdi.

Kullanim (B1-B5 dilimleri): uc imzasina `scope: DisciplineScoped` ekler ve sorgusunu
`discipline_scope.item_visible_clause(scope, BoqItem)` ile suzer. Rota bekcisi
(`tests/core/test_disiplin_rota_bekcisi.py`) bu bagimliligi arar.

`RequireUnrestricted` (DSC-B2, Ü6): kisitli kullaniciya 403 veren tek bagimlilik — toplu/yapisal
islemler (grup ac/sil, gun silme, dondurma...). Kapsami `DisciplineScoped` ile cozdugu icin
bagimlilik agacinda `resolve_discipline_scope` gorunur: rota bekcisi bu rotalari da ISARETLI sayar
(imzada `scope` parametresi yoktur; "scope govdede kullanilir" kurali onlari kendiliginden atlar).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_scope import DisciplineScope, user_scope
from app.modules.users.models import User


async def resolve_discipline_scope(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> DisciplineScope:
    """Istegi yapan kullanicinin disiplin kapsami (atamasiz/kayitsiz → KISITSIZ)."""
    return await user_scope(session, user.id)


DisciplineScoped = Annotated[DisciplineScope, Depends(resolve_discipline_scope)]


async def require_unrestricted(scope: DisciplineScoped) -> None:
    """Kisitli kullaniciya 403 — govde mevcut izin kapisiyla (`permissions.require_permission`)
    BIREBIR ayni; atamasiz kullanici gecer."""
    if scope.is_restricted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Bu işlem için yetkiniz yok"
        )


RequireUnrestricted = Depends(require_unrestricted)

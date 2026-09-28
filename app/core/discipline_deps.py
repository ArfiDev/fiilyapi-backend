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

BUGUN HICBIR ROTA KULLANMAZ (B0 hicbir ucu suzmez).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

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

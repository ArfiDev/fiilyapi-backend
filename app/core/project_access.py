"""Proje ekibi erişimi (IZN-B3): görünür proje kümesinin rol süzgeci.

İki katmanlı karar (IZN-PLAN §2.2):

* Rota kapısı (`core/page_gate`): isteğin PROJE bağlamı çözülebiliyorsa o projedeki rolle,
  çözülemiyorsa yalnız ANA rolle karar verir; geçince (sayfa, bayrak) çiftlerini
  `core/gate_context`e yazar.
* Kesin karar `projects.service.visible_projects`te: kişinin YALNIZ ekibinde olduğu projelerden,
  yazılan çiftleri O PROJEDEKİ rolün karşıladıklarını döndürür. Her proje bağlamlı kaynak bu tek
  kapıdan geçtiği için (38 çağrı) kapı değişikliği tek yerde yaşar.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.gate_context import GateGroup
from app.core.page_gate import Cells, cells_of_roles, cells_satisfy


async def roles_satisfying(
    session: AsyncSession, role_by_project: dict[uuid.UUID, uuid.UUID], groups: list[GateGroup]
) -> set[uuid.UUID]:
    """`role_by_project` içinde, TÜM kapı gruplarını karşılayan projelerin kimlikleri.

    Her grup VEYA'lıdır (çiftlerden biri yeter), gruplar VE'lidir. Rolün hücresi yoksa o çift
    sağlanmamıştır (fail-closed).
    """
    if not groups:
        return set(role_by_project)
    keys = tuple({key for group in groups for key, _ in group})
    cells: dict[uuid.UUID, Cells] = await cells_of_roles(
        session, set(role_by_project.values()), keys
    )
    return {
        project_id
        for project_id, role_id in role_by_project.items()
        if all(cells_satisfy(cells[role_id], group) for group in groups)
    }

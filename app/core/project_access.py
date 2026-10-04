"""Proje ekibi erişimi (IZN-B3): rota kapısının KESİN karar bağlamı.

İki katmanlı karar (IZN-PLAN §2.2):

* Rota kapısı KABA: ana rol geçiyorsa ya da ekip rollerinden biri proje içi sayfalarla geçiyorsa
  istek içeri girer (`core/page_gate.gate_ok`).
* KESİN karar servistedir: görünen proje kümesi (`projects.service.visible_projects`) kişinin
  YALNIZ ekibinde olduğu projeleri, o projedeki rolün isteği karşılayıp karşılamadığına göre
  süzer. Her proje bağlamlı kaynak zaten bu tek kapıdan geçtiği için (38 çağrı) kapı değişikliği
  tek yerde yaşar.

Kapının gerektirdiği (sayfa, bayrak) çiftleri istek boyunca SESSION üzerinde tutulur
(`GateContext`): kapı geçince `record_gate`, `visible_projects` okur. Bağlam istek bitince (ve
başlamadan) SIFIRLANIR — testlerdeki ortak oturumda bir önceki isteğin kapısı sızmasın diye
`gate_request_scope` her isteği saran uygulama bağımlılığıdır.

Bağlam yoksa (kapısız uç: onay kutusu, AI aracı, doğrudan servis çağrısı) `visible_projects`
YALNIZ üyelik süzgecini uygular (rol düzeyi süzgeci yok): kapısız yolun kendi kapısı yoktur.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import DbSession
from app.core.page_gate import (
    Cells,
    PageFlag,
    cells_of_roles,
    cells_satisfy,
    project_pairs,
)

GATE_KEY: Final = "izn_b3.gate_context"

#: Bir kapının (VEYA'lı) çift grubu: `require_any_permission` birden çok kapıyı tek grupta toplar.
GateGroup = tuple[PageFlag, ...]


@dataclass(slots=True)
class GateContext:
    """İstek boyunca geçilen kapıların proje içi çift grupları (hepsi VE'lenir)."""

    groups: list[GateGroup] = field(default_factory=list)


def _context(session: AsyncSession) -> GateContext | None:
    return session.info.get(GATE_KEY)


def record_gate(session: AsyncSession, pairs: GateGroup) -> None:
    """Geçilen kapıyı bağlama yazar. Proje içi sayfa içermeyen kapı (şirket geneli) yazılmaz:
    onda ekip rolünün söyleyeceği bir şey yoktur."""
    scoped = project_pairs(pairs)
    if not scoped:
        return
    context = _context(session)
    if context is None:
        context = GateContext()
        session.info[GATE_KEY] = context
    context.groups.append(scoped)


def recorded_groups(session: AsyncSession) -> list[GateGroup]:
    context = _context(session)
    return list(context.groups) if context is not None else []


async def gate_request_scope(session: DbSession) -> AsyncGenerator[None, None]:
    """Uygulama bağımlılığı: istek başında ve sonunda kapı bağlamını SIFIRLAR."""
    session.info.pop(GATE_KEY, None)
    try:
        yield
    finally:
        session.info.pop(GATE_KEY, None)


async def roles_satisfying(
    session: AsyncSession, role_by_project: dict[uuid.UUID, uuid.UUID], groups: list[GateGroup]
) -> set[uuid.UUID]:
    """`role_by_project` içinde, kaydedilmiş TÜM kapı gruplarını karşılayan projelerin kimlikleri.

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

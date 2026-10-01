"""EV disiplin saglayicisi — `app.core.discipline_scope` portunun uygulamasi (DSC-B0).

Kalem → `boq_items.group_id` → `ev_group_disciplines(revision_id = R(site), boq_group_id)`
→ `discipline_id`. TEK tanim `item_discipline_expr`; `item_disciplines` ayni ifadeyi kosar.

🔴 R(site) = AKTIF ?? TASLAK (arsiv yok). `budget_service.current_revision`in TERSI ve
BILINCLI: gorunurluk calisma zamani verisiyle (gunluk dagitim, ev_input, raporlar, baseline)
tutarli olmali; taslak yeniden esleme dondurmaya kadar gorunurlugu degistirmez.

🔴 `EvRevision.site_id == item.site_id` sarti SILINEMEZ: yoksa ORDER BY/LIMIT baska
santiyenin aktif revizyonunu secer. `.correlate(item)` acik yazilir: alt sorgu dis `item`
satirina baglansin (ic revizyon alt sorgusu ayrica dis-dis sorguya baglanir).

Kayit YERI `catalog_router.py` import yan etkisidir (`register()`); dusurulurse port bos
kalir → kisit uygulanmaz (bekcisi `tests/earned_value/test_dsc_b0_discipline_scope.py`).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ColumnElement, case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import discipline_scope as port
from app.core.discipline_ref import DisciplineRef
from app.core.discipline_scope import DisciplineScope
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.catalog.models import EvDiscipline
from app.modules.earned_value.models import (
    EvGroupDiscipline,
    EvRevision,
    RevisionStatus,
    UserDiscipline,
)


def _visibility_revision(site_id_col: Any, correlate: Any) -> ColumnElement[Any]:
    """R(site): santiyenin aktif revizyonu, yoksa taslagi (arsiv yok sayilir)."""
    return (
        select(EvRevision.id)
        .where(
            EvRevision.site_id == site_id_col,
            EvRevision.status.in_([RevisionStatus.ACTIVE, RevisionStatus.DRAFT]),
        )
        .order_by(case((EvRevision.status == RevisionStatus.ACTIVE, 0), else_=1))
        .limit(1)
        .correlate(correlate)
        .scalar_subquery()
    )


def _discipline_of(group_id_col: Any, site_id_col: Any, correlate: Any) -> ColumnElement[Any]:
    """TEK SQL TANIMI: (grup kimligi, santiye kimligi) → R(site) revizyonundaki disiplin.
    Kalem ve grup ifadeleri AYNI alt sorguyu cagirir; ayrisamazlar."""
    return (
        select(EvGroupDiscipline.discipline_id)
        .where(
            EvGroupDiscipline.boq_group_id == group_id_col,
            EvGroupDiscipline.revision_id == _visibility_revision(site_id_col, correlate),
        )
        .correlate(correlate)
        .scalar_subquery()
    )


def item_discipline_expr(item: Any) -> ColumnElement[Any]:
    """Kalemin disiplini (SQL, TEK TANIM): eslenmemis grup / revizyonsuz santiye → NULL."""
    return _discipline_of(item.group_id, item.site_id, item)


def group_discipline_expr(group: Any) -> ColumnElement[Any]:
    """Grubun disiplini: kalem ifadesiyle AYNI tanim (`_discipline_of`)."""
    return _discipline_of(group.id, group.site_id, group)


class EvDisciplineProvider:
    """`DisciplineProvider` uygulamasi (durumsuz; tek ornek `PROVIDER`)."""

    async def user_scope(self, session: AsyncSession, user_id: uuid.UUID) -> DisciplineScope:
        rows = await session.execute(
            select(UserDiscipline.discipline_id).where(UserDiscipline.user_id == user_id)
        )
        return DisciplineScope.of(set(rows.scalars()))

    async def user_disciplines_detail(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> list[DisciplineRef]:
        rows = await session.execute(
            select(EvDiscipline)
            .join(UserDiscipline, UserDiscipline.discipline_id == EvDiscipline.id)
            .where(UserDiscipline.user_id == user_id)
        )
        return sorted(
            (DisciplineRef.model_validate(row) for row in rows.scalars()), key=lambda d: str(d.id)
        )

    def item_discipline_expr(self, item: Any) -> ColumnElement[Any]:
        return item_discipline_expr(item)

    def group_discipline_expr(self, group: Any) -> ColumnElement[Any]:
        return group_discipline_expr(group)

    def user_discipline_ids_subquery(self, user_id: uuid.UUID) -> Any:
        return select(UserDiscipline.discipline_id).where(UserDiscipline.user_id == user_id)

    async def item_disciplines(
        self, session: AsyncSession, item_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID | None]:
        rows = await session.execute(
            select(BoqItem.id, self.item_discipline_expr(BoqItem)).where(BoqItem.id.in_(item_ids))
        )
        found = {item_id: discipline_id for item_id, discipline_id in rows.all()}
        return {item_id: found.get(item_id) for item_id in item_ids}

    async def group_disciplines(
        self, session: AsyncSession, group_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID | None]:
        rows = await session.execute(
            select(BoqGroup.id, self.group_discipline_expr(BoqGroup)).where(
                BoqGroup.id.in_(group_ids)
            )
        )
        found = {group_id: discipline_id for group_id, discipline_id in rows.all()}
        return {group_id: found.get(group_id) for group_id in group_ids}


PROVIDER = EvDisciplineProvider()


def register() -> None:
    """Porta kaydol (idempotent). `catalog_router` import edilince cagrilir."""
    port.register_provider(PROVIDER)

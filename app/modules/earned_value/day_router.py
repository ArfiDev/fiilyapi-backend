"""Gunluk saat dagitimi + gun kilidi uclari — `/sites/{site_id}/earned-value/…`.

Kapilar: okuma VIEW · dagitim yazma WRITE (K17: "dagitim blogunu duzenleme = earned_value
yazma") · kilit acma APPROVE (§2 "yetkili gerekceyle acar"; B3'te rapor onayi da APPROVE).
Bu modul import edilince EV adaptoru cekirdek porta KAYDOLUR (`diary_adapter.register`).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_deps import DisciplineScoped, RequireUnrestricted
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.ratelimit import client_ip
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.earned_value import audit_messages as msg
from app.modules.earned_value import day_view
from app.modules.earned_value import diary_adapter as adp
from app.modules.earned_value.access import (
    APPROVE,
    VIEW,
    WRITE,
    SiteContext,
    completed_site_guard,
    visible_site,
)
from app.modules.earned_value.budget_scope import prune_tree
from app.modules.earned_value.guards import SITE_COMPLETED_DAY_READ_ONLY
from app.modules.earned_value.schemas_day import (
    AllocationSave,
    CodeIn,
    CodeNodeOut,
    DayView,
    LockOut,
    PreviousAllocationOut,
    RowPatternOut,
    ShareOut,
    UnlockBody,
)
from app.modules.users.models import User

adp.register()

router = APIRouter(tags=["earned-value"], responses=COMMON_ERROR_RESPONSES)

_User = Annotated[User, Depends(get_current_user)]
_Db = DbSession
#: Yazma uclari: gorunmeyen 404 → tamamlanmis santiye 409 → govde 422 (PLN-B3.0).
_Writable = Annotated[SiteContext, Depends(completed_site_guard(SITE_COMPLETED_DAY_READ_ONLY))]
_DAY = "/sites/{site_id}/earned-value/days/{day}"


@router.get(
    "/sites/{site_id}/earned-value/code-tree", response_model=list[CodeNodeOut], dependencies=[VIEW]
)
async def get_code_tree(
    site_id: uuid.UUID, user: _User, session: _Db, scope: DisciplineScoped
) -> list[CodeNodeOut]:
    """ "+ Is kodu ekle" secicisi: AKTIF baseline agaci (oransiz yaprak `has_rate=false`)."""
    # Kisitlida donmus agac `d:` kokunden budanir (`adp.active_tree` yazma yollarini besledigi
    # icin DEGISMEZ; budama burada, cagri yerinde).
    await visible_site(session, user, site_id)
    tree = await adp.active_tree(session, site_id)
    return day_view.code_nodes(prune_tree(tree, scope, None, True)) if tree else []


@router.get(_DAY, response_model=DayView, dependencies=[VIEW])
async def get_day(
    site_id: uuid.UUID, day: date, user: _User, session: _Db, scope: DisciplineScoped
) -> DayView:
    """Gunun dagitim baglami + kilit + ilerleme onizlemesi + Gonder kontrolu (§4.2)."""
    await visible_site(session, user, site_id)
    return await day_view.build_view(session, site_id, day, user, scope)


@router.put(f"{_DAY}/allocation", response_model=DayView, dependencies=[WRITE])
async def put_day_allocation(
    request: Request,
    site_id: uuid.UUID,
    ctx: _Writable,
    day: date,
    body: AllocationSave,
    user: _User,
    session: _Db,
    scope: DisciplineScoped,
) -> DayView:
    """Gunun saat dagitimi — TAM DEGISTIRME. Kilitli gun 409 · baseline yok 409."""
    # Kisitlida (DSC-B2) `save_allocation` BIRLESTIRIR (baska disiplinin kod/hucresine dokunmaz);
    # `unallocated_reason` govdede yoksa (model_fields_set) ortak not'a dokunulmaz (S4).
    await adp.save_allocation(
        session,
        site_id,
        day,
        user,
        [(c.node_id, c.rule) for c in body.codes],
        [adp.CellIn(c.row.kind, c.row.ref_id, c.node_id, c.hours) for c in body.cells],
        body.unallocated_reason,
        scope=scope,
        reason_provided="unallocated_reason" in body.model_fields_set,
    )
    await record_audit(
        session,
        action=AuditAction.update,
        detail=msg.day_allocation_saved(ctx.project.name, ctx.site.name, day, len(body.cells)),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await day_view.build_view(session, site_id, day, user, scope)


@router.get(
    f"{_DAY}/previous-allocation", response_model=PreviousAllocationOut, dependencies=[VIEW]
)
async def get_previous_allocation(
    site_id: uuid.UUID, day: date, user: _User, session: _Db, scope: DisciplineScoped
) -> PreviousAllocationOut:
    """B2-7 "Dunku dagilimi kopyala": son GONDERILMIS gunun deseni (satir basina pay)."""
    # Kisitlida (K4): kodlar/hucreler yalniz GORUNUR dugumler icin; `share` paydasi TAM satir
    # toplamidir (yeniden normallestirme YOK → baska disiplin payi ortuk kalir, gorunur paylar
    # toplami < 1 olabilir); gorunur hucresi olmayan satir duser.
    await visible_site(session, user, site_id)
    prev = await adp.previous_submitted_day(session, site_id, day)
    if prev is None:
        return PreviousAllocationOut(day=None, codes=[], rows=[])
    saved = await adp.load_saved(session, site_id, prev)
    visible: frozenset[str] | None = None
    if scope.is_restricted:
        visible = await adp.visible_node_ids(session, site_id, scope)
    by_row: dict[uuid.UUID, list] = {}
    for cell in saved.cells:
        by_row.setdefault(cell.row_id, []).append(cell)
    rows = []
    for r in saved.rows:
        cells = by_row.get(r.id, [])
        total = sum((c.hours for c in cells), Decimal(0))
        if total == 0:
            continue
        shown = [c for c in cells if visible is None or c.node_id in visible]
        if not shown:
            continue
        rows.append(
            RowPatternOut(
                kind=r.kind,  # type: ignore[arg-type]
                ref_id=r.personnel_id or r.subcontractor_id,  # type: ignore[arg-type]
                shares=[ShareOut(node_id=c.node_id, share=c.hours / total) for c in shown],
            )
        )
    return PreviousAllocationOut(
        day=prev,
        codes=[
            CodeIn(node_id=c.node_id, rule=c.rule)  # type: ignore[arg-type]
            for c in saved.codes
            if visible is None or c.node_id in visible
        ],
        rows=rows,
    )


@router.post(f"{_DAY}/unlock", response_model=LockOut, dependencies=[APPROVE, RequireUnrestricted])
async def unlock_day(
    request: Request,
    site_id: uuid.UUID,
    ctx: _Writable,
    day: date,
    body: UnlockBody,
    user: _User,
    session: _Db,
) -> LockOut:
    """Gun duzeyi kilit istisnasi (B2-6 b) — gerekceli; kilitli degilse 409."""
    await adp.unlock_day(session, site_id, day, user, body.reason)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=msg.day_unlocked(ctx.project.name, ctx.site.name, day, body.reason),
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return await day_view.lock_out(session, site_id, day)

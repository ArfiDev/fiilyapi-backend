"""Teklif OKUMA sorgulari (TKL-B4.2): liste, detay, revizyon detayi.

## N+1 YOK
Liste, kac teklif olursa olsun SABIT sayida sorgu calistirir (2): (1) filtreye uyan
tekliflerin SON revizyonlari (tek JOIN), (2) o revizyonlarin kalemleri (tek sorgu, ic sorguyla
secilir — IN listesi sinirina takilmaz). Toplamlar `calc` ile bellekte hesaplanir (hesap TEK
yerdedir). Sayfalama bellekte kesilir: sayfalama oncesi toplam ve durum ozeti zaten TUM filtreli
kumeyi gerektirir; kumenin olcegi (yuzlerce teklif) bunu karsilar.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import Integer, Select, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, OfferValidationError
from app.core.text import like_contains_pattern
from app.core.timezone import today
from app.modules.offers import calc
from app.modules.offers.locking import OFFER_MISSING, REVISION_MISSING
from app.modules.offers.models import (
    Offer,
    OfferGroup,
    OfferItem,
    OfferRevision,
    OfferRevisionStatus,
)
from app.modules.offers.offer_read_schemas import (
    HistoryKind,
    OfferDetailRead,
    OfferHistoryEventRead,
    OfferListItem,
    OfferListResponse,
    OfferListSummaryRead,
    OfferRevisionRead,
    OfferRevisionSummaryRead,
    OfferStatusSummaryRead,
)
from app.modules.offers.offer_views import (
    build_revision_read,
    revision_result,
    valid_until,
)
from app.modules.users.models import User

__all__ = [
    "build_offer_detail",
    "get_offer_detail",
    "get_revision_read",
    "list_offers",
    "read_revision",
]

_PCT_CENT = Decimal("0.01")
_HUNDRED = Decimal(100)
_LIKE_ESCAPE = "\\"

#: En yeni teklif once: (yil, sira) SAYISAL (`TKL-{yil}-{sira}`). `offer_no` METNE gore siralamak
#: 9999 sonrasinda bozulur (`TKL-2026-10000` < `TKL-2026-9999` metinde). Sayac monotondur ve yil
#: olusturma yilidir → bu sira olusturma sirasidir.
_NEWEST_FIRST = (
    cast(func.split_part(Offer.offer_no, "-", 2), Integer).desc(),
    cast(func.split_part(Offer.offer_no, "-", 3), Integer).desc(),
)

#: Gecmis olaylarinda ayni anda olusan olaylarin sirasi (acilis once).
_KIND_RANK: dict[str, int] = {"opened": 0, "sent": 1, "won": 2, "lost": 2, "withdrawn": 2}


def _item_input_columns() -> tuple:
    return (
        OfferItem.revision_id,
        OfferItem.quantity,
        OfferItem.unit_mhr,
        OfferItem.cost_unit_price,
        OfferItem.overhead_pct,
        OfferItem.profit_pct,
        OfferItem.offer_unit_price,
    )


def _inputs_by_revision(rows: Sequence) -> dict[uuid.UUID, list[calc.ItemInput]]:
    result: dict[uuid.UUID, list[calc.ItemInput]] = defaultdict(list)
    for revision_id, quantity, unit_mhr, cost, overhead, profit, offer_price in rows:
        result[revision_id].append(
            calc.ItemInput(
                quantity=quantity,
                unit_mhr=unit_mhr,
                cost_unit_price=cost,
                overhead_pct=overhead,
                profit_pct=profit,
                offer_unit_price=offer_price,
            )
        )
    return result


# ------------------------------------------------------------------- revizyon detayi


async def read_revision(
    session: AsyncSession, offer: Offer, revision: OfferRevision
) -> OfferRevisionRead:
    """Revizyonun tam okumasi (gruplar + kalemler + hesap). Yazma uclari da bunu dondurur."""
    latest_rev_no = await session.scalar(
        select(func.max(OfferRevision.rev_no)).where(OfferRevision.offer_id == offer.id)
    )
    groups = list(
        await session.scalars(select(OfferGroup).where(OfferGroup.revision_id == revision.id))
    )
    items = list(
        await session.scalars(select(OfferItem).where(OfferItem.revision_id == revision.id))
    )
    return build_revision_read(
        offer,
        revision,
        latest_rev_no=latest_rev_no if latest_rev_no is not None else revision.rev_no,
        groups=groups,
        items=items,
    )


async def get_revision_read(
    session: AsyncSession, offer_id: uuid.UUID, rev_no: int
) -> OfferRevisionRead:
    offer = await session.get(Offer, offer_id)
    if offer is None:
        raise NotFoundError(OFFER_MISSING)
    revision = await session.scalar(
        select(OfferRevision).where(
            OfferRevision.offer_id == offer_id, OfferRevision.rev_no == rev_no
        )
    )
    if revision is None:
        raise NotFoundError(REVISION_MISSING)
    return await read_revision(session, offer, revision)


# ------------------------------------------------------------------------ teklif detayi


def _history(
    revisions: Sequence[OfferRevision], names: dict[uuid.UUID, str]
) -> list[OfferHistoryEventRead]:
    events: list[OfferHistoryEventRead] = []
    for rev in revisions:
        stamps: list[tuple[HistoryKind, datetime | None, uuid.UUID | None]] = [
            ("opened", rev.created_at, rev.created_by_user_id),
            ("sent", rev.sent_at, rev.sent_by_user_id),
            ("won", rev.won_at, rev.won_by_user_id),
            ("lost", rev.lost_at, rev.lost_by_user_id),
            ("withdrawn", rev.withdrawn_at, rev.withdrawn_by_user_id),
        ]
        events.extend(
            OfferHistoryEventRead(
                at=at,
                kind=kind,
                rev_no=rev.rev_no,
                user_id=user_id,
                user_name=names.get(user_id) if user_id is not None else None,
            )
            for kind, at, user_id in stamps
            if at is not None
        )
    return sorted(events, key=lambda e: (e.at, e.rev_no, _KIND_RANK[e.kind]))


async def get_offer_detail(session: AsyncSession, offer_id: uuid.UUID) -> OfferDetailRead:
    offer = await session.get(Offer, offer_id)
    if offer is None:
        raise NotFoundError(OFFER_MISSING)
    return await build_offer_detail(session, offer)


async def _user_names(
    session: AsyncSession, offer: Offer, revisions: Sequence[OfferRevision]
) -> dict[uuid.UUID, str]:
    """Kunye + revizyon damgalarindaki TUM kullanicilarin adi — TEK sorgu (N+1 yok)."""
    ids: set[uuid.UUID] = set()
    if offer.prepared_by_user_id is not None:
        ids.add(offer.prepared_by_user_id)
    for rev in revisions:
        for user_id in (
            rev.created_by_user_id,
            rev.sent_by_user_id,
            rev.won_by_user_id,
            rev.lost_by_user_id,
            rev.withdrawn_by_user_id,
        ):
            if user_id is not None:
                ids.add(user_id)
    if not ids:
        return {}
    rows = await session.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
    return {user_id: name for user_id, name in rows}


async def build_offer_detail(session: AsyncSession, offer: Offer) -> OfferDetailRead:
    """Kunye + revizyon ozetleri + gecmis. 4 sorgu (teklif cagirandan gelir)."""
    revisions = list(
        await session.scalars(
            select(OfferRevision)
            .where(OfferRevision.offer_id == offer.id)
            .order_by(OfferRevision.rev_no)
            .execution_options(populate_existing=True)
        )
    )
    rows = (
        await session.execute(
            select(*_item_input_columns()).where(
                OfferItem.revision_id.in_([r.id for r in revisions])
            )
        )
    ).all()
    inputs = _inputs_by_revision(rows)
    summaries: list[OfferRevisionSummaryRead] = []
    for rev in revisions:
        result = revision_result(rev, inputs.get(rev.id, []))
        summaries.append(
            OfferRevisionSummaryRead(
                rev_no=rev.rev_no,
                status=rev.status,
                offer_date=rev.offer_date,
                valid_until=valid_until(rev),
                created_at=rev.created_at,
                updated_at=rev.updated_at,
                sent_at=rev.sent_at,
                won_at=rev.won_at,
                lost_at=rev.lost_at,
                withdrawn_at=rev.withdrawn_at,
                lost_reason=rev.lost_reason,
                winning_amount=rev.winning_amount,
                net=result.customer.net,
                gross=result.customer.gross,
                unpriced_count=result.unpriced_count,
            )
        )
    last = revisions[-1]
    names = await _user_names(session, offer, revisions)
    return OfferDetailRead(
        id=offer.id,
        offer_no=offer.offer_no,
        employer_id=offer.employer_id,
        employer_name=offer.employer_name,
        title=offer.title,
        scope_summary=offer.scope_summary,
        prepared_by_user_id=offer.prepared_by_user_id,
        prepared_by_name=(
            names.get(offer.prepared_by_user_id) if offer.prepared_by_user_id is not None else None
        ),
        status=last.status,
        latest_rev_no=last.rev_no,
        created_at=offer.created_at,
        updated_at=offer.updated_at,
        revisions=summaries,
        history=_history(revisions, names),
    )


# ---------------------------------------------------------------------------- liste


def _latest_revision_stmt(
    q: str | None,
    employer_id: uuid.UUID | None,
    date_from: date | None,
    date_to: date | None,
) -> Select[tuple[Offer, OfferRevision]]:
    latest = (
        select(func.max(OfferRevision.rev_no))
        .where(OfferRevision.offer_id == Offer.id)
        .correlate(Offer)
        .scalar_subquery()
    )
    stmt = (
        select(Offer, OfferRevision)
        .join(
            OfferRevision,
            and_(OfferRevision.offer_id == Offer.id, OfferRevision.rev_no == latest),
        )
        .order_by(*_NEWEST_FIRST)
    )
    if employer_id is not None:
        stmt = stmt.where(Offer.employer_id == employer_id)
    if date_from is not None:  # son revizyonun teklif tarihi, dahil-dahil
        stmt = stmt.where(OfferRevision.offer_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(OfferRevision.offer_date <= date_to)
    if q is not None and q.strip():
        pattern = like_contains_pattern(q.strip())
        stmt = stmt.where(
            or_(
                Offer.offer_no.ilike(pattern, escape=_LIKE_ESCAPE),
                Offer.title.ilike(pattern, escape=_LIKE_ESCAPE),
                Offer.employer_name.ilike(pattern, escape=_LIKE_ESCAPE),
            )
        )
    return stmt


def _win_rate(won: int, lost: int) -> Decimal | None:
    decided = won + lost
    if decided == 0:
        return None
    return (Decimal(won) * _HUNDRED / Decimal(decided)).quantize(_PCT_CENT, rounding=ROUND_HALF_UP)


async def list_offers(
    session: AsyncSession,
    *,
    status: OfferRevisionStatus | None,
    q: str | None,
    employer_id: uuid.UUID | None,
    offer_date_from: date | None,
    offer_date_to: date | None,
    limit: int,
    offset: int,
) -> OfferListResponse:
    if offer_date_from and offer_date_to and offer_date_from > offer_date_to:
        raise OfferValidationError("Başlangıç tarihi bitiş tarihinden sonra olamaz")
    base = _latest_revision_stmt(q, employer_id, offer_date_from, offer_date_to)
    rows = (await session.execute(base)).all()
    revision_ids = base.with_only_columns(OfferRevision.id).order_by(None)
    item_rows = (
        await session.execute(
            select(*_item_input_columns()).where(OfferItem.revision_id.in_(revision_ids))
        )
    ).all()
    inputs = _inputs_by_revision(item_rows)

    entries: list[tuple[Offer, OfferRevision, calc.RevisionResult]] = [
        (offer, rev, revision_result(rev, inputs.get(rev.id, []))) for offer, rev in rows
    ]
    counts: dict[OfferRevisionStatus, int] = dict.fromkeys(OfferRevisionStatus, 0)
    nets: dict[OfferRevisionStatus, Decimal] = dict.fromkeys(OfferRevisionStatus, Decimal(0))
    for _offer, rev, result in entries:
        counts[rev.status] += 1
        nets[rev.status] += result.customer.net
    current_day = today()
    expired = sum(
        1
        for _offer, rev, _result in entries
        if rev.status == OfferRevisionStatus.sent and valid_until(rev) < current_day
    )
    summary = OfferListSummaryRead(
        expired_count=expired,
        by_status=[
            OfferStatusSummaryRead(status=s, count=counts[s], net=nets[s])
            for s in OfferRevisionStatus
        ],
        win_rate=_win_rate(counts[OfferRevisionStatus.won], counts[OfferRevisionStatus.lost]),
    )
    shown = [e for e in entries if status is None or e[1].status == status]
    page = shown[offset : offset + limit]
    return OfferListResponse(
        items=[
            OfferListItem(
                id=offer.id,
                offer_no=offer.offer_no,
                rev_no=rev.rev_no,
                title=offer.title,
                employer_id=offer.employer_id,
                employer_name=offer.employer_name,
                scope_summary=offer.scope_summary,
                offer_date=rev.offer_date,
                valid_until=valid_until(rev),
                status=rev.status,
                net=result.customer.net,
                gross=result.customer.gross,
                unpriced_count=result.unpriced_count,
                created_at=offer.created_at,
            )
            for offer, rev, result in page
        ],
        total=len(shown),
        limit=limit,
        offset=offset,
        summary=summary,
    )

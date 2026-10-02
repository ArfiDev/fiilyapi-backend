"""Teklif yazim kilidi + revizyon cozumleme (TKL-B4.2).

🔴 KANON: teklifin HER yazimi (kunye, revizyon, gecis, grup, kalem, silme, yeni revizyon)
ONCE `lock_offer` ile teklif satirini `FOR UPDATE` kilitler; durum kontrolu KILIT ALTINDA
yapilir. Boylece eszamanli iki yazim sirayla kosar: ikincisi birincinin commit'ini bekler ve
revizyonun TAZE durumunu okur (gonderilmis revizyona kalem yazilamaz; iki yeni revizyon ayni
`rev_no`yu almaz). Kilit YALNIZ teklif satirindadir — revizyon/kalem satirlarina ayri kilit
yoktur (tek kilit = dongu yok).

Revizyon okumalari `populate_existing` ile TAZE okur (oturumun kimlik haritasinda bayat kopya
olsa bile kilit ALTINDA yeniden okunur; `catalog.service.lock_discipline` emsali).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.modules.offers.models import Offer, OfferRevision, OfferRevisionStatus

OFFER_MISSING = "Teklif bulunamadı"
REVISION_MISSING = "Teklif revizyonu bulunamadı"
NOT_LATEST_REVISION = "Yalnız en son revizyon üzerinde işlem yapılabilir"
REVISION_NOT_DRAFT = (
    "Revizyon taslak değil; içerik yalnız taslak revizyonda değiştirilebilir. "
    "Değişiklik için yeni revizyon açın"
)


async def lock_offer(session: AsyncSession, offer_id: uuid.UUID) -> Offer:
    """Teklif satirini `FOR UPDATE` kilitler ve TAZE okur; yoksa 404."""
    offer = await session.scalar(
        select(Offer)
        .where(Offer.id == offer_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if offer is None:
        raise NotFoundError(OFFER_MISSING)
    return offer


async def latest_revision(session: AsyncSession, offer_id: uuid.UUID) -> OfferRevision:
    """Son revizyon (en buyuk `rev_no`), TAZE okunur. Her teklifin Rev.0'i vardir."""
    revision = await session.scalar(
        select(OfferRevision)
        .where(OfferRevision.offer_id == offer_id)
        .order_by(OfferRevision.rev_no.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if revision is None:  # pragma: no cover - teklif Rev.0'siz olusamaz
        raise NotFoundError(REVISION_MISSING)
    return revision


async def lock_latest_revision(
    session: AsyncSession, offer_id: uuid.UUID, rev_no: int | None = None
) -> tuple[Offer, OfferRevision]:
    """Kilitle + son revizyonu coz. `rev_no` verilirse: yoksa 404, son degilse 409."""
    offer = await lock_offer(session, offer_id)
    revision = await latest_revision(session, offer_id)
    if rev_no is not None and rev_no != revision.rev_no:
        if rev_no < 0 or rev_no > revision.rev_no:
            raise NotFoundError(REVISION_MISSING)
        raise ConflictError(NOT_LATEST_REVISION)
    return offer, revision


def touch_revision(revision: OfferRevision) -> None:
    """Son KAYIT zamanini ilerletir (kosul/grup/kalem yazimi + gecis; OKUMA cagirmaz)."""
    revision.updated_at = datetime.now(UTC)


async def lock_draft_revision(
    session: AsyncSession, offer_id: uuid.UUID, rev_no: int | None = None
) -> tuple[Offer, OfferRevision]:
    """Icerik yazimi kapisi: kilitle, SON revizyon olmali ve `draft` olmali (aksi 409)."""
    offer, revision = await lock_latest_revision(session, offer_id, rev_no)
    if revision.status != OfferRevisionStatus.draft:
        raise ConflictError(REVISION_NOT_DRAFT)
    return offer, revision

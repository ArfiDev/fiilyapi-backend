"""Teklif yazma mantigi: olustur, kunye, kosullar, sil, yeni revizyon, durum gecisleri (TKL-B4.2).

Servis COMMIT ETMEZ (tek commit istek sonunda). Denetim satirlari ROUTER isidir. Her yazma
`locking.lock_offer` ile teklif satirini kilitler ve durum kontrolunu kilit ALTINDA yapar.
Kalem/grup yazimi `item_service.py`dedir.

## Durum makinesi (revizyon basina, T16/T31, SO-1/SO-2)
`draft → sent → won | lost` · `draft | sent → withdrawn` · `withdrawn` SON durum · taslaktan
dogrudan `won` YOK · `won`/`lost`/`withdrawn`dan cikis yok (kayip icin YENI REVIZYON acilir).
Gecisler YALNIZ en son revizyonda.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, OfferValidationError
from app.core.timezone import today
from app.modules.offers import locking, numbering
from app.modules.offers.models import (
    Offer,
    OfferGroup,
    OfferItem,
    OfferPriceEscalation,
    OfferRevision,
    OfferRevisionStatus,
)
from app.modules.offers.offer_schemas import (
    OfferCreate,
    OfferLoseRequest,
    OfferRevisionUpdate,
    OfferUpdate,
)
from app.modules.offers.service import get_settings
from app.modules.offers.settings_audit import snapshot_conditions
from app.modules.projects.models import Employer
from app.modules.users.models import User

EMPLOYER_MISSING = "İşveren bulunamadı"
DELETE_NOT_ALLOWED = "Teklif yalnız tek revizyonlu ve taslak iken silinebilir"
NEW_REVISION_NOT_ALLOWED = (
    "Yeni revizyon yalnız son revizyon gönderilmiş ya da kaybedilmiş iken açılabilir"
)
INDEX_REQUIRED = "Fiyat farkı «TÜİK endeksli» iken endeks türü zorunludur"
INDEX_NOT_ALLOWED = "Sabit fiyatta endeks türü girilemez"
NO_ITEMS_TO_SEND = "Teklifte kalem yok"
IMMUTABLE_NOTE = "Künye yalnız son revizyon taslak iken değiştirilebilir"

_STATUS_LABEL = {
    OfferRevisionStatus.draft: "taslak",
    OfferRevisionStatus.sent: "gönderilmiş",
    OfferRevisionStatus.won: "kazanılmış",
    OfferRevisionStatus.lost: "kaybedilmiş",
    OfferRevisionStatus.withdrawn: "vazgeçilmiş",
}


class OfferAction(str, enum.Enum):
    send = "send"
    win = "win"
    lose = "lose"
    withdraw = "withdraw"


#: Eylem → (izinli kaynak durumlar, hedef durum, denetim fiili).
TRANSITIONS: dict[OfferAction, tuple[frozenset[OfferRevisionStatus], OfferRevisionStatus, str]] = {
    OfferAction.send: (
        frozenset({OfferRevisionStatus.draft}),
        OfferRevisionStatus.sent,
        "gönderildi",
    ),
    OfferAction.win: (
        frozenset({OfferRevisionStatus.sent}),
        OfferRevisionStatus.won,
        "kazanıldı",
    ),
    OfferAction.lose: (
        frozenset({OfferRevisionStatus.sent}),
        OfferRevisionStatus.lost,
        "kaybedildi",
    ),
    OfferAction.withdraw: (
        frozenset({OfferRevisionStatus.draft, OfferRevisionStatus.sent}),
        OfferRevisionStatus.withdrawn,
        "vazgeçildi",
    ),
}

#: Yeni revizyon acilabilen durumlar (T36).
_REVISABLE = frozenset({OfferRevisionStatus.sent, OfferRevisionStatus.lost})


def _now() -> datetime:
    return datetime.now(UTC)


def check_escalation(escalation: OfferPriceEscalation, index_type: object | None) -> None:
    """`tuik` ⇔ endeks turu dolu (DB CHECK'in Turkce 422 karsiligi)."""
    if escalation == OfferPriceEscalation.tuik and index_type is None:
        raise OfferValidationError(INDEX_REQUIRED)
    if escalation == OfferPriceEscalation.fixed and index_type is not None:
        raise OfferValidationError(INDEX_NOT_ALLOWED)


async def _get_employer(session: AsyncSession, employer_id: uuid.UUID) -> Employer:
    employer = await session.get(Employer, employer_id)
    if employer is None:
        raise NotFoundError(EMPLOYER_MISSING)
    return employer


# --------------------------------------------------------------------------- olustur


async def create_offer(session: AsyncSession, user: User, data: OfferCreate) -> Offer:
    """Teklif + Rev.0 taslak. Kosullar govdede yoksa `offer_settings`ten KOPYALANIR."""
    employer = await _get_employer(session, data.employer_id)
    check_escalation(data.price_escalation, data.price_index_type)
    settings = await get_settings(session)
    given = data.model_fields_set

    offer = Offer(
        offer_no=await numbering.next_offer_no(session, year=numbering.current_offer_year()),
        employer_id=employer.id,
        employer_name=employer.name,
        title=data.title,
        scope_summary=data.scope_summary,
        prepared_by_user_id=user.id,
    )
    session.add(offer)
    await session.flush()

    def pick(name: str, default: Any) -> Any:
        value = getattr(data, name)
        return default if value is None else value

    session.add(
        OfferRevision(
            offer_id=offer.id,
            rev_no=0,
            status=OfferRevisionStatus.draft,
            offer_date=pick("offer_date", today()),
            validity_days=pick("validity_days", settings.default_validity_days),
            overhead_pct=pick("overhead_pct", settings.default_overhead_pct),
            profit_pct=pick("profit_pct", settings.default_profit_pct),
            vat_pct=pick("vat_pct", settings.default_vat_pct),
            payment_terms=(
                data.payment_terms if "payment_terms" in given else settings.default_payment_terms
            ),
            delivery_days=data.delivery_days,
            price_escalation=data.price_escalation,
            price_index_type=data.price_index_type,
            notes=data.notes,
            created_at=_now(),
            updated_at=_now(),
            created_by_user_id=user.id,
        )
    )
    await session.flush()
    return offer


# ---------------------------------------------------------------------------- kunye


async def update_offer(
    session: AsyncSession, user: User, offer_id: uuid.UUID, data: OfferUpdate
) -> tuple[Offer, bool]:
    """Kunye (isveren, is adi, kapsam ozeti) — yalniz SON revizyon `draft` iken (SO-3).
    `(teklif, degisti_mi)` doner: hicbir alan fiilen degismediyse yazilmaz, denetim satiri da
    yazilmaz (R2a)."""
    offer, _revision = await locking.lock_draft_revision(session, offer_id)
    changes = data.model_dump(exclude_unset=True)
    employer = None
    if "employer_id" in changes:
        employer = await _get_employer(session, changes["employer_id"])
    changed = False
    if employer is not None and employer.id != offer.employer_id:
        offer.employer_id = employer.id
        offer.employer_name = employer.name
        changed = True
    for field in ("title", "scope_summary"):
        if field in changes and changes[field] != getattr(offer, field):
            setattr(offer, field, changes[field])
            changed = True
    if changed:
        offer.updated_at = _now()
        await session.flush()
    return offer, changed


# -------------------------------------------------------------------------- kosullar


async def update_revision(
    session: AsyncSession,
    user: User,
    offer_id: uuid.UUID,
    rev_no: int,
    data: OfferRevisionUpdate,
) -> tuple[Offer, OfferRevision, dict[str, Any]]:
    """Kosullar. `(teklif, revizyon, ESKI kosullar)` doner (denetim `eski → yeni` farki icin);
    hicbir alan fiilen degismediyse yazilmaz."""
    offer, revision = await locking.lock_draft_revision(session, offer_id, rev_no)
    before = snapshot_conditions(revision)
    changes = data.model_dump(exclude_unset=True)
    if (
        changes.get("price_escalation") == OfferPriceEscalation.fixed
        and "price_index_type" not in changes
    ):
        changes["price_index_type"] = None  # sabit fiyata gecerken endeks turu kendiliginden duser
    escalation = changes.get("price_escalation", revision.price_escalation)
    index_type = changes.get("price_index_type", revision.price_index_type)
    check_escalation(escalation, index_type)
    effective = {field: value for field, value in changes.items() if before[field] != value}
    if not effective:
        return offer, revision, before
    for field, value in effective.items():
        setattr(revision, field, value)
    locking.touch_revision(revision)
    offer.updated_at = _now()
    await session.flush()
    await session.refresh(revision)  # yanit GET ile ayni bicim (DB'nin Numeric olcegi) — R3
    return offer, revision, before


# ----------------------------------------------------------------------------- sil


async def delete_offer(session: AsyncSession, user: User, offer_id: uuid.UUID) -> Offer:
    """Yalniz TEK revizyonlu ve o `draft` ise. Numara geri kullanilmaz (sayac monoton)."""
    offer, revision = await locking.lock_latest_revision(session, offer_id)
    revision_count = await session.scalar(
        select(func.count()).select_from(OfferRevision).where(OfferRevision.offer_id == offer_id)
    )
    if revision_count != 1 or revision.status != OfferRevisionStatus.draft:
        raise ConflictError(DELETE_NOT_ALLOWED)
    await session.delete(offer)
    await session.flush()
    return offer


# ------------------------------------------------------------------- yeni revizyon


async def create_revision(
    session: AsyncSession, user: User, offer_id: uuid.UUID
) -> tuple[Offer, OfferRevision]:
    """Yeni revizyon = onceki revizyonun KOPYASI (T36): kosullar + oranlar + gruplar + kalemler
    (fiyat/oran/adam-saat/elle B.F. dahil). Yeni `draft`, `rev_no` = max + 1 (teklif satiri
    kilitli + UQ). Kopyada kalemin grubu YENI revizyondaki karsilik grubuna eslenir."""
    offer, previous = await locking.lock_latest_revision(session, offer_id)
    if previous.status not in _REVISABLE:
        raise ConflictError(NEW_REVISION_NOT_ALLOWED)

    revision = OfferRevision(
        id=uuid.uuid4(),
        offer_id=offer.id,
        rev_no=previous.rev_no + 1,
        status=OfferRevisionStatus.draft,
        offer_date=today(),
        validity_days=previous.validity_days,
        overhead_pct=previous.overhead_pct,
        profit_pct=previous.profit_pct,
        vat_pct=previous.vat_pct,
        payment_terms=previous.payment_terms,
        delivery_days=previous.delivery_days,
        price_escalation=previous.price_escalation,
        price_index_type=previous.price_index_type,
        notes=previous.notes,
        created_at=_now(),
        updated_at=_now(),
        created_by_user_id=user.id,
    )
    session.add(revision)
    await session.flush()

    group_map: dict[uuid.UUID, uuid.UUID] = {}
    for group in await session.scalars(
        select(OfferGroup).where(OfferGroup.revision_id == previous.id)
    ):
        new_id = uuid.uuid4()
        group_map[group.id] = new_id
        session.add(
            OfferGroup(
                id=new_id,
                revision_id=revision.id,
                name=group.name,
                sort_order=group.sort_order,
            )
        )
    await session.flush()

    for item in await session.scalars(
        select(OfferItem).where(OfferItem.revision_id == previous.id)
    ):
        session.add(
            OfferItem(
                revision_id=revision.id,
                group_id=group_map[item.group_id],
                sort_order=item.sort_order,
                catalog_item_id=item.catalog_item_id,
                poz_no=item.poz_no,
                description=item.description,
                unit=item.unit,
                quantity=item.quantity,
                unit_mhr=item.unit_mhr,
                cost_unit_price=item.cost_unit_price,
                overhead_pct=item.overhead_pct,
                profit_pct=item.profit_pct,
                offer_unit_price=item.offer_unit_price,
            )
        )
    offer.updated_at = _now()
    await session.flush()
    return offer, revision


# --------------------------------------------------------------------- durum gecisi


async def transition(
    session: AsyncSession,
    user: User,
    offer_id: uuid.UUID,
    rev_no: int,
    action: OfferAction,
    lose: OfferLoseRequest | None = None,
) -> tuple[Offer, OfferRevision]:
    """Durum gecisi — kilit altinda: yalniz SON revizyon, yalniz izinli kaynak durum."""
    offer, revision = await locking.lock_latest_revision(session, offer_id, rev_no)
    allowed_from, target, _verb = TRANSITIONS[action]
    if revision.status not in allowed_from:
        raise ConflictError(
            f"Revizyon {_STATUS_LABEL[revision.status]} durumda; bu işlem yapılamaz"
        )
    if action is OfferAction.send:
        # Kalemsiz teklif gonderilmez (SO-9); kontrol teklif satiri kilidi ALTINDA. Fiyatsiz
        # kalemli revizyon gonderilebilir.
        has_items = await session.scalar(
            select(OfferItem.id).where(OfferItem.revision_id == revision.id).limit(1)
        )
        if has_items is None:
            raise OfferValidationError(NO_ITEMS_TO_SEND)
    now = _now()
    revision.status = target
    if action is OfferAction.send:
        revision.sent_at, revision.sent_by_user_id = now, user.id
    elif action is OfferAction.win:
        revision.won_at, revision.won_by_user_id = now, user.id
    elif action is OfferAction.lose:
        revision.lost_at, revision.lost_by_user_id = now, user.id
        if lose is not None:
            revision.lost_reason = lose.lost_reason or None
            revision.winning_amount = lose.winning_amount
    else:
        revision.withdrawn_at, revision.withdrawn_by_user_id = now, user.id
    revision.updated_at = now
    offer.updated_at = now
    await session.flush()
    return offer, revision

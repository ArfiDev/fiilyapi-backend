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

import dataclasses
import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, OfferValidationError
from app.core.timezone import today
from app.modules.offers import locking, numbering, offer_seed, template_service
from app.modules.offers.models import (
    Offer,
    OfferItem,
    OfferPriceEscalation,
    OfferRevision,
    OfferRevisionStatus,
)
from app.modules.offers.offer_schemas import (
    OfferCopySource,
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
UNQUANTIFIED_ITEMS = "Miktarı girilmemiş kalem var"
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


@dataclass(frozen=True, slots=True)
class OfferOrigin:
    """Teklifin kaynagi (denetim metni icin): bos | sablon | kopya."""

    template_name: str | None = None
    source_offer_no: str | None = None
    source_rev_no: int | None = None


@dataclass(frozen=True, slots=True)
class _Defaults:
    """Kosul varsayilanlari (govde alani YOKSA kullanilir): ayar | sablon+ayar | kaynak revizyon."""

    validity_days: int
    overhead_pct: Any
    profit_pct: Any
    vat_pct: Any
    payment_terms: str | None
    delivery_days: int | None
    price_escalation: OfferPriceEscalation
    price_index_type: Any
    notes: str | None


async def _lock_copy_source(
    session: AsyncSession, source: OfferCopySource
) -> tuple[Offer, OfferRevision]:
    """Kopya kaynagi: teklif satiri `FOR SHARE` (kaynak kopya sirasinda degismez/silinmez;
    eszamanli kopyalar birbirini BEKLETMEZ). Kaynak yoksa 404."""
    offer = await session.scalar(
        select(Offer)
        .where(Offer.id == source.offer_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if offer is None:
        raise NotFoundError(locking.OFFER_MISSING)
    revision = await session.scalar(
        select(OfferRevision)
        .where(OfferRevision.offer_id == offer.id, OfferRevision.rev_no == source.rev_no)
        .execution_options(populate_existing=True)
    )
    if revision is None:
        raise NotFoundError(locking.REVISION_MISSING)
    return offer, revision


async def create_offer(session: AsyncSession, user: User, data: OfferCreate) -> Offer:
    offer, _origin = await create_offer_with_origin(session, user, data)
    return offer


async def create_offer_with_origin(
    session: AsyncSession, user: User, data: OfferCreate
) -> tuple[Offer, OfferOrigin]:
    """Teklif + Rev.0 taslak. Kosullar govdede yoksa `offer_settings`ten KOPYALANIR.

    Kaynak (en fazla biri): `template_id` → sablonun gruplari + kalemleri, MIKTAR BOS, oranlar
    govde ?? sablon ?? ayar (B5.1); `copy_from` → kaynak revizyonun kosullari + oranlari +
    kalemleri birebir, kunye govde ?? kaynak (SO-8). Kaynak teklif/revizyon/sablon DEGISMEZ.
    """
    settings = await get_settings(session)
    given = data.model_fields_set
    template = None
    source_offer = source_rev = None
    defaults = _Defaults(
        validity_days=settings.default_validity_days,
        overhead_pct=settings.default_overhead_pct,
        profit_pct=settings.default_profit_pct,
        vat_pct=settings.default_vat_pct,
        payment_terms=settings.default_payment_terms,
        delivery_days=None,
        price_escalation=OfferPriceEscalation.fixed,
        price_index_type=None,
        notes=None,
    )
    if data.template_id is not None:
        template = await template_service.lock_template_shared(session, data.template_id)
        defaults = dataclasses.replace(
            defaults,
            overhead_pct=(
                template.overhead_pct
                if template.overhead_pct is not None
                else defaults.overhead_pct
            ),
            profit_pct=(
                template.profit_pct if template.profit_pct is not None else defaults.profit_pct
            ),
        )
    if data.copy_from is not None:
        source_offer, source_rev = await _lock_copy_source(session, data.copy_from)
        defaults = _Defaults(
            validity_days=source_rev.validity_days,
            overhead_pct=source_rev.overhead_pct,
            profit_pct=source_rev.profit_pct,
            vat_pct=source_rev.vat_pct,
            payment_terms=source_rev.payment_terms,
            delivery_days=source_rev.delivery_days,
            price_escalation=source_rev.price_escalation,
            price_index_type=source_rev.price_index_type,
            notes=source_rev.notes,
        )

    employer_id = data.employer_id
    if employer_id is None and source_offer is not None:
        employer_id = source_offer.employer_id
    employer = await _get_employer(session, employer_id)  # type: ignore[arg-type]
    if "price_escalation" in given:
        escalation, index_type = data.price_escalation, data.price_index_type
    else:
        escalation = defaults.price_escalation
        index_type = (
            data.price_index_type if "price_index_type" in given else defaults.price_index_type
        )
    check_escalation(escalation, index_type)

    offer = Offer(
        offer_no=await numbering.next_offer_no(session, year=numbering.current_offer_year()),
        employer_id=employer.id,
        employer_name=employer.name,
        title=data.title if data.title is not None else source_offer.title,  # type: ignore[union-attr]
        scope_summary=(
            data.scope_summary
            if source_offer is None or "scope_summary" in given
            else source_offer.scope_summary
        ),
        prepared_by_user_id=user.id,
        template_id=template.id if template is not None else None,
    )
    session.add(offer)
    await session.flush()

    def pick(name: str, default: Any) -> Any:
        value = getattr(data, name)
        return default if value is None else value

    def explicit(name: str, default: Any) -> Any:
        return getattr(data, name) if name in given else default

    revision = OfferRevision(
        offer_id=offer.id,
        rev_no=0,
        status=OfferRevisionStatus.draft,
        offer_date=pick("offer_date", today()),
        validity_days=pick("validity_days", defaults.validity_days),
        overhead_pct=pick("overhead_pct", defaults.overhead_pct),
        profit_pct=pick("profit_pct", defaults.profit_pct),
        vat_pct=pick("vat_pct", defaults.vat_pct),
        payment_terms=explicit("payment_terms", defaults.payment_terms),
        delivery_days=explicit("delivery_days", defaults.delivery_days),
        price_escalation=escalation,
        price_index_type=index_type,
        notes=explicit("notes", defaults.notes),
        created_at=_now(),
        updated_at=_now(),
        created_by_user_id=user.id,
    )
    session.add(revision)
    await session.flush()

    if template is not None:
        await offer_seed.seed_from_template(
            session, template_id=template.id, target_revision_id=revision.id
        )
    if source_rev is not None and source_offer is not None:
        await offer_seed.copy_content(
            session, source_revision_id=source_rev.id, target_revision_id=revision.id
        )
    origin = OfferOrigin(
        template_name=template.name if template is not None else None,
        source_offer_no=source_offer.offer_no if source_offer is not None else None,
        source_rev_no=source_rev.rev_no if source_rev is not None else None,
    )
    return offer, origin


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

    await offer_seed.copy_content(
        session, source_revision_id=previous.id, target_revision_id=revision.id
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
        # SO-21: miktari girilmemis kalem varken gonderilmez (taslakta serbest); ayni kilit ALTINDA.
        unquantified = await session.scalar(
            select(OfferItem.id)
            .where(OfferItem.revision_id == revision.id, OfferItem.quantity.is_(None))
            .limit(1)
        )
        if unquantified is not None:
            raise OfferValidationError(UNQUANTIFIED_ITEMS)
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

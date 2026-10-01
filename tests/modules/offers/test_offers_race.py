"""TKL-B4.2 — eszamanli teklif yazimlari: HER yazim ONCE teklif satirini `FOR UPDATE` kilitler.

Emsal: `tests/modules/catalog/test_poz_no_race.py` (tutulan-kilit + `pg_stat_activity` bariyeri +
POZITIF KONTROL). Kurgu: tek kullanimlik yaris DB'si; oturum 1 yazar ve COMMIT ETMEDEN bekler;
oturum 2 baslar, beklemesi `pg_stat_activity`den OLCULUR; sonra 1 commit eder.

(a) iki ESZAMANLI "yeni revizyon": kilitli → ikincisi `offers ... FOR UPDATE`te BEKLER, birincinin
    commit'inden sonra son revizyonun (Rev.1 taslak) TAZE durumunu okur → 409; cift Rev YOK.
    KONTROL (kilitsiz okuma): ikisi de Rev.0'i `sent` okur, ikisi `rev_no = 1` yazar →
    `uq_offer_revisions_offer_rev` IntegrityError.
(b) eszamanli `send` + kalem ekleme: kilitli → kalem `offers ... FOR UPDATE`te BEKLER, send
    commit olunca revizyonu `sent` okur → 409, gonderilmis revizyona kalem YAZILAMAZ.
    KONTROL (kilitsiz): kalem BAYAT `draft` okur, yazar; send commit olunca revizyon `sent` ve
    ICINDE kalem var (degismez IHLALI) — bariyer `UPDATE offer_revisions`te (son kayit zamani).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.main  # noqa: F401  (tum modeller metadata'ya girsin)
from app.core.db import Base
from app.core.errors import ConflictError, NotFoundError
from app.modules.catalog.models import ContractorType, EvCatalogItem, EvDiscipline
from app.modules.offers import item_service, locking, offer_service
from app.modules.offers.models import Offer, OfferItem, OfferRevision
from app.modules.offers.offer_schemas import OfferCreate, OfferGroupCreate, OfferItemCreate
from app.modules.offers.offer_service import OfferAction
from app.modules.projects.models import Employer
from app.modules.roles.models import Role
from app.modules.users.models import User
from tests.earned_value_budget.test_budget_concurrency import (
    _BEKLEME_SINIRI,
    _KESISME_PAYI,
    _admin,
    _bekleyen_sorgu,
    _sonlandir,
    _sqlalchemy_dsn,
)

pytestmark = pytest.mark.asyncio


@dataclass(frozen=True, slots=True)
class _Ortam:
    database: str
    engine: AsyncEngine
    Session: async_sessionmaker[AsyncSession]
    user_id: uuid.UUID
    offer_id: uuid.UUID
    group_id: uuid.UUID
    catalog_id: uuid.UUID


@asynccontextmanager
async def _ortam(*, gonderilmis: bool):
    """Tek kullanimlik DB + COMMIT'li zemin: teklif (Rev.0 `draft`/`sent`) + grup + katalog."""
    database = f"offer_yaris_{uuid.uuid4().hex[:8]}"
    await _admin(f'CREATE DATABASE "{database}"')
    engine = create_async_engine(_sqlalchemy_dsn(database))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            role = Role(key="offer_yaris", name="Teklif Yarış Rolü")
            s.add(role)
            await s.flush()
            user = User(
                email="yaris@teklif.co", password_hash="x", full_name="Yarış", role_id=role.id
            )
            employer = Employer(name="Yarış İşveren")
            disiplin = EvDiscipline(
                code="YRS",
                name="Yarış",
                color="#2563eb",
                default_contractor_type=ContractorType.OWN,
            )
            s.add_all([user, employer, disiplin])
            await s.flush()
            kalem = EvCatalogItem(
                poz_no="YRS-0001",
                discipline_id=disiplin.id,
                name="Beton",
                uom="m3",
                standard_unit_mhr=Decimal("1.5"),
                default_contractor_type=ContractorType.OWN,
                ref_price=Decimal("100"),
            )
            s.add(kalem)
            await s.flush()
            offer = await offer_service.create_offer(
                s, user, OfferCreate(employer_id=employer.id, title="Yarış teklifi")
            )
            grup = await item_service.create_group(s, offer.id, 0, OfferGroupCreate(name="G"))
            if gonderilmis:
                await offer_service.transition(s, user, offer.id, 0, OfferAction.send)
            await s.commit()
            ortam = _Ortam(database, engine, factory, user.id, offer.id, grup.id, kalem.id)
        yield ortam
    finally:
        await engine.dispose()
        await _admin(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


_Is = Callable[[AsyncSession], Awaitable[object]]


async def _yaris(
    ortam: _Ortam, birinci_is: _Is, ikinci_is: _Is
) -> tuple[str, BaseException | None]:
    """Oturum 1 `birinci_is`i yapar ve COMMIT ETMEDEN bekler; oturum 2 `ikinci_is`i baslatir,
    beklemesi OLCULUR; sonra 1 commit eder. Donus: (bekleyen sorgu, ikinci oturumun hatasi)."""

    async def _ikinci() -> None:
        async with ortam.Session() as session:
            try:
                await ikinci_is(session)
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    task: asyncio.Task[None] | None = None
    bekleyen = ""
    async with ortam.Session() as birinci:
        try:
            await birinci_is(birinci)
            task = asyncio.create_task(_ikinci())
            bekleyen = await _bekleyen_sorgu(ortam)  # type: ignore[arg-type]
            await asyncio.sleep(_KESISME_PAYI)
            assert not task.done(), "ikinci is birinci commit edilmeden BITTI"
            await birinci.commit()
        except BaseException:
            await birinci.rollback()
            await _sonlandir(task)
            raise
    assert task is not None
    try:
        await asyncio.wait_for(task, _BEKLEME_SINIRI)
    except (IntegrityError, ConflictError, NotFoundError, TimeoutError) as exc:
        return bekleyen, exc
    return bekleyen, None


async def _kullanici(session: AsyncSession, ortam: _Ortam) -> User:
    user = await session.get(User, ortam.user_id)
    assert user is not None
    return user


async def _revizyonlar(ortam: _Ortam) -> list[tuple[int, str]]:
    async with ortam.Session() as s:
        rows = await s.execute(
            select(OfferRevision.rev_no, OfferRevision.status)
            .where(OfferRevision.offer_id == ortam.offer_id)
            .order_by(OfferRevision.rev_no)
        )
        return [(n, st.value) for n, st in rows.all()]


async def _kalem_sayisi(ortam: _Ortam) -> int:
    async with ortam.Session() as s:
        return await s.scalar(select(func.count()).select_from(OfferItem)) or 0


# -------------------------------------------------------- (a) iki yeni revizyon


async def _iki_yeni_revizyon(ortam: _Ortam) -> tuple[str, BaseException | None]:
    async def _is(session: AsyncSession) -> None:
        await offer_service.create_revision(
            session, await _kullanici(session, ortam), ortam.offer_id
        )

    return await _yaris(ortam, _is, _is)


async def test_TKLB42_a_iki_eszamanli_yeni_revizyon_biri_409_cift_rev_YOK() -> None:
    async with _ortam(gonderilmis=True) as ortam:
        bekleyen, hata = await _iki_yeni_revizyon(ortam)

        assert "FROM offers" in bekleyen and "FOR UPDATE" in bekleyen, bekleyen
        assert isinstance(hata, ConflictError), f"ikinci istek 409 olmaliydi: {hata!r}"
        assert await _revizyonlar(ortam) == [(0, "sent"), (1, "draft")]  # CIFT Rev YOK


async def test_TKLB42_a_KONTROL_kilitsiz_iki_yeni_revizyon_ayni_rev_no_UQ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POZITIF KONTROL: `lock_offer` duz okuma → ikisi de Rev.0'i `sent` okur, ikisi `rev_no = 1`
    yazar; ikinci INSERT birincinin UQ girdisinde BEKLER, commit sonrasi IntegrityError."""

    async def _kilitsiz(session: AsyncSession, offer_id: uuid.UUID) -> Offer:
        offer = await session.get(Offer, offer_id)
        assert offer is not None
        return offer

    monkeypatch.setattr(locking, "lock_offer", _kilitsiz)
    async with _ortam(gonderilmis=True) as ortam:
        bekleyen, hata = await _iki_yeni_revizyon(ortam)

        assert bekleyen.startswith("INSERT INTO offer_revisions"), bekleyen
        assert isinstance(hata, IntegrityError), f"kilitsiz de temiz gecti: {hata!r}"
        assert "uq_offer_revisions_offer_rev" in str(hata.orig)
        assert await _revizyonlar(ortam) == [(0, "sent"), (1, "draft")]


# ------------------------------------------------------- (b) send + kalem ekleme


def _kalem_govdesi(ortam: _Ortam) -> OfferItemCreate:
    return OfferItemCreate(
        catalog_item_id=ortam.catalog_id, group_id=ortam.group_id, quantity=Decimal("1")
    )


async def _send_ve_kalem(ortam: _Ortam) -> tuple[str, BaseException | None]:
    async def _birinci(session: AsyncSession) -> None:
        await offer_service.transition(
            session, await _kullanici(session, ortam), ortam.offer_id, 0, OfferAction.send
        )

    async def _ikinci(session: AsyncSession) -> None:
        await item_service.add_items(session, ortam.offer_id, 0, [_kalem_govdesi(ortam)])

    return await _yaris(ortam, _birinci, _ikinci)


async def test_TKLB42_b_eszamanli_send_ve_kalem_ekleme_kalem_gonderilmise_YAZILAMAZ() -> None:
    async with _ortam(gonderilmis=False) as ortam:
        bekleyen, hata = await _send_ve_kalem(ortam)

        assert "FROM offers" in bekleyen and "FOR UPDATE" in bekleyen, bekleyen
        assert isinstance(hata, ConflictError), f"kalem 409 olmaliydi: {hata!r}"
        assert await _revizyonlar(ortam) == [(0, "sent")]
        assert await _kalem_sayisi(ortam) == 0  # gonderilmis revizyonda kalem YOK


async def test_TKLB42_b_KONTROL_kilitsiz_kalem_gonderilmis_revizyona_yazilir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POZITIF KONTROL: kilit KAPALI → kalem BAYAT `draft` okur ve yazar; `UPDATE offer_revisions`
    (son kayit zamani) send'in satir kilidinde bekler, send commit olunca ikisi de gecer →
    `sent` revizyonun ICINDE kalem var. Bu kirmizi, ustteki bekcinin kilidi gercekten
    olctugunun kanitidir."""

    async def _kilitsiz(session: AsyncSession, offer_id: uuid.UUID) -> Offer:
        offer = await session.get(Offer, offer_id)
        assert offer is not None
        return offer

    monkeypatch.setattr(locking, "lock_offer", _kilitsiz)
    async with _ortam(gonderilmis=False) as ortam:
        bekleyen, hata = await _send_ve_kalem(ortam)

        assert bekleyen.startswith("UPDATE offer_revisions"), bekleyen
        assert hata is None, f"kilitsiz kalem reddedildi?: {hata!r}"
        assert await _revizyonlar(ortam) == [(0, "sent")]
        assert await _kalem_sayisi(ortam) == 1  # DEGISMEZ IHLALI: gonderilmis revizyonda kalem

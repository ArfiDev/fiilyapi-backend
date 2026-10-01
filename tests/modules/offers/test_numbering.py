"""TKL-B4.1 — teklif numarasi: `TKL-{yil}-{sira:04d}`, yil bazli, MONOTON sayac (SO-7).

Ardisik/yil/genislik testleri paylasilan `db_session`da; YARIS testleri kendi tek kullanimlik
veritabaninda (emsal `tests/modules/accounting/test_fisno_concurrency.py`): `db_session` tek
baglanti + SAVEPOINT oldugu icin gercek bir satir kilidi hic yarismaz.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import Base
from app.modules.offers import numbering
from app.modules.offers.models import Offer, OfferCounter
from app.modules.offers.numbering import format_offer_no, next_offer_no
from app.modules.projects.models import Employer
from tests._yaris import YARIS_TAVANI_SN, kilitte_bekleyen_sorgu
from tests.modules.accounting.test_fisno_concurrency import (
    _create_scratch_database,
    _drop_scratch_database,
    _sqlalchemy_dsn,
)

YIL = 2026


# ------------------------------------------------------------ sirali / bicim


async def test_ardisik_numaralar_0001_0002(db_session: AsyncSession) -> None:
    assert await next_offer_no(db_session, year=YIL) == "TKL-2026-0001"
    assert await next_offer_no(db_session, year=YIL) == "TKL-2026-0002"
    assert await next_offer_no(db_session, year=YIL) == "TKL-2026-0003"


async def test_yil_basi_sifirlanir_ve_yillar_birbirini_etkilemez(db_session: AsyncSession) -> None:
    assert await next_offer_no(db_session, year=2026) == "TKL-2026-0001"
    assert await next_offer_no(db_session, year=2026) == "TKL-2026-0002"
    assert await next_offer_no(db_session, year=2027) == "TKL-2027-0001"
    assert await next_offer_no(db_session, year=2026) == "TKL-2026-0003"


async def test_9999_sonrasi_numara_budanmaz_uzar(db_session: AsyncSession) -> None:
    db_session.add(OfferCounter(year=YIL, last_no=9998))
    await db_session.flush()
    assert await next_offer_no(db_session, year=YIL) == "TKL-2026-9999"
    assert await next_offer_no(db_session, year=YIL) == "TKL-2026-10000"
    assert await next_offer_no(db_session, year=YIL) == "TKL-2026-10001"


def test_format_en_az_dort_hane() -> None:
    assert format_offer_no(2026, 1) == "TKL-2026-0001"
    assert format_offer_no(2026, 14) == "TKL-2026-0014"
    assert format_offer_no(2026, 10000) == "TKL-2026-10000"


async def test_silinen_teklifin_numarasi_TEKRAR_kullanilmaz(db_session: AsyncSession) -> None:
    """Sayac teklif satirlarindan BAGIMSIZ: en buyuk numarali teklif silinse de numara geri gelmez
    (max + 1 uretici burada 0002 yerine 0001 verirdi)."""
    employer = Employer(name="Silme Testi İşvereni")
    db_session.add(employer)
    await db_session.flush()
    no = await next_offer_no(db_session, year=YIL)
    db_session.add(Offer(offer_no=no, employer_id=employer.id, employer_name="x", title="Taslak"))
    await db_session.flush()
    assert no == "TKL-2026-0001"

    await db_session.execute(delete(Offer).where(Offer.offer_no == no))
    assert (await db_session.scalars(select(Offer))).all() == []

    assert await next_offer_no(db_session, year=YIL) == "TKL-2026-0002"


async def test_sayac_satiri_yil_basina_tek_ve_son_numarayi_tutar(db_session: AsyncSession) -> None:
    for _ in range(3):
        await next_offer_no(db_session, year=YIL)
    rows = (await db_session.execute(select(OfferCounter.year, OfferCounter.last_no))).all()
    assert [tuple(r) for r in rows] == [(YIL, 3)]


# ------------------------------------------------------------------ yil


class _SahteDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        return datetime(2026, 12, 31, 21, 30, tzinfo=UTC).astimezone(tz)


def test_yil_Istanbul_saatiyle_belirlenir_UTC_ile_degil(monkeypatch: pytest.MonkeyPatch) -> None:
    """31 Aralik 21:30 UTC = 1 Ocak 00:30 Istanbul: teklif YENI yilin numarasini alir."""
    monkeypatch.setattr(numbering, "datetime", _SahteDatetime)
    assert numbering.current_offer_year() == 2027


def test_yil_yardimcisi_gercek_saatle_makul_bir_yil_doner() -> None:
    assert numbering.current_offer_year() >= 2026


# ------------------------------------------------------------------- yaris


@asynccontextmanager
async def _yaris_ortami():  # noqa: ANN201
    database = await _create_scratch_database()
    engine = create_async_engine(_sqlalchemy_dsn(database))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()
        await _drop_scratch_database(database)


async def _naif_sonraki(session: AsyncSession, year: int) -> str:
    """POZITIF KONTROL uretici: kilitsiz DUZ OKUMA + 1 (yanlis yontem). Sayac degerini
    kilitlemeden okur, sonra yazar — iki esli istek ayni degeri okur."""
    last = await session.scalar(
        text("SELECT last_no FROM offer_counters WHERE year = :y"), {"y": year}
    )
    n = (last or 0) + 1
    await session.execute(
        text(
            "INSERT INTO offer_counters (year, last_no) VALUES (:y, :n) "
            "ON CONFLICT (year) DO UPDATE SET last_no = :n"
        ),
        {"y": year, "n": n},
    )
    return format_offer_no(year, n)


async def _yaris(engine, factory, uretici, *, once_commit: int) -> tuple[str, str, str]:
    """Oturum 1 numara alir ama COMMIT ETMEZ; oturum 2'nin bir KILITTE beklemesi
    `pg_stat_activity`den OLCULUR (uyku degil); sonra 1 commit eder. Donus: (n1, n2, bekleyen)."""
    async with factory() as hazirlik:
        for _ in range(once_commit):
            await next_offer_no(hazirlik, year=YIL)
        await hazirlik.commit()

    async def _ikinci() -> str:
        async with factory() as s:
            no = await uretici(s, YIL)
            await s.commit()
            return no

    async with factory() as birinci:
        n1 = await uretici(birinci, YIL)
        gorev = asyncio.create_task(_ikinci())
        try:
            bekleyen = await kilitte_bekleyen_sorgu(
                engine,
                gorev,
                mesaj="ikinci teklif numarası KİLİTTE beklemedi — üretim kilitsiz",
            )
        except BaseException:
            await birinci.rollback()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(gorev, timeout=YARIS_TAVANI_SN)
            raise
        await birinci.commit()
    n2 = await asyncio.wait_for(gorev, timeout=YARIS_TAVANI_SN)
    return n1, n2, bekleyen


async def _gercek(session: AsyncSession, year: int) -> str:
    return await next_offer_no(session, year=year)


async def test_eszamanli_iki_teklif_AYNI_numarayi_alamaz_sayac_VAR() -> None:
    async with _yaris_ortami() as (engine, factory):
        n1, n2, bekleyen = await _yaris(engine, factory, _gercek, once_commit=1)
    assert "offer_counters" in bekleyen and "ON CONFLICT" in bekleyen, bekleyen
    assert (n1, n2) == ("TKL-2026-0002", "TKL-2026-0003")


async def test_yilin_ilk_teklifi_yarisinda_da_numara_TEKIL_sayac_YOK() -> None:
    """Sayac satiri HENUZ YOK: UPSERT-SONRA-KILITLE (kilitlenecek satirin varligi da kilidin
    parcasi). 'Once SELECT, yoksa INSERT' iki kez 0001 verirdi."""
    async with _yaris_ortami() as (engine, factory):
        n1, n2, bekleyen = await _yaris(engine, factory, _gercek, once_commit=0)
    assert "offer_counters" in bekleyen, bekleyen
    assert (n1, n2) == ("TKL-2026-0001", "TKL-2026-0002")


async def test_POZITIF_KONTROL_kilitsiz_duz_okuma_AYNI_numarayi_uretir() -> None:
    """Olcum aletinin ISE YARADIGININ kaniti: sayac yerine kilitsiz okuma + 1 ayni yaris
    senaryosunda IKI KEZ ayni numarayi verir (kardes testler bu yuzden anlamlidir)."""
    async with _yaris_ortami() as (engine, factory):
        n1, n2, _ = await _yaris(engine, factory, _naif_sonraki, once_commit=1)
    assert n1 == n2 == "TKL-2026-0002"

"""TKL-B2 — iki ESZAMANLI katalog olusturma ayni disiplinde: farkli ARDISIK poz no.

Emsal: `tests/earned_value_budget/test_evborc5_approve_lock.py` (tutulan-kilit + bariyer).
Kurgu: tek kullanimlik yaris DB'si; oturum 1 kalemi olusturur (numara aldi, COMMIT YOK →
disiplin satiri kilidi tutuluyor); oturum 2 ayni disiplinde olusturur.
* Kilitli (bugunku kod): 2 `ev_disciplines … FOR UPDATE`te BEKLER; 1 commit edince
  sayaci TAZE okur → `KAB-0002`.
* POZITIF KONTROL (kilitsiz okuma): 2 sayaci 0 okur, `UPDATE ev_disciplines`te 1'in satir
  kilidinde BEKLER; 1 commit edince 2 de `KAB-0001` yazar → `uq_ev_catalog_items_poz_no`
  IntegrityError. Bariyer bekleyen SORGUYU olcer (uyku degil) — kilitsiz yolda da ikinci
  oturumun gercekten kesistigi kanitlanir ("alakasiz onceki kilit yarisi yutar" tuzagi:
  kontrolun KIRMIZISI bu testin kilidi gercekten olctugunun kanitidir).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog import service as core
from app.modules.catalog.models import EvCatalogItem, EvDiscipline
from app.modules.earned_value.engine import ContractorType
from tests._poz_no_degismezi import assert_poz_invariant
from tests.earned_value_budget.test_budget_concurrency import (
    _BEKLEME_SINIRI,
    _KESISME_PAYI,
    _bekleyen_sorgu,
    _Ortam,
    _sonlandir,
    _yaris_ortami,
)

pytestmark = pytest.mark.asyncio


def _fields(ortam: _Ortam, name: str) -> dict:
    return {
        "discipline_id": ortam.discipline_id,
        "name": name,
        "uom": "m3",
        "standard_unit_mhr": Decimal("1.5"),
        "default_contractor_type": ContractorType.OWN,
    }


_Is = Callable[[AsyncSession], Awaitable[object]]


async def _yaris(
    ortam: _Ortam, birinci_is: _Is, ikinci_is: _Is
) -> tuple[str, BaseException | None]:
    """Tutulan-kilit senaryosu: oturum 1 `birinci_is`i yapar ve COMMIT ETMEDEN bekler; oturum 2
    `ikinci_is`i baslatir, beklemesi `pg_stat_activity`den OLCULUR; sonra 1 commit eder.
    Donus: (bekleyen sorgu, ikinci oturumun hatasi | None)."""

    async def _ikinci() -> None:
        async with ortam.Session() as session:
            await ikinci_is(session)
            await session.commit()

    task: asyncio.Task[None] | None = None
    bekleyen = ""
    async with ortam.Session() as birinci:
        try:
            await birinci_is(birinci)
            task = asyncio.create_task(_ikinci())
            bekleyen = await _bekleyen_sorgu(ortam)
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
    except (IntegrityError, RuntimeError, TimeoutError) as exc:
        return bekleyen, exc
    return bekleyen, None


async def _iki_olusturma(ortam: _Ortam) -> tuple[str, BaseException | None]:
    async def _birinci(session: AsyncSession) -> None:
        item = await core.create_item(session, _fields(ortam, "Birinci"))
        assert item.poz_no == "KAB-0001"

    async def _ikinci(session: AsyncSession) -> None:
        await core.create_item(session, _fields(ortam, "Ikinci"))

    return await _yaris(ortam, _birinci, _ikinci)


async def _iki_kod_degisimi(ortam: _Ortam) -> tuple[str, BaseException | None]:
    """Disiplinde 1 kalem (`KAB-0001`); 1: KAB→KBB (commit yok), 2: KAB→KCC."""
    async with ortam.Session() as seed:
        await core.create_item(seed, _fields(ortam, "Tohum"))
        await seed.commit()

    async def _birinci(session: AsyncSession) -> None:
        await core.update_discipline(session, ortam.discipline_id, {"code": "KBB"})

    async def _ikinci(session: AsyncSession) -> None:
        await core.update_discipline(session, ortam.discipline_id, {"code": "KCC"})

    return await _yaris(ortam, _birinci, _ikinci)


async def _kod_durumu(ortam: _Ortam) -> tuple[str, list[str]]:
    async with ortam.Session() as session:
        await assert_poz_invariant(session)
        code = await session.scalar(
            select(EvDiscipline.code).where(EvDiscipline.id == ortam.discipline_id)
        )
        nos = list(await session.scalars(select(EvCatalogItem.poz_no)))
        return code or "", sorted(nos)


async def _durum(ortam: _Ortam) -> tuple[list[str], int]:
    async with ortam.Session() as session:
        nos = list(
            await session.scalars(
                select(EvCatalogItem.poz_no)
                .where(EvCatalogItem.discipline_id == ortam.discipline_id)
                .order_by(EvCatalogItem.poz_no)
            )
        )
        counter = await session.scalar(
            select(EvDiscipline.poz_counter).where(EvDiscipline.id == ortam.discipline_id)
        )
        return nos, counter or 0


async def test_TKLB2_concurrent_creates_get_distinct_consecutive_numbers() -> None:
    async with _yaris_ortami() as ortam:
        bekleyen, hata = await _iki_olusturma(ortam)

        # sayac yazimi: FOR NO KEY UPDATE (R2; FK KEY SHARE alanlari bekletmez)
        assert "FROM ev_disciplines" in bekleyen and "FOR NO KEY UPDATE" in bekleyen, bekleyen
        assert hata is None, f"ikinci olusturma basarisiz: {hata!r}"
        assert await _durum(ortam) == (["KAB-0001", "KAB-0002"], 2)


async def test_TKLB2_KONTROL_without_discipline_lock_numbers_collide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POZITIF KONTROL: kilit KAPALI (duz okuma), senaryo AYNI → ikisi de KAB-0001 → UQ."""

    async def _kilitsiz(session: AsyncSession, discipline_id: uuid.UUID) -> EvDiscipline:
        return await core.get_discipline(session, discipline_id)

    monkeypatch.setattr(core, "lock_discipline", _kilitsiz)
    async with _yaris_ortami() as ortam:
        bekleyen, hata = await _iki_olusturma(ortam)

        # ikinci oturum sayaci OKUDU ve birincinin satir kilidinde bekliyor → cakisma kesin
        assert bekleyen.startswith("UPDATE ev_disciplines"), bekleyen
        assert isinstance(hata, IntegrityError), f"kilitsiz de temiz gecti: {hata!r}"
        assert "uq_ev_catalog_items_poz_no" in str(hata.orig)
        assert await _durum(ortam) == (["KAB-0001"], 1)


async def test_TKLB2_concurrent_code_changes_serialize_and_last_code_wins() -> None:
    """A→B ve A→C ESZAMANLI: 2 disiplin satirinda (`FOR UPDATE`) BEKLER, 1 commit edince kodu
    TAZE (KBB) okur → KBB→KCC; son kod kazanir, tum onekler guncel kod, degismez SQL'i temiz."""
    async with _yaris_ortami() as ortam:
        bekleyen, hata = await _iki_kod_degisimi(ortam)

        assert "FROM ev_disciplines" in bekleyen and "FOR UPDATE" in bekleyen, bekleyen
        assert "NO KEY" not in bekleyen, bekleyen  # kod degisimi TAM kilit ister
        assert hata is None, f"ikinci kod degisimi basarisiz: {hata!r}"
        assert await _kod_durumu(ortam) == ("KCC", ["KCC-0001"])


async def test_TKLB2_KONTROL_code_change_without_lock_breaks_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POZITIF KONTROL: `update_discipline`daki kilit duz okumaya cevrilince 2 BAYAT kodu (KAB)
    okur, `UPDATE ev_disciplines`te 1'in satir kilidinde bekler; 1 commit edince 2 eski onek
    `KAB-` ile arar ama kalem `KBB-0001` → `renumber_for_code_change` degismez ihlalini yakalar
    (RuntimeError, geri alinir). Bu kirmizi kilidin gercekten olculdugunun kanitidir."""

    async def _kilitsiz(
        session: AsyncSession, discipline_id: uuid.UUID, *, rewrites_code: bool = False
    ) -> EvDiscipline:
        return await core.get_discipline(session, discipline_id)

    monkeypatch.setattr(core, "lock_discipline", _kilitsiz)
    async with _yaris_ortami() as ortam:
        bekleyen, hata = await _iki_kod_degisimi(ortam)

        assert bekleyen.startswith("UPDATE ev_disciplines"), bekleyen
        assert isinstance(hata, RuntimeError), f"kilitsiz de temiz gecti: {hata!r}"
        assert "degismezi bozuk" in str(hata)

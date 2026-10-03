"""KAT-B1 — `bulk_upsert_items` ESZAMANLILIK bekcileri (tutulan-kilit + bariyer).

Emsal: `test_poz_no_race.py` (oturum 1 COMMIT ETMEDEN bekler, oturum 2 baslar; bekleyen sorgu
`pg_stat_activity`den OLCULUR). Uc iddia, her birinin POZITIF KONTROLU (kilit kapatilinca
KIRMIZI) ile:

1. iki toplu istek AYNI disiplinde → ikisi de basarili, poz no'lar tekil ve ARDISIK
   (boslugusuz), sayac dogru. Iki kilit birden bu sonucu verir: toplu-aktarim advisory kilidi
   VE disiplin sayac kilidi. Kontrol: IKISI de kapali → carpisma.
2. TEKIL olusturma + toplu (tekil yol advisory kilidi ALMAZ) → yalniz DISIPLIN kilidi
   serilestirir. Kontrol: disiplin kilidi kapali → carpisma. (Bu test advisory kilidinin
   disiplin kilidini MASKELEMEDIGINI kanitlar: 1. testte advisory tek basina yarisi yutar.)
3. iki toplu istek ayni `source_code`u FARKLI disiplinlerde ekler → ikincisi birincinin
   kodunu GORUR ve temiz 422 alir. Kontrol: advisory kilidi kapali → ikisi de "yok" gorur,
   ikincisi DB UQ'da IntegrityError ile patlar.
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

from app.core.errors import EarnedValueValidationError
from app.modules.catalog import bulk
from app.modules.catalog import service as core
from app.modules.catalog.models import ContractorType, EvCatalogItem, EvDiscipline
from tests.earned_value_budget.test_budget_concurrency import (
    _BEKLEME_SINIRI,
    _KESISME_PAYI,
    _bekleyen_sorgu,
    _Ortam,
    _sonlandir,
    _yaris_ortami,
)

pytestmark = pytest.mark.asyncio

_Is = Callable[[AsyncSession], Awaitable[object]]


def _entry(ortam: _Ortam, name: str, **over) -> dict:
    base = {
        "discipline_id": ortam.discipline_id,
        "name": name,
        "uom": "m3",
        "standard_unit_mhr": Decimal("1.5"),
        "default_contractor_type": "own",
        "description": None,
        "ref_price": None,
        "source_code": None,
        "ref_price_date": None,
    }
    return {**base, **over}


async def _yaris(
    ortam: _Ortam, birinci_is: _Is, ikinci_is: _Is
) -> tuple[str, BaseException | None]:
    """Oturum 1 `birinci_is`i yapar, COMMIT ETMEDEN bekler; oturum 2 `ikinci_is`i baslatir
    (beklemesi OLCULUR); sonra 1 commit eder. Donus: (bekleyen sorgu, 2. oturumun hatasi)."""

    async def _ikinci() -> None:
        async with ortam.Session() as session:
            try:
                await ikinci_is(session)
            except BaseException:
                await session.rollback()
                raise
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
    except (IntegrityError, EarnedValueValidationError, RuntimeError, TimeoutError) as exc:
        return bekleyen, exc
    return bekleyen, None


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


async def _iki_toplu(ortam: _Ortam) -> tuple[str, BaseException | None]:
    async def _birinci(session: AsyncSession) -> None:
        rows = await bulk.bulk_upsert_items(
            session, [_entry(ortam, "A1"), _entry(ortam, "A2")], "error"
        )
        assert [r.item.poz_no for r in rows] == ["KAB-0001", "KAB-0002"]

    async def _ikinci(session: AsyncSession) -> None:
        await bulk.bulk_upsert_items(session, [_entry(ortam, "B1"), _entry(ortam, "B2")], "error")

    return await _yaris(ortam, _birinci, _ikinci)


async def _tekil_sonra_toplu(ortam: _Ortam) -> tuple[str, BaseException | None]:
    """Oturum 1 TEKIL olusturur (sayac kilidi + numara, commit yok); oturum 2 TOPLU baslar.
    Kilidi OKUYAN taraf toplu yoldur: toplu yoldaki disiplin kilidi kalkarsa bayat sayac okur."""

    async def _birinci(session: AsyncSession) -> None:
        fields = _entry(ortam, "Tekil")
        fields["default_contractor_type"] = ContractorType.OWN
        await core.create_item(session, fields)

    async def _ikinci(session: AsyncSession) -> None:
        await bulk.bulk_upsert_items(session, [_entry(ortam, "A1"), _entry(ortam, "A2")], "error")

    return await _yaris(ortam, _birinci, _ikinci)


async def _ikinci_disiplin(ortam: _Ortam) -> uuid.UUID:
    async with ortam.Session() as session:
        disc = EvDiscipline(
            code="ELK", name="Elektrik", color="#2563eb", default_contractor_type=ContractorType.OWN
        )
        session.add(disc)
        await session.flush()
        await session.commit()
        return disc.id


async def _ayni_kod_farkli_disiplin(ortam: _Ortam) -> tuple[str, BaseException | None]:
    elk = await _ikinci_disiplin(ortam)

    async def _birinci(session: AsyncSession) -> None:
        await bulk.bulk_upsert_items(
            session, [_entry(ortam, "A1", source_code="15.100.1001")], "error"
        )

    async def _ikinci(session: AsyncSession) -> None:
        await bulk.bulk_upsert_items(
            session, [_entry(ortam, "B1", discipline_id=elk, source_code="15.100.1001")], "error"
        )

    return await _yaris(ortam, _birinci, _ikinci)


async def _hicbir_kilit_yok(session: AsyncSession) -> None:
    return None


async def _kilitsiz_disiplin(
    session: AsyncSession, discipline_id: uuid.UUID, *, rewrites_code: bool = False
) -> EvDiscipline:
    return await core.get_discipline(session, discipline_id)


async def test_KATB1_iki_eszamanli_toplu_ayni_disiplin_ardisik_poz_no() -> None:
    async with _yaris_ortami() as ortam:
        bekleyen, hata = await _iki_toplu(ortam)

        assert "pg_advisory_xact_lock" in bekleyen, bekleyen  # toplu aktarim kilidinde bekler
        assert hata is None, f"ikinci toplu basarisiz: {hata!r}"
        assert await _durum(ortam) == (["KAB-0001", "KAB-0002", "KAB-0003", "KAB-0004"], 4)


async def test_KATB1_KONTROL_iki_kilit_de_kapali_poz_no_carpisir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POZITIF KONTROL: advisory + disiplin kilidi KAPALI → ikinci toplu sayaci BAYAT okur,
    birincinin `UPDATE ev_disciplines` satir kilidinde bekler; commit sonrasi ikisi de
    `KAB-0001` yazar → `uq_ev_catalog_items_poz_no`."""
    monkeypatch.setattr(bulk, "_serialize_imports", _hicbir_kilit_yok)
    monkeypatch.setattr(core, "lock_discipline", _kilitsiz_disiplin)
    async with _yaris_ortami() as ortam:
        bekleyen, hata = await _iki_toplu(ortam)

        assert bekleyen.startswith("UPDATE ev_disciplines"), bekleyen
        assert isinstance(hata, IntegrityError), f"kilitsiz de temiz gecti: {hata!r}"
        assert "uq_ev_catalog_items_poz_no" in str(hata.orig)


async def test_KATB1_tekil_olusturma_ve_toplu_disiplin_kilidiyle_serilesir() -> None:
    async with _yaris_ortami() as ortam:
        bekleyen, hata = await _tekil_sonra_toplu(ortam)

        assert "FROM ev_disciplines" in bekleyen and "FOR NO KEY UPDATE" in bekleyen, bekleyen
        assert hata is None, f"toplu istek basarisiz: {hata!r}"
        assert await _durum(ortam) == (["KAB-0001", "KAB-0002", "KAB-0003"], 3)


async def test_KATB1_KONTROL_toplu_yolunda_disiplin_kilidi_kapali_tekille_carpisir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POZITIF KONTROL: yalniz DISIPLIN kilidi kapali (advisory acik ama tekil yol onu almaz)
    → toplu istek bayat sayaci okur, `UPDATE`te bekler, ikisi de `KAB-0001`'i yazar."""
    monkeypatch.setattr(core, "lock_discipline", _kilitsiz_disiplin)
    async with _yaris_ortami() as ortam:
        bekleyen, hata = await _tekil_sonra_toplu(ortam)

        assert bekleyen.startswith("UPDATE ev_disciplines"), bekleyen
        assert isinstance(hata, IntegrityError), f"kilitsiz de temiz gecti: {hata!r}"
        assert "uq_ev_catalog_items_poz_no" in str(hata.orig)


async def test_KATB1_ayni_kaynak_kodu_iki_toplu_ikincisi_temiz_422() -> None:
    async with _yaris_ortami() as ortam:
        bekleyen, hata = await _ayni_kod_farkli_disiplin(ortam)

        assert "pg_advisory_xact_lock" in bekleyen, bekleyen
        assert isinstance(hata, EarnedValueValidationError), f"beklenen temiz 422: {hata!r}"
        assert hata.errors[0]["loc"] == ["body", "items", 0, "source_code"]
        async with ortam.Session() as session:
            kodlar = list(await session.scalars(select(EvCatalogItem.source_code)))
        assert kodlar == ["15.100.1001"]


async def test_KATB1_KONTROL_advisory_kapali_ayni_kaynak_kodu_DB_UQ_ile_patlar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POZITIF KONTROL: advisory kilidi KAPALI → ikinci toplu kodu 'yok' gorur (FARKLI disiplin,
    ayri sayac kilidi), INSERT'i birincinin INSERT'inin UQ girdisinde bekler; ilk commit
    edince temiz 422 DEGIL `IntegrityError` (DB UQ yedegi) alir."""
    monkeypatch.setattr(bulk, "_serialize_imports", _hicbir_kilit_yok)
    async with _yaris_ortami() as ortam:
        bekleyen, hata = await _ayni_kod_farkli_disiplin(ortam)

        assert bekleyen.startswith("INSERT INTO ev_catalog_items"), bekleyen
        assert isinstance(hata, IntegrityError), f"advisory'siz de temiz gecti: {hata!r}"
        assert "uq_ev_catalog_items_source_code" in str(hata.orig)

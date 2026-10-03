"""KAT-B1.1 — kalem kilidi (Y1/O2) ve disiplin → kalem kilit sirasi (O1) ESZAMANLILIK bekcileri.

Emsal: `test_kat_b1_bulk_race.py` (tutulan-kilit + bariyer, `_yaris`). Her bekcinin
POZITIF KONTROLU var (kilit/sira bozulunca KIRMIZI olan davranis birebir uretilir).

* Y1: PATCH/bulk fiyat+tarih karari KILIT ALTINDA okunan taze fiyatla verilir. Kilitsiz →
  bayat fiyatla karar → tarih yanlis fiyata yapisir ya da DB CHECK'i (`..._requires_price`) patlar.
* O2: `update_price` kalem kilidi (`FOR NO KEY UPDATE`); kilitsiz → 12'lik fiyata 10'luk
  fiyatin tarihi yapisir.
* O1: toplu `update_price` + yeni kalem, disiplin KODU degisimi / kalem TASIMA ile
  kilit dongusu YOK (40P01 yok). Kontrol: eski sira (kalem → disiplin) EMULE edilince 40P01.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EarnedValueValidationError
from app.modules.catalog import bulk
from app.modules.catalog import service as core
from app.modules.catalog.models import ContractorType, EvCatalogItem, EvDiscipline
from tests._poz_no_degismezi import assert_poz_invariant
from tests.earned_value_budget.test_budget_concurrency import (
    _BEKLEME_SINIRI,
    _bekleyen_sorgu,
    _Ortam,
    _yaris_ortami,
)

from .test_kat_b1_bulk_race import _entry, _ikinci_disiplin, _yaris

pytestmark = pytest.mark.asyncio

CK = "ck_ev_catalog_items_ref_price_date_requires_price"


async def _tohum(ortam: _Ortam, price: str = "100", day: date = date(2025, 1, 1)):
    """Tek kalem (`S.1`, fiyat + tarih), COMMIT'li. Kimligini dondurur."""
    async with ortam.Session() as s:
        fields = _entry(
            ortam,
            "Mevcut",
            source_code="S.1",
            ref_price=Decimal(price),
            ref_price_date=day,
        )
        fields["default_contractor_type"] = ContractorType.OWN
        item = await core.create_item(s, fields)
        await s.commit()
        return item.id


async def _fiyat(ortam: _Ortam, item_id) -> tuple[Decimal | None, date | None]:
    async with ortam.Session() as s:
        row = (
            await s.execute(
                select(EvCatalogItem.ref_price, EvCatalogItem.ref_price_date).where(
                    EvCatalogItem.id == item_id
                )
            )
        ).one()
        return row[0], row[1]


async def _kilitsiz_kalem(session: AsyncSession, item_id):
    return await core.get_item(session, item_id)


def _kilitsiz_toplu_okuma(gercek):
    async def _okuma(session, codes, *, lock):  # noqa: ANN001
        return await gercek(session, codes, lock=False)

    return _okuma


# ----------------------------------------------------------------- Y1 (PATCH)


async def _patch_yarisi(ortam: _Ortam, item_id):
    async def _birinci(session: AsyncSession) -> None:  # fiyati NULL yapar (tarih de temizlenir)
        await core.update_item(session, item_id, {"ref_price": None})

    async def _ikinci(session: AsyncSession) -> None:  # bayat fiyatla yalniz tarih
        await core.update_item(session, item_id, {"ref_price_date": date(2025, 6, 1)})

    return await _yaris(ortam, _birinci, _ikinci)


async def test_KATB11_Y1_patch_taze_fiyatla_karar_verir_degismez_bozulmaz() -> None:
    async with _yaris_ortami() as ortam:
        item_id = await _tohum(ortam)
        bekleyen, hata = await _patch_yarisi(ortam, item_id)

        assert "FROM ev_catalog_items" in bekleyen and "FOR NO KEY UPDATE" in bekleyen, bekleyen
        assert isinstance(hata, EarnedValueValidationError), f"beklenen 422 kurali: {hata!r}"
        assert await _fiyat(ortam, item_id) == (None, None)


async def test_KATB11_Y1_KONTROL_kalem_kilidi_kapali_bayat_fiyat_CHECK_ile_patlar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "lock_item", _kilitsiz_kalem)
    async with _yaris_ortami() as ortam:
        item_id = await _tohum(ortam)
        bekleyen, hata = await _patch_yarisi(ortam, item_id)

        assert bekleyen.startswith("UPDATE ev_catalog_items"), bekleyen
        assert isinstance(hata, IntegrityError), f"kilitsiz de temiz gecti: {hata!r}"
        assert CK in str(hata.orig)  # DB yedek savunmasi bozuk durumu reddetti
        assert await _fiyat(ortam, item_id) == (None, None)


# --------------------------------------------------- Y1/D5 (toplu yalniz tarih)


async def _toplu_tarih_yarisi(ortam: _Ortam, item_id):
    async def _birinci(session: AsyncSession) -> None:
        await core.update_item(session, item_id, {"ref_price": None})

    async def _ikinci(session: AsyncSession) -> None:  # fiyatsiz, yalniz tarih (kural d)
        await bulk.bulk_upsert_items(
            session,
            [_entry(ortam, "x", source_code="S.1", ref_price_date=date(2025, 6, 1))],
            "update_price",
        )

    return await _yaris(ortam, _birinci, _ikinci)


async def test_KATB11_Y1_toplu_yalniz_tarih_kilit_altinda_taze_fiyata_bakar() -> None:
    async with _yaris_ortami() as ortam:
        item_id = await _tohum(ortam)
        bekleyen, hata = await _toplu_tarih_yarisi(ortam, item_id)

        assert "FROM ev_catalog_items" in bekleyen and "FOR NO KEY UPDATE" in bekleyen, bekleyen
        assert isinstance(hata, EarnedValueValidationError), f"beklenen 422: {hata!r}"
        assert hata.errors[0]["loc"] == ["body", "items", 0, "ref_price_date"]
        assert await _fiyat(ortam, item_id) == (None, None)


async def test_KATB11_Y1_KONTROL_toplu_kalem_kilidi_kapali_CHECK_ile_patlar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bulk, "_existing_by_source", _kilitsiz_toplu_okuma(bulk._existing_by_source)
    )
    async with _yaris_ortami() as ortam:
        item_id = await _tohum(ortam)
        bekleyen, hata = await _toplu_tarih_yarisi(ortam, item_id)

        assert bekleyen.startswith("UPDATE ev_catalog_items"), bekleyen
        assert isinstance(hata, IntegrityError), f"kilitsiz de temiz gecti: {hata!r}"
        assert CK in str(hata.orig)


# ------------------------------------------------------------------- O2


async def _update_price_yarisi(ortam: _Ortam, item_id):
    async def _birinci(session: AsyncSession) -> None:  # PATCH: fiyat 100 → 120 (tarih NULL)
        await core.update_item(session, item_id, {"ref_price": Decimal("120")})

    async def _ikinci(session: AsyncSession) -> None:  # ayni (bayat) fiyat 100 + yeni tarih
        await bulk.bulk_upsert_items(
            session,
            [
                _entry(
                    ortam,
                    "x",
                    source_code="S.1",
                    ref_price=Decimal("100"),
                    ref_price_date=date(2026, 1, 1),
                )
            ],
            "update_price",
        )

    return await _yaris(ortam, _birinci, _ikinci)


async def test_KATB11_O2_update_price_kalem_kilidi_taze_fiyatla_karsilastirir() -> None:
    async with _yaris_ortami() as ortam:
        item_id = await _tohum(ortam)
        bekleyen, hata = await _update_price_yarisi(ortam, item_id)

        assert "FROM ev_catalog_items" in bekleyen and "FOR NO KEY UPDATE" in bekleyen, bekleyen
        assert hata is None, f"toplu basarisiz: {hata!r}"
        # toplu 120'yi GORDU → 100'e geri yazdi VE kendi tarihini bagladi (tutarli)
        assert await _fiyat(ortam, item_id) == (Decimal("100.00"), date(2026, 1, 1))


async def test_KATB11_O2_KONTROL_kalem_kilidi_kapali_tarih_yanlis_fiyata_yapisir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bulk, "_existing_by_source", _kilitsiz_toplu_okuma(bulk._existing_by_source)
    )
    async with _yaris_ortami() as ortam:
        item_id = await _tohum(ortam)
        bekleyen, hata = await _update_price_yarisi(ortam, item_id)

        assert bekleyen.startswith("UPDATE ev_catalog_items"), bekleyen
        assert hata is None
        # bayat fiyatla 'ayni fiyat, yalniz tarih' sanildi: 120'ye 2026 tarihi yapisti
        assert await _fiyat(ortam, item_id) == (Decimal("120.00"), date(2026, 1, 1))


# ------------------------------------------------------------------- O1


async def _kod_ve_hedef(ortam: _Ortam, hedef_elk: bool):
    elk = await _ikinci_disiplin(ortam)
    item_id = await _tohum(ortam, price="10")
    return elk, item_id, (elk if hedef_elk else ortam.discipline_id)


def _toplu_girdi(ortam: _Ortam, hedef):
    return [
        _entry(ortam, "x", discipline_id=hedef, source_code="S.1", ref_price=Decimal("12")),
        _entry(ortam, "Yeni", discipline_id=hedef, source_code="S.2"),
    ]


async def _deadlock_senaryosu(ortam: _Ortam, mod: str, *, eski_sira: bool = False):
    """Toplu `update_price` (eslesen S.1 + yeni S.2) kilitlerini ALIP bekler; sonra diger islem
    (disiplin KODU degisimi ya da kalem TASIMA) baslar; bekleme olculur; toplu serbest kalir.
    `eski_sira=True`: toplu yol ESKI sirayi EMULE eder (once kalem, sonra disiplin)."""
    elk, item_id, hedef = await _kod_ve_hedef(ortam, hedef_elk=mod == "move")
    paused, go = asyncio.Event(), asyncio.Event()
    state: dict = {}
    gercek = bulk._existing_by_source
    gercek_lock = core.lock_discipline

    async def _kapili(session, codes, *, lock):  # noqa: ANN001
        sonuc = await gercek(session, codes, lock=lock)
        if lock and session is state.get("s"):
            paused.set()
            await go.wait()
        return sonuc

    async def _eski_sira_kilit(session, discipline_id, *, rewrites_code=False):  # noqa: ANN001
        # ESKI sira emulesi: kalem kilidi ZATEN alinmis gibi — once kalem, sonra disiplin.
        if session is state.get("s") and not state.get("item_locked"):
            state["item_locked"] = True
            await session.execute(
                select(EvCatalogItem.id).where(EvCatalogItem.id == item_id).with_for_update()
            )
            paused.set()
            await go.wait()
        return await gercek_lock(session, discipline_id, rewrites_code=rewrites_code)

    async def _toplu() -> object:
        async with ortam.Session() as s:
            state["s"] = s
            try:
                await bulk.bulk_upsert_items(s, _toplu_girdi(ortam, hedef), "update_price")
                await s.commit()
                return "ok"
            except BaseException as exc:
                await s.rollback()
                return exc

    async def _diger() -> object:
        async with ortam.Session() as s:
            try:
                if mod == "code":
                    await core.update_discipline(s, ortam.discipline_id, {"code": "KBX"})
                else:
                    await core.update_item(s, item_id, {"discipline_id": elk})
                await s.commit()
                return "ok"
            except BaseException as exc:
                await s.rollback()
                return exc

    mp = pytest.MonkeyPatch()
    try:
        if eski_sira:
            mp.setattr(core, "lock_discipline", _eski_sira_kilit)
        else:
            mp.setattr(bulk, "_existing_by_source", _kapili)
        tb = asyncio.create_task(_toplu())
        await asyncio.wait_for(paused.wait(), _BEKLEME_SINIRI)
        td = asyncio.create_task(_diger())
        bekleyen = await _bekleyen_sorgu(ortam)
        go.set()
        sonuclar = await asyncio.wait_for(asyncio.gather(tb, td), _BEKLEME_SINIRI)
    finally:
        go.set()
        mp.undo()
    return bekleyen, sonuclar


@pytest.mark.parametrize("mod", ["code", "move"])
async def test_KATB11_O1_toplu_update_price_kod_degisimi_ve_tasima_ile_kilitlenmez(mod) -> None:
    async with _yaris_ortami() as ortam:
        bekleyen, (toplu, diger) = await _deadlock_senaryosu(ortam, mod)

        assert "FROM ev_disciplines" in bekleyen and "FOR" in bekleyen, bekleyen
        assert toplu == "ok", f"toplu: {toplu!r}"
        assert diger == "ok", f"diger: {diger!r}"
        async with ortam.Session() as s:
            await assert_poz_invariant(s)
            assert await s.scalar(
                select(EvDiscipline.code).where(EvDiscipline.id == ortam.discipline_id)
            ) == ("KBX" if mod == "code" else "KAB")
            nos = sorted(await s.scalars(select(EvCatalogItem.poz_no)))
            assert len(nos) == 2  # eslesen (fiyat guncellendi) + yeni


@pytest.mark.parametrize("mod", ["code", "move"])
async def test_KATB11_O1_KONTROL_eski_kilit_sirasi_40P01_ile_kilitlenir(mod) -> None:
    """POZITIF KONTROL: toplu yol ESKI sirayla (kalem → disiplin) kilitlenirse ayni senaryo
    deadlock (40P01) verir — senaryo dongusu gercekten yakaliyor."""
    async with _yaris_ortami() as ortam:
        try:
            _bekleyen, sonuclar = await _deadlock_senaryosu(ortam, mod, eski_sira=True)
        except AssertionError:
            raise
        hatalar = [r for r in sonuclar if isinstance(r, DBAPIError)]
        assert hatalar, f"eski sirada deadlock olusmadi: {sonuclar!r}"
        assert getattr(hatalar[0].orig, "sqlstate", None) == "40P01", hatalar[0]

"""TKL-B6.3 — `contract_seed` portunun EV adaptoru (`earned_value/contract_adapter.py`).

Dunya: `_tkl_b6.py` (bolumsuz santiye, 6 sozlesme kalemi, 4 grup). Beklenenler elle:
  yuvalar  ci1 2.5 offer · ci2 0.6 catalog · ci3 0.7 offer · ci4 1.9 catalog · ci5 0.8 catalog
           ci6 yuvasiz (None)
  disiplin Betonarme→KAB (ikisi KAB) · Duvar→DUV · Karisik→KARISIK (uyari) · Katalogsuz→yok
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select

from app.core import contract_seed
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.earned_value import contract_adapter
from app.modules.earned_value.models import (
    EvBaselineCurve,
    EvBaselineLeaf,
    EvContractItemRate,
    EvGroupDiscipline,
    EvItemSettings,
    EvLeafSettings,
    EvRevision,
    EvWindow,
    RateSource,
    RevisionStatus,
)

from . import _tkl_b6 as w

D = Decimal


@pytest.fixture(autouse=True)
def _port_kaydi_korunur():
    snapshot = contract_seed.registered()
    yield
    contract_seed.restore(snapshot)


async def _rows(db, model, **where):
    stmt = select(model)
    for key, val in where.items():
        stmt = stmt.where(getattr(model, key) == val)
    return list((await db.execute(stmt)).scalars())


async def _rev(db, santiye) -> EvRevision:
    return (
        await db.execute(select(EvRevision).where(EvRevision.site_id == santiye.id))
    ).scalar_one()


# ------------------------------------------------------------------ yuvalar (santiyesiz)


async def test_santiyesiz_yalniz_yuvalar_dogru_kaynakla(
    seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    dunya = await w.kur(seeded_db, proje, santiye, katalog, boq=False)
    req = w.istek(proje, None, dunya, w.TUM_ORANLAR)

    uyarilar = await contract_adapter.seed_hook(seeded_db, req)

    slots = {r.contract_item_id: r for r in await _rows(seeded_db, EvContractItemRate)}
    beklenen = {
        "ci1": (D("2.5"), RateSource.OFFER),
        "ci2": (D("0.6"), RateSource.CATALOG),
        "ci3": (D("0.7"), RateSource.OFFER),
        "ci4": (D("1.9"), RateSource.CATALOG),
        "ci5": (D("0.8"), RateSource.CATALOG),
    }
    assert {
        k: (slots[dunya.ci[k].id].unit_mhr, slots[dunya.ci[k].id].source) for k in beklenen
    } == beklenen
    assert dunya.ci["ci6"].id not in slots  # yuvasiz kalem
    assert [u.code for u in uyarilar] == ["no_rate_slot"]
    # santiye yok → revizyon/esleme/pencere YAZILMAZ
    assert await _rows(seeded_db, EvRevision) == []
    assert await _rows(seeded_db, EvGroupDiscipline) == []


async def test_sifir_oran_yuva_sayilmaz(seeded_db, proje, santiye, katalog) -> None:
    """`ev_contract_item_rates.unit_mhr > 0` CHECK'i: sifir yuva degil → uyari (kod patlamaz)."""
    dunya = await w.kur(seeded_db, proje, santiye, katalog, boq=False)
    req = w.istek(proje, None, dunya, {"ci1": (D("0"), True)})
    uyarilar = await contract_adapter.seed_hook(seeded_db, req)
    assert await _rows(seeded_db, EvContractItemRate) == []
    assert [u.code for u in uyarilar] == ["no_rate_slot"]


# ------------------------------------------------------------------ Rev.0 TASLAK (santiyeli)


async def test_santiyeli_rev0_taslak_dondurulmaz_baseline_yok(
    seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    kab, _duv = disiplinler
    dunya = await w.kur(seeded_db, proje, santiye, katalog)
    # engelsiz dunya: dondurulsaydi basarili olurdu → "taslak" iddiasi donduruculugu ayirir
    elle = {dunya.cg["Karışık"].id: kab.id, dunya.cg["Katalogsuz"].id: kab.id}
    req = w.istek(proje, santiye, dunya, w.TUM_ORANLAR, elle=elle)
    await contract_adapter.seed_hook(seeded_db, req)

    rev = await _rev(seeded_db, santiye)
    assert (rev.number, rev.status, rev.frozen_at) == (0, RevisionStatus.DRAFT, None)
    assert rev.name == "Rev.0 — TKL-TEST"
    for model in (EvBaselineLeaf, EvBaselineCurve):
        assert (await seeded_db.scalar(select(func.count()).select_from(model))) == 0


async def test_grup_disiplinleri_tek_disiplin_eslenir_karisik_uyari(
    seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    kab, duv = disiplinler
    dunya = await w.kur(seeded_db, proje, santiye, katalog)
    uyarilar = await contract_adapter.seed_hook(
        seeded_db, w.istek(proje, santiye, dunya, w.TUM_ORANLAR)
    )

    esleme = {r.boq_group_id: r.discipline_id for r in await _rows(seeded_db, EvGroupDiscipline)}
    assert esleme == {dunya.bg["Betonarme"].id: kab.id, dunya.bg["Duvar"].id: duv.id}
    karisik = [u for u in uyarilar if u.code == "mixed_discipline_group"]
    assert [u.group_id for u in karisik] == [dunya.cg["Karışık"].id]
    assert dunya.bg["Karışık"].id not in esleme  # eslenmedi


async def test_elle_esleme_karisik_grubu_cozer_ve_homojeni_ezer(
    seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    kab, duv = disiplinler
    dunya = await w.kur(seeded_db, proje, santiye, katalog)
    elle = {dunya.cg["Karışık"].id: duv.id, dunya.cg["Betonarme"].id: duv.id}
    uyarilar = await contract_adapter.seed_hook(
        seeded_db, w.istek(proje, santiye, dunya, w.TUM_ORANLAR, elle=elle)
    )

    esleme = {r.boq_group_id: r.discipline_id for r in await _rows(seeded_db, EvGroupDiscipline)}
    assert esleme[dunya.bg["Karışık"].id] == duv.id
    assert esleme[dunya.bg["Betonarme"].id] == duv.id  # elle > katalog (KAB)
    assert esleme[dunya.bg["Duvar"].id] == duv.id
    assert not [u for u in uyarilar if u.code == "mixed_discipline_group"]


async def test_catalog_baglari_ve_bolumsuz_oranlar_kaynakla(
    seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    dunya = await w.kur(seeded_db, proje, santiye, katalog)
    await contract_adapter.seed_hook(seeded_db, w.istek(proje, santiye, dunya, w.TUM_ORANLAR))

    rev = await _rev(seeded_db, santiye)
    baglar = {r.boq_item_id: r.catalog_item_id for r in await _rows(seeded_db, EvItemSettings)}
    assert baglar == {
        dunya.bi["ci1"].id: katalog["beton"].id,
        dunya.bi["ci2"].id: katalog["tugla2"].id,
        dunya.bi["ci3"].id: katalog["tugla"].id,
        dunya.bi["ci4"].id: katalog["beton"].id,
        dunya.bi["ci5"].id: katalog["tugla"].id,
    }  # ci6 katalogsuz → bag yok
    yapraklar = {
        r.boq_item_id: (r.section_id, r.unit_mhr, r.rate_source)
        for r in await _rows(seeded_db, EvLeafSettings, revision_id=rev.id)
    }
    assert yapraklar == {
        dunya.bi["ci1"].id: (None, D("2.5"), RateSource.OFFER),
        dunya.bi["ci2"].id: (None, D("0.6"), RateSource.CATALOG),
        dunya.bi["ci3"].id: (None, D("0.7"), RateSource.OFFER),
        dunya.bi["ci4"].id: (None, D("1.9"), RateSource.CATALOG),
        dunya.bi["ci5"].id: (None, D("0.8"), RateSource.CATALOG),
    }


async def test_yuvasiz_katalogsuz_kalem_oransiz_kalir(
    seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    """ci6: yuva yok, katalog yok → yaprak orani yazilmaz; ci3 yuvasiz ama kataloglu → katalog."""
    dunya = await w.kur(seeded_db, proje, santiye, katalog)
    oranlar = {"ci3": (None, False), "ci6": (None, False)}
    uyarilar = await contract_adapter.seed_hook(seeded_db, w.istek(proje, santiye, dunya, oranlar))

    rev = await _rev(seeded_db, santiye)
    yapraklar = {
        r.boq_item_id: (r.unit_mhr, r.rate_source)
        for r in await _rows(seeded_db, EvLeafSettings, revision_id=rev.id)
        if r.unit_mhr is not None
    }
    # SO-34: ci3 yuvasiz → katalog standardi (tugla 0.55) + catalog
    assert yapraklar[dunya.bi["ci3"].id] == (D("0.55"), RateSource.CATALOG)
    assert dunya.bi["ci6"].id not in yapraklar
    assert [u.code for u in uyarilar if u.code == "no_rate_slot"] == ["no_rate_slot"]


async def test_pencereler_eslenen_her_disipline_baslangic_bitis(
    seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    kab, duv = disiplinler
    dunya = await w.kur(seeded_db, proje, santiye, katalog)
    await contract_adapter.seed_hook(seeded_db, w.istek(proje, santiye, dunya, w.TUM_ORANLAR))

    rev = await _rev(seeded_db, santiye)
    pencereler = {
        (r.discipline_id, r.section_id): (r.start_date, r.end_date)
        for r in await _rows(seeded_db, EvWindow, revision_id=rev.id)
    }
    assert pencereler == {(kab.id, None): (w.START, w.END), (duv.id, None): (w.START, w.END)}


async def test_tarihsiz_projede_pencere_yazilmaz(
    seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    dunya = await w.kur(seeded_db, proje, santiye, katalog)
    req = w.istek(proje, santiye, dunya, w.TUM_ORANLAR, baslangic=None, bitis=None)
    await contract_adapter.seed_hook(seeded_db, req)
    assert await _rows(seeded_db, EvWindow) == []


async def test_santiyede_boq_satiri_olmayan_kalem_uyarisi(
    seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    dunya = await w.kur(seeded_db, proje, santiye, katalog)
    ekstra = EmployerContractItem(
        project_id=proje.id,
        group_id=dunya.cg["Duvar"].id,
        code="S-099",
        description="Dağıtılmamış",
        unit="m3",
        quantity=D(1),
        unit_price=D(1),
        sort_order=99,
        catalog_item_id=katalog["tugla"].id,
    )
    seeded_db.add(ekstra)
    await seeded_db.flush()
    dunya.ci["x"] = ekstra
    uyarilar = await contract_adapter.seed_hook(
        seeded_db, w.istek(proje, santiye, dunya, {**w.TUM_ORANLAR, "x": (D("1"), False)})
    )
    kodlar = [u.code for u in uyarilar]
    assert "item_not_in_site" in kodlar
    assert "1 sözleşme kalemi" in next(u.message for u in uyarilar if u.code == "item_not_in_site")


# ------------------------------------------------------------------ hata → geri alma, port


async def test_hata_yukselir_ve_cagiranin_islemi_geri_alinir(
    seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    from app.core.errors import NotFoundError

    dunya = await w.kur(seeded_db, proje, santiye, katalog)
    elle = {dunya.cg["Karışık"].id: uuid.uuid4()}  # olmayan disiplin
    req = w.istek(proje, santiye, dunya, w.TUM_ORANLAR, elle=elle)
    with pytest.raises(NotFoundError):
        async with seeded_db.begin_nested():
            await contract_seed.run_seed_hooks(seeded_db, req)
    # yuvalar da geri alindi (savepoint) — kismi yazim yok
    assert await _rows(seeded_db, EvContractItemRate) == []
    assert await _rows(seeded_db, EvRevision) == []


async def test_bekleyen_kayit_run_seed_hooks_ev_kancasini_calistirir(
    seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    dunya = await w.kur(seeded_db, proje, santiye, katalog, boq=False)
    contract_seed.unregister_all()
    contract_adapter.register()
    contract_adapter.register()  # idempotan
    assert contract_seed.registered() == (contract_adapter.seed_hook,)
    await contract_seed.run_seed_hooks(seeded_db, w.istek(proje, None, dunya, w.TUM_ORANLAR))
    assert len(await _rows(seeded_db, EvContractItemRate)) == 5


async def test_uygulama_acilinca_ev_kancasi_kayitli() -> None:
    from app.main import app  # noqa: F401  (import yan etkisi kaydi tetikler)

    beklenen = {"app.modules.earned_value.contract_adapter"}
    assert {h.__module__ for h in contract_seed.registered()} == beklenen


# ------------------------------------------------------------------ performans


@contextmanager
def _sayac():
    from tests.conftest import test_engine

    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)


async def _olcekli(db, proje, santiye, katalog, adet: int):
    grup = EmployerContractGroup(project_id=proje.id, name="Büyük", sort_order=1)
    db.add(grup)
    await db.flush()
    bgrup = BoqGroup(site_id=santiye.id, name="Büyük", sort_order=1)
    db.add(bgrup)
    await db.flush()
    cis, bis = [], []
    for n in range(adet):
        ci = EmployerContractItem(
            project_id=proje.id, group_id=grup.id, code=f"B{adet}-{n:04d}", description=f"K{n}",
            unit="m3", quantity=D(1), unit_price=D(1), sort_order=n,
            catalog_item_id=katalog["beton"].id,
        )  # fmt: skip
        cis.append(ci)
    db.add_all(cis)
    await db.flush()
    for n, ci in enumerate(cis):
        bis.append(
            BoqItem(
                site_id=santiye.id,
                group_id=bgrup.id,
                contract_item_id=ci.id,
                code=ci.code,
                description=ci.description,
                unit="m3",
                quantity=D(1),
                unit_price=D(1),
                sort_order=n,
            )  # fmt: skip
        )
    db.add_all(bis)
    await db.flush()
    return cis


async def test_sorgu_sayisi_kalem_sayisindan_bagimsiz(
    seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    from app.core.contract_seed import ContractItemSeed, ContractSeedRequest
    from app.modules.projects.models import ProjectContract
    from app.modules.sites.models import Site

    seeded_db.add(ProjectContract(project_id=proje.id, contract_no="B6-OLC", amount=D("1")))
    sayilar = {}
    for adet in (10, 40):
        site = Site(project_id=proje.id, code=f"EV-{adet}", name=f"S{adet}")
        seeded_db.add(site)
        await seeded_db.flush()
        cis = await _olcekli(seeded_db, proje, site, katalog, adet)
        req = ContractSeedRequest(
            project_id=proje.id, site_id=site.id, start=w.START, end=w.END,
            items=tuple(ContractItemSeed(c.id, c.catalog_item_id, D("1.5"), True) for c in cis),
            group_disciplines={}, actor_id=None, label="x",
        )  # fmt: skip
        with _sayac() as sorgular:
            await contract_adapter.seed_hook(seeded_db, req)
        sayilar[adet] = len(sorgular)
        assert (
            await seeded_db.scalar(
                select(func.count())
                .select_from(EvLeafSettings)
                .join(EvRevision, EvRevision.id == EvLeafSettings.revision_id)
                .where(EvRevision.site_id == site.id)
            )
        ) == adet
    assert sayilar[10] == sayilar[40], sayilar
    assert sayilar[40] < 40, sayilar  # kalem basina sorgu yok

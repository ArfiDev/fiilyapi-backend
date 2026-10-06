"""IZN-B4d (earned_value 1/2) — bütçe / katalog / günlük / ayar yüzeyi hassas alan maskesi.

🔴 Bu yüzey TL TAŞIMAZ: bütçe adam-saat (`budget_mhr`, `unit_mhr`), saat, miktar, oran, endeks.
GECE KARARI: hepsi `Hassas.yok` (bkz. `earned_value/mask_types.py`). Bu yüzden:

* HERHANGİ bir gizli kategori kümesi (`maliyet_kar`, `sozlesme_fiyat`, `tum_tutarlar`, hepsi) bütçe
  yanıtını DEĞİŞTİRMEZ — adam-saat maliyet/kâr/sözleşme fiyatı değildir (POZİTİF + NEGATİF kontrol);
* `maliyet_kar` gizli rol `unit_mhr`'yi (PATCH gövdesinde) yine yazabilir: gövdede hassas alan yok,
  yazma kapısı 403 vermez;
* sözleşme kökenli TL (kazanılmış/planlanan değer TL'si) rapor şemalarındadır → 2/2.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.field_mask import Hassas, etiketler
from app.core.mask_route import MaskeRotasi
from app.core.sayfalar import HiddenCategory
from app.modules.earned_value import (
    catalog_router,
    day_router,
    report_router,
    router,
    schemas_budget,
    schemas_catalog,
    schemas_day,
    schemas_settings,
    settings_router,
)
from tests._hassas_alan import rol_gizle

from .test_budget_api import _map, _rates, _url

pytestmark = pytest.mark.asyncio

_HEPSI = tuple(HiddenCategory)

_KUMELER = {
    "maliyet_kar": (HiddenCategory.maliyet_kar,),
    "sozlesme_fiyat": (HiddenCategory.sozlesme_fiyat,),
    "sozlesme_ve_maliyet": (HiddenCategory.sozlesme_fiyat, HiddenCategory.maliyet_kar),
    "tum_tutarlar": (HiddenCategory.tum_tutarlar,),
    "hepsi": _HEPSI,
}


@pytest.fixture
async def dolu_butce(client, admin, santiye, boq, disiplinler) -> None:
    await _map(client, santiye, admin, boq, disiplinler)
    await _rates(client, santiye, admin, boq)


async def test_bayraksiz_rol_adam_saat_butcesini_gorur_pozitif_kontrol(
    client, sef, seeded_db, santiye, dolu_butce
):
    await rol_gizle(seeded_db, "site_chief")  # hiçbir kategori gizli değil

    gorunum = (await client.get(_url(santiye), headers=sef)).json()

    assert Decimal(gorunum["totals"]["direct_budget_mhr"]) == Decimal(225)  # I1 200 + I2 25
    yaprak = next(
        lf
        for d in gorunum["disciplines"]
        for g in d["groups"]
        for i in g["items"]
        for lf in i["leaves"]
        if i["code"] == "01.001" and lf["section_name"] == "A Blok"
    )
    assert Decimal(yaprak["unit_mhr"]) == 2
    assert Decimal(yaprak["budget_mhr"]) == 120
    assert Decimal(yaprak["planned_qty"]) == 60


@pytest.mark.parametrize("ad", list(_KUMELER))
async def test_gizli_kategori_adam_saat_butcesini_gizlemez(
    ad, client, sef, seeded_db, santiye, dolu_butce
):
    await rol_gizle(seeded_db, "site_chief")
    acik = (await client.get(_url(santiye), headers=sef)).json()

    await rol_gizle(seeded_db, "site_chief", *_KUMELER[ad])
    maskeli = (await client.get(_url(santiye), headers=sef)).json()

    # Adam-saat TL değildir: gizli roller de aynı bütçeyi görür (null YOK).
    assert maskeli == acik
    assert maskeli["totals"]["direct_budget_mhr"] is not None


async def test_maliyet_kar_gizli_rol_unit_mhr_yazabilir_403_degil(
    client, sef, seeded_db, santiye, boq, dolu_butce
):
    await rol_gizle(seeded_db, "site_chief", *_HEPSI)

    yanit = await client.patch(
        _url(santiye, "/leaves"),
        headers=sef,
        json={
            "leaves": [
                {"boq_item_id": str(boq["i2"].id), "section_id": str(boq["s1"].id), "unit_mhr": "1"}
            ]
        },
    )

    assert yanit.status_code == 200, yanit.text
    yaprak = next(
        lf
        for d in yanit.json()["disciplines"]
        for g in d["groups"]
        for i in g["items"]
        for lf in i["leaves"]
        if i["code"] == "02.001"
    )
    assert Decimal(yaprak["unit_mhr"]) == 1
    assert Decimal(yaprak["budget_mhr"]) == 50  # 50 m2 × 1


async def test_bu_adimin_semalari_para_etiketi_tasimaz_hepsi_yok():
    for modul in (schemas_budget, schemas_catalog, schemas_day, schemas_settings):
        for sema in vars(modul).values():
            if not (isinstance(sema, type) and hasattr(sema, "model_fields")):
                continue
            if sema.__module__ != modul.__name__:
                continue
            for ad, alan in sema.model_fields.items():
                kategoriler = etiketler(alan)
                assert kategoriler <= {Hassas.yok}, f"{sema.__name__}.{ad}: {kategoriler}"


async def test_tum_earned_value_routerlari_maske_rotasi_tasir():
    for modul in (
        router,
        catalog_router,
        day_router,
        settings_router,
        report_router,
    ):
        assert modul.router.routes, modul.__name__
        for rota in modul.router.routes:
            assert isinstance(rota, MaskeRotasi), f"{modul.__name__}: {rota}"

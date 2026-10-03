# ruff: noqa: F811  (fikstürler test_tkl_b3_katalog_bagi'den ice aktarilir, parametre adi ayni)
"""KAT-B2.1 — isveren sozlesme kalemine Bakanlik poz no'su (`source_code`) SNAPSHOT'i.

Katalog bagi varsa sunucu katalogdan kopyalar (tekil + toplu); baglisiz (elle) kalem NULL
(K2); istemci Create/Update govdesinde gonderemez (422); katalogdaki kod sonradan
degisse/temizlense/kalem silinse kopya korunur; `code` ve BOQ ayna kumesi DEGISMEZ.
"""

import uuid

import pytest

from app.modules.boq.models import BoqItem
from app.modules.catalog.models import EvCatalogItem
from app.modules.contracts.service import MIRRORED_ITEM_FIELDS

from .test_tkl_b3_katalog_bagi import (  # noqa: F401  (fikstürler)
    _govde,
    _satir,
    grup,
    katalog,
    proje,
)

BASE = "/projects/{}/contract/items"


@pytest.fixture
async def kodlu(seeded_db, katalog) -> list[uuid.UUID]:
    for kimlik, kod in zip(katalog, ("15.100.1001", None, "15.100.1003"), strict=True):
        (await seeded_db.get(EvCatalogItem, kimlik)).source_code = kod
    await seeded_db.flush()
    return katalog


async def _kod(db, item_id) -> str | None:
    return (await _satir(db, item_id)).source_code


async def test_tekil_bagli_kalem_katalog_kodunu_kopyalar_yanitta_doner(
    client, admin_headers, proje, grup, kodlu, seeded_db
):
    yanit = await client.post(
        BASE.format(proje.id),
        json=_govde(grup, catalog_item_id=str(kodlu[0])),
        headers=admin_headers,
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["source_code"] == "15.100.1001"
    assert yanit.json()["code"] == "03.001"  # code'a dokunulmaz (K1)
    assert await _kod(seeded_db, yanit.json()["id"]) == "15.100.1001"
    liste = await client.get(BASE.format(proje.id), headers=admin_headers)
    kalemler = [i for g in liste.json()["groups"] for i in g["items"]]
    assert [i["source_code"] for i in kalemler] == ["15.100.1001"]


async def test_tekil_katalog_kodu_bos_ya_da_baglisiz_kalem_NULL(
    client, admin_headers, proje, grup, kodlu
):
    bos = await client.post(
        BASE.format(proje.id),
        json=_govde(grup, "A", catalog_item_id=str(kodlu[1])),
        headers=admin_headers,
    )
    baglisiz = await client.post(
        BASE.format(proje.id), json=_govde(grup, "B"), headers=admin_headers
    )
    assert bos.json()["source_code"] is None
    assert baglisiz.status_code == 201 and baglisiz.json()["source_code"] is None


async def test_toplu_katalog_kodunu_tek_sorguyla_kopyalar(
    client, admin_headers, proje, grup, kodlu
):
    govde = {
        "items": [
            _govde(grup, "A", catalog_item_id=str(kodlu[0])),
            _govde(grup, "B", catalog_item_id=str(kodlu[1])),
            _govde(grup, "C", catalog_item_id=str(kodlu[2])),
            _govde(grup, "D"),
            _govde(grup, "E", catalog_item_id=str(kodlu[0])),
        ]
    }
    yanit = await client.post(BASE.format(proje.id) + "/bulk", json=govde, headers=admin_headers)
    assert yanit.status_code == 201, yanit.text
    assert [i["source_code"] for i in yanit.json()["items"]] == [
        "15.100.1001",
        None,
        "15.100.1003",
        None,
        "15.100.1001",
    ]


async def test_SNAPSHOT_katalog_kodu_degisir_temizlenir_kalem_silinir_kopya_korunur(
    client, admin_headers, proje, grup, kodlu, seeded_db
):
    tekil = await client.post(
        BASE.format(proje.id),
        json=_govde(grup, "A", catalog_item_id=str(kodlu[0])),
        headers=admin_headers,
    )
    toplu = await client.post(
        BASE.format(proje.id) + "/bulk",
        json={"items": [_govde(grup, "B", catalog_item_id=str(kodlu[2]))]},
        headers=admin_headers,
    )
    (await seeded_db.get(EvCatalogItem, kodlu[0])).source_code = "99.9.9"
    (await seeded_db.get(EvCatalogItem, kodlu[2])).source_code = None
    await seeded_db.flush()
    assert await _kod(seeded_db, tekil.json()["id"]) == "15.100.1001"
    assert await _kod(seeded_db, toplu.json()["items"][0]["id"]) == "15.100.1003"
    # katalog kalemi SILININCE (FK SET NULL) kopya yine korunur
    silinen = await seeded_db.get(EvCatalogItem, kodlu[0])
    await seeded_db.delete(silinen)
    await seeded_db.flush()
    satir = await _satir(seeded_db, tekil.json()["id"])
    assert satir.catalog_item_id is None and satir.source_code == "15.100.1001"
    # PATCH baska alan kopyaya dokunmaz
    yama = await client.patch(
        f"/contracts/employer/items/{tekil.json()['id']}",
        json={"description": "Yeni"},
        headers=admin_headers,
    )
    assert yama.status_code == 200 and yama.json()["source_code"] == "15.100.1001"


@pytest.mark.parametrize("deger", ["X.1", None])
async def test_K2_istemci_source_code_gonderemez_tekil_toplu_PATCH_422(
    client, admin_headers, proje, grup, kodlu, seeded_db, deger
):
    tekil = await client.post(
        BASE.format(proje.id),
        json=_govde(grup, "A", source_code=deger),
        headers=admin_headers,
    )
    toplu = await client.post(
        BASE.format(proje.id) + "/bulk",
        json={"items": [_govde(grup, "B", source_code=deger, catalog_item_id=str(kodlu[0]))]},
        headers=admin_headers,
    )
    assert tekil.status_code == 422 and "source_code" in tekil.text
    assert toplu.status_code == 422 and "source_code" in toplu.text
    var = await client.post(
        BASE.format(proje.id),
        json=_govde(grup, "C", catalog_item_id=str(kodlu[0])),
        headers=admin_headers,
    )
    yama = await client.patch(
        f"/contracts/employer/items/{var.json()['id']}",
        json={"source_code": deger},
        headers=admin_headers,
    )
    assert yama.status_code == 422 and "source_code" in yama.text
    assert await _kod(seeded_db, var.json()["id"]) == "15.100.1001"
    # hicbir sey yazilmadi (tekil + toplu reddedildi): yalniz "C" var
    liste = await client.get(BASE.format(proje.id), headers=admin_headers)
    assert [i["code"] for g in liste.json()["groups"] for i in g["items"]] == ["C"]


def test_source_code_BOQ_ayna_kumesine_girmez():
    assert "source_code" not in MIRRORED_ITEM_FIELDS
    assert not hasattr(BoqItem, "source_code")

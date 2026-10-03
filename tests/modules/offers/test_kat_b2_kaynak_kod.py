"""KAT-B2.1 — Bakanlik poz no'su (`source_code`) teklif kalemine SNAPSHOT olarak tasinir.

Yollar: tekil ekleme · toplu ekleme · sablondan teklif · tekliften kopya · yeni revizyon ·
donusturme (K3: kaynak YALNIZ teklif snapshot'i). Degismezlik: katalogdaki kod sonradan
degisse/temizlense kalem eski kodu korur; istemci gonderemez, PATCH'te degistiremez (422).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.modules.contracts.models import EmployerContractItem

from ._convert import govde, kalem_govdesi, kazanilmis_teklif
from ._offers import (
    URL,
    gecis,
    grup,
    kalem,
    rev_url,
    revizyon,
    sablon,
    sablon_icerik,
    teklif,
    tum_kalemler,
)

pytestmark = pytest.mark.usefixtures("son_fiyat")

KOD = ["15.100.1001", None, "15.100.1003"]  # Beton, Kalip (kodsuz), Demir


@pytest.fixture(autouse=True)
async def kodlu_katalog(seeded_db, katalog):
    for entry, kod in zip(katalog, KOD, strict=True):
        entry.source_code = kod
    await seeded_db.flush()
    return katalog


async def _katalog_degistir(db, katalog, yeni: list[str | None]) -> None:
    for entry, kod in zip(katalog, yeni, strict=True):
        entry.source_code = kod
    await db.flush()


def _kodlar(rev: dict) -> dict[str, str | None]:
    return {k["description"]: k["source_code"] for k in tum_kalemler(rev)}


BEKLENEN = {"Beton": "15.100.1001", "Kalıp": None, "Demir": "15.100.1003"}


async def _uc_kalemli(client, admin, isveren, katalog) -> dict:
    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    for entry in katalog:
        await kalem(client, admin, o["id"], g["id"], entry.id, cost_unit_price="10")
    return o


async def test_tekil_ekleme_katalog_kodunu_kopyalar_ve_yanitta_doner(
    client, admin, isveren, katalog
) -> None:
    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    dolu = await kalem(client, admin, o["id"], g["id"], katalog[0].id)
    bos = await kalem(client, admin, o["id"], g["id"], katalog[1].id)
    assert dolu["source_code"] == "15.100.1001"
    assert bos["source_code"] is None
    assert _kodlar(await revizyon(client, admin, o["id"])) == {
        "Beton": "15.100.1001",
        "Kalıp": None,
    }


async def test_toplu_ekleme_katalog_kodunu_kopyalar(client, admin, isveren, katalog) -> None:
    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    govde_ = {"items": [{"catalog_item_id": str(k.id), "group_id": g["id"]} for k in katalog]}
    resp = await client.post(rev_url(o["id"]) + "/items/bulk", json=govde_, headers=admin)
    assert resp.status_code == 201, resp.text
    assert [i["source_code"] for i in resp.json()["items"]] == KOD


async def test_SNAPSHOT_katalog_kodu_degisince_ya_da_temizlenince_kalem_eski_kodu_korur(
    client, admin, isveren, katalog, seeded_db
) -> None:
    o = await _uc_kalemli(client, admin, isveren, katalog)
    await _katalog_degistir(seeded_db, katalog, ["99.0.1", "99.0.2", None])
    assert _kodlar(await revizyon(client, admin, o["id"])) == BEKLENEN


async def test_istemci_source_code_gonderemez_ekleme_ve_toplu_ve_PATCH_422(
    client, admin, isveren, katalog
) -> None:
    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    govde_ = {"catalog_item_id": str(katalog[0].id), "group_id": g["id"], "source_code": "X.1"}
    assert (
        await client.post(rev_url(o["id"]) + "/items", json=govde_, headers=admin)
    ).status_code == 422
    assert (
        await client.post(rev_url(o["id"]) + "/items/bulk", json={"items": [govde_]}, headers=admin)
    ).status_code == 422
    k = await kalem(client, admin, o["id"], g["id"], katalog[0].id)
    resp = await client.patch(
        rev_url(o["id"]) + f"/items/{k['id']}", json={"source_code": "X.1"}, headers=admin
    )
    assert resp.status_code == 422 and "source_code" in resp.text
    assert "katalogdan gelen alan değiştirilemez" in resp.text  # _IMMUTABLE listesi (strict degil)
    assert _kodlar(await revizyon(client, admin, o["id"])) == {"Beton": "15.100.1001"}


async def test_sablondan_teklif_kodu_kopyalar_ve_sablon_sonrasi_degisim_etkilemez(
    client, admin, isveren, katalog, seeded_db
) -> None:
    sab = await sablon(client, admin, "Villa")
    await sablon_icerik(client, admin, sab["id"], [("Kaba", [k.id for k in katalog])])
    resp = await client.post(
        URL,
        json={"employer_id": str(isveren.id), "title": "Ş", "template_id": sab["id"]},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    oid = resp.json()["id"]
    assert _kodlar(await revizyon(client, admin, oid)) == BEKLENEN
    await _katalog_degistir(seeded_db, katalog, [None, "1.1", "2.2"])
    assert _kodlar(await revizyon(client, admin, oid)) == BEKLENEN


async def test_kopya_ve_yeni_revizyon_snapshot_AYNEN_tasir_katalogdan_yeniden_okumaz(
    client, admin, isveren, katalog, seeded_db
) -> None:
    o = await _uc_kalemli(client, admin, isveren, katalog)
    await gecis(client, admin, o["id"], "send")
    assert (await gecis(client, admin, o["id"], "lose", lost_reason="x")).status_code == 200
    await _katalog_degistir(seeded_db, katalog, ["99.0.1", "99.0.2", "99.0.3"])  # kopyadan ONCE

    r = await client.post(f"{URL}/{o['id']}/revisions", headers=admin)
    assert r.status_code == 201, r.text
    assert _kodlar(await revizyon(client, admin, o["id"], 1)) == BEKLENEN

    k = await client.post(
        URL, json={"copy_from": {"offer_id": o["id"], "rev_no": 0}}, headers=admin
    )
    assert k.status_code == 201, k.text
    assert _kodlar(await revizyon(client, admin, k.json()["id"])) == BEKLENEN


async def _donustur(client, admin, seeded_db, kz, ekstra: list[dict]) -> dict[str, str | None]:
    """`govde(kz)` + ilk gruba `ekstra` satirlar; donen: sozlesme kalemi code → source_code."""
    g = govde(kz)
    g["groups"][0]["items"].extend(ekstra)
    resp = await client.post(f"/offers/{kz.offer_id}/convert", json=g, headers=admin)
    assert resp.status_code == 200, resp.text
    satirlar = (
        await seeded_db.scalars(
            select(EmployerContractItem).where(
                EmployerContractItem.project_id == uuid.UUID(resp.json()["project_id"])
            )
        )
    ).all()
    return {s.code: s.source_code for s in satirlar}


def _bagsiz(kz, ad: str, code: str) -> dict:
    """Ekranda katalogdan eklenen satir: `offer_item_id` YOK, `catalog_item_id` var."""
    return kalem_govdesi(kz.kalemler[ad], code, offer_item_id=None)


async def test_donusturme_K3_teklif_snapshoti_katalog_sonradan_dolsa_da_NULL(
    client, admin, isveren, katalog, seeded_db, tohum_kancasi
) -> None:
    kz = await kazanilmis_teklif(client, admin, isveren, katalog)
    # Kalip teklifte NULL; katalog SONRADAN dolar. Beton/Demir kodu degisir.
    await _katalog_degistir(seeded_db, katalog, ["88.0.1", "88.0.2", "88.0.3"])
    kodlar = await _donustur(client, admin, seeded_db, kz, [])
    assert kodlar["B-01"] == "15.100.1001"  # (1) teklif snapshot'i (katalog artik 88.0.1)
    assert kodlar["B-02"] is None  # (1) snapshot NULL → NULL, katalog OKUNMAZ
    assert kodlar["I-01"] == "15.100.1003"


async def test_donusturme_teklif_kalemi_yoksa_katalogdan_kopya_kodlu_ve_kodsuz(
    client, admin, isveren, katalog, seeded_db, tohum_kancasi
) -> None:
    kz = await kazanilmis_teklif(client, admin, isveren, katalog)
    await _katalog_degistir(seeded_db, katalog, ["88.0.1", None, "88.0.3"])
    kodlar = await _donustur(
        client, admin, seeded_db, kz, [_bagsiz(kz, "Beton", "X-01"), _bagsiz(kz, "Kalıp", "X-02")]
    )
    assert kodlar["X-01"] == "88.0.1"  # (2) katalog kodlu → kopyalanir (bugunku katalog)
    assert kodlar["X-02"] is None  # (2) katalog kodu NULL → NULL
    assert kodlar["I-02"] == "88.0.3"  # govde(kz)'in bagsiz Demir satiri (2)
    # karisik tek donusturme: (1) ve (2) birlikte
    assert kodlar["B-01"] == "15.100.1001" and kodlar["B-02"] is None

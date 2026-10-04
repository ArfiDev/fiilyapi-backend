"""DSC-B1 → IZN-B3 — EV disiplin listesi + katalog: sirket geneli, kapsamla SUZULMEZ.

`recent_actuals` / katalog "gerceklesen" proje kapsami bu dilimde DEGISMEZ (K6, Ü8 ertelendi).
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests._disiplin_dunyasi import Dunya


async def _liste(client: AsyncClient, url: str, baslik) -> list[dict]:
    resp = await client.get(url, headers=baslik)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_disiplin_listesi_kisitliya_da_TAM_gorunur(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    """IZN-B3: disiplin PROJE BAŞINA atanır; şirket disiplin listesi kapsamla SÜZÜLMEZ."""
    hepsi = {x["id"] for x in await _liste(client, "/earned-value/disciplines", atamasiz)}
    c = {x["id"] for x in await _liste(client, "/earned-value/disciplines", civil)}
    e = {x["id"] for x in await _liste(client, "/earned-value/disciplines", elek)}
    assert hepsi == {str(dunya.kab.id), str(dunya.duv.id)}
    assert c == e == hepsi


async def test_katalog_kisitliya_da_TAM_gorunur(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    ad = {
        k: {x["name"] for x in await _liste(client, "/earned-value/catalog", b)}
        for k, b in (("hepsi", atamasiz), ("civil", civil), ("elek", elek))
    }
    assert ad == {
        "hepsi": {"Beton", "Tuğla"},
        "civil": {"Beton", "Tuğla"},
        "elek": {"Beton", "Tuğla"},
    }


async def test_katalog_disiplin_sorgusu_kisitliya_da_atamasizla_ayni(
    client: AsyncClient, dunya: Dunya, atamasiz, civil
) -> None:
    olmayan = await _liste(client, f"/earned-value/catalog?discipline_id={uuid.uuid4()}", civil)
    assert olmayan == []
    for baslik in (civil, atamasiz):
        kendi = await _liste(client, f"/earned-value/catalog?discipline_id={dunya.kab.id}", baslik)
        assert [x["name"] for x in kendi] == ["Beton"]
        diger = await _liste(client, f"/earned-value/catalog?discipline_id={dunya.duv.id}", baslik)
        assert [x["name"] for x in diger] == ["Tuğla"]

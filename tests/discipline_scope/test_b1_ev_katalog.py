"""DSC-B1 — EV disiplin listesi + katalog (Ü8/K6): kisitliya yalniz KENDI disiplinleri.

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


async def test_disiplin_listesi_kisitliya_yalniz_kendi_disiplinleri(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    hepsi = {x["id"] for x in await _liste(client, "/earned-value/disciplines", atamasiz)}
    c = {x["id"] for x in await _liste(client, "/earned-value/disciplines", civil)}
    e = {x["id"] for x in await _liste(client, "/earned-value/disciplines", elek)}
    assert hepsi == {str(dunya.kab.id), str(dunya.duv.id)}
    assert c == {str(dunya.kab.id)} and e == {str(dunya.duv.id)}
    assert c | e == hepsi


async def test_katalog_kisitliya_yalniz_kendi_disiplininin_kalemleri(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    ad = {
        k: {x["name"] for x in await _liste(client, "/earned-value/catalog", b)}
        for k, b in (("hepsi", atamasiz), ("civil", civil), ("elek", elek))
    }
    assert ad == {"hepsi": {"Beton", "Tuğla"}, "civil": {"Beton"}, "elek": {"Tuğla"}}


async def test_katalog_yabanci_disiplin_sorgusu_bos_olmayan_disiplinle_ayni(
    client: AsyncClient, dunya: Dunya, atamasiz, civil
) -> None:
    yabanci = await _liste(client, f"/earned-value/catalog?discipline_id={dunya.duv.id}", civil)
    olmayan = await _liste(client, f"/earned-value/catalog?discipline_id={uuid.uuid4()}", civil)
    assert yabanci == olmayan == []
    kendi = await _liste(client, f"/earned-value/catalog?discipline_id={dunya.kab.id}", civil)
    assert [x["name"] for x in kendi] == ["Beton"]
    # atamasiz ayni sorguda kalemi gorur (suzgec yalniz kisitliya)
    assert (
        len(await _liste(client, f"/earned-value/catalog?discipline_id={dunya.duv.id}", atamasiz))
        == 1
    )

"""DSC-B1 — stok satır okumaları (`GET /stock/entries` · `GET /sections/{id}/stock`).

Dünya stok hareketleri: alım (I1 · I2 · NULL) · transfer (yalnız NULL) · sarf (I1·S1 · I2·S1 ·
NULL·S1) · sarf-2 (I1·S2 · NULL·S2). Ü1 sonucu (K8 kabul): NULL kalemli satır kısıtlıya
görünmez → alım/transfer satırlarının çoğu kısıtlıdan gizlidir.
"""

from __future__ import annotations

from decimal import Decimal

from httpx import AsyncClient

from tests._disiplin_dunyasi import Dunya

D = Decimal


async def _hareketler(client: AsyncClient, baslik, kuyruk: str = "") -> dict:
    resp = await client.get(f"/stock/entries{kuyruk}", headers=baslik)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _satirlar(govde: dict) -> dict[str, set[str | None]]:
    """hareket kimliği → satırlarının boq_item_id kümesi."""
    return {e["id"]: {ln["boq_item_id"] for ln in e["lines"]} for e in govde["items"]}


async def test_hareket_listesi_iki_aktor_ayrik_ve_satirlari_gorunur_kalemle_sinirli(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    h = {ad: str(x.id) for ad, x in dunya.stok_hareketi.items()}
    i1, i2 = str(dunya.i["i1"].id), str(dunya.i["i2"].id)
    hepsi = await _hareketler(client, atamasiz)
    c, e = await _hareketler(client, civil), await _hareketler(client, elek)

    assert set(_satirlar(hepsi)) == set(h.values()) and hepsi["total"] == 4
    assert _satirlar(c) == {h["alim"]: {i1}, h["sarf"]: {i1}, h["sarf_yabanci"]: {i1}}
    assert _satirlar(e) == {h["alim"]: {i2}, h["sarf"]: {i2}}
    assert c["total"] == 3 and e["total"] == 2  # sayac liste ile ayni kume
    # transfer yalniz NULL satirli: kimseye (kisitli) gorunmez; atamasiz gorur
    assert h["transfer"] not in _satirlar(c) and h["transfer"] not in _satirlar(e)
    assert h["transfer"] in _satirlar(hepsi)


async def test_hareket_listesi_null_satir_kisitliya_hic_sizmaz(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    for baslik in (civil, elek):
        govde = await _hareketler(client, baslik)
        assert None not in {ln["boq_item_id"] for e in govde["items"] for ln in e["lines"]}
    tam = await _hareketler(client, atamasiz)
    assert None in {ln["boq_item_id"] for e in tam["items"] for ln in e["lines"]}


async def test_hareket_listesi_diger_suzgecler_kumeyi_daraltir_sayac_tutarli(
    client: AsyncClient, dunya: Dunya, civil, elek
) -> None:
    """`entry_type=transfer`: yalnız NULL satır → iki kısıtlı da BOŞ liste, total 0."""
    for baslik in (civil, elek):
        govde = await _hareketler(client, baslik, "?entry_type=transfer")
        assert govde["items"] == [] and govde["total"] == 0
    sarf = await _hareketler(client, civil, "?entry_type=adjustment&limit=1")
    assert len(sarf["items"]) == 1 and sarf["total"] == 2  # sayfa 1 ama toplam kume 2


async def _bolum_stok(client: AsyncClient, d: Dunya, baslik, bolum: str) -> dict:
    resp = await client.get(f"/sections/{getattr(d, bolum).id}/stock", headers=baslik)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_bolum_stok_satir_sayac_ve_kpi_ayni_kumeden(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    hepsi = await _bolum_stok(client, dunya, atamasiz, "s1")
    c = await _bolum_stok(client, dunya, civil, "s1")
    e = await _bolum_stok(client, dunya, elek, "s1")
    i1, i2 = str(dunya.i["i1"].id), str(dunya.i["i2"].id)

    assert {r["boq_item_id"] for r in hepsi["items"]} == {i1, i2, None}
    assert {r["boq_item_id"] for r in c["items"]} == {i1} and c["total"] == 1
    assert {r["boq_item_id"] for r in e["items"]} == {i2} and e["total"] == 1
    # civil: 30 x 12.50 = 375.00 sarf; elek: 12 x 8.00 = 96.00
    # (NULL satir -4 x 12.5 = 50 kisitliya DAHIL DEGIL)
    assert D(c["kpis"]["issued_value"]) == D("375.00") and c["kpis"]["item_count"] == 1
    assert D(e["kpis"]["issued_value"]) == D("96.00") and e["kpis"]["item_count"] == 1
    assert D(hepsi["kpis"]["issued_value"]) == D("521.00")  # 375 + 96 + 50
    for govde in (c, e):
        assert D(govde["kpis"]["total_value"]) == sum(
            (D(r["total_value"]) for r in govde["items"]), D(0)
        )
        assert govde["kpis"]["lines_without_price"] == 0


async def test_bolum_stok_baska_bolum_yalniz_kendi_satirlari(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    """S2: I1 -6 (k1 x 12.50) ve NULL -2 (fiyatsız). elek'e HİÇ satır düşmez; KPI sıfır."""
    hepsi = await _bolum_stok(client, dunya, atamasiz, "s2")
    c = await _bolum_stok(client, dunya, civil, "s2")
    e = await _bolum_stok(client, dunya, elek, "s2")
    assert hepsi["total"] == 2 and hepsi["kpis"]["lines_without_price"] == 1
    assert c["total"] == 1 and D(c["kpis"]["issued_value"]) == D("75.00")
    assert e["items"] == [] and e["total"] == 0
    assert D(e["kpis"]["issued_value"]) == 0 and e["kpis"]["item_count"] == 0
    assert e["kpis"]["lines_without_price"] == 0

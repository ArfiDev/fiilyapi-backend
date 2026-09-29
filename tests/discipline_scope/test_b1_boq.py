"""DSC-B1 — BOQ okuma uçları (GET boq · export · allocations) iki aktörlü görünürlük.

Dünya: I1(G1→KAB=civil) · I2(G2→DUV=elek) · I3(G3 eşlemesiz → Ü1: kısıtlıya görünmez).
civil ve elek AYNI sıradan rol + AYNI proje erişimine sahiptir; tek fark disiplin atamasıdır.
"""

from __future__ import annotations

import io
import uuid
from decimal import Decimal

from httpx import AsyncClient
from openpyxl import load_workbook

from tests._disiplin_dunyasi import Dunya

D = Decimal


def _kalemler(govde: dict) -> set[str]:
    return {i["id"] for g in govde["groups"] for i in g["items"]}


async def _boq(client: AsyncClient, d: Dunya, baslik: dict[str, str], kuyruk: str = "") -> dict:
    resp = await client.get(f"/sites/{d.santiye.id}/boq{kuyruk}", headers=baslik)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _tutar(govde: dict) -> Decimal:
    return sum((D(i["amount"]) for g in govde["groups"] for i in g["items"]), D(0))


async def test_iki_aktor_ayrik_kalem_kumesi_gorur_birlesim_atamasizin_nullsuz_hali(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    i = {ad: str(k.id) for ad, k in dunya.i.items()}
    hepsi = await _boq(client, dunya, atamasiz)
    c = await _boq(client, dunya, civil)
    e = await _boq(client, dunya, elek)
    assert _kalemler(hepsi) == {i["i1"], i["i2"], i["i3"]}
    assert _kalemler(c) == {i["i1"]}
    assert _kalemler(e) == {i["i2"]}
    assert _kalemler(c) & _kalemler(e) == set()
    assert _kalemler(c) | _kalemler(e) == _kalemler(hepsi) - {i["i3"]}


async def test_bos_grup_kalmaz_ve_grand_total_kendi_kalemlerinin_toplami(
    client: AsyncClient, dunya: Dunya, civil, elek
) -> None:
    for baslik, grup, tutar in ((civil, "g1", D("1000.00")), (elek, "g2", D("1000.00"))):
        govde = await _boq(client, dunya, baslik)
        assert [g["id"] for g in govde["groups"]] == [str(dunya.g[grup].id)]
        assert D(govde["totals"]["grand_total"]) == tutar == _tutar(govde)
        assert D(govde["groups"][0]["group_total"]) == tutar


async def test_grand_progress_pct_kendi_disiplininden_hesaplanir(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    """Sirket geneli %8,13; civil yalniz I1 (10/100) → %10,00; elek yalniz I2 (5/50) → %10,00.
    Satirlar suzuk ama agrega sirket geneli olsaydi (yarim suzme) civil %8,13 gorurdu."""
    pct = {
        ad: (await _boq(client, dunya, h))["totals"]["grand_progress_pct"]
        for ad, h in (("hepsi", atamasiz), ("civil", civil), ("elek", elek))
    }
    assert pct["hepsi"]["value"] == "8.13"
    assert pct["civil"]["value"] == "10.00"
    assert pct["elek"]["value"] == "10.00"


async def test_bolum_suzgeci_ve_bolum_yuzdesi_kendi_kumesinden(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    s1 = f"?section_id={dunya.s1.id}"
    hepsi = await _boq(client, dunya, atamasiz, s1)
    c = await _boq(client, dunya, civil, s1)
    e = await _boq(client, dunya, elek, s1)
    assert _kalemler(c) == {str(dunya.i["i1"].id)}
    assert _kalemler(e) == {str(dunya.i["i2"].id)}
    assert _kalemler(c) | _kalemler(e) == _kalemler(hepsi)
    # S1 tahsisi: I1 60 (gerceklesen 5) → 8,33 · I2 50 (gerceklesen 4+1) → 10,00 · birlesik 9,38
    assert c["totals"]["grand_progress_pct"]["value"] == "8.33"
    assert e["totals"]["grand_progress_pct"]["value"] == "10.00"
    assert hepsi["totals"]["grand_progress_pct"]["value"] == "9.38"
    assert D(c["totals"]["grand_total"]) == _tutar(c)


async def test_yalniz_I3_bolumu_kisitliya_bos_liste(
    client: AsyncClient, dunya: Dunya, civil, elek
) -> None:
    """S2'de I3 (disiplinsiz) ve I1 var: civil yalniz I1'i, elek HIC kalem gormez."""
    govde_c = await _boq(client, dunya, civil, f"?section_id={dunya.s2.id}")
    govde_e = await _boq(client, dunya, elek, f"?section_id={dunya.s2.id}")
    assert _kalemler(govde_c) == {str(dunya.i["i1"].id)}
    assert govde_e["groups"] == []
    assert D(govde_e["totals"]["grand_total"]) == 0


async def test_export_ayni_kumeyi_yazar(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    async def hucreler(baslik: dict[str, str]) -> dict[int, list]:
        resp = await client.get(f"/sites/{dunya.santiye.id}/boq/export", headers=baslik)
        assert resp.status_code == 200, resp.text
        sayfa = load_workbook(io.BytesIO(resp.content)).active
        return {r[0].row: [c.value for c in r] for r in sayfa.iter_rows()}

    def poz(satirlar: dict[int, list]) -> set[str]:
        return {r[0] for r in satirlar.values() if r[0] and r[0][:2].isdigit() and r[0][2:3] == "."}

    def genel(satirlar: dict[int, list]) -> str:
        return next(r[5] for r in satirlar.values() if r[0] == "GENEL TOPLAM")

    hepsi, c, e = await hucreler(atamasiz), await hucreler(civil), await hucreler(elek)
    assert poz(hepsi) == {"01.001", "02.001", "03.001"}
    assert poz(c) == {"01.001"} and poz(e) == {"02.001"}
    assert genel(c) == "1000.00" and genel(e) == "1000.00" and genel(hepsi) == "3200.00"


async def _tahsis(client: AsyncClient, kalem_id: uuid.UUID, baslik: dict[str, str]):
    return await client.get(f"/boq/items/{kalem_id}/allocations", headers=baslik)


async def test_tahsis_yabanci_kalem_404_ve_govde_olmayanla_ayni(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    yok = await _tahsis(client, dunya.yabanci_kalem_kimligi, civil)
    assert yok.status_code == 404
    for baslik, yabanci in ((civil, "i2"), (elek, "i1"), (civil, "i3"), (elek, "i3")):
        yanit = await _tahsis(client, dunya.i[yabanci].id, baslik)
        assert yanit.status_code == 404, yabanci
        assert yanit.json() == yok.json()
        assert yanit.headers["content-type"] == yok.headers["content-type"]


async def test_tahsis_kendi_kalemi_ve_atamasiz_hepsini_gorur(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    assert (await _tahsis(client, dunya.i["i1"].id, civil)).status_code == 200
    assert (await _tahsis(client, dunya.i["i2"].id, elek)).status_code == 200
    for ad in ("i1", "i2", "i3"):
        assert (await _tahsis(client, dunya.i[ad].id, atamasiz)).status_code == 200

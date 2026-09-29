"""DSC-B1 — EV butce uclari (budget · diff · schedule · suggestions) iki aktorlu gorunurluk.

Dunya (bkz. `tests/_disiplin_dunyasi.py`): AKTIF donmus baseline G1→KAB (I1), G2→DUV (I2), G3
eslemesiz (I3); TASLAK'ta G1→DUV yeniden eslendi ve I2·S1 orani 0.6. civil=KAB, elek=DUV.

K5 KURALI (beklentiler ondan turetilir):
* DONMUS (aktif) agac yalniz `d:` kokunden budanir (civil I1, elek I2); `d:none` hep budanir.
* TASLAK agac `d:` koku ∩ R(site) (R = aktif ?? taslak → I1=KAB, I2=DUV, I3=NULL):
  - civil: taslakta `d:KAB` yok (G1 `d:DUV`a tasindi) → `d:` kapsam disi → HICBIR SEY gormez.
  - elek: `d:DUV` kapsamda AMA I1'in R(site) disiplini KAB ≠ DUV → G1/I1 budanir; yalniz G2/I2.
  - Yani I1 taslakta KIMSEYE gorunmez; iki kumenin birlesimi atamasizin (d:none'suz) hali DEGIL.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

from httpx import AsyncClient

from app.core.discipline_scope import DisciplineScope
from app.modules.earned_value.budget_scope import prune_tree
from app.modules.earned_value.budget_tree import (
    BudgetTree,
    DisciplineNode,
    GroupNode,
    ItemNode,
    LeafNode,
    compute_findings,
)
from app.modules.earned_value.engine import ContractorType
from tests._disiplin_dunyasi import Dunya

D = Decimal
BUTCE = "/sites/{}/earned-value/budget"


async def _get(client: AsyncClient, d: Dunya, baslik, kuyruk: str = "") -> dict:
    resp = await client.get(BUTCE.format(d.santiye.id) + kuyruk, headers=baslik)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _aktif_id(client: AsyncClient, d: Dunya, baslik) -> str:
    revizyonlar = await _get(client, d, baslik, "/revisions")
    return next(r["id"] for r in revizyonlar if r["status"] == "active")


async def _taslak_id(client: AsyncClient, d: Dunya, baslik) -> str:
    revizyonlar = await _get(client, d, baslik, "/revisions")
    return next(r["id"] for r in revizyonlar if r["status"] == "draft")


def _kalemler(govde: dict) -> set[str]:
    return {i["item_id"] for x in govde["disciplines"] for g in x["groups"] for i in g["items"]}


def _yapraklar(govde: dict) -> list[dict]:
    return [
        lf
        for x in govde["disciplines"]
        for g in x["groups"]
        for i in g["items"]
        for lf in i["leaves"]
    ]


def _dugum_kimlikleri(govde: dict) -> set[str]:
    out: set[str] = set()
    for x in govde["disciplines"]:
        out.add(x["id"])
        for g in x["groups"]:
            out.add(g["id"])
            for i in g["items"]:
                out.add(i["id"])
                out.update(lf["id"] for lf in i["leaves"])
    return out


def _dogrudan(govde: dict) -> Decimal:
    return sum((D(lf["budget_mhr"]) for lf in _yapraklar(govde) if lf["is_direct"]), D(0))


# ------------------------------------------------------------------ AKTIF (donmus) agac


async def test_aktif_agac_d_kokunden_budanir_ayrik_kumeler_ve_d_none_hep_budanir(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    aktif = await _aktif_id(client, dunya, atamasiz)
    q = f"?revision_id={aktif}"
    i = {ad: str(k.id) for ad, k in dunya.i.items()}
    hepsi = await _get(client, dunya, atamasiz, q)
    c = await _get(client, dunya, civil, q)
    e = await _get(client, dunya, elek, q)
    assert _kalemler(hepsi) == {i["i1"], i["i2"], i["i3"]}
    assert _kalemler(c) == {i["i1"]}
    assert _kalemler(e) == {i["i2"]}
    assert _kalemler(c) | _kalemler(e) == _kalemler(hepsi) - {i["i3"]}
    # d:none (Ü1) hicbir kisitliya gorunmez
    assert all(x["discipline_id"] is not None for x in c["disciplines"] + e["disciplines"])
    # donmus agacta civil G1'i gorur (taslakta gormedigi G1'i): R degil `d:` koku belirler
    assert [g["group_id"] for x in c["disciplines"] for g in x["groups"]] == [str(dunya.g["g1"].id)]


async def test_aktif_agac_toplamlari_ve_paylar_kendi_yapraklarinin_toplami(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    aktif = await _aktif_id(client, dunya, atamasiz)
    q = f"?revision_id={aktif}"
    hepsi = await _get(client, dunya, atamasiz, q)
    c = await _get(client, dunya, civil, q)
    e = await _get(client, dunya, elek, q)
    assert D(hepsi["totals"]["direct_budget_mhr"]) == D(225)  # I1 200 + I2 25 + I3 0 (oransiz)
    assert D(c["totals"]["direct_budget_mhr"]) == _dogrudan(c) == D(200)
    assert D(e["totals"]["direct_budget_mhr"]) == _dogrudan(e) == D(25)
    for govde, kalem_sayisi, yaprak_sayisi in ((c, 1, 3), (e, 1, 1)):
        assert govde["totals"]["item_count"] == kalem_sayisi
        assert govde["totals"]["leaf_count"] == len(_yapraklar(govde)) == yaprak_sayisi
        # pay paydasi budanmis agacin dogrudan toplami: tek disiplin → %100
        assert [D(x["share"]) for x in govde["disciplines"]] == [D(1)]
        assert sum((D(x["budget_mhr"]) for x in govde["disciplines"]), D(0)) == _dogrudan(govde)
    # yarim suzme olsaydi (sirket geneli payda) civil payi 200/245 olurdu
    assert D(hepsi["disciplines"][0]["share"]) < D(1)


async def test_yabanci_revizyon_kimligi_404_kalir(client: AsyncClient, dunya: Dunya, civil) -> None:
    resp = await client.get(
        BUTCE.format(dunya.santiye.id) + f"?revision_id={dunya.yabanci_kalem_kimligi}",
        headers=civil,
    )
    assert resp.status_code == 404, resp.text


# ------------------------------------------------------------------ TASLAK: d: ∩ R(site)


async def test_taslak_d_kesisim_R_kurali_civil_hicbir_sey_elek_yalniz_i2(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    i = {ad: str(k.id) for ad, k in dunya.i.items()}
    hepsi = await _get(client, dunya, atamasiz)
    c = await _get(client, dunya, civil)
    e = await _get(client, dunya, elek)
    # atamasiz taslakta G1'i DUV altinda, G3'u d:none altinda gorur
    assert {x["discipline_id"] for x in hepsi["disciplines"]} == {str(dunya.duv.id), None}
    assert _kalemler(hepsi) == {i["i1"], i["i2"], i["i3"]}
    # civil: taslakta `d:KAB` yok (d: kesimi bos) → I1'in R disiplini KAB olsa da gorunmez
    assert c["disciplines"] == []
    # elek: d:DUV kapsamda ama I1'in R(site) disiplini KAB ≠ DUV → G1/I1 budanir
    assert _kalemler(e) == {i["i2"]}
    assert [g["group_id"] for x in e["disciplines"] for g in x["groups"]] == [str(dunya.g["g2"].id)]
    # I1 taslakta KIMSEYE gorunmez → birlesim atamasizin d:none'suz halinden KUCUK
    assert _kalemler(c) | _kalemler(e) == {i["i2"]}
    assert _kalemler(c) | _kalemler(e) < _kalemler(hepsi) - {i["i3"]}


async def test_taslak_toplam_pay_ve_bulgular_budanmis_agactan_yeniden_hesaplanir(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    hepsi = await _get(client, dunya, atamasiz)
    c = await _get(client, dunya, civil)
    e = await _get(client, dunya, elek)
    assert D(hepsi["totals"]["direct_budget_mhr"]) == D(230)  # I1 200 + I2 30
    assert D(e["totals"]["direct_budget_mhr"]) == _dogrudan(e) == D(30)
    assert e["totals"]["item_count"] == 1 and e["totals"]["leaf_count"] == 1
    assert [D(x["share"]) for x in e["disciplines"]] == [D(1)]
    assert D(c["totals"]["direct_budget_mhr"]) == 0 and c["totals"]["leaf_count"] == 0
    # bulgular: civil'in bos agacinda HIC bulgu yok (sirket geneli agacin bulgulari degil);
    assert c["freeze_blockers"] == [] and c["freeze_warnings"] == []
    assert hepsi["freeze_warnings"] != []  # sirket geneli agacin uyarisi var (kontrol)
    # elek'in bulgulari yalniz KENDI dugumlerine isaret eder
    gorunen = _dugum_kimlikleri(e)
    for bulgu in e["freeze_blockers"] + e["freeze_warnings"]:
        assert set(bulgu["node_ids"]) <= gorunen
        assert bulgu["count"] == len(bulgu["node_ids"])
    metin = json.dumps(e)
    assert str(dunya.i["i1"].id) not in metin and str(dunya.i["i3"].id) not in metin


# ------------------------------------------------------------------ diff


async def test_diff_iki_agac_da_budanir_ve_direkt_toplamlar_budanmis_agactan(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    taslak = await _taslak_id(client, dunya, atamasiz)
    yol = f"{BUTCE.format(dunya.santiye.id)}/revisions/{taslak}/diff"
    hepsi = (await client.get(yol, headers=atamasiz)).json()
    c = (await client.get(yol, headers=civil)).json()
    e = (await client.get(yol, headers=elek)).json()
    assert D(hepsi["direct_before_mhr"]) == D(225) and D(hepsi["direct_after_mhr"]) == D(230)
    # elek: once (donmus, d:DUV) yalniz I2 = 25 · sonra (taslak d:∩R) yalniz I2 = 30
    assert (D(e["direct_before_mhr"]), D(e["direct_after_mhr"])) == (D(25), D(30))
    assert [x["item_code"] for x in e["leaves"]] == ["02.001"]
    assert e["leaves"][0]["reason"] == "rate_changed"
    # civil: once (donmus, d:KAB) I1 = 200 · sonra (taslakta hicbir sey gorunmez) = 0
    assert (D(c["direct_before_mhr"]), D(c["direct_after_mhr"])) == (D(200), D(0))
    assert {x["item_code"] for x in c["leaves"]} == {"01.001"}
    assert {x["reason"] for x in c["leaves"]} == {"removed"}
    assert "02.001" not in json.dumps(c) and "01.001" not in json.dumps(e)


# ------------------------------------------------------------------ schedule


async def test_takvim_cubuklari_budanmis_yapraklardan(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    aktif = await _aktif_id(client, dunya, atamasiz)
    q = f"/schedule?revision_id={aktif}"
    c = await _get(client, dunya, civil, q)
    e = await _get(client, dunya, elek, q)
    assert {b["discipline_id"] for b in c["bars"]} == {str(dunya.kab.id)}
    assert {b["discipline_id"] for b in e["bars"]} == {str(dunya.duv.id)}
    # civil cubuklari: S1 120 · S2 60 · bolumsuz 20 (I1); elek: S1 25 (I2)
    assert sorted(D(b["budget_mhr"]) for b in c["bars"]) == [D(20), D(60), D(120)]
    assert [D(b["budget_mhr"]) for b in e["bars"]] == [D(25)]
    # taslak (varsayilan): civil'in cubugu yok; elek yalniz I2·S1 = 30
    assert (await _get(client, dunya, civil, "/schedule"))["bars"] == []
    taslak_e = await _get(client, dunya, elek, "/schedule")
    assert [D(b["budget_mhr"]) for b in taslak_e["bars"]] == [D(30)]


# ------------------------------------------------------------------ suggestions


async def _oneri(client: AsyncClient, d: Dunya, baslik, kalem) -> tuple[int, dict]:
    resp = await client.get(
        f"{BUTCE.format(d.santiye.id)}/items/{kalem}/suggestions", headers=baslik
    )
    return resp.status_code, resp.json()


async def test_yabanci_kalem_onerisi_olmayan_kalemle_birebir_ayni_govde(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    yok = await _oneri(client, dunya, civil, dunya.yabanci_kalem_kimligi)
    assert yok[0] == 422 and "BOQ kalemi bu şantiyeye ait değil" in json.dumps(
        yok[1], ensure_ascii=False
    )
    # civil ← I2 (elek'in), elek ← I1 (taslakta R ile budanan), civil ← I3 (disiplinsiz)
    assert await _oneri(client, dunya, civil, dunya.i["i2"].id) == yok
    assert await _oneri(client, dunya, elek, dunya.i["i1"].id) == yok
    assert await _oneri(client, dunya, civil, dunya.i["i3"].id) == yok
    # kendi kalemi normal: elek I2 (taslakta gorunur) → 200 + kendi disiplininin katalogu
    kod, govde = await _oneri(client, dunya, elek, dunya.i["i2"].id)
    assert kod == 200
    assert [x["name"] for x in govde["catalog"]] == ["Tuğla"]
    # atamasiz her kalemi gorur
    assert (await _oneri(client, dunya, atamasiz, dunya.i["i1"].id))[0] == 200


# ------------------------------------------------------------------ saf birim: prune_tree


def _yaprak(item_id, ad: str, mhr: str, *, direct: bool = True) -> LeafNode:
    return LeafNode(
        id=f"l:{item_id}:none",
        item_id=item_id,
        section_id=None,
        section_name=None,
        planned_qty=D(1),
        unit_mhr=D(mhr),
        rate_source=None,
        contractor_type=ContractorType.OWN,
        contractor_overridden=False,
        is_direct=direct,
        is_direct_overridden=False,
        budget_mhr=D(mhr),
        window_start=None,
        window_end=None,
        window_source=None,
    )


def _agac() -> tuple[BudgetTree, dict]:
    k = {ad: uuid.uuid4() for ad in ("a", "b", "n", "ia", "ib", "in")}

    def d(ad, disc, grup):
        return DisciplineNode(
            f"d:{disc or 'none'}", disc, None, None, None, ContractorType.OWN, "linear", (grup,)
        )

    def g(gid, item_id):
        item = ItemNode(
            f"i:{item_id}",
            item_id,
            "c",
            "d",
            "m2",
            ContractorType.OWN,
            True,
            True,
            None,
            (_yaprak(item_id, "x", "10"),),
        )
        return GroupNode(f"g:{gid}", gid, "G", None, (item,))

    agac = BudgetTree(
        (
            d("a", k["a"], g(uuid.uuid4(), k["ia"])),
            d("b", k["b"], g(uuid.uuid4(), k["ib"])),
            d(None, None, g(uuid.uuid4(), k["in"])),
        ),
        (),
        (),
    )
    return agac, k


def test_prune_tree_kisitsizda_ayni_nesne_donmus_d_none_hep_budanir() -> None:
    agac, k = _agac()
    assert prune_tree(agac, DisciplineScope(None), None, True) is agac
    kapsam = DisciplineScope(frozenset({k["a"]}))
    donmus = prune_tree(agac, kapsam, None, True)
    assert [x.discipline_id for x in donmus.disciplines] == [k["a"]]
    # taslakta gorunur kalem kumesi disiplin kokunden GENIS olsa da d:none budanir
    taslak = prune_tree(agac, kapsam, {k["ia"], k["ib"], k["in"]}, False, lambda _d: True)
    assert [x.discipline_id for x in taslak.disciplines] == [k["a"]]
    # d: ∩ R: kok kapsamda ama kalem R kumesinde degil → budanir; bos disiplin kalmaz
    assert prune_tree(agac, kapsam, {k["ib"]}, False, lambda _d: True).disciplines == ()
    # kisitlida gorunur kalem kumesi None → fail-closed
    assert prune_tree(agac, kapsam, None, False, lambda _d: True).disciplines == ()


def test_prune_tree_bulgulari_budanmis_agactan_yeniden_hesaplar() -> None:
    agac, k = _agac()
    kapsam = DisciplineScope(frozenset({k["a"]}))
    b, w = compute_findings(agac, lambda _d: True)
    assert any(f.count for f in (*b, *w))  # tam agacta bulgu var (d:none disiplinsiz grup)
    budanmis = prune_tree(agac, kapsam, {k["ia"]}, False, lambda _d: True)
    assert (budanmis.blockers, budanmis.warnings) == compute_findings(budanmis, lambda _d: True)
    assert not any(
        str(k["in"]) in nid for f in (*budanmis.blockers, *budanmis.warnings) for nid in f.node_ids
    )


# ------------------------------------------------------------------ capraz katalog bagi (Ü8)


async def _capraz_bagla(client: AsyncClient, d: Dunya, admin) -> str:
    """Admin I2'yi (DUV) KAB katalog kalemi "Beton"a baglar (`patch_item` disiplin denetlemez)."""
    beton = str(_kimlik_beton())
    resp = await client.patch(
        f"{BUTCE.format(d.santiye.id)}/items/{d.i['i2'].id}",
        headers=admin,
        json={"catalog_item_id": beton},
    )
    assert resp.status_code == 200, resp.text
    return beton


def _kimlik_beton() -> uuid.UUID:
    from tests._disiplin_dunyasi import _kimlik

    return _kimlik(21, 1)


async def test_capraz_bagli_katalog_onerisi_kisitliya_sizmaz_atamasizda_degismez(
    client: AsyncClient, dunya: Dunya, atamasiz, elek
) -> None:
    beton = await _capraz_bagla(client, dunya, atamasiz)
    kod, e = await _oneri(client, dunya, elek, dunya.i["i2"].id)
    assert kod == 200
    assert beton not in json.dumps(e) and str(dunya.kab.id) not in json.dumps(e)
    assert [x["name"] for x in e["catalog"]] == ["Tuğla"]
    _, hepsi = await _oneri(client, dunya, atamasiz, dunya.i["i2"].id)
    linked = [x for x in hepsi["catalog"] if x["match"] == "linked"]
    assert [(x["catalog_item_id"], x["discipline_id"]) for x in linked] == [
        (beton, str(dunya.kab.id))
    ]


async def test_capraz_bagli_kalemin_catalog_item_id_kisitliya_none_atamasizda_dolu(
    client: AsyncClient, dunya: Dunya, atamasiz, elek
) -> None:
    beton = await _capraz_bagla(client, dunya, atamasiz)

    def i2(govde: dict) -> dict:
        return next(
            i
            for x in govde["disciplines"]
            for g in x["groups"]
            for i in g["items"]
            if i["item_id"] == str(dunya.i["i2"].id)
        )

    assert i2(await _get(client, dunya, elek))["catalog_item_id"] is None
    assert i2(await _get(client, dunya, atamasiz))["catalog_item_id"] == beton
    assert beton not in json.dumps(await _get(client, dunya, elek))

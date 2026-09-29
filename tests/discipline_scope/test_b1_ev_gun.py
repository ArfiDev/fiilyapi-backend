"""DSC-B1 — EV gun uclari (code-tree · days/{day} · previous-allocation) iki aktorlu gorunurluk.

Aktif (DONMUS) agac yalniz `d:` kokunden budanir (K5): civil {d:KAB · G1 · I1 · 3 yaprak},
elek {d:DUV · G2 · I2 · I2·S1}; `d:none` (G3/I3) hep budanir. 05.05 dagilimi: Ali → I1·S1 5 sa
(civil), Veli → I2·S1 4 sa (elek). K4: gun toplamlari (kaynak/dagitilan/dagitilmamis) KAYNAK
SAATIdir, suzulmez; baska disiplin payi ortuktur, acik alan yoktur.
"""

from __future__ import annotations

import json
from decimal import Decimal

from httpx import AsyncClient

from app.modules.earned_value.models import EvDayCode
from tests._disiplin_dunyasi import GUN1, GUN2, Dunya

D = Decimal


def _ev(d: Dunya, kuyruk: str) -> str:
    return f"/sites/{d.santiye.id}/earned-value{kuyruk}"


async def _get(client: AsyncClient, url: str, baslik) -> dict | list:
    resp = await client.get(url, headers=baslik)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------ code-tree


async def test_kod_agaci_ayrik_kumeler_birlesim_atamasizin_d_none_suz_hali(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    url = _ev(dunya, "/code-tree")
    hepsi = {n["id"] for n in await _get(client, url, atamasiz)}
    c = {n["id"] for n in await _get(client, url, civil)}
    e = {n["id"] for n in await _get(client, url, elek)}
    i = {ad: k.id for ad, k in dunya.i.items()}
    assert f"i:{i['i1']}" in c and f"i:{i['i2']}" in e
    assert c & e == set()
    assert f"d:{dunya.kab.id}" in c and f"d:{dunya.duv.id}" in e
    d_none = {x for x in hepsi if x == "d:none" or str(i["i3"]) in x or str(dunya.g["g3"].id) in x}
    assert len(d_none) == 5  # d:none · g3 · i3 · 2 yaprak (S2 + bolumsuz)
    assert c | e == hepsi - d_none
    assert "d:none" not in c | e


# ------------------------------------------------------------------ days/{day}


async def test_gun_kodlar_hucreler_ilerleme_budanmis_toplamlar_suzulmez(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    url = _ev(dunya, f"/days/{GUN1.isoformat()}")
    hepsi = await _get(client, url, atamasiz)
    c = await _get(client, url, civil)
    e = await _get(client, url, elek)
    k1 = f"l:{dunya.i['i1'].id}:{dunya.s1.id}"
    k2 = f"l:{dunya.i['i2'].id}:{dunya.s1.id}"
    assert [x["node_id"] for x in hepsi["codes"]] == [k1, k2]
    assert [x["node_id"] for x in c["codes"]] == [k1]
    assert [x["node_id"] for x in e["codes"]] == [k2]
    assert [x["node_id"] for x in c["cells"]] == [k1]
    assert [x["node_id"] for x in e["cells"]] == [k2]
    # K4: totals KAYNAK saatidir, suzulmez (baska disiplin payi ortuk) — atamasizla birebir ayni
    assert c["totals"] == e["totals"] == hepsi["totals"]
    assert D(c["totals"]["source_hours"]) == 9 and D(c["totals"]["unallocated_hours"]) == 0
    # ilerleme budanmis agacta motorla yeniden hesaplanir: civil harcanan 5 (Ali), kazanilan 16
    assert (D(c["progress"]["spent_day"]), D(c["progress"]["earned_day"])) == (D(5), D(16))
    assert (D(e["progress"]["spent_day"]), D(e["progress"]["earned_day"])) == (D(4), D(2))
    assert D(hepsi["progress"]["spent_day"]) == 9 and D(hepsi["progress"]["earned_day"]) == 18
    assert {x["node_id"] for x in c["progress"]["leaves"]} == {
        f"l:{dunya.i['i1'].id}:{dunya.s1.id}",
        f"l:{dunya.i['i1'].id}:{dunya.s2.id}",
        f"l:{dunya.i['i1'].id}:none",
    }
    assert {x["node_id"] for x in c["progress"]["items"]} == {f"i:{dunya.i['i1'].id}"}
    # takvim numaralari kapsamdan bagimsiz (tam agactan)
    assert (c["day_no"], c["week_no"], c["has_baseline"]) == (
        hepsi["day_no"],
        hepsi["week_no"],
        True,
    )


async def test_gun_uyarilari_gorunmeyen_kodu_sizdirmaz(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek, seeded_db
) -> None:
    """🔴 `warnings` "Is kodu aktif baseline'da yok: <node>" gorunmeyen (ama baseline'da VAR olan)
    kodu sizdirabilirdi: uyari TAM etiket kumesiyle hesaplanir, gorunmeyen kod uyari uretmez."""
    url = _ev(dunya, f"/days/{GUN1.isoformat()}")
    i1, i2 = str(dunya.i["i1"].id), str(dunya.i["i2"].id)
    hepsi = await _get(client, url, atamasiz)
    c = await _get(client, url, civil)
    e = await _get(client, url, elek)
    assert hepsi["warnings"] == [] and c["warnings"] == [] and e["warnings"] == []
    assert i2 not in json.dumps(c) and i1 not in json.dumps(e)
    assert str(dunya.g["g2"].id) not in json.dumps(c)
    # baseline'da HIC olmayan (asili) kod: atamasiz uyari gorur, kisitli gormez (fail-closed)
    asili = f"l:{dunya.yabanci_kalem_kimligi}:none"
    seeded_db.add(EvDayCode(site_id=dunya.santiye.id, day=GUN1, node_id=asili, rule="direct"))
    await seeded_db.flush()
    assert any(asili in w for w in (await _get(client, url, atamasiz))["warnings"])
    for baslik in (civil, elek):
        govde = await _get(client, url, baslik)
        assert govde["warnings"] == [] and asili not in json.dumps(govde)


# ------------------------------------------------------------------ previous-allocation


async def test_onceki_dagilim_gorunur_dugumler_veli_satiri_civilde_duser(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    url = _ev(dunya, f"/days/{GUN2.isoformat()}/previous-allocation")
    hepsi = await _get(client, url, atamasiz)
    c = await _get(client, url, civil)
    e = await _get(client, url, elek)
    assert hepsi["day"] == c["day"] == e["day"] == GUN1.isoformat()
    assert len(hepsi["rows"]) == 2
    k1 = f"l:{dunya.i['i1'].id}:{dunya.s1.id}"
    k2 = f"l:{dunya.i['i2'].id}:{dunya.s1.id}"
    assert [x["node_id"] for x in c["codes"]] == [k1]
    assert [x["node_id"] for x in e["codes"]] == [k2]
    # gorunur hucresi olmayan satir duser: civil yalniz Ali, elek yalniz Veli
    assert [[s["node_id"] for s in r["shares"]] for r in c["rows"]] == [[k1]]
    assert [[s["node_id"] for s in r["shares"]] for r in e["rows"]] == [[k2]]
    assert {r["ref_id"] for r in c["rows"]}.isdisjoint({r["ref_id"] for r in e["rows"]})


async def test_onceki_dagilim_pay_paydasi_tam_satir_toplami_yeniden_normallesmez(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek, seeded_db
) -> None:
    """Ali 5 sa'i IKI disipline boldu: I1·S1 3 (civil) + I2·S1 2 (elek); Veli 4 sa I2·S1.
    K4: pay = hucre / TAM satir toplami → civil Ali'de 3/5 = 0.6 gorur (gorunur toplama
    bolunse 1.0 olurdu); elek Ali'de 0.4, Veli'de 1."""
    from sqlalchemy import select

    from app.modules.personnel.models import Personnel

    ali = (
        await seeded_db.execute(select(Personnel.id).where(Personnel.full_name == "Ali Usta"))
    ).scalar_one()
    veli = (
        await seeded_db.execute(select(Personnel.id).where(Personnel.full_name == "Veli Usta"))
    ).scalar_one()
    k1 = f"l:{dunya.i['i1'].id}:{dunya.s1.id}"
    k2 = f"l:{dunya.i['i2'].id}:{dunya.s1.id}"

    def hucre(kisi, node, saat):
        return {"row": {"kind": "personnel", "ref_id": str(kisi)}, "node_id": node, "hours": saat}

    resp = await client.put(
        _ev(dunya, f"/days/{GUN1.isoformat()}/allocation"),
        headers=atamasiz,
        json={
            "codes": [{"node_id": k1, "rule": "direct"}, {"node_id": k2, "rule": "direct"}],
            "cells": [hucre(ali, k1, "3"), hucre(ali, k2, "2"), hucre(veli, k2, "4")],
        },
    )
    assert resp.status_code == 200, resp.text
    url = _ev(dunya, f"/days/{GUN2.isoformat()}/previous-allocation")
    c = await _get(client, url, civil)
    e = await _get(client, url, elek)
    assert [(r["ref_id"], [D(s["share"]) for s in r["shares"]]) for r in c["rows"]] == [
        (str(ali), [D("0.6")])
    ]
    assert {r["ref_id"]: [D(s["share"]) for s in r["shares"]] for r in e["rows"]} == {
        str(ali): [D("0.4")],
        str(veli): [D(1)],
    }
    tam = await _get(client, url, atamasiz)
    ali_tam = next(r for r in tam["rows"] if r["ref_id"] == str(ali))
    assert sum(D(s["share"]) for s in ali_tam["shares"]) == 1

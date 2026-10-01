"""BDG-B1 — bolum dagilim matrisi, DISIPLIN KAPSAMI (iki aktorlu).

Dunya (`tests/_disiplin_dunyasi.py`): I1 01.001 (KAB, 100, 10 TL) S1 60 + S2 30 ·
I2 02.001 (DUV, 50, 20 TL) S1 50 · I3 03.001 (eslemesiz, 40, 30 TL) S2 20.
`civil_yazar` yalniz KAB'i gorur; `yazar_atamasiz` ayni rolde atamasiz esidir
(POZITIF KONTROL: 422/eksik sayac kapidan degil disiplinden gelir).
"""

from __future__ import annotations

from decimal import Decimal

from httpx import AsyncClient

from tests._disiplin_dunyasi import Dunya
from tests.discipline_scope._b2_yardim import YOK_KIMLIK, ham_satirlar, ozet

YAZANLAR = True
D = Decimal


def _url(d: Dunya) -> str:
    return f"/sites/{d.santiye.id}/boq/section-distribution"


def _hucre(item, section, quantity) -> dict:
    return {"boq_item_id": str(item.id), "section_id": str(section.id), "quantity": quantity}


async def _tahsisler(session, d: Dunya) -> list:
    ids = ",".join(f"'{d.i[k].id}'" for k in ("i1", "i2", "i3"))
    return await ham_satirlar(session, "boq_item_section_allocations", f"boq_item_id IN ({ids})")


async def test_kisitli_GET_yalniz_kendi_kalemleri_ve_dort_alan_baska_disiplini_saymaz(
    client: AsyncClient, dunya: Dunya, civil_yazar, yazar_atamasiz
) -> None:
    kisitli = (await client.get(_url(dunya), headers=civil_yazar)).json()
    atamasiz = (await client.get(_url(dunya), headers=yazar_atamasiz)).json()

    # --- POZITIF KONTROL: atamasiz esi hepsini gorur ---
    assert [i["code"] for g in atamasiz["groups"] for i in g["items"]] == [
        "01.001",
        "02.001",
        "03.001",
    ]
    assert atamasiz["total_item_count"] == 3
    assert atamasiz["unallocated_item_count"] == 2  # I1 (10 kaldi) ve I3 (20 kaldi)
    assert atamasiz["distributed_item_count"] == 1  # I2 tam
    assert atamasiz["unallocated_item_codes"] == ["01.001", "03.001"]

    # --- kisitli: dort alan AYRI AYRI ---
    assert [i["code"] for g in kisitli["groups"] for i in g["items"]] == ["01.001"]
    assert kisitli["total_item_count"] == 1
    assert kisitli["distributed_item_count"] == 0  # I2'nin tam dagitimi SAYILMAZ
    assert kisitli["unallocated_item_count"] == 1  # I3'un atanmamisi SAYILMAZ
    assert kisitli["unallocated_item_codes"] == ["01.001"]
    ozetler = {s["section_name"]: s for s in kisitli["section_summaries"]}
    s1 = ozetler[dunya.s1.name]
    s2 = ozetler[dunya.s2.name]
    assert [i["code"] for i in s1["items"]] == ["01.001"]  # I2'nin S1 payi YOK
    assert D(s1["total_amount"]) == D("600.00")  # 60 x 10 (I2'nin 1000 TL'si katilmaz)
    assert [i["code"] for i in s2["items"]] == ["01.001"]  # I3'un S2 payi YOK
    assert D(s2["total_amount"]) == D("300.00")
    # pozitif kontrolun aynasi: atamasizda ayni bolum ozetleri daha buyuk
    a_s1 = {s["section_name"]: s for s in atamasiz["section_summaries"]}[dunya.s1.name]
    assert D(a_s1["total_amount"]) == D("1600.00")
    # bolum kolonlari disiplinle SUZULMEZ
    assert len(kisitli["sections"]) == len(atamasiz["sections"]) == 2
    for govde in (kisitli, atamasiz):
        assert (
            govde["distributed_item_count"] + govde["unallocated_item_count"]
            == govde["total_item_count"]
        )


async def test_kisitli_PUT_baska_disiplin_kalemi_olmayanla_ayni_422_hicbir_sey_yazilmaz(
    client: AsyncClient, dunya: Dunya, civil_yazar, yazar_atamasiz, db_session
) -> None:
    i1, i2, i3 = dunya.i["i1"], dunya.i["i2"], dunya.i["i3"]
    oncesi = await _tahsisler(db_session, dunya)

    olmayan = ozet(
        await client.put(
            _url(dunya),
            headers=civil_yazar,
            json={
                "allocations": [
                    {
                        "boq_item_id": str(YOK_KIMLIK),
                        "section_id": str(dunya.s1.id),
                        "quantity": "1",
                    }
                ]
            },
        )
    )
    assert olmayan[0] == 422
    for yabanci in (i2, i3):  # baska disiplin · eslemesiz (Ü1)
        # AYNI govdede kendi kalemine ait GECERLI hucre de var: atomiklik.
        resp = await client.put(
            _url(dunya),
            headers=civil_yazar,
            json={"allocations": [_hucre(i1, dunya.s1, "70"), _hucre(yabanci, dunya.s1, "10")]},
        )
        assert resp.status_code == 422
        assert resp.json() == olmayan[2]
        assert await _tahsisler(db_session, dunya) == oncesi  # birebir ayni satirlar

    # --- POZITIF KONTROL: atamasiz esi ayni govdeyle yazabilir (I2: 50 + 0 <= 50) ---
    pozitif = await client.put(
        _url(dunya),
        headers=yazar_atamasiz,
        json={"allocations": [_hucre(i2, dunya.s2, "0"), _hucre(i2, dunya.s1, "40")]},
    )
    assert pozitif.status_code == 200, pozitif.text


async def test_kisitli_kendi_kalemine_yazabilir(
    client: AsyncClient, dunya: Dunya, civil_yazar
) -> None:
    i1 = dunya.i["i1"]

    resp = await client.put(
        _url(dunya), headers=civil_yazar, json={"allocations": [_hucre(i1, dunya.s1, "70")]}
    )

    assert resp.status_code == 200, resp.text
    govde = resp.json()
    assert govde["total_item_count"] == 1
    assert D(govde["groups"][0]["items"][0]["allocated_quantity"]) == D("100")  # 70 + 30
    assert govde["distributed_item_count"] == 1  # artik tam
    assert govde["unallocated_item_count"] == 0
    assert govde["unallocated_item_codes"] == []
    # asim kisitlida da calisir: 70 -> 71 (+30) = 101 > 100
    asim = await client.put(
        _url(dunya), headers=civil_yazar, json={"allocations": [_hucre(i1, dunya.s1, "71")]}
    )
    assert asim.status_code == 422
    assert "01.001" in asim.json()["detail"]

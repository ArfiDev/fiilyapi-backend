"""DSC-B3: POST budget/preview kapsamla budanır (Ç2, Ü7, S7) + diff `moved_out` (S8).

Dünya B1 dünyası: AKTİF donmuş G1→KAB(I1), G2→DUV(I2), G3 eşlemesiz(I3); TASLAK'ta G1→DUV.
"""

from __future__ import annotations

import json
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqItemSectionAllocation
from tests._disiplin_dunyasi import Dunya, _kimlik

D = Decimal
BUTCE = "/sites/{}/earned-value/budget"


async def _revizyon(client: AsyncClient, d: Dunya, baslik, durum: str) -> str:
    yol = BUTCE.format(d.santiye.id) + "/revisions"
    return next(
        r["id"] for r in (await client.get(yol, headers=baslik)).json() if r["status"] == durum
    )


async def _onizle(client: AsyncClient, d: Dunya, baslik, govde: dict) -> dict:
    resp = await client.post(BUTCE.format(d.santiye.id) + "/preview", headers=baslik, json=govde)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _gunler(seri: dict) -> dict[str, Decimal]:
    return {x["day"]: D(x["mhr"]) for x in seri["days"]}


def _disiplin(govde: dict, kod: str) -> dict | None:
    return next((x for x in govde["disciplines"] if x["code"] == kod), None)


# ------------------------------------------------------------------ preview · donmuş (Ç2)


async def test_donmus_kisitli_onizleme_200_ve_disiplin_serisi_atamasizla_ayni(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    aktif = {"revision_id": await _revizyon(client, dunya, atamasiz, "active")}
    tam = await _onizle(client, dunya, atamasiz, aktif)
    for baslik, kod in ((civil, "KAB"), (elek, "DUV")):
        kisitli = await _onizle(client, dunya, baslik, aktif)  # yabancı eğri → 500 olmamalı (Ç2)
        assert [x["code"] for x in kisitli["disciplines"]] == [kod]
        seri, tam_seri = kisitli["disciplines"][0]["series"], _disiplin(tam, kod)["series"]
        # S7: aralık KENDİ pencerelerinden (atamasızın sıfır kuyruğu yok); paylar AYNI
        n = len(seri["days"])
        assert seri["days"] == tam_seri["days"][:n] and seri["budget_mhr"] == tam_seri["budget_mhr"]
        assert {x["mhr"] for x in tam_seri["days"][n:]} <= {"0"}
        assert [w["mhr"] for w in seri["weeks"]] == [w["mhr"] for w in tam_seri["weeks"]][
            : len(seri["weeks"])
        ]  # son hafta kendi penceresine kırpılır (iş günü/gerekli kişi değişir)


async def test_donmus_gun_toplamlari_civil_arti_elek_arti_disiplinsiz_atamasiz_eder(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    aktif = {"revision_id": await _revizyon(client, dunya, atamasiz, "active")}
    tam = await _onizle(client, dunya, atamasiz, aktif)
    c = await _onizle(client, dunya, civil, aktif)
    e = await _onizle(client, dunya, elek, aktif)
    tam_gun, c_gun, e_gun = _gunler(tam["total"]), _gunler(c["total"]), _gunler(e["total"])
    # d:none payı = atamasız toplam − (KAB + DUV disiplin serileri); kısıtlılarda YOKTUR
    kab, duv = (_gunler(_disiplin(tam, k)["series"]) for k in ("KAB", "DUV"))
    for gun, deger in tam_gun.items():
        none_pay = deger - kab.get(gun, D(0)) - duv.get(gun, D(0))
        assert c_gun.get(gun, D(0)) + e_gun.get(gun, D(0)) == deger - none_pay, gun
    assert D(c["indirect_budget_mhr"]) + D(e["indirect_budget_mhr"]) <= D(
        tam["indirect_budget_mhr"]
    )
    assert "Boya" not in json.dumps(c) and str(dunya.i["i3"].id) not in json.dumps(c)


# ------------------------------------------------------------------ preview · taslak (Ü7)


async def test_taslak_kisitli_onizleme_budanmis_agactan(
    client: AsyncClient, dunya: Dunya, civil, elek
) -> None:
    c = await _onizle(client, dunya, civil, {})
    assert c["disciplines"] == [] and D(c["indirect_budget_mhr"]) == 0
    e = await _onizle(client, dunya, elek, {})
    assert [x["code"] for x in e["disciplines"]] == ["DUV"]
    assert abs(sum(_gunler(e["total"]).values()) - D(30)) < D("0.01")  # yalnız I2 (I1 R(site)=KAB)


async def test_yabanci_disiplin_ezmesi_yok_sayilir_kendi_ezmesi_etkilidir(
    client: AsyncClient, dunya: Dunya, elek
) -> None:
    kab, duv = str(dunya.kab.id), str(dunya.duv.id)
    bos = await _onizle(client, dunya, elek, {})
    yabanci = {
        "distributions": [{"discipline_id": kab, "distribution": "bell"}],
        "windows": [
            {
                "discipline_id": kab,
                "section_id": str(dunya.s1.id),
                "start_date": "2026-05-05",
                "end_date": "2026-05-08",
            }
        ],
    }
    assert await _onizle(client, dunya, elek, yabanci) == bos
    kendi = {"distributions": [{"discipline_id": duv, "distribution": "front"}]}
    assert await _onizle(client, dunya, elek, kendi) != bos  # kanal canlı: ezme gerçekten işler


# ------------------------------------------------------------------ diff · moved_out (S8)


async def _fark(client: AsyncClient, d: Dunya, baslik, rev: str) -> list[dict]:
    yol = f"{BUTCE.format(d.santiye.id)}/revisions/{rev}/diff"
    resp = await client.get(yol, headers=baslik)
    assert resp.status_code == 200, resp.text
    return resp.json()["leaves"]


async def test_taslakta_baska_disipline_tasinan_yaprak_moved_out(
    client: AsyncClient, dunya: Dunya, atamasiz, civil
) -> None:
    taslak = await _revizyon(client, dunya, atamasiz, "draft")
    yapraklar = await _fark(client, dunya, civil, taslak)
    assert {x["item_code"] for x in yapraklar} == {"01.001"}
    assert {x["reason"] for x in yapraklar} == {"moved_out"}
    for x in yapraklar:  # hedef disiplin verilmez; sonraki değerler boş, bütçe 0
        assert x["qty"] is None and x["unit_mhr"] is None and D(x["budget_mhr"]) == 0
        assert D(x["delta_mhr"]) == -D(x["prev_budget_mhr"])
    assert "DUV" not in json.dumps(yapraklar) and "Elektrik" not in json.dumps(yapraklar)
    # atamasızda moved_out OLUŞAMAZ
    assert "moved_out" not in {x["reason"] for x in await _fark(client, dunya, atamasiz, taslak)}


async def test_gercekten_silinen_yaprak_kisitlida_da_removed(
    client: AsyncClient, dunya: Dunya, seeded_db: AsyncSession, atamasiz, elek
) -> None:
    """I2·S1 tahsisi taslak BOQ'dan kalkar → yaprak kısıtsız ağaçta da yok → `removed`."""
    await seeded_db.execute(
        delete(BoqItemSectionAllocation).where(BoqItemSectionAllocation.id == _kimlik(14, 3))
    )
    await seeded_db.flush()
    taslak = await _revizyon(client, dunya, atamasiz, "draft")
    nedenler = {x["reason"] for x in await _fark(client, dunya, elek, taslak)}
    assert "removed" in nedenler and "moved_out" not in nedenler

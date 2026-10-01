"""TKL-B5.1 — sablondan teklif (`POST /offers` + `template_id`).

Rev.0: sablonun gruplari + kalemleri; MIKTAR BOS (SO-21); maliyet = son fiyat → referans → bos
(T32/T38, TEK `last_price.latest` cagrisi); oranlar govde ?? sablon ?? ayar; `offers.template_id`
yazilir. Varsayilan sablon KENDILIGINDEN kullanilmaz.
"""

from __future__ import annotations

import uuid

import pytest

from app.modules.audit import messages
from app.modules.audit.models import AuditAction

from .._boq import _audit_details
from ._offers import (
    TPL,
    D,
    detay,
    gecis,
    kalem,
    rev_url,
    revizyon,
    sablon,
    sablon_icerik,
    tum_kalemler,
)

pytestmark = pytest.mark.usefixtures("son_fiyat")


async def _teklif_sablondan(client, admin, isveren, template_id, **over) -> dict:
    govde = {"employer_id": str(isveren.id), "title": "Şablondan iş", "template_id": template_id}
    resp = await client.post("/offers", json={**govde, **over}, headers=admin)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def s(client, admin, katalog) -> dict:
    """Sablon: Kaba [Beton, Kalip], Ince [Demir]; GG 9, kar 21."""
    sab = await sablon(client, admin, "Villa", overhead_pct="9", profit_pct="21")
    await sablon_icerik(
        client,
        admin,
        sab["id"],
        [("Kaba", [katalog[0].id, katalog[1].id]), ("İnce", [katalog[2].id])],
    )
    return sab


async def test_gruplar_kalemler_kopya_alanlar_miktar_BOS_template_id(
    client, admin, isveren, katalog, s
) -> None:
    o = await _teklif_sablondan(client, admin, isveren, s["id"])
    assert o["template_id"] == s["id"] and o["status"] == "draft" and o["latest_rev_no"] == 0
    rev = await revizyon(client, admin, o["id"])
    assert [g["name"] for g in rev["groups"]] == ["Kaba", "İnce"]
    kaba, ince = rev["groups"]
    assert [k["poz_no"] for k in kaba["items"]] == [katalog[0].poz_no, katalog[1].poz_no]
    assert [k["poz_no"] for k in ince["items"]] == [katalog[2].poz_no]
    beton = kaba["items"][0]
    assert (beton["description"], beton["unit"]) == ("Beton", "m3")
    assert D(beton["unit_mhr"]) == D("1.5")  # katalogdan
    assert [D(k["unit_mhr"]) for g in rev["groups"] for k in g["items"]] == [
        D("1.5"), D("2"), D("0.75")
    ]  # fmt: skip
    assert all(k["quantity"] is None for k in tum_kalemler(rev))  # SO-21: miktar BOS
    assert all(k["offer_unit_price"] is None for k in tum_kalemler(rev))
    assert all(k["overhead_pct"] is None and k["profit_pct"] is None for k in tum_kalemler(rev))
    assert rev["totals"]["unquantified_count"] == 3
    assert D(rev["totals"]["customer"]["net"]) == D("0")


async def test_maliyet_son_fiyat_once_referans_sonra_bos_TEK_cagri(
    client, admin, isveren, katalog, s, son_fiyat
) -> None:
    son_fiyat.koy(katalog[0].id, "77.50")  # Beton: son 77,50 (ref 100,00) → son fiyat KAZANIR
    o = await _teklif_sablondan(client, admin, isveren, s["id"])
    kalemler = tum_kalemler(await revizyon(client, admin, o["id"]))
    beton, kalip, demir = kalemler
    assert D(beton["cost_unit_price"]) == D("77.50")  # son fiyat > referans
    assert kalip["cost_unit_price"] is None  # son yok, ref yok → bos
    assert D(demir["cost_unit_price"]) == D("50.00")  # son yok → referans
    assert (beton["priced"], kalip["priced"], demir["priced"]) == (True, False, True)
    assert len(son_fiyat.cagrilar) == 1  # TEK toplu cagri
    assert set(son_fiyat.cagrilar[0]) == {katalog[0].id, katalog[1].id, katalog[2].id}


async def test_oranlar_sablondan_ayar_vb_kosullar_ayardan(
    client, admin, isveren, katalog, s
) -> None:
    o = await _teklif_sablondan(client, admin, isveren, s["id"])
    rev = await revizyon(client, admin, o["id"])
    assert (D(rev["overhead_pct"]), D(rev["profit_pct"])) == (D("9"), D("21"))  # sablondan
    assert D(rev["vat_pct"]) == D("20") and rev["validity_days"] == 30  # ayardan
    assert rev["payment_terms"] == "Ödeme aylık hakedişle, 30 gün vadeli"
    assert rev["price_escalation"] == "fixed"


async def test_govde_orani_sablonu_ezer_sablonda_oran_yoksa_ayar(
    client, admin, isveren, katalog, s
) -> None:
    o = await _teklif_sablondan(client, admin, isveren, s["id"], overhead_pct="5")
    rev = await revizyon(client, admin, o["id"])
    assert (D(rev["overhead_pct"]), D(rev["profit_pct"])) == (D("5"), D("21"))

    bos = await sablon(client, admin, "Oransız")  # GG/kar yok → ayar (12 / 15)
    await sablon_icerik(client, admin, bos["id"], [("G", [katalog[0].id])])
    o2 = await _teklif_sablondan(client, admin, isveren, bos["id"])
    rev2 = await revizyon(client, admin, o2["id"])
    assert (D(rev2["overhead_pct"]), D(rev2["profit_pct"])) == (D("12"), D("15"))


async def test_kullanim_sayisi_ve_sablon_DEGISMEZ(client, admin, isveren, katalog, s) -> None:
    once = (await client.get(f"{TPL}/{s['id']}", headers=admin)).json()
    assert once["usage_count"] == 0
    await _teklif_sablondan(client, admin, isveren, s["id"])
    sonra = (await client.get(f"{TPL}/{s['id']}", headers=admin)).json()
    assert sonra["usage_count"] == 1
    await _teklif_sablondan(client, admin, isveren, s["id"], title="İkinci")
    liste = (await client.get(TPL, headers=admin)).json()["items"]
    assert liste[0]["usage_count"] == 2
    once.pop("usage_count"), sonra.pop("usage_count")
    assert {k: v for k, v in once.items() if k != "updated_at"} == {
        k: v for k, v in sonra.items() if k != "updated_at"
    }  # sablon icerigi/oranlari degismedi
    assert once["updated_at"] == sonra["updated_at"]


async def test_VARSAYILAN_sablon_acik_secim_olmadan_KULLANILMAZ(
    client, admin, isveren, katalog, s
) -> None:
    await client.post(f"{TPL}/{s['id']}/default", headers=admin)
    resp = await client.post(
        "/offers", json={"employer_id": str(isveren.id), "title": "Boş"}, headers=admin
    )
    assert resp.status_code == 201
    o = resp.json()
    assert o["template_id"] is None
    rev = await revizyon(client, admin, o["id"])
    assert rev["groups"] == []
    assert (D(rev["overhead_pct"]), D(rev["profit_pct"])) == (D("12"), D("15"))


async def test_bilinmeyen_sablon_404_teklif_YAZILMAZ(client, admin, isveren) -> None:
    resp = await client.post(
        "/offers",
        json={"employer_id": str(isveren.id), "title": "X", "template_id": str(uuid.uuid4())},
        headers=admin,
    )
    assert resp.status_code == 404, resp.text
    assert (await client.get("/offers", headers=admin)).json()["total"] == 0


async def test_template_id_ve_copy_from_birlikte_422(client, admin, isveren, s) -> None:
    resp = await client.post(
        "/offers",
        json={
            "employer_id": str(isveren.id),
            "title": "X",
            "template_id": s["id"],
            "copy_from": {"offer_id": str(uuid.uuid4()), "rev_no": 0},
        },
        headers=admin,
    )
    assert resp.status_code == 422, resp.text
    assert "birlikte verilemez" in resp.text


async def test_isveren_ve_is_adi_kaynaksizsa_ZORUNLU(client, admin, isveren) -> None:
    r1 = await client.post("/offers", json={"title": "X"}, headers=admin)
    r2 = await client.post("/offers", json={"employer_id": str(isveren.id)}, headers=admin)
    assert r1.status_code == 422 and "İşveren zorunludur" in r1.text
    assert r2.status_code == 422 and "İş adı zorunludur" in r2.text


async def test_bos_sablondan_yalniz_gruplar_gelir(client, admin, isveren, katalog) -> None:
    sab = await sablon(client, admin, "Boş")
    d = await sablon_icerik(client, admin, sab["id"], [("A", []), ("B", [])])
    assert d["item_count"] == 0
    o = await _teklif_sablondan(client, admin, isveren, sab["id"])
    rev = await revizyon(client, admin, o["id"])
    assert [g["name"] for g in rev["groups"]] == ["A", "B"]
    assert tum_kalemler(rev) == []


async def test_SO21_sablondan_teklif_miktar_girilmeden_gonderilemez_girilince_gonderilir(
    client, admin, isveren, katalog, s
) -> None:
    o = await _teklif_sablondan(client, admin, isveren, s["id"])
    resp = await gecis(client, admin, o["id"], "send", dolu=False)
    assert resp.status_code == 422 and "Miktarı girilmemiş kalem var" in resp.text
    for k in tum_kalemler(await revizyon(client, admin, o["id"])):
        ok = await client.patch(
            rev_url(o["id"]) + f"/items/{k['id']}", json={"quantity": "2"}, headers=admin
        )
        assert ok.status_code == 200, ok.text
    resp = await gecis(client, admin, o["id"], "send", dolu=False)
    assert resp.status_code == 200, resp.text
    rev = await revizyon(client, admin, o["id"])
    assert rev["totals"]["unquantified_count"] == 0
    # Beton 100 (ref) x1,09 x1,21 = 131,89 → x2 = 263,78; Demir 50 → 65,95 → 131,90
    assert D(rev["totals"]["customer"]["net"]) == D("395.68")


async def test_yeni_teklif_sablonu_ve_kalem_ekleme_birlikte_calisir(
    client, admin, isveren, katalog, s
) -> None:
    o = await _teklif_sablondan(client, admin, isveren, s["id"])
    rev = await revizyon(client, admin, o["id"])
    ekstra = await kalem(
        client, admin, o["id"], rev["groups"][0]["id"], katalog[0].id, quantity="1"
    )
    assert ekstra["quantity"] is not None
    assert (await revizyon(client, admin, o["id"]))["totals"]["unquantified_count"] == 3


async def test_denetim_sablondan_olusturma_metni_ve_numara_kaynak_eki(
    client, admin, isveren, s, db_session
) -> None:
    o = await _teklif_sablondan(client, admin, isveren, s["id"])
    kayitlar = await _audit_details(db_session, AuditAction.create)
    assert (
        messages.offer_created_from_template(o["offer_no"], o["title"], o["employer_name"], "Villa")
        in kayitlar
    )
    assert messages.offer_created(o["offer_no"], o["title"], o["employer_name"]) not in kayitlar
    assert (await detay(client, admin, o["id"]))["template_id"] == s["id"]

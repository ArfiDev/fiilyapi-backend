"""TKL-B5.1 / SO-21 — miktarsiz kalem: calc + API + gonderim kurali.

`quantity IS NULL` = "miktar girilmedi": toplamlara GIRMEZ, adam-saati `None` (bilinmiyor, 0 DEGIL),
AYRI sayac `unquantified_count`; taslakta serbest, gonderimde engellenir (422).
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.modules.offers.calc import ItemInput, calc_item, calc_revision

from ._offers import URL, detay, gecis, grup, kalem, kalemli_yap, rev_url, revizyon, teklif

REV = {"overhead_pct": D("12"), "profit_pct": D("15")}


def _girdi(c: str | None, q: str | None, **kw) -> ItemInput:
    return ItemInput(
        quantity=None if q is None else D(q),
        unit_mhr=D("2"),
        cost_unit_price=None if c is None else D(c),
        **{k: D(v) for k, v in kw.items()},
    )


# ---------------------------------------------------------------------------- calc


def test_miktarsiz_fiyatli_kalem_tutar_ve_ic_degerleri_YOK_birim_fiyat_VAR() -> None:
    r = calc_item(_girdi("100", None), **REV)
    assert r.priced is True and r.quantified is False
    assert r.customer is not None
    assert r.customer.unit_price == D("128.80")  # 100 x 1,12 x 1,15
    assert r.customer.amount is None  # 0 DEGIL: tutar uretilmez
    assert r.internal.cost is None and r.internal.overhead is None and r.internal.profit is None
    assert r.internal.man_hours is None  # bilinmiyor: 0 DEGIL


def test_miktarsiz_elle_birim_fiyat_kar_yuzdesi_turetilir_tutar_yok() -> None:
    r = calc_item(_girdi("100", None, offer_unit_price="140"), **REV)
    assert r.customer is not None and r.customer.unit_price == D("140.00")
    assert r.customer.amount is None
    assert r.internal.profit_pct == D("25.00")  # 140 / (100 x 1,12) - 1


def test_toplamlar_miktarsiz_kalemi_DISLAR_sayaclar_bagimsiz() -> None:
    sonuc = calc_revision(
        [
            _girdi("100", "10"),  # fiyatli + miktarli: tutar 1288,00
            _girdi("100", None),  # fiyatli, miktarsiz
            _girdi(None, None),  # fiyatsiz VE miktarsiz: IKI sayaca da girer
            _girdi(None, "4"),  # fiyatsiz, miktarli
        ],
        vat_pct=D("20"),
        **REV,
    )
    assert sonuc.customer.net == D("1288.00")
    assert sonuc.customer.vat == D("257.60") and sonuc.customer.gross == D("1545.60")
    assert sonuc.internal.cost == D("1000.00")
    assert sonuc.internal.overhead == D("120.00") and sonuc.internal.profit == D("168.00")
    assert sonuc.internal.man_hours == D("28")  # (10 + 4) x 2; miktarsizlar (None) girmez
    assert sonuc.unpriced_count == 2  # miktarsiz-fiyatsiz + miktarli-fiyatsiz
    assert sonuc.unquantified_count == 2  # miktarsiz-fiyatli + miktarsiz-fiyatsiz


def test_miktarlari_dolu_revizyonda_unquantified_count_SIFIR_ve_hesap_ayni() -> None:
    sonuc = calc_revision([_girdi("100", "10"), _girdi(None, "1")], vat_pct=D("20"), **REV)
    assert sonuc.unquantified_count == 0 and sonuc.unpriced_count == 1
    assert sonuc.customer.net == D("1288.00")


def test_manuel_fiyat_maliyetsiz_miktarsizda_da_hata() -> None:
    from app.modules.offers.calc import ManualPriceWithoutCostError

    with pytest.raises(ManualPriceWithoutCostError):
        calc_item(_girdi(None, None, offer_unit_price="5"), **REV)


# ----------------------------------------------------------------------------- API


@pytest.fixture
async def t(client, admin, isveren):
    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    return o["id"], g["id"]


async def _miktarsiz(client, admin, oid, gid, katalog_id, **over) -> dict:
    govde = {"catalog_item_id": str(katalog_id), "group_id": gid, **over}
    resp = await client.post(rev_url(oid) + "/items", json=govde, headers=admin)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_miktar_gonderilmezse_NULL_kalem_toplama_girmez(client, admin, t, katalog) -> None:
    oid, gid = t
    await kalem(client, admin, oid, gid, katalog[0].id, quantity="10", cost_unit_price="100")
    k = await _miktarsiz(client, admin, oid, gid, katalog[0].id, cost_unit_price="100")
    assert k["quantity"] is None
    assert k["priced"] is True
    assert D(k["customer"]["unit_price"]) == D("128.80")
    assert k["customer"]["amount"] is None
    assert k["internal"]["cost"] is None and k["internal"]["man_hours"] is None

    rev = await revizyon(client, admin, oid)
    assert D(rev["totals"]["customer"]["net"]) == D("1288.00")
    assert rev["totals"]["unquantified_count"] == 1
    assert rev["totals"]["unpriced_count"] == 0
    assert D(rev["totals"]["internal"]["man_hours"]) == D("15.0")  # yalniz 10 x 1,5


async def test_acik_null_miktar_kabul_edilir_toplu_da(client, admin, t, katalog) -> None:
    oid, gid = t
    govde = {
        "items": [
            {"catalog_item_id": str(katalog[0].id), "group_id": gid, "quantity": None},
            {"catalog_item_id": str(katalog[2].id), "group_id": gid},
        ]
    }
    resp = await client.post(rev_url(oid) + "/items/bulk", json=govde, headers=admin)
    assert resp.status_code == 201, resp.text
    assert [k["quantity"] for k in resp.json()["items"]] == [None, None]
    assert (await revizyon(client, admin, oid))["totals"]["unquantified_count"] == 2


async def test_sifir_ve_negatif_miktar_hala_422(client, admin, t, katalog) -> None:
    oid, gid = t
    for kotu in ("0", "-1"):
        govde = {"catalog_item_id": str(katalog[0].id), "group_id": gid, "quantity": kotu}
        resp = await client.post(rev_url(oid) + "/items", json=govde, headers=admin)
        assert resp.status_code == 422, resp.text


async def test_patch_miktar_girilir_toplama_girer_null_patch_422(client, admin, t, katalog) -> None:
    oid, gid = t
    k = await _miktarsiz(client, admin, oid, gid, katalog[0].id, cost_unit_price="100")
    resp = await client.patch(
        rev_url(oid) + f"/items/{k['id']}", json={"quantity": None}, headers=admin
    )
    assert resp.status_code == 422, resp.text
    resp = await client.patch(
        rev_url(oid) + f"/items/{k['id']}", json={"quantity": "10"}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    assert D(resp.json()["customer"]["amount"]) == D("1288.00")
    rev = await revizyon(client, admin, oid)
    assert rev["totals"]["unquantified_count"] == 0
    assert D(rev["totals"]["customer"]["net"]) == D("1288.00")


async def test_sayac_teklif_detayi_ve_listede(client, admin, t, katalog) -> None:
    oid, gid = t
    await _miktarsiz(client, admin, oid, gid, katalog[0].id)
    await _miktarsiz(client, admin, oid, gid, katalog[1].id)  # Kalip: ref yok → fiyatsiz da
    rev = await revizyon(client, admin, oid)
    assert rev["totals"]["unquantified_count"] == 2 and rev["totals"]["unpriced_count"] == 1
    ozet = (await detay(client, admin, oid))["revisions"][0]
    assert ozet["unquantified_count"] == 2 and ozet["unpriced_count"] == 1
    satir = (await client.get(URL, headers=admin)).json()["items"][0]
    assert satir["unquantified_count"] == 2 and satir["unpriced_count"] == 1


async def test_SO21_miktarsiz_kalemli_revizyon_send_422_durum_degismez(
    client, admin, t, katalog
) -> None:
    oid, gid = t
    await kalem(client, admin, oid, gid, katalog[0].id, quantity="2")
    k = await _miktarsiz(client, admin, oid, gid, katalog[2].id)
    resp = await gecis(client, admin, oid, "send", dolu=False)
    assert resp.status_code == 422, resp.text
    assert "Miktarı girilmemiş kalem var" in resp.text
    rev = await revizyon(client, admin, oid)
    assert rev["status"] == "draft" and rev["sent_at"] is None

    ok = await client.patch(
        rev_url(oid) + f"/items/{k['id']}", json={"quantity": "1"}, headers=admin
    )
    assert ok.status_code == 200, ok.text
    resp = await gecis(client, admin, oid, "send", dolu=False)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "sent"


async def test_kalemsiz_mesaji_miktar_mesajindan_ONCE_gelir(client, admin, t) -> None:
    resp = await gecis(client, admin, t[0], "send", dolu=False)
    assert resp.status_code == 422 and "Teklifte kalem yok" in resp.text


async def test_fiyatsiz_ama_miktarli_kalem_hala_gonderilebilir(client, admin, t, katalog) -> None:
    oid, gid = t
    await kalem(client, admin, oid, gid, katalog[1].id, quantity="3", cost_unit_price=None)
    resp = await gecis(client, admin, oid, "send", dolu=False)
    assert resp.status_code == 200, resp.text


async def test_kalemli_yap_yardimcisi_miktarli_kalem_ekler(client, admin, t) -> None:
    oid, _ = t
    await kalemli_yap(client, admin, oid)
    assert (await gecis(client, admin, oid, "send", dolu=False)).status_code == 200


def test_miktarsiz_FIYATSIZ_kalemde_de_adam_saat_None_miktarli_fiyatsizda_dolu() -> None:
    bos = calc_item(_girdi(None, None), **REV)
    assert bos.priced is False and bos.quantified is False
    assert bos.internal.man_hours is None
    dolu = calc_item(_girdi(None, "4"), **REV)
    assert dolu.priced is False and dolu.internal.man_hours == D("8")  # 4 x 2

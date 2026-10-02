"""TKL-B4.2 — grup + kalem uclari: ekleme (SO-6 uc dal), toplu hep-ya-hic, PATCH (SO-4 birlesik
dogrulama, katalog bagi degismez), detay hesabi (ELLE hesaplanmis sabitler), tavan/hassasiyet
(E4/E5).

Beklenen para degerleri ELLE hesaplanmistir (uretim fonksiyonundan turetilmez).
Formul: B.F. = ROUND(c x (1+g) x (1+k)); tutar = ROUND(B.F. x q); maliyet = ROUND(c x q);
GG = ROUND(c x (1+g) x q) - maliyet; kar = tutar - maliyet - GG.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.modules.audit.models import AuditAction
from app.modules.offers.models import OfferItem

from .._boq import _audit_details
from ._offers import URL, D, detay, grup, kalem, rev_url, revizyon, teklif, tum_kalemler


@pytest.fixture
async def t(client, admin, isveren):
    """Bir teklif + `Kaba` grubu (Rev.0 taslak)."""
    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    return o["id"], g["id"]


# ---------------------------------------------------------------- ekleme: kopya alanlar


async def test_kalem_katalogdan_KOPYALAR_ve_adam_saat_katalogdan(client, admin, t, katalog) -> None:
    oid, gid = t
    beton = katalog[0]
    k = await kalem(client, admin, oid, gid, beton.id, quantity="2.5")
    assert k["poz_no"] == beton.poz_no
    assert k["description"] == "Beton"  # = katalog adi
    assert k["unit"] == "m3"
    assert D(k["unit_mhr"]) == D("1.5")  # govdede yok → katalog standart adam-saati
    assert D(k["quantity"]) == D("2.5")
    assert k["catalog_item_id"] == str(beton.id) and k["group_id"] == gid
    ozel = await kalem(client, admin, oid, gid, beton.id, unit_mhr="3.25")
    assert D(ozel["unit_mhr"]) == D("3.25")


async def test_katalog_yok_404_ve_catalog_item_id_zorunlu_422(client, admin, t) -> None:
    oid, gid = t
    govde = {"catalog_item_id": str(uuid.uuid4()), "group_id": gid, "quantity": "1"}
    assert (
        await client.post(rev_url(oid) + "/items", json=govde, headers=admin)
    ).status_code == 404
    eksik = {"group_id": gid, "quantity": "1"}
    assert (
        await client.post(rev_url(oid) + "/items", json=eksik, headers=admin)
    ).status_code == 422
    # katalog disi kalem (serbest ad) YOK
    serbest = {"group_id": gid, "quantity": "1", "description": "Serbest"}
    assert (
        await client.post(rev_url(oid) + "/items", json=serbest, headers=admin)
    ).status_code == 422


async def test_grup_baska_revizyonda_422(client, admin, isveren, t, katalog) -> None:
    oid, _ = t
    baska = await teklif(client, admin, isveren, title="Baska")
    bg = await grup(client, admin, baska["id"], name="Baska grup")
    govde = {"catalog_item_id": str(katalog[0].id), "group_id": bg["id"], "quantity": "1"}
    resp = await client.post(rev_url(oid) + "/items", json=govde, headers=admin)
    assert resp.status_code == 422, resp.text
    assert "Grup bu revizyona ait değil" in resp.json()["detail"]
    govde["group_id"] = str(uuid.uuid4())  # hic olmayan grup: govde-ici referans kanonu → 404
    assert (
        await client.post(rev_url(oid) + "/items", json=govde, headers=admin)
    ).status_code == 404


@pytest.mark.parametrize("quantity", ["0", "-1", "1000000000.001"])
async def test_miktar_sinirlari_422(client, admin, t, katalog, quantity) -> None:
    oid, gid = t
    govde = {"catalog_item_id": str(katalog[0].id), "group_id": gid, "quantity": quantity}
    assert (
        await client.post(rev_url(oid) + "/items", json=govde, headers=admin)
    ).status_code == 422


# ------------------------------------------------------------------- SO-6 (uc dal)


async def test_SO6_son_fiyat_referanstan_ONCE_gelir(client, admin, t, katalog, son_fiyat) -> None:
    oid, gid = t
    son_fiyat.koy(katalog[0].id, "77.50")  # Beton: son fiyat 77,50 (ref 100,00)
    k = await kalem(client, admin, oid, gid, katalog[0].id)
    assert D(k["cost_unit_price"]) == D("77.50")


async def test_SO6_son_fiyat_yoksa_referans(client, admin, t, katalog, son_fiyat) -> None:
    oid, gid = t
    k = await kalem(client, admin, oid, gid, katalog[2].id)  # Demir: son yok, ref 50,00
    assert D(k["cost_unit_price"]) == D("50.00")
    assert k["priced"] is True


async def test_SO6_ikisi_de_yoksa_bos_ve_fiyatsiz(client, admin, t, katalog, son_fiyat) -> None:
    oid, gid = t
    k = await kalem(client, admin, oid, gid, katalog[1].id)  # Kalip: son yok, ref yok
    assert k["cost_unit_price"] is None
    assert k["priced"] is False and k["customer"] is None
    assert D(k["internal"]["man_hours"]) == D("2")  # adam-saat fiyatsizda da dolu (1 x 2)


async def test_SO6_acik_null_son_fiyat_ve_referansa_RAGMEN_bos_kalir(
    client, admin, t, katalog, son_fiyat
) -> None:
    oid, gid = t
    son_fiyat.koy(katalog[0].id, "77.50")
    k = await kalem(client, admin, oid, gid, katalog[0].id, cost_unit_price=None)
    assert k["cost_unit_price"] is None and k["priced"] is False
    assert son_fiyat.cagrilar == []  # maliyeti gonderilen kalem icin saglayici CAGRILMAZ


async def test_SO6_govdedeki_maliyet_oneriyi_ezer(client, admin, t, katalog, son_fiyat) -> None:
    oid, gid = t
    son_fiyat.koy(katalog[0].id, "77.50")
    k = await kalem(client, admin, oid, gid, katalog[0].id, cost_unit_price="10.00")
    assert D(k["cost_unit_price"]) == D("10.00")


async def test_SO6_toplu_son_fiyat_TEK_cagri_ve_kalem_basina_dogru_dal(
    client, admin, t, katalog, son_fiyat
) -> None:
    oid, gid = t
    son_fiyat.koy(katalog[0].id, "77.50")
    govde = {
        "items": [
            {"catalog_item_id": str(katalog[0].id), "group_id": gid, "quantity": "1"},
            {"catalog_item_id": str(katalog[1].id), "group_id": gid, "quantity": "1"},
            {"catalog_item_id": str(katalog[2].id), "group_id": gid, "quantity": "1"},
            {
                "catalog_item_id": str(katalog[2].id),
                "group_id": gid,
                "quantity": "1",
                "cost_unit_price": "9.99",
            },
        ]
    }
    resp = await client.post(rev_url(oid) + "/items/bulk", json=govde, headers=admin)
    assert resp.status_code == 201, resp.text
    maliyetler = [k["cost_unit_price"] for k in resp.json()["items"]]
    assert [None if m is None else D(m) for m in maliyetler] == [
        D("77.50"),
        None,
        D("50.00"),
        D("9.99"),
    ]
    assert len(son_fiyat.cagrilar) == 1  # TEK toplu cagri
    assert set(son_fiyat.cagrilar[0]) == {katalog[0].id, katalog[1].id, katalog[2].id}


# ---------------------------------------------------------------- toplu: hep-ya-hic


async def _kalem_sayisi(db_session) -> int:
    return await db_session.scalar(select(func.count()).select_from(OfferItem))


async def test_toplu_hep_ya_hic_katalog_yok_hicbiri_yazilmaz(
    client, admin, t, katalog, db_session
) -> None:
    oid, gid = t
    govde = {
        "items": [
            {"catalog_item_id": str(katalog[0].id), "group_id": gid, "quantity": "1"},
            {"catalog_item_id": str(uuid.uuid4()), "group_id": gid, "quantity": "1"},
        ]
    }
    resp = await client.post(rev_url(oid) + "/items/bulk", json=govde, headers=admin)
    assert resp.status_code == 404, resp.text
    assert await _kalem_sayisi(db_session) == 0
    assert not [d for d in await _audit_details(db_session, AuditAction.create) if "kalem" in d]


async def test_toplu_hep_ya_hic_bilinmeyen_grup_404_kalem_sirasi_mesajda(
    client, admin, t, katalog, db_session
) -> None:
    oid, gid = t
    govde = {
        "items": [
            {"catalog_item_id": str(katalog[0].id), "group_id": gid, "quantity": "1"},
            {"catalog_item_id": str(katalog[1].id), "group_id": str(uuid.uuid4()), "quantity": "1"},
        ]
    }
    resp = await client.post(rev_url(oid) + "/items/bulk", json=govde, headers=admin)
    assert resp.status_code == 404, resp.text  # hicbir revizyonda olmayan grup (TKL-B4.4 R4)
    assert resp.json()["detail"].startswith("Kalem 2:")
    assert await _kalem_sayisi(db_session) == 0


async def test_toplu_SO4_ihlali_hep_ya_hic(client, admin, t, katalog, db_session) -> None:
    oid, gid = t
    govde = {
        "items": [
            {"catalog_item_id": str(katalog[0].id), "group_id": gid, "quantity": "1"},
            {  # Kalip: oneri yok + elle B.F. → maliyet bos kalir → 422
                "catalog_item_id": str(katalog[1].id),
                "group_id": gid,
                "quantity": "1",
                "offer_unit_price": "10.00",
            },
        ]
    }
    resp = await client.post(rev_url(oid) + "/items/bulk", json=govde, headers=admin)
    assert resp.status_code == 422, resp.text
    assert "Kalem 2:" in resp.json()["detail"]
    assert await _kalem_sayisi(db_session) == 0


@pytest.mark.parametrize(("adet", "kod"), [(0, 422), (200, 201), (201, 422)])
async def test_toplu_sinirlar_1_ile_200(client, admin, t, katalog, db_session, adet, kod) -> None:
    oid, gid = t
    ogeler = [
        {"catalog_item_id": str(katalog[i % 3].id), "group_id": gid, "quantity": "1"}
        for i in range(adet)
    ]
    resp = await client.post(rev_url(oid) + "/items/bulk", json={"items": ogeler}, headers=admin)
    assert resp.status_code == kod, resp.text[:300]
    assert await _kalem_sayisi(db_session) == (200 if kod == 201 else 0)


async def test_toplu_TEK_denetim_satiri_tekil_ekleme_satir_yazmaz(
    client, admin, t, katalog, db_session
) -> None:
    oid, gid = t
    await kalem(client, admin, oid, gid, katalog[0].id)  # tekil: denetim YOK
    antes = len(await _audit_details(db_session, AuditAction.create))
    govde = {
        "items": [
            {"catalog_item_id": str(katalog[i].id), "group_id": gid, "quantity": "1"}
            for i in range(3)
        ]
    }
    resp = await client.post(rev_url(oid) + "/items/bulk", json=govde, headers=admin)
    assert resp.status_code == 201, resp.text
    detaylar = await _audit_details(db_session, AuditAction.create)
    assert len(detaylar) == antes + 1
    no = (await detay(client, admin, oid))["offer_no"]
    assert detaylar[-1] == (
        f"Teklife 3 kalem eklendi: {no} Rev.0 · "
        f"{katalog[0].poz_no}, {katalog[1].poz_no}, {katalog[2].poz_no}"
    )


async def test_toplu_sira_numaralari_grupta_ardisik(client, admin, t, katalog) -> None:
    oid, gid = t
    govde = {
        "items": [
            {"catalog_item_id": str(katalog[i].id), "group_id": gid, "quantity": "1"}
            for i in range(3)
        ]
    }
    await client.post(rev_url(oid) + "/items/bulk", json=govde, headers=admin)
    await kalem(client, admin, oid, gid, katalog[0].id)
    siralar = [k["sort_order"] for k in tum_kalemler(await revizyon(client, admin, oid))]
    assert sorted(siralar) == [0, 1, 2, 3]


# ----------------------------------------------------------- kalem PATCH (+ SO-4)


async def _fiyatli(client, admin, oid, gid, katalog_id, **over) -> dict:
    return await kalem(client, admin, oid, gid, katalog_id, cost_unit_price="100.00", **over)


def _kalem_url(oid: str, item_id: str) -> str:
    return rev_url(oid) + f"/items/{item_id}"


@pytest.mark.parametrize("alan", ["catalog_item_id", "poz_no", "description", "unit"])
async def test_PATCH_katalog_alani_degistirilemez_acik_422(client, admin, t, katalog, alan) -> None:
    oid, gid = t
    k = await _fiyatli(client, admin, oid, gid, katalog[0].id)
    deger = str(katalog[1].id) if alan == "catalog_item_id" else "X"
    resp = await client.patch(_kalem_url(oid, k["id"]), json={alan: deger}, headers=admin)
    assert resp.status_code == 422, resp.text
    assert "değiştirilemez" in resp.text and alan in resp.text
    assert (await revizyon(client, admin, oid))["groups"][0]["items"][0]["poz_no"] == k["poz_no"]


async def test_SO4_PATCH_maliyetsiz_kaleme_elle_BF_422(
    client, admin, t, katalog, son_fiyat
) -> None:
    oid, gid = t
    k = await kalem(client, admin, oid, gid, katalog[1].id)  # maliyet bos
    resp = await client.patch(
        _kalem_url(oid, k["id"]), json={"offer_unit_price": "10.00"}, headers=admin
    )
    assert resp.status_code == 422, resp.text
    assert "elle teklif birim fiyatı girilemez" in resp.json()["detail"]
    kayit = tum_kalemler(await revizyon(client, admin, oid))[0]
    assert kayit["offer_unit_price"] is None  # reddedilen yazma hicbir sey degistirmedi


async def test_SO4_E1_PATCH_yalniz_maliyet_null_elle_BF_KALIYORSA_422(
    client, admin, t, katalog, db_session
) -> None:
    """BIRLESIK durum: gövdede yalniz `cost_unit_price: null`; elle B.F. kayitta duruyor."""
    oid, gid = t
    k = await _fiyatli(client, admin, oid, gid, katalog[0].id, offer_unit_price="150.00")
    resp = await client.patch(
        _kalem_url(oid, k["id"]), json={"cost_unit_price": None}, headers=admin
    )
    assert resp.status_code == 422, resp.text  # 409/500 DEGIL (CHECK'e carpmaz)
    kayit = tum_kalemler(await revizyon(client, admin, oid))[0]
    assert D(kayit["cost_unit_price"]) == D("100.00") and D(kayit["offer_unit_price"]) == D("150")
    # ayni istekte kilit da kalkarsa gecerli
    ok = await client.patch(
        _kalem_url(oid, k["id"]),
        json={"cost_unit_price": None, "offer_unit_price": None},
        headers=admin,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["priced"] is False


async def test_SO4_E1_ekleme_acik_null_maliyet_ve_elle_BF_422(
    client, admin, t, katalog, son_fiyat
) -> None:
    oid, gid = t
    govde = {
        "catalog_item_id": str(katalog[0].id),  # ref 100 VAR ama acik null onu ezer
        "group_id": gid,
        "quantity": "1",
        "cost_unit_price": None,
        "offer_unit_price": "10.00",
    }
    resp = await client.post(rev_url(oid) + "/items", json=govde, headers=admin)
    assert resp.status_code == 422, resp.text
    # oneri varsa (ref 100) maliyet dolar → elle B.F. ile birlikte gecerli
    del govde["cost_unit_price"]
    ok = await client.post(rev_url(oid) + "/items", json=govde, headers=admin)
    assert ok.status_code == 201, ok.text
    assert D(ok.json()["customer"]["unit_price"]) == D("10.00")


async def test_PATCH_oran_null_revizyon_genelini_kullanir_elle_BF_null_kilidi_kaldirir(
    client, admin, t, katalog
) -> None:
    oid, gid = t
    k = await _fiyatli(client, admin, oid, gid, katalog[0].id, overhead_pct="0", profit_pct="0")
    assert D(k["customer"]["unit_price"]) == D("100.00")  # kalem oranlari 0
    r1 = await client.patch(
        _kalem_url(oid, k["id"]), json={"overhead_pct": None, "profit_pct": None}, headers=admin
    )
    assert r1.status_code == 200, r1.text
    assert D(r1.json()["customer"]["unit_price"]) == D("128.80")  # %12 / %15 genel
    assert r1.json()["overhead_pct"] is None
    r2 = await client.patch(
        _kalem_url(oid, k["id"]), json={"offer_unit_price": "200.00"}, headers=admin
    )
    assert D(r2.json()["customer"]["unit_price"]) == D("200.00")
    r3 = await client.patch(
        _kalem_url(oid, k["id"]), json={"offer_unit_price": None}, headers=admin
    )
    assert D(r3.json()["customer"]["unit_price"]) == D("128.80")  # kilit kalkti


@pytest.mark.parametrize(
    "govde",
    [
        {"quantity": None},
        {"quantity": "0"},
        {"unit_mhr": "0"},
        {"unit_mhr": None},
        {"group_id": None},
        {"sort_order": None},
        {"sort_order": -1},
        {"overhead_pct": "100.01"},
        {"profit_pct": "1000"},
        {"cost_unit_price": "-0.01"},
    ],
)
async def test_PATCH_gecersiz_degerler_422(client, admin, t, katalog, govde) -> None:
    oid, gid = t
    k = await _fiyatli(client, admin, oid, gid, katalog[0].id)
    resp = await client.patch(_kalem_url(oid, k["id"]), json=govde, headers=admin)
    assert resp.status_code == 422, resp.text


async def test_PATCH_grup_tasima_ve_baska_revizyonun_grubu_422(
    client, admin, isveren, t, katalog
) -> None:
    oid, gid = t
    g2 = await grup(client, admin, oid, name="Ince")
    k = await _fiyatli(client, admin, oid, gid, katalog[0].id)
    ok = await client.patch(_kalem_url(oid, k["id"]), json={"group_id": g2["id"]}, headers=admin)
    assert ok.status_code == 200 and ok.json()["group_id"] == g2["id"]
    baska = await teklif(client, admin, isveren, title="Baska")
    bg = await grup(client, admin, baska["id"], name="B")
    kotu = await client.patch(_kalem_url(oid, k["id"]), json={"group_id": bg["id"]}, headers=admin)
    assert kotu.status_code == 422, kotu.text


async def test_PATCH_olmayan_ve_baska_tekliflerin_kalemi_404(
    client, admin, isveren, t, katalog
) -> None:
    oid, gid = t
    baska = await teklif(client, admin, isveren, title="Baska")
    bg = await grup(client, admin, baska["id"], name="B")
    bk = await _fiyatli(client, admin, baska["id"], bg["id"], katalog[0].id)
    assert (
        await client.patch(_kalem_url(oid, bk["id"]), json={"quantity": "2"}, headers=admin)
    ).status_code == 404
    assert (
        await client.patch(
            _kalem_url(oid, str(uuid.uuid4())), json={"quantity": "2"}, headers=admin
        )
    ).status_code == 404


async def test_kalem_ve_grup_silme_grup_icindekilerle_birlikte(
    client, admin, t, katalog, db_session
) -> None:
    oid, gid = t
    g2 = await grup(client, admin, oid, name="Ince")
    k1 = await _fiyatli(client, admin, oid, gid, katalog[0].id)
    await _fiyatli(client, admin, oid, g2["id"], katalog[2].id)
    assert (await client.delete(_kalem_url(oid, k1["id"]), headers=admin)).status_code == 204
    assert (await client.delete(_kalem_url(oid, k1["id"]), headers=admin)).status_code == 404
    assert await _kalem_sayisi(db_session) == 1
    assert (
        await client.delete(rev_url(oid) + f"/groups/{g2['id']}", headers=admin)
    ).status_code == 204
    assert await _kalem_sayisi(db_session) == 0  # grupla birlikte gitti
    rev = await revizyon(client, admin, oid)
    assert [g["name"] for g in rev["groups"]] == ["Kaba"]


async def test_grup_patch_ad_ve_sira_bos_ad_ve_null_422(client, admin, t) -> None:
    oid, gid = t
    url = rev_url(oid) + f"/groups/{gid}"
    ok = await client.patch(url, json={"name": " Yeni ad ", "sort_order": 7}, headers=admin)
    assert ok.status_code == 200 and ok.json() == {"id": gid, "name": "Yeni ad", "sort_order": 7}
    for kotu in ({"name": ""}, {"name": "   "}, {"name": None}, {"sort_order": None}):
        assert (await client.patch(url, json=kotu, headers=admin)).status_code == 422
    assert (
        await client.patch(
            rev_url(oid) + f"/groups/{uuid.uuid4()}", json={"name": "x"}, headers=admin
        )
    ).status_code == 404


async def test_kalem_ve_grup_tekil_duzenlemeleri_denetim_satiri_YAZMAZ(
    client, admin, t, katalog, db_session
) -> None:
    """Tasarim karari: gurultu. Yapisal olaylar (olustur/kunye/sil/revizyon/gecis/toplu) yazar."""
    oid, gid = t
    onceki = {a: len(await _audit_details(db_session, a)) for a in AuditAction}
    k = await _fiyatli(client, admin, oid, gid, katalog[0].id)
    await client.patch(_kalem_url(oid, k["id"]), json={"quantity": "3"}, headers=admin)
    g2 = await grup(client, admin, oid, name="Ince")
    await client.patch(rev_url(oid) + f"/groups/{g2['id']}", json={"name": "z"}, headers=admin)
    await client.delete(_kalem_url(oid, k["id"]), headers=admin)
    await client.delete(rev_url(oid) + f"/groups/{g2['id']}", headers=admin)
    sonra = {a: len(await _audit_details(db_session, a)) for a in AuditAction}
    assert sonra == onceki


# ------------------------------------------------------------- detay: calc sabitleri


async def test_detay_hesap_elle_hesaplanmis_sabitler_ve_degismez(
    client, admin, t, katalog, son_fiyat
) -> None:
    """Rev: GG %12 / kar %15 / KDV %20 (ayar varsayilani). Dort kalem:
    1) c=100 q=10 mhr=2   → B.F. 128,80 · tutar 1288,00 · maliyet 1000,00 · GG 120,00 · kar 168,00
    2) maliyet YOK q=4 mhr=2,5 → fiyatsiz (toplama girmez), adam-saat 10
    3) c=50 q=3 mhr=1 ELLE B.F. 80 → tutar 240,00 · maliyet 150,00 · GG 18,00 · kar 72,00
       (GG = ROUND(50x1,12x3 = 168) - 150; turev kar % = 80/56 - 1 = 42,86)
    4) c=33,33 q=3 mhr=0,5 GG %10 kar %20 → B.F. 44,00 · tutar 132,00 · maliyet 99,99 ·
       GG = ROUND(109,989) - 99,99 = 10,00 · kar 22,01
    Toplam: net 1660,00 · KDV 332,00 · brut 1992,00 · maliyet 1249,99 · GG 148,00 · kar 262,01 ·
    genel kar % = 262,01 / (1249,99 + 148,00) = 18,74 · adam-saat 20 + 10 + 3 + 1,5 = 34,5."""
    oid, gid = t
    a = await kalem(
        client, admin, oid, gid, katalog[0].id, quantity="10", cost_unit_price="100", unit_mhr="2"
    )
    b = await kalem(client, admin, oid, gid, katalog[1].id, quantity="4", unit_mhr="2.5")
    c = await kalem(
        client,
        admin,
        oid,
        gid,
        katalog[2].id,
        quantity="3",
        unit_mhr="1",
        cost_unit_price="50",
        offer_unit_price="80",
    )
    d = await kalem(
        client,
        admin,
        oid,
        gid,
        katalog[0].id,
        quantity="3",
        unit_mhr="0.5",
        cost_unit_price="33.33",
        overhead_pct="10",
        profit_pct="20",
    )
    rev = await revizyon(client, admin, oid)
    kalemler = {k["id"]: k for k in tum_kalemler(rev)}

    def deger(k: dict) -> tuple:
        c_, i_ = k["customer"], k["internal"]
        return tuple(
            D(x) for x in (c_["unit_price"], c_["amount"], i_["cost"], i_["overhead"], i_["profit"])
        )

    assert deger(kalemler[a["id"]]) == (
        D("128.80"),
        D("1288.00"),
        D("1000.00"),
        D("120.00"),
        D("168.00"),
    )
    assert D(kalemler[a["id"]]["internal"]["profit_pct"]) == D("15")  # uygulanan kar %
    assert kalemler[b["id"]]["priced"] is False and kalemler[b["id"]]["customer"] is None
    assert D(kalemler[b["id"]]["internal"]["man_hours"]) == D("10")
    assert deger(kalemler[c["id"]]) == (
        D("80.00"),
        D("240.00"),
        D("150.00"),
        D("18.00"),
        D("72.00"),
    )
    assert D(kalemler[c["id"]]["internal"]["profit_pct"]) == D("42.86")  # turev
    assert deger(kalemler[d["id"]]) == (D("44.00"), D("132.00"), D("99.99"), D("10.00"), D("22.01"))
    for k in kalemler.values():
        if k["priced"]:  # DEGISMEZ: maliyet + GG + kar = tutar, kalem basina BIREBIR
            i_ = k["internal"]
            assert D(i_["cost"]) + D(i_["overhead"]) + D(i_["profit"]) == D(k["customer"]["amount"])

    t_ = rev["totals"]
    assert (D(t_["customer"]["net"]), D(t_["customer"]["vat"]), D(t_["customer"]["gross"])) == (
        D("1660.00"),
        D("332.00"),
        D("1992.00"),
    )
    ic = t_["internal"]
    assert (D(ic["cost"]), D(ic["overhead"]), D(ic["profit"])) == (
        D("1249.99"),
        D("148.00"),
        D("262.01"),
    )
    assert D(ic["profit_pct"]) == D("18.74")
    assert D(ic["man_hours"]) == D("34.5")
    assert t_["unpriced_count"] == 1
    assert D(ic["cost"]) + D(ic["overhead"]) + D(ic["profit"]) == D(t_["customer"]["net"])

    # teklif detayi + liste ayni net/brut/fiyatsiz sayisini tasir
    ozet = (await detay(client, admin, oid))["revisions"][0]
    assert (D(ozet["net"]), D(ozet["gross"]), ozet["unpriced_count"]) == (
        D("1660.00"),
        D("1992.00"),
        1,
    )
    satir = (await client.get(URL, headers=admin)).json()["items"][0]
    assert (D(satir["net"]), D(satir["gross"]), satir["unpriced_count"]) == (
        D("1660.00"),
        D("1992.00"),
        1,
    )


async def test_detay_kosul_degisince_toplamlar_yeniden_hesaplanir(
    client, admin, t, katalog
) -> None:
    oid, gid = t
    await kalem(client, admin, oid, gid, katalog[0].id, quantity="10", cost_unit_price="100")
    r = await client.patch(
        rev_url(oid), json={"overhead_pct": "0", "profit_pct": "0", "vat_pct": "10"}, headers=admin
    )
    assert r.status_code == 200, r.text
    top = r.json()["totals"]
    assert (D(top["customer"]["net"]), D(top["customer"]["vat"]), D(top["customer"]["gross"])) == (
        D("1000.00"),
        D("100.00"),
        D("1100.00"),
    )


async def test_detay_gruplar_ve_kalemler_sirali_bos_revizyon_sifir(
    client, admin, isveren, katalog
) -> None:
    o = await teklif(client, admin, isveren)
    rev = await revizyon(client, admin, o["id"])
    assert rev["groups"] == []
    assert (D(rev["totals"]["customer"]["net"]), rev["totals"]["unpriced_count"]) == (D(0), 0)
    assert rev["totals"]["internal"]["profit_pct"] is None
    g1 = await grup(client, admin, o["id"], name="B grubu")
    g0 = await grup(client, admin, o["id"], name="A grubu")
    await client.patch(
        rev_url(o["id"]) + f"/groups/{g0['id']}", json={"sort_order": 0}, headers=admin
    )
    await client.patch(
        rev_url(o["id"]) + f"/groups/{g1['id']}", json={"sort_order": 5}, headers=admin
    )
    assert [g["name"] for g in (await revizyon(client, admin, o["id"]))["groups"]] == [
        "A grubu",
        "B grubu",
    ]


# ------------------------------------------------------ E4 / E5: tavan ve hassasiyet


@pytest.mark.parametrize(
    "govde",
    [
        {"cost_unit_price": "1000000000000.01"},  # > 1e12
        {"offer_unit_price": "1000000000000.01"},
        {"cost_unit_price": "10.001"},  # E5: kurus ustu hassasiyet
        {"offer_unit_price": "10.005"},
        {"quantity": "1000000000.001"},  # > 1e9
        {"unit_mhr": "1000000.0001"},  # > 1e6
    ],
)
async def test_E4_E5_tavan_ve_kurus_hassasiyeti_422_500_degil(
    client, admin, t, katalog, govde
) -> None:
    oid, gid = t
    istek = {
        "catalog_item_id": str(katalog[0].id),
        "group_id": gid,
        "quantity": "1",
        "cost_unit_price": "1",
        **govde,
    }
    resp = await client.post(rev_url(oid) + "/items", json=istek, headers=admin)
    assert resp.status_code == 422, resp.text


async def test_E4_tavandaki_kalem_kabul_edilir_ve_detay_500_vermez(
    client, admin, t, katalog
) -> None:
    """c = 1e12, q = 1e9, GG %100, kar %999,99 → B.F. = 21 999 800 000 000,00 (ELLE)."""
    oid, gid = t
    k = await kalem(
        client,
        admin,
        oid,
        gid,
        katalog[0].id,
        quantity="1000000000",
        cost_unit_price="1000000000000.00",
        overhead_pct="100",
        profit_pct="999.99",
        unit_mhr="1000000",
    )
    assert D(k["customer"]["unit_price"]) == D("21999800000000.00")
    assert D(k["customer"]["amount"]) == D("21999800000000000000000.00")
    rev = await revizyon(client, admin, oid)
    assert D(rev["totals"]["customer"]["net"]) == D("21999800000000000000000.00")
    assert D(rev["totals"]["internal"]["man_hours"]) == D("1000000000000000")

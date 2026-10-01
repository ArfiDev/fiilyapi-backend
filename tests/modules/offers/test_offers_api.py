"""TKL-B4.2 — teklif uclari: olusturma, kunye, durum gecisleri, yeni revizyon, silme, denetim.

Kalem/grup/hesap testleri `test_offer_items_api.py`, liste `test_offers_list_api.py`, izin/maske
`test_offers_izin_maske.py`, yarislar `test_offers_race.py`dedir.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.core.timezone import today
from app.modules.audit.models import AuditAction
from app.modules.offers import numbering
from app.modules.offers.models import Offer, OfferRevision

from .._boq import _audit_details
from ._offers import (
    URL,
    D,
    detay,
    durum_yap,
    gecis,
    grup,
    kalem,
    rev_url,
    revizyon,
    teklif,
    tum_kalemler,
)

YIL = numbering.current_offer_year()
ODEME = "Ödeme aylık hakedişle, 30 gün vadeli"


# ----------------------------------------------------------------------- olusturma


async def test_olustur_numara_rev0_taslak_ve_ayar_varsayilanlari(client, admin, isveren) -> None:
    o = await teklif(client, admin, isveren, scope_summary="Kaba inşaat")
    assert o["offer_no"] == f"TKL-{YIL}-0001"
    assert o["employer_id"] == str(isveren.id) and o["employer_name"] == "Akın İnşaat A.Ş."
    assert o["title"] == "A Blok Kaba İnşaat" and o["scope_summary"] == "Kaba inşaat"
    assert o["status"] == "draft" and o["latest_rev_no"] == 0
    assert [r["rev_no"] for r in o["revisions"]] == [0]
    assert [e["kind"] for e in o["history"]] == ["opened"]

    rev = await revizyon(client, admin, o["id"])
    assert rev["status"] == "draft" and rev["rev_no"] == 0
    assert rev["is_latest"] is True and rev["is_editable"] is True
    assert (D(rev["overhead_pct"]), D(rev["profit_pct"]), D(rev["vat_pct"])) == (
        D(12),
        D(15),
        D(20),
    )
    assert rev["validity_days"] == 30 and rev["payment_terms"] == ODEME
    assert rev["offer_date"] == today().isoformat()  # Istanbul bugunu (date.today() DEGIL)
    assert (
        rev["valid_until"] == (today().replace()).fromordinal(today().toordinal() + 30).isoformat()
    )
    assert rev["price_escalation"] == "fixed" and rev["price_index_type"] is None  # SO-5
    assert rev["delivery_days"] is None and rev["notes"] is None
    assert rev["groups"] == []
    assert not [k for k in rev if "currency" in k]  # yalniz TL


async def test_olustur_ikinci_teklif_ardisik_numara(client, admin, isveren) -> None:
    a = await teklif(client, admin, isveren)
    b = await teklif(client, admin, isveren, title="İkinci")
    assert (a["offer_no"], b["offer_no"]) == (f"TKL-{YIL}-0001", f"TKL-{YIL}-0002")


async def test_olustur_kosullar_AYARDAN_kopyalanir_govde_ezer(client, admin, isveren) -> None:
    await client.put(
        "/offers/settings",
        json={
            "default_overhead_pct": "10",
            "default_profit_pct": "18.5",
            "default_vat_pct": "8",
            "default_validity_days": 45,
            "default_payment_terms": "Peşin",
        },
        headers=admin,
    )
    o = await teklif(client, admin, isveren)
    rev = await revizyon(client, admin, o["id"])
    assert (D(rev["overhead_pct"]), D(rev["profit_pct"]), D(rev["vat_pct"])) == (
        D(10),
        D("18.5"),
        D(8),
    )
    assert rev["validity_days"] == 45 and rev["payment_terms"] == "Peşin"

    ezen = await teklif(
        client,
        admin,
        isveren,
        overhead_pct="5",
        profit_pct="7",
        vat_pct="1",
        validity_days=10,
        payment_terms="%50 avans",
        delivery_days=90,
        notes="Not",
        offer_date="2026-01-15",
        price_escalation="tuik",
        price_index_type="ufe",
    )
    r2 = await revizyon(client, admin, ezen["id"])
    assert (D(r2["overhead_pct"]), D(r2["profit_pct"]), D(r2["vat_pct"])) == (D(5), D(7), D(1))
    assert (r2["validity_days"], r2["payment_terms"], r2["delivery_days"]) == (10, "%50 avans", 90)
    assert r2["offer_date"] == "2026-01-15" and r2["valid_until"] == "2026-01-25"
    assert (r2["price_escalation"], r2["price_index_type"], r2["notes"]) == ("tuik", "ufe", "Not")


async def test_olustur_acik_null_odeme_kosulu_bos_kalir(client, admin, isveren) -> None:
    o = await teklif(client, admin, isveren, payment_terms=None)
    assert (await revizyon(client, admin, o["id"]))["payment_terms"] is None


async def test_olustur_olmayan_isveren_404_numara_harcanmaz(client, admin, isveren) -> None:
    resp = await client.post(
        URL, json={"employer_id": str(uuid.uuid4()), "title": "X"}, headers=admin
    )
    assert resp.status_code == 404, resp.text
    assert (await teklif(client, admin, isveren))["offer_no"] == f"TKL-{YIL}-0001"


@pytest.mark.parametrize(
    "ezme",
    [
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        {"overhead_pct": "100.01"},
        {"profit_pct": "1000"},
        {"vat_pct": "-1"},
        {"validity_days": 0},
        {"validity_days": 366},
        {"delivery_days": -1},
        {"price_escalation": "tuik"},  # endeks turu zorunlu
        {"price_escalation": "fixed", "price_index_type": "ufe"},  # sabitte endeks yok
        {"price_escalation": "bilinmeyen"},
        {"para_birimi": "USD"},
        {"offer_no": "TKL-1"},
    ],
)
async def test_olustur_gecersiz_govde_422(client, admin, isveren, ezme) -> None:
    govde = {"employer_id": str(isveren.id), "title": "T", **ezme}
    assert (await client.post(URL, json=govde, headers=admin)).status_code == 422


# ------------------------------------------------------------------------- kunye


async def test_kunye_taslakta_degisir_isveren_adi_anlik_goruntu_guncellenir(
    client, admin, isveren, db_session
) -> None:
    from app.modules.projects.models import Employer

    baska = Employer(name="Başka Yapı Ltd.")
    db_session.add(baska)
    await db_session.flush()
    o = await teklif(client, admin, isveren, scope_summary="eski")
    resp = await client.patch(
        f"{URL}/{o['id']}",
        json={"title": "Yeni iş", "scope_summary": None, "employer_id": str(baska.id)},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Yeni iş" and body["scope_summary"] is None
    assert body["employer_id"] == str(baska.id) and body["employer_name"] == "Başka Yapı Ltd."
    assert body["offer_no"] == o["offer_no"]  # numara degismez


async def test_kunye_gonderilmis_revizyonda_409_degisiklik_yok(client, admin, isveren) -> None:
    o = await teklif(client, admin, isveren)
    await durum_yap(client, admin, o["id"], "sent")
    resp = await client.patch(f"{URL}/{o['id']}", json={"title": "Yeni"}, headers=admin)
    assert resp.status_code == 409, resp.text
    assert (await detay(client, admin, o["id"]))["title"] == "A Blok Kaba İnşaat"


async def test_kunye_olmayan_isveren_404_olmayan_teklif_404_null_baslik_422(
    client, admin, isveren
) -> None:
    o = await teklif(client, admin, isveren)
    r1 = await client.patch(
        f"{URL}/{o['id']}", json={"employer_id": str(uuid.uuid4())}, headers=admin
    )
    assert r1.status_code == 404
    assert (
        await client.patch(f"{URL}/{uuid.uuid4()}", json={"title": "x"}, headers=admin)
    ).status_code == 404
    assert (
        await client.patch(f"{URL}/{o['id']}", json={"title": None}, headers=admin)
    ).status_code == 422
    assert (
        await client.patch(f"{URL}/{o['id']}", json={"offer_no": "x"}, headers=admin)
    ).status_code == 422


# ---------------------------------------------------- kosullar (revizyon PATCH)


async def test_kosullar_taslakta_degisir_gonderilmiste_409(client, admin, isveren) -> None:
    o = await teklif(client, admin, isveren)
    url = rev_url(o["id"])
    ok = await client.patch(
        url,
        json={"notes": "Not", "delivery_days": 60, "vat_pct": "10", "payment_terms": None},
        headers=admin,
    )
    assert ok.status_code == 200, ok.text
    assert (ok.json()["notes"], ok.json()["delivery_days"], ok.json()["payment_terms"]) == (
        "Not",
        60,
        None,
    )
    assert D(ok.json()["vat_pct"]) == D(10)
    await durum_yap(client, admin, o["id"], "sent")
    assert (await client.patch(url, json={"notes": "x"}, headers=admin)).status_code == 409


async def test_kosullar_fiyat_farki_tuik_endeks_zorunlu_sabite_gecince_endeks_duser(
    client, admin, isveren
) -> None:
    o = await teklif(client, admin, isveren)
    url = rev_url(o["id"])
    assert (
        await client.patch(url, json={"price_escalation": "tuik"}, headers=admin)
    ).status_code == 422
    ok = await client.patch(
        url, json={"price_escalation": "tuik", "price_index_type": "ufe"}, headers=admin
    )
    assert ok.status_code == 200 and ok.json()["price_index_type"] == "ufe"
    # tuik iken endeks turunu bosaltmak 422
    assert (
        await client.patch(url, json={"price_index_type": None}, headers=admin)
    ).status_code == 422
    # sabite gecince endeks turu KENDILIGINDEN duser (CHECK'e carpmaz)
    sabit = await client.patch(url, json={"price_escalation": "fixed"}, headers=admin)
    assert sabit.status_code == 200, sabit.text
    assert sabit.json()["price_index_type"] is None
    # sabitte endeks gondermek 422
    assert (
        await client.patch(url, json={"price_index_type": "ufe"}, headers=admin)
    ).status_code == 422


@pytest.mark.parametrize(
    "govde",
    [
        {"offer_date": None},
        {"validity_days": None},
        {"overhead_pct": None},
        {"profit_pct": None},
        {"vat_pct": None},
        {"price_escalation": None},
        {"validity_days": 0},
        {"overhead_pct": "101"},
        {"offer_unit_price": "1"},
    ],
)
async def test_kosullar_gecersiz_govde_422(client, admin, isveren, govde) -> None:
    o = await teklif(client, admin, isveren)
    assert (await client.patch(rev_url(o["id"]), json=govde, headers=admin)).status_code == 422


# ---------------------------------------------------------------- durum gecisleri

DURUMLAR = ["draft", "sent", "won", "lost", "withdrawn"]
EYLEMLER = ["send", "win", "lose", "withdraw"]
IZINLI = {
    "draft": {"send": "sent", "withdraw": "withdrawn"},
    "sent": {"win": "won", "lose": "lost", "withdraw": "withdrawn"},
    "won": {},
    "lost": {},
    "withdrawn": {},
}


@pytest.mark.parametrize("durum", DURUMLAR)
@pytest.mark.parametrize("eylem", EYLEMLER)
async def test_gecis_tablosu_TUM_ciftler(client, admin, isveren, durum, eylem) -> None:
    o = await teklif(client, admin, isveren)
    await durum_yap(client, admin, o["id"], durum)
    resp = await gecis(client, admin, o["id"], eylem)
    hedef = IZINLI[durum].get(eylem)
    son = (await detay(client, admin, o["id"]))["revisions"][0]["status"]
    if hedef is None:
        assert resp.status_code == 409, f"{durum} -> {eylem} izinsiz olmaliydi: {resp.text}"
        assert son == durum  # reddedilen gecis hicbir sey degistirmedi
    else:
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == hedef and son == hedef


async def test_taslaktan_dogrudan_kazanma_YOK_409(client, admin, isveren) -> None:
    o = await teklif(client, admin, isveren)
    resp = await gecis(client, admin, o["id"], "win")
    assert resp.status_code == 409
    assert "taslak" in resp.json()["detail"]


async def test_gecis_damgalari_ve_kullanici(client, admin, isveren) -> None:
    o = await teklif(client, admin, isveren)
    assert (await revizyon(client, admin, o["id"]))["sent_at"] is None
    await gecis(client, admin, o["id"], "send")
    r = await revizyon(client, admin, o["id"])
    assert r["sent_at"] is not None and r["won_at"] is None and r["lost_at"] is None
    await gecis(client, admin, o["id"], "win")
    r = await revizyon(client, admin, o["id"])
    assert r["status"] == "won" and r["won_at"] is not None and r["is_editable"] is False
    assert r["sent_at"] is not None  # gonderim damgasi KORUNUR


async def test_vazgecme_taslaktan_sent_at_yok_gonderilmisten_sent_at_korunur(
    client, admin, isveren
) -> None:
    a = await teklif(client, admin, isveren)
    await gecis(client, admin, a["id"], "withdraw")
    ra = await revizyon(client, admin, a["id"])
    assert (ra["status"], ra["sent_at"]) == ("withdrawn", None) and ra["withdrawn_at"] is not None
    b = await teklif(client, admin, isveren, title="B")
    await durum_yap(client, admin, b["id"], "sent")
    await gecis(client, admin, b["id"], "withdraw")
    rb = await revizyon(client, admin, b["id"])
    assert rb["status"] == "withdrawn" and rb["sent_at"] is not None


async def test_withdrawn_son_durum_yeni_revizyon_da_acilamaz(client, admin, isveren) -> None:
    o = await teklif(client, admin, isveren)
    await gecis(client, admin, o["id"], "withdraw")
    assert (await client.post(f"{URL}/{o['id']}/revisions", headers=admin)).status_code == 409


async def test_lose_govdesi_istege_bagli_alanlar(client, admin, isveren) -> None:
    o = await teklif(client, admin, isveren)
    await gecis(client, admin, o["id"], "send")
    resp = await gecis(
        client, admin, o["id"], "lose", lost_reason="  Fiyat yüksek  ", winning_amount="1234567.89"
    )
    assert resp.status_code == 200, resp.text
    r = await revizyon(client, admin, o["id"])
    assert r["status"] == "lost" and r["lost_at"] is not None
    assert r["lost_reason"] == "Fiyat yüksek" and D(r["winning_amount"]) == D("1234567.89")
    ozet = (await detay(client, admin, o["id"]))["revisions"][0]
    assert ozet["lost_reason"] == "Fiyat yüksek" and D(ozet["winning_amount"]) == D("1234567.89")


async def test_lose_govdesiz_ve_bos_govde_alanlari_bos(client, admin, isveren) -> None:
    for govde in (None, {}):
        o = await teklif(client, admin, isveren, title=f"T{govde}")
        await gecis(client, admin, o["id"], "send")
        resp = await client.post(rev_url(o["id"]) + "/lose", json=govde, headers=admin)
        assert resp.status_code == 200, resp.text
        r = await revizyon(client, admin, o["id"])
        assert (r["status"], r["lost_reason"], r["winning_amount"]) == ("lost", None, None)


@pytest.mark.parametrize(
    "govde",
    [
        {"winning_amount": "-0.01"},
        {"winning_amount": "1.001"},
        {"winning_amount": "x"},
        {"bilinmeyen": 1},
    ],
)
async def test_lose_gecersiz_govde_422_durum_degismez(client, admin, isveren, govde) -> None:
    o = await teklif(client, admin, isveren)
    await gecis(client, admin, o["id"], "send")
    assert (
        await client.post(rev_url(o["id"]) + "/lose", json=govde, headers=admin)
    ).status_code == 422
    assert (await revizyon(client, admin, o["id"]))["status"] == "sent"


async def test_gecis_yalniz_SON_revizyonda_eski_revizyon_409_olmayan_404(
    client, admin, isveren
) -> None:
    o = await teklif(client, admin, isveren)
    await durum_yap(client, admin, o["id"], "sent")
    assert (await client.post(f"{URL}/{o['id']}/revisions", headers=admin)).status_code == 201
    # Rev.0 artik son degil: sent olsa bile gecis YOK
    for eylem in ("win", "lose", "withdraw"):
        r = await gecis(client, admin, o["id"], eylem, 0)
        assert r.status_code == 409, f"{eylem}: {r.text}"
    assert (await revizyon(client, admin, o["id"], 0))["status"] == "sent"
    assert (await gecis(client, admin, o["id"], "send", 7)).status_code == 404
    assert (
        await client.post(f"{URL}/{uuid.uuid4()}/revisions/0/send", headers=admin)
    ).status_code == 404


async def test_revizyon_gecmisi_olaylari_sirali_acilis_ve_damgalar(client, admin, isveren) -> None:
    o = await teklif(client, admin, isveren)
    await gecis(client, admin, o["id"], "send")
    await gecis(client, admin, o["id"], "lose")
    await client.post(f"{URL}/{o['id']}/revisions", headers=admin)
    await gecis(client, admin, o["id"], "send", 1)
    await gecis(client, admin, o["id"], "win", 1)
    d = await detay(client, admin, o["id"])
    assert [(e["rev_no"], e["kind"]) for e in d["history"]] == [
        (0, "opened"),
        (0, "sent"),
        (0, "lost"),
        (1, "opened"),
        (1, "sent"),
        (1, "won"),
    ]
    assert all(e["user_id"] for e in d["history"])
    assert d["status"] == "won" and d["latest_rev_no"] == 1  # durum = SON revizyonun durumu
    assert [(r["rev_no"], r["status"]) for r in d["revisions"]] == [(0, "lost"), (1, "won")]


# ------------------------------------------------------- icerik yazimi taslak kapisi


async def _yazimlar(client, admin, oid: str, gid: str, kid: str, katalog_id) -> dict[str, object]:
    """Her icerik yazimi icin (ad → yanit): hepsi taslak DEGILKEN 409 olmali."""
    url = rev_url(oid)
    govde = {"catalog_item_id": str(katalog_id), "group_id": gid, "quantity": "1"}
    return {
        "kosul": await client.patch(url, json={"notes": "x"}, headers=admin),
        "grup_ekle": await client.post(url + "/groups", json={"name": "Y"}, headers=admin),
        "grup_patch": await client.patch(url + f"/groups/{gid}", json={"name": "Y"}, headers=admin),
        "grup_sil": await client.delete(url + f"/groups/{gid}", headers=admin),
        "kalem_ekle": await client.post(url + "/items", json=govde, headers=admin),
        "kalem_toplu": await client.post(
            url + "/items/bulk", json={"items": [govde]}, headers=admin
        ),
        "kalem_patch": await client.patch(
            url + f"/items/{kid}", json={"quantity": "2"}, headers=admin
        ),
        "kalem_sil": await client.delete(url + f"/items/{kid}", headers=admin),
    }


@pytest.mark.parametrize("durum", ["sent", "won", "lost", "withdrawn"])
async def test_icerik_yazimi_taslak_degilken_HEPSI_409(
    client, admin, isveren, katalog, durum
) -> None:
    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    k = await kalem(client, admin, o["id"], g["id"], katalog[0].id)
    await durum_yap(client, admin, o["id"], durum)
    yanitlar = await _yazimlar(client, admin, o["id"], g["id"], k["id"], katalog[0].id)
    assert {ad: r.status_code for ad, r in yanitlar.items()} == dict.fromkeys(yanitlar, 409)
    assert all("taslak" in r.json()["detail"] for r in yanitlar.values())
    rev = await revizyon(client, admin, o["id"])
    assert (
        len(tum_kalemler(rev)) == 1 and rev["groups"][0]["name"] == "Kaba"
    )  # hicbir sey degismedi


async def test_icerik_yazimi_son_olmayan_revizyonda_409_taslak_yeni_revizyonda_olur(
    client, admin, isveren, katalog
) -> None:
    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    k = await kalem(client, admin, o["id"], g["id"], katalog[0].id)
    await durum_yap(client, admin, o["id"], "sent")
    await client.post(f"{URL}/{o['id']}/revisions", headers=admin)
    eski = await _yazimlar(client, admin, o["id"], g["id"], k["id"], katalog[0].id)  # Rev.0'a
    assert {r.status_code for r in eski.values()} == {409}
    assert (await client.get(rev_url(o["id"], 5), headers=admin)).status_code == 404
    yeni = await revizyon(client, admin, o["id"], 1)
    assert yeni["is_editable"] is True
    ok = await client.post(rev_url(o["id"], 1) + "/groups", json={"name": "Yeni"}, headers=admin)
    assert ok.status_code == 201


# ------------------------------------------------------------------ yeni revizyon


async def _dolu_teklif(client, admin, isveren, katalog) -> tuple[dict, dict, dict, dict]:
    o = await teklif(client, admin, isveren, delivery_days=30, notes="N")
    await client.patch(
        rev_url(o["id"]),
        json={
            "overhead_pct": "9",
            "profit_pct": "11",
            "vat_pct": "10",
            "validity_days": 20,
            "payment_terms": "%30 avans",
            "price_escalation": "tuik",
            "price_index_type": "ufe",
        },
        headers=admin,
    )
    g1 = await grup(client, admin, o["id"], name="Kaba")
    g2 = await grup(client, admin, o["id"], name="İnce")
    a = await kalem(
        client,
        admin,
        o["id"],
        g1["id"],
        katalog[0].id,
        quantity="10",
        cost_unit_price="100",
        unit_mhr="2.5",
        overhead_pct="5",
    )
    b = await kalem(
        client,
        admin,
        o["id"],
        g2["id"],
        katalog[2].id,
        quantity="3",
        cost_unit_price="50",
        offer_unit_price="80",
        profit_pct="30",
    )
    c = await kalem(
        client, admin, o["id"], g2["id"], katalog[1].id, quantity="4", cost_unit_price=None
    )
    return o, g1, g2, {"a": a, "b": b, "c": c}


@pytest.mark.parametrize("durum", ["sent", "lost"])
async def test_yeni_revizyon_sent_ve_lostdan_olur_kopya_tam(
    client, admin, isveren, katalog, durum
) -> None:
    o, g1, g2, k = await _dolu_teklif(client, admin, isveren, katalog)
    await durum_yap(client, admin, o["id"], durum)
    eski_once = await revizyon(client, admin, o["id"], 0)

    resp = await client.post(f"{URL}/{o['id']}/revisions", headers=admin)
    assert resp.status_code == 201, resp.text
    yeni = resp.json()
    assert yeni["rev_no"] == 1 and yeni["status"] == "draft" and yeni["is_editable"] is True
    assert yeni["is_latest"] is True and yeni["sent_at"] is None and yeni["lost_at"] is None
    assert yeni["lost_reason"] is None and yeni["winning_amount"] is None
    assert yeni["offer_date"] == today().isoformat()  # yeni revizyon bugun

    # kosullar KOPYA
    for alan in ("overhead_pct", "profit_pct", "vat_pct"):
        assert D(yeni[alan]) == D(eski_once[alan])
    for alan in (
        "validity_days",
        "payment_terms",
        "delivery_days",
        "notes",
        "price_escalation",
        "price_index_type",
    ):
        assert yeni[alan] == eski_once[alan], alan
    assert yeni["price_index_type"] == "ufe"

    # gruplar: ayni ad/sira, YENI kimlik; kalemler karsilik gruba eslenmis
    assert [g["name"] for g in yeni["groups"]] == [g["name"] for g in eski_once["groups"]]
    assert not {g["id"] for g in yeni["groups"]} & {g["id"] for g in eski_once["groups"]}
    gruplu = {g["name"]: sorted(i["poz_no"] for i in g["items"]) for g in yeni["groups"]}
    assert gruplu == {
        "Kaba": [katalog[0].poz_no],
        "İnce": sorted([katalog[2].poz_no, katalog[1].poz_no]),
    }
    # kalem alanlari BIREBIR (fiyat/oran/adam-saat/elle B.F./hesap)
    anahtar = lambda i: i["poz_no"]  # noqa: E731
    eski_k = sorted(tum_kalemler(eski_once), key=anahtar)
    yeni_k = sorted(tum_kalemler(yeni), key=anahtar)
    assert len(yeni_k) == 3
    for e, y in zip(eski_k, yeni_k, strict=True):
        assert e["id"] != y["id"]
        for alan in ("poz_no", "description", "unit", "catalog_item_id", "sort_order", "priced"):
            assert e[alan] == y[alan], alan
        for alan in (
            "quantity",
            "unit_mhr",
            "cost_unit_price",
            "overhead_pct",
            "profit_pct",
            "offer_unit_price",
        ):
            assert (e[alan] is None) == (y[alan] is None), alan
            if e[alan] is not None:
                assert D(e[alan]) == D(y[alan]), alan
        assert e["customer"] == y["customer"] and e["internal"] == y["internal"]
    assert yeni["totals"] == eski_once["totals"]

    # eski revizyon DEGISMEDI
    eski_sonra = await revizyon(client, admin, o["id"], 0)
    assert eski_once.pop("is_latest") is True and eski_sonra.pop("is_latest") is False
    assert eski_sonra == eski_once  # son revizyon olmaktan cikmasi DISINDA aynen
    d = await detay(client, admin, o["id"])
    assert (d["status"], d["latest_rev_no"]) == ("draft", 1)  # teklif durumu = son revizyon


@pytest.mark.parametrize("durum", ["draft", "won", "withdrawn"])
async def test_yeni_revizyon_taslak_won_withdrawndan_409(client, admin, isveren, durum) -> None:
    o = await teklif(client, admin, isveren)
    await durum_yap(client, admin, o["id"], durum)
    resp = await client.post(f"{URL}/{o['id']}/revisions", headers=admin)
    assert resp.status_code == 409, resp.text
    assert (await detay(client, admin, o["id"]))["latest_rev_no"] == 0


async def test_yeni_revizyon_ardisik_rev_no_ve_ikinci_taslak_acilamaz(
    client, admin, isveren
) -> None:
    o = await teklif(client, admin, isveren)
    await durum_yap(client, admin, o["id"], "sent")
    r1 = await client.post(f"{URL}/{o['id']}/revisions", headers=admin)
    assert r1.status_code == 201 and r1.json()["rev_no"] == 1
    assert (
        await client.post(f"{URL}/{o['id']}/revisions", headers=admin)
    ).status_code == 409  # Rev.1 taslak
    await durum_yap(client, admin, o["id"], "lost", rev_no=1) if False else None
    await gecis(client, admin, o["id"], "send", 1)
    await gecis(client, admin, o["id"], "lose", 1)
    r2 = await client.post(f"{URL}/{o['id']}/revisions", headers=admin)
    assert r2.status_code == 201 and r2.json()["rev_no"] == 2
    assert (await client.post(f"{URL}/{uuid.uuid4()}/revisions", headers=admin)).status_code == 404


async def test_yeni_revizyon_kopyasi_bagimsiz_yeni_revizyon_degisince_eski_degismez(
    client, admin, isveren, katalog
) -> None:
    o, g1, g2, k = await _dolu_teklif(client, admin, isveren, katalog)
    await durum_yap(client, admin, o["id"], "sent")
    yeni = (await client.post(f"{URL}/{o['id']}/revisions", headers=admin)).json()
    yeni_kalem = tum_kalemler(yeni)[0]
    await client.patch(
        rev_url(o["id"], 1) + f"/items/{yeni_kalem['id']}", json={"quantity": "99"}, headers=admin
    )
    await client.delete(rev_url(o["id"], 1) + f"/groups/{yeni['groups'][0]['id']}", headers=admin)
    eski = await revizyon(client, admin, o["id"], 0)
    assert len(tum_kalemler(eski)) == 3
    assert {D(i["quantity"]) for i in tum_kalemler(eski)} == {D(10), D(3), D(4)}


# --------------------------------------------------------------------------- silme


async def test_sil_tek_revizyonlu_taslak_204_numara_GERI_KULLANILMAZ(
    client, admin, isveren, db_session
) -> None:
    a = await teklif(client, admin, isveren)
    b = await teklif(client, admin, isveren, title="B")
    assert (await client.delete(f"{URL}/{b['id']}", headers=admin)).status_code == 204
    assert (await client.get(f"{URL}/{b['id']}", headers=admin)).status_code == 404
    assert await db_session.scalar(select(func.count()).select_from(Offer)) == 1
    assert await db_session.scalar(select(func.count()).select_from(OfferRevision)) == 1
    c = await teklif(client, admin, isveren, title="C")
    assert (a["offer_no"], b["offer_no"], c["offer_no"]) == (
        f"TKL-{YIL}-0001",
        f"TKL-{YIL}-0002",
        f"TKL-{YIL}-0003",  # 0002 yeniden VERILMEDI
    )
    assert (await client.delete(f"{URL}/{b['id']}", headers=admin)).status_code == 404


async def test_sil_grup_ve_kalemleriyle_birlikte_gider(
    client, admin, isveren, katalog, db_session
) -> None:
    from app.modules.offers.models import OfferGroup, OfferItem

    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    await kalem(client, admin, o["id"], g["id"], katalog[0].id)
    assert (await client.delete(f"{URL}/{o['id']}", headers=admin)).status_code == 204
    assert await db_session.scalar(select(func.count()).select_from(OfferGroup)) == 0
    assert await db_session.scalar(select(func.count()).select_from(OfferItem)) == 0


@pytest.mark.parametrize("durum", ["sent", "won", "lost", "withdrawn"])
async def test_sil_taslak_degilse_409(client, admin, isveren, durum) -> None:
    o = await teklif(client, admin, isveren)
    await durum_yap(client, admin, o["id"], durum)
    assert (await client.delete(f"{URL}/{o['id']}", headers=admin)).status_code == 409
    assert (await client.get(f"{URL}/{o['id']}", headers=admin)).status_code == 200


async def test_sil_birden_cok_revizyonlu_409_taslak_olsa_bile(client, admin, isveren) -> None:
    o = await teklif(client, admin, isveren)
    await durum_yap(client, admin, o["id"], "sent")
    await client.post(f"{URL}/{o['id']}/revisions", headers=admin)  # Rev.1 taslak
    assert (await detay(client, admin, o["id"]))["status"] == "draft"
    assert (await client.delete(f"{URL}/{o['id']}", headers=admin)).status_code == 409


# -------------------------------------------------------------------------- denetim


async def test_denetim_satirlari_olustur_kunye_sil_revizyon_gecis_kosul(
    client, admin, isveren, db_session
) -> None:
    o = await teklif(client, admin, isveren)
    no = o["offer_no"]
    await client.patch(f"{URL}/{o['id']}", json={"title": "Yeni ad"}, headers=admin)
    await client.patch(rev_url(o["id"]), json={"notes": "x"}, headers=admin)
    await gecis(client, admin, o["id"], "send")
    await client.post(f"{URL}/{o['id']}/revisions", headers=admin)
    await gecis(client, admin, o["id"], "withdraw", 1)
    b = await teklif(client, admin, isveren, title="Silinecek")
    await client.delete(f"{URL}/{b['id']}", headers=admin)

    assert await _audit_details(db_session, AuditAction.create) == [
        f"Teklif oluşturuldu: {no} · A Blok Kaba İnşaat (Akın İnşaat A.Ş.)",
        f"Teklif yeni revizyon açıldı: {no} Rev.1",
        f"Teklif oluşturuldu: {b['offer_no']} · Silinecek (Akın İnşaat A.Ş.)",
    ]
    assert await _audit_details(db_session, AuditAction.update) == [
        f"Teklif künyesi güncellendi: {no} · Yeni ad (Akın İnşaat A.Ş.)",
        f"Teklif koşulları güncellendi: {no} Rev.0",
        f"Teklif gönderildi: {no} Rev.0",
        f"Teklif vazgeçildi: {no} Rev.1",
    ]
    assert await _audit_details(db_session, AuditAction.delete) == [
        f"Teklif silindi: {b['offer_no']} · Silinecek"
    ]


async def test_denetim_reddedilen_islem_satir_yazmaz(client, admin, isveren, db_session) -> None:
    o = await teklif(client, admin, isveren)
    onceki = len(await _audit_details(db_session, AuditAction.update))
    assert (await gecis(client, admin, o["id"], "win")).status_code == 409
    assert (
        await client.patch(
            f"{URL}/{o['id']}", json={"employer_id": str(uuid.uuid4())}, headers=admin
        )
    ).status_code == 404
    assert len(await _audit_details(db_session, AuditAction.update)) == onceki


# ------------------------------------------------------------------------ yol sirasi


async def test_offers_settings_literal_yol_offer_id_parametresinden_ONCE(client, admin) -> None:
    resp = await client.get(f"{URL}/settings", headers=admin)
    assert resp.status_code == 200, resp.text
    assert "default_overhead_pct" in resp.json()
    from app.modules.offers.router import router

    paths = [getattr(r, "path", "") for r in router.routes]
    assert paths.index("/offers/settings") < paths.index("/offers/{offer_id}")

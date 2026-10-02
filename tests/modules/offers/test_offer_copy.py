"""TKL-B5.1 / SO-8 — mevcut tekliften kopyala (`POST /offers` + `copy_from`).

YENI teklif (yeni numara) Rev.0 draft; kaynak revizyonun kosullari + oranlari + gruplari + kalemleri
(miktar, maliyet, g/k, ELLE B.F., adam-saat dahil) birebir; kunye govde ?? kaynak; kaynak
teklif/revizyon DEGISMEZ; `lost_*`/durum damgalari kopyalanmaz.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.timezone import today
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.projects.models import Employer

from .._boq import _audit_details
from ._offers import URL, D, detay, gecis, grup, kalem, rev_url, revizyon, teklif, tum_kalemler

KOSULLAR = {
    "offer_date": "2026-01-15",
    "validity_days": 45,
    "overhead_pct": "9.00",
    "profit_pct": "21.50",
    "vat_pct": "18.00",
    "payment_terms": "Peşin %30, kalan hakedişle",
    "delivery_days": 120,
    "price_escalation": "tuik",
    "price_index_type": "ufe",
    "notes": "Kaynak notu",
}


@pytest.fixture
async def kaynak(client, admin, isveren, katalog) -> dict:
    """Kaynak: Rev.0 KAYBEDILMIS (neden + kazanan tutar dolu), Rev.1 taslak (kopya + degisiklik)."""
    o = await teklif(client, admin, isveren, title="Kaynak İş", scope_summary="Kapsam özeti")
    patch = await client.patch(rev_url(o["id"]), json=KOSULLAR, headers=admin)
    assert patch.status_code == 200, patch.text
    g1 = await grup(client, admin, o["id"], name="Kaba")
    g2 = await grup(client, admin, o["id"], name="İnce")
    await kalem(
        client, admin, o["id"], g1["id"], katalog[0].id,
        quantity="10", cost_unit_price="100", overhead_pct="5", profit_pct="30", unit_mhr="9.5",
    )  # fmt: skip
    await kalem(
        client, admin, o["id"], g1["id"], katalog[2].id,
        quantity="3", cost_unit_price="50", offer_unit_price="80",
    )  # fmt: skip
    await kalem(client, admin, o["id"], g2["id"], katalog[1].id, quantity="7", cost_unit_price=None)
    await gecis(client, admin, o["id"], "send")
    lost = await gecis(client, admin, o["id"], "lose", lost_reason="Pahalı", winning_amount="900")
    assert lost.status_code == 200, lost.text
    return o


def _kalem_ozeti(rev: dict) -> list[tuple]:
    return [
        (
            g["name"], k["poz_no"], k["description"], k["unit"], k["quantity"], k["unit_mhr"],
            k["cost_unit_price"], k["overhead_pct"], k["profit_pct"], k["offer_unit_price"],
            k["sort_order"],
        )
        for g in rev["groups"]
        for k in g["items"]
    ]  # fmt: skip


async def _kopya(client, admin, kaynak_id: str, rev_no: int = 0, **over) -> dict:
    govde = {"copy_from": {"offer_id": kaynak_id, "rev_no": rev_no}, **over}
    resp = await client.post(URL, json=govde, headers=admin)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_kopya_yeni_numara_kunye_kaynaktan_Rev0_taslak(client, admin, kaynak) -> None:
    k = await _kopya(client, admin, kaynak["id"])
    assert k["id"] != kaynak["id"] and k["offer_no"] != kaynak["offer_no"]
    assert (k["title"], k["employer_id"], k["employer_name"]) == (
        "Kaynak İş", kaynak["employer_id"], kaynak["employer_name"]
    )  # fmt: skip
    assert k["scope_summary"] == "Kapsam özeti"
    assert k["status"] == "draft" and k["latest_rev_no"] == 0 and len(k["revisions"]) == 1
    assert k["template_id"] is None
    rev = k["revisions"][0]
    assert rev["rev_no"] == 0 and rev["sent_at"] is None and rev["lost_at"] is None
    assert rev["lost_reason"] is None and rev["winning_amount"] is None and rev["won_at"] is None
    assert [e["kind"] for e in k["history"]] == ["opened"]  # kaynagin send/lose damgalari YOK


async def test_kopya_kosullar_oranlar_gruplar_kalemler_BIREBIR_hesap_ayni(
    client, admin, kaynak
) -> None:
    k = await _kopya(client, admin, kaynak["id"])
    asil = await revizyon(client, admin, kaynak["id"])
    kopya = await revizyon(client, admin, k["id"])
    for alan in (
        "validity_days", "overhead_pct", "profit_pct", "vat_pct", "payment_terms",
        "delivery_days", "price_escalation", "price_index_type", "notes",
    ):  # fmt: skip
        assert kopya[alan] == asil[alan], alan
    assert kopya["offer_date"] == today().isoformat()  # yeni teklif bugunku tarihle
    assert kopya["status"] == "draft" and kopya["is_editable"] is True
    assert _kalem_ozeti(kopya) == _kalem_ozeti(asil)
    assert kopya["totals"] == asil["totals"]
    assert {g["id"] for g in kopya["groups"]}.isdisjoint({g["id"] for g in asil["groups"]})
    assert {k_["id"] for k_ in tum_kalemler(kopya)}.isdisjoint(
        {k_["id"] for k_ in tum_kalemler(asil)}
    )
    # elle B.F. ve kalem oranlari ve adam-saat KOPYALANDI (S5 mutasyon hedefi)
    elle = next(i for i in tum_kalemler(kopya) if i["offer_unit_price"] is not None)
    assert D(elle["offer_unit_price"]) == D("80.00") and D(elle["cost_unit_price"]) == D("50")
    beton = tum_kalemler(kopya)[0]
    assert (D(beton["overhead_pct"]), D(beton["profit_pct"]), D(beton["unit_mhr"])) == (
        D("5"), D("30"), D("9.5")
    )  # fmt: skip
    assert tum_kalemler(kopya)[-1]["cost_unit_price"] is None  # fiyatsiz kalem fiyatsiz kopyalandi


async def test_kaynak_teklif_ve_revizyonlari_DEGISMEZ(client, admin, kaynak) -> None:
    once_detay = await detay(client, admin, kaynak["id"])
    once = [await revizyon(client, admin, kaynak["id"], n) for n in (0,)]
    await _kopya(client, admin, kaynak["id"])
    await _kopya(client, admin, kaynak["id"], title="İkinci kopya")
    assert await detay(client, admin, kaynak["id"]) == once_detay
    assert [await revizyon(client, admin, kaynak["id"], n) for n in (0,)] == once
    assert once_detay["status"] == "lost"  # kayip damgalari yerinde


async def test_govde_kunye_ve_kosullari_kaynagi_EZER(client, admin, kaynak, db_session) -> None:
    diger = Employer(name="Başka İşveren A.Ş.")
    db_session.add(diger)
    await db_session.flush()
    k = await _kopya(
        client, admin, kaynak["id"],
        employer_id=str(diger.id), title="Yeni İş", scope_summary="Yeni kapsam",
        validity_days=10, payment_terms=None, notes="Yeni not", offer_date="2026-12-01",
        price_escalation="fixed",
    )  # fmt: skip
    assert (k["employer_name"], k["title"], k["scope_summary"]) == (
        "Başka İşveren A.Ş.", "Yeni İş", "Yeni kapsam"
    )  # fmt: skip
    rev = await revizyon(client, admin, k["id"])
    assert rev["validity_days"] == 10 and rev["payment_terms"] is None
    assert rev["notes"] == "Yeni not" and rev["offer_date"] == "2026-12-01"
    assert rev["price_escalation"] == "fixed" and rev["price_index_type"] is None
    assert D(rev["overhead_pct"]) == D("9") and rev["delivery_days"] == 120  # verilmeyen: kaynak


async def test_baska_revizyondan_kopya_ve_gonderilmis_son_olmayan_revizyon(
    client, admin, kaynak, katalog
) -> None:
    r1 = (await client.post(f"{URL}/{kaynak['id']}/revisions", headers=admin)).json()
    item = r1["groups"][0]["items"][0]
    ok = await client.patch(
        rev_url(kaynak["id"], 1) + f"/items/{item['id']}", json={"quantity": "99"}, headers=admin
    )
    assert ok.status_code == 200, ok.text
    k0 = await _kopya(client, admin, kaynak["id"], 0)
    k1 = await _kopya(client, admin, kaynak["id"], 1)
    q0 = tum_kalemler(await revizyon(client, admin, k0["id"]))[0]["quantity"]
    q1 = tum_kalemler(await revizyon(client, admin, k1["id"]))[0]["quantity"]
    assert (D(q0), D(q1)) == (D("10"), D("99"))  # her biri kendi revizyonunu kopyaladi


async def test_miktarsiz_kalem_NULL_olarak_kopyalanir(client, admin, isveren, katalog) -> None:
    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    resp = await client.post(
        rev_url(o["id"]) + "/items",
        json={"catalog_item_id": str(katalog[0].id), "group_id": g["id"]},
        headers=admin,
    )
    assert resp.status_code == 201 and resp.json()["quantity"] is None
    k = await _kopya(client, admin, o["id"])
    rev = await revizyon(client, admin, k["id"])
    assert tum_kalemler(rev)[0]["quantity"] is None
    assert rev["totals"]["unquantified_count"] == 1


async def test_kaynak_yok_404_revizyon_yok_404_ikisi_de_teklif_YAZMAZ(
    client, admin, kaynak
) -> None:
    for offer_id, rev_no in ((str(uuid.uuid4()), 0), (kaynak["id"], 9)):
        resp = await client.post(
            URL, json={"copy_from": {"offer_id": offer_id, "rev_no": rev_no}}, headers=admin
        )
        assert resp.status_code == 404, resp.text
    assert (await client.get(URL, headers=admin)).json()["total"] == 1  # yalniz kaynak


async def test_copy_from_gecersiz_govde_422(client, admin, kaynak) -> None:
    for kotu in (
        {"offer_id": kaynak["id"]},
        {"offer_id": kaynak["id"], "rev_no": -1},
        {"offer_id": kaynak["id"], "rev_no": 0, "x": 1},
    ):
        resp = await client.post(URL, json={"copy_from": kotu}, headers=admin)
        assert resp.status_code == 422, (kotu, resp.text)


async def test_denetim_kopyadan_olusturma_metni(client, admin, kaynak, db_session) -> None:
    k = await _kopya(client, admin, kaynak["id"])
    kayitlar = await _audit_details(db_session, AuditAction.create)
    assert (
        messages.offer_created_from_copy(
            k["offer_no"], k["title"], k["employer_name"], kaynak["offer_no"], 0
        )
        in kayitlar
    )


async def test_SO23_sablondan_olusan_teklifin_kopyasi_template_id_TASIMAZ_kullanim_ARTMAZ(
    client, admin, isveren, katalog
) -> None:
    """Kopya sablondan DEGIL kaynaktan dogar: `template_id` bos, sablonun kullanim sayisi sabit."""
    from ._offers import TPL, sablon, sablon_icerik

    sab = await sablon(client, admin, "Villa")
    await sablon_icerik(client, admin, sab["id"], [("Kaba", [katalog[0].id])])
    resp = await client.post(
        URL,
        json={"employer_id": str(isveren.id), "title": "Şablonlu", "template_id": sab["id"]},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    kaynak_teklif = resp.json()
    assert kaynak_teklif["template_id"] == sab["id"]
    once = (await client.get(f"{TPL}/{sab['id']}", headers=admin)).json()["usage_count"]
    assert once == 1

    kopya = await _kopya(client, admin, kaynak_teklif["id"])
    assert kopya["template_id"] is None
    assert (await detay(client, admin, kopya["id"]))["template_id"] is None
    sonra = (await client.get(f"{TPL}/{sab['id']}", headers=admin)).json()["usage_count"]
    assert sonra == once

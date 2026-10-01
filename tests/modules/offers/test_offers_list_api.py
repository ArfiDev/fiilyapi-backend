"""TKL-B4.2 — `GET /offers`: filtreler, durum ozet kartlari, kazanma orani, suresi gecmis adedi,
tarih araligi, sayfalama, siralama ve N+1 bekcisi.

Her fikstur teklifi TEK kalemdir: maliyet 100,00 x 1 → B.F. 128,80 (GG %12, kar %15) →
net 128,80 · KDV %20 = 25,76 · brut 154,56 (ELLE hesaplanmis).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import event, text

from app.core.timezone import today
from app.modules.offers import numbering
from app.modules.projects.models import Employer

from ._offers import URL, D, durum_yap, gecis, grup, kalem, teklif

YIL = numbering.current_offer_year()


async def _mk(
    client, admin, isveren, katalog, title="T", durum="draft", maliyet="100", ikinci=False, **over
) -> dict:
    o = await teklif(client, admin, isveren, title=title, **over)
    g = await grup(client, admin, o["id"])
    await kalem(client, admin, o["id"], g["id"], katalog[0].id, cost_unit_price=maliyet)
    if ikinci:  # ikinci grup + kalem (durum gecisinden ONCE: gonderilince yazilamaz)
        g2 = await grup(client, admin, o["id"], name="Ince")
        await kalem(client, admin, o["id"], g2["id"], katalog[2].id)
    await durum_yap(client, admin, o["id"], durum)
    return o


async def _liste(client, admin, **params) -> dict:
    resp = await client.get(URL, params=params, headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _no(satir: dict) -> str:
    return satir["offer_no"]


# ------------------------------------------------------------------------ alanlar


async def test_liste_satir_alanlari_ve_tutarlar(client, admin, isveren, katalog) -> None:
    o = await _mk(
        client, admin, isveren, katalog, title="A Blok", scope_summary="Kaba", durum="sent"
    )
    govde = await _liste(client, admin)
    assert govde["total"] == 1 and govde["limit"] == 50 and govde["offset"] == 0
    [s] = govde["items"]
    assert s["id"] == o["id"] and s["offer_no"] == o["offer_no"] and s["rev_no"] == 0
    assert s["title"] == "A Blok" and s["scope_summary"] == "Kaba"
    assert s["employer_id"] == str(isveren.id) and s["employer_name"] == "Akın İnşaat A.Ş."
    assert s["status"] == "sent" and s["unpriced_count"] == 0
    assert (D(s["net"]), D(s["gross"])) == (D("128.80"), D("154.56"))
    assert s["offer_date"] == today().isoformat()
    assert s["valid_until"] == (today() + timedelta(days=30)).isoformat()


async def test_liste_bos_iken_sifirlar_ve_win_rate_None(client, admin) -> None:
    govde = await _liste(client, admin)
    assert govde["items"] == [] and govde["total"] == 0
    assert govde["summary"]["win_rate"] is None and govde["summary"]["expired_count"] == 0
    assert [(c["status"], c["count"], D(c["net"])) for c in govde["summary"]["by_status"]] == [
        (s, 0, D(0)) for s in ("draft", "sent", "won", "lost", "withdrawn")
    ]


async def test_liste_son_revizyonun_durumu_ve_tutari_gosterilir(
    client, admin, isveren, katalog
) -> None:
    o = await _mk(client, admin, isveren, katalog, durum="lost")
    await client.post(f"{URL}/{o['id']}/revisions", headers=admin)
    [s] = (await _liste(client, admin))["items"]
    assert (s["rev_no"], s["status"]) == (1, "draft")
    assert D(s["net"]) == D("128.80")  # kopya kalem


# ---------------------------------------------------------------------- filtreler


async def test_filtre_status_ve_ozet_status_filtresinden_BAGIMSIZ(
    client, admin, isveren, katalog
) -> None:
    await _mk(client, admin, isveren, katalog, "D", "draft")
    await _mk(client, admin, isveren, katalog, "S1", "sent")
    await _mk(client, admin, isveren, katalog, "S2", "sent")
    await _mk(client, admin, isveren, katalog, "W", "won")
    govde = await _liste(client, admin, status="sent")
    assert govde["total"] == 2 and {s["title"] for s in govde["items"]} == {"S1", "S2"}
    kartlar = {c["status"]: c["count"] for c in govde["summary"]["by_status"]}
    assert kartlar == {"draft": 1, "sent": 2, "won": 1, "lost": 0, "withdrawn": 0}  # dagilim sabit
    assert (
        await client.get(URL, params={"status": "bilinmeyen"}, headers=admin)
    ).status_code == 422


async def test_filtre_q_no_is_adi_isveren_adi(client, admin, isveren, katalog, db_session) -> None:
    baska = Employer(name="Zeta Yapı")
    db_session.add(baska)
    await db_session.flush()
    a = await _mk(client, admin, isveren, katalog, "Okul binası")
    ob = await teklif(client, admin, baska, title="Hastane")
    assert {s["id"] for s in (await _liste(client, admin, q="okul"))["items"]} == {
        a["id"]
    }  # is adi, kucuk harf
    assert {s["id"] for s in (await _liste(client, admin, q="ZETA"))["items"]} == {
        ob["id"]
    }  # isveren
    assert {s["id"] for s in (await _liste(client, admin, q="0002"))["items"]} == {ob["id"]}  # no
    assert {s["id"] for s in (await _liste(client, admin, q=a["offer_no"]))["items"]} == {a["id"]}
    assert (await _liste(client, admin, q="yok boyle"))["items"] == []


async def test_filtre_q_LIKE_joker_degil_kacirilir(client, admin, isveren, katalog) -> None:
    yuzde = await _mk(client, admin, isveren, katalog, "100% Kaba")
    alt = await _mk(client, admin, isveren, katalog, "A_B blok")
    await _mk(client, admin, isveren, katalog, "Düz")
    assert [s["id"] for s in (await _liste(client, admin, q="%"))["items"]] == [yuzde["id"]]
    assert [s["id"] for s in (await _liste(client, admin, q="_"))["items"]] == [alt["id"]]
    assert (await _liste(client, admin, q="\\"))["items"] == []


async def test_filtre_isveren(client, admin, isveren, katalog, db_session) -> None:
    baska = Employer(name="Zeta Yapı")
    db_session.add(baska)
    await db_session.flush()
    a = await _mk(client, admin, isveren, katalog, "A")
    b = await teklif(client, admin, baska, title="B")
    assert [s["id"] for s in (await _liste(client, admin, employer_id=str(baska.id)))["items"]] == [
        b["id"]
    ]
    assert [
        s["id"] for s in (await _liste(client, admin, employer_id=str(isveren.id)))["items"]
    ] == [a["id"]]


async def test_filtre_tarih_araligi_dahil_dahil_son_revizyonun_teklif_tarihi(
    client, admin, isveren, katalog
) -> None:
    for gun, ad in ((10, "on"), (20, "yirmi"), (30, "otuz")):
        await _mk(client, admin, isveren, katalog, ad, offer_date=f"2026-03-{gun:02d}")

    async def b(**p) -> list[str]:
        return sorted(s["title"] for s in (await _liste(client, admin, **p))["items"])

    assert await b(offer_date_from="2026-03-20", offer_date_to="2026-03-20") == [
        "yirmi"
    ]  # dahil-dahil
    assert await b(offer_date_from="2026-03-20") == ["otuz", "yirmi"]
    assert await b(offer_date_to="2026-03-20") == ["on", "yirmi"]
    assert await b(offer_date_from="2026-03-11", offer_date_to="2026-03-29") == ["yirmi"]
    assert await b(offer_date_from="2026-04-01") == []
    ters = await client.get(
        URL, params={"offer_date_from": "2026-03-21", "offer_date_to": "2026-03-20"}, headers=admin
    )
    assert ters.status_code == 422, ters.text
    assert (
        await client.get(URL, params={"offer_date_from": "x"}, headers=admin)
    ).status_code == 422


async def test_tarih_filtresi_ozet_kartlarina_da_uygulanir(client, admin, isveren, katalog) -> None:
    await _mk(client, admin, isveren, katalog, "Eski", "won", offer_date="2026-01-05")
    await _mk(client, admin, isveren, katalog, "Yeni", "lost", offer_date="2026-03-05")
    govde = await _liste(client, admin, offer_date_from="2026-03-01")
    assert {c["status"]: c["count"] for c in govde["summary"]["by_status"]}["won"] == 0
    assert govde["summary"]["win_rate"] == "0.00"  # 0 kazanilan / 1 kaybedilen


# --------------------------------------------------------------------------- ozet


async def test_ozet_durum_kartlari_net_toplam_ve_kazanma_orani(
    client, admin, isveren, katalog
) -> None:
    await _mk(client, admin, isveren, katalog, "d1", "draft")
    await _mk(client, admin, isveren, katalog, "d2", "draft", maliyet="200")  # net 257,60
    await _mk(client, admin, isveren, katalog, "s1", "sent")
    for i in range(2):
        await _mk(client, admin, isveren, katalog, f"w{i}", "won")
    await _mk(client, admin, isveren, katalog, "l1", "lost")
    await _mk(client, admin, isveren, katalog, "x1", "withdrawn")
    ozet = (await _liste(client, admin))["summary"]
    kartlar = {c["status"]: (c["count"], D(c["net"])) for c in ozet["by_status"]}
    assert kartlar == {
        "draft": (2, D("386.40")),  # 128,80 + 257,60
        "sent": (1, D("128.80")),
        "won": (2, D("257.60")),
        "lost": (1, D("128.80")),
        "withdrawn": (1, D("128.80")),
    }
    assert D(ozet["win_rate"]) == D("66.67")  # 2 / (2 + 1) (vazgecilenler PAYDADA yok)


async def test_ozet_q_filtresine_uyar(client, admin, isveren, katalog) -> None:
    await _mk(client, admin, isveren, katalog, "Okul", "won")
    await _mk(client, admin, isveren, katalog, "Park", "lost")
    ozet = (await _liste(client, admin, q="okul"))["summary"]
    assert D(ozet["win_rate"]) == D("100.00")


async def test_ozet_suresi_gecmis_adedi_sinir_gunu_ve_yalniz_sent(
    client, admin, isveren, katalog
) -> None:
    bugun = today()

    def tarih(gecen_gun: int) -> str:
        return (bugun - timedelta(days=gecen_gun)).isoformat()

    # valid_until = teklif tarihi + 30 gun
    await _mk(
        client, admin, isveren, katalog, "dun-bitti", "sent", offer_date=tarih(31)
    )  # bitis dun → GECTI
    await _mk(
        client, admin, isveren, katalog, "bugun-biter", "sent", offer_date=tarih(30)
    )  # bitis BUGUN → gecmedi
    await _mk(client, admin, isveren, katalog, "yarin", "sent", offer_date=tarih(29))
    await _mk(
        client, admin, isveren, katalog, "taslak", "draft", offer_date=tarih(90)
    )  # sent degil
    await _mk(client, admin, isveren, katalog, "kazanildi", "won", offer_date=tarih(90))
    await _mk(client, admin, isveren, katalog, "eski-gecti", "sent", offer_date=tarih(400))
    ozet = (await _liste(client, admin))["summary"]
    assert ozet["expired_count"] == 2  # dun-bitti + eski-gecti; sinir gunu (bugun) GECMEMIS
    satirlar = {s["title"]: s["valid_until"] for s in (await _liste(client, admin))["items"]}
    assert satirlar["bugun-biter"] == bugun.isoformat()


# ------------------------------------------------------------- sayfalama, siralama


async def test_sayfalama_limit_offset_toplam(client, admin, isveren, katalog) -> None:
    for i in range(5):
        await _mk(client, admin, isveren, katalog, f"T{i}")
    s1 = await _liste(client, admin, limit=2, offset=0)
    s2 = await _liste(client, admin, limit=2, offset=2)
    s3 = await _liste(client, admin, limit=2, offset=4)
    assert (s1["total"], len(s1["items"]), len(s2["items"]), len(s3["items"])) == (5, 2, 2, 1)
    assert [x["title"] for s in (s1, s2, s3) for x in s["items"]] == ["T4", "T3", "T2", "T1", "T0"]
    assert s1["summary"] == s3["summary"]  # kartlar sayfadan bagimsiz
    for kotu in ({"limit": 0}, {"limit": 201}, {"offset": -1}):
        assert (await client.get(URL, params=kotu, headers=admin)).status_code == 422


async def test_E6_siralama_offer_no_METNE_gore_degil_yil_sira_sayisal(
    client, admin, isveren, katalog, db_session
) -> None:
    """Sayac 9998'den baslar: 9999 ve 10000 olusur. Metne gore `TKL-2026-10000` < `TKL-2026-9999`
    olurdu ve en yeni teklif listenin ALTINA duserdi."""
    await db_session.execute(
        text("INSERT INTO offer_counters (year, last_no) VALUES (:y, 9998)"), {"y": YIL}
    )
    a = await _mk(client, admin, isveren, katalog, "A")
    b = await _mk(client, admin, isveren, katalog, "B")
    assert (a["offer_no"], b["offer_no"]) == (f"TKL-{YIL}-9999", f"TKL-{YIL}-10000")
    assert [_no(s) for s in (await _liste(client, admin))["items"]] == [
        b["offer_no"],
        a["offer_no"],
    ]
    # onceki yilin teklifi (elle) ne kadar buyuk sayida olursa olsun ALTTA kalir
    await db_session.execute(
        text(
            "INSERT INTO offers (id, offer_no, employer_id, employer_name, title) "
            "VALUES (gen_random_uuid(), :no, :eid, 'x', 'Gecen yil')"
        ),
        {"no": f"TKL-{YIL - 1}-99999", "eid": isveren.id},
    )
    await db_session.execute(
        text(
            "INSERT INTO offer_revisions (id, offer_id, rev_no, offer_date, overhead_pct, "
            "profit_pct, vat_pct) SELECT gen_random_uuid(), id, 0, '2025-01-01', 12, 15, 20 "
            "FROM offers WHERE title = 'Gecen yil'"
        )
    )
    siralar = [_no(s) for s in (await _liste(client, admin))["items"]]
    assert siralar == [b["offer_no"], a["offer_no"], f"TKL-{YIL - 1}-99999"]


# ------------------------------------------------------------------------- N+1


@contextmanager
def _sayac():
    from tests.conftest import test_engine

    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)


async def test_N1_liste_sorgu_sayisi_teklif_sayisindan_bagimsiz(
    client, admin, isveren, katalog
) -> None:
    await _mk(client, admin, isveren, katalog, "ilk")
    with _sayac() as bir:
        assert (await client.get(URL, headers=admin)).status_code == 200
    for i in range(5):  # toplam 6 teklif, 2 kalemli, farkli durumlarda, bir kismi 2 revizyonlu
        durum = ("sent", "lost", "won", "draft", "lost")[i]
        o = await _mk(client, admin, isveren, katalog, f"T{i}", durum, ikinci=True)
        if durum in ("sent", "lost"):
            await client.post(f"{URL}/{o['id']}/revisions", headers=admin)
    with _sayac() as alti:
        resp = await client.get(URL, headers=admin)
    assert resp.status_code == 200 and resp.json()["total"] == 6
    assert len(alti) == len(bir), f"N+1: 1 teklifte {len(bir)}, 6 teklifte {len(alti)} sorgu"
    # ve liste sorgulari: teklif+son revizyon JOIN'i + kalemler (+ oturum/izin sorgulari sabit)
    assert sum("FROM offer_items" in s for s in alti) == 1
    assert sum("offer_revisions" in s for s in alti) == 2  # SABIT: JOIN'li liste + kalem ic sorgusu


async def test_gecis_sonrasi_liste_guncel(client, admin, isveren, katalog) -> None:
    o = await _mk(client, admin, isveren, katalog, "G", "draft")
    await gecis(client, admin, o["id"], "send")
    assert (await _liste(client, admin))["items"][0]["status"] == "sent"

"""DSC-B3 Σ: budanmış raporlar TOPLAMSALDIR — civil + elek + atamasız.d:none == atamasız
(Decimal ==); iki_disiplin == civil + elek. Oranlar: kısıtlı satır == atamasız aynı satır."""

from __future__ import annotations

from decimal import Decimal

from httpx import AsyncClient

from tests._disiplin_dunyasi import Dunya
from tests.discipline_scope._b3_yardim import (
    GUN_KILITLI,
    GUN_TASLAK,
    bul,
    gunluk,
    haftalik,
    panel,
    sayi,
    toplam,
)

_PANEL_TARIH = "2026-05-06"


def _sigma(alan_tam: object, civil: object, elek: object, yok: object, iki: object) -> None:
    assert toplam(civil, elek, yok) == sayi(alan_tam)
    assert sayi(iki) == toplam(civil, elek)


async def _dort(client: AsyncClient, d: Dunya, cek) -> dict[str, dict]:
    return {ad: await cek(client, d, ad) for ad in ("atamasiz", "civil", "elek", "iki_disiplin")}


async def test_sigma_panel_kpi_ve_genel_satir(client: AsyncClient, dunya_b3: Dunya) -> None:
    async def cek(c, d, ad):
        return await panel(c, d, ad, _PANEL_TARIH)

    p = await _dort(client, dunya_b3, cek)
    yok = bul(p["atamasiz"]["rows"], node_id="d:none")
    for alan in ("budget_mhr", "earned_cum", "earned_day", "spent_day"):
        _sigma(
            p["atamasiz"]["kpi"][alan],
            p["civil"]["kpi"][alan],
            p["elek"]["kpi"][alan],
            yok.get(alan, 0),
            p["iki_disiplin"]["kpi"][alan],
        )
    genel = {ad: bul(r["rows"], scope="overall") for ad, r in p.items()}
    for alan in ("budget_mhr", "earned_cum", "spent_cum"):
        _sigma(
            genel["atamasiz"][alan],
            genel["civil"][alan],
            genel["elek"][alan],
            yok[alan],
            genel["iki_disiplin"][alan],
        )
    # oranlar: kendi Σ'larından yeniden hesap (PF = kazanılan ÷ harcanan)
    for ad in ("civil", "elek", "iki_disiplin"):
        g = genel[ad]
        assert sayi(g["earned_cum"]) / sayi(g["spent_cum"]) == sayi(g["pf_cum"]) or (
            round(sayi(g["earned_cum"]) / sayi(g["spent_cum"]), 2) == round(sayi(g["pf_cum"]), 2)
        )


async def test_sigma_panel_cubuklar_gun_gun(client: AsyncClient, dunya_b3: Dunya) -> None:
    async def cek(c, d, ad):
        return await panel(c, d, ad, _PANEL_TARIH)

    p = await _dort(client, dunya_b3, cek)
    n = len(p["atamasiz"]["bars"])
    assert n and all(len(r["bars"]) == n for r in p.values())
    for i in range(n):
        for alan in ("earned_day", "spent_day"):
            _sigma(
                p["atamasiz"]["bars"][i][alan],
                p["civil"]["bars"][i][alan],
                p["elek"]["bars"][i][alan],
                0,
                p["iki_disiplin"]["bars"][i][alan],
            )
            assert p["civil"]["bars"][i]["day"] == p["atamasiz"]["bars"][i]["day"]


async def test_kisitli_satir_atamasiz_ayni_satir_ve_esdeger_kisi(
    client: AsyncClient, dunya_b3: Dunya
) -> None:
    tam = await panel(client, dunya_b3, "atamasiz", _PANEL_TARIH)
    civil = await panel(client, dunya_b3, "civil", _PANEL_TARIH)
    elek = await panel(client, dunya_b3, "elek", _PANEL_TARIH)
    assert bul(civil["rows"], node_id="d:" + str(dunya_b3.kab.id)) == bul(
        tam["rows"], node_id="d:" + str(dunya_b3.kab.id)
    )
    assert bul(elek["rows"], node_id="d:" + str(dunya_b3.duv.id)) == bul(
        tam["rows"], node_id="d:" + str(dunya_b3.duv.id)
    )
    # S2: kısıtlıda kişi SAYIMI site geneli olurdu → eşdeğer kişi
    assert tam["actual_basis"] == "headcount"
    assert civil["actual_basis"] == elek["actual_basis"] == "equivalent"
    assert {r["node_id"] for r in civil["rows"] if r["scope"] == "discipline"} == {
        "d:" + str(dunya_b3.kab.id)
    }
    assert [d["id"] for d in elek["disciplines"]] == ["d:" + str(dunya_b3.duv.id)]


async def test_sigma_gunluk_canli_kpi_ve_altbilgi(client: AsyncClient, dunya_b3: Dunya) -> None:
    for gun in (GUN_KILITLI, GUN_TASLAK):

        async def cek(c, d, ad, gun=gun):
            return await gunluk(c, d, ad, gun)

        g = await _dort(client, dunya_b3, cek)
        yok = bul(g["atamasiz"]["kpis"], node_id="d:none")
        satir = {ad: bul(r["kpis"], kind="overall") for ad, r in g.items()}
        for alan in ("budget_mhr", "earned_day", "earned_cum", "spent_day", "spent_cum"):
            _sigma(
                satir["atamasiz"][alan],
                satir["civil"][alan],
                satir["elek"][alan],
                yok[alan],
                satir["iki_disiplin"][alan],
            )
        for alan in ("spent_total_day", "unallocated_day"):
            _sigma(
                g["atamasiz"]["footer"][alan],
                g["civil"]["footer"][alan],
                g["elek"]["footer"][alan],
                0,
                g["iki_disiplin"]["footer"][alan],
            )
        for i, nokta in enumerate(g["atamasiz"]["trend"]):
            for alan in ("earned_day", "spent_day"):
                _sigma(
                    nokta[alan],
                    g["civil"]["trend"][i][alan],
                    g["elek"]["trend"][i][alan],
                    0,
                    g["iki_disiplin"]["trend"][i][alan],
                )


async def test_sigma_qurr_f_l_toplamlari(client: AsyncClient, dunya_b3: Dunya) -> None:
    for hafta in (1, 3):

        async def cek(c, d, ad, hafta=hafta):
            return await haftalik(c, d, ad, hafta)

        q = await _dort(client, dunya_b3, cek)
        yok = bul(q["atamasiz"]["totals"], kind="discipline", node_id="d:none")
        toplamlar = {ad: bul(r["totals"], kind="all_total") for ad, r in q.items()}
        for alan in (
            "g_budget_mhr",
            "h_earned_cum",
            "i_spent_cum",
            "j_remaining_mhr",
            "k_earned_week",
            "l_spent_week",
        ):
            _sigma(
                toplamlar["atamasiz"][alan],
                toplamlar["civil"][alan],
                toplamlar["elek"][alan],
                yok[alan],
                toplamlar["iki_disiplin"][alan],
            )
        # satır kimliği: kısıtlı QURR satırı == atamasız aynı satır
        for ad in ("civil", "elek"):
            for satir in q[ad]["rows"]:
                assert satir in q["atamasiz"]["rows"], (ad, satir["node_id"])
        assert Decimal(0) <= sayi(toplamlar["civil"]["i_spent_cum"])

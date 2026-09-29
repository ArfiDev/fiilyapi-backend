"""DSC-B3 (canlı yol): EV raporları disiplin kapsamına göre BUDANMIŞ girdiyle hesaplanır.

Değişmezler: Ç1 takvim sınırı TAM girdiden · Ç3 sınıflama TAM ağaçla (budanan kalem unknown'a
düşmez) · Ü3 puantaj kaynağı site düzeyi · Σ toplamsallık (civil + elek + d:none = atamasız) ·
Ü7 yabancı disiplin/kalem yok gibi. Dünya `dunya_b3` (bkz. `_b3_dunya`).
"""

from __future__ import annotations

from httpx import AsyncClient

from tests._disiplin_dunyasi import Dunya
from tests.discipline_scope._b3_yardim import (
    GUN_KILITLI,
    GUN_TASLAK,
    ev,
    gunluk,
    haftalik,
    panel,
    sayi,
    toplam,
)

KISITLILAR = ("civil", "elek")


# ------------------------------------------------------------------ Ç1 takvim sınırı


async def test_c1_elektrik_takvim_siniri_atamasizla_ayni_panel(
    client: AsyncClient, dunya_b3: Dunya
) -> None:
    """Elektrik'in tek yaprağı 04–15.05; sınır budamadan SONRA hesaplansaydı bitiş 15.05 olurdu."""
    tam = await panel(client, dunya_b3, "atamasiz", "2026-05-05")
    assert tam["calendar_end"] == "2026-05-29"
    for ad in (*KISITLILAR, "iki_disiplin"):
        kisitli = await panel(client, dunya_b3, ad, "2026-05-05")
        for alan in ("calendar_start", "calendar_end", "day_no", "week_no", "week_start"):
            assert kisitli[alan] == tam[alan], (ad, alan)


async def test_c1_elektrik_qurr_takvimi_ve_hafta_kirpmasi_ayni(
    client: AsyncClient, dunya_b3: Dunya
) -> None:
    tam = await haftalik(client, dunya_b3, "atamasiz", 1)
    for ad in (*KISITLILAR, "iki_disiplin"):
        kisitli = await haftalik(client, dunya_b3, ad, 1)
        for alan in ("calendar_start", "calendar_end", "last_week_no", "week_start", "report_date"):
            assert kisitli[alan] == tam[alan], (ad, alan)
    h3_tam = await haftalik(client, dunya_b3, "atamasiz", 3)
    h3_elek = await haftalik(client, dunya_b3, "elek", 3)  # takvim kırpılsaydı 404
    assert h3_elek["week_start"] == h3_tam["week_start"] == "2026-05-18"


# ------------------------------------------------------------------ Ç3 bilinmeyen satırlar


def _bilinmeyen(rapor: dict) -> list[dict]:
    return [w for w in rapor["warnings"] if w["code"] == "unknown_line"]


async def test_c3_budanan_kalem_unknown_lines_a_dusmez(
    client: AsyncClient, dunya_b3: Dunya
) -> None:
    """E3 07.05: I2·S2 (Elektrik'in KENDİ bilinmeyen satırı) yalnız Elektrik'e görünür."""
    tam = await gunluk(client, dunya_b3, "atamasiz", GUN_TASLAK)
    assert len(_bilinmeyen(tam)) == 1
    assert _bilinmeyen(await gunluk(client, dunya_b3, "civil", GUN_TASLAK)) == []
    assert len(_bilinmeyen(await gunluk(client, dunya_b3, "elek", GUN_TASLAK))) == 1
    assert len(_bilinmeyen(await gunluk(client, dunya_b3, "iki_disiplin", GUN_TASLAK))) == 1


async def test_c3_bolumsuz_disiplinsiz_satir_iki_kisitliya_da_gorunmez(
    client: AsyncClient, dunya_b3: Dunya
) -> None:
    """I3 (d:none) satırları: ne düğüm ne uyarı."""
    for ad in KISITLILAR:
        rapor = await gunluk(client, dunya_b3, ad, GUN_TASLAK)
        ids = {q["node_id"] for q in rapor["quantities"]}
        assert not any(str(dunya_b3.i["i3"].id) in i for i in ids), ad
        assert not any(i == "d:none" for i in ids), ad
        p = await panel(client, dunya_b3, ad, GUN_TASLAK)
        assert all("none" not in (r["node_id"] or "") for r in p["rows"]), ad


# ------------------------------------------------------------------ Ü3 puantaj kaynağı


async def test_u3_puantaj_kaynagi_site_duzeyi_kisitlida_ayni(
    client: AsyncClient, dunya_b3: Dunya
) -> None:
    tam = await gunluk(client, dunya_b3, "atamasiz", GUN_KILITLI)
    assert sayi(tam["footer"]["timesheet_total_day"]) == 9
    for ad in KISITLILAR:
        kisitli = await gunluk(client, dunya_b3, ad, GUN_KILITLI)
        for alan in ("timesheet_total_day", "undistributed_day"):
            assert kisitli["footer"][alan] == tam["footer"][alan], (ad, alan)
        p = await panel(client, dunya_b3, ad, GUN_KILITLI)
        t = await panel(client, dunya_b3, "atamasiz", GUN_KILITLI)
        for alan in ("timesheet_total_day", "undistributed_day"):
            assert p["kpi"][alan] == t["kpi"][alan], (ad, alan)


async def test_u3_harcanan_toplam_ve_dagitilmamis_kendi_disiplinlerinde(
    client: AsyncClient, dunya_b3: Dunya
) -> None:
    tam = (await gunluk(client, dunya_b3, "atamasiz", GUN_KILITLI))["footer"]
    civil = (await gunluk(client, dunya_b3, "civil", GUN_KILITLI))["footer"]
    elek = (await gunluk(client, dunya_b3, "elek", GUN_KILITLI))["footer"]
    assert sayi(civil["spent_total_day"]) == 5  # Ali 5 sa → I1·S1
    assert sayi(elek["spent_total_day"]) == 4  # Veli 4 sa → I2·S1
    for alan in ("spent_total_day", "unallocated_day"):
        assert toplam(civil[alan], elek[alan]) == sayi(tam[alan]), alan


async def test_kapsamda_baseline_koku_yoksa_rapor_yok_500_degil(
    client: AsyncClient, dunya_b3: Dunya, seeded_db, user_factory
) -> None:
    """Atandığı disiplinin bu şantiyenin aktif baseline'ında kökü yok → budanmış orman BOŞ;
    motor boş ağaç kabul etmez (ValueError): 500 değil, "baseline yok" yanıtları."""
    from app.modules.earned_value.engine import ContractorType
    from app.modules.earned_value.models import EvDiscipline, UserDiscipline
    from app.modules.users.models import UserProjectAccess
    from tests._disiplin_dunyasi import SIFRE, _giris, _kimlik

    d = dunya_b3
    bos = EvDiscipline(
        id=_kimlik(20, 9),
        code="BOS",
        name="Boş",
        color="#111111",
        default_contractor_type=ContractorType.OWN,
        sort_order=9,
    )
    seeded_db.add(bos)
    await seeded_db.flush()
    user = await user_factory(email="bos@dsc-b3.co", password=SIFRE, role_key="project_manager")
    seeded_db.add_all(
        [
            UserProjectAccess(user_id=user.id, project_id=d.proje.id, all_projects=False),
            UserDiscipline(user_id=user.id, discipline_id=bos.id),
        ]
    )
    await seeded_db.flush()
    baslik = await _giris(client, "bos@dsc-b3.co")
    p = await client.get(
        ev(d, "/panel"), headers=baslik, params={"date": "2026-05-06", "range": "4w"}
    )
    assert p.status_code == 200 and p.json()["has_baseline"] is False
    g = await client.get(ev(d, "/reports/daily"), headers=baslik, params={"date": GUN_TASLAK})
    assert g.status_code == 200 and g.json()["status"] == "not_generated"
    q = await client.get(ev(d, "/reports/weekly"), headers=baslik, params={"week": 1})
    assert q.status_code == 409
    o = await client.get(ev(d, "/settings/preview"), headers=baslik)
    assert o.status_code == 200 and o.json()["has_baseline"] is False


async def test_c3_dondurmadan_sonra_baska_gruba_tasinan_kalem_unknown_e_dusmez(
    client: AsyncClient, dunya_b3: Dunya
) -> None:
    """Sınıflama TAM donmuş ağaçla: dondurmadan SONRA I1 G1→G2 (DUV) taşınınca donmuş `d:` (KAB)
    ile R(site) (DUV) ayrışır; Civil'in baseline satırları Elektrik'e "baseline'da yok" diye
    sızmamalı (sınıflama budanmış kümeyle yapılırsa 05.05'te 2, 07.05'te 4 unknown_line olur)."""
    d = dunya_b3

    def sinyal(rapor: dict) -> list[tuple[str, str | None]]:
        return [
            (w["code"], w["target_id"]) for w in rapor["warnings"] if w["code"] == "unknown_line"
        ]

    once: dict[tuple[str, str], object] = {}
    for ad in ("civil", "elek"):
        for gun in (GUN_KILITLI, GUN_TASLAK):
            once[(ad, gun)] = sinyal(await gunluk(client, d, ad, gun))
        once[(ad, "panel")] = sinyal(await panel(client, d, ad, GUN_TASLAK))
    resp = await client.patch(
        f"/boq/items/{d.i['i1'].id}",
        headers=d.baslik["yazar_atamasiz"],
        json={"group_id": str(d.g["g2"].id)},
    )
    assert resp.status_code == 200, resp.text
    for ad in ("civil", "elek"):
        for gun in (GUN_KILITLI, GUN_TASLAK):
            assert sinyal(await gunluk(client, d, ad, gun)) == once[(ad, gun)], (ad, gun)
        assert sinyal(await panel(client, d, ad, GUN_TASLAK)) == once[(ad, "panel")], ad

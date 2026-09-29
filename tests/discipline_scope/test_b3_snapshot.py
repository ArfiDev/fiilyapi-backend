"""DSC-B3 Ü4: onaylı günlük snapshot'ı kısıtlıya YENİDEN HESAPLANMADAN budanır.

Birim kısım DB'siz (`restrict_snapshot`); entegrasyon kısmı `dunya_b3` (06.05 onaylı; onaydan
SONRA tolerans 5.00, snapshot'ta 2.00).
"""

from __future__ import annotations

import json

from httpx import AsyncClient

from app.modules.earned_value.report_snapshot_scope import restrict_snapshot
from app.modules.earned_value.schemas_reports import DailyReport
from tests._disiplin_dunyasi import Dunya
from tests.discipline_scope import _golden
from tests.discipline_scope._b3_yardim import gunluk

GUN_ONAYLI = "2026-05-06"
YABANCI_KOD = ("02.001", "Tuğla", "Elektrik", "Boya", "Disiplinsiz")


def _kimlikler(rapor: DailyReport, on: str) -> list[str]:
    return [q.node_id for q in rapor.quantities if q.node_id.startswith(on)]


def _golden_rapor() -> DailyReport:
    """Golden gövdesi (etiketli) → geçerli DailyReport (etiketler gerçek değere çevrilir)."""
    govde = _golden.oku("b3_gunluk_0506_onayli")["body"]  # type: ignore[index]
    govde["generated_at"] = govde["approved_at"] = "2026-05-06T18:00:00+00:00"
    govde["approved_by"]["id"] = "00000000-0000-0000-0000-000000000001"
    govde["revision"]["id"] = "00000000-0000-0000-0000-000000000002"
    govde["revision"]["frozen_at"] = "2026-05-01T09:00:00+00:00"
    return DailyReport.model_validate(govde)


# ------------------------------------------------------------------ birim (DB'siz)


def test_eski_snapshot_varsayilan_alanlarla_dogrulanir_ve_budanir() -> None:
    govde = _golden_rapor().model_dump(mode="json")
    for alan in ("pf_bands", "calendar_start", "calendar_end"):
        govde.pop(alan)  # eski snapshot: EV-BORC-3 öncesi alanlar yok
    eski = DailyReport.model_validate(govde)
    assert eski.pf_bands is None
    assert restrict_snapshot(eski, {"d:<KAB>"}).footer is None


def test_izinli_blok_disiplin_grup_kalem_ve_esli_uyarilar_kalir() -> None:
    kalan = restrict_snapshot(_golden_rapor(), {"d:<DUV>"})
    assert [q.node_id for q in kalan.quantities] == ["d:<DUV>", "g:<G2>", "i:<I2>"]
    assert [k.node_id for k in kalan.kpis] == ["d:<DUV>"]
    assert [(w.code, w.target_id) for w in kalan.warnings if w.target != "day"] == [
        ("pf_out_of_band", "i:<I2>")
    ]
    assert kalan.unrated_entries == []  # l:<I3>:<S2> d:none bloğu


def test_duser_alanlar_ve_baslik_kalir() -> None:
    tam = _golden_rapor()
    kalan = restrict_snapshot(tam, {"d:<KAB>", "d:<DUV>"})
    assert kalan.footer is None and kalan.trend == []
    assert all(k.kind.startswith("discipline") for k in kalan.kpis)
    assert {w.code for w in kalan.warnings} == {"pf_out_of_band", "missing_diary"}
    for alan in ("day_no", "week_no", "approved_by", "missing_diary_dates", "weather", "pf_bands"):
        assert getattr(kalan, alan) == getattr(tam, alan), alan
    assert kalan.tolerance_points == tam.tolerance_points


def test_bos_izinli_kume_hicbir_disiplin_satiri_birakmaz() -> None:
    kalan = restrict_snapshot(_golden_rapor(), set())
    assert kalan.kpis == [] and kalan.quantities == []
    assert [w for w in kalan.warnings if w.target != "day"] == []


def test_blogu_belirsiz_satir_atilir() -> None:
    govde = _golden_rapor().model_dump(mode="json")
    yetim = dict(govde["quantities"][2], node_id="i:<YETIM>")  # `d:` bloğundan ÖNCE
    ters = dict(govde["quantities"][2], node_id="x:<BILINMEYEN>")
    govde["quantities"] = [yetim, *govde["quantities"][:3], ters]
    kalan = restrict_snapshot(DailyReport.model_validate(govde), {"d:<KAB>"})
    assert [q.node_id for q in kalan.quantities] == ["d:<KAB>", "g:<G1>", "i:<I1>"]


# ------------------------------------------------------------------ entegrasyon


def _bayt(nesne: object) -> str:
    return json.dumps(nesne, sort_keys=True, ensure_ascii=False)


async def test_kisitli_yalniz_kendi_satirlari_yeniden_hesaplanmadan(
    client: AsyncClient, dunya_b3: Dunya
) -> None:
    tam = await gunluk(client, dunya_b3, "atamasiz", GUN_ONAYLI)
    assert tam["tolerance_points"] == "2.00"  # snapshot; canlı ayar artık 5.00
    for ad, kod in (("civil", "d:" + str(dunya_b3.kab.id)), ("elek", "d:" + str(dunya_b3.duv.id))):
        r = await gunluk(client, dunya_b3, ad, GUN_ONAYLI)
        assert r["tolerance_points"] == "2.00", ad
        assert [k["node_id"] for k in r["kpis"]] == [kod], ad
        tam_kpi = next(k for k in tam["kpis"] if k["node_id"] == kod)
        assert _bayt(r["kpis"][0]) == _bayt(tam_kpi), ad
        kalan = [q for q in tam["quantities"] if q["node_id"] == kod or _blok(tam, q) == kod]
        assert _bayt(r["quantities"]) == _bayt(kalan), ad
        assert r["footer"] is None and r["trend"] == [], ad
        assert not any(k["kind"].startswith(("overall", "non_direct")) for k in r["kpis"]), ad
        assert all(w["code"] not in ("unknown_line", "undistributed_hours") for w in r["warnings"])
        assert r["approved_by"] == tam["approved_by"] and r["week_no"] == tam["week_no"], ad


def _blok(rapor: dict, satir: dict) -> str | None:
    sahip = None
    for q in rapor["quantities"]:
        if q["node_id"].startswith("d:"):
            sahip = q["node_id"]
        if q is satir:
            return sahip
    return None


async def test_yabanci_kod_ad_kimlik_sizmaz(client: AsyncClient, dunya_b3: Dunya) -> None:
    metin = _bayt(await gunluk(client, dunya_b3, "civil", GUN_ONAYLI))
    for yabanci in (*YABANCI_KOD, str(dunya_b3.duv.id), str(dunya_b3.i["i2"].id)):
        assert yabanci not in metin, yabanci
    metin = _bayt(await gunluk(client, dunya_b3, "elek", GUN_ONAYLI))
    assert str(dunya_b3.kab.id) not in metin and "Civil" not in metin


async def test_iki_disiplin_civil_ve_elek_satirlarinin_birlesimidir(
    client: AsyncClient, dunya_b3: Dunya
) -> None:
    civil = await gunluk(client, dunya_b3, "civil", GUN_ONAYLI)
    elek = await gunluk(client, dunya_b3, "elek", GUN_ONAYLI)
    iki = await gunluk(client, dunya_b3, "iki_disiplin", GUN_ONAYLI)
    assert iki["kpis"] == civil["kpis"] + elek["kpis"]
    assert iki["quantities"] == civil["quantities"] + elek["quantities"]


async def test_atamasiz_onayli_golden_ile_birebir_payload(
    client: AsyncClient, dunya_b3: Dunya
) -> None:
    tam = await gunluk(client, dunya_b3, "atamasiz", GUN_ONAYLI)
    assert tam["footer"] is not None and len(tam["trend"]) == 7
    assert any(k["kind"] == "overall" for k in tam["kpis"])

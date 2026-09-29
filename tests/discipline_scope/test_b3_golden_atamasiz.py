"""DSC-B3 GOLDEN: ATAMASIZ aktörün B3 okuma uçları (EV raporları, ayarlar, bütçe önizleme) BİREBİR
değişmez. Golden app/ değişmeden (B0b tabanında) alındı: `UPDATE_B3_GOLDEN=1 pytest …` yazar;
normal koşuda yalnız KIYAS edilir. Dünya: `_b3_dunya.genislet` (bkz. orada).
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient, Response

from tests._disiplin_dunyasi import Dunya
from tests.discipline_scope._golden import golden_yolu, normalize, oku, xlsx_dokumu, yaz

GUNCELLE = os.environ.get("UPDATE_B3_GOLDEN") == "1"
Istek = Callable[[AsyncClient, Dunya, dict[str, str], dict[str, str]], Awaitable[Response]]


def _s(d: Dunya, kuyruk: str) -> str:
    return f"/sites/{d.santiye.id}/earned-value{kuyruk}"


def _get(kuyruk: str, params: dict[str, object] | None = None) -> Istek:
    async def istek(c, d, h, ust):
        return await c.get(_s(d, kuyruk), headers=h, params=params)

    return istek


def _panel(tarih: str, **ek: str) -> Istek:
    async def istek(c, d, h, ust):
        params = {"date": tarih, "range": "4w", **ek}
        if "discipline_id" in params:
            params["discipline_id"] = params["discipline_id"].replace("KAB", str(d.kab.id))
        return await c.get(_s(d, "/panel"), headers=h, params=params)

    return istek


def _bilesik(olcu: str, pay: list[str], payda: str) -> Istek:
    async def istek(c, d, h, ust):
        kalem = {k: str(v.id) for k, v in d.i.items()}
        kalem["yok"] = str(d.yabanci_kalem_kimligi)
        params = {
            "measure": olcu,
            "numerator_item_id": [kalem[p] for p in pay],
            "denominator_item_id": kalem[payda],
            "date": "2026-05-06",
        }
        return await c.get(_s(d, "/settings/preview/composite"), headers=h, params=params)

    return istek


def _onizleme(govde) -> Istek:
    async def istek(c, d, h, ust):
        yuk = govde(d, ust) if callable(govde) else govde
        return await c.post(_s(d, "/budget/preview"), headers=h, json=yuk)

    return istek


def _ezme(d: Dunya, ust: dict[str, str]) -> dict[str, object]:
    return {
        "distributions": [
            {"discipline_id": str(d.kab.id), "distribution": "bell"},
            {"discipline_id": str(d.duv.id), "distribution": "front"},
        ],
        "windows": [
            {
                "discipline_id": str(d.duv.id),
                "section_id": str(d.s1.id),
                "start_date": "2026-05-05",
                "end_date": "2026-05-14",
            }
        ],
    }


# ad -> (istek, tür)
SENARYOLAR: dict[str, tuple[Istek, str]] = {
    "b3_panel_0505_4w": (_panel("2026-05-05"), "json"),
    "b3_panel_0506_3m": (_panel("2026-05-06", range="3m"), "json"),
    "b3_panel_0506_all": (_panel("2026-05-06", range="all"), "json"),
    "b3_panel_0506_kab": (_panel("2026-05-06", discipline_id="d:KAB"), "json"),
    "b3_panel_0506_dnone": (_panel("2026-05-06", discipline_id="d:none"), "json"),
    "b3_panel_0506_kendi": (_panel("2026-05-06", contractor_type="own"), "json"),
    # Ek golden (DSC-B3 B ajanı): 07.05 taslak günü → panel unknown_line uyarıları görünür
    # (kısıtsız dalda budama yolu mutantı yalnız b3_gunluk_0507'ye değil buraya da düşsün).
    "b3_panel_0507_all": (_panel("2026-05-07", range="all"), "json"),
    "b3_gunluk_0505": (_get("/reports/daily", {"date": "2026-05-05"}), "json"),
    "b3_gunluk_0506_onayli": (_get("/reports/daily", {"date": "2026-05-06"}), "json"),
    "b3_gunluk_0507": (_get("/reports/daily", {"date": "2026-05-07"}), "json"),
    "b3_qurr_h1": (_get("/reports/weekly", {"week": 1}), "json"),
    "b3_qurr_h3": (_get("/reports/weekly", {"week": 3}), "json"),
    "b3_qurr_varsayilan": (_get("/reports/weekly"), "json"),
    "b3_qurr_h1_xlsx": (_get("/reports/weekly.xlsx", {"week": 1}), "xlsx"),
    "b3_qurr_h3_xlsx": (_get("/reports/weekly.xlsx", {"week": 3}), "xlsx"),
    "b3_ayarlar": (_get("/settings"), "json"),
    "b3_ayar_onizleme_0506": (_get("/settings/preview", {"date": "2026-05-06"}), "json"),
    "b3_ayar_onizleme_varsayilan": (_get("/settings/preview"), "json"),
    "b3_bilesik_i1_i1": (_bilesik("spent", ["i1"], "i1"), "json"),
    "b3_bilesik_i1i2_i1": (_bilesik("earned", ["i1", "i2"], "i1"), "json"),
    "b3_bilesik_i3_i3": (_bilesik("spent", ["i3"], "i3"), "json"),
    "b3_bilesik_yok_i1": (_bilesik("spent", ["yok"], "i1"), "json"),
    "b3_butce_onizleme_aktif": (_onizleme(lambda d, u: {"revision_id": u["<ACTIVE>"]}), "json"),
    "b3_butce_onizleme_taslak": (_onizleme({}), "json"),
    "b3_butce_onizleme_taslak_ezme": (_onizleme(_ezme), "json"),
}


async def _revizyonlar(client: AsyncClient, d: Dunya, baslik: dict[str, str]) -> dict[str, str]:
    resp = await client.get(_s(d, "/budget/revisions"), headers=baslik)
    assert resp.status_code == 200, resp.text
    return {f"<{r['status'].upper()}>": r["id"] for r in resp.json()}


@pytest.mark.parametrize("ad", sorted(SENARYOLAR))
async def test_b3_atamasiz_golden(client: AsyncClient, dunya_b3: Dunya, ad: str) -> None:
    d = dunya_b3
    ust = await _revizyonlar(client, d, d.baslik["atamasiz"])
    istek, tur = SENARYOLAR[ad]
    resp = await istek(client, d, d.baslik["atamasiz"], ust)
    assert resp.status_code == 200, resp.text
    if tur == "xlsx":
        yanit = {
            "status": resp.status_code,
            "content_type": resp.headers["content-type"],
            "cells": xlsx_dokumu(resp.content, d.etiketler),
        }
    else:
        yanit = {"status": resp.status_code, "body": normalize(resp.json(), d.etiketler)}
    if GUNCELLE:
        yaz(ad, yanit)
        return
    assert golden_yolu(ad).exists(), f"golden yok: {ad} (UPDATE_B3_GOLDEN=1 ile üret)"
    assert yanit == oku(ad)

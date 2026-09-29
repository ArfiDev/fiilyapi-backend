"""DSC-B1 GOLDEN: ATAMASIZ aktörün B1 kapsamındaki HER ucunun yanıtı BİREBİR değişmez.

Golden B0 kodunda (app/ değişmeden) alındı: `UPDATE_B1_GOLDEN=1 pytest …` yazar; normal
koşuda yalnız KIYAS edilir. UUID'ler dünya etiketleriyle (`<I1>`, `<entry:e1>`…), zaman
damgaları `<ts>` ile normalize edilir; xlsx için hücre dökümü (sayfa, satır, sütun, değer).

Kısıtsızda `item_visible_clause` yalnız `true` üretir ve hiçbir süzgeç eklenmez: bu test o
iddianın kanıtıdır (ItemVisible sarmalayıcısı, `item_fk_conditions`/`visible_item_set`
kısıtsızda boş → yanıt aynı).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable

import pytest
from httpx import AsyncClient

from tests._disiplin_dunyasi import GUN1, GUN2, Dunya
from tests.discipline_scope._golden import golden_yolu, normalize, oku, xlsx_dokumu, yaz

Url = Callable[[Dunya], str]


def _b(d: Dunya, kuyruk: str = "") -> str:
    return f"/sites/{d.santiye.id}/earned-value/budget{kuyruk}"


def _ev(d: Dunya, kuyruk: str) -> str:
    return f"/sites/{d.santiye.id}/earned-value{kuyruk}"


# ad -> (URL üretici, tür). tür: "json" | "xlsx"
UCLAR: dict[str, tuple[Url, str]] = {
    "boq": (lambda d: f"/sites/{d.santiye.id}/boq", "json"),
    "boq_bolum": (lambda d: f"/sites/{d.santiye.id}/boq?section_id={d.s1.id}", "json"),
    "boq_export": (lambda d: f"/sites/{d.santiye.id}/boq/export", "xlsx"),
    "boq_tahsis_i1": (lambda d: f"/boq/items/{d.i['i1'].id}/allocations", "json"),
    "boq_tahsis_i3": (lambda d: f"/boq/items/{d.i['i3'].id}/allocations", "json"),
    "gunluk_detay_e1": (lambda d: f"/diary/{d.gunluk['e1'].id}", "json"),
    "gunluk_detay_e2_bolum": (
        lambda d: f"/diary/{d.gunluk['e2'].id}?section_id={d.s1.id}",
        "json",
    ),
    "gunluk_liste": (lambda d: f"/sites/{d.santiye.id}/diary", "json"),
    "gunluk_liste_bolum": (lambda d: f"/sites/{d.santiye.id}/diary?section_id={d.s2.id}", "json"),
    "gunluk_ozet": (lambda d: f"/sites/{d.santiye.id}/diary/summary", "json"),
    "ev_butce_taslak": (lambda d: _b(d), "json"),
    "ev_butce_aktif": (lambda d: _b(d, "?revision_id=<AKTIF>"), "json"),
    "ev_butce_fark": (lambda d: _b(d, "/revisions/<TASLAK>/diff"), "json"),
    "ev_takvim": (lambda d: _b(d, "/schedule"), "json"),
    "ev_oneri_i1": (lambda d: _b(d, f"/items/{d.i['i1'].id}/suggestions"), "json"),
    "ev_oneri_i2": (lambda d: _b(d, f"/items/{d.i['i2'].id}/suggestions"), "json"),
    "ev_kod_agaci": (lambda d: _ev(d, "/code-tree"), "json"),
    "ev_gun": (lambda d: _ev(d, f"/days/{GUN1.isoformat()}"), "json"),
    "ev_onceki_dagilim": (
        lambda d: _ev(d, f"/days/{GUN2.isoformat()}/previous-allocation"),
        "json",
    ),
    "ev_disiplinler": (lambda d: "/earned-value/disciplines", "json"),
    "ev_katalog": (lambda d: "/earned-value/catalog", "json"),
    "stok_hareketleri": (lambda d: "/stock/entries", "json"),
    "stok_hareketleri_transfer": (lambda d: "/stock/entries?entry_type=transfer", "json"),
    "bolum_stok": (lambda d: f"/sections/{d.s1.id}/stock", "json"),
    "bolum_stok_s2": (lambda d: f"/sections/{d.s2.id}/stock", "json"),
}
GUNCELLE = os.environ.get("UPDATE_B1_GOLDEN") == "1"


async def _revizyonlari_etiketle(
    client: AsyncClient, d: Dunya, baslik: dict[str, str]
) -> dict[str, str]:
    """Aktif/taslak revizyon kimliklerini etiketler (`<rev:active>`) ve URL yer tutucuları
    (`<AKTIF>`/`<TASLAK>`) için döner."""
    resp = await client.get(_b(d, "/revisions"), headers=baslik)
    assert resp.status_code == 200, resp.text
    kimlik = {r["status"]: r["id"] for r in resp.json()}
    for durum, deger in kimlik.items():
        d.etiketler[uuid.UUID(deger)] = f"<rev:{durum}>"
    return {"<AKTIF>": kimlik["active"], "<TASLAK>": kimlik["draft"]}


async def _yanit(
    client: AsyncClient, d: Dunya, ad: str, baslik: dict[str, str]
) -> dict[str, object]:
    url_uretici, tur = UCLAR[ad]
    url = url_uretici(d)
    for yertutucu, kimlik in (await _revizyonlari_etiketle(client, d, baslik)).items():
        url = url.replace(yertutucu, kimlik)
    resp = await client.get(url, headers=baslik)
    if tur == "xlsx":
        assert resp.status_code == 200, resp.text
        return {
            "status": resp.status_code,
            "content_type": resp.headers["content-type"],
            "cells": xlsx_dokumu(resp.content, d.etiketler),
        }
    return {"status": resp.status_code, "body": normalize(resp.json(), d.etiketler)}


@pytest.mark.parametrize("ad", sorted(UCLAR))
async def test_atamasiz_yanit_golden_ile_birebir(
    client: AsyncClient, dunya: Dunya, atamasiz: dict[str, str], ad: str
) -> None:
    yanit = await _yanit(client, dunya, ad, atamasiz)
    assert yanit["status"] == 200
    if GUNCELLE:
        yaz(ad, yanit)
        return
    assert golden_yolu(ad).exists(), f"golden yok: {ad} (UPDATE_B1_GOLDEN=1 ile üret)"
    assert yanit == oku(ad)

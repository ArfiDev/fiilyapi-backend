"""DSC-B4 (KULLANICI KARARI 2026-09-29, "stok disiplinsiz") — stok okumaları disiplin kuralının
TAMAMEN dışındadır.

Bu dosya B1'in "kısıtlı yalnız görünür-kalem satırını görür" testlerinin YERİNİ aldı (o beklenti
artık yanlış: `GET /stock/entries` ve `GET /sections/{id}/stock` süzmesi geri alındı). Yeni iddia:
civil (KAB) ve elek (DUV) aktörlerinin yanıtı, atamasız yanıtla TAM GÖVDE olarak EŞİT — dört stok
okuma ucu (`/stock/entries`, `/sections/{id}/stock` S1/S2, `/stock/summary`, `/sites/{id}/stock`)
ve süzgeç varyantları. Atamasız golden'lar (`golden/stok_*`, `bolum_stok*`, `b4_*stok*`) DEĞİŞMEZ.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests._disiplin_dunyasi import Dunya

_UC_ADLARI = (
    "hareketler",
    "hareketler_transfer",
    "hareketler_sayfa",
    "bolum_s1",
    "bolum_s2",
    "ozet",
    "ozet_kritik",
    "santiye",
    "santiye_s1",
)


def _uclar(d: Dunya) -> dict[str, tuple[str, dict[str, str]]]:
    return {
        "hareketler": ("/stock/entries", {}),
        "hareketler_transfer": ("/stock/entries", {"entry_type": "transfer"}),
        "hareketler_sayfa": ("/stock/entries", {"entry_type": "adjustment", "limit": "1"}),
        "bolum_s1": (f"/sections/{d.s1.id}/stock", {}),
        "bolum_s2": (f"/sections/{d.s2.id}/stock", {}),
        "ozet": ("/stock/summary", {}),
        "ozet_kritik": ("/stock/summary", {"status": "critical"}),
        "santiye": (f"/sites/{d.santiye.id}/stock", {}),
        "santiye_s1": (f"/sites/{d.santiye.id}/stock", {"section_id": str(d.s1.id)}),
    }


@pytest.mark.parametrize("uc", _UC_ADLARI)
@pytest.mark.parametrize("aktor", ["civil", "elek"])
async def test_stok_kisitli_yanit_atamasizla_birebir(
    client: AsyncClient, dunya: Dunya, uc: str, aktor: str
) -> None:
    yol, params = _uclar(dunya)[uc]
    tam = await client.get(yol, headers=dunya.baslik["atamasiz"], params=params)
    kisitli = await client.get(yol, headers=dunya.baslik[aktor], params=params)
    assert tam.status_code == kisitli.status_code == 200, (tam.text, kisitli.text)
    assert kisitli.json() == tam.json()


async def test_stok_gercek_veri_bos_degil(client: AsyncClient, dunya: Dunya) -> None:
    """Sahte-yeşil koruması: eşitlik iki boş yanıtla sağlanmasın (dünya dolu)."""
    hareketler = (await client.get("/stock/entries", headers=dunya.baslik["civil"])).json()
    assert hareketler["total"] == 4
    assert any(ln["boq_item_id"] is None for e in hareketler["items"] for ln in e["lines"])
    bolum = (
        await client.get(f"/sections/{dunya.s1.id}/stock", headers=dunya.baslik["elek"])
    ).json()
    assert bolum["total"] == 3

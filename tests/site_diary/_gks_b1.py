"""GKS-B1 test yardımcıları: iskelet satırı anahtarı ve istek kısaltmaları."""

from datetime import date
from decimal import Decimal

from httpx import AsyncClient

from tests.site_diary.conftest import KarisikSantiye

GUN = date(2026, 7, 20)


def anahtarlar(satirlar: list[dict]) -> list[tuple[str, str | None, Decimal]]:
    """Satır listesi → (kalem kodu, bölüm adı | None, planlı) — sıra KORUNUR."""
    return [
        (
            satir["code"],
            satir["section_name"],
            Decimal(satir["planned_quantity"]),
        )
        for satir in satirlar
    ]


def beklenen(tanim: list[tuple[str, str | None, str]]) -> list[tuple[str, str | None, Decimal]]:
    return [(kod, bolum, Decimal(planli)) for kod, bolum, planli in tanim]


def satir(ks: KarisikSantiye, kod: str, miktar: str, bolum: str | None = None, **ekstra) -> dict:
    govde: dict = {"boq_item_id": str(ks.items[kod].id), "quantity": miktar, **ekstra}
    if bolum is not None:
        govde["section_id"] = str((ks.s1 if bolum == "S1" else ks.s2).id)
    else:
        govde["section_id"] = None
    return govde


async def onizleme(client: AsyncClient, headers, site_id, tarih: date = GUN, bolum=None):  # noqa: ANN201
    params = {"entry_date": tarih.isoformat()}
    if bolum is not None:
        params["section_id"] = str(bolum.id)
    return await client.get(f"/sites/{site_id}/diary/skeleton", params=params, headers=headers)


async def olustur(client: AsyncClient, headers, site_id, tarih: date = GUN, bolum=None, **ekstra):  # noqa: ANN201
    govde = {"entry_date": tarih.isoformat(), **ekstra}
    if bolum is not None:
        govde["section_id"] = str(bolum.id)
    return await client.post(f"/sites/{site_id}/diary", json=govde, headers=headers)

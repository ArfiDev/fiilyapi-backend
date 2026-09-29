"""B3 test yardımcıları: rapor çekme + Decimal toplama."""

from __future__ import annotations

from decimal import Decimal

from httpx import AsyncClient

from tests._disiplin_dunyasi import Dunya

D = Decimal
ZERO = Decimal(0)
GUN_KILITLI = "2026-05-05"  # onay 06.05 kilidi: snapshot yok → canlı yol
GUN_TASLAK = "2026-05-07"


def ev(d: Dunya, kuyruk: str) -> str:
    return f"/sites/{d.santiye.id}/earned-value{kuyruk}"


async def al(
    client: AsyncClient, d: Dunya, kuyruk: str, ad: str, params: dict | None = None
) -> dict:
    resp = await client.get(ev(d, kuyruk), headers=d.baslik[ad], params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def panel(client: AsyncClient, d: Dunya, ad: str, tarih: str = "2026-05-06", **ek) -> dict:
    return await al(client, d, "/panel", ad, {"date": tarih, "range": "4w", **ek})


async def gunluk(client: AsyncClient, d: Dunya, ad: str, tarih: str) -> dict:
    return await al(client, d, "/reports/daily", ad, {"date": tarih})


async def haftalik(client: AsyncClient, d: Dunya, ad: str, hafta: int = 1) -> dict:
    return await al(client, d, "/reports/weekly", ad, {"week": hafta})


def sayi(deger: object) -> Decimal:
    return ZERO if deger is None else Decimal(str(deger))


def bul(satirlar: list[dict], **kosul: object) -> dict:
    return next(s for s in satirlar if all(s.get(k) == v for k, v in kosul.items()))


def toplam(*degerler: object) -> Decimal:
    return sum((sayi(v) for v in degerler), ZERO)

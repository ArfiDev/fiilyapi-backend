"""TKL-B4.2 test yardimcilari (HTTP uzerinden teklif kurma)."""

from __future__ import annotations

from decimal import Decimal

D = Decimal

URL = "/offers"


def rev_url(offer_id: str, rev_no: int = 0) -> str:
    return f"{URL}/{offer_id}/revisions/{rev_no}"


async def teklif(client, admin, isveren, **over) -> dict:
    govde = {"employer_id": str(isveren.id), "title": "A Blok Kaba İnşaat", **over}
    resp = await client.post(URL, json=govde, headers=admin)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def grup(client, admin, offer_id: str, rev_no: int = 0, name: str = "Kaba") -> dict:
    resp = await client.post(
        rev_url(offer_id, rev_no) + "/groups", json={"name": name}, headers=admin
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def kalem(
    client, admin, offer_id: str, group_id: str, katalog_id, rev_no: int = 0, **over
) -> dict:
    govde = {
        "catalog_item_id": str(katalog_id),
        "group_id": group_id,
        "quantity": "1",
        **over,
    }
    resp = await client.post(rev_url(offer_id, rev_no) + "/items", json=govde, headers=admin)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def revizyon(client, admin, offer_id: str, rev_no: int = 0) -> dict:
    resp = await client.get(rev_url(offer_id, rev_no), headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def detay(client, admin, offer_id: str) -> dict:
    resp = await client.get(f"{URL}/{offer_id}", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def kalemli_yap(client, admin, offer_id: str, rev_no: int = 0) -> None:
    """Revizyonda hic kalem yoksa tek (fiyatsiz) kalem ekler: bos teklif gonderilemez (TKL-B4.3).
    `katalog` fikstürü yukluyse (autouse) ilk katalog kalemi kullanilir."""
    resp = await client.get(rev_url(offer_id, rev_no), headers=admin)
    if resp.status_code != 200:
        return  # revizyon yok: gecis kendi 404'unu versin
    rev = resp.json()
    if rev["status"] != "draft" or tum_kalemler(rev):
        return  # taslak degil: gecis kendi 409'unu versin
    katalog = (await client.get("/catalog/items", headers=admin)).json()["items"]
    g = rev["groups"][0] if rev["groups"] else await grup(client, admin, offer_id, rev_no)
    await kalem(client, admin, offer_id, g["id"], katalog[0]["id"], rev_no, cost_unit_price=None)


async def gecis(
    client, admin, offer_id: str, eylem: str, rev_no: int = 0, *, dolu: bool = True, **govde
) -> object:
    """Gecis cagrisi. `send` icin revizyon kalemsizse once bir kalem eklenir (`dolu=False`: ekleme
    YAPMA — bos gonderim kuralini sinayan testler icin)."""
    if eylem == "send" and dolu:
        await kalemli_yap(client, admin, offer_id, rev_no)
    return await client.post(
        rev_url(offer_id, rev_no) + f"/{eylem}", json=govde or None, headers=admin
    )


async def durum_yap(client, admin, offer_id: str, durum: str, rev_no: int = 0) -> None:
    """Revizyonu istenen duruma getirir (`draft` → hicbir sey)."""
    yol = {
        "draft": [],
        "sent": ["send"],
        "won": ["send", "win"],
        "lost": ["send", "lose"],
        "withdrawn": ["withdraw"],
    }[durum]
    for eylem in yol:
        resp = await gecis(client, admin, offer_id, eylem, rev_no)
        assert resp.status_code == 200, resp.text


def tum_kalemler(rev: dict) -> list[dict]:
    return [k for g in rev["groups"] for k in g["items"]]


# ----------------------------------------------------------------- sablon (TKL-B5.1)

TPL = "/offers/templates"


async def sablon(client, admin, name: str = "Villa Kaba", **over) -> dict:
    resp = await client.post(TPL, json={"name": name, **over}, headers=admin)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def sablon_ua(client, admin, template_id: str) -> str:
    """Sablonun GUNCEL `updated_at` metni (iyimser kilit `expected_updated_at`i icin)."""
    resp = await client.get(f"{TPL}/{template_id}", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()["updated_at"]


async def sablon_icerik(client, admin, template_id: str, gruplar: list[tuple[str, list]]) -> dict:
    """`gruplar`: `[(grup adi, [katalog_id, ...]), ...]` — PUT content ile tam degistirir."""
    govde = {
        "groups": [
            {"name": ad, "items": [{"catalog_item_id": str(k)} for k in kalemler]}
            for ad, kalemler in gruplar
        ],
        "expected_updated_at": await sablon_ua(client, admin, template_id),
    }
    resp = await client.put(f"{TPL}/{template_id}/content", json=govde, headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def sablon_kalemleri(detay_: dict) -> list[list[str]]:
    """Sablon detayindaki gruplarin poz no listeleri (sirayla)."""
    return [[i["poz_no"] for i in g["items"]] for g in detay_["groups"]]

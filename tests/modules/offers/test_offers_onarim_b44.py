"""TKL-B4.4 — B4.2 cürütme bulgularının onarımı: R1 tarih aralığı · R2 denetim · R3 yazma
yanıtı biçimi · R4 gövde-içi grup referansı."""

from __future__ import annotations

import uuid

import pytest

from app.modules.audit.models import AuditAction

from .._boq import _audit_details
from ._offers import URL, detay, grup, kalem, rev_url, revizyon, teklif, tum_kalemler

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------------------------ R1


@pytest.mark.parametrize("tarih", ["9999-12-31", "3000-01-01", "1999-12-31"])
async def test_r1_offer_date_aralik_disi_create_422(client, admin, isveren, tarih) -> None:
    resp = await client.post(
        URL, json={"employer_id": str(isveren.id), "title": "x", "offer_date": tarih}, headers=admin
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("tarih", ["9999-12-31", "3000-01-01", "1999-12-31"])
async def test_r1_offer_date_aralik_disi_revizyon_patch_422(client, admin, isveren, tarih) -> None:
    o = await teklif(client, admin, isveren)
    resp = await client.patch(rev_url(o["id"]), json={"offer_date": tarih}, headers=admin)
    assert resp.status_code == 422, resp.text
    assert (await client.get(URL, headers=admin)).status_code == 200


@pytest.mark.parametrize("tarih", ["2000-01-01", "2999-12-31"])
async def test_r1_sinir_degerleri_kabul_valid_until_tasmaz(client, admin, isveren, tarih) -> None:
    o = await teklif(client, admin, isveren, offer_date=tarih, validity_days=365)
    rev = await revizyon(client, admin, o["id"])
    assert rev["offer_date"] == tarih and rev["valid_until"] is not None
    assert (await client.get(URL, headers=admin)).status_code == 200


# ------------------------------------------------------------------------------ R2a


async def _update_sayisi(db_session) -> int:
    return len(await _audit_details(db_session, AuditAction.update))


async def test_r2a_bos_ve_degismeyen_patch_denetim_yazmaz(
    client, admin, isveren, db_session
) -> None:
    o = await teklif(client, admin, isveren, notes="n")
    onceki = await _update_sayisi(db_session)
    for yol, govde in [
        (f"{URL}/{o['id']}", {}),
        (rev_url(o["id"]), {}),
        (f"{URL}/{o['id']}", {"title": o["title"], "employer_id": str(isveren.id)}),
        (rev_url(o["id"]), {"notes": "n", "validity_days": 30}),
    ]:
        resp = await client.patch(yol, json=govde, headers=admin)
        assert resp.status_code == 200, resp.text
    assert await _update_sayisi(db_session) == onceki


async def test_r2a_degisen_patch_denetim_yazar(client, admin, isveren, db_session) -> None:
    o = await teklif(client, admin, isveren)
    onceki = await _update_sayisi(db_session)
    assert (
        await client.patch(f"{URL}/{o['id']}", json={"title": "Yeni"}, headers=admin)
    ).status_code == 200
    assert (
        await client.patch(rev_url(o["id"]), json={"notes": "x"}, headers=admin)
    ).status_code == 200
    assert await _update_sayisi(db_session) == onceki + 2


# ------------------------------------------------------------------------------ R2b


async def test_r2b_kosul_denetimi_eski_yeni(client, admin, isveren, db_session) -> None:
    o = await teklif(client, admin, isveren, offer_date="2026-01-05")
    resp = await client.patch(
        rev_url(o["id"]),
        json={
            "overhead_pct": "10",
            "profit_pct": "20",
            "vat_pct": "18",
            "validity_days": 45,
            "delivery_days": 90,
            "offer_date": "2026-02-01",
            "price_escalation": "tuik",
            "price_index_type": "ufe",
            "payment_terms": "Peşin",
            "notes": "N" * 100,
        },
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    satir = (await _audit_details(db_session, AuditAction.update))[-1]
    assert satir.startswith(f"Teklif koşulları güncellendi: {o['offer_no']} Rev.0 · ")
    for parca in (
        "genel gider %12.00 → %10",
        "kâr %15.00 → %20",
        "KDV %20.00 → %18",
        "geçerlilik 30 → 45 gün",
        "teslim süresi boş → 90 gün",
        "teklif tarihi 2026-01-05 → 2026-02-01",
        "fiyat farkı sabit → TÜİK endeksli (ufe)",
        "ödeme koşulu «Ödeme aylık hakedişle, 30 gün vadeli» → «Peşin»",
        f"notlar «boş» → «{'N' * 60}…»",
    ):
        assert parca in satir, (parca, satir)


async def test_r2b_yalniz_degisen_alan_yazilir(client, admin, isveren, db_session) -> None:
    o = await teklif(client, admin, isveren)
    await client.patch(rev_url(o["id"]), json={"vat_pct": "18", "notes": None}, headers=admin)
    satir = (await _audit_details(db_session, AuditAction.update))[-1]
    assert satir.endswith("Rev.0 · KDV %20.00 → %18.00")


# ------------------------------------------------------------------------------ R2c


async def test_r2c_dolu_grup_silme_tek_satir_bos_grup_satir_yok(
    client, admin, isveren, katalog, db_session
) -> None:
    o = await teklif(client, admin, isveren)
    dolu = await grup(client, admin, o["id"], name="Kaba")
    bos = await grup(client, admin, o["id"], name="Boş")
    for _ in range(3):
        await kalem(client, admin, o["id"], dolu["id"], katalog[0].id)
    r = await client.delete(rev_url(o["id"]) + f"/groups/{bos['id']}", headers=admin)
    assert r.status_code == 204
    assert await _audit_details(db_session, AuditAction.delete) == []
    r = await client.delete(rev_url(o["id"]) + f"/groups/{dolu['id']}", headers=admin)
    assert r.status_code == 204
    assert await _audit_details(db_session, AuditAction.delete) == [
        f"Teklif grubu silindi: {o['offer_no']} Rev.0 · Kaba · 3 kalem"
    ]


# ------------------------------------------------------------------------------ R3


async def _kalem_get(client, admin, o, item_id) -> dict:
    return next(
        k for k in tum_kalemler(await revizyon(client, admin, o["id"])) if k["id"] == item_id
    )


async def test_r3_kalem_post_patch_yaniti_get_ile_ayni(client, admin, isveren, katalog) -> None:
    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    k = await kalem(
        client,
        admin,
        o["id"],
        g["id"],
        katalog[0].id,
        cost_unit_price="1",
        quantity="2",
        overhead_pct="5",
        unit_mhr="3",
    )
    assert k == await _kalem_get(client, admin, o, k["id"])
    p = await client.patch(
        rev_url(o["id"]) + f"/items/{k['id']}",
        json={"quantity": "7", "cost_unit_price": "4", "profit_pct": "9"},
        headers=admin,
    )
    assert p.status_code == 200, p.text
    assert p.json() == await _kalem_get(client, admin, o, k["id"])


async def test_r3_toplu_yaniti_get_ile_ayni(client, admin, isveren, katalog) -> None:
    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    items = [
        {
            "catalog_item_id": str(katalog[i].id),
            "group_id": g["id"],
            "quantity": "1",
            "cost_unit_price": "2",
            "unit_mhr": "1",
        }
        for i in (2, 0, 1)
    ]
    resp = await client.post(rev_url(o["id"]) + "/items/bulk", json={"items": items}, headers=admin)
    assert resp.status_code == 201, resp.text
    yazilan = resp.json()["items"]
    assert [k["catalog_item_id"] for k in yazilan] == [i["catalog_item_id"] for i in items]
    for k in yazilan:
        assert k == await _kalem_get(client, admin, o, k["id"])


async def test_r3_revizyon_ve_kunye_patch_yaniti_get_ile_ayni(client, admin, isveren) -> None:
    o = await teklif(client, admin, isveren)
    r = await client.patch(
        rev_url(o["id"]),
        json={"overhead_pct": "20", "vat_pct": "8", "profit_pct": "1"},
        headers=admin,
    )
    assert r.status_code == 200, r.text
    assert r.json() == await revizyon(client, admin, o["id"])
    k = await client.patch(f"{URL}/{o['id']}", json={"title": "Yeni"}, headers=admin)
    assert k.status_code == 200, k.text
    assert k.json() == await detay(client, admin, o["id"])


# ------------------------------------------------------------------------------ R4


async def test_r4_bilinmeyen_grup_404_baska_teklifin_grubu_422(
    client, admin, isveren, katalog
) -> None:
    a = await teklif(client, admin, isveren)
    b = await teklif(client, admin, isveren, title="B")
    ga = await grup(client, admin, a["id"])
    gb = await grup(client, admin, b["id"])
    yok = str(uuid.uuid4())
    govde = {"catalog_item_id": str(katalog[0].id), "quantity": "1"}
    post = rev_url(a["id"]) + "/items"
    assert (
        await client.post(post, json={**govde, "group_id": yok}, headers=admin)
    ).status_code == 404
    assert (
        await client.post(post, json={**govde, "group_id": gb["id"]}, headers=admin)
    ).status_code == 422
    toplu = {"items": [{**govde, "group_id": ga["id"]}, {**govde, "group_id": yok}]}
    assert (await client.post(post + "/bulk", json=toplu, headers=admin)).status_code == 404
    k = await kalem(client, admin, a["id"], ga["id"], katalog[0].id)
    patch = rev_url(a["id"]) + f"/items/{k['id']}"
    assert (await client.patch(patch, json={"group_id": yok}, headers=admin)).status_code == 404
    assert (
        await client.patch(patch, json={"group_id": gb["id"]}, headers=admin)
    ).status_code == 422

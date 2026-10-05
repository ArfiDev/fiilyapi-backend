"""TKL-B6.7 — kalem okumasinda acik `quantified` bayragi (kimlik kovasi, maskelenmez).

`quantity` metraj alanidir (IZN-B4: hicbir gizli alan kategorisine girmez; `finance` kapsami
kaldirildi); fiyat gizliyken para `None` olur. Frontend "miktarsiz kalem"i
bu yuzden `quantity`den cikaramaz. `quantified` = `calc.ItemResult.quantified`, `priced`tan
BAGIMSIZDIR.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.access import AccessLevel
from app.core.sayfalar import HiddenCategory
from app.modules.roles.models import Role, RoleHiddenField

from .._boq import _auth, _login_with_access, _set_permission
from ._offers import URL, gecis, kalem, rev_url, revizyon, teklif, tum_kalemler


@pytest.fixture
async def karisik(client, admin, isveren, katalog) -> dict:
    """4 kalem: (miktarli/fiyatli) (miktarli/fiyatsiz) (miktarsiz/fiyatli) (miktarsiz/fiyatsiz)."""
    o = await teklif(client, admin, isveren)
    g = (
        await client.post(rev_url(o["id"]) + "/groups", json={"name": "Kaba"}, headers=admin)
    ).json()
    kurulan = {}
    kurulan["mf"] = await kalem(
        client, admin, o["id"], g["id"], katalog[0].id, quantity="10", cost_unit_price="100"
    )
    kurulan["mn"] = await kalem(client, admin, o["id"], g["id"], katalog[1].id, quantity="2")
    kurulan["nf"] = await kalem(
        client, admin, o["id"], g["id"], katalog[2].id, quantity=None, cost_unit_price="50"
    )
    kurulan["nn"] = await kalem(client, admin, o["id"], g["id"], katalog[1].id, quantity=None)
    return {"offer_id": o["id"], "group_id": g["id"], "items": kurulan}


BEKLENEN = {
    "mf": (True, True),  # (priced, quantified)
    "mn": (False, True),
    "nf": (True, False),
    "nn": (False, False),
}


def _ozet(rev: dict, kurulan: dict) -> dict[str, tuple[bool, bool]]:
    kimlik = {v["id"]: ad for ad, v in kurulan.items()}
    return {kimlik[k["id"]]: (k["priced"], k["quantified"]) for k in tum_kalemler(rev)}


async def test_quantified_priced_ten_bagimsiz_dort_kombinasyon(client, admin, karisik) -> None:
    rev = await revizyon(client, admin, karisik["offer_id"])
    assert _ozet(rev, karisik["items"]) == BEKLENEN


async def test_post_yaniti_get_ile_ayni(client, admin, karisik) -> None:
    for ad, k in karisik["items"].items():
        assert (k["priced"], k["quantified"]) == BEKLENEN[ad]


async def test_patch_yaniti_get_ile_ayni_ve_miktar_girince_true_olur(
    client, admin, karisik
) -> None:
    oid = karisik["offer_id"]
    k = karisik["items"]["nf"]
    resp = await client.patch(
        rev_url(oid) + f"/items/{k['id']}", json={"quantity": "4"}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["quantified"] is True
    rev = await revizyon(client, admin, oid)
    assert {x["id"]: x["quantified"] for x in tum_kalemler(rev)}[k["id"]] is True
    # fiyat PATCH'i miktarsiz kalemde bayragi DEGISTIRMEZ
    k2 = karisik["items"]["nn"]
    resp = await client.patch(
        rev_url(oid) + f"/items/{k2['id']}", json={"cost_unit_price": "10"}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    assert (resp.json()["priced"], resp.json()["quantified"]) == (True, False)


async def test_toplu_ekleme_yaniti_bayragi_tasir(client, admin, karisik, katalog) -> None:
    govde = {
        "items": [
            {"catalog_item_id": str(katalog[1].id), "group_id": karisik["group_id"]},
            {
                "catalog_item_id": str(katalog[2].id),
                "group_id": karisik["group_id"],
                "quantity": "3",
            },
        ]
    }
    resp = await client.post(
        rev_url(karisik["offer_id"]) + "/items/bulk", json=govde, headers=admin
    )
    assert resp.status_code == 201, resp.text
    assert [k["quantified"] for k in resp.json()["items"]] == [False, True]


async def test_yeni_revizyon_ve_detay_bayragi_tasir(client, admin, karisik) -> None:
    oid = karisik["offer_id"]
    # kaybedilmis revizyondan yeni revizyon (kalemler kopyalanir); gonderim miktarsiz kalemde
    # 422 verir, bu yuzden once miktarsizlar tamamlanir, sonra tekrar miktarsiz kalem eklenir
    for ad in ("nf", "nn"):
        k = karisik["items"][ad]
        resp = await client.patch(
            rev_url(oid) + f"/items/{k['id']}", json={"quantity": "1"}, headers=admin
        )
        assert resp.status_code == 200, resp.text
    resp = await gecis(client, admin, oid, "send")
    assert resp.status_code == 200, resp.text
    resp = await gecis(client, admin, oid, "lose", lost_reason="Fiyat", winning_amount="1.00")
    assert resp.status_code == 200, resp.text
    yeni = await client.post(f"{URL}/{oid}/revisions", headers=admin)
    assert yeni.status_code == 201, yeni.text
    assert [k["quantified"] for k in tum_kalemler(yeni.json())] == [True] * 4
    rev1_kalemleri = tum_kalemler(await revizyon(client, admin, oid, 1))
    assert rev1_kalemleri and all(k["quantified"] is True for k in rev1_kalemleri)


@pytest.mark.parametrize("kategori", [HiddenCategory.sozlesme_fiyat, HiddenCategory.tum_tutarlar])
async def test_gizli_alanlarda_bayrak_DOGRU_miktar_gorunur(
    client, admin, db_session, user_factory, karisik, kategori
) -> None:
    await _set_permission(db_session, "accounting", "contracts", AccessLevel.view)
    rol_id = (
        await db_session.execute(select(Role.id).where(Role.key == "accounting"))
    ).scalar_one()
    db_session.add(RoleHiddenField(role_id=rol_id, category=kategori))
    await db_session.flush()
    token = await _login_with_access(
        client, db_session, user_factory, "accounting", f"acc.{uuid.uuid4().hex[:6]}@tkl.co"
    )
    rev = (await client.get(rev_url(karisik["offer_id"], 0), headers=_auth(token))).json()
    assert _ozet(rev, karisik["items"]) == BEKLENEN

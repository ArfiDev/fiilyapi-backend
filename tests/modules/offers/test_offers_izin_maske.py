"""TKL-B4.2 — teklif uclarinin izin kapisi ve alan maskesi.

GECICI IZIN (T25): okuma `contracts:view`, yazma `contracts:full` + `RequireUnrestricted`;
`site_chief`/`field_engineer` (contracts=none) 403. Maske: para alanlari `limited` kapsamda
`None`; yuzdeler/adam-saat/sayaclar gorunur; miktar `operasyonel` (`finance` kapsamda `None`).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.core.access import AccessLevel
from app.core.sayfalar import HiddenCategory
from app.modules.catalog.models import ContractorType, EvDiscipline
from app.modules.offers.models import Offer
from app.modules.roles.models import Role, RoleHiddenField
from app.modules.roles.seed_data import MATRIX, ROLE_ORDER
from app.modules.users.models import User
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz
from tests._proje_ekibi import baska_projede_disiplinli

from .._boq import _auth, _login_with_access
from ._offers import URL, D, durum_yap, gecis, grup, kalem, rev_url, teklif

#: Para anahtarlari (`Gorunurluk.para`): limited kapsamda HEPSI None olmali.
PARA_ANAHTARLARI = {
    "cost_unit_price",
    "offer_unit_price",
    "unit_price",
    "amount",
    "cost",
    "overhead",
    "profit",
    "net",
    "vat",
    "gross",
    "winning_amount",
}


async def _gizle(db_session, role_key: str, *kategoriler: HiddenCategory) -> None:
    """IZN-B4: rolun gizli alan kutucuklarini testte ACIKCA kurar (seede bagimli olma)."""
    rol_id = (await db_session.execute(select(Role.id).where(Role.key == role_key))).scalar_one()
    for kategori in kategoriler:
        db_session.add(RoleHiddenField(role_id=rol_id, category=kategori))
    await db_session.flush()


async def _giris(client, db_session, user_factory, role_key: str) -> dict[str, str]:
    token = await _login_with_access(
        client, db_session, user_factory, role_key, f"{role_key}.{uuid.uuid4().hex[:6]}@tkl.co"
    )
    return _auth(token)


@pytest.fixture
async def dolu(client, admin, isveren, katalog) -> dict:
    """Rev.0 KAYBEDILMIS (kazanan tutar dolu) + Rev.1 taslak (fiyatli, fiyatsiz, elle B.F.li)."""
    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    await kalem(
        client, admin, o["id"], g["id"], katalog[0].id, quantity="10", cost_unit_price="100"
    )
    await gecis(client, admin, o["id"], "send")
    await gecis(client, admin, o["id"], "lose", lost_reason="Fiyat", winning_amount="900.00")
    yeni = (await client.post(f"{URL}/{o['id']}/revisions", headers=admin)).json()
    g1 = yeni["groups"][0]["id"]  # Rev.1'in KENDI grubu (kopya, yeni kimlik)
    k2 = await kalem(
        client,
        admin,
        o["id"],
        g1,
        katalog[2].id,
        rev_no=1,
        quantity="3",
        cost_unit_price="50",
        offer_unit_price="80",
    )
    await kalem(client, admin, o["id"], g1, katalog[1].id, rev_no=1, quantity="2")
    return {"offer_id": o["id"], "group_id": g1, "item_id": k2["id"], "katalog": katalog}


def _para_alanlari(node, yol: str = "", ust: str = "") -> list[tuple[str, object]]:
    bulunan: list[tuple[str, object]] = []
    if isinstance(node, dict):
        for anahtar, deger in node.items():
            tam = f"{yol}.{anahtar}" if yol else anahtar
            if anahtar in PARA_ANAHTARLARI or (anahtar == "profit_pct" and ust == "internal"):
                bulunan.append((tam, deger))
            bulunan += _para_alanlari(deger, tam, anahtar)
    elif isinstance(node, list):
        for i, oge in enumerate(node):
            bulunan += _para_alanlari(oge, f"{yol}[{i}]", ust)
    return bulunan


async def _okumalar(client, headers, dolu) -> dict[str, dict]:
    oid = dolu["offer_id"]
    istekler = {
        "rev1": rev_url(oid, 1),
        "rev0": rev_url(oid, 0),
        "detay": f"{URL}/{oid}",
        "liste": URL,
    }
    sonuc = {}
    for ad, yol in istekler.items():
        resp = await client.get(yol, headers=headers)
        assert resp.status_code == 200, f"{ad}: {resp.text}"
        sonuc[ad] = resp.json()
    return sonuc


# ------------------------------------------------------------------------ kapi


async def test_kimliksiz_istek_401(client) -> None:
    assert (await client.get(URL)).status_code == 401
    assert (await client.post(URL, json={})).status_code == 401
    assert (await client.get(f"{URL}/{uuid.uuid4()}")).status_code == 401


def _tum_uclar(dolu: dict) -> list[tuple[str, str, object]]:
    oid, gid, kid = dolu["offer_id"], dolu["group_id"], dolu["item_id"]
    govde = {"catalog_item_id": str(dolu["katalog"][0].id), "group_id": gid, "quantity": "1"}
    return [
        ("GET", URL, None),
        ("GET", f"{URL}/{oid}", None),
        ("GET", rev_url(oid, 1), None),
        ("POST", URL, {"employer_id": str(uuid.uuid4()), "title": "x"}),
        ("PATCH", f"{URL}/{oid}", {"title": "y"}),
        ("DELETE", f"{URL}/{oid}", None),
        ("POST", f"{URL}/{oid}/revisions", None),
        ("PATCH", rev_url(oid, 1), {"notes": "n"}),
        ("POST", rev_url(oid, 1) + "/send", None),
        ("POST", rev_url(oid, 1) + "/groups", {"name": "g"}),
        ("PATCH", rev_url(oid, 1) + f"/groups/{gid}", {"name": "g"}),
        ("DELETE", rev_url(oid, 1) + f"/groups/{gid}", None),
        ("POST", rev_url(oid, 1) + "/items", govde),
        ("POST", rev_url(oid, 1) + "/items/bulk", {"items": [govde]}),
        ("PATCH", rev_url(oid, 1) + f"/items/{kid}", {"quantity": "2"}),
        ("DELETE", rev_url(oid, 1) + f"/items/{kid}", None),
    ]


@pytest.mark.parametrize("role_key", ["site_chief", "field_engineer"])
async def test_contracts_yok_roller_TUM_uclarda_403(
    client, admin, db_session, user_factory, dolu, role_key
) -> None:
    assert MATRIX["contracts"][ROLE_ORDER.index(role_key)][0] == AccessLevel.none  # on kosul
    kisi = await _giris(client, db_session, user_factory, role_key)
    for yontem, yol, govde in _tum_uclar(dolu):
        resp = await client.request(yontem, yol, json=govde, headers=kisi)
        assert resp.status_code == 403, f"{yontem} {yol}: {resp.status_code}"
    # reddedilen yazmalar hicbir sey degistirmedi
    assert await db_session.scalar(select(func.count()).select_from(Offer)) == 1
    assert (await client.get(rev_url(dolu["offer_id"], 1), headers=admin)).status_code == 200


async def test_contracts_view_okur_ama_YAZAMAZ(
    client, admin, db_session, user_factory, dolu
) -> None:
    await modul_duzeyi_yaz(db_session, "accounting", "contracts", AccessLevel.view)
    muhasebe = await _giris(client, db_session, user_factory, "accounting")
    for yontem, yol, govde in _tum_uclar(dolu):
        resp = await client.request(yontem, yol, json=govde, headers=muhasebe)
        beklenen = 200 if yontem == "GET" else 403
        assert resp.status_code == beklenen, f"{yontem} {yol}: {resp.status_code} {resp.text[:120]}"
    rev = (await client.get(rev_url(dolu["offer_id"], 1), headers=admin)).json()
    assert rev["status"] == "draft" and len(rev["groups"][0]["items"]) == 3  # dokunulmadi
    assert rev["notes"] is None


async def test_proje_basina_disiplinli_kullanici_teklif_modulunu_gorur(
    client, admin, db_session, user_factory, dolu
) -> None:
    """IZN-B3: teklif modulu sirket geneli — bir projede disiplinle kisitli kullanici da okur
    (R5 `RequireUnrestricted` kalkti); hicbir uc disiplin yuzunden 403 vermez."""
    disiplin = EvDiscipline(
        code="KIS", name="Kisitli", color="#2563EB", default_contractor_type=ContractorType.OWN
    )
    db_session.add(disiplin)
    await db_session.flush()
    token = await _login_with_access(
        client, db_session, user_factory, "project_manager", "pm.kisitli.teklif@tkl.co"
    )
    uid = (
        await db_session.execute(select(User.id).where(User.email == "pm.kisitli.teklif@tkl.co"))
    ).scalar_one()
    await baska_projede_disiplinli(db_session, uid, disiplin.id)
    kisitli = _auth(token)
    for yontem, yol, govde in [*_tum_uclar(dolu), ("GET", f"{URL}/settings", None)]:
        resp = await client.request(yontem, yol, json=govde, headers=kisitli)
        if yontem == "DELETE":  # SIL-B1: silme HER KOŞULDA yalnız Sistem Yöneticisi
            assert resp.status_code == 403, f"{yontem} {yol}: {resp.status_code}"
            continue
        assert resp.status_code != 403, f"{yontem} {yol}: {resp.status_code}"
    # POZITIF KONTROL: ayni rol (project_manager), disiplin atamasi YOK → okumalar 200
    serbest = _auth(
        await _login_with_access(
            client, db_session, user_factory, "project_manager", "pm.serbest.teklif@tkl.co"
        )
    )
    for yol in (URL, f"{URL}/{dolu['offer_id']}", rev_url(dolu["offer_id"], 1), f"{URL}/settings"):
        resp = await client.get(yol, headers=serbest)
        assert resp.status_code == 200, f"GET {yol}: {resp.status_code}"


async def test_gecis_uclari_da_kisitliya_kapali(
    client, admin, db_session, user_factory, isveren
) -> None:
    await modul_duzeyi_yaz(db_session, "accounting", "contracts", AccessLevel.view)
    o = await teklif(client, admin, isveren)
    muhasebe = await _giris(client, db_session, user_factory, "accounting")
    for eylem in ("send", "win", "lose", "withdraw"):
        resp = await client.post(rev_url(o["id"]) + f"/{eylem}", headers=muhasebe)
        assert resp.status_code == 403, eylem
    await durum_yap(client, admin, o["id"], "draft")  # hala taslak


# ----------------------------------------------------------------------- maske


async def test_limited_kapsamda_TUM_para_alanlari_None_diger_alanlar_gorunur(
    client, admin, db_session, user_factory, dolu
) -> None:
    yonetici = await _okumalar(client, admin, dolu)
    # on kosul: yonetici icin para alanlari VAR ve doludur (test bos degil)
    for ad, govde in yonetici.items():
        alanlar = _para_alanlari(govde)
        assert alanlar, f"{ad}: para anahtari bulunamadi — tarayici bos"
        assert any(v is not None for _, v in alanlar), f"{ad}: yonetici hic para gormuyor"
    assert D(yonetici["rev1"]["totals"]["customer"]["net"]) == D("1528.00")

    await modul_duzeyi_yaz(db_session, "accounting", "contracts", AccessLevel.view)
    await _gizle(db_session, "accounting", HiddenCategory.tum_tutarlar)
    sinirli = await _giris(client, db_session, user_factory, "accounting")
    gorulen = await _okumalar(client, sinirli, dolu)
    for ad, govde in gorulen.items():
        sizan = [(yol, v) for yol, v in _para_alanlari(govde) if v is not None]
        assert not sizan, f"{ad}: limited kapsamda para SIZDI: {sizan}"
        assert len(_para_alanlari(govde)) == len(
            _para_alanlari(yonetici[ad])
        )  # alan DUSMEDI, None oldu

    rev = gorulen["rev1"]
    # KDV orani, adam-saat, miktar, durumlar GORUNUR; genel gider/kar orani maliyet_kar
    # (tum_tutarlar onu da kapsar) → None: maliyet `net`ten geri hesaplanamasin (GECE KARARI).
    assert rev["overhead_pct"] is None and rev["profit_pct"] is None
    assert D(rev["vat_pct"]) == D(20)
    kalemler = [k for g in rev["groups"] for k in g["items"]]
    assert {D(k["quantity"]) for k in kalemler} == {D(10), D(3), D(2)}
    assert all(k["internal"]["man_hours"] is not None for k in kalemler)
    assert rev["totals"]["internal"]["man_hours"] is not None
    assert rev["totals"]["unpriced_count"] == 1 and rev["status"] == "draft"
    # zarf: sayac/oran gorunur
    assert {c["status"]: c["count"] for c in gorulen["liste"]["summary"]["by_status"]}["draft"] == 1
    assert gorulen["liste"]["summary"]["expired_count"] == 0
    assert gorulen["rev0"]["lost_reason"] == "Fiyat"  # metin gorunur, tutar degil


async def test_yalniz_maliyet_kar_gizliyken_musteri_fiyati_gorunur_maliyet_ve_oranlar_None(
    client, admin, db_session, user_factory, dolu
) -> None:
    """IZN-B4: eski `finance` kapsami karsiliksiz. Kategoriler BAGIMSIZDIR: `maliyet_kar` gizliyken
    musteriye verilen fiyat (`sozlesme_fiyat`) GORUNUR; maliyet birim fiyati ve oranlar None."""
    await modul_duzeyi_yaz(db_session, "accounting", "contracts", AccessLevel.view)
    await _gizle(db_session, "accounting", HiddenCategory.maliyet_kar)
    muhasebe = await _giris(client, db_session, user_factory, "accounting")
    rev = (await client.get(rev_url(dolu["offer_id"], 1), headers=muhasebe)).json()
    kalemler = [k for g in rev["groups"] for k in g["items"]]
    assert {D(k["quantity"]) for k in kalemler} == {D(10), D(3), D(2)}  # miktar gizlenmez
    assert D(rev["totals"]["customer"]["net"]) == D("1528.00")  # musteri fiyati GORUNUR
    assert rev["overhead_pct"] is None and rev["profit_pct"] is None
    assert all(k["cost_unit_price"] is None for k in kalemler)

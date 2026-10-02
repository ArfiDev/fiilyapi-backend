"""TKL-B5.1 — teklif sablonu uclari (`/offers/templates`): CRUD, tek varsayilan, tekliften
sablon, sablondan kopya, izin kapisi, denetim satirlari ve ROTA SIRASI.

Sablon FIYAT/MIKTAR TASIMAZ (T12). Kullanim sayisi `offers.template_id` bagindan turer.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.core.access import AccessLevel, Scope
from app.core.router_registry import ROUTERS
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.catalog.models import ContractorType, EvDiscipline
from app.modules.earned_value.models import UserDiscipline
from app.modules.offers.models import OfferTemplate
from app.modules.offers.router import router as offers_router
from app.modules.offers.template_router import router as templates_router
from app.modules.users.models import User

from .._boq import _audit_details, _auth, _login_with_access, _set_permission
from ._offers import (
    TPL,
    D,
    grup,
    kalem,
    sablon,
    sablon_icerik,
    sablon_kalemleri,
    teklif,
)

# --------------------------------------------------------------- rota sirasi (BEKCI)


def test_ROTA_SIRASI_sablon_router_offers_routerindan_ONCE() -> None:
    """`/offers/templates` literaldir; `/offers/{offer_id}`den ONCE kayitli olmali."""
    sirali = list(ROUTERS)
    assert sirali.index(templates_router) < sirali.index(offers_router)


async def test_GET_templates_dinamik_offer_id_yoluna_DUSMEZ(client, admin) -> None:
    resp = await client.get(TPL, headers=admin)
    assert resp.status_code == 200, resp.text  # 422 (UUID ayristirma) olsaydi golgelenmisti
    assert resp.json() == {"items": [], "total": 0}


# ----------------------------------------------------------------------------- CRUD


async def test_olustur_bos_sablon_ve_listede_gorunur(client, admin) -> None:
    s = await sablon(client, admin, "Villa Kaba", description="Müstakil villa", profit_pct="18")
    assert s["name"] == "Villa Kaba" and s["is_default"] is False
    assert D(s["profit_pct"]) == D("18") and s["overhead_pct"] is None
    assert s["groups"] == [] and s["usage_count"] == 0
    liste = (await client.get(TPL, headers=admin)).json()
    assert liste["total"] == 1
    satir = liste["items"][0]
    assert (satir["group_count"], satir["item_count"], satir["usage_count"]) == (0, 0, 0)
    assert "cost_unit_price" not in str(satir) and "quantity" not in str(satir)  # fiyat/miktar YOK


@pytest.mark.parametrize(
    "govde",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 81},
        {},
        {"name": "A", "profit_pct": "1000"},
        {"name": "A", "overhead_pct": "101"},
        {"name": "A", "extra": 1},
    ],
)
async def test_olustur_gecersiz_govde_422(client, admin, govde) -> None:
    resp = await client.post(TPL, json=govde, headers=admin)
    assert resp.status_code == 422, resp.text


async def test_ad_80_karakter_kabul(client, admin) -> None:
    await sablon(client, admin, "x" * 80)


async def test_detay_404(client, admin) -> None:
    assert (await client.get(f"{TPL}/{uuid.uuid4()}", headers=admin)).status_code == 404


async def test_icerik_degistir_sirayla_kalir_ve_tekrar_degistirince_eskisi_gider(
    client, admin, katalog
) -> None:
    s = await sablon(client, admin)
    d = await sablon_icerik(
        client,
        admin,
        s["id"],
        [("Kaba", [katalog[2].id, katalog[0].id]), ("İnce", [katalog[1].id])],
    )
    assert [g["name"] for g in d["groups"]] == ["Kaba", "İnce"]
    assert sablon_kalemleri(d) == [
        [katalog[2].poz_no, katalog[0].poz_no],
        [katalog[1].poz_no],
    ]
    assert d["groups"][0]["items"][0]["description"] == "Demir"
    assert (d["group_count"], d["item_count"]) == (2, 3)

    d2 = await sablon_icerik(client, admin, s["id"], [("Tek", [katalog[1].id])])
    assert sablon_kalemleri(d2) == [[katalog[1].poz_no]]
    assert (d2["group_count"], d2["item_count"]) == (1, 1)
    d3 = await sablon_icerik(client, admin, s["id"], [])
    assert d3["groups"] == [] and d3["item_count"] == 0


async def test_icerik_bilinmeyen_katalog_404_hicbir_sey_degismez(client, admin, katalog) -> None:
    s = await sablon(client, admin)
    await sablon_icerik(client, admin, s["id"], [("Kaba", [katalog[0].id])])
    govde = {"groups": [{"name": "Y", "items": [{"catalog_item_id": str(uuid.uuid4())}]}]}
    resp = await client.put(f"{TPL}/{s['id']}/content", json=govde, headers=admin)
    assert resp.status_code == 404, resp.text
    assert sablon_kalemleri((await client.get(f"{TPL}/{s['id']}", headers=admin)).json()) == [
        [katalog[0].poz_no]
    ]


async def test_icerik_bos_grup_adi_422_ve_fiyat_miktar_alani_422(client, admin, katalog) -> None:
    s = await sablon(client, admin)
    for govde in (
        {"groups": [{"name": " ", "items": []}]},
        {
            "groups": [
                {"name": "G", "items": [{"catalog_item_id": str(katalog[0].id), "quantity": "1"}]}
            ]
        },
    ):
        resp = await client.put(f"{TPL}/{s['id']}/content", json=govde, headers=admin)
        assert resp.status_code == 422, resp.text


async def test_patch_alanlar_ve_null_temizler(client, admin, db_session) -> None:
    s = await sablon(client, admin, "A", description="x", overhead_pct="10", profit_pct="20")
    resp = await client.patch(
        f"{TPL}/{s['id']}",
        json={"name": "B", "description": None, "profit_pct": None, "overhead_pct": "11"},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["name"] == "B" and d["description"] is None and d["profit_pct"] is None
    assert D(d["overhead_pct"]) == D("11")
    assert await _audit_details(db_session, AuditAction.update) == [
        messages.offer_template_updated("B")
    ]


async def test_patch_ad_null_veya_is_default_null_422(client, admin) -> None:
    s = await sablon(client, admin)
    for govde in ({"name": None}, {"is_default": None}, {"name": ""}):
        resp = await client.patch(f"{TPL}/{s['id']}", json=govde, headers=admin)
        assert resp.status_code == 422, (govde, resp.text)


async def test_patch_degisiklik_yoksa_denetim_satiri_YAZILMAZ(client, admin, db_session) -> None:
    s = await sablon(client, admin, "A", overhead_pct="10")
    resp = await client.patch(
        f"{TPL}/{s['id']}", json={"name": "A", "overhead_pct": "10.00"}, headers=admin
    )
    assert resp.status_code == 200
    assert await _audit_details(db_session, AuditAction.update) == []


async def test_sil_sablon_gider_teklifler_KALIR_template_id_NULL(client, admin, isveren) -> None:
    s = await sablon(client, admin)
    o = await _sablondan(client, admin, isveren, s["id"])
    assert o["template_id"] == s["id"]
    assert (await client.delete(f"{TPL}/{s['id']}", headers=admin)).status_code == 204
    assert (await client.get(f"{TPL}/{s['id']}", headers=admin)).status_code == 404
    assert (await client.delete(f"{TPL}/{s['id']}", headers=admin)).status_code == 404
    sonra = (await client.get(f"/offers/{o['id']}", headers=admin)).json()
    assert sonra["template_id"] is None and sonra["offer_no"] == o["offer_no"]


async def _sablondan(client, admin, isveren, template_id: str, **over) -> dict:
    govde = {"employer_id": str(isveren.id), "title": "Şablonlu iş", "template_id": template_id}
    resp = await client.post("/offers", json={**govde, **over}, headers=admin)
    assert resp.status_code == 201, resp.text
    return resp.json()


# -------------------------------------------------------------------- tek varsayilan


async def _varsayilanlar(client, admin) -> list[str]:
    liste = (await client.get(TPL, headers=admin)).json()["items"]
    return [s["name"] for s in liste if s["is_default"]]


async def test_varsayilan_yap_eskisi_AYNI_islemde_duser_tek_varsayilan(
    client, admin, db_session
) -> None:
    a = await sablon(client, admin, "A")
    b = await sablon(client, admin, "B")
    r = await client.post(f"{TPL}/{a['id']}/default", headers=admin)
    assert r.status_code == 200 and r.json()["is_default"] is True
    assert await _varsayilanlar(client, admin) == ["A"]

    r = await client.post(f"{TPL}/{b['id']}/default", headers=admin)
    assert r.status_code == 200 and r.json()["is_default"] is True
    assert await _varsayilanlar(client, admin) == ["B"]  # A dustu
    assert (await client.get(f"{TPL}/{a['id']}", headers=admin)).json()["is_default"] is False
    sayi = await db_session.scalar(
        select(func.count()).select_from(OfferTemplate).where(OfferTemplate.is_default.is_(True))
    )
    assert sayi == 1
    # liste: varsayilan en ustte
    assert [s["name"] for s in (await client.get(TPL, headers=admin)).json()["items"]] == ["B", "A"]


async def test_varsayilan_yap_zaten_varsayilansa_degisiklik_ve_denetim_YOK(
    client, admin, db_session
) -> None:
    a = await sablon(client, admin, "A")
    await client.post(f"{TPL}/{a['id']}/default", headers=admin)
    onceki = await _audit_details(db_session, AuditAction.update)
    assert onceki == [messages.offer_template_default_set("A")]
    r = await client.post(f"{TPL}/{a['id']}/default", headers=admin)
    assert r.status_code == 200 and r.json()["is_default"] is True
    assert await _audit_details(db_session, AuditAction.update) == onceki


async def test_patch_is_default_true_da_eskisini_dusurur_false_kaldirir(client, admin) -> None:
    a = await sablon(client, admin, "A")
    b = await sablon(client, admin, "B")
    await client.post(f"{TPL}/{a['id']}/default", headers=admin)
    r = await client.patch(f"{TPL}/{b['id']}", json={"is_default": True}, headers=admin)
    assert r.status_code == 200 and r.json()["is_default"] is True
    assert await _varsayilanlar(client, admin) == ["B"]
    r = await client.patch(f"{TPL}/{b['id']}", json={"is_default": False}, headers=admin)
    assert r.json()["is_default"] is False
    assert await _varsayilanlar(client, admin) == []


async def test_varsayilan_yap_404(client, admin) -> None:
    resp = await client.post(f"{TPL}/{uuid.uuid4()}/default", headers=admin)
    assert resp.status_code == 404


# ------------------------------------------------------------------- tekliften sablon


async def _dolu_teklif(client, admin, isveren, katalog) -> dict:
    o = await teklif(client, admin, isveren)
    resp = await client.patch(
        f"/offers/{o['id']}/revisions/0",
        json={"overhead_pct": "9", "profit_pct": "21"},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    g1 = await grup(client, admin, o["id"], name="Kaba")
    g2 = await grup(client, admin, o["id"], name="İnce")
    await kalem(
        client, admin, o["id"], g1["id"], katalog[0].id,
        quantity="10", cost_unit_price="100", offer_unit_price="150",
    )  # fmt: skip
    await kalem(client, admin, o["id"], g1["id"], katalog[2].id, quantity="3")
    await kalem(client, admin, o["id"], g2["id"], katalog[1].id, quantity="7", cost_unit_price="5")
    return o


async def test_tekliften_sablon_gruplar_sira_oranlar_VAR_fiyat_miktar_YOK(
    client, admin, isveren, katalog, db_session
) -> None:
    o = await _dolu_teklif(client, admin, isveren, katalog)
    resp = await client.post(
        f"{TPL}/from-offer",
        json={"offer_id": o["id"], "rev_no": 0, "name": "Tekliften", "description": "d"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    d = resp.json()
    assert d["name"] == "Tekliften" and d["is_default"] is False
    assert (D(d["overhead_pct"]), D(d["profit_pct"])) == (D("9"), D("21"))
    assert [g["name"] for g in d["groups"]] == ["Kaba", "İnce"]
    assert sablon_kalemleri(d) == [
        [katalog[0].poz_no, katalog[2].poz_no],
        [katalog[1].poz_no],
    ]
    metin = str(d)
    for yasak in ("quantity", "cost_unit_price", "offer_unit_price", "unit_price", "amount"):
        assert yasak not in metin, yasak
    assert messages.offer_template_from_offer("Tekliften", o["offer_no"], 0) in (
        await _audit_details(db_session, AuditAction.create)
    )


async def test_tekliften_sablon_kaynak_teklif_DEGISMEZ_ve_audit(
    client, admin, isveren, katalog, db_session
) -> None:
    o = await _dolu_teklif(client, admin, isveren, katalog)
    once = (await client.get(f"/offers/{o['id']}/revisions/0", headers=admin)).json()
    await client.post(
        f"{TPL}/from-offer", json={"offer_id": o["id"], "rev_no": 0, "name": "T"}, headers=admin
    )
    sonra = (await client.get(f"/offers/{o['id']}/revisions/0", headers=admin)).json()
    assert once == sonra
    kayitlar = await _audit_details(db_session, AuditAction.create)
    assert messages.offer_template_from_offer("T", o["offer_no"], 0) in kayitlar


async def test_tekliften_sablon_404ler_ve_ad_zorunlu(client, admin, isveren, katalog) -> None:
    o = await _dolu_teklif(client, admin, isveren, katalog)
    r = await client.post(
        f"{TPL}/from-offer", json={"offer_id": str(uuid.uuid4()), "rev_no": 0, "name": "T"},
        headers=admin,
    )  # fmt: skip
    assert r.status_code == 404
    r = await client.post(
        f"{TPL}/from-offer", json={"offer_id": o["id"], "rev_no": 5, "name": "T"}, headers=admin
    )
    assert r.status_code == 404
    r = await client.post(
        f"{TPL}/from-offer", json={"offer_id": o["id"], "rev_no": 0}, headers=admin
    )
    assert r.status_code == 422
    assert (await client.get(TPL, headers=admin)).json()["total"] == 0  # hicbiri yazilmadi


async def test_tekliften_sablon_gonderilmis_revizyondan_da_olur(
    client, admin, isveren, katalog
) -> None:
    o = await _dolu_teklif(client, admin, isveren, katalog)
    ok = await client.patch(
        f"/offers/{o['id']}/revisions/0", json={"offer_date": "2026-10-01"}, headers=admin
    )
    assert ok.status_code == 200
    # miktari dolu kalemler: gonderilebilir
    assert (
        await client.post(f"/offers/{o['id']}/revisions/0/send", headers=admin)
    ).status_code == 200
    r = await client.post(
        f"{TPL}/from-offer", json={"offer_id": o["id"], "rev_no": 0, "name": "G"}, headers=admin
    )
    assert r.status_code == 201, r.text


# ------------------------------------------------------------------ sablondan kopya


async def test_sablon_kopyasi_icerik_ve_oranlar_birebir_varsayilan_DEGIL(
    client, admin, katalog, db_session
) -> None:
    s = await sablon(
        client, admin, "Orijinal", description="açık", overhead_pct="8", profit_pct="16"
    )
    await sablon_icerik(
        client, admin, s["id"], [("A", [katalog[0].id, katalog[1].id]), ("B", [katalog[2].id])]
    )
    await client.post(f"{TPL}/{s['id']}/default", headers=admin)
    r = await client.post(f"{TPL}/{s['id']}/copy", headers=admin)
    assert r.status_code == 201, r.text
    k = r.json()
    assert k["id"] != s["id"] and k["name"] == "Orijinal (kopya)"
    assert k["is_default"] is False
    assert k["description"] == "açık" and (D(k["overhead_pct"]), D(k["profit_pct"])) == (
        D("8"),
        D("16"),
    )
    assert sablon_kalemleri(k) == [
        [katalog[0].poz_no, katalog[1].poz_no],
        [katalog[2].poz_no],
    ]
    assert {g["id"] for g in k["groups"]}.isdisjoint({"x"})
    asil = (await client.get(f"{TPL}/{s['id']}", headers=admin)).json()
    assert asil["is_default"] is True and asil["item_count"] == 3  # kaynak degismedi
    assert k["groups"][0]["id"] != asil["groups"][0]["id"]  # yeni kimlikler
    assert await _varsayilanlar(client, admin) == ["Orijinal"]
    assert messages.offer_template_copied("Orijinal (kopya)", "Orijinal") in await _audit_details(
        db_session, AuditAction.create
    )


async def test_sablon_kopyasi_ad_verilebilir_80_siniri(client, admin) -> None:
    s = await sablon(client, admin, "x" * 80)
    r = await client.post(f"{TPL}/{s['id']}/copy", json={"name": "Özel"}, headers=admin)
    assert r.json()["name"] == "Özel"
    r = await client.post(f"{TPL}/{s['id']}/copy", headers=admin)
    assert r.status_code == 201 and len(r.json()["name"]) == 80  # kisaltilir, 80'i asmaz
    r = await client.post(f"{TPL}/{s['id']}/copy", json={"name": "y" * 81}, headers=admin)
    assert r.status_code == 422
    assert (await client.post(f"{TPL}/{uuid.uuid4()}/copy", headers=admin)).status_code == 404


# ------------------------------------------------------------------------- denetim


async def test_denetim_satirlari_olustur_icerik_sil(client, admin, katalog, db_session) -> None:
    s = await sablon(client, admin, "Denetim")
    await sablon_icerik(client, admin, s["id"], [("G", [katalog[0].id, katalog[1].id])])
    await client.delete(f"{TPL}/{s['id']}", headers=admin)
    assert await _audit_details(db_session, AuditAction.create) == [
        messages.offer_template_created("Denetim")
    ]
    assert await _audit_details(db_session, AuditAction.update) == [
        messages.offer_template_content_replaced("Denetim", 1, 2)
    ]
    assert await _audit_details(db_session, AuditAction.delete) == [
        messages.offer_template_deleted("Denetim")
    ]


# ------------------------------------------------------------------------ izin kapisi


async def _giris(client, db_session, user_factory, rol: str) -> dict[str, str]:
    token = await _login_with_access(
        client, db_session, user_factory, rol, f"{rol}.{uuid.uuid4().hex[:6]}@tkl.co"
    )
    return _auth(token)


def _tum_uclar(sablon_id: str) -> list[tuple[str, str, dict | None]]:
    return [
        ("GET", TPL, None),
        ("GET", f"{TPL}/{sablon_id}", None),
        ("POST", TPL, {"name": "Yeni"}),
        ("PATCH", f"{TPL}/{sablon_id}", {"name": "Değişti"}),
        ("PUT", f"{TPL}/{sablon_id}/content", {"groups": []}),
        ("POST", f"{TPL}/{sablon_id}/default", None),
        ("POST", f"{TPL}/{sablon_id}/copy", None),
        ("DELETE", f"{TPL}/{sablon_id}", None),
    ]


async def test_contracts_none_rol_HEPSI_403(client, admin, db_session, user_factory) -> None:
    s = await sablon(client, admin)
    kisi = await _giris(client, db_session, user_factory, "site_chief")
    for yontem, yol, govde in _tum_uclar(s["id"]):
        resp = await client.request(yontem, yol, json=govde, headers=kisi)
        assert resp.status_code == 403, f"{yontem} {yol}: {resp.status_code}"
    assert (await client.get(f"{TPL}/{s['id']}", headers=admin)).json()["name"] == s["name"]


async def test_contracts_view_okur_ama_YAZAMAZ(client, admin, db_session, user_factory) -> None:
    s = await sablon(client, admin)
    await _set_permission(db_session, "accounting", "contracts", AccessLevel.view, Scope.all)
    muhasebe = await _giris(client, db_session, user_factory, "accounting")
    for yontem, yol, govde in _tum_uclar(s["id"]):
        resp = await client.request(yontem, yol, json=govde, headers=muhasebe)
        beklenen = 200 if yontem == "GET" else 403
        assert resp.status_code == beklenen, f"{yontem} {yol}: {resp.status_code}"
    assert (await client.get(f"{TPL}/{s['id']}", headers=admin)).json()["is_default"] is False
    assert (await client.post(f"{TPL}/from-offer", json={"offer_id": str(uuid.uuid4()),
        "rev_no": 0, "name": "x"}, headers=muhasebe)).status_code == 403  # fmt: skip


async def test_kisitli_disiplinli_kullanici_okur_ama_yazamaz(
    client, admin, db_session, user_factory
) -> None:
    s = await sablon(client, admin)
    disiplin = EvDiscipline(
        code="KSB", name="Kisitli", color="#2563EB", default_contractor_type=ContractorType.OWN
    )
    db_session.add(disiplin)
    await db_session.flush()
    token = await _login_with_access(
        client, db_session, user_factory, "project_manager", "pm.kisitli.sablon@tkl.co"
    )
    uid = (
        await db_session.execute(select(User.id).where(User.email == "pm.kisitli.sablon@tkl.co"))
    ).scalar_one()
    db_session.add(UserDiscipline(user_id=uid, discipline_id=disiplin.id))
    await db_session.flush()
    kisitli = _auth(token)
    for yontem, yol, govde in _tum_uclar(s["id"]):
        resp = await client.request(yontem, yol, json=govde, headers=kisitli)
        beklenen = 200 if yontem == "GET" else 403
        assert resp.status_code == beklenen, f"{yontem} {yol}: {resp.status_code}"


# --------------------------------------------------------------- tavan (servis duzeyi, V3)


async def test_tekliften_sablon_101_grup_422_ve_sablon_YAZILMAZ(
    client, admin, isveren, db_session
) -> None:
    from app.modules.offers.template_schemas import TEMPLATE_GROUPS_MAX, TEMPLATE_GROUPS_TOO_MANY

    o = await teklif(client, admin, isveren)
    for i in range(TEMPLATE_GROUPS_MAX + 1):
        await grup(client, admin, o["id"], name=f"G{i}")
    once = (await db_session.execute(select(func.count()).select_from(OfferTemplate))).scalar_one()
    resp = await client.post(
        f"{TPL}/from-offer", json={"offer_id": o["id"], "rev_no": 0, "name": "Cok"}, headers=admin
    )
    assert resp.status_code == 422, resp.text
    assert TEMPLATE_GROUPS_TOO_MANY in resp.text
    sonra = (await db_session.execute(select(func.count()).select_from(OfferTemplate))).scalar_one()
    assert sonra == once  # hep-ya-hic: yarim sablon kalmaz


async def test_servis_yazma_yolu_1001_kalemde_Turkce_422_hatasi() -> None:
    from app.core.errors import OfferValidationError
    from app.modules.offers.template_schemas import TEMPLATE_ITEMS_MAX, TEMPLATE_ITEMS_TOO_MANY
    from app.modules.offers.template_service import _replace_rows

    cok = [("G", [uuid.uuid4() for _ in range(TEMPLATE_ITEMS_MAX + 1)])]
    # tavan DB'ye dokunmadan once uygulanir (oturum gerekmez)
    with pytest.raises(OfferValidationError, match=TEMPLATE_ITEMS_TOO_MANY):
        await _replace_rows(None, None, cok)  # type: ignore[arg-type]

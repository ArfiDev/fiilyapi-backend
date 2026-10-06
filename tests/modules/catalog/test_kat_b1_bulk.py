# ruff: noqa: F811  (admin/kab fiksturleri test_catalog_items_api.py'den ithal edilir)
"""KAT-B1 — `POST /catalog/items/bulk` (hep-ya-hic toplu ekleme + kaynak kodu ile fiyat guncelleme).

Eszamanlilik: `test_kat_b1_bulk_race.py`. Tekil semantik: `test_kat_b1_kaynak_kod.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.core.access import AccessLevel
from app.modules.audit.models import AuditLog
from app.modules.catalog.models import EvCatalogItem, EvDiscipline
from app.modules.users.models import User
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz
from tests._proje_ekibi import baska_projede_disiplinli

from .._boq import _auth, _login_with_access
from .test_catalog_items_api import (  # noqa: F401  (fikstur + yardimcilar yeniden kullanilir)
    URL,
    _disiplin,
    _giris,
    _govde,
    admin,
    kab,
)

pytestmark = pytest.mark.asyncio

BULK = f"{URL}/bulk"


def _kalem(disiplin, name: str, **over) -> dict:
    return _govde(disiplin, name=name, **over)


async def _toplu(client, headers, items: list[dict], **ust):
    return await client.post(BULK, json={"items": items, **ust}, headers=headers)


async def _sayilar(db_session) -> tuple[int, list[int]]:
    adet = await db_session.scalar(select(func.count()).select_from(EvCatalogItem))
    sayac = list(
        await db_session.scalars(select(EvDiscipline.poz_counter).order_by(EvDiscipline.code))
    )
    return adet, sayac


async def _denetim_adedi(db_session) -> int:
    return await db_session.scalar(select(func.count()).select_from(AuditLog))


# ------------------------------------------------------------ mutlu yol


async def test_toplu_ekle_poz_no_sunucu_uretir_disiplin_basina_ardisik_tek_denetim_satiri(
    client, admin, kab, db_session
) -> None:
    elk = await _disiplin(db_session, "ELK", "Elektrik")
    onceki = await _denetim_adedi(db_session)
    resp = await _toplu(
        client,
        admin,
        [
            _kalem(
                kab,
                "Beton",
                source_code="15.100.1001",
                ref_price="100",
                ref_price_date="2026-01-01",
            ),
            _kalem(elk, "Kablo", source_code=" 15.100.1002 "),
            _kalem(kab, "Kalıp"),
        ],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["created"], body["updated"], body["unchanged"]) == (3, 0, 0)
    assert [(r["index"], r["poz_no"], r["action"]) for r in body["items"]] == [
        (0, "KAB-0001", "created"),
        (1, "ELK-0001", "created"),
        (2, "KAB-0002", "created"),
    ]
    listed = {i["poz_no"]: i for i in (await client.get(URL, headers=admin)).json()["items"]}
    assert listed["KAB-0001"]["source_code"] == "15.100.1001"
    assert listed["KAB-0001"]["ref_price_date"] == "2026-01-01"
    assert listed["KAB-0001"]["price_updated_at"] is not None
    assert listed["ELK-0001"]["source_code"] == "15.100.1002"  # kirpildi
    assert listed["KAB-0002"]["ref_price"] is None
    assert await _sayilar(db_session) == (3, [1, 2])  # ELK, KAB (kod sirasi)
    # TEK ozet denetim satiri (sayilar + disiplinler)
    assert await _denetim_adedi(db_session) == onceki + 1
    detaylar = list(
        await db_session.scalars(
            select(AuditLog.detail).where(AuditLog.detail.like("%toplu aktarım%"))
        )
    )
    assert len(detaylar) == 1
    assert "3 kalem eklendi" in detaylar[0] and "disiplinler: ELK, KAB" in detaylar[0]


async def test_tavan_200_gecer_201_ve_bos_liste_ve_fazla_alan_422(client, admin, kab) -> None:
    items = [_kalem(kab, f"K{i}") for i in range(200)]
    ok = await _toplu(client, admin, items)
    assert ok.status_code == 201, ok.text
    assert [r["poz_no"] for r in ok.json()["items"]][-1] == "KAB-0200"
    assert (await _toplu(client, admin, [*items, _kalem(kab, "Fazla")])).status_code == 422
    assert (await _toplu(client, admin, [])).status_code == 422
    assert (await _toplu(client, admin, [_kalem(kab, "Y")], bilinmeyen=1)).status_code == 422
    govdede_poz = _kalem(kab, "Z", poz_no="KAB-0999")
    assert (await _toplu(client, admin, [govdede_poz])).status_code == 422
    assert (
        await _toplu(client, admin, [_kalem(kab, "W")], on_source_conflict="sil")
    ).status_code == 422


# ------------------------------------------------------------ hep-ya-hic


async def test_200_ogeden_sonuncusu_hatali_hicbir_satir_yazilmaz_sayac_ilerlemez(
    client, admin, kab, db_session
) -> None:
    items = [_kalem(kab, f"K{i}") for i in range(199)]
    items.append(_kalem(kab, "k0"))  # K0 ile ayni ad+birim (buyuk/kucuk harf farki)
    onceki = await _denetim_adedi(db_session)
    resp = await _toplu(client, admin, items)
    assert resp.status_code == 422, resp.text
    errors = resp.json()["errors"]
    assert [e["loc"] for e in errors] == [["body", "items", 199, "name"]]
    assert "satır 1" in errors[0]["message"]
    assert await _sayilar(db_session) == (0, [0])
    assert await _denetim_adedi(db_session) == onceki  # reddedilen istek denetim yazmaz


async def test_db_ile_ad_birim_cakismasi_dogru_indeksle_raporlanir(
    client, admin, kab, db_session
) -> None:
    await client.post(URL, json=_govde(kab, name="Beton"), headers=admin)
    resp = await _toplu(client, admin, [_kalem(kab, "A"), _kalem(kab, "B"), _kalem(kab, " BETON ")])
    assert resp.status_code == 422, resp.text
    errors = resp.json()["errors"]
    assert [e["loc"] for e in errors] == [["body", "items", 2, "name"]]
    assert "Beton" in errors[0]["message"]
    assert await _sayilar(db_session) == (1, [1])  # yalniz onceki tekil kalem


async def test_tum_hatalar_birlikte_toplanir_indekse_gore_sirali(
    client, admin, kab, db_session
) -> None:
    await client.post(URL, json=_govde(kab, name="Var", source_code="15.100.1"), headers=admin)
    yok = str(uuid.uuid4())
    resp = await _toplu(
        client,
        admin,
        [
            _kalem(kab, "Tamam"),
            {**_kalem(kab, "Disiplinsiz"), "discipline_id": yok},
            _kalem(kab, "Kod1", source_code="15.100.9"),
            _kalem(kab, "Kod2", source_code="15.100.9"),  # istek ici tekrar
            _kalem(kab, "KodVar", source_code="15.100.1"),  # DB'de var (error kipi)
            _kalem(kab, "TarihliFiyatsiz", ref_price_date="2026-01-01"),
        ],
    )
    assert resp.status_code == 422, resp.text
    locs = [e["loc"] for e in resp.json()["errors"]]
    assert locs == [
        ["body", "items", 1, "discipline_id"],
        ["body", "items", 3, "source_code"],
        ["body", "items", 4, "source_code"],
        ["body", "items", 5, "ref_price_date"],
    ]
    assert "satır 3" in resp.json()["errors"][1]["message"]
    assert "update_price" in resp.json()["errors"][2]["message"]
    assert await _sayilar(db_session) == (1, [1])


async def test_error_kipinde_mevcut_kaynak_kodu_yeni_kalemleri_de_engeller(
    client, admin, kab, db_session
) -> None:
    await client.post(URL, json=_govde(kab, name="Var", source_code="K.1"), headers=admin)
    resp = await _toplu(
        client, admin, [_kalem(kab, "Yeni"), _kalem(kab, "Var2", source_code="K.1")]
    )
    assert resp.status_code == 422
    assert await _sayilar(db_session) == (1, [1])


# ------------------------------------------------------------ update_price


async def _tohum(client, admin, kab) -> dict[str, dict]:
    """Dort kalem: A (10, 2025), B (10, 2025), C (10, 2025), D (fiyatsiz)."""
    out = {}
    for ad in "ABC":
        r = await client.post(
            URL,
            json=_govde(
                kab,
                name=f"Kalem {ad}",
                source_code=f"S.{ad}",
                ref_price="10.00",
                ref_price_date="2025-01-01",
                standard_unit_mhr="2.0000",
            ),
            headers=admin,
        )
        out[ad] = r.json()
    r = await client.post(URL, json=_govde(kab, name="Kalem D", source_code="S.D"), headers=admin)
    out["D"] = r.json()
    return out


async def test_update_price_yalniz_fiyat_ve_tarihi_gunceller_action_alanlari_dogru(
    client, admin, kab, db_session
) -> None:
    seed = await _tohum(client, admin, kab)
    resp = await _toplu(
        client,
        admin,
        [
            # A: fiyat degisir + tarih verilir; ad/birim/mhr FARKLI gelir → yok sayilmali
            _kalem(
                kab,
                "BASKA AD",
                uom="kg",
                standard_unit_mhr="9.0000",
                default_contractor_type="subcon",
                description="yeni",
                source_code="S.A",
                ref_price="12.00",
                ref_price_date="2026-01-01",
            ),
            # B: fiyat AYNI, tarih degisir → yalniz tarih, damga ayni
            _kalem(kab, "B", source_code="S.B", ref_price="10.0", ref_price_date="2026-01-01"),
            # C: fiyat AYNI, tarih yok → degismedi (eski tarih korunur)
            _kalem(kab, "C", source_code="S.C", ref_price="10.00"),
            # D: fiyat gonderilmedi → degismedi (mevcut fiyat/tarih silinmez)
            _kalem(kab, "D", source_code="S.D"),
            # yeni kalem
            _kalem(kab, "Yeni", source_code="S.E", ref_price="5.00", ref_price_date="2026-01-01"),
            # kodsuz yeni kalem
            _kalem(kab, "Kodsuz"),
        ],
        on_source_conflict="update_price",
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["created"], body["updated"], body["unchanged"]) == (2, 2, 2)
    assert [(r["index"], r["action"]) for r in body["items"]] == [
        (0, "price_updated"),
        (1, "price_updated"),
        (2, "unchanged"),
        (3, "unchanged"),
        (4, "created"),
        (5, "created"),
    ]
    assert [r["poz_no"] for r in body["items"]][:4] == [seed[k]["poz_no"] for k in "ABCD"]
    assert [r["poz_no"] for r in body["items"]][4:] == ["KAB-0005", "KAB-0006"]
    assert [r["id"] for r in body["items"]][:4] == [seed[k]["id"] for k in "ABCD"]

    now = {i["poz_no"]: i for i in (await client.get(URL, headers=admin)).json()["items"]}
    a = now[seed["A"]["poz_no"]]
    assert (a["name"], a["uom"], a["standard_unit_mhr"], a["description"]) == (
        "Kalem A",
        "m²",
        "2.0000",
        None,
    )
    assert a["default_contractor_type"] == "own"
    assert a["standard_updated_at"] == seed["A"]["standard_updated_at"]
    assert (a["ref_price"], a["ref_price_date"]) == ("12.00", "2026-01-01")
    assert datetime.fromisoformat(a["price_updated_at"]) > datetime.fromisoformat(
        seed["A"]["price_updated_at"]
    )
    b = now[seed["B"]["poz_no"]]
    assert (b["ref_price"], b["ref_price_date"]) == ("10.00", "2026-01-01")
    assert b["price_updated_at"] == seed["B"]["price_updated_at"]  # yalniz tarih → damga sabit
    c = now[seed["C"]["poz_no"]]
    assert (c["ref_price"], c["ref_price_date"]) == ("10.00", "2025-01-01")
    d = now[seed["D"]["poz_no"]]
    assert (d["ref_price"], d["ref_price_date"], d["price_updated_at"]) == (None, None, None)
    assert now["KAB-0005"]["source_code"] == "S.E"


async def test_update_price_fiyat_degisir_tarih_verilmezse_tarih_NULL_olur(
    client, admin, kab
) -> None:
    seed = await _tohum(client, admin, kab)
    resp = await _toplu(
        client,
        admin,
        [_kalem(kab, "A", source_code="S.A", ref_price="11.00")],
        on_source_conflict="update_price",
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["items"][0]["action"] == "price_updated"
    a = next(
        i
        for i in (await client.get(URL, headers=admin)).json()["items"]
        if i["poz_no"] == seed["A"]["poz_no"]
    )
    assert (a["ref_price"], a["ref_price_date"]) == ("11.00", None)


async def test_update_price_hepsi_mevcut_ikinci_kez_ayni_yukleme_degismedi(
    client, admin, kab, db_session
) -> None:
    govde = [
        _kalem(kab, "A", source_code="S.A", ref_price="10.00", ref_price_date="2026-01-01"),
        _kalem(kab, "B", source_code="S.B", ref_price="20.00", ref_price_date="2026-01-01"),
    ]
    assert (await _toplu(client, admin, govde)).status_code == 201
    onceki = await _denetim_adedi(db_session)
    again = await _toplu(client, admin, govde, on_source_conflict="update_price")
    assert again.status_code == 201, again.text
    assert (again.json()["created"], again.json()["updated"], again.json()["unchanged"]) == (
        0,
        0,
        2,
    )
    assert await _sayilar(db_session) == (2, [2])
    assert await _denetim_adedi(db_session) == onceki + 1  # yine TEK ozet satir


# ------------------------------------------------------------ yetki


async def test_yetki_contracts_view_ve_contracts_yok_403_kimliksiz_401_disiplinli_gecer(
    client, admin, kab, db_session, user_factory
) -> None:
    govde = {"items": [_kalem(kab, "X")]}
    assert (await client.post(BULK, json=govde)).status_code == 401

    # contracts:view (okur, yazamaz)
    await modul_duzeyi_yaz(db_session, "accounting", "contracts", AccessLevel.view)
    muhasebe = await _giris(client, db_session, user_factory, "accounting")
    assert (await client.post(BULK, json=govde, headers=muhasebe)).status_code == 403

    # contracts yok (EV okuyabilen saha rolu)
    saha = await _giris(client, db_session, user_factory, "site_chief")
    assert (await client.post(BULK, json=govde, headers=saha)).status_code == 403

    # contracts:full + bir projede disiplin atanmis kullanici: IZN-B3 — katalog sirket geneli, GECER
    token = await _login_with_access(
        client, db_session, user_factory, "project_manager", "pm.bulk@tkl.co"
    )
    user_id = (
        await db_session.execute(select(User.id).where(User.email == "pm.bulk@tkl.co"))
    ).scalar_one()
    await baska_projede_disiplinli(db_session, user_id, kab.id)
    assert (await client.post(BULK, json=govde, headers=_auth(token))).status_code == 201

    # 403 alanlar bir sey yazmadi: katalogda YALNIZ disiplinli kullanicinin kalemi var
    assert len((await client.get(URL, headers=admin)).json()["items"]) == 1

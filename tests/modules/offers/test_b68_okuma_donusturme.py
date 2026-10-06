"""TKL-B6.8a — teklif okumalarinda donusturme bilgisi (BD-1) + liste suzgeci `conversion` (BD-3).

EV tarafi SAHTE kancayla taklit edilir (`tohum_kancasi`); donusturme gercek uc ile yapilir.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import event, text

from app.core.access import AccessLevel
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz

from .._boq import _auth, _login_with_access
from ._convert import govde, kazanilmis_teklif, url
from ._offers import URL, detay, teklif

pytestmark = pytest.mark.usefixtures("tohum_kancasi")


async def _donustur(client, admin, isveren, katalog, ad: str) -> dict:
    """Kazanilmis teklifi kurar ve GERCEK uctan donusturur; teklif kimligi + proje yaniti doner."""
    kz = await kazanilmis_teklif(client, admin, isveren, katalog, title=f"Teklif {ad}")
    istek = govde(kz)
    istek["project"]["name"] = f"Proje {ad}"
    istek["contract"]["contract_no"] = f"SZL-{ad}"
    resp = await client.post(url(kz.offer_id), json=istek, headers=admin)
    assert resp.status_code == 200, resp.text
    return {"offer_id": kz.offer_id, "offer_no": kz.offer_no, "yanit": resp.json()}


async def _kazanilmis_donusmemis(client, admin, isveren, katalog, ad: str) -> str:
    kz = await kazanilmis_teklif(client, admin, isveren, katalog, title=f"Bekleyen {ad}")
    return kz.offer_id


async def _liste(client, admin, **params) -> dict:
    resp = await client.get(URL, params=params, headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _satir(liste: dict, offer_id: str) -> dict:
    return next(s for s in liste["items"] if s["id"] == offer_id)


# --------------------------------------------------------------------------- detay


async def test_detay_donusmus_teklifte_proje_ve_donusturme_bilgisi(
    client, admin, isveren, katalog, db_session
) -> None:
    d = await _donustur(client, admin, isveren, katalog, "A")
    govde_ = await detay(client, admin, d["offer_id"])
    proje = govde_["project"]
    assert govde_["project_id"] == proje["id"]  # eski alan KORUNUR
    assert set(proje) == {"id", "code", "name", "slug"}
    assert proje["name"] == "Proje A" and proje["code"]
    assert govde_["converted_at"] is not None
    assert govde_["converted_by_name"]
    assert govde_["conversion_state"] == "converted"


async def test_detay_donusmemis_teklifte_alanlar_null(client, admin, isveren, katalog) -> None:
    offer_id = await _kazanilmis_donusmemis(client, admin, isveren, katalog, "B")
    govde_ = await detay(client, admin, offer_id)
    assert govde_["project"] is None and govde_["project_id"] is None
    assert govde_["converted_at"] is None and govde_["converted_by_name"] is None
    assert all(e["kind"] != "converted" for e in govde_["history"])


async def test_gecmiste_converted_olayi_kazanildidan_sonra(client, admin, isveren, katalog) -> None:
    d = await _donustur(client, admin, isveren, katalog, "C")
    govde_ = await detay(client, admin, d["offer_id"])
    turler = [e["kind"] for e in govde_["history"]]
    assert turler[-2:] == ["won", "converted"], turler
    olay = govde_["history"][-1]
    assert olay["at"] == govde_["converted_at"]
    assert olay["user_name"] == govde_["converted_by_name"]
    assert olay["user_id"] is not None
    assert olay["rev_no"] == govde_["latest_rev_no"]


async def test_donusturen_kullanici_silinmisse_ad_null(
    client, admin, isveren, katalog, db_session
) -> None:
    d = await _donustur(client, admin, isveren, katalog, "D")
    await db_session.execute(
        text("UPDATE offers SET converted_by_user_id = NULL WHERE id = :i"),
        {"i": uuid.UUID(d["offer_id"])},
    )
    await db_session.flush()
    govde_ = await detay(client, admin, d["offer_id"])
    assert govde_["converted_by_name"] is None and govde_["converted_at"] is not None
    assert govde_["project"] is not None
    assert govde_["history"][-1]["kind"] == "converted"
    assert govde_["history"][-1]["user_name"] is None
    liste = await _liste(client, admin)
    assert _satir(liste, d["offer_id"])["converted_by_name"] is None


# --------------------------------------------------------------------------- liste


async def test_liste_satirinda_proje_ve_donusturme_alanlari(
    client, admin, isveren, katalog
) -> None:
    d = await _donustur(client, admin, isveren, katalog, "E")
    bekleyen = await _kazanilmis_donusmemis(client, admin, isveren, katalog, "E")
    liste = await _liste(client, admin)
    donen = _satir(liste, d["offer_id"])
    assert donen["project"]["name"] == "Proje E"
    assert donen["project"]["id"] == donen["project_id"]
    assert donen["converted_at"] is not None and donen["converted_by_name"]
    bos = _satir(liste, bekleyen)
    assert bos["project"] is None and bos["converted_at"] is None
    assert bos["converted_by_name"] is None


# ------------------------------------------------------------------------ suzgec


async def test_conversion_suzgeci_iki_deger_ve_gecersiz_422(
    client, admin, isveren, katalog
) -> None:
    d = await _donustur(client, admin, isveren, katalog, "F")
    bekleyen = await _kazanilmis_donusmemis(client, admin, isveren, katalog, "F")
    taslak = (await teklif(client, admin, isveren))["id"]

    bekleyenler = await _liste(client, admin, conversion="won_not_converted")
    assert [s["id"] for s in bekleyenler["items"]] == [bekleyen]
    assert bekleyenler["total"] == 1
    donusenler = await _liste(client, admin, conversion="converted")
    assert [s["id"] for s in donusenler["items"]] == [d["offer_id"]]
    tumu = await _liste(client, admin)
    assert {s["id"] for s in tumu["items"]} == {d["offer_id"], bekleyen, taslak}

    resp = await client.get(URL, params={"conversion": "hepsi"}, headers=admin)
    assert resp.status_code == 422


async def test_conversion_diger_suzgeclerle_birlesir_ozet_suzgecten_bagimsiz(
    client, admin, isveren, katalog
) -> None:
    d = await _donustur(client, admin, isveren, katalog, "G")
    bekleyen = await _kazanilmis_donusmemis(client, admin, isveren, katalog, "G")
    # status=won + converted → yalniz donusen; status=sent + converted → bos
    assert [
        s["id"]
        for s in (await _liste(client, admin, conversion="converted", status="won"))["items"]
    ] == [d["offer_id"]]
    assert (await _liste(client, admin, conversion="converted", status="sent"))["items"] == []
    # q ile birlesim
    q_bekleyen = await _liste(client, admin, conversion="won_not_converted", q="Bekleyen G")
    assert [s["id"] for s in q_bekleyen["items"]] == [bekleyen]
    assert (await _liste(client, admin, conversion="converted", q="Bekleyen G"))["items"] == []
    # ozet: conversion suzgecinden BAGIMSIZ (SO-12 ile ayni kalip)
    ozet = (await _liste(client, admin, conversion="converted"))["summary"]
    assert ozet["won_not_converted_count"] == 1
    assert {c["status"]: c["count"] for c in ozet["by_status"]}["won"] == 2


# ------------------------------------------------------------------------------ N+1


@contextmanager
def _sayac():
    from tests.conftest import test_engine

    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)


async def test_N1_donusmus_tekliflerle_liste_sorgu_sayisi_sabit(
    client, admin, isveren, katalog
) -> None:
    await _donustur(client, admin, isveren, katalog, "N0")
    await client.get(URL, headers=admin)  # isitma: ilk istekteki tek seferlik oturum yazimi
    with _sayac() as bir:
        assert (await client.get(URL, headers=admin)).status_code == 200
    for i in range(1, 5):
        await _donustur(client, admin, isveren, katalog, f"N{i}")
    await _kazanilmis_donusmemis(client, admin, isveren, katalog, "N")
    with _sayac() as bes:
        resp = await client.get(URL, headers=admin)
    assert resp.status_code == 200 and resp.json()["total"] == 6
    assert sum(s["project"] is not None for s in resp.json()["items"]) == 5
    assert len(bes) == len(bir), f"N+1: 1 donusmus teklifte {len(bir)}, 5'te {len(bes)} sorgu"
    assert sum("FROM projects" in s or "JOIN projects" in s for s in bes) == 1


# ------------------------------------------------------------------------------ maske


@pytest.mark.parametrize("tum_tutarlar", [True, None])
async def test_maske_limited_ve_finance_kapsamda_donusturme_alanlari_gorunur(
    client, admin, isveren, katalog, db_session, user_factory, tum_tutarlar
) -> None:
    d = await _donustur(client, admin, isveren, katalog, "M")
    await modul_duzeyi_yaz(
        db_session, "accounting", "contracts", AccessLevel.view, tum_tutarlar=tum_tutarlar
    )
    token = await _login_with_access(
        client, db_session, user_factory, "accounting", f"m.{uuid.uuid4().hex[:6]}@tkl.co"
    )
    kisitli = _auth(token)
    detay_ = await detay(client, kisitli, d["offer_id"])
    satir = _satir(await _liste(client, kisitli), d["offer_id"])
    for govde_ in (detay_, satir):
        assert govde_["project"]["name"] == "Proje M" and govde_["project"]["code"]
        assert govde_["converted_at"] is not None and govde_["converted_by_name"]
    assert detay_["history"][-1]["kind"] == "converted"
    assert len((await _liste(client, kisitli, conversion="converted"))["items"]) == 1

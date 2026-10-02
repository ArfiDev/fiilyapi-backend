"""TKL-B4.2 — F1 kullanici adlari, F2 revizyon `updated_at`, F5 metin birligi ("Vazgeçildi").

F1: `prepared_by_name` + gecmis olaylarinda `user_name` (kullanici silinmisse `None`), TEK sorgu.
F2: kosul/grup/kalem yazimi ve gecis revizyonun `updated_at`ini ilerletir; OKUMA ilerletmez.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import delete, event

from app.modules.offers import locking
from app.modules.users.models import User

from .._boq import _auth
from ._offers import URL, detay, gecis, grup, kalem, rev_url, revizyon, teklif

# ------------------------------------------------------------------------- F1


async def _kisi(client, db_session, user_factory, ad: str) -> tuple[dict[str, str], User]:
    email = f"{uuid.uuid4().hex[:8]}@tkl.co"
    user = await user_factory(
        email=email, password="parola1234", role_key="system_admin", full_name=ad
    )
    token = (
        await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    ).json()["access_token"]
    return _auth(token), user


async def test_F1_hazirlayan_ve_gecmis_olay_kullanici_adlari(
    client, db_session, user_factory, isveren, seeded_db
) -> None:
    ayse, ayse_u = await _kisi(client, db_session, user_factory, "Ayşe Yılmaz")
    mehmet, mehmet_u = await _kisi(client, db_session, user_factory, "Mehmet Demir")
    o = await teklif(client, ayse, isveren)
    assert o["prepared_by_name"] == "Ayşe Yılmaz" and o["prepared_by_user_id"] == str(ayse_u.id)
    await gecis(client, mehmet, o["id"], "send")
    await gecis(client, ayse, o["id"], "lose")
    await client.post(f"{URL}/{o['id']}/revisions", headers=mehmet)
    d = await detay(client, ayse, o["id"])
    olaylar = [(e["rev_no"], e["kind"], e["user_name"]) for e in d["history"]]
    assert olaylar == [
        (0, "opened", "Ayşe Yılmaz"),
        (0, "sent", "Mehmet Demir"),
        (0, "lost", "Ayşe Yılmaz"),
        (1, "opened", "Mehmet Demir"),
    ]
    assert d["prepared_by_name"] == "Ayşe Yılmaz"  # kunye = ilk hazirlayan
    assert mehmet_u.id is not None


async def test_F1_kullanici_silinmisse_ad_ve_kimlik_None(
    client, admin, db_session, user_factory, isveren
) -> None:
    gecici, gecici_u = await _kisi(client, db_session, user_factory, "Geçici Kullanıcı")
    o = await teklif(client, gecici, isveren)
    await gecis(client, gecici, o["id"], "send")
    await db_session.execute(delete(User).where(User.id == gecici_u.id))
    await db_session.flush()
    d = await detay(client, admin, o["id"])
    assert d["prepared_by_user_id"] is None and d["prepared_by_name"] is None
    assert [(e["kind"], e["user_id"], e["user_name"]) for e in d["history"]] == [
        ("opened", None, None),
        ("sent", None, None),
    ]


async def test_F1_kullanici_adlari_TEK_sorguda_N_artmaz(
    client, admin, db_session, user_factory, isveren
) -> None:
    from tests.conftest import test_engine

    o = await teklif(client, admin, isveren)

    async def say() -> int:
        ifadeler: list[str] = []

        def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
            ifadeler.append(statement)

        event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
        try:
            assert (await client.get(f"{URL}/{o['id']}", headers=admin)).status_code == 200
        finally:
            event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)
        return len(ifadeler)

    az = await say()
    for eylem in ("send", "lose"):
        await gecis(client, admin, o["id"], eylem)
    for _ in range(3):  # 3 revizyon daha, hepsi gonderilmis/kaybedilmis → cok sayida olay
        yeni = (await client.post(f"{URL}/{o['id']}/revisions", headers=admin)).json()
        await gecis(client, admin, o["id"], "send", yeni["rev_no"])
        await gecis(client, admin, o["id"], "lose", yeni["rev_no"])
    cok = await say()
    assert cok == az, f"N+1: az olayda {az}, cok olayda {cok} sorgu"


# ------------------------------------------------------------------------- F2


async def _guncelleme(client, admin, oid: str, rev_no: int = 0) -> str:
    return (await revizyon(client, admin, oid, rev_no))["updated_at"]


async def test_F2_yazimlar_updated_at_ilerletir_okuma_ilerletmez(
    client, admin, isveren, katalog
) -> None:
    o = await teklif(client, admin, isveren)
    oid = o["id"]
    t0 = await _guncelleme(client, admin, oid)

    # OKUMA ilerletmez (revizyon + detay + liste + tekrar revizyon)
    await client.get(f"{URL}/{oid}", headers=admin)
    await client.get(URL, headers=admin)
    assert await _guncelleme(client, admin, oid) == t0

    async def ilerledi(onceki: str) -> str:
        yeni = await _guncelleme(client, admin, oid)
        assert yeni > onceki, f"updated_at ILERLEMEDI: {onceki} -> {yeni}"
        return yeni

    g = await grup(client, admin, oid)  # grup ekleme
    t1 = await ilerledi(t0)
    k = await kalem(
        client, admin, oid, g["id"], katalog[0].id, cost_unit_price="10"
    )  # kalem ekleme
    t2 = await ilerledi(t1)
    await client.patch(rev_url(oid) + f"/items/{k['id']}", json={"quantity": "5"}, headers=admin)
    t3 = await ilerledi(t2)  # kalem guncelleme
    await client.patch(rev_url(oid) + f"/groups/{g['id']}", json={"name": "Yeni"}, headers=admin)
    t4 = await ilerledi(t3)  # grup guncelleme
    await client.patch(rev_url(oid), json={"notes": "n"}, headers=admin)
    t5 = await ilerledi(t4)  # kosul
    toplu = {
        "items": [{"catalog_item_id": str(katalog[2].id), "group_id": g["id"], "quantity": "1"}]
    }
    await client.post(rev_url(oid) + "/items/bulk", json=toplu, headers=admin)
    t6 = await ilerledi(t5)  # toplu ekleme
    await client.delete(rev_url(oid) + f"/items/{k['id']}", headers=admin)
    t7 = await ilerledi(t6)  # kalem silme
    await client.delete(rev_url(oid) + f"/groups/{g['id']}", headers=admin)
    t8 = await ilerledi(t7)  # grup silme
    await gecis(client, admin, oid, "send")
    await ilerledi(t8)  # gecis

    d = (await detay(client, admin, oid))["revisions"][0]
    assert d["updated_at"] == await _guncelleme(client, admin, oid)  # ozet = detay


async def test_F2_reddedilen_yazim_updated_at_ilerletmez_yeni_revizyon_kendi_zamani(
    client, admin, isveren, katalog
) -> None:
    o = await teklif(client, admin, isveren)
    oid = o["id"]
    g = await grup(client, admin, oid)
    t0 = await _guncelleme(client, admin, oid)
    kotu = {"catalog_item_id": str(uuid.uuid4()), "group_id": g["id"], "quantity": "1"}
    assert (await client.post(rev_url(oid) + "/items", json=kotu, headers=admin)).status_code == 404
    assert (
        await client.patch(rev_url(oid), json={"validity_days": 0}, headers=admin)
    ).status_code == 422
    assert await _guncelleme(client, admin, oid) == t0
    await gecis(client, admin, oid, "send")
    t1 = await _guncelleme(client, admin, oid)
    assert (await client.patch(rev_url(oid), json={"notes": "x"}, headers=admin)).status_code == 409
    assert await _guncelleme(client, admin, oid) == t1  # 409 ilerletmez
    yeni = (await client.post(f"{URL}/{oid}/revisions", headers=admin)).json()
    assert yeni["updated_at"] >= t1  # yeni revizyon KENDI zamanini tasir
    assert await _guncelleme(client, admin, oid, 0) == t1  # eski revizyon degismedi


def test_F2_touch_revision_kaynakta_yalniz_yazma_yollarinda() -> None:
    """Okuma modulleri (`offer_queries`, `offer_views`) revizyonu ASLA dokunmaz."""
    kok = Path(locking.__file__).parent
    for ad in ("offer_queries.py", "offer_views.py"):
        assert "updated_at =" not in (kok / ad).read_text(encoding="utf-8"), ad
        assert "touch_revision" not in (kok / ad).read_text(encoding="utf-8"), ad


# ------------------------------------------------------------------------- F5


async def test_F5_withdrawn_durumunun_turkce_adi_Vazgecildi(
    client, admin, isveren, db_session
) -> None:
    from app.modules.audit.models import AuditAction

    from .._boq import _audit_details

    o = await teklif(client, admin, isveren)
    await gecis(client, admin, o["id"], "withdraw")
    resp = await gecis(client, admin, o["id"], "send")  # withdrawn'dan cikis YOK
    assert resp.status_code == 409
    assert "vazgeçilmiş" in resp.json()["detail"]
    no = o["offer_no"]
    assert f"Teklif vazgeçildi: {no} Rev.0" in await _audit_details(db_session, AuditAction.update)


def test_F5_modulde_geri_cekil_metni_YOK() -> None:
    """T31: durumun adi "Vazgeçildi"; "geri çekil…" hicbir teklif metninde gecmez."""
    kok = Path(locking.__file__).parent
    dosyalar = [*kok.glob("*.py"), kok.parent / "audit" / "messages" / "offers.py"]
    for yol in dosyalar:
        assert "geri çek" not in yol.read_text(encoding="utf-8").lower(), yol.name

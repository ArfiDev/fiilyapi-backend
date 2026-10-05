"""IZN-B4b onarımı — `GET /subcontractor-progress-payments/summary` projeler ÜSTÜ toplamdır.

Toplam proje başına bölünemez → FAIL-CLOSED: kişinin HERHANGİ bir projesinde `maliyet_kar`
gizliyse KPI toplamları gizli (birleşim, B4a ile tutarlı); liste satırı ise KENDİ projesindeki
rolle maskelenir (`project_id`).
"""

from __future__ import annotations

from app.core.sayfalar import HiddenCategory
from tests._hassas_alan import rol_gizli
from tests._proje_ekibi import ekibe_ekle
from tests.subcontractor_progress_payments.conftest import _auth

H = HiddenCategory
_PAROLA = "parola1234"


async def _kisi(client, seeded_db, user_factory, ad, proje_a, proje_b, *, b):
    r_ana = await rol_gizli(seeded_db, f"{ad}_ana", set())
    r_a = await rol_gizli(seeded_db, f"{ad}_a", set())
    r_b = await rol_gizli(seeded_db, f"{ad}_b", b)
    user = await user_factory(email=f"{ad}@b4b.co", password=_PAROLA, role_key=r_ana.key)
    await ekibe_ekle(seeded_db, user, proje_a.id, r_a.id)
    await ekibe_ekle(seeded_db, user, proje_b.id, r_b.id)
    yanit = await client.post("/auth/login", json={"email": f"{ad}@b4b.co", "password": _PAROLA})
    return _auth(yanit.json()["access_token"])


async def test_ozet_B_gizliyse_toplam_gizli_liste_satiri_projesine_gore(
    client,
    seeded_db,
    user_factory,
    taseron_sozlesmesi_fabrikasi,
    hakedis_fabrikasi,
    sozlesme_sahibi,
):
    sozlesme_a, proje_a, _ = await taseron_sozlesmesi_fabrikasi("OZ-A")
    sozlesme_b, proje_b, _ = await taseron_sozlesmesi_fabrikasi("OZ-B")
    hakedis_a = await hakedis_fabrikasi(sozlesme_a, sozlesme_sahibi)
    hakedis_b = await hakedis_fabrikasi(sozlesme_b, sozlesme_sahibi)
    gizli = await _kisi(client, seeded_db, user_factory, "oz1", proje_a, proje_b, b={H.maliyet_kar})
    acik = await _kisi(client, seeded_db, user_factory, "oz2", proje_a, proje_b, b=set())

    ozet_gizli = (
        await client.get("/subcontractor-progress-payments/summary", headers=gizli)
    ).json()
    ozet_acik = (await client.get("/subcontractor-progress-payments/summary", headers=acik)).json()
    liste = (await client.get("/subcontractor-progress-payments", headers=gizli)).json()

    assert ozet_acik["total_gross"] is not None  # POZİTİF KONTROL
    assert ozet_gizli["total_gross"] is None  # B gizli → projeler üstü toplam gizli
    assert ozet_gizli["pending_gross"] is None
    satirlar = {s["id"]: s for s in liste["items"]}
    assert satirlar[str(hakedis_a.id)]["gross_total"] is not None  # A satırı AÇIK
    assert satirlar[str(hakedis_b.id)]["gross_total"] is None  # B satırı gizli

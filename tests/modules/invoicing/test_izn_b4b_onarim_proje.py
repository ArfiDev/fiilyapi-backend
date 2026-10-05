"""IZN-B4b onarımı — fatura uçları proje başına maske (A açık / B gizli matrisi).

* `GET /invoices` satırı `project_id` ile KENDİ projesindeki rolle maskelenir.
* `GET /invoices/{id}` ve `GET /invoices/{id}/payments`: `RESOLVERS` `/invoices/{invoice_id}`
  faturanın projesini çözer → o projedeki rol (ana rol DEĞİL).
* `GET /invoices/summary` projeler ÜSTÜ toplamdır: kişinin HERHANGİ bir projesinde gizli
  kategori varsa toplam gizli (birleşim, fail-closed; B4a ile tutarlı).
* `party_name` `satis_alici`dır (satır başına).
"""

from __future__ import annotations

from app.core.sayfalar import HiddenCategory
from tests._hassas_alan import rol_gizli
from tests._proje_ekibi import ekibe_ekle
from tests.modules.invoicing.conftest import _auth

H = HiddenCategory
_PAROLA = "parola1234"
_GIZLI = {H.maliyet_kar, H.banka_kasa, H.satis_alici}


async def _giris(client, email):
    resp = await client.post("/auth/login", json={"email": email, "password": _PAROLA})
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])


async def _dunya(client, seeded_db, user_factory, project_factory, fatura_fabrikasi, ad, *, a, b):
    """A ve B projesi + her birinde bir fatura; kişi A'da `a`, B'de `b` gizli kümeli rolle üye."""
    proje_a = await project_factory(f"{ad}-A", name="Proje A")
    proje_b = await project_factory(f"{ad}-B", name="Proje B")
    fatura_a = await fatura_fabrikasi(project=proje_a, party_name="Alıcı A Ltd.")
    fatura_b = await fatura_fabrikasi(project=proje_b, party_name="Alıcı B Ltd.")
    r_ana = await rol_gizli(seeded_db, f"{ad}_ana", set())
    r_a = await rol_gizli(seeded_db, f"{ad}_a", a)
    r_b = await rol_gizli(seeded_db, f"{ad}_b", b)
    user = await user_factory(email=f"{ad}@b4b.co", password=_PAROLA, role_key=r_ana.key)
    await ekibe_ekle(seeded_db, user, proje_a.id, r_a.id)
    await ekibe_ekle(seeded_db, user, proje_b.id, r_b.id)
    return await _giris(client, f"{ad}@b4b.co"), fatura_a, fatura_b


async def test_liste_satiri_KENDI_projesindeki_rolle_maskelenir(
    client, seeded_db, user_factory, project_factory, fatura_fabrikasi
):
    basliklar, fatura_a, fatura_b = await _dunya(
        client, seeded_db, user_factory, project_factory, fatura_fabrikasi, "il", a=set(), b=_GIZLI
    )

    yanit = await client.get("/invoices", headers=basliklar)

    assert yanit.status_code == 200, yanit.text
    satirlar = {s["id"]: s for s in yanit.json()["items"]}
    assert satirlar[str(fatura_a.id)]["total"] is not None  # POZİTİF KONTROL: A açık
    assert satirlar[str(fatura_a.id)]["party_name"] == "Alıcı A Ltd."
    assert satirlar[str(fatura_b.id)]["total"] is None  # B gizli
    assert satirlar[str(fatura_b.id)]["party_name"] is None  # `party_name` = satis_alici


async def test_detay_ve_odemeler_FATURANIN_projesindeki_rolle_cozulur(
    client, seeded_db, user_factory, project_factory, fatura_fabrikasi
):
    """`RESOLVERS` `/invoices/{invoice_id}` yoksa istek birleşime düşer ve A'daki AÇIK rol de
    gizlenirdi (aşırı maskeleme); çözücü varken A açık, B gizli."""
    basliklar, fatura_a, fatura_b = await _dunya(
        client, seeded_db, user_factory, project_factory, fatura_fabrikasi, "id", a=set(), b=_GIZLI
    )

    detay_a = (await client.get(f"/invoices/{fatura_a.id}", headers=basliklar)).json()
    detay_b = (await client.get(f"/invoices/{fatura_b.id}", headers=basliklar)).json()
    odeme_a = (await client.get(f"/invoices/{fatura_a.id}/payments", headers=basliklar)).json()
    odeme_b = (await client.get(f"/invoices/{fatura_b.id}/payments", headers=basliklar)).json()

    assert detay_a["total"] is not None and detay_b["total"] is None
    assert odeme_a["paid_total"] is not None and odeme_a["remaining"] is not None
    assert odeme_b["paid_total"] is None and odeme_b["remaining"] is None


async def test_ozet_projeler_ustu_toplam_BIRLESIM_ile_gizlenir(
    client, seeded_db, user_factory, project_factory, fatura_fabrikasi
):
    """Özet birden çok projeyi toplar: B'deki rol gizliyse A açık olsa da toplam GİZLİ (GECE
    KARARI: proje başına bölünemeyen toplam fail-closed). İkizi: hiçbiri gizli değilse AÇIK."""
    gizli, _, _ = await _dunya(
        client, seeded_db, user_factory, project_factory, fatura_fabrikasi, "os1", a=set(), b=_GIZLI
    )
    acik, _, _ = await _dunya(
        client, seeded_db, user_factory, project_factory, fatura_fabrikasi, "os2", a=set(), b=set()
    )

    ozet_gizli = (await client.get("/invoices/summary", headers=gizli)).json()
    ozet_acik = (await client.get("/invoices/summary", headers=acik)).json()

    assert ozet_acik["vat_difference"] is not None  # POZİTİF KONTROL
    assert ozet_gizli["vat_difference"] is None
    assert ozet_gizli["receivable"]["amount"] is None

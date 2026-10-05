"""IZN-B4 — kişisel veri (`str`) maskesi: alıcı bilgisi (`satis_alici`) + şirket künyesi (`yok`).

`str` alanlar `tum_tutarlar` ile GİZLENMEZ (tutar değildir); yalnız kendi kategorileriyle. Maskeli
alan `null` döner (tip zaten `str | None`: OpenAPI'de DEĞİŞİKLİK yok).
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.sayfalar import HiddenCategory
from tests._hassas_alan import rol_gizli
from tests._proje_ekibi import tum_projeler
from tests.modules._boq import _auth

PASSWORD = "parola1234"
H = HiddenCategory
_ALICI = {
    "customer_type": "person",
    "name": "Ayşe Yılmaz",
    "national_id": "12345678901",
    "phone": "05320000000",
    "email": "ayse@ornek.co",
    "address": "Kadıköy / İstanbul",
}


async def _giris(client: AsyncClient, user_factory, session, email: str, rol_key: str) -> dict:
    user = await user_factory(email=email, password=PASSWORD, role_key=rol_key)
    await tum_projeler(session, user)
    resp = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return _auth(resp.json()["access_token"])


async def _alici_olustur(client: AsyncClient, baslik: dict) -> str:
    resp = await client.post("/customers", json=_ALICI, headers=baslik)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_satis_alici_gizli_ALICI_kisisel_alanlari_null_ad_ve_tip_durur(
    client, db_session, user_factory
) -> None:
    await rol_gizli(db_session, "pii_acik", set())
    await rol_gizli(db_session, "pii_gizli", {H.satis_alici})
    acik = await _giris(client, user_factory, db_session, "acik@pii.co", "pii_acik")
    gizli = await _giris(client, user_factory, db_session, "gizli@pii.co", "pii_gizli")
    alici_id = await _alici_olustur(client, acik)

    tam = (await client.get(f"/customers/{alici_id}", headers=acik)).json()
    kisitli = (await client.get(f"/customers/{alici_id}", headers=gizli)).json()
    liste = (await client.get("/customers", headers=gizli)).json()["items"][0]

    assert tam["national_id"] == "12345678901"  # POZİTİF KONTROL
    for alan in ("national_id", "tax_number", "phone", "email", "address"):
        assert kisitli[alan] is None, alan
        assert liste[alan] is None, alan
    # IZN-B4a (GECE KARARI): alıcı ADI da `satis_alici` (satıştaki `customer_name` ile tutarlı).
    assert kisitli["name"] is None
    assert liste["name"] is None
    assert tam["name"] == "Ayşe Yılmaz"  # POZİTİF KONTROL
    assert kisitli["customer_type"] == "person"


async def test_tum_tutarlar_METIN_kisisel_alani_GIZLEMEZ(client, db_session, user_factory) -> None:
    """ "Tüm TUTARLAR" bir kişisel bilgi anahtarı değildir."""
    await rol_gizli(db_session, "pii_tutar", {H.tum_tutarlar})
    baslik = await _giris(client, user_factory, db_session, "tutar@pii.co", "pii_tutar")
    alici_id = await _alici_olustur(client, baslik)

    govde = (await client.get(f"/customers/{alici_id}", headers=baslik)).json()

    assert govde["national_id"] == "12345678901"
    assert govde["phone"] == "05320000000"


async def test_YAZMA_satis_alici_gizliyken_kisisel_alan_gonderen_403_ad_guncellenir(
    client, db_session, user_factory
) -> None:
    await rol_gizli(db_session, "pii_yaz_a", set())
    await rol_gizli(db_session, "pii_yaz_g", {H.satis_alici})
    acik = await _giris(client, user_factory, db_session, "ya@pii.co", "pii_yaz_a")
    gizli = await _giris(client, user_factory, db_session, "yg@pii.co", "pii_yaz_g")
    alici_id = await _alici_olustur(client, acik)

    red = await client.patch(f"/customers/{alici_id}", json={"phone": "0"}, headers=gizli)
    assert red.status_code == 403, red.text
    assert "phone" in red.json()["detail"]

    ok = await client.patch(f"/customers/{alici_id}", json={"name": "Ayşe Demir"}, headers=gizli)
    assert ok.status_code == 200, ok.text
    assert ok.json()["name"] is None  # yazma yanıtı da maskelenir (ad `satis_alici`)
    assert ok.json()["phone"] is None
    guncel = (await client.get(f"/customers/{alici_id}", headers=acik)).json()
    assert guncel["name"] == "Ayşe Demir"  # ad gerçekten güncellendi
    # DB'de telefon DEĞİŞMEDİ
    assert (await client.get(f"/customers/{alici_id}", headers=acik)).json()[
        "phone"
    ] == "05320000000"


async def test_sirket_kunyesi_yok_etiketli_hicbir_bayrakla_gizlenmez(
    client, db_session, user_factory
) -> None:
    await rol_gizli(db_session, "pii_firma", set(HiddenCategory))
    baslik = await _giris(client, user_factory, db_session, "firma@pii.co", "pii_firma")

    resp = await client.get("/company", headers=baslik)

    assert resp.status_code == 200, resp.text
    assert resp.json()["default_vat_rate"] is not None

"""IZN-B4c — personel JSON maskesi ve yazma kapısı (`maas_kisisel`).

Sahte-yeşil onarımı: `PersonnelResponse.iban` ve `PersonnelUpdate.wage_amount`/`iban` etiketi
`yok`a çevrilince 1397 test yeşil kalıyordu — JSON yanıtı ve yazma kapısı hiç sınanmamıştı
(yalnız Excel hücresi sınanıyordu). Gizleyen rol: `maas_kisisel` gizli özel rol (`gizli_headers`);
gizlemeyen rol: `hr_manager` (`ik_headers`, madde 20 ile kişisel alanları GÖRÜR).
"""

import pytest
from sqlalchemy import select

from app.core.sayfalar import HiddenCategory, PageLevel
from app.modules.roles import seed_data
from app.modules.roles.models import Role
from tests._hassas_alan import rol_gizli
from tests._proje_ekibi import ekibe_ekle

KISISEL_ALANLAR = (
    "tc_no",
    "iban",
    "sgk_no",
    "phone",
    "email",
    "address",
    "emergency_contact_phone",
    "birth_date",
    "wage_amount",
)
KART = {
    "full_name": "Ayşe Demir",
    "trade": "Kalıpçı",
    "source": "company",
    "tc_no": "10000000146",
    "iban": "TR330006100519786457841326",
    "sgk_no": "12345678901",
    "phone": "05321234567",
    "email": "ayse@ornek.co",
    "address": "Kadıköy, İstanbul",
    "emergency_contact_name": "Fatma Demir",
    "emergency_contact_phone": "05329876543",
    "birth_date": "1990-04-12",
    "wage_type": "daily",
    "wage_amount": "1500.00",
}
# Yazma kapısı denemesi için GEÇERLİ yeni değerler (alan başına).
YENI_DEGER = {
    "tc_no": "10000000146",
    "iban": "TR470006200519786457841327",
    "sgk_no": "99999999999",
    "phone": "05320000000",
    "email": "yeni@ornek.co",
    "address": "Yeni adres",
    "emergency_contact_phone": "05321111111",
    "birth_date": "1991-01-01",
    "wage_amount": "2.00",
}

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def kisi(client, ik_headers) -> dict:
    yanit = await client.post("/personnel", json=KART, headers=ik_headers)
    assert yanit.status_code == 201, yanit.text
    return yanit.json()


async def _liste_satiri(client, headers, kimlik: str) -> dict:
    yanit = await client.get("/personnel", params={"limit": 100}, headers=headers)
    assert yanit.status_code == 200, yanit.text
    return next(s for s in yanit.json()["items"] if s["id"] == kimlik)


async def test_gizlemeyen_rol_dokuz_kisisel_alani_GORUR(client, ik_headers, kisi):
    """Pozitif kontrol: maske bağlanmamış olsaydı bu da yeşil kalırdı."""
    detay = (await client.get(f"/personnel/{kisi['id']}", headers=ik_headers)).json()
    satir = await _liste_satiri(client, ik_headers, kisi["id"])
    for alan in KISISEL_ALANLAR:
        assert detay[alan] is not None, f"detay {alan}"
        assert satir[alan] is not None, f"liste {alan}"
    assert detay["iban"] == KART["iban"]
    assert detay["wage_amount"] == KART["wage_amount"]


async def test_gizli_rolde_dokuz_kisisel_alan_null_ad_ve_meslek_acik(
    client, ik_headers, gizli_headers, kisi
):
    detay = (await client.get(f"/personnel/{kisi['id']}", headers=gizli_headers)).json()
    satir = await _liste_satiri(client, gizli_headers, kisi["id"])
    for alan in KISISEL_ALANLAR:
        assert detay[alan] is None, f"detay {alan} SIZDI"
        assert satir[alan] is None, f"liste {alan} SIZDI"
    for yuzey in (detay, satir):
        assert yuzey["full_name"] == "Ayşe Demir"
        assert yuzey["trade"] == "Kalıpçı"
        assert yuzey["emergency_contact_name"] == "Fatma Demir"  # kişisel etiketi YOK (ad)


@pytest.mark.parametrize("alan", KISISEL_ALANLAR)
async def test_gizli_rolde_dolu_kisisel_alan_PATCH_403_veri_degismez(
    client, ik_headers, gizli_headers, kisi, alan
):
    yanit = await client.patch(
        f"/personnel/{kisi['id']}", json={alan: YENI_DEGER[alan]}, headers=gizli_headers
    )
    assert yanit.status_code == 403, yanit.text
    assert alan in yanit.json()["detail"]
    sonra = (await client.get(f"/personnel/{kisi['id']}", headers=ik_headers)).json()
    assert sonra[alan] == kisi[alan], f"{alan} 403'e rağmen DEĞİŞTİ"


async def test_gizli_rolde_iban_null_gondermek_403_veri_SILINMEZ(
    client, ik_headers, gizli_headers, kisi
):
    yanit = await client.patch(
        f"/personnel/{kisi['id']}", json={"iban": None}, headers=gizli_headers
    )
    assert yanit.status_code == 403, yanit.text
    sonra = (await client.get(f"/personnel/{kisi['id']}", headers=ik_headers)).json()
    assert sonra["iban"] == KART["iban"]


async def test_gizli_rolde_yalniz_ad_PATCH_200_iban_korunur(
    client, ik_headers, gizli_headers, kisi
):
    yanit = await client.patch(
        f"/personnel/{kisi['id']}", json={"full_name": "Ayşe D."}, headers=gizli_headers
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["full_name"] == "Ayşe D."
    assert yanit.json()["iban"] is None  # yanıt maskeli
    sonra = (await client.get(f"/personnel/{kisi['id']}", headers=ik_headers)).json()
    assert sonra["iban"] == KART["iban"]
    assert sonra["wage_amount"] == KART["wage_amount"]
    assert sonra["full_name"] == "Ayşe D."


async def test_gizlemeyen_rol_kisisel_alani_yazar(client, ik_headers, kisi):
    yanit = await client.patch(
        f"/personnel/{kisi['id']}", json={"wage_amount": "1600.00"}, headers=ik_headers
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["wage_amount"] == "1600.00"


# --- Gövdeye eklenen `project_id` maskeyi / yazma kapısını ATLATMAZ (çürütme bulgusu) ---
# Çekirdek düzeltmenin kendi testleri: `tests/modules/test_izn_b4c_govde_baglam.py`.


@pytest.fixture
async def ekip_istismarcisi(client, seeded_db, user_factory, project_factory, ik_headers, kisi):
    """Ana rol `maas_kisisel` gizli · X projesinde ekip rolü `hr_manager` (gizlemez)."""
    await seed_data.seed_izn_reference_data(seeded_db)
    proje = await project_factory("PM-X", name="X Projesi")
    await rol_gizli(seeded_db, "ana_maas_gizli", {HiddenCategory.maas_kisisel}, PageLevel.edit)
    kullanici = await user_factory(
        email="istismar@personnel.co", password="parola1234", role_key="ana_maas_gizli"
    )
    hr = (await seeded_db.execute(select(Role).where(Role.key == "hr_manager"))).scalar_one()
    await ekibe_ekle(seeded_db, kullanici, proje.id, hr.id)
    yanit = await client.post(
        "/auth/login", json={"email": "istismar@personnel.co", "password": "parola1234"}
    )
    assert yanit.status_code == 200, yanit.text
    return proje, {"Authorization": f"Bearer {yanit.json()['access_token']}"}


async def test_govdeye_eklenen_project_id_maskeyi_ATLATMAZ(client, kisi, ekip_istismarcisi):
    proje, baslik = ekip_istismarcisi
    yanit = await client.patch(
        f"/personnel/{kisi['id']}",
        json={"full_name": "Ayşe D.", "project_id": str(proje.id)},
        headers=baslik,
    )
    assert yanit.status_code == 200, yanit.text
    for alan in ("iban", "tc_no", "wage_amount"):
        assert yanit.json()[alan] is None, f"{alan} gövdeye eklenen project_id ile SIZDI"
    detay = await client.get(f"/personnel/{kisi['id']}", headers=baslik)
    assert detay.json()["iban"] is None  # gövdesiz okuma da maskeli (ana rol ∪ ekip rolleri)


async def test_govdeye_eklenen_project_id_yazma_kapisini_ATLATMAZ(
    client, ik_headers, kisi, ekip_istismarcisi
):
    proje, baslik = ekip_istismarcisi
    yanit = await client.patch(
        f"/personnel/{kisi['id']}",
        json={"iban": YENI_DEGER["iban"], "wage_amount": "1.00", "project_id": str(proje.id)},
        headers=baslik,
    )
    assert yanit.status_code == 403, yanit.text
    sonra = (await client.get(f"/personnel/{kisi['id']}", headers=ik_headers)).json()
    assert sonra["iban"] == KART["iban"] and sonra["wage_amount"] == KART["wage_amount"]

"""IZN-B5a (ajan A) — rol uçları (madde 15), bordro oranı ucu (madde 8), eşik/katalog incelikleri.

Madde 15: `ayarlar.kullanicilar` Görür bitinin rol uçlarını açmaması.
Madde 8: `PUT /payroll/rates/{yıl}/{kaynak}` vergi dilimi ucuyla AYNI kapıyı alır
(`ayarlar.bordro_oranlari` Düzenler).
"""

import pytest
from sqlalchemy import select, update

from app.core.sayfalar import PageLevel
from app.modules.roles.models import Role, RolePagePermission
from tests._ekip_dunyasi import rol_kur

pytestmark = pytest.mark.asyncio

SIFRE = "parola1234"


async def _rol_hucreli(session, key: str, hucreler: dict[str, PageLevel]) -> Role:
    """Yalnız verilen sayfalarda hücresi olan özel rol (diğer hepsi `none`)."""
    rol = await rol_kur(session, key, PageLevel.none)
    for sayfa, level in hucreler.items():
        await session.execute(
            update(RolePagePermission)
            .where(RolePagePermission.role_id == rol.id, RolePagePermission.page_key == sayfa)
            .values(level=level)
        )
    await session.flush()
    return rol


async def _baslik(client, user_factory, role_key: str) -> dict[str, str]:
    email = f"{role_key}@b5a.co"
    await user_factory(email=email, password=SIFRE, role_key=role_key)
    yanit = await client.post("/auth/login", json={"email": email, "password": SIFRE})
    assert yanit.status_code == 200, yanit.text
    return {"Authorization": f"Bearer {yanit.json()['access_token']}"}


async def _rol_id(session, key: str):
    return (await session.execute(select(Role.id).where(Role.key == key))).scalar_one()


# --- Madde 15 -----------------------------------------------------------------------------------

ROL_DETAY_UCLARI = ("/roles/{id}/permissions", "/roles/{id}/pages")


@pytest.mark.parametrize("yol", ["/roles/{id}/permissions", "/roles/{id}/pages", "/modules"])
async def test_kullanicilar_gorur_rol_detay_uclarini_ACAMAZ(client, seeded_db, user_factory, yol):
    await _rol_hucreli(seeded_db, "kul_gorur", {"ayarlar.kullanicilar": PageLevel.view})
    basliklar = await _baslik(client, user_factory, "kul_gorur")
    rid = await _rol_id(seeded_db, "kul_gorur")
    yanit = await client.get(yol.format(id=rid), headers=basliklar)
    assert yanit.status_code == 403, yanit.text


@pytest.mark.parametrize("sayfa", ["ayarlar.rol_yonetimi", "ayarlar.sayfa_izinleri"])
@pytest.mark.parametrize("yol", ["/roles/{id}/permissions", "/roles/{id}/pages", "/modules"])
async def test_rol_ekranlari_gorur_rol_detay_uclarini_acar(
    client, seeded_db, user_factory, yol, sayfa
):
    anahtar = f"rol_gorur_{sayfa.split('.')[1]}"
    await _rol_hucreli(seeded_db, anahtar, {sayfa: PageLevel.view})
    basliklar = await _baslik(client, user_factory, anahtar)
    rid = await _rol_id(seeded_db, anahtar)
    yanit = await client.get(yol.format(id=rid), headers=basliklar)
    assert yanit.status_code == 200, yanit.text


async def test_roles_liste_kullanicilar_yalniz_gorur_403(client, seeded_db, user_factory):
    await _rol_hucreli(seeded_db, "kul_gorur2", {"ayarlar.kullanicilar": PageLevel.view})
    basliklar = await _baslik(client, user_factory, "kul_gorur2")
    assert (await client.get("/roles", headers=basliklar)).status_code == 403


async def test_roles_liste_kullanicilar_DUZENLER_acik(client, seeded_db, user_factory):
    """Kullanıcıya rol atamak için rol listesi gerekir (UsersScreen/UserAccessModal)."""
    await _rol_hucreli(seeded_db, "kul_duzenler", {"ayarlar.kullanicilar": PageLevel.edit})
    basliklar = await _baslik(client, user_factory, "kul_duzenler")
    assert (await client.get("/roles", headers=basliklar)).status_code == 200


@pytest.mark.parametrize("sayfa", ["ayarlar.rol_yonetimi", "ayarlar.sayfa_izinleri"])
async def test_roles_liste_rol_ekranlari_gorur_acik(client, seeded_db, user_factory, sayfa):
    anahtar = f"liste_{sayfa.split('.')[1]}"
    await _rol_hucreli(seeded_db, anahtar, {sayfa: PageLevel.view})
    basliklar = await _baslik(client, user_factory, anahtar)
    assert (await client.get("/roles", headers=basliklar)).status_code == 200


async def test_roles_hicbir_sayfa_hucresi_olmayan_403(client, seeded_db, user_factory):
    await _rol_hucreli(seeded_db, "bos_rol", {})
    basliklar = await _baslik(client, user_factory, "bos_rol")
    assert (await client.get("/roles", headers=basliklar)).status_code == 403


async def test_sistem_yoneticisi_rol_uclarini_acar(client, seeded_db, user_factory):
    basliklar = await _baslik(client, user_factory, "system_admin")
    rid = await _rol_id(seeded_db, "system_admin")
    for yol in ("/roles", "/modules", f"/roles/{rid}/permissions", f"/roles/{rid}/pages"):
        assert (await client.get(yol, headers=basliklar)).status_code == 200, yol


# --- Madde 8 ------------------------------------------------------------------------------------

ORANLAR = {
    "sgk_employee_pct": "14.000",
    "unemployment_employee_pct": "1.000",
    "income_tax_pct": None,
    "stamp_tax_pct": "0.759",
    "sgk_employer_pct": "20.500",
    "unemployment_employer_pct": "2.000",
    "short_work_pct": "1.000",
}


async def test_bordro_duzenler_ama_bordro_oranlari_yok_oran_PUT_403(
    client, seeded_db, user_factory
):
    await _rol_hucreli(
        seeded_db,
        "bordro_duz",
        {"mali.bordro": PageLevel.edit, "mali.sgk_bildirimi": PageLevel.edit},
    )
    basliklar = await _baslik(client, user_factory, "bordro_duz")
    yanit = await client.put("/payroll/rates/2027/company", json=ORANLAR, headers=basliklar)
    assert yanit.status_code == 403, yanit.text


async def test_bordro_oranlari_duzenler_oran_PUT_200(client, seeded_db, user_factory):
    await _rol_hucreli(seeded_db, "oran_duz", {"ayarlar.bordro_oranlari": PageLevel.edit})
    basliklar = await _baslik(client, user_factory, "oran_duz")
    yanit = await client.put("/payroll/rates/2027/company", json=ORANLAR, headers=basliklar)
    assert yanit.status_code == 200, yanit.text


async def test_oran_ucu_vergi_dilimi_ucuyla_ayni_kapi(client, seeded_db, user_factory):
    """Aynı sayfa → iki uç da aynı karar (ayrışma yok)."""
    await _rol_hucreli(seeded_db, "oran_gorur", {"ayarlar.bordro_oranlari": PageLevel.view})
    basliklar = await _baslik(client, user_factory, "oran_gorur")
    oran = await client.put("/payroll/rates/2027/company", json=ORANLAR, headers=basliklar)
    dilim = await client.put(
        "/payroll/tax-brackets/2027/wage", json={"brackets": []}, headers=basliklar
    )
    assert oran.status_code == dilim.status_code == 403

"""IZN-B5b (ajan C) — madde 7: teklif/sözleşme/katalog yazmaları SAYFA BAŞINA.

Eskiden 36 yazma ucunun hepsi `contracts:full` idi: dokuz sayfanın HERHANGİ BİRİNİN Düzenler
bayrağı (örn. yalnız `teklif.sablonlar`) hepsini açıyordu. Şimdi her uç kendi sayfasının (ya da
CEO kararı 3/5 ile belirlenen sayfa kümesinin) Düzenler bayrağına bağlıdır.

Tablo UYGULAMADAN BAĞIMSIZ elle yazıldı. Her sayfa için TEK hücreli (yalnız o sayfada Düzenler)
özel rol kurulur ve 36 ucun HEPSİ çağrılır: kapı reddederse 403, geçerse (gövde/kayıt hatası dahil)
403 DIŞI bir yanıt gelir. Beklenen: 403 ⇔ sayfa ucun kümesinde DEĞİL.
"""

import uuid

import pytest
from sqlalchemy import update

from app.core.sayfalar import PageLevel
from app.modules.roles.models import RolePagePermission
from tests._ekip_dunyasi import rol_kur

pytestmark = pytest.mark.asyncio

SIFRE = "parola1234"

_KATALOG = ("teklif.is_kalemi_katalogu",)
_DAGILIM = ("teklif.poz_dagilimi", "proje.is_kalemleri")  # CEO kararı 3
_ISVEREN = ("teklif.isveren_sozlesme", "proje.is_kalemleri")
_FIRMA_EKLE = ("teklif.taseron_firmalar", "teklif.sozlesmeler")  # CEO kararı 5
_FIRMA = ("teklif.taseron_firmalar",)
_SOZ_OLUSTUR = ("teklif.sozlesmeler", "teklif.taseron_sozlesme")  # CEO kararı 5
_SOZ = ("teklif.taseron_sozlesme",)
_SABLON = ("teklif.sablonlar",)
_TEKLIF = ("teklif.teklif_hazirlama",)

_OFFER = "/offers/{offer_id}"
_REV = _OFFER + "/revisions/{rev_no}"

#: (yöntem, yol) → ucu açan sayfalar (herhangi biri yeter). 36 satır.
UCLAR: dict[tuple[str, str], tuple[str, ...]] = {
    ("POST", "/catalog/items"): _KATALOG,
    ("POST", "/catalog/items/bulk"): _KATALOG,
    ("PATCH", "/catalog/items/{item_id}"): _KATALOG,
    ("PUT", "/projects/{project_id}/contract/distribution"): _DAGILIM,
    ("POST", "/projects/{project_id}/contract/groups"): _ISVEREN,
    ("POST", "/projects/{project_id}/contract/items"): _ISVEREN,
    ("POST", "/projects/{project_id}/contract/items/bulk"): _ISVEREN,
    ("PATCH", "/contracts/employer/groups/{group_id}"): _ISVEREN,
    ("PATCH", "/contracts/employer/items/{item_id}"): _ISVEREN,
    ("POST", "/subcontractors"): _FIRMA_EKLE,
    ("PATCH", "/subcontractors/{subcontractor_id}"): _FIRMA,
    ("POST", "/projects/{project_id}/subcontractor-contracts"): _SOZ_OLUSTUR,
    ("PATCH", "/subcontractor-contracts/{contract_id}"): _SOZ,
    ("POST", "/subcontractor-contracts/{contract_id}/items"): _SOZ,
    ("PATCH", "/subcontractor-contracts/items/{item_id}"): _SOZ,
    ("POST", "/subcontractor-contracts/{contract_id}/items/load-from-employer"): _SOZ,
    ("POST", "/offers/templates"): _SABLON,
    ("POST", "/offers/templates/from-offer"): _SABLON,
    ("PATCH", "/offers/templates/{template_id}"): _SABLON,
    ("PUT", "/offers/templates/{template_id}/content"): _SABLON,
    ("POST", "/offers/templates/{template_id}/default"): _SABLON,
    ("POST", "/offers/templates/{template_id}/copy"): _SABLON,
    ("PUT", "/offers/settings"): _TEKLIF,
    ("POST", "/offers"): _TEKLIF,
    ("PATCH", _OFFER): _TEKLIF,
    ("POST", _OFFER + "/revisions"): _TEKLIF,
    ("PATCH", _REV): _TEKLIF,
    ("POST", _REV + "/send"): _TEKLIF,
    ("POST", _REV + "/win"): _TEKLIF,
    ("POST", _REV + "/lose"): _TEKLIF,
    ("POST", _REV + "/withdraw"): _TEKLIF,
    ("POST", _REV + "/groups"): _TEKLIF,
    ("PATCH", _REV + "/groups/{group_id}"): _TEKLIF,
    ("POST", _REV + "/items"): _TEKLIF,
    ("POST", _REV + "/items/bulk"): _TEKLIF,
    ("PATCH", _REV + "/items/{item_id}"): _TEKLIF,
}

SAYFALAR = sorted({p for pages in UCLAR.values() for p in pages})


def _doldur(yol: str) -> str:
    ayni = uuid.uuid4()
    return yol.format(
        offer_id=ayni,
        rev_no=1,
        project_id=ayni,
        item_id=ayni,
        group_id=ayni,
        contract_id=ayni,
        subcontractor_id=ayni,
        template_id=ayni,
    )


async def test_tablo_36_uc_ve_9_sayfa() -> None:
    assert len(UCLAR) == 36
    assert len(SAYFALAR) == 9


async def _oturum(client, session, user_factory, key: str, sayfa: str, level: PageLevel):
    rol = await rol_kur(session, key, PageLevel.none)
    await session.execute(
        update(RolePagePermission)
        .where(RolePagePermission.role_id == rol.id, RolePagePermission.page_key == sayfa)
        .values(level=level)
    )
    await session.flush()
    email = f"{key}@b5bc.co"
    await user_factory(email=email, password=SIFRE, role_key=key)
    yanit = await client.post("/auth/login", json={"email": email, "password": SIFRE})
    assert yanit.status_code == 200, yanit.text
    return {"Authorization": f"Bearer {yanit.json()['access_token']}"}


async def _durumlar(client, basliklar) -> dict[tuple[str, str], int]:
    out = {}
    for yontem, yol in UCLAR:
        yanit = await client.request(yontem, _doldur(yol), json={}, headers=basliklar)
        out[(yontem, yol)] = yanit.status_code
    return out


@pytest.mark.parametrize("sayfa", SAYFALAR)
async def test_tek_sayfanin_duzenleri_yalniz_kendi_uclarini_acar(
    client, seeded_db, user_factory, sayfa
) -> None:
    """(i) komşu sayfanın Düzenler'i bu ucu AÇMAZ (403) · (ii) doğru sayfanınki açar (403 DIŞI)."""
    anahtar = "c_" + sayfa.replace(".", "_")
    basliklar = await _oturum(client, seeded_db, user_factory, anahtar, sayfa, PageLevel.edit)
    durumlar = await _durumlar(client, basliklar)
    yanlis = {
        uc: (kod, sayfa in UCLAR[uc])
        for uc, kod in durumlar.items()
        if (kod != 403) != (sayfa in UCLAR[uc])
    }
    assert yanlis == {}, f"{sayfa}: (durum, kümede mi) beklenmeyen: {yanlis}"


@pytest.mark.parametrize("sayfa", SAYFALAR)
async def test_yalniz_gorur_hicbir_yazma_ucunu_acmaz(
    client, seeded_db, user_factory, sayfa
) -> None:
    anahtar = "cv_" + sayfa.replace(".", "_")
    basliklar = await _oturum(client, seeded_db, user_factory, anahtar, sayfa, PageLevel.view)
    durumlar = await _durumlar(client, basliklar)
    assert {uc for uc, kod in durumlar.items() if kod != 403} == set()

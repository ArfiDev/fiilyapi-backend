"""IZN-B4c madde 20 — eski rollerin gizli alan kümeleri (CEO onaylı), GERÇEK HTTP.

B1, eski `limited` kapsamı rol için KÜRESEL `tum_tutarlar`a çevirmişti; yeni maske küresel olduğu
için İK Müdürü ücret/bordroyu, Satınalma kendi fiyatlarını göremiyordu. Onaylı tablo:

* hr_manager / site_chief / field_engineer → {sozlesme_fiyat, maliyet_kar, banka_kasa, satis_alici}
* procurement → {sozlesme_fiyat, satis_alici, banka_kasa}
* patron / accounting / project_manager → boş; yeni roller (`IZN_HIDDEN_FIELDS`) AYNEN.

Migration ↔ seed eşitliği `test_izn_b4c_migration.py`tedir; bordro tarafı
`payroll/test_izn_b4c_payroll_maske.py`tedir.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.sayfalar import HiddenCategory
from app.modules.roles import seed_data
from app.modules.roles.models import Role, RoleHiddenField
from tests.modules._boq import _auth, _group, _item, _login_with_access, _site

H = HiddenCategory
_SAHA_VE_IK = {H.sozlesme_fiyat, H.maliyet_kar, H.banka_kasa, H.satis_alici}
_BUTCE = Decimal("8400000.00")


@pytest.fixture(autouse=True)
async def _izn_tohumu(seeded_db):
    """13 rolün sayfa hücreleri + gizli alan bayrakları (`seed_data.HIDDEN_FIELDS`) + `viewer`."""
    await seed_data.seed_izn_reference_data(seeded_db)


def test_onayli_kume_tablosu_ve_yeni_roller_aynen() -> None:
    hf = seed_data.HIDDEN_FIELDS
    for rol in ("hr_manager", "site_chief", "field_engineer"):
        assert hf[rol] == _SAHA_VE_IK, rol
        assert H.maas_kisisel not in hf[rol] and H.tum_tutarlar not in hf[rol]
    assert hf["procurement"] == {H.sozlesme_fiyat, H.satis_alici, H.banka_kasa}
    for rol in ("patron", "accounting", "project_manager"):
        assert hf[rol] == frozenset(), rol
    assert hf["planning_engineer"] == {H.tum_tutarlar}
    assert hf["warehouse_keeper"] == {H.tum_tutarlar}
    assert hf["viewer"] == {H.tum_tutarlar, H.maas_kisisel}
    assert hf["cost_engineer"] == {H.maas_kisisel}
    assert hf["technical_office"] == frozenset() == hf["finance_manager"]
    # Tablo yalnız tanımlı eski rollere ait; kalan eski roller boş küme.
    assert set(seed_data.ESKI_ROL_GIZLI_ALANLAR) <= set(seed_data.ROLE_ORDER)
    assert "system_admin" not in hf


async def test_seed_edilmis_db_tabloyla_ayni(db_session) -> None:
    rows = (
        await db_session.execute(
            select(Role.key, RoleHiddenField.category).join(
                RoleHiddenField, RoleHiddenField.role_id == Role.id
            )
        )
    ).all()
    db: dict[str, set[HiddenCategory]] = {}
    for key, category in rows:
        db.setdefault(key, set()).add(category)
    for key, kume in seed_data.HIDDEN_FIELDS.items():
        assert db.get(key, set()) == set(kume), key


async def _santiye(db_session, project_factory):
    project = await project_factory("M20-1")
    site = await _site(db_session, project, code="M20-S", budget=_BUTCE)
    return project, site


async def _giris(client, db_session, user_factory, rol: str):
    token = await _login_with_access(client, db_session, user_factory, rol, f"{rol}@m20.co")
    return _auth(token)


async def _personel(client, headers) -> str:
    yanit = await client.post(
        "/personnel",
        json={
            "full_name": "Ahmet Yılmaz",
            "source": "company",
            "wage_type": "daily",
            "wage_amount": "1500.00",
        },
        headers=headers,
    )
    assert yanit.status_code == 201, yanit.text
    return yanit.json()["id"]


async def test_hr_manager_ucreti_GORUR_ve_yazar_sozlesme_butce_gizli(
    client, db_session, user_factory, project_factory
) -> None:
    _p, site = await _santiye(db_session, project_factory)
    ik = await _giris(client, db_session, user_factory, "hr_manager")

    pid = await _personel(client, ik)  # POST: ücret dolu → 403 OLMAZ (maas_kisisel açık)
    detay = await client.get(f"/personnel/{pid}", headers=ik)
    assert detay.status_code == 200, detay.text
    assert detay.json()["wage_amount"] == "1500.00", "İK MÜDÜRÜ ÜCRETİ GÖRMÜYOR"
    guncel = await client.patch(f"/personnel/{pid}", json={"wage_amount": "1600.00"}, headers=ik)
    assert guncel.status_code == 200, guncel.text
    assert guncel.json()["wage_amount"] == "1600.00"

    # sozlesme_fiyat / maliyet_kar etiketli alanlar hâlâ gizli.
    sayfa = await client.get(f"/sites/{site.id}", headers=ik)
    assert sayfa.status_code == 200, sayfa.text
    govde = sayfa.json()
    assert govde["budget"] is None, "ŞANTİYE BÜTÇESİ (maliyet_kar) İK'YA SIZDI"
    assert govde["contract_amount"]["value"] is None, "SÖZLEŞME BEDELİ İK'YA SIZDI"


async def test_site_chief_ucreti_gorur_para_alanlari_gizli(
    client, db_session, user_factory, project_factory
) -> None:
    _p, site = await _santiye(db_session, project_factory)
    ik = await _giris(client, db_session, user_factory, "hr_manager")
    pid = await _personel(client, ik)
    sef = await _giris(client, db_session, user_factory, "site_chief")

    detay = await client.get(f"/personnel/{pid}", headers=sef)
    assert detay.status_code == 200, detay.text
    assert detay.json()["wage_amount"] == "1500.00", "maas_kisisel şef için AÇIK olmalı"
    sayfa = (await client.get(f"/sites/{site.id}", headers=sef)).json()
    assert sayfa["budget"] is None
    assert sayfa["contract_amount"]["value"] is None


async def test_procurement_maliyet_kar_GORUR_sozlesme_fiyati_gizli(
    client, db_session, user_factory, project_factory
) -> None:
    _p, site = await _santiye(db_session, project_factory)
    group = await _group(db_session, site)
    await _item(db_session, site, group, quantity=Decimal("10.000"), unit_price=Decimal("280.00"))
    satinalma = await _giris(client, db_session, user_factory, "procurement")

    sayfa = await client.get(f"/sites/{site.id}", headers=satinalma)
    assert sayfa.status_code == 200, sayfa.text
    govde = sayfa.json()
    assert govde["budget"] == "8400000.00", "SATINALMA maliyet_kar'ı GÖRMELİ (madde 20)"
    assert govde["contract_amount"]["value"] is None, "SÖZLEŞME BEDELİ SATINALMAYA SIZDI"

    boq = await client.get(f"/sites/{site.id}/boq", headers=satinalma)
    assert boq.status_code == 200, boq.text
    kalem = boq.json()["groups"][0]["items"][0]
    assert kalem["unit_price"] is None, "BOQ BİRİM FİYATI (sozlesme_fiyat) SATINALMAYA SIZDI"


async def test_viewer_hala_hepsini_gizli_gorur(
    client, db_session, user_factory, project_factory
) -> None:
    _p, site = await _santiye(db_session, project_factory)
    ik = await _giris(client, db_session, user_factory, "hr_manager")
    pid = await _personel(client, ik)
    izleyici = await _giris(client, db_session, user_factory, "viewer")

    detay = await client.get(f"/personnel/{pid}", headers=izleyici)
    assert detay.status_code == 200, detay.text
    assert detay.json()["wage_amount"] is None, "GÖRÜNTÜLEYİCİ ÜCRETİ GÖRDÜ"
    sayfa = (await client.get(f"/sites/{site.id}", headers=izleyici)).json()
    assert sayfa["budget"] is None
    assert sayfa["contract_amount"]["value"] is None

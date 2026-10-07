"""TKL-B4.1 — `GET/PUT /offers/settings` (teklif varsayilanlari, tekil satir).

Izin (GECICI, T25 deseni): okuma `contracts:view`, yazma `contracts:full` + `RequireUnrestricted`.
`site_chief`/`field_engineer` (contracts=none) 403. Yuzdeler yuzde biriminde (12 = %12).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.access import AccessLevel
from app.core.sayfalar import HiddenCategory
from app.modules.audit.models import AuditAction
from app.modules.offers.models import OfferSettings
from app.modules.users.models import User
from tests._hassas_alan import rol_gizle
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz
from tests._proje_ekibi import baska_projede_disiplinli

from .._boq import _audit_details, _auth, _login_with_access

URL = "/offers/settings"

VARSAYILAN_ODEME = "Ödeme aylık hakedişle, 30 gün vadeli"


def _govde(**over) -> dict:
    base = {
        "default_overhead_pct": "10.50",
        "default_profit_pct": "18",
        "default_vat_pct": "10",
        "default_validity_days": 45,
        "default_payment_terms": "%30 avans, kalanı hakedişle",
    }
    return {**base, **over}


async def _giris(client, db_session, user_factory, role_key: str) -> dict[str, str]:
    token = await _login_with_access(
        client, db_session, user_factory, role_key, f"{role_key}.{uuid.uuid4().hex[:6]}@tkl.co"
    )
    return _auth(token)


@pytest.fixture
async def admin(client, db_session, user_factory, seeded_db):
    return await _giris(client, db_session, user_factory, "system_admin")


# ---------------------------------------------------------------- mutlu yol


async def test_get_varsayilan_12_15_20_30_gun_ve_odeme_metni(client, admin) -> None:
    resp = await client.get(URL, headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert Decimal(body["default_overhead_pct"]) == Decimal("12")
    assert Decimal(body["default_profit_pct"]) == Decimal("15")
    assert Decimal(body["default_vat_pct"]) == Decimal("20")
    assert body["default_validity_days"] == 30
    assert body["default_payment_terms"] == VARSAYILAN_ODEME
    assert "updated_at" in body
    assert not [k for k in body if "currency" in k or "para_birimi" in k]  # yalniz TL


async def test_put_gunceller_get_yansitir_ve_denetim_yazar(client, admin, db_session) -> None:
    resp = await client.put(URL, json=_govde(), headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert Decimal(body["default_overhead_pct"]) == Decimal("10.5")
    assert Decimal(body["default_profit_pct"]) == Decimal("18")
    assert Decimal(body["default_vat_pct"]) == Decimal("10")
    assert body["default_validity_days"] == 45
    assert body["default_payment_terms"] == "%30 avans, kalanı hakedişle"

    again = (await client.get(URL, headers=admin)).json()
    assert again == body

    assert await _audit_details(db_session, AuditAction.update) == [
        "Teklif ayarları güncellendi: genel gider %12.00 → %10.50 · kâr %15.00 → %18.00 · "
        "KDV %20.00 → %10.00 · geçerlilik 30 → 45 gün · "
        f"ödeme koşulu «{VARSAYILAN_ODEME}» → «%30 avans, kalanı hakedişle»"
    ]


async def test_E8_yalniz_odeme_kosulu_degisince_metinde_yalniz_eski_yeni_odeme_kosulu(
    client, admin, db_session
) -> None:
    """Degisen HER alan `eski → yeni`; degismeyenler metinde YOK (ayar tohum degerlerinde)."""
    resp = await client.put(
        URL,
        json=_govde(
            default_overhead_pct="12",
            default_profit_pct="15",
            default_vat_pct="20",
            default_validity_days=30,
            default_payment_terms="Peşin",
        ),
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert await _audit_details(db_session, AuditAction.update) == [
        f"Teklif ayarları güncellendi: ödeme koşulu «{VARSAYILAN_ODEME}» → «Peşin»"
    ]


async def test_E8_degisiklik_yoksa_denetim_satiri_YAZILMAZ(client, admin, db_session) -> None:
    ayni = _govde(
        default_overhead_pct="12",
        default_profit_pct="15",
        default_vat_pct="20",
        default_validity_days=30,
        default_payment_terms=VARSAYILAN_ODEME,
    )
    resp = await client.put(URL, json=ayni, headers=admin)
    assert resp.status_code == 200, resp.text  # 200 + mevcut kayit
    assert await _audit_details(db_session, AuditAction.update) == []
    # degisiklikten sonra ayni gövde tekrarlanirsa da ikinci satir YOK
    await client.put(URL, json=_govde(), headers=admin)
    await client.put(URL, json=_govde(), headers=admin)
    assert len(await _audit_details(db_session, AuditAction.update)) == 1


async def test_E8_uzun_odeme_kosulu_denetim_metninde_kisaltilir(client, admin, db_session) -> None:
    uzun = "A" * 200
    await client.put(URL, json=_govde(default_payment_terms=uzun), headers=admin)
    [metin] = await _audit_details(db_session, AuditAction.update)
    assert f"«{'A' * 60}…»" in metin
    assert "A" * 61 not in metin


async def test_tekil_satir_iki_put_sonrasi_hala_tek(client, admin, db_session) -> None:
    await client.get(URL, headers=admin)
    await client.put(URL, json=_govde(), headers=admin)
    await client.put(URL, json=_govde(default_vat_pct="8"), headers=admin)
    assert await db_session.scalar(select(func.count()).select_from(OfferSettings)) == 1
    assert Decimal((await client.get(URL, headers=admin)).json()["default_vat_pct"]) == 8


async def test_sinir_degerleri_kabul_edilir(client, admin) -> None:
    resp = await client.put(
        URL,
        json=_govde(
            default_overhead_pct="100",
            default_profit_pct="999.99",
            default_vat_pct="0",
            default_validity_days=365,
        ),
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    low = await client.put(
        URL,
        json=_govde(default_overhead_pct="0", default_profit_pct="0", default_validity_days=1),
        headers=admin,
    )
    assert low.status_code == 200, low.text


# ------------------------------------------------------------------- 422


@pytest.mark.parametrize(
    "ezme",
    [
        {"default_overhead_pct": "100.01"},
        {"default_overhead_pct": "-0.01"},
        {"default_profit_pct": "1000"},
        {"default_profit_pct": "-1"},
        {"default_vat_pct": "100.01"},
        {"default_vat_pct": "-5"},
        {"default_validity_days": 0},
        {"default_validity_days": 366},
        {"default_validity_days": -3},
        {"default_payment_terms": ""},
        {"default_payment_terms": "   "},
        {"default_overhead_pct": "12.345"},
        {"para_birimi": "USD"},
    ],
)
async def test_aralik_disi_ve_bicim_hatasi_422(client, admin, ezme) -> None:
    resp = await client.put(URL, json=_govde(**ezme), headers=admin)
    assert resp.status_code == 422, resp.text
    # reddedilen istek hicbir seyi degistirmez
    assert Decimal((await client.get(URL, headers=admin)).json()["default_overhead_pct"]) == 12


@pytest.mark.parametrize(
    "eksik",
    [
        "default_overhead_pct",
        "default_profit_pct",
        "default_vat_pct",
        "default_validity_days",
        "default_payment_terms",
    ],
)
async def test_put_TAM_degistirmedir_eksik_alan_422(client, admin, eksik) -> None:
    govde = _govde()
    del govde[eksik]
    assert (await client.put(URL, json=govde, headers=admin)).status_code == 422


# ------------------------------------------------------------------- izin


async def test_kimliksiz_istek_401(client) -> None:
    assert (await client.get(URL)).status_code == 401
    assert (await client.put(URL, json=_govde())).status_code == 401


@pytest.mark.parametrize("role_key", ["site_chief", "field_engineer"])
async def test_contracts_yok_roller_403(client, admin, db_session, user_factory, role_key) -> None:
    from app.modules.roles.seed_data import MATRIX, ROLE_ORDER

    assert MATRIX["contracts"][ROLE_ORDER.index(role_key)] == AccessLevel.none  # on kosul
    kisi = await _giris(client, db_session, user_factory, role_key)
    assert (await client.get(URL, headers=kisi)).status_code == 403
    assert (await client.put(URL, json=_govde(), headers=kisi)).status_code == 403
    # reddedilen yazma hicbir seyi degistirmedi
    assert Decimal((await client.get(URL, headers=admin)).json()["default_vat_pct"]) == 20


async def test_contracts_view_okur_ama_yazamaz(client, admin, db_session, user_factory) -> None:
    await modul_duzeyi_yaz(db_session, "accounting", "contracts", AccessLevel.view)
    muhasebe = await _giris(client, db_session, user_factory, "accounting")
    assert (await client.get(URL, headers=muhasebe)).status_code == 200
    assert (await client.put(URL, json=_govde(), headers=muhasebe)).status_code == 403
    assert Decimal((await client.get(URL, headers=admin)).json()["default_vat_pct"]) == 20


async def test_proje_basina_disiplinli_kullanici_ayarlari_gorur_ve_yazar(
    client, admin, db_session, user_factory
) -> None:
    from app.modules.catalog.models import ContractorType, EvDiscipline

    disiplin = EvDiscipline(
        code="KAB", name="Kaba", color="#2563EB", default_contractor_type=ContractorType.OWN
    )
    db_session.add(disiplin)
    await db_session.flush()
    token = await _login_with_access(
        client, db_session, user_factory, "project_manager", "pm.kisitli.offers@tkl.co"
    )
    user_id = (
        await db_session.execute(select(User.id).where(User.email == "pm.kisitli.offers@tkl.co"))
    ).scalar_one()
    await baska_projede_disiplinli(db_session, user_id, disiplin.id)
    kisitli = _auth(token)

    # IZN-B3: teklif ayarlari sirket geneli — proje basina disiplin kisiti etkilemez (R5 kalkti)
    assert (await client.get(URL, headers=kisitli)).status_code == 200
    assert (await client.put(URL, json=_govde(), headers=kisitli)).status_code == 200
    # PUT kisitli kullanicinin govdesini yazdi (403 degil)
    assert Decimal((await client.get(URL, headers=admin)).json()["default_vat_pct"]) == Decimal(
        _govde()["default_vat_pct"]
    )


async def test_maliyet_kar_gizli_rolde_genel_gider_ve_kar_varsayilani_gizlenir_KDV_ve_gun_durur(
    client, admin, db_session, user_factory
) -> None:
    """IZN-B4a: varsayılan genel gider/kâr oranları `maliyet_kar` (revizyon oranlarıyla geri
    hesaplanabilir); KDV oranı ve geçerlilik günü herkese açık. Bayraksız rol hepsini görür."""
    await modul_duzeyi_yaz(db_session, "accounting", "contracts", AccessLevel.view)
    await rol_gizle(db_session, "accounting")
    acik = (
        await client.get(URL, headers=await _giris(client, db_session, user_factory, "accounting"))
    ).json()
    assert Decimal(acik["default_profit_pct"]) == 15  # POZİTİF KONTROL

    await rol_gizle(db_session, "accounting", HiddenCategory.maliyet_kar)
    gizli = (
        await client.get(URL, headers=await _giris(client, db_session, user_factory, "accounting"))
    ).json()
    assert gizli["default_profit_pct"] is None
    assert gizli["default_overhead_pct"] is None
    assert Decimal(gizli["default_vat_pct"]) == 20
    assert gizli["default_validity_days"] == 30

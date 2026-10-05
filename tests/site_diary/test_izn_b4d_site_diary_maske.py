"""IZN-B4d — şantiye günlüğü hassas alan maskesi: maliyet gizli rolde fiyat/tutar `null`, miktar
görünür; bayraksız rol hepsini görür (POZİTİF KONTROL). Günlük yazma gövdesinde parasal alan
YOKTUR (fiyat BOQ'dan gelir, `extra=forbid`) → "PUT/PATCH'te gizli alan dolu → 403" bu modülde
uygulanacak alan bulmaz; onun yerine gizli rolün miktar/hava yazabildiği ve fiyat göndermenin
422 olduğu kanıtlanır.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.core.sayfalar import HiddenCategory
from tests._hassas_alan import rol_gizle

pytestmark = pytest.mark.asyncio

_TARIH = date(2026, 7, 15)


@pytest.fixture
async def gonderilmis_gun(gunluk_api, sef_headers, santiye, sozlesme_kalemi_fabrikasi):
    """Gönderilmiş günlük: 01.001 × 10 (21.500) + 02.001 × 4 (1.850); 01.001 sözleşmeye köprülü."""
    site, proje, kalemler = santiye
    await sozlesme_kalemi_fabrikasi(kalemler[0], proje, quantity=Decimal("200.000"))
    govde = await gunluk_api(
        sef_headers,
        site.id,
        _TARIH,
        [
            {"boq_item_id": str(kalemler[0].id), "quantity": "10"},
            {"boq_item_id": str(kalemler[1].id), "quantity": "4"},
        ],
    )
    return site, govde


async def test_bayraksiz_rol_fiyat_ve_tutarlari_gorur(
    client, sef_headers, seeded_db, gonderilmis_gun
):
    site, govde = gonderilmis_gun
    await rol_gizle(seeded_db, "site_chief")  # POZİTİF KONTROL: hiçbir kategori gizli değil

    detay = (await client.get(f"/diary/{govde['id']}", headers=sef_headers)).json()
    assert Decimal(detay["lines_total"]) == Decimal("222400.00")
    satir = detay["lines"][0]
    assert Decimal(satir["unit_price"]) == Decimal("21500.00")
    assert Decimal(satir["line_amount"]) == Decimal("215000.00")

    ozet = (
        await client.get(f"/sites/{site.id}/diary/summary?year=2026&month=7", headers=sef_headers)
    ).json()
    assert Decimal(ozet["total_amount"]) == Decimal("222400.00")
    assert Decimal(ozet["items"][0]["contract_item_unit_price"]) == Decimal("1850.00")


async def test_maliyet_kar_gizli_rolde_tutar_null_miktar_gorunur(
    client, sef_headers, seeded_db, gonderilmis_gun
):
    site, govde = gonderilmis_gun
    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)

    detay = (await client.get(f"/diary/{govde['id']}", headers=sef_headers)).json()
    assert detay["lines_total"] is None
    satir = detay["lines"][0]
    assert satir["unit_price"] is None and satir["line_amount"] is None
    assert satir["quantity"] == "10.000"
    assert satir["cumulative_quantity"] == "10.000"
    assert satir["code"] == "01.001"

    liste = (await client.get(f"/sites/{site.id}/diary", headers=sef_headers)).json()
    assert liste["items"][0]["lines_total"] is None

    ozet = (
        await client.get(f"/sites/{site.id}/diary/summary?year=2026&month=7", headers=sef_headers)
    ).json()
    assert ozet["total_amount"] is None
    kalem = ozet["items"][0]
    assert kalem["amount"] is None and kalem["unit_price"] is None
    assert kalem["boq_amount"] is None and kalem["contract_item_unit_price"] is None
    assert Decimal(kalem["quantity"]) == Decimal("10")
    assert Decimal(kalem["boq_quantity"]) == Decimal("200")
    assert Decimal(kalem["completion_ratio"]) == Decimal("0.05")

    iskelet = (
        await client.get(
            f"/sites/{site.id}/diary/skeleton?entry_date=2026-07-16", headers=sef_headers
        )
    ).json()
    assert iskelet["lines_total"] is None
    assert iskelet["lines"][0]["unit_price"] is None
    assert iskelet["lines"][0]["line_amount"] is None


async def test_maliyet_kar_gizli_rol_yazabilir_yanit_maskeli_fiyat_gondermek_422(
    client, sef_headers, seeded_db, santiye
):
    site, _, kalemler = santiye
    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)

    # POST serbest, yanıt maskeli.
    yeni = await client.post(
        f"/sites/{site.id}/diary", json={"entry_date": _TARIH.isoformat()}, headers=sef_headers
    )
    assert yeni.status_code == 201, yeni.text
    assert yeni.json()["lines_total"] is None
    entry_id = yeni.json()["id"]

    # Miktar (parasal değil) gizli rolde yazılır; yanıtta miktar görünür, tutar null.
    miktar = await client.put(
        f"/diary/{entry_id}/lines",
        json={"lines": [{"boq_item_id": str(kalemler[0].id), "quantity": "7"}]},
        headers=sef_headers,
    )
    assert miktar.status_code == 200, miktar.text
    satir = next(s for s in miktar.json()["lines"] if s["code"] == "01.001")
    assert satir["quantity"] == "7.000" and satir["line_amount"] is None

    hava = await client.patch(
        f"/diary/{entry_id}", json={"temp_max_c": "31.5", "wind_ms": "4.0"}, headers=sef_headers
    )
    assert hava.status_code == 200, hava.text
    assert hava.json()["temp_max_c"] == "31.5"

    # Fiyat/tutar gövdede zaten YOK: gönderilirse 422 (maske değil şema reddeder).
    fiyatli = await client.put(
        f"/diary/{entry_id}/lines",
        json={"lines": [{"boq_item_id": str(kalemler[0].id), "quantity": "7", "unit_price": "1"}]},
        headers=sef_headers,
    )
    assert fiyatli.status_code == 422, fiyatli.text

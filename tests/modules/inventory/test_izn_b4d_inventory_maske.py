"""IZN-B4d — stok hassas alan maskesi: maliyet gizli rolde fiyat/değer `null`, miktar/bakiye
görünür, POST serbest, bayraksız rol hepsini görür (POZİTİF KONTROL).

Stokta PUT/PATCH ile yazılan `maliyet_kar` alanı YOKTUR (`PATCH /stock/items` yalnız `min_stock`
= `yok` taşır; fiyat yalnız POST hareket satırındadır), bu yüzden 403 testi yoktur.
"""

import pytest

from app.core.sayfalar import HiddenCategory
from app.modules.inventory.models import StockCategory
from tests._hassas_alan import rol_gizle

_GUN = "2026-07-27"


@pytest.fixture
async def stok(client, admin_headers, depo_fabrikasi, kart_fabrikasi):
    """Bir depoya fiyatlı bir giriş: 10 Ton × 21500,00."""
    depo = await depo_fabrikasi("D-1 Ambar")
    kart = await kart_fabrikasi(
        "SNK-0421", category=StockCategory.steel, unit="Ton", min_stock="4.000"
    )
    yanit = await client.post(
        "/stock/entries",
        json={
            "entry_type": "purchase",
            "entry_date": _GUN,
            "warehouse_id": str(depo.id),
            "lines": [{"item_id": str(kart.id), "quantity": "10.000", "unit_price": "21500.00"}],
        },
        headers=admin_headers,
    )
    assert yanit.status_code == 201, yanit.text
    return depo, kart


@pytest.mark.asyncio
async def test_bayraksiz_rol_fiyat_ve_degeri_gorur(client, satinalma_headers, stok):
    govde = (await client.get("/stock/summary", headers=satinalma_headers)).json()
    assert govde["items"][0]["last_unit_price"] == "21500.00"
    assert govde["kpis"]["total_value"] == "215000.00"


@pytest.mark.asyncio
async def test_maliyet_kar_gizli_rolde_tutar_null_miktar_gorunur(
    client, satinalma_headers, seeded_db, stok
):
    await rol_gizle(seeded_db, "procurement", HiddenCategory.maliyet_kar)

    ozet = (await client.get("/stock/summary", headers=satinalma_headers)).json()
    satir = ozet["items"][0]
    assert satir["last_unit_price"] is None
    assert ozet["kpis"]["total_value"] is None
    assert satir["balance"] == "10.000"
    assert satir["min_stock"] == "4.000"

    hareket = (await client.get("/stock/entries", headers=satinalma_headers)).json()
    kalem = hareket["items"][0]["lines"][0]
    assert kalem["unit_price"] is None
    assert kalem["quantity"] == "10.000"


@pytest.mark.asyncio
async def test_maliyet_kar_gizli_rol_hareket_POSTlayabilir(
    client, satinalma_headers, seeded_db, stok
):
    depo, kart = stok
    await rol_gizle(seeded_db, "procurement", HiddenCategory.maliyet_kar)
    yanit = await client.post(
        "/stock/entries",
        json={
            "entry_type": "purchase",
            "entry_date": _GUN,
            "warehouse_id": str(depo.id),
            "lines": [{"item_id": str(kart.id), "quantity": "1.000", "unit_price": "22000.00"}],
        },
        headers=satinalma_headers,
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["lines"][0]["unit_price"] is None

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


@pytest.fixture
async def santiye_stogu(
    client, admin_headers, gorunen_santiye, depo_fabrikasi, kart_fabrikasi, bolum_fabrikasi
):
    """Şantiye deposuna 10 Ton × 21500 giriş + bölüme 4 Ton sarf (fiyatlı düzeltme)."""
    depo = await depo_fabrikasi("D-S Ambar", site=gorunen_santiye)
    kart = await kart_fabrikasi("SNK-0500", category=StockCategory.steel, unit="Ton")
    bolum = await bolum_fabrikasi(gorunen_santiye, "A9", "A9 Kolon")
    for tip, miktar, bolum_id in (("purchase", "10.000", None), ("adjustment", "-4.000", bolum.id)):
        satir = {"item_id": str(kart.id), "quantity": miktar, "unit_price": "21500.00"}
        if bolum_id is not None:
            satir["section_id"] = str(bolum_id)
        yanit = await client.post(
            "/stock/entries",
            json={
                "entry_type": tip,
                "entry_date": _GUN,
                "warehouse_id": str(depo.id),
                "lines": [satir],
            },
            headers=admin_headers,
        )
        assert yanit.status_code == 201, yanit.text
    return gorunen_santiye, bolum


@pytest.mark.asyncio
async def test_santiye_stok_kpisi_total_value_gizli_rolde_null(
    client, satinalma_headers, seeded_db, santiye_stogu
):
    """INV1 — `SiteStockKpis.total_value`: bayraksız rol görür, gizli rolde `null`."""
    santiye, _ = santiye_stogu
    yol = f"/sites/{santiye.id}/stock"
    acik = await client.get(yol, headers=satinalma_headers)
    assert acik.status_code == 200, acik.text
    assert acik.json()["kpis"]["total_value"] is not None

    await rol_gizle(seeded_db, "procurement", HiddenCategory.maliyet_kar)
    gizli = (await client.get(yol, headers=satinalma_headers)).json()
    assert gizli["kpis"]["total_value"] is None
    assert gizli["kpis"]["total_items"] == acik.json()["kpis"]["total_items"]  # sayaç görünür


@pytest.mark.asyncio
async def test_bolum_stok_kpisi_issued_value_gizli_rolde_null(
    client, satinalma_headers, seeded_db, santiye_stogu
):
    """INV2 — `SectionStockKpis.issued_value` (+ `total_value`): gizli rolde `null`."""
    _, bolum = santiye_stogu
    yol = f"/sections/{bolum.id}/stock"
    acik = (await client.get(yol, headers=satinalma_headers)).json()
    assert acik["kpis"]["issued_value"] == "86000.00"
    assert acik["kpis"]["total_value"] is not None

    await rol_gizle(seeded_db, "procurement", HiddenCategory.maliyet_kar)
    gizli = (await client.get(yol, headers=satinalma_headers)).json()
    assert gizli["kpis"]["issued_value"] is None
    assert gizli["kpis"]["total_value"] is None
    assert gizli["items"][0]["issued_quantity"] == "4.000"

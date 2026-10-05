"""IZN-B4d — ekipman hassas alan maskesi: maliyet gizli rolde tutar `null`, saat/sayaç görünür,
PUT/PATCH'te dolu tutar 403, bayraksız rol hepsini görür (POZİTİF KONTROL)."""

from datetime import date
from decimal import Decimal

import pytest

from app.core.sayfalar import HiddenCategory
from app.modules.equipment.models import (
    EquipmentCategory,
    EquipmentFuelLog,
    EquipmentOwnership,
    EquipmentRatePeriod,
    EquipmentRentalInvoice,
    EquipmentRentalInvoiceLine,
    RentalInvoiceStatus,
    RentalLineKind,
)
from app.modules.procurement.models import PaymentTerms, Supplier
from tests._hassas_alan import rol_gizle


@pytest.fixture
async def makine(ekipman_fabrikasi, gorunen_santiye):
    return await ekipman_fabrikasi(
        "Ekskavatör EX-1",
        site=gorunen_santiye,
        category=EquipmentCategory.machinery,
        ownership=EquipmentOwnership.owned,
        purchase_amount="4250000.00",
        hourmeter_hours="1200.00",
    )


@pytest.mark.asyncio
async def test_bayraksiz_rol_tutari_gorur(client, sef_headers, makine):
    govde = (await client.get("/equipment", headers=sef_headers)).json()["items"][0]
    assert govde["purchase_amount"] == "4250000.00"


@pytest.mark.asyncio
async def test_maliyet_kar_gizli_rolde_tutar_null_sayac_gorunur(
    client, sef_headers, seeded_db, makine
):
    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)
    govde = (await client.get("/equipment", headers=sef_headers)).json()["items"][0]
    assert govde["purchase_amount"] is None
    assert govde["hourmeter_hours"] == "1200.00"


@pytest.mark.asyncio
async def test_maliyet_kar_gizli_rol_dolu_tutari_PATCHte_gonderemez(
    client, sef_headers, seeded_db, makine
):
    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)
    yanit = await client.patch(
        f"/equipment/{makine.id}", json={"purchase_amount": "1.00"}, headers=sef_headers
    )
    assert yanit.status_code == 403, yanit.text


@pytest.fixture
async def yakit(seeded_db, makine, gorunen_santiye):
    kayit = EquipmentFuelLog(
        equipment_id=makine.id,
        fuel_date=date(2026, 7, 5),
        site_id=gorunen_santiye.id,
        liters=Decimal("100.000"),
        unit_price=Decimal("40.0000"),
    )
    seeded_db.add(kayit)
    await seeded_db.flush()
    return kayit


@pytest.fixture
async def kira_faturasi(seeded_db, makine, gorunen_santiye):
    """Taslak kira faturası + bir `rented` kalem (kalemin bedeli ve hakediş toplamı maskelenir)."""
    tedarikci = Supplier(name="Kira Tedarikçi", payment_terms=PaymentTerms.days_30)
    seeded_db.add(tedarikci)
    await seeded_db.flush()
    fatura = EquipmentRentalInvoice(
        supplier_id=tedarikci.id,
        invoice_no="KF-1",
        invoice_amount=Decimal("100000.00"),
        period_year=2026,
        period_month=7,
        site_id=gorunen_santiye.id,
        rate_period=EquipmentRatePeriod.hourly,
        vat_rate=Decimal("0.20"),
        status=RentalInvoiceStatus.draft,
    )
    seeded_db.add(fatura)
    await seeded_db.flush()
    kalem = EquipmentRentalInvoiceLine(
        invoice_id=fatura.id,
        equipment_id=makine.id,
        line_kind=RentalLineKind.rented,
        site_id=gorunen_santiye.id,
        worked_hours=Decimal("10.00"),
        breakdown_hours=Decimal("0.00"),
        rate_amount=Decimal("320.00"),
        invoiced_hours=Decimal("10.00"),
    )
    seeded_db.add(kalem)
    await seeded_db.flush()
    return fatura, kalem


@pytest.mark.asyncio
async def test_yakit_amount_ve_unit_price_gizli_rolde_null_litre_gorunur(
    client, sef_headers, seeded_db, yakit
):
    """EQ1 — `FuelLogResponse.amount` (+ `unit_price`): bayraksız rol görür, gizli rolde `null`."""
    yol = f"/equipment/fuel-logs/{yakit.id}"
    acik = (await client.get(yol, headers=sef_headers)).json()
    assert Decimal(acik["amount"]) == Decimal("4000")
    assert acik["unit_price"] is not None

    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)
    gizli = (await client.get(yol, headers=sef_headers)).json()
    assert gizli["amount"] is None and gizli["unit_price"] is None
    assert Decimal(gizli["liters"]) == Decimal("100")


@pytest.mark.asyncio
async def test_makine_market_value_dolu_ya_da_null_PATCHte_403(
    client, sef_headers, seeded_db, makine
):
    """EQ2 — `EquipmentUpdate.market_value` yazma kapısı: dolu da `null` da 403."""
    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)
    for deger in ("1.00", None):
        yanit = await client.patch(
            f"/equipment/{makine.id}", json={"market_value": deger}, headers=sef_headers
        )
        assert yanit.status_code == 403, f"{deger!r}: {yanit.text}"


@pytest.mark.asyncio
async def test_yakit_unit_price_PATCHte_403(client, sef_headers, seeded_db, yakit):
    """EQ3 — `FuelLogUpdate.unit_price` yazma kapısı; bayraksız rolde aynı gövde 200 (pozitif)."""
    yol = f"/equipment/fuel-logs/{yakit.id}"
    acik = await client.patch(yol, json={"unit_price": "41.0000"}, headers=sef_headers)
    assert acik.status_code == 200, acik.text

    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)
    gizli = await client.patch(yol, json={"unit_price": "50.0000"}, headers=sef_headers)
    assert gizli.status_code == 403, gizli.text


@pytest.mark.asyncio
async def test_ozet_monthly_cost_gizli_rolde_null(client, sef_headers, seeded_db, makine):
    """EQ5 — `EquipmentSummaryResponse.monthly_cost`: bayraksız rolde sayı, gizli rolde `null`."""
    acik = (await client.get("/equipment/summary", headers=sef_headers)).json()
    assert acik["monthly_cost"] is not None

    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)
    gizli = (await client.get("/equipment/summary", headers=sef_headers)).json()
    assert gizli["monthly_cost"] is None
    assert gizli["monthly_cost_unknown_count"] == acik["monthly_cost_unknown_count"]


@pytest.mark.asyncio
async def test_kira_kalemi_rate_amount_PATCHte_403(client, sef_headers, seeded_db, kira_faturasi):
    """EQ9 — `RentalInvoiceLineUpdate.rate_amount` yazma kapısı (bayraksız rolde 200)."""
    _, kalem = kira_faturasi
    yol = f"/equipment/rental-invoice-lines/{kalem.id}"
    acik = await client.patch(yol, json={"rate_amount": "330.00"}, headers=sef_headers)
    assert acik.status_code == 200, acik.text

    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)
    gizli = await client.patch(yol, json={"rate_amount": "1.00"}, headers=sef_headers)
    assert gizli.status_code == 403, gizli.text


@pytest.mark.asyncio
async def test_kira_faturasi_toplamlari_gizli_rolde_null_saat_gorunur(
    client, sef_headers, seeded_db, kira_faturasi
):
    """EQ10 — `RentalInvoiceTotals.our_total` (+ KDV/ödenecek toplam): gizli rolde `null`."""
    fatura, _ = kira_faturasi
    yol = f"/equipment/rental-invoices/{fatura.id}"
    acik = (await client.get(yol, headers=sef_headers)).json()
    assert Decimal(acik["totals"]["our_total"]) == Decimal("3200")

    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)
    gizli = (await client.get(yol, headers=sef_headers)).json()
    toplam = gizli["totals"]
    assert toplam["our_total"] is None
    assert toplam["payable_total"] is None
    assert toplam["vat_rate"] == acik["totals"]["vat_rate"]  # oran para değil


@pytest.mark.asyncio
async def test_kira_odemesi_yalniz_maliyet_kardir_banka_kasa_etkilemez(
    client, sef_headers, seeded_db, kira_faturasi
):
    """ONARIM — `payable_total` yalnız `maliyet_kar`: `banka_kasa` gizli rol onu GÖRÜR (iki toplanan
    zaten görünür; ayrı gizleme anlamsızdı), `maliyet_kar` gizli rol görmez."""
    fatura, _ = kira_faturasi
    yol = f"/equipment/rental-invoices/{fatura.id}"
    await rol_gizle(seeded_db, "site_chief", HiddenCategory.banka_kasa)
    banka = (await client.get(yol, headers=sef_headers)).json()
    assert banka["totals"]["payable_total"] is not None
    assert banka["payable_total"] is not None

    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)
    maliyet = (await client.get(yol, headers=sef_headers)).json()
    assert maliyet["totals"]["payable_total"] is None
    assert maliyet["payable_total"] is None

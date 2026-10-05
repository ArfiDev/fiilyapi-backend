"""IZN-B4d — ekipman hassas alan maskesi: maliyet gizli rolde tutar `null`, saat/sayaç görünür,
PUT/PATCH'te dolu tutar 403, bayraksız rol hepsini görür (POZİTİF KONTROL)."""

import pytest

from app.core.sayfalar import HiddenCategory
from app.modules.equipment.models import EquipmentCategory, EquipmentOwnership
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

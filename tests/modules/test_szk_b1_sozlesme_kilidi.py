"""SZK-B1 — sözleşmeye bağlı şantiye kaleminde ayna alanları kilidi (Z1-Z3),
`contract_item_id` yanıtta ve iki uçta miktar hane/tavan sınırı.

Kilit kümesi `contracts.service.MIRRORED_ITEM_FIELDS`tan okunur; testler de aynı
kümeyi PARAMETRE olarak kullanır (ikinci liste yazılmaz).
"""

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.modules.boq.schemas import SECTION_DISTRIBUTION_MAX_CELLS
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.contracts.schemas import ContractAllocationInput, ContractDistributionSave
from app.modules.contracts.service import MIRRORED_ITEM_FIELDS
from app.modules.projects.models import ProjectContract
from app.modules.sites.models import Section

from ._boq import _auth, _group, _item, _login, _site

_DEGISIK = {
    "code": "99.999",
    "description": "Başka tarif",
    "unit": "adet",
    "unit_price": "1.00",
}


async def _bagli_kalem(session, project, site, group, **kwargs):
    """Sözleşme kalemine bağlı BOQ satırı (kod/tarif/birim/fiyat aynada aynı)."""
    session.add(
        ProjectContract(
            project_id=project.id,
            contract_no="SZK-B1",
            amount=Decimal("50000000"),
            advance_pct=Decimal("20"),
        )
    )
    await session.flush()
    c_group = EmployerContractGroup(project_id=project.id, name="Sözleşme grubu", sort_order=0)
    session.add(c_group)
    await session.flush()
    c_item = EmployerContractItem(
        project_id=project.id,
        group_id=c_group.id,
        code="01.001",
        description="Kazı (Makine ile)",
        unit="m³",
        quantity=Decimal("5000.000"),
        unit_price=Decimal("280.00"),
        sort_order=0,
    )
    session.add(c_item)
    await session.flush()
    item = await _item(session, site, group, contract_item_id=c_item.id, **kwargs)
    return item, c_item


@pytest.fixture
async def kurulum(client, db_session, user_factory, project_factory):
    project = await project_factory("SZK-B1")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    token = await _login(client, user_factory, "system_admin", "admin@szk-b1.co")
    bagli, c_item = await _bagli_kalem(db_session, project, site, group)
    serbest = await _item(db_session, site, group, code="02.001", description="Serbest")
    return {
        "project": project,
        "site": site,
        "group": group,
        "headers": _auth(token),
        "bagli": bagli,
        "serbest": serbest,
        "c_item": c_item,
    }


async def _patch(client, kurulum, item, govde):
    return await client.patch(f"/boq/items/{item.id}", json=govde, headers=kurulum["headers"])


# --- contract_item_id yanıtta ---


async def test_liste_yanitinda_contract_item_id_bagli_ve_bagsiz(client, kurulum):
    yanit = await client.get(f"/sites/{kurulum['site'].id}/boq", headers=kurulum["headers"])

    assert yanit.status_code == 200, yanit.text
    satirlar = {s["id"]: s for g in yanit.json()["groups"] for s in g["items"]}
    assert satirlar[str(kurulum["bagli"].id)]["contract_item_id"] == str(kurulum["c_item"].id)
    assert satirlar[str(kurulum["serbest"].id)]["contract_item_id"] is None


async def test_patch_ve_tahsis_yanitinda_contract_item_id(client, db_session, kurulum):
    bagli = kurulum["bagli"]
    patch = await _patch(client, kurulum, bagli, {"sort_order": 3})
    assert patch.status_code == 200, patch.text
    assert patch.json()["contract_item_id"] == str(kurulum["c_item"].id)

    kat = Section(site_id=kurulum["site"].id, name="Kat 1")
    db_session.add(kat)
    await db_session.flush()
    put = await client.put(
        f"/boq/items/{bagli.id}/allocations",
        json={"allocations": [{"section_id": str(kat.id), "quantity": "10.000"}]},
        headers=kurulum["headers"],
    )
    assert put.status_code == 200, put.text
    assert put.json()["item"]["contract_item_id"] == str(kurulum["c_item"].id)


# --- kilit ---


@pytest.mark.parametrize("alan", MIRRORED_ITEM_FIELDS)
async def test_bagli_kalemde_kilitli_alan_farkli_degerle_422_alan_adli(client, kurulum, alan):
    yanit = await _patch(client, kurulum, kurulum["bagli"], {alan: _DEGISIK[alan]})

    assert yanit.status_code == 422, yanit.text
    assert alan in yanit.json()["detail"]


async def test_bagli_kalemde_ayni_degerlerle_tam_govde_200(client, kurulum):
    govde = {
        "code": "01.001",
        "description": "Kazı (Makine ile)",
        "unit": "m³",
        "unit_price": "280",  # sayısal eşitlik: 280 == 280.00
    }

    yanit = await _patch(client, kurulum, kurulum["bagli"], govde)

    assert yanit.status_code == 200, yanit.text
    assert Decimal(yanit.json()["unit_price"]) == Decimal("280.00")


async def test_bagli_kalemde_ayni_deger_yaninda_farkli_deger_yine_422(client, kurulum):
    govde = {"code": "01.001", "unit_price": "281.00"}

    yanit = await _patch(client, kurulum, kurulum["bagli"], govde)

    assert yanit.status_code == 422
    assert "unit_price" in yanit.json()["detail"]
    assert "code" not in yanit.json()["detail"]


async def test_bagli_kalemde_miktar_grup_sira_acik(client, db_session, kurulum):
    diger_grup = await _group(db_session, kurulum["site"], name="DİĞER", sort_order=2)

    yanit = await _patch(
        client,
        kurulum,
        kurulum["bagli"],
        {"quantity": "900.000", "group_id": str(diger_grup.id), "sort_order": 7},
    )

    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert Decimal(govde["quantity"]) == Decimal("900.000")
    assert govde["sort_order"] == 7


async def test_bagsiz_kalemde_dort_alan_serbest(client, kurulum):
    yanit = await _patch(client, kurulum, kurulum["serbest"], _DEGISIK)

    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["code"] == "99.999"
    assert govde["description"] == "Başka tarif"
    assert govde["unit"] == "adet"
    assert Decimal(govde["unit_price"]) == Decimal("1.00")


# --- hane / tavan ---


@pytest.mark.parametrize("miktar", ["1e30", "0.0004", "123456789012345", "1.0001"])
async def test_tek_kalem_tahsis_hane_ihlali_422(client, kurulum, miktar):
    yanit = await client.put(
        f"/boq/items/{kurulum['serbest'].id}/allocations",
        json={"allocations": [{"section_id": str(uuid.uuid4()), "quantity": miktar}]},
        headers=kurulum["headers"],
    )

    assert yanit.status_code == 422, yanit.text


@pytest.mark.parametrize("miktar", ["1e30", "0.0004", "123456789012345"])
async def test_sozlesme_dagitimi_hane_ihlali_422(client, kurulum, miktar):
    yanit = await client.put(
        f"/projects/{kurulum['project'].id}/contract/distribution",
        json={
            "allocations": [
                {
                    "contract_item_id": str(kurulum["c_item"].id),
                    "site_id": str(kurulum["site"].id),
                    "quantity": miktar,
                }
            ]
        },
        headers=kurulum["headers"],
    )

    assert yanit.status_code == 422, yanit.text


async def test_sozlesme_dagitimi_hucre_tavani_asilirsa_422(client, kurulum):
    hucre = {
        "contract_item_id": str(uuid.uuid4()),
        "site_id": str(uuid.uuid4()),
        "quantity": "1.000",
    }

    yanit = await client.put(
        f"/projects/{kurulum['project'].id}/contract/distribution",
        json={"allocations": [hucre] * (SECTION_DISTRIBUTION_MAX_CELLS + 1)},
        headers=kurulum["headers"],
    )

    assert yanit.status_code == 422, yanit.text


def test_on_dort_hane_gecerli_ve_none_kaldirma_anlami_korunur():
    ortak = {"contract_item_id": uuid.uuid4(), "site_id": uuid.uuid4()}

    tam = ContractAllocationInput(**ortak, quantity=Decimal("99999999999.999"))
    kaldir = ContractAllocationInput(**ortak)

    assert tam.quantity == Decimal("99999999999.999")
    assert kaldir.quantity is None
    with pytest.raises(ValidationError):
        ContractAllocationInput(**ortak, quantity=Decimal("0"))
    assert ContractDistributionSave().allocations == []

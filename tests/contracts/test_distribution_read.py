"""Task C7 — Poz dağılımı okuma ucu (spec §6.3 GET kısmı, `POZ` mockup).

Kapsam: yalnız `GET /projects/{id}/contract/distribution`. Yazma (`PUT`) C8'in
işi — burada AÇILMAZ, test edilmez.

Senaryo (`dagitimli_proje` fixture'ı): iki şantiye (A, B), bir grup, iki kalem:
* "04.001" Demir donatı — 200 Ton, A'ya 120, B'ye 80 dağıtılmış → Kalan 0
  (`POZ` 56, 84, 100 deseni).
* "09.014" İnce Sıva (Alçı) — hiçbir şantiyeye dağıtılmamış (`POZ` 65 uyarısı).
"""

import uuid
from decimal import Decimal

import pytest

from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.projects.models import ProjectContract
from app.modules.sites.models import Site


async def _contract(session, project_id, **kwargs) -> ProjectContract:
    defaults = {
        "contract_no": "SZL-2026-020",
        "amount": Decimal("50000000"),
        "advance_pct": Decimal("20"),
    }
    defaults.update(kwargs)
    contract = ProjectContract(project_id=project_id, **defaults)
    session.add(contract)
    await session.flush()
    return contract


async def _site(session, project_id, code, name) -> Site:
    site = Site(project_id=project_id, code=code, name=name)
    session.add(site)
    await session.flush()
    return site


async def _boq_group(session, site_id, name) -> BoqGroup:
    group = BoqGroup(site_id=site_id, name=name)
    session.add(group)
    await session.flush()
    return group


async def _boq_item(session, site_id, group_id, contract_item_id, **kwargs) -> BoqItem:
    defaults = {
        "code": "04.001",
        "description": "Demir donatı",
        "unit": "Ton",
        "quantity": Decimal("100"),
        "unit_price": Decimal("21500"),
    }
    defaults.update(kwargs)
    item = BoqItem(
        site_id=site_id, group_id=group_id, contract_item_id=contract_item_id, **defaults
    )
    session.add(item)
    await session.flush()
    return item


@pytest.fixture
async def _dagitim_kurulumu(seeded_db, project_factory):
    project = await project_factory(code="CL-DIST-01", name="Dağıtımlı Proje")
    await _contract(seeded_db, project.id, contract_no="SZL-2026-020")

    site_a = await _site(seeded_db, project.id, "SNT-A", "Şantiye A")
    site_b = await _site(seeded_db, project.id, "SNT-B", "Şantiye B")

    group = EmployerContractGroup(project_id=project.id, name="A — Demir İşleri", sort_order=0)
    seeded_db.add(group)
    await seeded_db.flush()

    distributed_item = EmployerContractItem(
        project_id=project.id,
        group_id=group.id,
        code="04.001",
        description="Demir donatı",
        unit="Ton",
        quantity=Decimal("200"),
        unit_price=Decimal("21500"),
        sort_order=0,
    )
    undistributed_item = EmployerContractItem(
        project_id=project.id,
        group_id=group.id,
        code="09.014",
        description="İnce Sıva (Alçı)",
        unit="m²",
        quantity=Decimal("500"),
        unit_price=Decimal("180"),
        sort_order=1,
    )
    seeded_db.add_all([distributed_item, undistributed_item])
    await seeded_db.flush()

    boq_group_a = await _boq_group(seeded_db, site_a.id, "A — Demir İşleri")
    boq_group_b = await _boq_group(seeded_db, site_b.id, "A — Demir İşleri")
    await _boq_item(
        seeded_db,
        site_a.id,
        boq_group_a.id,
        distributed_item.id,
        code="04.001",
        description="Demir donatı",
        unit="Ton",
        quantity=Decimal("120"),
        unit_price=Decimal("21500"),
    )
    await _boq_item(
        seeded_db,
        site_b.id,
        boq_group_b.id,
        distributed_item.id,
        code="04.001",
        description="Demir donatı",
        unit="Ton",
        quantity=Decimal("80"),
        unit_price=Decimal("21500"),
    )

    return {
        "project_id": project.id,
        "site_a_id": site_a.id,
        "site_b_id": site_b.id,
    }


@pytest.fixture
async def dagitimli_proje(_dagitim_kurulumu) -> uuid.UUID:
    return _dagitim_kurulumu["project_id"]


@pytest.fixture
async def santiye(_dagitim_kurulumu) -> uuid.UUID:
    return _dagitim_kurulumu["site_a_id"]


@pytest.mark.asyncio
async def test_kalan_ve_dagitilmamis(client, admin_headers, dagitimli_proje):
    yanit = await client.get(
        f"/projects/{dagitimli_proje}/contract/distribution", headers=admin_headers
    )
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    kalem = govde["groups"][0]["items"][0]  # 200 Ton, A:120 B:80
    assert Decimal(kalem["remaining_quantity"]) == Decimal("0")
    assert govde["undistributed_item_count"] == 1
    assert "İnce Sıva (Alçı)" in govde["undistributed_item_names"]
    assert govde["distributed_item_count"] == 1
    assert govde["total_item_count"] == 2


@pytest.mark.asyncio
async def test_santiye_ozeti_bedeli(client, admin_headers, dagitimli_proje, santiye):
    govde = (
        await client.get(
            f"/projects/{dagitimli_proje}/contract/distribution", headers=admin_headers
        )
    ).json()
    ozet = next(o for o in govde["site_summaries"] if o["site_id"] == str(santiye))
    # 120 Ton × ₺21.500
    assert Decimal(ozet["total_amount"]) == Decimal("2580000")


@pytest.mark.asyncio
async def test_sites_dagitim_kolonlari(client, admin_headers, dagitimli_proje):
    """`POZ` 82-83: `sites[]` projenin şantiyeleri kadardır."""
    govde = (
        await client.get(
            f"/projects/{dagitimli_proje}/contract/distribution", headers=admin_headers
        )
    ).json()
    assert {s["name"] for s in govde["sites"]} == {"Şantiye A", "Şantiye B"}


@pytest.mark.asyncio
async def test_sozlesmesiz_proje_404(client, admin_headers, project_factory):
    proje = await project_factory(code="CL-DIST-99", name="Sözleşmesiz Proje")

    yanit = await client.get(f"/projects/{proje.id}/contract/distribution", headers=admin_headers)

    assert yanit.status_code == 404


@pytest.mark.asyncio
async def test_gorunmeyen_proje_ile_olmayan_proje_ayni_yanit(
    client, kisitli_headers, gorunmeyen_proje
):
    gercek = await client.get(
        f"/projects/{gorunmeyen_proje.id}/contract/distribution", headers=kisitli_headers
    )
    sahte = await client.get(
        f"/projects/{uuid.uuid4()}/contract/distribution", headers=kisitli_headers
    )
    assert gercek.status_code == sahte.status_code == 404
    assert gercek.json() == sahte.json()


@pytest.mark.asyncio
async def test_yetkisiz_rol_403(client, site_chief_headers, dagitimli_proje):
    yanit = await client.get(
        f"/projects/{dagitimli_proje}/contract/distribution", headers=site_chief_headers
    )
    assert yanit.status_code == 403


@pytest.mark.asyncio
async def test_contract_item_id_null_boq_satiri_gorunmez(
    client, admin_headers, dagitimli_proje, seeded_db, _dagitim_kurulumu
):
    """Spec §3.3: `contract_item_id IS NULL` olan BOQ satırı — şantiyenin kendi

    başına girdiği poz — dağıtım ekranında hiçbir yerde görünmez.
    """
    site_a_id = _dagitim_kurulumu["site_a_id"]
    grup = await _boq_group(seeded_db, site_a_id, "Şantiye Kendi Pozları")
    await _boq_item(
        seeded_db,
        site_a_id,
        grup.id,
        None,
        code="99.001",
        description="Sahaya özgü poz",
        unit="Adet",
        quantity=Decimal("5"),
        unit_price=Decimal("100"),
    )

    yanit = await client.get(
        f"/projects/{dagitimli_proje}/contract/distribution", headers=admin_headers
    )
    govde = yanit.json()
    tum_kodlar = {
        alloc["boq_item_id"]
        for grup in govde["groups"]
        for kalem in grup["items"]
        for alloc in kalem["allocations"]
    }
    assert len(tum_kodlar) == 2  # yalnız iki dağıtılmış BOQ satırı (A:120, B:80)


@pytest.mark.asyncio
async def test_dagitim_kalemi_source_code_doner_kodsuz_null(
    client, admin_headers, dagitimli_proje, seeded_db
):
    """KAT-B2.4 (K10): isveren hakedis FORMU dagitim kaleminden kurulur → kalemin Bakanlik poz
    no'su (`employer_contract_items.source_code`) okunur; kodsuz kalem `null`."""
    from sqlalchemy import update

    await seeded_db.execute(
        update(EmployerContractItem)
        .where(EmployerContractItem.project_id == dagitimli_proje)
        .where(EmployerContractItem.code == "04.001")
        .values(source_code="15.100.1001")
    )
    await seeded_db.flush()
    govde = (
        await client.get(
            f"/projects/{dagitimli_proje}/contract/distribution", headers=admin_headers
        )
    ).json()
    kalemler = {k["code"]: k for g in govde["groups"] for k in g["items"]}
    assert kalemler["04.001"]["source_code"] == "15.100.1001"
    assert kalemler["09.014"]["source_code"] is None

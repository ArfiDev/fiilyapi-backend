"""PLN-B1.3 — sirket disiplin listesi (K2) + silme kurali (B1-9)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqGroup
from app.modules.catalog.models import EvDiscipline
from app.modules.earned_value import guards
from app.modules.earned_value.engine import ContractorType
from app.modules.earned_value.models import (
    EvBaselineLeaf,
    EvDistribution,
    EvGroupDiscipline,
    EvRevision,
    EvWindow,
    RevisionStatus,
)
from app.modules.sites.models import Site

from .conftest import audit_details

URL = "/earned-value/disciplines"


def new(code: str = "MEK", **overrides) -> dict:
    return {
        "code": code,
        "name": "Mekanik Tesisat",
        "color": "#0EA5E9",
        "default_contractor_type": "subcon",
        "sort_order": 3,
        **overrides,
    }


def _loc_fields(resp) -> set[str]:
    return {str(err["loc"][-1]) for err in resp.json()["detail"]}


async def test_liste_sort_order_sonra_kod_sirali(
    client: AsyncClient, admin, disiplin_fabrikasi
) -> None:
    await disiplin_fabrikasi("ELK", sort_order=2)
    await disiplin_fabrikasi("DUV", sort_order=1)
    await disiplin_fabrikasi("KAB", sort_order=1, color="#123ABC")
    resp = await client.get(URL, headers=admin)
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["code"] for r in rows] == ["DUV", "KAB", "ELK"]
    assert set(rows[1]) == {
        "id",
        "code",
        "name",
        "color",
        "default_contractor_type",
        "sort_order",
        "used_by_item_count",
        "used_by_site_count",
        "user_count",
    }
    assert rows[1]["color"] == "#123ABC"


async def test_ekle_201_ve_audit(client: AsyncClient, admin, seeded_db: AsyncSession) -> None:
    resp = await client.post(URL, json=new(code="  MEK "), headers=admin)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["code"] == "MEK"  # bosluk kirpilir
    assert data["default_contractor_type"] == "subcon"
    assert await seeded_db.get(EvDiscipline, uuid.UUID(data["id"])) is not None
    assert "Disiplin eklendi: MEK · Mekanik Tesisat" in await audit_details(seeded_db)


async def test_kod_alinmissa_409_alana_ozel(client: AsyncClient, admin, disiplin_fabrikasi) -> None:
    await disiplin_fabrikasi("MEK")
    resp = await client.post(URL, json=new(), headers=admin)
    assert resp.status_code == 409
    assert resp.json()["detail"] == guards.DISCIPLINE_CODE_TAKEN


@pytest.mark.parametrize("color", ["#12345", "123456", "#GGGGGG", "#1234567", "blue"])
async def test_renk_hex_degilse_422(client: AsyncClient, admin, color: str) -> None:
    resp = await client.post(URL, json=new(color=color), headers=admin)
    assert resp.status_code == 422
    assert "color" in _loc_fields(resp)


async def test_patch_kismi_gecmeyen_alan_dokunulmaz(
    client: AsyncClient, admin, disiplin_fabrikasi, seeded_db: AsyncSession
) -> None:
    row = await disiplin_fabrikasi("KAB", "Kaba", color="#111111")
    resp = await client.patch(f"{URL}/{row.id}", json={"name": "Kaba İnşaat"}, headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Kaba İnşaat"
    assert resp.json()["code"] == "KAB" and resp.json()["color"] == "#111111"
    assert "Disiplin güncellendi: KAB · Kaba İnşaat" in await audit_details(seeded_db)


@pytest.mark.parametrize(
    "field", ["code", "name", "color", "default_contractor_type", "sort_order"]
)
async def test_patch_acik_null_422_alan_adli(
    client: AsyncClient, admin, kab: EvDiscipline, field: str
) -> None:
    resp = await client.patch(f"{URL}/{kab.id}", json={field: None}, headers=admin)
    assert resp.status_code == 422
    assert field in _loc_fields(resp)


async def test_patch_kod_baskasininkiyse_409_kendisininkiyse_200(
    client: AsyncClient, admin, disiplin_fabrikasi
) -> None:
    kab = await disiplin_fabrikasi("KAB")
    await disiplin_fabrikasi("DUV")
    same = await client.patch(f"{URL}/{kab.id}", json={"code": "KAB"}, headers=admin)
    assert same.status_code == 200
    taken = await client.patch(f"{URL}/{kab.id}", json={"code": "DUV"}, headers=admin)
    assert taken.status_code == 409
    assert taken.json()["detail"] == guards.DISCIPLINE_CODE_TAKEN


async def test_olmayan_disiplin_404(client: AsyncClient, admin) -> None:
    for resp in (
        await client.patch(f"{URL}/{uuid.uuid4()}", json={"name": "X"}, headers=admin),
        await client.delete(f"{URL}/{uuid.uuid4()}", headers=admin),
    ):
        assert resp.status_code == 404
        assert resp.json()["detail"] == guards.DISCIPLINE_MISSING


async def test_kullanilmayan_disiplin_silinir_204_audit(
    client: AsyncClient, admin, disiplin_fabrikasi, seeded_db: AsyncSession
) -> None:
    row = await disiplin_fabrikasi("TMP", "Geçici")
    row_id = row.id
    resp = await client.delete(f"{URL}/{row_id}", headers=admin)
    assert resp.status_code == 204
    assert resp.content == b""
    assert await seeded_db.get(EvDiscipline, row_id) is None
    assert "Disiplin silindi: TMP · Geçici" in await audit_details(seeded_db)


# --- kullanimda (B1-9): bes tablonun her biri tek basina silmeyi engeller ---


async def _revision(session: AsyncSession, site: Site) -> EvRevision:
    rev = EvRevision(site_id=site.id, number=0, status=RevisionStatus.DRAFT)
    session.add(rev)
    await session.flush()
    return rev


async def _use_group(session, site, rev, disc) -> None:
    group = BoqGroup(site_id=site.id, name="KABA")
    session.add(group)
    await session.flush()
    session.add(EvGroupDiscipline(revision_id=rev.id, boq_group_id=group.id, discipline_id=disc.id))


async def _use_catalog(session, site, rev, disc) -> None:
    from app.modules.catalog.models import EvCatalogItem

    session.add(
        EvCatalogItem(
            discipline_id=disc.id,
            name="Beton",
            uom="m³",
            standard_unit_mhr=Decimal("1.8"),
            default_contractor_type=ContractorType.OWN,
        )
    )


async def _use_distribution(session, site, rev, disc) -> None:
    session.add(EvDistribution(revision_id=rev.id, discipline_id=disc.id, distribution="bell"))


async def _use_window(session, site, rev, disc) -> None:
    session.add(
        EvWindow(
            revision_id=rev.id,
            discipline_id=disc.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 2, 1),
        )
    )


async def _use_baseline(session, site, rev, disc) -> None:
    session.add(
        EvBaselineLeaf(
            revision_id=rev.id,
            boq_item_id=uuid.uuid4(),
            boq_group_id=uuid.uuid4(),
            discipline_id=disc.id,
            item_code="KAB.01",
            item_description="Beton",
            uom="m³",
            planned_qty=Decimal("10"),
            unit_mhr=Decimal("1.8"),
            contractor_type=ContractorType.OWN,
            is_direct=True,
            distribution="linear",
            budget_mhr=Decimal("18"),
        )
    )


@pytest.mark.parametrize(
    "use",
    [_use_group, _use_catalog, _use_baseline],
    ids=["grup-eslemesi", "katalog", "baseline"],
)
async def test_kullanimdaki_disiplin_409(
    client: AsyncClient,
    admin,
    kab: EvDiscipline,
    santiye: Site,
    seeded_db: AsyncSession,
    use,
) -> None:
    rev = await _revision(seeded_db, santiye)
    await use(seeded_db, santiye, rev, kab)
    await seeded_db.flush()
    resp = await client.delete(f"{URL}/{kab.id}", headers=admin)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.DISCIPLINE_IN_USE
    assert await seeded_db.get(EvDiscipline, kab.id) is not None


async def test_baska_disiplinin_kullanimi_engellemez(
    client: AsyncClient, admin, disiplin_fabrikasi, santiye: Site, seeded_db: AsyncSession
) -> None:
    used = await disiplin_fabrikasi("KAB")
    free = await disiplin_fabrikasi("DUV")
    rev = await _revision(seeded_db, santiye)
    await _use_distribution(seeded_db, santiye, rev, used)
    await seeded_db.flush()
    assert (await client.delete(f"{URL}/{free.id}", headers=admin)).status_code == 204


# --- sayaçlar ve tutarlı silme kuralı (CEO, PLN-B2 eki) ------------------------------


@pytest.mark.parametrize(
    ("use", "items", "sites"),
    [(_use_group, 0, 1), (_use_catalog, 1, 0), (_use_baseline, 0, 1)],
    ids=["grup-eslemesi", "katalog", "baseline"],
)
async def test_sayaclar_listede_ve_silme_kurali_tutarli(
    client: AsyncClient,
    admin,
    kab: EvDiscipline,
    santiye: Site,
    seeded_db: AsyncSession,
    use,
    items,
    sites,
) -> None:
    rev = await _revision(seeded_db, santiye)
    await use(seeded_db, santiye, rev, kab)
    await seeded_db.flush()
    row = next(r for r in (await client.get(URL, headers=admin)).json() if r["id"] == str(kab.id))
    assert (row["used_by_item_count"], row["used_by_site_count"]) == (items, sites)
    assert (await client.delete(f"{URL}/{kab.id}", headers=admin)).status_code == 409


@pytest.mark.parametrize("use", [_use_distribution, _use_window], ids=["dagilim", "pencere"])
async def test_eslemesiz_dagilim_pencere_artigi_silmeyi_engellemez_ve_temizlenir(
    client: AsyncClient, admin, kab: EvDiscipline, santiye: Site, seeded_db: AsyncSession, use
) -> None:
    """Sayaçlar 0 → silinebilir; grubu olmayan disiplinin dağılım/pencere ezmesi anlamsızdır,
    disiplinle birlikte gider (FK'ye çarpıp 500 VERMEZ)."""
    rev = await _revision(seeded_db, santiye)
    await use(seeded_db, santiye, rev, kab)
    await seeded_db.flush()
    row = next(r for r in (await client.get(URL, headers=admin)).json() if r["id"] == str(kab.id))
    assert (row["used_by_item_count"], row["used_by_site_count"]) == (0, 0)
    assert (await client.delete(f"{URL}/{kab.id}", headers=admin)).status_code == 204
    assert await seeded_db.get(EvDiscipline, kab.id) is None


async def test_santiye_sayaci_ayni_santiyeyi_bir_kez_sayar(
    client: AsyncClient, admin, kab: EvDiscipline, santiye: Site, seeded_db: AsyncSession
) -> None:
    rev = await _revision(seeded_db, santiye)
    await _use_group(seeded_db, santiye, rev, kab)
    await _use_group(seeded_db, santiye, rev, kab)  # ikinci grup, aynı şantiye
    await _use_baseline(seeded_db, santiye, rev, kab)
    await seeded_db.flush()
    row = next(r for r in (await client.get(URL, headers=admin)).json() if r["id"] == str(kab.id))
    assert row["used_by_site_count"] == 1

"""T3 — BOQ repository + servis (okuma yolu, spec §5.1-5.2)."""

from decimal import Decimal

import pytest

from app.core.errors import NotFoundError
from app.modules.boq import service
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.sites.models import Site


async def _grant_all(session, user) -> None:
    user.all_projects = True
    await session.flush()


async def _patron(session, user_factory, email: str):
    user = await user_factory(email=email, password="parola1234", role_key="patron")
    await _grant_all(session, user)
    return user


async def _site(session, project, code: str = "A-BLOK", **kwargs) -> Site:
    site = Site(project_id=project.id, code=code, name=kwargs.pop("name", "A-Blok Şantiyesi"))
    for field, value in kwargs.items():
        setattr(site, field, value)
    session.add(site)
    await session.flush()
    return site


async def _group(session, site, name: str = "TOPRAK VE TEMEL İŞLERİ", **kwargs) -> BoqGroup:
    group = BoqGroup(site_id=site.id, name=name, **kwargs)
    session.add(group)
    await session.flush()
    return group


async def _item(session, site, group, code: str = "01.001", **kwargs) -> BoqItem:
    defaults = {
        "description": "Kazı (Makine ile)",
        "unit": "m³",
        "quantity": Decimal("1240.000"),
        "unit_price": Decimal("280.00"),
    }
    defaults.update(kwargs)
    item = BoqItem(site_id=site.id, group_id=group.id, code=code, **defaults)
    session.add(item)
    await session.flush()
    return item


async def test_empty_boq_returns_zero_grand_total(seeded_db, user_factory, project_factory):
    project = await project_factory("BOQ-1")
    site = await _site(seeded_db, project)
    user = await _patron(seeded_db, user_factory, "boq1@t.co")

    result = await service.get_boq_for_site(seeded_db, user, site.id)

    assert result.groups == []
    assert result.totals.grand_total == Decimal("0.00")
    assert result.totals.contract_total.available is False
    assert result.totals.contract_total.pending_module == "contracts"


async def test_group_and_grand_totals_are_computed_from_items(
    seeded_db, user_factory, project_factory
):
    project = await project_factory("BOQ-2")
    site = await _site(seeded_db, project)
    group_a = await _group(seeded_db, site, name="TOPRAK VE TEMEL İŞLERİ", sort_order=1)
    group_b = await _group(seeded_db, site, name="BETONARME İŞLERİ", sort_order=2)
    await _item(
        seeded_db,
        site,
        group_a,
        code="01.001",
        quantity=Decimal("1240.000"),
        unit_price=Decimal("280.00"),
    )
    await _item(
        seeded_db,
        site,
        group_a,
        code="01.002",
        quantity=Decimal("10.000"),
        unit_price=Decimal("50.00"),
    )
    await _item(
        seeded_db,
        site,
        group_b,
        code="02.001",
        quantity=Decimal("100.000"),
        unit_price=Decimal("1000.00"),
    )
    user = await _patron(seeded_db, user_factory, "boq2@t.co")

    result = await service.get_boq_for_site(seeded_db, user, site.id)

    assert [g.name for g in result.groups] == ["TOPRAK VE TEMEL İŞLERİ", "BETONARME İŞLERİ"]
    assert result.groups[0].group_total == Decimal("347700.00")
    assert result.groups[1].group_total == Decimal("100000.00")
    assert result.totals.grand_total == Decimal("447700.00")


async def test_items_sorted_by_sort_order_then_code(seeded_db, user_factory, project_factory):
    project = await project_factory("BOQ-3")
    site = await _site(seeded_db, project)
    group = await _group(seeded_db, site)
    await _item(seeded_db, site, group, code="01.002", sort_order=1)
    await _item(seeded_db, site, group, code="01.001", sort_order=1)
    await _item(seeded_db, site, group, code="00.001", sort_order=0)
    user = await _patron(seeded_db, user_factory, "boq3@t.co")

    result = await service.get_boq_for_site(seeded_db, user, site.id)

    codes = [item.code for item in result.groups[0].items]
    assert codes == ["00.001", "01.001", "01.002"]


async def test_groups_sorted_by_sort_order(seeded_db, user_factory, project_factory):
    project = await project_factory("BOQ-4")
    site = await _site(seeded_db, project)
    await _group(seeded_db, site, name="IKINCI", sort_order=2)
    await _group(seeded_db, site, name="BIRINCI", sort_order=1)
    user = await _patron(seeded_db, user_factory, "boq4@t.co")

    result = await service.get_boq_for_site(seeded_db, user, site.id)

    assert [g.name for g in result.groups] == ["BIRINCI", "IKINCI"]


async def test_invisible_site_raises_not_found(seeded_db, user_factory, project_factory):
    """P2 §5.2 deseni: gorunmeyen santiye icin 404 — servis katmani NotFoundError firlatir."""
    project = await project_factory("BOQ-5")
    site = await _site(seeded_db, project)
    # user_project_access GRANT edilmedi -> proje/santiye kullaniciya gorunmez.
    other = await user_factory(email="boq5@t.co", password="parola1234", role_key="site_chief")

    with pytest.raises(NotFoundError):
        await service.get_boq_for_site(seeded_db, other, site.id)

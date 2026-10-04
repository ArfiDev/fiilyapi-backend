"""B3 — blok/unite okuma yolu: repository + servis (spec §6.1, §7.4, §4.4, §5.2).

Gorunurluk suzgeci BURADA YENIDEN YAZILMAZ: `projects.service.visible_projects`
yeniden kullanilir (P2 `sites.service` deseni). Toplamlar filtreden ETKILENMEZ
(spec §7.4) ve Decimal ile hesaplanir — float asla.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.core.errors import NotFoundError
from app.modules.projects.models import Project
from app.modules.sites.models import Site
from app.modules.units import service
from app.modules.units.models import Block, Unit, UnitKind, UnitOwnerSide, UnitSalesStatus
from app.modules.units.schemas import UnitOwnerSideFilter, UnitValueBasis


async def _patron(session, user_factory, email: str):
    user = await user_factory(email=email, password="parola1234", role_key="patron")
    user.all_projects = True
    await session.flush()
    return user


async def _site(session, project: Project, code: str = "SANTIYE-1", name: str = "Merkez") -> Site:
    site = Site(project_id=project.id, code=code, name=name)
    session.add(site)
    await session.flush()
    return site


async def _block(session, project: Project, site: Site, name: str = "A Blok", **kwargs) -> Block:
    block = Block(project_id=project.id, site_id=site.id, name=name, **kwargs)
    session.add(block)
    await session.flush()
    return block


async def _unit(session, project: Project, block: Block, unit_no: str = "1", **kwargs) -> Unit:
    defaults: dict = {"unit_kind": UnitKind.apartment}
    defaults.update(kwargs)
    unit = Unit(project_id=project.id, block_id=block.id, unit_no=unit_no, **defaults)
    session.add(unit)
    await session.flush()
    return unit


@contextmanager
def _captured_statements() -> Iterator[list[str]]:
    """Calisan ham SQL ifadelerini toplar — N+1 korumasi icin (test 17)."""
    statements: list[str] = []

    def _handler(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", _handler)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", _handler)


# --- value_basis (spec §4.4) ---


async def test_value_basis_kat_karsiligi_uses_appraisal(seeded_db, user_factory, project_factory):
    project = await project_factory("U3-1", project_type="kat_karsiligi")
    site = await _site(seeded_db, project)
    block = await _block(seeded_db, project, site)
    await _unit(
        seeded_db,
        project,
        block,
        "1",
        list_price=Decimal("100.00"),
        appraisal_value=Decimal("40.00"),
    )
    user = await _patron(seeded_db, user_factory, "u31@t.co")

    totals = (await service.list_units(seeded_db, user, project.id)).totals

    assert totals.value_basis is UnitValueBasis.appraisal_value
    assert totals.total_value == Decimal("40.00")


async def test_value_basis_kendi_yatirim_uses_list_price(seeded_db, user_factory, project_factory):
    project = await project_factory("U3-2", project_type="kendi_yatirim")
    site = await _site(seeded_db, project)
    block = await _block(seeded_db, project, site)
    await _unit(
        seeded_db,
        project,
        block,
        "1",
        list_price=Decimal("100.00"),
        appraisal_value=Decimal("40.00"),
    )
    user = await _patron(seeded_db, user_factory, "u32@t.co")

    totals = (await service.list_units(seeded_db, user, project.id)).totals

    assert totals.value_basis is UnitValueBasis.list_price
    assert totals.total_value == Decimal("100.00")


async def test_value_basis_taahhut_uses_list_price(seeded_db, user_factory, project_factory):
    project = await project_factory("U3-3", project_type="taahhut")
    site = await _site(seeded_db, project)
    block = await _block(seeded_db, project, site)
    await _unit(seeded_db, project, block, "1", list_price=Decimal("250.00"))
    user = await _patron(seeded_db, user_factory, "u33@t.co")

    totals = (await service.list_units(seeded_db, user, project.id)).totals

    assert totals.value_basis is UnitValueBasis.list_price
    assert totals.total_value == Decimal("250.00")


async def test_total_value_treats_null_basis_as_zero(seeded_db, user_factory, project_factory):
    """Taban sutunu NULL olan satir toplami BOZMAZ (spec §6.1)."""
    project = await project_factory("U3-4", project_type="kendi_yatirim")
    site = await _site(seeded_db, project)
    block = await _block(seeded_db, project, site)
    await _unit(seeded_db, project, block, "1", list_price=Decimal("100.00"))
    await _unit(seeded_db, project, block, "2", list_price=None)
    user = await _patron(seeded_db, user_factory, "u34@t.co")

    totals = (await service.list_units(seeded_db, user, project.id)).totals

    assert totals.total_value == Decimal("100.00")
    assert totals.average_value == Decimal("50.00")


async def test_total_list_price_and_appraisal_returned_separately(
    seeded_db, user_factory, project_factory
):
    project = await project_factory("U3-5", project_type="kat_karsiligi")
    site = await _site(seeded_db, project)
    block = await _block(seeded_db, project, site)
    await _unit(
        seeded_db,
        project,
        block,
        "1",
        list_price=Decimal("100.00"),
        appraisal_value=Decimal("40.00"),
        gross_area_m2=Decimal("120.50"),
    )
    await _unit(
        seeded_db,
        project,
        block,
        "2",
        list_price=Decimal("200.00"),
        appraisal_value=Decimal("60.00"),
        gross_area_m2=Decimal("80.25"),
    )
    user = await _patron(seeded_db, user_factory, "u35@t.co")

    totals = (await service.list_units(seeded_db, user, project.id)).totals

    assert totals.total_list_price == Decimal("300.00")
    assert totals.total_appraisal_value == Decimal("100.00")
    assert totals.total_gross_area_m2 == Decimal("200.75")


async def test_average_value_none_when_no_units(seeded_db, user_factory, project_factory):
    """Sifira bolme YOK."""
    project = await project_factory("U3-6")
    site = await _site(seeded_db, project)
    await _block(seeded_db, project, site)
    user = await _patron(seeded_db, user_factory, "u36@t.co")

    totals = (await service.list_units(seeded_db, user, project.id)).totals

    assert totals.average_value is None


# --- taraf ozetleri (spec §5.2, §5.3) ---


async def test_unit_share_pct_derived(seeded_db, user_factory, project_factory):
    """KKP: 42 unitenin 23'u bizde = %54.76. Sozlesme %55'ten sapma HATA DEGIL."""
    project = await project_factory("U3-7", project_type="kat_karsiligi")
    site = await _site(seeded_db, project)
    block = await _block(seeded_db, project, site)
    for index in range(42):
        side = UnitOwnerSide.contractor if index < 23 else UnitOwnerSide.landowner
        await _unit(seeded_db, project, block, str(index + 1), owner_side=side)
    user = await _patron(seeded_db, user_factory, "u37@t.co")

    totals = (await service.list_units(seeded_db, user, project.id)).totals
    by_side = {summary.side: summary for summary in totals.sides}

    assert by_side[UnitOwnerSide.contractor].share_pct == Decimal("54.76")
    assert by_side[UnitOwnerSide.landowner].share_pct == Decimal("45.24")


async def test_sides_include_unassigned_bucket(seeded_db, user_factory, project_factory):
    """Uc grup DA HER ZAMAN doner — hic unite olmasa bile 0'li (spec §5.3)."""
    project = await project_factory("U3-8", project_type="kat_karsiligi")
    site = await _site(seeded_db, project)
    await _block(seeded_db, project, site)
    user = await _patron(seeded_db, user_factory, "u38@t.co")

    totals = (await service.list_units(seeded_db, user, project.id)).totals

    assert [summary.side for summary in totals.sides] == [
        UnitOwnerSide.contractor,
        UnitOwnerSide.landowner,
        None,
    ]
    assert all(summary.counts.total == 0 for summary in totals.sides)
    assert all(summary.total_value == Decimal("0.00") for summary in totals.sides)
    assert all(summary.average_value is None for summary in totals.sides)


# --- gruplama ve siralama (spec §6.1) ---


async def test_blocks_without_units_are_returned(seeded_db, user_factory, project_factory):
    """Yeni acilan blok ekranda gorunmezse kullanici kaydettigini goremez."""
    project = await project_factory("U3-9")
    site = await _site(seeded_db, project)
    await _block(seeded_db, project, site, name="A Blok")
    user = await _patron(seeded_db, user_factory, "u39@t.co")

    response = await service.list_units(seeded_db, user, project.id)

    assert [group.block.name for group in response.blocks] == ["A Blok"]
    assert response.blocks[0].units == []
    assert response.blocks[0].block.site_name == "Merkez"
    assert response.blocks[0].block.counts.total == 0


async def test_block_ordering_sort_order_then_name(seeded_db, user_factory, project_factory):
    project = await project_factory("U3-10")
    site = await _site(seeded_db, project)
    await _block(seeded_db, project, site, name="Zemin", sort_order=0)
    await _block(seeded_db, project, site, name="B Blok", sort_order=1)
    await _block(seeded_db, project, site, name="A Blok", sort_order=1)
    user = await _patron(seeded_db, user_factory, "u310@t.co")

    response = await service.list_units(seeded_db, user, project.id)

    assert [group.block.name for group in response.blocks] == ["Zemin", "A Blok", "B Blok"]


async def test_unit_ordering_sort_order_then_unit_no(seeded_db, user_factory, project_factory):
    """`unit_no` metin oldugu icin alfabetik sira "10 < 2" verir; `sort_order` onceliklidir."""
    project = await project_factory("U3-11")
    site = await _site(seeded_db, project)
    block = await _block(seeded_db, project, site)
    await _unit(seeded_db, project, block, "10", sort_order=10)
    await _unit(seeded_db, project, block, "2", sort_order=2)
    user = await _patron(seeded_db, user_factory, "u311@t.co")

    response = await service.list_units(seeded_db, user, project.id)

    assert [unit.unit_no for unit in response.blocks[0].units] == ["2", "10"]


# --- suzgecler (spec §7.4) ---


async def test_totals_ignore_filters(seeded_db, user_factory, project_factory):
    """P1 `list_projects_overview` kuralinin birebir tekrari: yalniz `blocks` suzulur."""
    project = await project_factory("U3-12", project_type="kendi_yatirim")
    site = await _site(seeded_db, project)
    block_a = await _block(seeded_db, project, site, name="A Blok")
    block_b = await _block(seeded_db, project, site, name="B Blok")
    await _unit(seeded_db, project, block_a, "1", list_price=Decimal("100.00"))
    await _unit(seeded_db, project, block_b, "1", list_price=Decimal("300.00"))
    user = await _patron(seeded_db, user_factory, "u312@t.co")

    response = await service.list_units(seeded_db, user, project.id, block_id=block_a.id)

    assert [group.block.name for group in response.blocks] == ["A Blok"]
    assert response.totals.counts.total == 2
    assert response.totals.total_value == Decimal("400.00")


async def test_owner_side_unassigned_filter_matches_nulls(seeded_db, user_factory, project_factory):
    project = await project_factory("U3-13", project_type="kat_karsiligi")
    site = await _site(seeded_db, project)
    block = await _block(seeded_db, project, site)
    await _unit(seeded_db, project, block, "1", owner_side=UnitOwnerSide.contractor)
    await _unit(seeded_db, project, block, "2", owner_side=None)
    user = await _patron(seeded_db, user_factory, "u313@t.co")

    response = await service.list_units(
        seeded_db, user, project.id, owner_side=UnitOwnerSideFilter.unassigned
    )

    assert [unit.unit_no for unit in response.blocks[0].units] == ["2"]


async def test_site_filter_resolves_through_block(seeded_db, user_factory, project_factory):
    """`units`'te `site_id` YOK — suzgec blok uzerinden calisir (spec §4.0)."""
    project = await project_factory("U3-14")
    site_a = await _site(seeded_db, project, code="S-A", name="Kuzey")
    site_b = await _site(seeded_db, project, code="S-B", name="Guney")
    block_a = await _block(seeded_db, project, site_a, name="A Blok")
    block_b = await _block(seeded_db, project, site_b, name="B Blok")
    await _unit(seeded_db, project, block_a, "1")
    await _unit(seeded_db, project, block_b, "1")
    user = await _patron(seeded_db, user_factory, "u314@t.co")

    response = await service.list_units(seeded_db, user, project.id, site_id=site_b.id)

    assert [group.block.name for group in response.blocks] == ["B Blok"]
    assert [group.block.site_name for group in response.blocks] == ["Guney"]


async def test_kind_filter_selects_units(seeded_db, user_factory, project_factory):
    project = await project_factory("U3-14B")
    site = await _site(seeded_db, project)
    block = await _block(seeded_db, project, site)
    await _unit(seeded_db, project, block, "1", unit_kind=UnitKind.apartment)
    await _unit(seeded_db, project, block, "D1", unit_kind=UnitKind.shop)
    user = await _patron(seeded_db, user_factory, "u314b@t.co")

    response = await service.list_units(seeded_db, user, project.id, kind=UnitKind.shop)

    assert [unit.unit_no for unit in response.blocks[0].units] == ["D1"]
    assert response.totals.counts.apartment == 1
    assert response.totals.counts.shop == 1


# --- yer tutucular ve bos proje (spec §6.1) ---


async def test_placeholders_carry_correct_pending_module(seeded_db, user_factory, project_factory):
    project = await project_factory("U3-15")
    site = await _site(seeded_db, project)
    block = await _block(seeded_db, project, site)
    await _unit(seeded_db, project, block, "1")
    user = await _patron(seeded_db, user_factory, "u315@t.co")

    response = await service.list_units(seeded_db, user, project.id)
    unit = response.blocks[0].units[0]
    totals = response.totals

    # P3.1 §4.4: `sales_status` ARTIK YER TUTUCU DEGIL — gercek sutun degeri.
    assert unit.sales_status is UnitSalesStatus.listed
    # P8 T5: satis fiyati/alicisi YER TUTUCU DEGIL — satis kaydi yoksa `None`.
    assert unit.sale_price is None
    assert unit.buyer_name is None
    # P9 T3: hissedar da YER TUTUCU DEGIL — atama yoksa iki alan da `None`.
    assert unit.shareholder_id is None
    assert unit.shareholder_name is None
    assert unit.unit_cost.pending_module == "project_costs"
    # UE 97-99: maliyet yoksa kâr da yok (karar 3).
    assert unit.expected_profit.pending_module == "project_costs"
    # P3.1 §8.2: uc sayac GERCEK oldu; ciro P8 T5'te gercege baglandi.
    assert totals.sold_units == 0
    assert totals.available_units == 1
    # P8 T5: ciro da gercek degerdir; satis kaydi olmayan projede 0.00.
    assert totals.sales_revenue == Decimal("0.00")
    assert totals.average_sale_price is None
    assert all(
        placeholder.available is False
        for placeholder in (
            unit.unit_cost,
            unit.expected_profit,
        )
    )


async def test_empty_project_totals_are_zero_not_none(seeded_db, user_factory, project_factory):
    project = await project_factory("U3-16")
    user = await _patron(seeded_db, user_factory, "u316@t.co")

    totals = (await service.list_units(seeded_db, user, project.id)).totals

    assert totals.total_value == Decimal("0.00")
    assert totals.total_list_price == Decimal("0.00")
    assert totals.total_appraisal_value == Decimal("0.00")
    assert totals.total_gross_area_m2 == Decimal("0.00")
    assert totals.counts.total == 0


# --- N+1 korumasi ---


async def test_reads_units_in_single_query(seeded_db, user_factory, project_factory):
    """Blok basina ayri sorgu ATILMAZ — uniteler tek sorguda cekilip Python'da dagitilir."""
    project = await project_factory("U3-17")
    site = await _site(seeded_db, project)
    for index in range(5):
        block = await _block(seeded_db, project, site, name=f"{index} Blok")
        await _unit(seeded_db, project, block, "1")
    user = await _patron(seeded_db, user_factory, "u317@t.co")

    with _captured_statements() as statements:
        await service.list_units(seeded_db, user, project.id)

    unit_queries = [s for s in statements if "FROM units" in s]
    assert len(unit_queries) == 1, statements


# --- gorunurluk (spec §8) ---


async def test_invisible_project_raises_not_found(seeded_db, user_factory, project_factory):
    """Gorunmeyen proje 404 — 403 DEGIL; `visible_projects` yeniden kullanilir."""
    project = await project_factory("U3-18")
    user = await user_factory(email="u318@t.co", password="parola1234", role_key="patron")

    with pytest.raises(NotFoundError):
        await service.list_units(seeded_db, user, project.id)

    with pytest.raises(NotFoundError):
        await service.list_blocks(seeded_db, user, project.id)


async def test_unknown_project_raises_not_found(seeded_db, user_factory):
    user = await _patron(seeded_db, user_factory, "u319@t.co")

    with pytest.raises(NotFoundError):
        await service.list_units(seeded_db, user, uuid.uuid4())


async def test_list_blocks_returns_counts_and_site_name(seeded_db, user_factory, project_factory):
    project = await project_factory("U3-20")
    site = await _site(seeded_db, project, name="Kuzey")
    block = await _block(seeded_db, project, site, name="A Blok")
    await _unit(seeded_db, project, block, "1", unit_kind=UnitKind.apartment)
    await _unit(seeded_db, project, block, "D1", unit_kind=UnitKind.shop)
    user = await _patron(seeded_db, user_factory, "u320@t.co")

    response = await service.list_blocks(seeded_db, user, project.id)

    assert [b.name for b in response.blocks] == ["A Blok"]
    assert response.blocks[0].site_name == "Kuzey"
    assert response.blocks[0].counts.apartment == 1
    assert response.blocks[0].counts.shop == 1
    assert response.blocks[0].counts.total == 2

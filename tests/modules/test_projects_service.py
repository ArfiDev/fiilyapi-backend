from decimal import Decimal

import pytest

from app.core.discipline_scope import UNRESTRICTED
from app.core.errors import NotFoundError, ProjectTypeMismatchError
from app.modules.projects.models import LandShareShareholder, ProjectLandShare
from app.modules.projects.schemas import (
    ProjectCreate,
    ProjectInvestmentInput,
    ProjectLandShareInput,
    ProjectUpdate,
)
from app.modules.projects.service import (
    create_project,
    get_project_detail,
    list_projects_overview,
    update_project,
)
from app.modules.users.models import ProjectMember


async def _grant_all(seeded_db, user) -> None:
    user.all_projects = True
    await seeded_db.flush()


async def _writer(seeded_db, user_factory, email: str):
    """Yazma testleri icin tum projelere erisimi olan aktor.

    `update_project` artik gorunurluk suzgecinden geciyor (IDOR duzeltmesi):
    servis testleri de gercek bir aktor tasimak zorunda.
    """
    user = await user_factory(email=email, password="parola1234", role_key="patron")
    await _grant_all(seeded_db, user)
    return user


async def test_counts_ignore_filters(seeded_db, user_factory, project_factory):
    await project_factory("T-1", project_type="taahhut", status="active")
    await project_factory("T-2", project_type="taahhut", status="completed")
    await project_factory("KY-1", project_type="kendi_yatirim", status="active")
    await project_factory("KK-1", project_type="kat_karsiligi", status="active")
    user = await user_factory(email="p@t.co", password="parola1234", role_key="patron")
    await _grant_all(seeded_db, user)

    result = await list_projects_overview(
        seeded_db, user, type_filter="taahhut", status_filter=None, scope=UNRESTRICTED
    )

    assert [p.code for p in result.items] == ["T-1", "T-2"]
    assert result.counts.all == 4
    assert result.counts.taahhut == 2
    assert result.counts.kendi_yatirim == 1
    assert result.counts.kat_karsiligi == 1
    assert result.counts.completed == 1


async def test_status_filter_selects_completed(seeded_db, user_factory, project_factory):
    await project_factory("T-1", status="active")
    await project_factory("T-2", status="completed")
    user = await user_factory(email="p2@t.co", password="parola1234", role_key="patron")
    await _grant_all(seeded_db, user)

    result = await list_projects_overview(
        seeded_db, user, type_filter=None, status_filter="completed", scope=UNRESTRICTED
    )

    assert [p.code for p in result.items] == ["T-2"]
    assert result.counts.all == 2


async def test_scope_filter_limits_non_admin(seeded_db, user_factory, project_factory):
    granted = await project_factory("T-1")
    await project_factory("T-2")
    user = await user_factory(email="p3@t.co", password="parola1234", role_key="patron")
    seeded_db.add(ProjectMember(user_id=user.id, project_id=granted.id, role_id=user.role_id))
    await seeded_db.flush()

    result = await list_projects_overview(
        seeded_db, user, type_filter=None, status_filter=None, scope=UNRESTRICTED
    )

    assert [p.code for p in result.items] == ["T-1"]
    assert result.counts.all == 1


async def test_admin_bypasses_scope_filter(seeded_db, user_factory, project_factory):
    """Ayarlar kilitlenme korumasi: erisim satiri olmayan system_admin her seyi gorur."""
    await project_factory("T-1")
    await project_factory("T-2")
    admin = await user_factory(email="a@t.co", password="parola1234", role_key="system_admin")

    result = await list_projects_overview(
        seeded_db, admin, type_filter=None, status_filter=None, scope=UNRESTRICTED
    )

    assert [p.code for p in result.items] == ["T-1", "T-2"]


async def test_taahhut_item_has_contracting_placeholders(seeded_db, user_factory, project_factory):
    await project_factory(
        "T-1",
        project_type="taahhut",
        category="Konut",
        city="Ankara",
        employer_name="Güneşkent A.Ş.",
        contract_amount="11200000.00",
    )
    user = await user_factory(email="p4@t.co", password="parola1234", role_key="patron")
    await _grant_all(seeded_db, user)

    item = (await list_projects_overview(seeded_db, user, None, None, scope=UNRESTRICTED)).items[0]

    assert item.investment is None
    assert item.land_share is None
    assert item.contract_amount == Decimal("11200000.00")
    # P10 T4: `spent` BAGLANDI (taseron hakedisi approved+paid brut). Hakedisi
    # olmayan projede `0.00` GERCEK cevaptir; eski `progress_payments` etiketi
    # ISVEREN hakedisini gosteriyordu ve yanlisti (o taahhutte GELIRDIR).
    assert item.contracting.spent.available is True
    assert item.contracting.spent.value == Decimal("0.00")
    assert item.contracting.spent.pending_module is None
    # T4 (puantaj §4): `worker_count` YER TUTUCU DEGIL — kaydi olmayan projede
    # bile `available` true, sayi UYDURULMAZ (0). Davranisin tamami
    # `tests/timesheet/test_worker_count_binding.py`de.
    assert item.contracting.worker_count.available is True
    assert item.contracting.worker_count.count == 0
    assert item.contracting.worker_count.pending_module == "timesheet"
    assert item.contracting.subcontractor_count.pending_module == "subcontracts"


async def test_land_share_item_is_real_where_data_exists(seeded_db, user_factory, project_factory):
    project = await project_factory("KK-1", project_type="kat_karsiligi")
    seeded_db.add(
        ProjectLandShare(
            project_id=project.id,
            landowner_name="Yılmaz Ailesi",
            our_share_pct=Decimal("55.00"),
            owner_share_pct=Decimal("45.00"),
        )
    )
    for name in ("A. Yılmaz", "B. Yılmaz", "C. Yılmaz"):
        seeded_db.add(
            LandShareShareholder(project_id=project.id, name=name, share_pct=Decimal("33.33"))
        )
    await seeded_db.flush()
    user = await user_factory(email="p5@t.co", password="parola1234", role_key="patron")
    await _grant_all(seeded_db, user)

    item = (await list_projects_overview(seeded_db, user, None, None, scope=UNRESTRICTED)).items[0]

    assert item.contracting is None
    assert item.land_share.landowner_name == "Yılmaz Ailesi"
    assert item.land_share.our_share_pct == Decimal("55.00")
    assert item.land_share.land_cost == Decimal("0")
    assert item.land_share.shareholder_count == 3
    assert item.land_share.construction_cost.pending_module == "project_costs"
    # DEGISTI (KK-TARAF): `pending_module` iddiasi KORUNDU (dolu CountPlaceholder
    # modul adini tasimaya devam eder — `_worker_count` emsali), yanina bugunku
    # gercek eklendi: bu projenin hic unitesi yok ve iki sayac da DOLU zarf
    # icinde `0` doner ("bilinmiyor" degil, "bizim payimizda sifir unite").
    assert item.land_share.our_unit_count.pending_module == "units"
    assert item.land_share.our_unit_count.available is True
    assert item.land_share.our_unit_count.count == 0
    assert item.land_share.owner_unit_count.count == 0


async def test_detail_outside_visible_set_raises_not_found(
    seeded_db, user_factory, project_factory
):
    hidden = await project_factory("T-1")
    user = await user_factory(email="p6@t.co", password="parola1234", role_key="patron")

    with pytest.raises(NotFoundError):
        await get_project_detail(seeded_db, user, hidden.id, scope=UNRESTRICTED)


async def test_create_taahhut_project(db_session):
    # employer_name gövdeden kaldırıldı (B4); taahhüt zorunlulukları için ya tam
    # veri ya taslak gerek. Burada taslak yeter (id + uzantıların None'lığı test edilir).
    project = await create_project(
        db_session,
        ProjectCreate(
            code="GK-C",
            name="Güneşkent C-Blok",
            project_type="taahhut",
            is_draft=True,
        ),
    )
    assert project.id is not None
    assert project.investment is None
    assert project.land_share is None


async def test_create_kat_karsiligi_with_shareholders(db_session):
    project = await create_project(
        db_session,
        ProjectCreate(
            code="KK-9",
            name="Bahçelievler Konut",
            project_type="kat_karsiligi",
            city="Ankara",
            land_share=ProjectLandShareInput(
                landowner_name="Yılmaz Ailesi",
                our_share_pct=Decimal("55.00"),
                owner_share_pct=Decimal("45.00"),
                shareholders=[
                    {"name": "A. Yılmaz", "share_pct": Decimal("60.00")},
                    {"name": "B. Yılmaz", "share_pct": Decimal("40.00")},
                ],
            ),
        ),
    )
    assert project.land_share.our_share_pct == Decimal("55.00")
    assert [s.name for s in project.shareholders] == ["A. Yılmaz", "B. Yılmaz"]


async def test_investment_on_taahhut_raises_422_error(db_session):
    with pytest.raises(ProjectTypeMismatchError):
        await create_project(
            db_session,
            ProjectCreate(
                code="T-9",
                name="Yanlış",
                project_type="taahhut",
                investment=ProjectInvestmentInput(sales_target=Decimal("1.00")),
            ),
        )


async def test_land_share_on_kendi_yatirim_raises_422_error(db_session):
    with pytest.raises(ProjectTypeMismatchError):
        await create_project(
            db_session,
            ProjectCreate(
                code="KY-9",
                name="Yanlış",
                project_type="kendi_yatirim",
                land_share=ProjectLandShareInput(
                    landowner_name="X",
                    our_share_pct=Decimal("50.00"),
                    owner_share_pct=Decimal("50.00"),
                ),
            ),
        )


async def test_update_replaces_shareholder_list(seeded_db, user_factory):
    actor = await _writer(seeded_db, user_factory, "upd1@t.co")
    project = await create_project(
        seeded_db,
        ProjectCreate(
            code="KK-10",
            name="Replace Testi",
            project_type="kat_karsiligi",
            city="Ankara",
            land_share=ProjectLandShareInput(
                landowner_name="Yılmaz Ailesi",
                our_share_pct=Decimal("55.00"),
                owner_share_pct=Decimal("45.00"),
                shareholders=[{"name": "Eski", "share_pct": Decimal("100.00")}],
            ),
        ),
    )

    updated = await update_project(
        seeded_db,
        actor,
        project.id,
        ProjectUpdate(
            land_share=ProjectLandShareInput(
                landowner_name="Yılmaz Ailesi",
                our_share_pct=Decimal("60.00"),
                owner_share_pct=Decimal("40.00"),
                shareholders=[
                    {"name": "Yeni 1", "share_pct": Decimal("70.00")},
                    {"name": "Yeni 2", "share_pct": Decimal("30.00")},
                ],
            )
        ),
    )

    assert updated.land_share.our_share_pct == Decimal("60.00")
    assert [s.name for s in updated.shareholders] == ["Yeni 1", "Yeni 2"]


async def test_update_common_fields_only(seeded_db, user_factory, project_factory):
    actor = await _writer(seeded_db, user_factory, "upd2@t.co")
    project = await project_factory("T-5", name="Eski Ad")

    updated = await update_project(
        seeded_db, actor, project.id, ProjectUpdate(name="Yeni Ad", city="Bursa")
    )

    assert updated.name == "Yeni Ad"
    assert updated.city == "Bursa"
    assert updated.code == "T-5"


async def test_update_missing_project_raises_not_found(seeded_db, user_factory):
    import uuid as uuid_mod

    actor = await _writer(seeded_db, user_factory, "upd3@t.co")

    with pytest.raises(NotFoundError):
        await update_project(seeded_db, actor, uuid_mod.uuid4(), ProjectUpdate(name="X"))

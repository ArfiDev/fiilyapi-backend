"""Task 9 — GET /projects/{id} yanitindaki santiye sayaci (spec §1).

Bu GERCEK bir degerdir, yer tutucu degil: sayacin girdisi (sites tablosu) bu
dilimde yazildi. P1 sozlesmesine EKLEMEDIR, kirici degisiklik degil.
"""

from app.core.discipline_scope import UNRESTRICTED
from app.modules.projects.schemas import ProjectDetailResponse, ProjectListItem
from app.modules.projects.service import get_project_detail
from app.modules.sites.models import Site


async def _admin(session, user_factory, email: str):
    return await user_factory(email=email, password="parola1234", role_key="system_admin")


def test_site_count_is_a_real_field_not_a_placeholder():
    assert "site_count" in ProjectDetailResponse.model_fields
    assert ProjectDetailResponse.model_fields["site_count"].annotation is int


def test_list_item_contract_is_unchanged():
    """P1 liste sozlesmesi genisletilmedi — degisiklik yalniz detayda (plan Task 9)."""
    assert "site_count" not in ProjectListItem.model_fields


async def test_project_without_sites_counts_zero(seeded_db, user_factory, project_factory):
    project = await project_factory("SC-1")
    admin = await _admin(seeded_db, user_factory, "sc1@t.co")

    detail = await get_project_detail(seeded_db, admin, project.id, scope=UNRESTRICTED)

    assert detail.site_count == 0


async def test_project_with_two_sites_counts_two(seeded_db, user_factory, project_factory):
    project = await project_factory("SC-2")
    seeded_db.add(Site(project_id=project.id, code="A", name="A-Blok"))
    seeded_db.add(Site(project_id=project.id, code="B", name="B-Blok"))
    await seeded_db.flush()
    admin = await _admin(seeded_db, user_factory, "sc2@t.co")

    detail = await get_project_detail(seeded_db, admin, project.id, scope=UNRESTRICTED)

    assert detail.site_count == 2


async def test_site_count_does_not_leak_other_projects(seeded_db, user_factory, project_factory):
    project = await project_factory("SC-3")
    other = await project_factory("SC-4")
    seeded_db.add(Site(project_id=project.id, code="A", name="A-Blok"))
    seeded_db.add(Site(project_id=other.id, code="X", name="X-Blok"))
    seeded_db.add(Site(project_id=other.id, code="Y", name="Y-Blok"))
    await seeded_db.flush()
    admin = await _admin(seeded_db, user_factory, "sc3@t.co")

    assert (
        await get_project_detail(seeded_db, admin, project.id, scope=UNRESTRICTED)
    ).site_count == 1
    assert (
        await get_project_detail(seeded_db, admin, other.id, scope=UNRESTRICTED)
    ).site_count == 2


async def test_site_count_over_api(client, db_session, user_factory, project_factory):
    project = await project_factory("SC-5")
    db_session.add(Site(project_id=project.id, code="A", name="A-Blok"))
    await db_session.flush()
    user = await user_factory(email="sc5@t.co", password="parola1234", role_key="patron")
    user.all_projects = True
    await db_session.flush()
    login = await client.post("/auth/login", json={"email": "sc5@t.co", "password": "parola1234"})
    token = login.json()["access_token"]

    resp = await client.get(f"/projects/{project.id}", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.json()["site_count"] == 1


async def test_created_project_detail_reports_zero_sites(client, db_session, user_factory):
    # Proje olusturma admin isidir (kullanici karari 2026-07-28) — bu yuzden
    # aktor system_admin; erisim satiri gerekmez, admin suzgeci atlar.
    await user_factory(email="sc6@t.co", password="parola1234", role_key="system_admin")
    await db_session.flush()
    login = await client.post("/auth/login", json={"email": "sc6@t.co", "password": "parola1234"})
    token = login.json()["access_token"]

    resp = await client.post(
        "/projects",
        json={"code": "SC-7", "name": "Yeni Proje", "project_type": "taahhut", "is_draft": True},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 201
    assert resp.json()["site_count"] == 0

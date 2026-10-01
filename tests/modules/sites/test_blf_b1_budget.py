"""BLF-B1.1 — bolum bedeli YALNIZ turev (kullanici karari F-a).

* `budget_amount` Create/Update govdesinden ve yanittan KALKTI; eski istemci gonderirse
  Pydantic varsayilani (`extra="ignore"`) sessizce yok sayar (422 DEGIL) ve DB kolonu degismez.
* Taslak disi bedelsiz bolum 201 (eskiden 422 `SECTION_BUDGET_REQUIRED`).
* Turev `budget` BOQ tahsisi eklenince dolar.
"""

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.modules.boq.models import BoqItemSectionAllocation
from app.modules.sites.models import Section, Site
from app.modules.users.models import UserProjectAccess
from tests._section_types import SEED_TYPE_IDS, seed_section_types
from tests.modules._boq import _group, _item

WRITE_ROLE = "patron"

_PUBLISHED = {
    "name": "Kat 1-5",
    "section_type_id": str(SEED_TYPE_IDS["structural"]),
    "manager_name": "Ali Veli",
    "start_date": "2026-10-01",
    "end_date": "2027-03-31",
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, session, user_factory) -> str:
    address = f"{WRITE_ROLE}-{uuid.uuid4().hex[:6]}@t.co"
    user = await user_factory(email=address, password="parola1234", role_key=WRITE_ROLE)
    session.add(UserProjectAccess(user_id=user.id, project_id=None, all_projects=True))
    await session.flush()
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


async def _site(session, project_factory, slug: str) -> Site:
    project = await project_factory(f"{slug}-{uuid.uuid4().hex[:6]}")
    site = Site(project_id=project.id, code=f"SNT-{uuid.uuid4().hex[:6]}", name="Şantiye")
    session.add(site)
    await seed_section_types(session)
    await session.flush()
    return site


async def test_published_section_without_budget_is_201(
    client, db_session, user_factory, project_factory
):
    site = await _site(db_session, project_factory, "BLF-BEDELSIZ")
    token = await _login(client, db_session, user_factory)

    resp = await client.post(f"/sites/{site.id}/sections", json=_PUBLISHED, headers=_auth(token))

    assert resp.status_code == 201, resp.text
    assert resp.json()["is_draft"] is False


async def test_budget_amount_in_post_body_is_ignored_not_422(
    client, db_session, user_factory, project_factory
):
    site = await _site(db_session, project_factory, "BLF-POST-IGN")
    token = await _login(client, db_session, user_factory)

    resp = await client.post(
        f"/sites/{site.id}/sections",
        json={**_PUBLISHED, "budget_amount": "2840000.00"},
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    stored = await db_session.get(Section, uuid.UUID(resp.json()["id"]))
    assert stored.budget_amount is None  # yazilmadi
    assert "budget_amount" not in resp.json()


async def test_budget_amount_in_patch_body_is_ignored_and_column_unchanged(
    client, db_session, user_factory, project_factory
):
    site = await _site(db_session, project_factory, "BLF-PATCH-IGN")
    legacy = Section(site_id=site.id, name="Eski", budget_amount=Decimal("5.00"))
    db_session.add(legacy)
    await db_session.flush()
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sections/{legacy.id}",
        json={"name": "Yeni Ad", "budget_amount": "999.00"},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(legacy)
    assert legacy.name == "Yeni Ad"
    assert legacy.budget_amount == Decimal("5.00")  # eski kolon degismedi
    assert "budget_amount" not in resp.json()
    # Negatif deger bile artik 422 DEGIL: alan semada yok.
    again = await client.patch(
        f"/sections/{legacy.id}", json={"budget_amount": "-1.00"}, headers=_auth(token)
    )
    assert again.status_code == 200, again.text


async def test_no_response_surface_exposes_budget_amount(
    client, db_session, user_factory, project_factory
):
    site = await _site(db_session, project_factory, "BLF-YUZEY")
    legacy = Section(site_id=site.id, name="Eski", budget_amount=Decimal("5.00"))
    db_session.add(legacy)
    await db_session.flush()
    token = await _login(client, db_session, user_factory)

    detail = (await client.get(f"/sections/{legacy.id}", headers=_auth(token))).json()
    listing = (await client.get(f"/sites/{site.id}/sections", headers=_auth(token))).json()
    site_detail = (await client.get(f"/sites/{site.id}", headers=_auth(token))).json()

    assert "budget_amount" not in detail
    assert all("budget_amount" not in row for row in listing["items"])
    assert all("budget_amount" not in row for row in site_detail["sections"])


async def test_derived_budget_fills_when_allocation_is_added(
    client, db_session, user_factory, project_factory
):
    site = await _site(db_session, project_factory, "BLF-TUREV")
    token = await _login(client, db_session, user_factory)
    created = await client.post(f"/sites/{site.id}/sections", json=_PUBLISHED, headers=_auth(token))
    section_id = uuid.UUID(created.json()["id"])
    assert created.json()["budget"] == {"available": True, "value": "0.00", "pending_module": None}

    group = await _group(db_session, site)
    item = await _item(db_session, site, group, code="01.001")
    db_session.add(
        BoqItemSectionAllocation(
            boq_item_id=item.id, section_id=section_id, quantity=Decimal("400.000")
        )
    )
    await db_session.flush()

    detail = (await client.get(f"/sections/{section_id}", headers=_auth(token))).json()

    assert detail["budget"]["value"] == "112000.00"  # 400 x 280 (varsayilan poz fiyati)
    rows = (await db_session.execute(select(Section).where(Section.id == section_id))).scalars()
    assert next(iter(rows)).budget_amount is None  # kolondan degil, tahsisten turedi

"""Bütçe uçları fikstürleri — bağımsız kurulum (`tests/site_planning/conftest.py` deseni).

```
Şantiye "A-Blok"  bölümler: S1 04–15.05.2026 (12 kişi) · S2 18–29.05.2026 (8 kişi)
G1 Betonarme  I1 01.001 "Beton"  m3  100 → S1 60 · S2 30 · Bölümsüz 10
G2 Duvar      I2 02.001 "Tuğla"  m2   50 → S1 50
Disiplinler   KAB (own) · DUV (subcon)
Katalog       KAB "Beton" m³ 1,80 · DUV "Tuğla" m2 0,55 · KAB "TUĞLA" m2 0,60
```
"TUĞLA" KAB'dadir: ayni disiplinde "Tuğla" ile yan yana DURAMAZ (KATALOG-UQ,
`uq_ev_catalog_items_disc_name_key_uom_key`). Belirsizlik artik yalniz DISIPLINSIZ kalemde
dogar (G2 eslenmezse I2 her iki disiplinin tam eslesmesini gorur).
earned_value matrisi: system_admin A · site_chief APPROVE · field_engineer DRAFT ·
accounting VIEW · hr_manager NONE.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqGroup, BoqItem, BoqItemSectionAllocation
from app.modules.catalog.models import EvCatalogItem, EvDiscipline
from app.modules.earned_value.engine import ContractorType
from app.modules.projects.models import Project
from app.modules.sites.models import Section, Site
from app.modules.users.models import User, UserProjectAccess

D = Decimal


async def _headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    role_key: str,
    email: str,
    project: Project | None = None,
) -> dict[str, str]:
    await user_factory(email=email, password="parola1234", role_key=role_key)
    if project is not None:
        user = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one()
        seeded_db.add(UserProjectAccess(user_id=user.id, project_id=project.id, all_projects=False))
        await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
async def proje(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="EV-P01", name="Güneşkent Konut")


@pytest.fixture
async def santiye(seeded_db: AsyncSession, proje: Project) -> Site:
    site = Site(project_id=proje.id, code="EV-A", name="A-Blok Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def gorunmeyen_santiye(seeded_db: AsyncSession, project_factory) -> Site:
    project = await project_factory(code="EV-G01", name="Görünmeyen Proje")
    site = Site(project_id=project.id, code="EV-G", name="Görünmeyen Şantiye")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def bolumler(seeded_db: AsyncSession, santiye: Site) -> tuple[Section, Section]:
    s1 = Section(
        site_id=santiye.id,
        name="A Blok",
        start_date=date(2026, 5, 4),
        end_date=date(2026, 5, 15),
        planned_worker_count=12,
        sort_order=1,
    )
    s2 = Section(
        site_id=santiye.id,
        name="B Blok",
        start_date=date(2026, 5, 18),
        end_date=date(2026, 5, 29),
        planned_worker_count=8,
        sort_order=2,
    )
    seeded_db.add_all([s1, s2])
    await seeded_db.flush()
    return s1, s2


@pytest.fixture
async def boq(seeded_db: AsyncSession, santiye: Site, bolumler) -> dict:
    s1, s2 = bolumler
    g1 = BoqGroup(site_id=santiye.id, name="Betonarme", sort_order=1)
    g2 = BoqGroup(site_id=santiye.id, name="Duvar", sort_order=2)
    seeded_db.add_all([g1, g2])
    await seeded_db.flush()
    i1 = BoqItem(
        site_id=santiye.id,
        group_id=g1.id,
        code="01.001",
        description="Beton",
        unit="m3",
        quantity=D(100),
        unit_price=D(0),
        sort_order=1,
    )
    i2 = BoqItem(
        site_id=santiye.id,
        group_id=g2.id,
        code="02.001",
        description="Tuğla",
        unit="m2",
        quantity=D(50),
        unit_price=D(0),
        sort_order=1,
    )
    seeded_db.add_all([i1, i2])
    await seeded_db.flush()
    seeded_db.add_all(
        [
            BoqItemSectionAllocation(boq_item_id=i1.id, section_id=s1.id, quantity=D(60)),
            BoqItemSectionAllocation(boq_item_id=i1.id, section_id=s2.id, quantity=D(30)),
            BoqItemSectionAllocation(boq_item_id=i2.id, section_id=s1.id, quantity=D(50)),
        ]
    )
    await seeded_db.flush()
    return {"g1": g1, "g2": g2, "i1": i1, "i2": i2, "s1": s1, "s2": s2}


@pytest.fixture
async def disiplinler(seeded_db: AsyncSession) -> tuple[EvDiscipline, EvDiscipline]:
    kab = EvDiscipline(
        code="KAB",
        name="Kaba İnşaat",
        color="#2563eb",
        default_contractor_type=ContractorType.OWN,
        sort_order=1,
    )
    duv = EvDiscipline(
        code="DUV",
        name="Duvar & Sıva",
        color="#16a34a",
        default_contractor_type=ContractorType.SUBCON,
        sort_order=2,
    )
    seeded_db.add_all([kab, duv])
    await seeded_db.flush()
    return kab, duv


@pytest.fixture
async def katalog(seeded_db: AsyncSession, disiplinler) -> dict:
    kab, duv = disiplinler
    rows = {
        "beton": EvCatalogItem(
            discipline_id=kab.id,
            name="Beton",
            uom="m³",
            standard_unit_mhr=D("1.80"),
            default_contractor_type=ContractorType.OWN,
        ),
        "tugla": EvCatalogItem(
            discipline_id=duv.id,
            name="Tuğla",
            uom="m2",
            standard_unit_mhr=D("0.55"),
            default_contractor_type=ContractorType.SUBCON,
        ),
        "tugla2": EvCatalogItem(
            discipline_id=kab.id,
            name="TUĞLA",
            uom="m2",
            standard_unit_mhr=D("0.60"),
            default_contractor_type=ContractorType.SUBCON,
        ),
    }
    seeded_db.add_all(rows.values())
    await seeded_db.flush()
    return rows


@pytest.fixture
async def admin(client, seeded_db, user_factory) -> dict[str, str]:
    return await _headers(client, seeded_db, user_factory, "system_admin", "admin@ev-b1.co")


@pytest.fixture
async def sef(client, seeded_db, user_factory, proje) -> dict[str, str]:
    return await _headers(client, seeded_db, user_factory, "site_chief", "sef@ev-b1.co", proje)


@pytest.fixture
async def saha(client, seeded_db, user_factory, proje) -> dict[str, str]:
    return await _headers(client, seeded_db, user_factory, "field_engineer", "saha@ev-b1.co", proje)


@pytest.fixture
async def muhasebe(client, seeded_db, user_factory, proje) -> dict[str, str]:
    return await _headers(client, seeded_db, user_factory, "accounting", "muh@ev-b1.co", proje)


@pytest.fixture
async def ik(client, seeded_db, user_factory, proje) -> dict[str, str]:
    return await _headers(client, seeded_db, user_factory, "hr_manager", "ik@ev-b1.co", proje)


# --- saha günü (PLN-B2): dondurulmuş baseline + puantaj + günlük -------------------------

#: 05.05.2026 Salı — S1 penceresi içinde. Puantaj: Ali 9 · Veli 8. Günlük: I1 Bölümsüz 5 m3.
DAY = date(2026, 5, 5)


@pytest.fixture
async def baseline(client, admin, santiye, boq, disiplinler) -> None:
    from .test_budget_api import _map, _rates, _url

    await _map(client, santiye, admin, boq, disiplinler)
    await _rates(client, santiye, admin, boq)
    assert (await client.post(_url(santiye, "/freeze"), headers=admin, json={})).status_code == 200


@pytest.fixture
async def saha_gunu(seeded_db: AsyncSession, santiye: Site, boq, admin) -> dict:
    from app.modules.personnel.models import Personnel
    from app.modules.site_diary.models import (
        DiaryStatus,
        SiteDiaryEntry,
        SiteDiaryLine,
        WorkerSource,
    )
    from app.modules.timesheet.models import TimesheetEntry

    creator = (
        await seeded_db.execute(select(User).where(User.email == "admin@ev-b1.co"))
    ).scalar_one()
    people = {
        name: Personnel(
            full_name=name,
            trade=trade,
            source=WorkerSource.company,
            is_active=True,
            is_draft=False,
        )
        for name, trade in (("Ali Usta", "Kalıpçı"), ("Veli Usta", "Betoncu"))
    }
    seeded_db.add_all(people.values())
    await seeded_db.flush()
    ts = {}
    for name, hours in (("Ali Usta", "9"), ("Veli Usta", "8")):
        entry = TimesheetEntry(
            personnel_id=people[name].id,
            site_id=santiye.id,
            project_id=santiye.project_id,
            work_date=DAY,
            hours=D(hours),
            created_by=creator.id,
        )
        seeded_db.add(entry)
        ts[name] = entry
    diary = SiteDiaryEntry(
        site_id=santiye.id,
        project_id=santiye.project_id,
        entry_date=DAY,
        status=DiaryStatus.draft,
        created_by=creator.id,
    )
    seeded_db.add(diary)
    await seeded_db.flush()
    line = SiteDiaryLine(
        entry_id=diary.id,
        boq_item_id=boq["i1"].id,
        code="01.001",
        description="Beton",
        unit="m3",
        unit_price=D(0),
        quantity=D(5),
    )
    seeded_db.add(line)
    await seeded_db.flush()
    return {
        "ali": people["Ali Usta"],
        "veli": people["Veli Usta"],
        "diary": diary,
        "line": line,
        "ts": ts,
    }

"""Planlama (EV) katalog + ayar uclari (PLN-B1.3) fixture'lari — bagimsiz kurulum.

`tests/site_planning/conftest.py` deseninin kardesi: kok `tests/conftest.py`in
`db_session`/`seeded_db`/`user_factory`/`project_factory` fixture'lari uzerine kurulur;
kardes paketlerden HICBIR SEY miras alinmaz.

Izin satiri `earned_value` (`roles/seed_data.py`, spec §3.9 B1-8):
system_admin=A · patron=F · site_chief=APPROVE · field_engineer=DRAFT · hr_manager=N ·
accounting=V · project_manager=F · procurement=N. Kapilar: VIEW=view · WRITE=draft ·
CATALOG=full · ADMIN=admin (`earned_value/access.py`).

system_admin disindaki her rol kapsami TEK projeye (`proje`) kisitli kurulur; boylece
ayni kullanici baska projenin santiyesinde IDOR yuzeyini sinar.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditLog
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.catalog.models import EvCatalogItem, EvDiscipline
from app.modules.earned_value.engine import ContractorType
from app.modules.projects.models import Project
from app.modules.sites.models import Site
from app.modules.users.models import UserProjectAccess

PASSWORD = "parola1234"

#: B1-8 satiri, rol → seed seviyesi (test adlarinda okunur kalsin diye).
ROLES = (
    "system_admin",
    "patron",
    "site_chief",
    "field_engineer",
    "hr_manager",
    "accounting",
    "project_manager",
    "procurement",
)
CAN_VIEW = {
    "system_admin",
    "patron",
    "site_chief",
    "field_engineer",
    "accounting",
    "project_manager",
}
CAN_WRITE = {"system_admin", "patron", "site_chief", "field_engineer", "project_manager"}
CAN_CATALOG = {"system_admin", "patron", "project_manager"}
CAN_ADMIN = {"system_admin"}

Headers = dict[str, str]
LoginFn = Callable[[str], Awaitable[Headers]]


def auth(token: str) -> Headers:
    return {"Authorization": f"Bearer {token}"}


def settings_url(site_id: uuid.UUID) -> str:
    return f"/sites/{site_id}/earned-value/settings"


async def audit_details(session: AsyncSession) -> list[str]:
    return list((await session.execute(select(AuditLog.detail))).scalars())


async def audit_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count(AuditLog.id)))).scalar_one()


# --- Projeler / santiyeler ---


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
async def ikinci_santiye(seeded_db: AsyncSession, proje: Project) -> Site:
    """AYNI projenin ikinci santiyesi — ayar yazimi `site_id` ile sinirli mi?"""
    site = Site(project_id=proje.id, code="EV-B", name="B-Blok Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def gorunmeyen_santiye(seeded_db: AsyncSession, project_factory) -> Site:
    """Kapsami kisitli kullanicilara ASLA gorunmeyen projenin santiyesi."""
    project = await project_factory(code="EV-G01", name="Görünmeyen Proje")
    site = Site(project_id=project.id, code="EV-G", name="Görünmeyen Şantiye")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


async def _boq_items(session: AsyncSession, site: Site, codes: list[str]) -> list[BoqItem]:
    group = BoqGroup(site_id=site.id, name="BETONARME İŞLERİ")
    session.add(group)
    await session.flush()
    items = [
        BoqItem(
            site_id=site.id,
            group_id=group.id,
            code=code,
            description=f"Kalem {code}",
            unit="m³",
            quantity=Decimal("100"),
            unit_price=Decimal("10"),
            sort_order=order,
        )
        for order, code in enumerate(codes)
    ]
    session.add_all(items)
    await session.flush()
    return items


@pytest.fixture
async def kalemler(seeded_db: AsyncSession, santiye: Site) -> list[BoqItem]:
    """`santiye`nin BOQ'u: kalip · demir · beton (AYP pacal ornegi)."""
    return await _boq_items(seeded_db, santiye, ["KAB.01.01", "KAB.01.02", "KAB.01.03"])


@pytest.fixture
async def yabanci_kalem(seeded_db: AsyncSession, ikinci_santiye: Site) -> BoqItem:
    """BASKA santiyenin (ayni proje) kalemi — pacal metrigine giremez."""
    return (await _boq_items(seeded_db, ikinci_santiye, ["KAB.09.01"]))[0]


# --- Kullanicilar ---


@pytest.fixture
def login(client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project) -> LoginFn:
    """Rol → Authorization basligi. system_admin disinda kapsam YALNIZ `proje`."""

    async def _login(role_key: str) -> Headers:
        email = f"{role_key}.{uuid.uuid4().hex[:8]}@ev-b13.co"
        user = await user_factory(
            email=email, password=PASSWORD, role_key=role_key, full_name=f"Kişi {role_key}"
        )
        if role_key != "system_admin":
            seeded_db.add(
                UserProjectAccess(user_id=user.id, project_id=proje.id, all_projects=False)
            )
            await seeded_db.flush()
        resp = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
        assert resp.status_code == 200, resp.text
        return auth(resp.json()["access_token"])

    return _login


@pytest.fixture
async def admin(login: LoginFn) -> Headers:
    return await login("system_admin")


# --- Sirket verisi (dogrudan DB) ---


@pytest.fixture
def disiplin_fabrikasi(seeded_db: AsyncSession):
    async def _create(
        code: str,
        name: str | None = None,
        *,
        color: str = "#2563EB",
        contractor: ContractorType = ContractorType.OWN,
        sort_order: int = 0,
    ) -> EvDiscipline:
        row = EvDiscipline(
            code=code,
            name=name or f"Disiplin {code}",
            color=color,
            default_contractor_type=contractor,
            sort_order=sort_order,
        )
        seeded_db.add(row)
        await seeded_db.flush()
        return row

    return _create


@pytest.fixture
def katalog_fabrikasi(seeded_db: AsyncSession):
    async def _create(
        discipline: EvDiscipline,
        name: str,
        *,
        uom: str = "m³",
        rate: str = "1.8000",
        contractor: ContractorType = ContractorType.OWN,
        description: str | None = None,
    ) -> EvCatalogItem:
        row = EvCatalogItem(
            discipline_id=discipline.id,
            name=name,
            uom=uom,
            standard_unit_mhr=Decimal(rate),
            default_contractor_type=contractor,
            description=description,
        )
        seeded_db.add(row)
        await seeded_db.flush()
        return row

    return _create


@pytest.fixture
async def kab(disiplin_fabrikasi) -> EvDiscipline:
    return await disiplin_fabrikasi("KAB", "Kaba İnşaat", sort_order=1)

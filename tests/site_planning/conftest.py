"""Şantiye planlama (T2) fixture'ları — bağımsız kurulum.

`tests/timesheet/conftest.py` deseninin kardeşi: kök `tests/conftest.py`in
`db_session`/`seeded_db`/`user_factory`/`project_factory` fixture'ları üzerine
kurulur, kardeş test paketlerinden HİÇBİR ŞEY miras alınmaz (pytest onları
yüklemez) ve `tests/progress_payments/test_concurrency.py`nin bilinen seed
sızıntısı borcuna BULAŞILMAZ.

İzin matrisi (`roles/seed_data.py` satır 170, **`site_diary`** — planlama kendi
modülünü AÇMAZ, spec §6 S1): system_admin=_A · patron=_F · site_chief=_F ·
field_engineer=_F · hr_manager=_N · accounting=_N · project_manager=_V ·
procurement=_N.

Yani: şef ve saha mühendisi tam yetkili, proje müdürü SALT OKUR (okuma ucunu
görebilmeli), İK planlamayı hiç göremez.
"""

import uuid
from datetime import date, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.projects.models import Project
from app.modules.site_planning.models import (
    PlanCellTag,
    PlanGoalStatus,
    PlanResourceKind,
    SitePlanCell,
    SitePlanGoal,
    SitePlanRow,
    SitePlanSprint,
)
from app.modules.sites.models import Section, Site
from app.modules.users.models import ProjectMember, User

# Mockup haftası: P107 şeridi "21 – 27 Temmuz 2026". Mockup'ın takvimi KURGUSAL
# (21 Temmuz 2026 gerçekte Salı'dır); testler GERÇEK takvimi kullanır, çünkü
# korkuluğun kendisi `date.weekday()` üzerinedir — kurgusal bir tarihle
# "Pazartesi şartı" sınanamaz. Gerçek Pazartesi: 20 Temmuz 2026.
HAFTA = date(2026, 7, 20)
ONCEKI_HAFTA = date(2026, 7, 13)


def gun(offset: int) -> date:
    """Haftanın `offset`. günü (0 = Pzt, 6 = Paz). Negatif/6 üstü değer hafta
    penceresinin DIŞINI adresler (sızıntı testleri)."""
    return HAFTA + timedelta(days=offset)


async def _login(client: AsyncClient, user_factory, role_key: str, email: str) -> str:
    await user_factory(email=email, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _scoped_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    role_key: str,
    email: str,
    project: Project,
) -> dict[str, str]:
    """Rolü verilen ama kapsamı TEK projeye kısıtlanmış kullanıcı (IDOR yüzeyi)."""
    await user_factory(email=email, password="parola1234", role_key=role_key)
    user = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one()
    seeded_db.add(ProjectMember(user_id=user.id, project_id=project.id, role_id=user.role_id))
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])


# --- Projeler / şantiyeler ---


@pytest.fixture
async def proje(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="PL-P01", name="Güneşkent Konut")


@pytest.fixture
async def santiye(seeded_db: AsyncSession, proje: Project) -> Site:
    site = Site(project_id=proje.id, code="PL-A", name="A-Blok Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def ikinci_santiye(seeded_db: AsyncSession, proje: Project) -> Site:
    """AYNI projenin ikinci şantiyesi — kapsam sınırı `visible_projects`in yan
    etkisiyle DEĞİL gerçekten `site_id` koşuluyla kurulmalıdır."""
    site = Site(project_id=proje.id, code="PL-B", name="B-Blok Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def gorunmeyen_santiye(seeded_db: AsyncSession, project_factory) -> Site:
    """Kapsamı kısıtlı kullanıcılara ASLA görünmeyen projenin şantiyesi."""
    project = await project_factory(code="PL-G01", name="Görünmeyen Proje")
    site = Site(project_id=project.id, code="PL-G", name="Görünmeyen Şantiye")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def bolum(seeded_db: AsyncSession, santiye: Site) -> Section:
    """P125 "Kat 6–10 Kaba" + P126 "Bölüm sorumlusu: Sercan Öztürk"."""
    section = Section(
        site_id=santiye.id,
        code="PB-1",
        name="Kat 6–10 Kaba",
        manager_name="Sercan Öztürk",
        sort_order=1,
    )
    seeded_db.add(section)
    await seeded_db.flush()
    return section


@pytest.fixture
async def ikinci_bolum(seeded_db: AsyncSession, santiye: Site) -> Section:
    section = Section(site_id=santiye.id, code="PB-2", name="Kat 1–5 İnce", sort_order=2)
    seeded_db.add(section)
    await seeded_db.flush()
    return section


# --- Kullanıcılar ---


@pytest.fixture
async def admin_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`system_admin` (`site_diary=_A`) — TÜM projeleri görür."""
    token = await _login(client, user_factory, "system_admin", "admin@pl-t2.co")
    return _auth(token)


@pytest.fixture
async def sef_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`site_chief` (`site_diary=_F`) — kapsamı `proje` ile SINIRLI."""
    return await _scoped_headers(
        client, seeded_db, user_factory, "site_chief", "sef@pl-t2.co", proje
    )


@pytest.fixture
async def pm_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`project_manager` (`site_diary=_V`) — planı OKUYABİLMELİ (spec §6 S1)."""
    return await _scoped_headers(
        client, seeded_db, user_factory, "project_manager", "pm@pl-t2.co", proje
    )


@pytest.fixture
async def saha_muh_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`field_engineer` (`site_diary=_F`) — T3 yazma uçlarını KULLANABİLMELİ.

    Puantajdan (`timesheet=_V`) bilinçli fark: planı saha mühendisi de doldurur,
    matrisi yalnız şef doldurur. İkisi AYNI izin modülünde olsaydı bu ayrım
    kaybolurdu.
    """
    return await _scoped_headers(
        client, seeded_db, user_factory, "field_engineer", "saha@pl-t3.co", proje
    )


@pytest.fixture
async def ik_headers(client: AsyncClient, seeded_db: AsyncSession, user_factory) -> dict[str, str]:
    """`hr_manager` (`site_diary=_N`) — okumada bile 403 (kapı en dışta)."""
    token = await _login(client, user_factory, "hr_manager", "ik@pl-t2.co")
    return _auth(token)


# --- Plan verisi (doğrudan DB — okuma testleri yazma ucuna bağımlı olmasın) ---


@pytest.fixture
def satir_fabrikasi(seeded_db: AsyncSession):
    async def _create(
        site: Site,
        label: str,
        *,
        kind: PlanResourceKind = PlanResourceKind.crew,
        section: Section | None = None,
        planned_worker_count: int | None = None,
        sort_order: int = 0,
    ) -> SitePlanRow:
        row = SitePlanRow(
            site_id=site.id,
            project_id=site.project_id,
            kind=kind,
            section_id=section.id if section is not None else None,
            label=label,
            planned_worker_count=planned_worker_count,
            sort_order=sort_order,
        )
        seeded_db.add(row)
        await seeded_db.flush()
        return row

    return _create


@pytest.fixture
def hucre_fabrikasi(seeded_db: AsyncSession):
    async def _create(
        row: SitePlanRow,
        plan_date: date,
        text: str,
        *,
        tag: PlanCellTag | None = None,
    ) -> SitePlanCell:
        cell = SitePlanCell(row_id=row.id, plan_date=plan_date, text=text, tag=tag)
        seeded_db.add(cell)
        await seeded_db.flush()
        return cell

    return _create


@pytest.fixture
def hedef_fabrikasi(seeded_db: AsyncSession):
    async def _create(
        site: Site,
        title: str,
        *,
        week_start: date = HAFTA,
        note: str | None = None,
        is_done: bool = False,
        status: PlanGoalStatus = PlanGoalStatus.in_progress,
        sort_order: int = 0,
    ) -> SitePlanGoal:
        goal = SitePlanGoal(
            site_id=site.id,
            project_id=site.project_id,
            week_start=week_start,
            title=title,
            note=note,
            is_done=is_done,
            status=status,
            sort_order=sort_order,
        )
        seeded_db.add(goal)
        await seeded_db.flush()
        return goal

    return _create


@pytest.fixture
def sprint_fabrikasi(seeded_db: AsyncSession):
    async def _create(site: Site, name: str, *, is_active: bool = True) -> SitePlanSprint:
        sprint = SitePlanSprint(site_id=site.id, name=name, is_active=is_active)
        seeded_db.add(sprint)
        await seeded_db.flush()
        return sprint

    return _create


def plan_url(site_id: uuid.UUID, week_start: date = HAFTA) -> str:
    return f"/sites/{site_id}/plan?week_start={week_start.isoformat()}"


def rows_url(site_id: uuid.UUID) -> str:
    """`PUT …/plan/rows` — satır listesi HAFTADAN BAĞIMSIZDIR (spec §2): satır
    ızgaranın kaynağıdır, hücre haftaya aittir."""
    return f"/sites/{site_id}/plan/rows"


def cells_url(site_id: uuid.UUID, week_start: date = HAFTA) -> str:
    return f"/sites/{site_id}/plan/cells?week_start={week_start.isoformat()}"


def goals_url(site_id: uuid.UUID, week_start: date = HAFTA) -> str:
    return f"/sites/{site_id}/plan/goals?week_start={week_start.isoformat()}"


def sprint_url(site_id: uuid.UUID) -> str:
    return f"/sites/{site_id}/plan/sprint"


def day_summary_url(site_id: uuid.UUID, start: date = HAFTA, days: int | None = None) -> str:
    """`GET …/plan/day-summary` — `start` HERHANGİ bir gün olabilir (Pazartesi
    şartı YOKTUR: bu haftalık ızgara değil, GK'nin kayan 5-gün bloğudur)."""
    url = f"/sites/{site_id}/plan/day-summary?start={start.isoformat()}"
    if days is not None:
        url += f"&days={days}"
    return url

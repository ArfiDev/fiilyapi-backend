"""Puantaj T3 — `timesheet` matris uçlarının fixture'ları.

`tests/site_diary/conftest.py` deseninin kardeşi: kök `tests/conftest.py`in
`db_session`/`seeded_db`/`user_factory`/`project_factory` fixture'ları üzerine
kurulur, kardeş test paketlerinden HİÇBİR ŞEY miras alınmaz (pytest onları
yüklemez) ve `tests/progress_payments/test_concurrency.py`nin bilinen seed
sızıntısı borcuna BULAŞILMAZ.

İzin matrisi (`roles/seed_data.py` satır 171, `timesheet`):
system_admin=_A · patron=_F · site_chief=**_F** · field_engineer=**_V** ·
hr_manager=_F · accounting=_V · project_manager=**_N** · procurement=_N.

Şantiye şefi tam yetkilidir (matrisi O doldurur), saha mühendisi SALT OKURDUR
(spec §3) ve proje müdürü puantaja hiç giremez.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.contracts.models import Subcontractor
from app.modules.personnel.models import Personnel
from app.modules.projects.models import Project
from app.modules.site_diary.models import WorkerSource
from app.modules.sites.models import Section, Site
from app.modules.timesheet.models import TimesheetCode, TimesheetEntry
from app.modules.users.models import ProjectMember, User
from tests.site_diary._port import port  # noqa: F401 — PLN-B2.1 port ikizi

# Mockup dönemi: E5 139 "Temmuz 2026".
YIL = 2026
AY = 7

# Mockup haftası: E5 96-97 "13 – 19 Temmuz 2026 · 29. Hafta" (ISO 2026-W29).
ISO_YIL = 2026
ISO_HAFTA = 29
#: Haftanın Pazartesi'sinin ayın kaçıncı günü olduğu — `gun(13)` = 13 Temmuz.
HAFTA_ILK_GUN = 13


def gun(day: int) -> date:
    return date(YIL, AY, day)


def hafta_gunu(offset: int) -> date:
    """29. haftanın `offset`. günü (0 = Pazartesi 13 Tem, 6 = Pazar 19 Tem)."""
    return gun(HAFTA_ILK_GUN + offset)


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
    return await project_factory(code="TS-P01", name="Güneşkent Konut")


@pytest.fixture
async def santiye(seeded_db: AsyncSession, proje: Project) -> Site:
    site = Site(project_id=proje.id, code="TS-A", name="A-Blok Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def ikinci_santiye(seeded_db: AsyncSession, proje: Project) -> Site:
    """AYNI projenin ikinci şantiyesi — kapsam sınırı testinin karşı tarafı.

    Kasten AYNI projededir: kapsam sınırı `visible_projects` süzgecinin yan
    etkisiyle DEĞİL, gerçekten `site_id` koşuluyla korunmalıdır.
    """
    site = Site(project_id=proje.id, code="TS-B", name="B-Blok Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def gorunmeyen_santiye(seeded_db: AsyncSession, project_factory) -> Site:
    """Kapsamı kısıtlı kullanıcılara ASLA görünmeyen projenin şantiyesi."""
    project = await project_factory(code="TS-G01", name="Görünmeyen Proje")
    site = Site(project_id=project.id, code="TS-G", name="Görünmeyen Şantiye")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def bolum(seeded_db: AsyncSession, santiye: Site) -> Section:
    """ŞP 117 "Kat 6–10 Kaba İnşaat" — başlık şeridindeki bölüm."""
    section = Section(site_id=santiye.id, code="B-1", name="Kat 6–10 Kaba İnşaat")
    seeded_db.add(section)
    await seeded_db.flush()
    return section


@pytest.fixture
async def ikinci_bolum(seeded_db: AsyncSession, santiye: Site) -> Section:
    section = Section(site_id=santiye.id, code="B-2", name="Kat 1–5 Kaba İnşaat")
    seeded_db.add(section)
    await seeded_db.flush()
    return section


@pytest.fixture
async def yabanci_bolum(seeded_db: AsyncSession, ikinci_santiye: Site) -> Section:
    """BAŞKA şantiyenin bölümü — sahiplik doğrulamasının yüzeyi."""
    section = Section(site_id=ikinci_santiye.id, code="B-9", name="Yabancı Bölüm")
    seeded_db.add(section)
    await seeded_db.flush()
    return section


# --- Kullanıcılar ---


@pytest.fixture
async def admin_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`system_admin` (`timesheet=_A`) — TÜM projeleri görür."""
    token = await _login(client, user_factory, "system_admin", "admin@ts-t3.co")
    return _auth(token)


@pytest.fixture
async def admin_kullanicisi(seeded_db: AsyncSession, admin_headers: dict[str, str]) -> User:
    return (
        await seeded_db.execute(select(User).where(User.email == "admin@ts-t3.co"))
    ).scalar_one()


@pytest.fixture
async def sef_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`site_chief` (`timesheet=_F`) — matrisi dolduran rol; kapsamı `proje`."""
    return await _scoped_headers(
        client, seeded_db, user_factory, "site_chief", "sef@ts-t3.co", proje
    )


@pytest.fixture
async def saha_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`field_engineer` (`timesheet=_V`) — SALT OKUR: PUT'ta 403 (spec §3)."""
    return await _scoped_headers(
        client, seeded_db, user_factory, "field_engineer", "saha@ts-t3.co", proje
    )


@pytest.fixture
async def pm_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`project_manager` (`timesheet=_N`) — okuma dahil 403."""
    return await _scoped_headers(
        client, seeded_db, user_factory, "project_manager", "pm@ts-t3.co", proje
    )


# --- Personel ---


@pytest.fixture
async def taseron(seeded_db: AsyncSession) -> Subcontractor:
    """ŞP 169 "Demir Ustası — Akın İnşaat" satırının firma adı."""
    sub = Subcontractor(name="Akın İnşaat")
    seeded_db.add(sub)
    await seeded_db.flush()
    return sub


@pytest.fixture
def personel_fabrikasi(seeded_db: AsyncSession):
    async def _create(
        full_name: str,
        *,
        trade: str | None = None,
        source: WorkerSource = WorkerSource.company,
        subcontractor_id: uuid.UUID | None = None,
        is_active: bool = True,
        is_draft: bool = False,
    ) -> Personnel:
        personnel = Personnel(
            full_name=full_name,
            trade=trade,
            source=source,
            subcontractor_id=subcontractor_id,
            is_active=is_active,
            is_draft=is_draft,
        )
        seeded_db.add(personnel)
        await seeded_db.flush()
        return personnel

    return _create


@pytest.fixture
async def mehmet(personel_fabrikasi) -> Personnel:
    """ŞP 149-150 — "Mehmet Yılmaz · Kalıpçı Usta · Şirket"."""
    return await personel_fabrikasi("Mehmet Yılmaz", trade="Kalıpçı Usta")


@pytest.fixture
async def ali(personel_fabrikasi, taseron: Subcontractor) -> Personnel:
    """ŞP 169-170 — "Ali Kaya · Demir Ustası — Akın İnşaat · Taşeron"."""
    return await personel_fabrikasi(
        "Ali Kaya",
        trade="Demir Ustası",
        source=WorkerSource.subcontractor,
        subcontractor_id=taseron.id,
    )


# --- Puantaj hücreleri (doğrudan DB) ---


@pytest.fixture
def hucre_fabrikasi(seeded_db: AsyncSession):
    """Doğrudan DB'ye hücre yazar — okuma testleri PUT ucuna bağımlı olmasın."""

    async def _create(
        site: Site,
        personnel: Personnel,
        work_date: date,
        creator: User,
        *,
        hours: Decimal | int | None = None,
        code: TimesheetCode | None = None,
        section: Section | None = None,
    ) -> TimesheetEntry:
        """Hücre = saat VEYA kod — fabrika da bu sözleşmeyi ZORLAR.

        Testin yanlışlıkla ikisini birden (ya da hiçbirini) vermesi, DB CHECK'i
        yerine burada patlar ve hata mesajı testin kendisini gösterir.
        """
        assert (hours is None) != (code is None), "hücre ya saat ya kod taşır"
        entry = TimesheetEntry(
            personnel_id=personnel.id,
            site_id=site.id,
            project_id=site.project_id,
            section_id=section.id if section is not None else None,
            work_date=work_date,
            hours=None if hours is None else Decimal(hours),
            code=code,
            created_by=creator.id,
        )
        seeded_db.add(entry)
        await seeded_db.flush()
        return entry

    return _create

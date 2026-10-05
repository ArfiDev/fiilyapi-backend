"""Puantaj T2 — `personnel` uçlarının login/yetki fixture'ları.

`tests/customers/conftest.py` deseninin birebiri: kök `tests/conftest.py`de hazır
başlık fixture'ı YOKTUR, her modül kendi `_login`/`_auth` yardımcısını kurar.

İzin matrisi (`roles/seed_data.py` satır 172, `personnel`):
system_admin=_A · patron=_F · site_chief=**_V** · field_engineer=_V ·
hr_manager=_F · accounting=_F · project_manager=_V · procurement=**_N**.

Şantiye şefi BİLİNÇLİ OLARAK `view`'dir (spec §5): işçiyi İK ekler, şef yalnız
okur. `procurement` hiçbir uca giremez.

`personnel` ŞİRKET-GENELİDİR (spec §3) — `visible_projects` süzgeci UYGULANMAZ.
`kisitli_ik_headers` bu kararı SINAMAK için vardır: proje kapsamı daraltılmış bir
İK kullanıcısı bile TÜM personeli görür.
"""

from collections.abc import AsyncGenerator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sayfalar import HiddenCategory
from app.modules.contracts.models import Subcontractor
from app.modules.projects.models import Project
from app.modules.users.models import ProjectMember, User
from tests._hassas_alan import rol_gizli


async def _login(client: AsyncClient, user_factory, role_key: str, email: str) -> str:
    await user_factory(email=email, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def admin_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`system_admin` — `personnel=_A`."""
    token = await _login(client, user_factory, "system_admin", "admin@personnel.co")
    return _auth(token)


@pytest.fixture
async def ik_headers(client: AsyncClient, seeded_db: AsyncSession, user_factory) -> dict[str, str]:
    """`hr_manager` — `personnel=_F`: personel kartını İK açar (spec §5)."""
    token = await _login(client, user_factory, "hr_manager", "ik@personnel.co")
    return _auth(token)


@pytest.fixture
async def gizli_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`maas_kisisel` gizli özel rol (personel sayfası düzenler): ücret `null` döner.
    `hr_manager` artık ücreti GÖRÜR (IZN-B4c madde 20)."""
    await rol_gizli(seeded_db, "personel_gizli", {HiddenCategory.maas_kisisel})
    token = await _login(client, user_factory, "personel_gizli", "gizli@personnel.co")
    return _auth(token)


@pytest.fixture
async def sef_headers(client: AsyncClient, seeded_db: AsyncSession, user_factory) -> dict[str, str]:
    """`site_chief` — `personnel=_V`: okur ama YAZAMAZ (403, spec §5)."""
    token = await _login(client, user_factory, "site_chief", "sef@personnel.co")
    return _auth(token)


@pytest.fixture
async def yetkisiz_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`procurement` — `personnel=_N`: her uçta 403."""
    token = await _login(client, user_factory, "procurement", "satinalma@personnel.co")
    return _auth(token)


@pytest.fixture
async def gorunmeyen_proje(seeded_db: AsyncSession, project_factory) -> Project:
    """`kisitli_ik_headers` kullanıcısına ASLA görünürlük verilmeyen proje."""
    return await project_factory(code="PRS-001", name="Görünmeyen Proje")


@pytest.fixture
async def kisitli_ik_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    gorunmeyen_proje: Project,
) -> AsyncGenerator[dict[str, str], None]:
    """`hr_manager` (`personnel=_F`) ama `user_project_access` HİÇBİR projeyi kapsamaz.

    Proje süzgeci uygulansaydı bu kullanıcı boş liste görürdü — spec §3 gereği
    GÖRMEZ, tüm personeli görür.
    """
    email = "kisitli-ik@personnel.co"
    await user_factory(email=email, password="parola1234", role_key="hr_manager")
    user = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one()
    seeded_db.add(
        ProjectMember(user_id=user.id, project_id=gorunmeyen_proje.id, role_id=user.role_id)
    )
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    yield _auth(resp.json()["access_token"])


@pytest.fixture
async def taseron(seeded_db: AsyncSession) -> Subcontractor:
    sub = Subcontractor(name="Akın İnşaat")
    seeded_db.add(sub)
    await seeded_db.flush()
    return sub

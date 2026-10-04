"""Task C5 test yardımcıları — `contracts` modülüne özgü login/erişim fixture'ları.

Kök `tests/conftest.py`'de `admin_headers` / `site_chief_headers` gibi hazır
başlık fixture'ları YOKTUR (`tests/modules/test_boq_api.py` deseni doğrulandı):
her modül kendi `_login`/`_auth` yardımcılarını tanımlar. Burada da aynı desen
izlenir — fixture adları brief taslağındaki isimlerle eşleşir ama uydurulmaz,
gerçek login akışı üzerinden üretilir.
"""

from collections.abc import AsyncGenerator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.projects.models import Project
from app.modules.users.models import ProjectMember, User


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
    """`system_admin` — tüm projeleri görür (spec §5.2 admin istisnası)."""
    token = await _login(client, user_factory, "system_admin", "admin@contracts-list.co")
    return _auth(token)


@pytest.fixture
async def site_chief_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`site_chief` — matriste `contracts=_N` (spec §5): 403 beklenir."""
    token = await _login(client, user_factory, "site_chief", "sefi@contracts-list.co")
    return _auth(token)


@pytest.fixture
async def ornek_proje(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="CL-001", name="Örnek Proje")


@pytest.fixture
async def gorunmeyen_proje(seeded_db: AsyncSession, project_factory) -> Project:
    """`kisitli_headers` kullanıcısına ASLA görünürlük verilmeyen proje."""
    return await project_factory(code="CL-002", name="Görünmeyen Proje")


@pytest.fixture
async def kisitli_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    ornek_proje: Project,
    gorunmeyen_proje: Project,
) -> AsyncGenerator[dict[str, str], None]:
    """`project_manager` — `contracts=_F` (görür/yazar) ama `user_project_access`

    yalnız `ornek_proje`'yi kapsar; `gorunmeyen_proje` kapsam DIŞI bırakılır
    (spec §6 iki katman: izin=yetki, `user_project_access`=kapsam).
    """
    email = "kisitli@contracts-list.co"
    await user_factory(email=email, password="parola1234", role_key="project_manager")
    user = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one()
    seeded_db.add(ProjectMember(user_id=user.id, project_id=ornek_proje.id, role_id=user.role_id))
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    yield _auth(resp.json()["access_token"])


@pytest.fixture
async def project_manager(seeded_db: AsyncSession, user_factory) -> User:
    """`project_manager` — `contracts=_F` (task C12 silme testleri): tüm

    projelere erişimi vardır (`all_projects=True`), tek fixture birden çok
    test dosyasının farklı projelerinde kullanılabilsin diye — `kisitli_headers`
    (tek proje kapsamı) burada AMAÇ DIŞI, silme testleri kapsam değil YETKİ
    seviyesini sınar.
    """
    email = "pm@contracts-delete.co"
    user = await user_factory(email=email, password="parola1234", role_key="project_manager")
    user.all_projects = True
    await seeded_db.flush()
    return user


@pytest.fixture
async def project_manager_headers(
    client: AsyncClient, seeded_db: AsyncSession, project_manager: User
) -> dict[str, str]:
    resp = await client.post(
        "/auth/login", json={"email": project_manager.email, "password": "parola1234"}
    )
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])

"""ST T2 — katalog + depo uçlarının login/yetki/kapsam fixture'ları.

`tests/personnel/conftest.py` deseninin kardeşi: kök `tests/conftest.py`de hazır
başlık fixture'ı YOKTUR, her test paketi kendi `_login`/`_auth` yardımcısını kurar.

İzin matrisi (`roles/seed_data.py`, **`inventory`** — 9. modül, grup
STOK_SATINALMA; seed'de ZATEN VARDI, matris DEĞİŞMEDİ):
system_admin=**_A** · patron=_F · site_chief=_V · field_engineer=_V ·
hr_manager=**_N** · accounting=**_N** · project_manager=_V · procurement=**_F**.

Yani: stok kartını ve depoyu satınalma (ve patron) açar, saha/şef/PM SALT OKUR,
İK ile muhasebe hiçbir uca giremez. Silme (`admin`) yalnız system_admin'dedir —
yani depo silmeyi patron da satınalma da YAPAMAZ.

Fixture seçimi bu üç seviyeyi temsil eder:
* `admin_headers` — `system_admin` (`_A`): DELETE'i yalnız o geçer; `projects=_A`
  olduğu için `visible_projects` süzgecini de ATLAR (tüm projeleri görür).
* `satinalma_headers` — `procurement` (`_F`): yazar, SİLEMEZ. `projects=_N`
  olduğu için kapsamı `user_project_access`ten gelir — **IDOR testlerinin
  taşıyıcısı budur**.
* `sef_headers` — `site_chief` (`_V`): okur, YAZAMAZ.
* `yetkisiz_headers` — `accounting` (`_N`): okumada bile 403.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.inventory.models import StockCategory, StockItem, Warehouse
from app.modules.projects.models import Project
from app.modules.sites.models import Section, Site
from app.modules.users.models import ProjectMember, User


async def _login(client: AsyncClient, user_factory, role_key: str, email: str) -> str:
    await user_factory(email=email, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def gorunen_proje(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="ST-P01", name="Güneşkent Konut")


@pytest.fixture
async def gorunmeyen_proje(seeded_db: AsyncSession, project_factory) -> Project:
    """`satinalma_headers` kullanıcısına ASLA erişim verilmeyen proje."""
    return await project_factory(code="ST-P02", name="Marina Ofis")


@pytest.fixture
async def gorunen_santiye(seeded_db: AsyncSession, gorunen_proje: Project) -> Site:
    site = Site(project_id=gorunen_proje.id, code="ST-A", name="A-Blok Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def gorunmeyen_santiye(seeded_db: AsyncSession, gorunmeyen_proje: Project) -> Site:
    site = Site(project_id=gorunmeyen_proje.id, code="ST-B", name="B-Blok Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def admin_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`system_admin` — `inventory=_A`, `projects=_A` (kapsam süzgecini atlar)."""
    token = await _login(client, user_factory, "system_admin", "admin@stok.co")
    return _auth(token)


@pytest.fixture
async def satinalma_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    gorunen_proje: Project,
    gorunmeyen_proje: Project,
) -> dict[str, str]:
    """`procurement` — `inventory=_F`; kapsamı YALNIZ `gorunen_proje`dir.

    `projects=_N` olduğu için `visible_projects` `user_project_access`ten okur;
    `gorunmeyen_proje` bilinçli olarak verilmez.
    """
    email = "satinalma@stok.co"
    user = await user_factory(email=email, password="parola1234", role_key="procurement")
    seeded_db.add(ProjectMember(user_id=user.id, project_id=gorunen_proje.id, role_id=user.role_id))
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])


@pytest.fixture
async def sef_headers(client: AsyncClient, seeded_db: AsyncSession, user_factory) -> dict[str, str]:
    """`site_chief` — `inventory=_V`: okur ama YAZAMAZ (403)."""
    token = await _login(client, user_factory, "site_chief", "sef@stok.co")
    return _auth(token)


@pytest.fixture
async def yetkisiz_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`accounting` — `inventory=_N`: her uçta 403 (okuma dahil)."""
    token = await _login(client, user_factory, "accounting", "muhasebe@stok.co")
    return _auth(token)


@pytest.fixture
def kart_fabrikasi(seeded_db: AsyncSession):
    async def _create(
        code: str,
        name: str = "Nervürlü Demir Ø12",
        *,
        category: StockCategory = StockCategory.steel,
        unit: str = "Ton",
        min_stock: str | None = None,
        is_active: bool = True,
    ) -> StockItem:
        from decimal import Decimal

        item = StockItem(
            code=code,
            name=name,
            category=category,
            unit=unit,
            min_stock=None if min_stock is None else Decimal(min_stock),
            is_active=is_active,
        )
        seeded_db.add(item)
        await seeded_db.flush()
        return item

    return _create


@pytest.fixture
def depo_fabrikasi(seeded_db: AsyncSession):
    """`site` verilmezse MERKEZ depo açılır (`site_id IS NULL`, spec §2)."""

    async def _create(name: str, *, site: Site | None = None) -> Warehouse:
        warehouse = Warehouse(name=name, site_id=None if site is None else site.id)
        seeded_db.add(warehouse)
        await seeded_db.flush()
        return warehouse

    return _create


@pytest.fixture
async def kullanici_kimligi(seeded_db: AsyncSession):
    """Denetim testleri için: e-postadan kullanıcı kimliği çözer."""

    async def _resolve(email: str) -> uuid.UUID:
        return (await seeded_db.execute(select(User).where(User.email == email))).scalar_one().id

    return _resolve


@pytest.fixture
def bolum_fabrikasi(seeded_db: AsyncSession):
    """STOK-BOLUM: bir şantiyenin bölümü."""

    async def _create(site: Site, code: str, name: str) -> Section:
        section = Section(site_id=site.id, code=code, name=name)
        seeded_db.add(section)
        await seeded_db.flush()
        return section

    return _create


@pytest.fixture
def poz_fabrikasi(seeded_db: AsyncSession):
    """STOK-BOLUM: bir şantiyenin BOQ pozu (grup dahil, çünkü `group_id` NOT NULL)."""

    async def _create(site: Site, code: str, description: str = "Nervürlü demir") -> BoqItem:
        group = BoqGroup(site_id=site.id, name=f"Grup {code}")
        seeded_db.add(group)
        await seeded_db.flush()
        item = BoqItem(
            site_id=site.id,
            group_id=group.id,
            code=code,
            description=description,
            unit="Ton",
            quantity=Decimal("1000.000"),
            unit_price=Decimal("100.00"),
        )
        seeded_db.add(item)
        await seeded_db.flush()
        return item

    return _create

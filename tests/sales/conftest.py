"""P8 T3 — `unit_sales` uçlarının login/kapsam/veri fixture'ları.

`tests/contracts/conftest.py` ve `tests/customers/conftest.py` deseninin
birleşimi: kök `tests/conftest.py`de hazır başlık fixture'ı YOKTUR, her modül
kendi `_login`/`_auth` yardımcısını kurar.

`customers`ten AYRILAN nokta: satış kaydı PROJE BAĞLAMLIDIR (spec §6), bu yüzden
burada `user_project_access` kapsamı da kurulur — izin (`sales` matrisi) ile
kapsam (`visible_projects`) İKİ AYRI katmandır ve ikisi de test edilir.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.customers.models import Customer, CustomerType
from app.modules.projects.models import Project
from app.modules.sites.models import Site
from app.modules.units.models import Block, Unit, UnitKind, UnitOwnerSide
from app.modules.users.models import ProjectMember, User

PAROLA = "parola1234"


async def _login(client: AsyncClient, email: str) -> str:
    resp = await client.post("/auth/login", json={"email": email, "password": PAROLA})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _kullanici_basliklari(
    client: AsyncClient,
    session: AsyncSession,
    user_factory,
    role_key: str,
    email: str,
    *,
    project_ids: list[uuid.UUID] | None = None,
    all_projects: bool = False,
) -> dict[str, str]:
    """Rol + kapsam kurup giriş yapar.

    `system_admin` dışındaki roller için görünürlük `user_project_access`ten
    gelir (`projects=admin` süzgeci atlar, spec §5.2) — kapsam verilmezse
    kullanıcı HİÇBİR projeyi göremez.
    """
    await user_factory(email=email, password=PAROLA, role_key=role_key)
    user = (await session.execute(select(User).where(User.email == email))).scalar_one()
    if all_projects:
        user.all_projects = True
    for project_id in project_ids or []:
        session.add(ProjectMember(user_id=user.id, project_id=project_id, role_id=user.role_id))
    await session.flush()
    return _auth(await _login(client, email))


# --- Veri iskeleti ---


@pytest.fixture
async def proje(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="SL-001", name="Yeşilvadi Rezidans")


@pytest.fixture
async def baska_proje(seeded_db: AsyncSession, project_factory) -> Project:
    """Ünite sınırı testleri için AYRI proje (aynı aktör ikisini de görür)."""
    return await project_factory(code="SL-002", name="Bahçelievler Konut")


@pytest.fixture
async def blok(seeded_db: AsyncSession, proje: Project) -> Block:
    site = Site(project_id=proje.id, code="SANTIYE-1", name="Merkez")
    seeded_db.add(site)
    await seeded_db.flush()
    block = Block(project_id=proje.id, site_id=site.id, name="A Blok")
    seeded_db.add(block)
    await seeded_db.flush()
    return block


@pytest.fixture
async def unite(seeded_db: AsyncSession, proje: Project, blok: Block) -> Unit:
    """F84 "Liste Fiyatı" satış kaydında anlık görüntüye alınır."""
    unit = Unit(
        project_id=proje.id,
        block_id=blok.id,
        unit_no="12",
        unit_kind=UnitKind.apartment,
        # `Decimal` — ham `str` verilseydi ORM kimlik haritasi flush sonrasi
        # da metni tutar ve `units/summary._sum` Decimal + str ile patlardi.
        list_price=Decimal("1480000.00"),
    )
    seeded_db.add(unit)
    await seeded_db.flush()
    return unit


@pytest.fixture
async def ikinci_unite(seeded_db: AsyncSession, proje: Project, blok: Block) -> Unit:
    unit = Unit(project_id=proje.id, block_id=blok.id, unit_no="13", unit_kind=UnitKind.apartment)
    seeded_db.add(unit)
    await seeded_db.flush()
    return unit


@pytest.fixture
async def arsa_sahibi_unitesi(seeded_db: AsyncSession, proje: Project, blok: Block) -> Unit:
    """`owner_side='landowner'` — satışa KAPALI (spec §8 S3, kullanıcı kararı)."""
    unit = Unit(
        project_id=proje.id,
        block_id=blok.id,
        unit_no="14",
        unit_kind=UnitKind.apartment,
        owner_side=UnitOwnerSide.landowner,
    )
    seeded_db.add(unit)
    await seeded_db.flush()
    return unit


@pytest.fixture
async def yabanci_unite(seeded_db: AsyncSession, baska_proje: Project) -> Unit:
    """BAŞKA projenin ünitesi — `POST /projects/{proje}/sales` için 404."""
    site = Site(project_id=baska_proje.id, code="SANTIYE-9", name="Uzak")
    seeded_db.add(site)
    await seeded_db.flush()
    block = Block(project_id=baska_proje.id, site_id=site.id, name="Z Blok")
    seeded_db.add(block)
    await seeded_db.flush()
    unit = Unit(
        project_id=baska_proje.id, block_id=block.id, unit_no="1", unit_kind=UnitKind.apartment
    )
    seeded_db.add(unit)
    await seeded_db.flush()
    return unit


@pytest.fixture
async def musteri(seeded_db: AsyncSession) -> Customer:
    customer = Customer(
        customer_type=CustomerType.person, name="Mehmet Aydın", national_id="12345678901"
    )
    seeded_db.add(customer)
    await seeded_db.flush()
    return customer


# --- Yetki / kapsam başlıkları ---


@pytest.fixture
async def admin_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`system_admin` — `sales=_A`; `projects=admin` görünürlük süzgecini atlar."""
    return await _kullanici_basliklari(
        client, seeded_db, user_factory, "system_admin", "admin@sales.co"
    )


@pytest.fixture
async def full_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`project_manager` — `sales=_F`: yazar ama SİLEMEZ (silme yalnız admin)."""
    return await _kullanici_basliklari(
        client, seeded_db, user_factory, "project_manager", "pm@sales.co", all_projects=True
    )


@pytest.fixture
async def view_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`accounting` — `sales=(view, finance)`: okur ama YAZAMAZ (403)."""
    return await _kullanici_basliklari(
        client, seeded_db, user_factory, "accounting", "muhasebe@sales.co", all_projects=True
    )


@pytest.fixture
async def yetkisiz_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`site_chief` — `sales=_N`: her uçta 403 (kapsamı olsa bile)."""
    return await _kullanici_basliklari(
        client, seeded_db, user_factory, "site_chief", "sefi@sales.co", all_projects=True
    )


@pytest.fixture
async def kapsam_disi_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, baska_proje: Project
) -> dict[str, str]:
    """`project_manager` — `sales=_F` (yetki VAR) ama kapsamı yalnız `baska_proje`.

    `proje` üzerindeki her uç **404** dönmeli (403 DEĞİL): varlık sızdırılmaz.
    """
    return await _kullanici_basliklari(
        client,
        seeded_db,
        user_factory,
        "project_manager",
        "kapsamdisi@sales.co",
        project_ids=[baska_proje.id],
    )

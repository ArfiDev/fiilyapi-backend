"""MK-1 T3 — ekipman uçlarının login/yetki/kapsam fixture'ları.

`tests/modules/inventory/conftest.py` deseninin kardeşi: kök `tests/conftest.py`de
hazır başlık fixture'ı YOKTUR, her test paketi kendi `_login`/`_auth` yardımcısını
kurar.

İzin matrisi (`roles/seed_data.py`, **`equipment`** — 21. modül, grup SAHA):
system_admin=**_A** · patron=_F · site_chief=**_F** · field_engineer=**_V** ·
hr_manager=**_N** · accounting=_F · project_manager=_F · procurement=_V.

Fixture seçimi bu dört seviyeyi temsil eder:
* `admin_headers` — `system_admin` (`_A`); `projects=_A` olduğu için
  `visible_projects` süzgecini de ATLAR (tüm projeleri görür).
* `sef_headers` — `site_chief` (`_F`): yazar. `projects=_LIM` olduğu için kapsamı
  `user_project_access`ten gelir — **K20 testlerinin taşıyıcısı budur**.
* `muhendis_headers` — `field_engineer` (`_V`): okur, YAZAMAZ (POST/PATCH 403).
* `yetkisiz_headers` — `hr_manager` (`_N`): okumada bile 403.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.accounting.models import ChartAccount
from app.modules.equipment.models import Equipment, EquipmentCategory, EquipmentStatus
from app.modules.projects.models import Project
from app.modules.sites.models import Site
from app.modules.users.models import ProjectMember


async def _login(client: AsyncClient, user_factory, role_key: str, email: str) -> str:
    await user_factory(email=email, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def gorunen_proje(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="MK-P01", name="Güneşkent Konut")


@pytest.fixture
async def gorunmeyen_proje(seeded_db: AsyncSession, project_factory) -> Project:
    """`sef_headers` kullanıcısına ASLA erişim verilmeyen proje."""
    return await project_factory(code="MK-P02", name="Marina Ofis")


@pytest.fixture
async def gorunen_santiye(seeded_db: AsyncSession, gorunen_proje: Project) -> Site:
    site = Site(project_id=gorunen_proje.id, code="MK-A", name="A-Blok Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def gorunmeyen_santiye(seeded_db: AsyncSession, gorunmeyen_proje: Project) -> Site:
    site = Site(project_id=gorunmeyen_proje.id, code="MK-B", name="B-Blok Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def admin_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`system_admin` — `equipment=_A`, `projects=_A` (kapsam süzgecini atlar)."""
    token = await _login(client, user_factory, "system_admin", "admin@makine.co")
    return _auth(token)


@pytest.fixture
async def sef_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    gorunen_proje: Project,
    gorunmeyen_proje: Project,
) -> dict[str, str]:
    """`site_chief` — `equipment=_F`; kapsamı YALNIZ `gorunen_proje`dir.

    `projects=_LIM` (admin DEĞİL) olduğu için `visible_projects`
    `user_project_access`ten okur; `gorunmeyen_proje` bilinçli olarak verilmez.
    """
    email = "sef@makine.co"
    user = await user_factory(email=email, password="parola1234", role_key="site_chief")
    seeded_db.add(ProjectMember(user_id=user.id, project_id=gorunen_proje.id, role_id=user.role_id))
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])


@pytest.fixture
async def muhendis_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    gorunen_proje: Project,
) -> dict[str, str]:
    """`field_engineer` — `equipment=_V`: okur ama YAZAMAZ (403)."""
    email = "muhendis@makine.co"
    user = await user_factory(email=email, password="parola1234", role_key="field_engineer")
    seeded_db.add(ProjectMember(user_id=user.id, project_id=gorunen_proje.id, role_id=user.role_id))
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])


@pytest.fixture
async def yetkisiz_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`hr_manager` — `equipment=_N`: her uçta 403 (okuma dahil)."""
    token = await _login(client, user_factory, "hr_manager", "ik@makine.co")
    return _auth(token)


@pytest.fixture
def ekipman_fabrikasi(seeded_db: AsyncSession):
    """`site` verilmezse DEPODAKİ ekipman açılır (`site_id IS NULL`, K4)."""

    async def _create(
        name: str,
        *,
        site: Site | None = None,
        category: EquipmentCategory = EquipmentCategory.machinery,
        status: EquipmentStatus = EquipmentStatus.working,
        **kwargs,
    ) -> Equipment:
        equipment = Equipment(
            name=name,
            category=category,
            status=status,
            site_id=None if site is None else site.id,
            **kwargs,
        )
        seeded_db.add(equipment)
        await seeded_db.flush()
        return equipment

    return _create


@pytest.fixture
async def kira_eslemesi(seeded_db: AsyncSession) -> dict[str, ChartAccount]:
    """🔴 MU-3D — kira hakedişi ailesinin `posting_rules` ÜRÜN eşlemesi.

    Canlıda bu satırları `a4b5c6d7e8f9` migration'ı tohumlar; test kümesi
    migration koşmaz (`Base.metadata.create_all`), bu yüzden faturayı
    `approved`a taşıyan HER test onu kurmak zorundadır. Eksik olduğunda
    `/approve` **422** verir ve onay HİÇ GERÇEKLEŞMEZ — fail-closed olan ve
    olması gereken taraf budur (`test_mu3d_hakedis_fisleme.py` o dalı BİLEREK
    bu fixture'sız ölçer).

    🔴 Hesabın TÜRÜ elle yazılmaz, TDHP tohumundan (`chart_seed_data`) okunur:
    elle yazılsaydı `740`ı `revenue` sayan bir kurulum `balance.SIGN`ın
    işaretini sessizce ters çevirir ve mutabakat testi yanlış bir büyüklükle
    tutardı.

    Eşleme `rental_posting.RENTAL_POSTING_RULES`ten kurulur: testte elle
    yazılsaydı üründeki demet bozulduğunda bu kurulum yeşil kalırdı.
    """
    from app.modules.accounting.chart_seed_data import CHART_ACCOUNTS
    from app.modules.accounting.models import JournalSourceType
    from app.modules.equipment.rental_posting import RENTAL_POSTING_RULES
    from app.modules.posting.models import PostingRule

    tohum = {satir.code: satir for satir in CHART_ACCOUNTS}
    hesaplar: dict[str, ChartAccount] = {}
    for _role_key, kod in RENTAL_POSTING_RULES:
        if kod in hesaplar:
            continue
        kart = tohum[kod]
        hesap = ChartAccount(
            code=kart.code,
            name=kart.name,
            account_type=kart.account_type,
            is_contra=kart.is_contra,
        )
        seeded_db.add(hesap)
        await seeded_db.flush()
        hesaplar[kod] = hesap
    for role_key, kod in RENTAL_POSTING_RULES:
        seeded_db.add(
            PostingRule(
                source_type=JournalSourceType.equipment_rental_invoice,
                role_key=role_key,
                account_id=hesaplar[kod].id,
            )
        )
    await seeded_db.flush()
    return hesaplar

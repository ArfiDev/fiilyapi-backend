"""H1 + H4 fixture'ları: sözleşme/şantiye kurulumu, login/erişim başlıkları ve
P7'ye özgü veri kurulumu (`tests/progress_payments/conftest.py` bölmesi).

🔴 Bu modül `conftest.py`ye AÇIKÇA import edilir; fixture görünürlüğü ve kapsamı
bölme öncesiyle AYNIDIR.
"""

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.progress_payments.models import (
    ProgressPayment,
    ProgressPaymentStatus,
)
from app.modules.projects.models import Project, ProjectContract
from app.modules.sites.models import Site
from app.modules.users.models import ProjectMember, User


async def _login(client: AsyncClient, user_factory, role_key: str, email: str) -> str:
    await user_factory(email=email, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- H1 fixture'ları (mevcut, model/migration testleri) ---


@pytest.fixture
async def hakedis_sozlesmesi(
    seeded_db: AsyncSession, project_factory
) -> tuple[Project, ProjectContract]:
    """OLU 92 deseni: sözleşmeli proje — `test_employer_items.py::_contract` varsayılanlarıyla."""
    project = await project_factory(code="PP-001", name="Hakedişli Proje")
    contract = ProjectContract(
        project_id=project.id,
        contract_no="SZL-2026-010",
        amount=Decimal("11200000"),
        advance_pct=Decimal("20"),
        retainage_pct=Decimal("5"),
        vat_pct=Decimal("20"),
    )
    seeded_db.add(contract)
    await seeded_db.flush()
    return project, contract


@pytest.fixture
async def hakedis_santiyesi(seeded_db: AsyncSession, hakedis_sozlesmesi) -> Site:
    project, _ = hakedis_sozlesmesi
    site = Site(project_id=project.id, code="SNT-2026-001", name="Test Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def hakedis_olusturan(seeded_db: AsyncSession, user_factory) -> User:
    return await user_factory(
        email="olusturan@progress-payments.co", password="parola1234", role_key="system_admin"
    )


# --- H4 fixture'ları: login/erişim (`tests/contracts/conftest.py` deseni) ---


@pytest.fixture
async def admin_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`system_admin` — `projects=_A` admin istisnası sayesinde tüm projeleri görür."""
    token = await _login(client, user_factory, "system_admin", "admin@pp-crud.co")
    return _auth(token)


@pytest.fixture
async def hr_headers(client: AsyncClient, seeded_db: AsyncSession, user_factory) -> dict[str, str]:
    """`hr_manager` — matriste `progress_payments=_N`: 403 beklenir (kapı, görünürlükten ÖNCE)."""
    token = await _login(client, user_factory, "hr_manager", "ik@pp-crud.co")
    return _auth(token)


@pytest.fixture
async def kisitli_proje(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="PP-K01", name="Kısıtlı Erişim Projesi")


@pytest.fixture
async def gorunmeyen_proje(seeded_db: AsyncSession, project_factory) -> uuid.UUID:
    """`kisitli_headers`/`site_chief_headers` kullanıcılarına ASLA görünürlük verilmeyen proje."""
    project = await project_factory(code="PP-K02", name="Görünmeyen Proje")
    return project.id


@pytest.fixture
async def kisitli_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    kisitli_proje: Project,
    gorunmeyen_proje: uuid.UUID,
) -> AsyncGenerator[dict[str, str], None]:
    """`project_manager` (`progress_payments=_APR`) ama `user_project_access`

    yalnız `kisitli_proje`'yi kapsar; `gorunmeyen_proje` kapsam DIŞI (spec §9.0
    iki katman: izin=yetki, `user_project_access`=kapsam).
    """
    email = "kisitli@pp-crud.co"
    await user_factory(email=email, password="parola1234", role_key="project_manager")
    user = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one()
    seeded_db.add(ProjectMember(user_id=user.id, project_id=kisitli_proje.id, role_id=user.role_id))
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    yield _auth(resp.json()["access_token"])


@pytest.fixture
async def site_chief_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    kisitli_proje: Project,
    gorunmeyen_proje: uuid.UUID,
) -> AsyncGenerator[dict[str, str], None]:
    """`site_chief` (`progress_payments=_DRF`, draft/scope=project) — yalnız

    `kisitli_proje`'ye atanmış; `gorunmeyen_proje`de oluşturma denemesi 404
    dönmelidir (403 DEĞİL — spec §9.0, varlık sızdırmaz).
    """
    email = "sefi@pp-crud.co"
    await user_factory(email=email, password="parola1234", role_key="site_chief")
    user = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one()
    seeded_db.add(ProjectMember(user_id=user.id, project_id=kisitli_proje.id, role_id=user.role_id))
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    yield _auth(resp.json()["access_token"])


# --- H4 fixture'ları: P7'ye özgü veri kurulumu ---


@pytest.fixture
async def sozlesmeli_proje(hakedis_sozlesmesi: tuple[Project, ProjectContract]) -> uuid.UUID:
    project, _ = hakedis_sozlesmesi
    return project.id


@pytest.fixture
async def sozlesmesiz_proje(seeded_db: AsyncSession, project_factory) -> uuid.UUID:
    """Sözleşme kaydı YOK — `NO_EMPLOYER_CONTRACT` 422 testinde kullanılır."""
    project = await project_factory(code="PP-005", name="Sözleşmesiz Proje")
    return project.id


@pytest.fixture
async def taslak_hakedisli_proje(
    seeded_db: AsyncSession,
    hakedis_sozlesmesi: tuple[Project, ProjectContract],
    hakedis_olusturan: User,
) -> uuid.UUID:
    """Zaten AÇIK (draft) bir hakedişi olan proje — D8/409 testinde kullanılır."""
    project, contract = hakedis_sozlesmesi
    payment = ProgressPayment(
        project_id=project.id,
        sequence_no=1,
        status=ProgressPaymentStatus.draft,
        vat_pct=contract.vat_pct,
        advance_pct=contract.advance_pct,
        retainage_pct=contract.retainage_pct,
        created_by=hakedis_olusturan.id,
    )
    seeded_db.add(payment)
    await seeded_db.flush()
    return project.id


@pytest.fixture
async def onay_bekleyen_hakedis(
    seeded_db: AsyncSession,
    hakedis_sozlesmesi: tuple[Project, ProjectContract],
    hakedis_olusturan: User,
) -> uuid.UUID:
    """`pending_approval` durumunda — `PATCH` 409 testinde kullanılır."""
    project, contract = hakedis_sozlesmesi
    payment = ProgressPayment(
        project_id=project.id,
        sequence_no=1,
        status=ProgressPaymentStatus.pending_approval,
        vat_pct=contract.vat_pct,
        advance_pct=contract.advance_pct,
        retainage_pct=contract.retainage_pct,
        created_by=hakedis_olusturan.id,
    )
    seeded_db.add(payment)
    await seeded_db.flush()
    return payment.id


@pytest.fixture
async def gorunmeyen_hakedis(
    seeded_db: AsyncSession, project_factory, hakedis_olusturan: User
) -> uuid.UUID:
    """Kendi başına bir proje+sözleşme+taslak hakediş — `kisitli_headers`/

    `site_chief_headers` kullanıcılarına HİÇBİR ZAMAN görünmeyen bir projededir.
    """
    project = await project_factory(code="PP-006", name="Görünmeyen Hakedişli Proje")
    contract = ProjectContract(
        project_id=project.id,
        contract_no="SZL-2026-011",
        amount=Decimal("1000000"),
        advance_pct=Decimal("10"),
        retainage_pct=Decimal("5"),
        vat_pct=Decimal("20"),
    )
    seeded_db.add(contract)
    await seeded_db.flush()
    payment = ProgressPayment(
        project_id=project.id,
        sequence_no=1,
        status=ProgressPaymentStatus.draft,
        vat_pct=contract.vat_pct,
        advance_pct=contract.advance_pct,
        retainage_pct=contract.retainage_pct,
        created_by=hakedis_olusturan.id,
    )
    seeded_db.add(payment)
    await seeded_db.flush()
    return payment.id


@pytest.fixture
async def ikinci_sozlesmeli_proje(
    seeded_db: AsyncSession, project_factory
) -> tuple[Project, ProjectContract]:
    """Y1 (H4 denetimi): `sozlesmeli_proje`'den TAMAMEN ayrı bir ikinci proje —
    çapraz-proje `contract_item_id`/`site_id` IDOR testlerinde "B projesi" olarak
    kullanılır. `admin_headers` (system_admin) HER İKİ projeyi de görür; testin
    amacı yetki DEĞİL, `service._build_lines`'taki sahiplik korkuluğudur."""
    project = await project_factory(code="PP-002", name="İkinci Proje")
    contract = ProjectContract(
        project_id=project.id,
        contract_no="SZL-2026-020",
        amount=Decimal("5000000"),
        advance_pct=Decimal("10"),
        retainage_pct=Decimal("5"),
        vat_pct=Decimal("20"),
    )
    seeded_db.add(contract)
    await seeded_db.flush()
    return project, contract


@pytest.fixture
async def ikinci_proje_santiyesi(
    seeded_db: AsyncSession, ikinci_sozlesmeli_proje: tuple[Project, ProjectContract]
) -> Site:
    project, _ = ikinci_sozlesmeli_proje
    site = Site(project_id=project.id, code="SNT-2026-002", name="İkinci Proje Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def ikinci_proje_kalemi(
    seeded_db: AsyncSession, ikinci_sozlesmeli_proje: tuple[Project, ProjectContract]
) -> EmployerContractItem:
    project, _ = ikinci_sozlesmeli_proje
    group = EmployerContractGroup(project_id=project.id, name="İkinci Proje Grubu", sort_order=1)
    seeded_db.add(group)
    await seeded_db.flush()
    item = EmployerContractItem(
        project_id=project.id,
        group_id=group.id,
        code="03.002",
        description="İkinci proje kalemi",
        unit="m³",
        quantity=Decimal("500"),
        unit_price=Decimal("2000"),
        sort_order=1,
    )
    seeded_db.add(item)
    await seeded_db.flush()
    return item


async def _dagit(
    seeded_db: AsyncSession, site: Site, item: EmployerContractItem, quota: Decimal
) -> BoqItem:
    """POZ dağıtımının test karşılığı: (kalem, şantiye) çiftine kota satırı açar.

    H5'in `ITEM_NOT_DISTRIBUTED`/`QUANTITY_EXCEEDS_QUOTA` korkulukları (spec §6.5/1-2)
    tam olarak bu satırın VARLIĞINA ve `quantity`sine bakar.
    """
    group = (
        (await seeded_db.execute(select(BoqGroup).where(BoqGroup.site_id == site.id)))
        .scalars()
        .first()
    )
    if group is None:
        group = BoqGroup(site_id=site.id, name="Dağıtım Grubu", sort_order=1)
        seeded_db.add(group)
        await seeded_db.flush()
    boq = BoqItem(
        site_id=site.id,
        group_id=group.id,
        code=item.code,
        description=item.description,
        unit=item.unit,
        quantity=quota,
        unit_price=item.unit_price,
        contract_item_id=item.id,
    )
    seeded_db.add(boq)
    await seeded_db.flush()
    return boq


@pytest.fixture
async def hakedis_kalemi(
    seeded_db: AsyncSession,
    hakedis_sozlesmesi: tuple[Project, ProjectContract],
    hakedis_santiyesi: Site,
) -> tuple[EmployerContractItem, str]:
    """OLU 114/116/119/100 satırı: `03.001` betonarme kalemi — snapshot testinde

    kullanılır. H5'ten itibaren dağıtım ön şartı (§6.5/1) HER yazımda koşar
    (POST'un iç içe `lines[]`'ı dahil, tek yol `lines.py`), bu yüzden kalem
    `hakedis_santiyesi`'ne 1.000 birim kotayla DAĞITILMIŞ olarak kurulur —
    dağıtılmamış hâli için ayrı `dagitilmamis_kalem` fixture'ı vardır.
    """
    project, _ = hakedis_sozlesmesi
    group = EmployerContractGroup(project_id=project.id, name="Betonarme İşleri", sort_order=1)
    seeded_db.add(group)
    await seeded_db.flush()
    item = EmployerContractItem(
        project_id=project.id,
        group_id=group.id,
        code="03.001",
        description="Beton C30/37 dökümü",
        unit="m³",
        quantity=Decimal("1000"),
        unit_price=Decimal("1850"),
        sort_order=1,
    )
    seeded_db.add(item)
    await seeded_db.flush()
    await _dagit(seeded_db, hakedis_santiyesi, item, Decimal("1000"))
    return item, group.name

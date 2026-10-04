"""Taşeron hakedişi (T2) fixture'ları — bağımsız kurulum.

Kök `tests/conftest.py`'deki `db_session`/`seeded_db`/`user_factory`/`project_factory`
üzerine kurulur. `tests/progress_payments/conftest.py` KARDEŞ dizindir ve pytest onu
otomatik YÜKLEMEZ; login/erişim deseni (aynı izin modülü: `progress_payments`) burada
yeniden kurulur. `tests/progress_payments/test_concurrency.py`'nin bilinen seed sızıntısı
borcuna bulaşmamak için bu paketin fixture'ları hiçbir şeyi paylaşmaz — her fixture
kendi verisini kurar.
"""

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.contracts.models import (
    EmployerContractGroup,
    EmployerContractItem,
    SubcontractorContract,
    SubcontractorContractItem,
)
from app.modules.projects.models import Project, ProjectContract
from app.modules.sites.models import Section, Site
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
)
from app.modules.users.models import ProjectMember, User

GRUP_ADI = "A — Betonarme İşleri"


async def _login(client: AsyncClient, user_factory, role_key: str, email: str) -> str:
    await user_factory(email=email, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- Erişim/kapsam fixture'ları (spec §9.0 iki katman: izin + `visible_projects`) ---


@pytest.fixture
async def admin_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    token = await _login(client, user_factory, "system_admin", "admin@thk-crud.co")
    return _auth(token)


@pytest.fixture
async def hr_headers(client: AsyncClient, seeded_db: AsyncSession, user_factory) -> dict[str, str]:
    """`hr_manager` — matriste `progress_payments=_N`: 403 (kapı, görünürlükten ÖNCE)."""
    token = await _login(client, user_factory, "hr_manager", "ik@thk-crud.co")
    return _auth(token)


@pytest.fixture
async def kisitli_proje(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="THK-K01", name="Kısıtlı Erişim Projesi")


@pytest.fixture
async def kisitli_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    kisitli_proje: Project,
) -> AsyncGenerator[dict[str, str], None]:
    """`project_manager` (`progress_payments=_APR`) ama kapsamı yalnız `kisitli_proje`."""
    email = "kisitli@thk-crud.co"
    await user_factory(email=email, password="parola1234", role_key="project_manager")
    user = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one()
    seeded_db.add(ProjectMember(user_id=user.id, project_id=kisitli_proje.id, role_id=user.role_id))
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    yield _auth(resp.json()["access_token"])


@pytest.fixture
async def sef_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    kisitli_proje: Project,
) -> AsyncGenerator[dict[str, str], None]:
    """`site_chief` (`progress_payments=_DRF`) — yalnız `kisitli_proje`'ye atanmış."""
    email = "sef@thk-crud.co"
    await user_factory(email=email, password="parola1234", role_key="site_chief")
    user = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one()
    seeded_db.add(ProjectMember(user_id=user.id, project_id=kisitli_proje.id, role_id=user.role_id))
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    yield _auth(resp.json()["access_token"])


@pytest.fixture
async def sef_kullanicisi(seeded_db: AsyncSession, sef_headers: dict[str, str]) -> User:
    return (
        await seeded_db.execute(select(User).where(User.email == "sef@thk-crud.co"))
    ).scalar_one()


# --- Veri kurulumu ---


@pytest.fixture
async def sozlesme_sahibi(seeded_db: AsyncSession, user_factory) -> User:
    """`subcontractor_contracts.created_by` NOT NULL — fixture'ların kurucu kullanıcısı."""
    return await user_factory(
        email="kurucu@thk-crud.co", password="parola1234", role_key="system_admin"
    )


@pytest.fixture
def taseron_sozlesmesi_fabrikasi(seeded_db: AsyncSession, project_factory, sozlesme_sahibi: User):
    """Proje + işveren poz grubu/kalemi + taşeron sözleşmesi (kalemleriyle) kurar.

    Kalemler işveren kalemine `source_contract_item_id` ile BAĞLIDIR: grup adı
    snapshot'ının (`employer_contract_groups.name`) çözülebilmesi için şart.
    """

    async def _create(
        code: str,
        *,
        project: Project | None = None,
        subcontractor_name: str = "Yıldız İnşaat Ltd.",
        unit_prices: list[Decimal | None] | None = None,
        vat_pct: Decimal = Decimal("20"),
        with_site: bool = True,
    ) -> tuple[SubcontractorContract, Project, Site | None]:
        if project is None:
            project = await project_factory(code=code, name=f"{code} Projesi")
        # `employer_contract_groups.project_id` -> `project_contracts` FK'si:
        # poz grubu ancak isveren sozlesmesi olan projede acilir.
        mevcut = await seeded_db.get(ProjectContract, project.id)
        if mevcut is None:
            seeded_db.add(
                ProjectContract(
                    project_id=project.id,
                    contract_no=f"{code}-SZL",
                    amount=Decimal("11200000"),
                )
            )
            await seeded_db.flush()
        group = EmployerContractGroup(project_id=project.id, name=GRUP_ADI, sort_order=0)
        seeded_db.add(group)
        await seeded_db.flush()

        site: Site | None = None
        if with_site:
            site = Site(project_id=project.id, code=f"{code}-SNT", name="Test Şantiyesi")
            seeded_db.add(site)
            await seeded_db.flush()

        contract = SubcontractorContract(
            project_id=project.id,
            site_id=site.id if site is not None else None,
            subcontractor_name=subcontractor_name,
            contract_no=f"{code}-TSZ",
            advance_pct=Decimal("10"),
            retainage_pct=Decimal("5"),
            vat_pct=vat_pct,
            created_by=sozlesme_sahibi.id,
        )
        seeded_db.add(contract)
        await seeded_db.flush()

        prices = unit_prices if unit_prices is not None else [Decimal("21500"), Decimal("1850")]
        for index, price in enumerate(prices):
            employer_item = EmployerContractItem(
                project_id=project.id,
                group_id=group.id,
                # Kod PROJE ici tekildir: ayni projede ikinci sozlesme kurulabilsin diye
                # sozlesme etiketi kodun parcasidir.
                code=f"{code}.{index + 1:03d}",
                description=f"Kalem {index + 1}",
                unit="Ton",
                quantity=Decimal("200"),
                unit_price=Decimal("25000"),
            )
            seeded_db.add(employer_item)
            await seeded_db.flush()
            seeded_db.add(
                SubcontractorContractItem(
                    contract_id=contract.id,
                    source_contract_item_id=employer_item.id,
                    code=employer_item.code,
                    description=employer_item.description,
                    unit=employer_item.unit,
                    quantity=Decimal("200"),
                    unit_price=price,
                    sort_order=index,
                )
            )
        await seeded_db.flush()
        await seeded_db.refresh(contract)
        return contract, project, site

    return _create


@pytest.fixture
def ikiz_sozlesme_fabrikasi(seeded_db: AsyncSession, sozlesme_sahibi: User):
    """AYNI projede, AYNI işveren kalemine (`source_contract_item_id`) köprü kuran
    İKİNCİ taşeron sözleşmesi — ÇİFT SAYIM (TH-PRJGENEL) kurulumu.

    `subcontractor_contract_items`te UNIQUE yalnız `(contract_id, code)`tur;
    aynı kaynak kalemi iki sözleşmenin göstermesi DB'de serbesttir.
    """

    async def _create(
        contract: SubcontractorContract,
        kaynak_kalem_id: uuid.UUID,
        *,
        code: str = "THK-IKIZ",
        quantity: Decimal = Decimal("200"),
    ) -> SubcontractorContract:
        ikiz = SubcontractorContract(
            project_id=contract.project_id,
            site_id=contract.site_id,
            subcontractor_name="İkiz Taşeron Ltd.",
            contract_no=f"{code}-TSZ",
            advance_pct=Decimal("10"),
            retainage_pct=Decimal("5"),
            vat_pct=Decimal("20"),
            created_by=sozlesme_sahibi.id,
        )
        seeded_db.add(ikiz)
        await seeded_db.flush()
        seeded_db.add(
            SubcontractorContractItem(
                contract_id=ikiz.id,
                source_contract_item_id=kaynak_kalem_id,
                code=f"{code}.001",
                description="İkiz kalem",
                unit="Ton",
                quantity=quantity,
                unit_price=Decimal("21500"),
                sort_order=0,
            )
        )
        await seeded_db.flush()
        await seeded_db.refresh(ikiz)
        return ikiz

    return _create


@pytest.fixture
async def taseron_sozlesmesi(
    taseron_sozlesmesi_fabrikasi,
) -> tuple[SubcontractorContract, Project, Site | None]:
    return await taseron_sozlesmesi_fabrikasi("THK-001")


@pytest.fixture
async def fiyatsiz_sozlesme(
    taseron_sozlesmesi_fabrikasi,
) -> SubcontractorContract:
    """Bir kalemin `unit_price`ı NULL — "girilmedi ≠ 0 TL" guard'ı (spec §2)."""
    contract, _, _ = await taseron_sozlesmesi_fabrikasi(
        "THK-002", unit_prices=[Decimal("21500"), None]
    )
    return contract


@pytest.fixture
async def bolum(seeded_db: AsyncSession, taseron_sozlesmesi) -> Section:
    _, _, site = taseron_sozlesmesi
    section = Section(site_id=site.id, code="B-1", name="A Blok")
    seeded_db.add(section)
    await seeded_db.flush()
    return section


@pytest.fixture
async def yabanci_bolum(seeded_db: AsyncSession, taseron_sozlesmesi_fabrikasi) -> Section:
    """Başka projenin şantiyesindeki bölüm — 422 testinde kullanılır."""
    _, _, site = await taseron_sozlesmesi_fabrikasi("THK-003")
    section = Section(site_id=site.id, code="B-9", name="Yabancı Blok")
    seeded_db.add(section)
    await seeded_db.flush()
    return section


@pytest.fixture
def hakedis_fabrikasi(seeded_db: AsyncSession):
    """Doğrudan DB'ye hakediş yazar — durum geçişi uçları T4'te (burada YOK)."""

    async def _create(
        contract: SubcontractorContract,
        creator: User,
        *,
        sequence_no: int = 1,
        status: SubcontractorPaymentStatus = SubcontractorPaymentStatus.draft,
        period_year: int | None = None,
        period_month: int | None = None,
    ) -> SubcontractorProgressPayment:
        payment = SubcontractorProgressPayment(
            contract_id=contract.id,
            project_id=contract.project_id,
            sequence_no=sequence_no,
            status=status,
            period_year=period_year,
            period_month=period_month,
            vat_pct=contract.vat_pct,
            advance_pct=contract.advance_pct,
            retainage_pct=contract.retainage_pct,
            created_by=creator.id,
        )
        seeded_db.add(payment)
        await seeded_db.flush()
        return payment

    return _create


@pytest.fixture
async def admin_kullanicisi(seeded_db: AsyncSession, admin_headers: dict[str, str]) -> User:
    return (
        await seeded_db.execute(select(User).where(User.email == "admin@thk-crud.co"))
    ).scalar_one()


@pytest.fixture
async def gorunmeyen_hakedis(
    taseron_sozlesmesi_fabrikasi, hakedis_fabrikasi, admin_kullanicisi: User
) -> uuid.UUID:
    """`kisitli_headers`/`sef_headers` kullanıcılarının kapsamı DIŞINDA bir hakediş."""
    contract, _, _ = await taseron_sozlesmesi_fabrikasi("THK-G01")
    payment = await hakedis_fabrikasi(contract, admin_kullanicisi)
    return payment.id


@pytest.fixture
async def gorunmeyen_sozlesme(taseron_sozlesmesi_fabrikasi) -> uuid.UUID:
    contract, _, _ = await taseron_sozlesmesi_fabrikasi("THK-G02")
    return contract.id


@pytest.fixture(autouse=True)
async def _mu3d_esleme(seeded_db: AsyncSession) -> None:
    """🔴 MU-3D — taşeron hakedişi `posting_rules` eşlemesi, **AUTOUSE**.

    Gerekçe kardeş pakettedir (`tests/progress_payments/conftest.py::
    _mu3d_esleme`): fişleme bu paketin ölçtüğü kural DEĞİL, bir altyapı ön
    koşuludur; fail-closed dalı `tests/modules/posting/` altında, autouse'un
    ulaşmadığı yerde ayrıca ölçülür.

    """
    from app.modules.accounting.models import JournalSourceType
    from app.modules.subcontractor_progress_payments.posting import (
        SUBCONTRACTOR_POSTING_RULES,
    )
    from tests._hakedis_esleme import esleme_kur

    await esleme_kur(
        seeded_db,
        JournalSourceType.subcontractor_progress_payment,
        SUBCONTRACTOR_POSTING_RULES,
    )

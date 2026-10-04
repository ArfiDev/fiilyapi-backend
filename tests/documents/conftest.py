"""Belge çekirdeği (T1) fixture'ları — bağımsız kurulum.

`tests/site_planning/conftest.py` deseninin kardeşi: kök `tests/conftest.py`in
`db_session`/`seeded_db`/`user_factory`/`project_factory` fixture'ları üzerine
kurulur, kardeş test paketlerinden HİÇBİR ŞEY miras alınmaz (pytest onları
yüklemez) ve `tests/progress_payments/test_concurrency.py`nin bilinen seed
sızıntısı borcuna BULAŞILMAZ.

İzin matrisi (`roles/seed_data.py`, **`documents`** — 20. modül, grup MALI;
belge çekirdeği spec §6 / §7 S2): system_admin=_A · patron=_F · site_chief=_F ·
field_engineer=_F · hr_manager=_V · accounting=_F · project_manager=_V ·
procurement=_V.

Yani: şef ve saha mühendisi belge yükleyebilir (arşivi sahada onlar besler),
muhasebe de tam yetkilidir (fatura/sözleşme eki); İK, proje müdürü ve satınalma
SALT OKUR. Silme yalnız system_admin'dedir (`_A`).

T1 testleri doğrudan DB katmanına bakar; T2 ile birlikte KLASÖR uçlarının HTTP
fixture'ları (kimlik + kapsam) eklendi. Yetki fixture'ları seçilirken üç seviyeyi
de temsil eden roller alındı:

* `admin_headers` — `system_admin` (`_A`): DELETE'i yalnız o geçer, tüm projeleri görür.
* `sef_headers` — `site_chief` (`_F`), kapsamı TEK projeye kısıtlı: yazar, SİLEMEZ.
* `pm_headers` — `project_manager` (`_V`), aynı kapsam: okur, YAZAMAZ.

`documents` satırında hiçbir rol `_N` DEĞİLDİR (spec §6 gerekçesi: arşiv ortak
hafızadır), bu yüzden "okumada 403" senaryosu YOKTUR — izin ayrımı ancak
`full` (POST/PATCH) ve `admin` (DELETE) kapılarında sınanabilir.
"""

import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.contracts.models import SubcontractorContract
from app.modules.customers.models import Customer, CustomerType
from app.modules.documents import link_owners
from app.modules.documents.models import (
    Document,
    DocumentBlob,
    DocumentFolder,
    EntityDocumentScope,
    EntityDocumentType,
)
from app.modules.projects.models import Project
from app.modules.sales.models import SaleType, UnitSale, UnitSaleStatus
from app.modules.sites.models import Section, Site
from app.modules.units.models import Block, Unit, UnitKind
from app.modules.users.models import ProjectMember, User

# Beyaz listeye göre tipik bir künye (spec §4): PDF, 48 MB'ın çok altında.
ORNEK_MIME = "application/pdf"


@pytest.fixture
async def proje(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="BC-P01", name="Güneşkent Konut")


@pytest.fixture
async def ikinci_proje(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="BC-P02", name="Marina Ofis")


@pytest.fixture
async def santiye(seeded_db: AsyncSession, proje: Project) -> Site:
    site = Site(project_id=proje.id, code="BC-A", name="A-Blok Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
def klasor_fabrikasi(seeded_db: AsyncSession):
    """`site` verilmezse PROJE DÜZEYİ klasör açılır (`site_id IS NULL`, spec §2)."""

    async def _create(
        project: Project,
        name: str,
        *,
        site: Site | None = None,
        parent: DocumentFolder | None = None,
    ) -> DocumentFolder:
        folder = DocumentFolder(
            project_id=project.id,
            site_id=site.id if site is not None else None,
            parent_id=parent.id if parent is not None else None,
            name=name,
        )
        seeded_db.add(folder)
        await seeded_db.flush()
        return folder

    return _create


@pytest.fixture
def belge_fabrikasi(seeded_db: AsyncSession):
    """Künye + (istenirse) ikili içerik. Blob AYRI tabloya yazılır (spec §2)."""

    async def _create(
        project: Project,
        filename: str,
        *,
        site: Site | None = None,
        folder: DocumentFolder | None = None,
        description: str | None = None,
        uploaded_by_name: str | None = None,
        size_bytes: int = 1024,
        mime_type: str = ORNEK_MIME,
        data: bytes | None = None,
    ) -> Document:
        document = Document(
            project_id=project.id,
            site_id=site.id if site is not None else None,
            folder_id=folder.id if folder is not None else None,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            description=description,
            uploaded_by_name=uploaded_by_name,
        )
        seeded_db.add(document)
        await seeded_db.flush()
        if data is not None:
            seeded_db.add(DocumentBlob(document_id=document.id, data=data))
            await seeded_db.flush()
        return document

    return _create


# --- Kapsam dışı kayıtlar (IDOR yüzeyi) ---


@pytest.fixture
async def gorunmeyen_santiye(seeded_db: AsyncSession, ikinci_proje: Project) -> Site:
    """Kapsamı kısıtlı kullanıcıların ASLA göremediği projenin şantiyesi.

    İki işi birden görür: (a) görünmeyen kapsamda gerçek bir kayıt üretir, (b)
    `site_id` başka projeye aitken 422 dönen kural için hedef sağlar.
    """
    site = Site(project_id=ikinci_proje.id, code="BC-G", name="Görünmeyen Şantiye")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


# --- Kimlik / yetki ---


async def _login(client: AsyncClient, user_factory, role_key: str, email: str) -> dict[str, str]:
    await user_factory(email=email, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _scoped_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    role_key: str,
    email: str,
    project: Project,
) -> dict[str, str]:
    """Rolü verilen ama kapsamı TEK projeye kısıtlanmış kullanıcı (IDOR yüzeyi)."""
    headers = await _login(client, user_factory, role_key, email)
    user = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one()
    seeded_db.add(ProjectMember(user_id=user.id, project_id=project.id, role_id=user.role_id))
    await seeded_db.flush()
    return headers


@pytest.fixture
async def admin_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`system_admin` (`documents=_A`) — silme kapısını yalnız bu rol geçer."""
    return await _login(client, user_factory, "system_admin", "admin@bc-t2.co")


@pytest.fixture
async def sef_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`site_chief` (`documents=_F`), kapsamı `proje` ile SINIRLI — yazar, silemez."""
    return await _scoped_headers(
        client, seeded_db, user_factory, "site_chief", "sef@bc-t2.co", proje
    )


@pytest.fixture
async def pm_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`project_manager` (`documents=_V`), aynı kapsam — okur, YAZAMAZ."""
    return await _scoped_headers(
        client, seeded_db, user_factory, "project_manager", "pm@bc-t2.co", proje
    )


# ===========================================================================
# BC-3 — belge ↔ varlık bağı fixture'ları
# ===========================================================================
#
# Slot kataloğu `create_all` ile GELMEZ (seed migration'dadır); testler seed'i
# migration dosyasının KENDİSİNDEN okur (`SLOT_SEED`), ikinci bir liste
# yazılmaz — böylece migration ile test aynı 18 satırı görür, ayrışamaz.
#
# Sahip kayıtları (`bolum` · `unite` · `satis` · `taseron_sozlesmesi`) `proje`de,
# `gorunmeyen_*` karşılıkları `ikinci_proje`de açılır (IDOR yüzeyi).
#
# Başlıklar: `admin_headers` (system_admin) · `pm_headers` (project_manager,
# kapsamı `proje` ile SINIRLI; dört sahip modülünde de `full`) ·
# `muhasebe_headers` (accounting; dört sahip modülünde de `view`, yazamaz).

BC3_MIGRATION_PATH = (
    Path(__file__).parents[2] / "alembic" / "versions" / "c5d8e2f1a4b7_bc3_belge_varlik_bagi.py"
)


def load_bc3_migration():
    """Migration modülünü DOSYA YOLUNDAN yükler (paket değildir)."""
    spec = importlib.util.spec_from_file_location("bc3_migration", BC3_MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SahipDurumu(NamedTuple):
    """Parametrik sahip: spec + görünen kayıt + görünmeyen (ikinci proje) kayıt."""

    spec: link_owners.OwnerSpec
    owner_id: uuid.UUID
    yabanci_owner_id: uuid.UUID


@pytest.fixture
async def slot_katalogu(seeded_db: AsyncSession) -> dict[tuple[str, str], EntityDocumentType]:
    """18 slot, migration'ın `SLOT_SEED`inden birebir. Anahtar `(scope, code)`."""
    migration = load_bc3_migration()
    rows: dict[tuple[str, str], EntityDocumentType] = {}
    for scope, code, name, is_required, sort_order in migration.SLOT_SEED:
        row = EntityDocumentType(
            scope=EntityDocumentScope(scope),
            code=code,
            name=name,
            is_required=is_required,
            sort_order=sort_order,
        )
        seeded_db.add(row)
        rows[(scope, code)] = row
    await seeded_db.flush()
    return rows


@pytest.fixture
async def kayit_sahibi(user_factory) -> User:
    """`created_by` isteyen sahipler (satış, taşeron sözleşmesi) için kullanıcı."""
    return await user_factory(email="sahip@bc3.co", password="parola1234", role_key="patron")


# --- görünen sahipler (proje) ---


@pytest.fixture
async def bolum(seeded_db: AsyncSession, santiye: Site) -> Section:
    section = Section(site_id=santiye.id, name="Kaba İnşaat")
    seeded_db.add(section)
    await seeded_db.flush()
    return section


@pytest.fixture
async def unite(seeded_db: AsyncSession, proje: Project, santiye: Site) -> Unit:
    block = Block(project_id=proje.id, site_id=santiye.id, name="A Blok")
    seeded_db.add(block)
    await seeded_db.flush()
    unit = Unit(project_id=proje.id, block_id=block.id, unit_no="12", unit_kind=UnitKind.apartment)
    seeded_db.add(unit)
    await seeded_db.flush()
    return unit


@pytest.fixture
async def satis(
    seeded_db: AsyncSession, proje: Project, unite: Unit, kayit_sahibi: User
) -> UnitSale:
    customer = Customer(customer_type=CustomerType.person, name="Mehmet Aydın")
    seeded_db.add(customer)
    await seeded_db.flush()
    sale = UnitSale(
        unit_id=unite.id,
        project_id=proje.id,
        customer_id=customer.id,
        sale_type=SaleType.sale,
        status=UnitSaleStatus.active,
        sale_price=Decimal("1480000.00"),
        created_by=kayit_sahibi.id,
    )
    seeded_db.add(sale)
    await seeded_db.flush()
    return sale


@pytest.fixture
async def taseron_sozlesmesi(
    seeded_db: AsyncSession, proje: Project, kayit_sahibi: User
) -> SubcontractorContract:
    contract = SubcontractorContract(
        project_id=proje.id, contract_no="TS-2026-001", created_by=kayit_sahibi.id
    )
    seeded_db.add(contract)
    await seeded_db.flush()
    return contract


# --- görünmeyen sahipler (ikinci proje) ---


@pytest.fixture
async def gorunmeyen_bolum(seeded_db: AsyncSession, gorunmeyen_santiye: Site) -> Section:
    section = Section(site_id=gorunmeyen_santiye.id, name="Görünmeyen Bölüm")
    seeded_db.add(section)
    await seeded_db.flush()
    return section


@pytest.fixture
async def gorunmeyen_unite(
    seeded_db: AsyncSession, ikinci_proje: Project, gorunmeyen_santiye: Site
) -> Unit:
    block = Block(project_id=ikinci_proje.id, site_id=gorunmeyen_santiye.id, name="Z Blok")
    seeded_db.add(block)
    await seeded_db.flush()
    unit = Unit(
        project_id=ikinci_proje.id, block_id=block.id, unit_no="1", unit_kind=UnitKind.apartment
    )
    seeded_db.add(unit)
    await seeded_db.flush()
    return unit


@pytest.fixture
async def gorunmeyen_satis(
    seeded_db: AsyncSession, ikinci_proje: Project, gorunmeyen_unite: Unit, kayit_sahibi: User
) -> UnitSale:
    customer = Customer(customer_type=CustomerType.company, name="Uzak Ltd.")
    seeded_db.add(customer)
    await seeded_db.flush()
    sale = UnitSale(
        unit_id=gorunmeyen_unite.id,
        project_id=ikinci_proje.id,
        customer_id=customer.id,
        sale_type=SaleType.sale,
        status=UnitSaleStatus.active,
        sale_price=Decimal("990000.00"),
        created_by=kayit_sahibi.id,
    )
    seeded_db.add(sale)
    await seeded_db.flush()
    return sale


@pytest.fixture
async def gorunmeyen_taseron_sozlesmesi(
    seeded_db: AsyncSession, ikinci_proje: Project, kayit_sahibi: User
) -> SubcontractorContract:
    contract = SubcontractorContract(project_id=ikinci_proje.id, created_by=kayit_sahibi.id)
    seeded_db.add(contract)
    await seeded_db.flush()
    return contract


_SAHIP_FIXTURE = {
    "section": ("bolum", "gorunmeyen_bolum", link_owners.SECTION),
    "unit": ("unite", "gorunmeyen_unite", link_owners.UNIT),
    "unit_sale": ("satis", "gorunmeyen_satis", link_owners.UNIT_SALE),
    "subcontractor_contract": (
        "taseron_sozlesmesi",
        "gorunmeyen_taseron_sozlesmesi",
        link_owners.SUBCONTRACTOR_CONTRACT,
    ),
}


@pytest.fixture(params=tuple(_SAHIP_FIXTURE))
def sahip(request) -> SahipDurumu:
    """Dört sahibi TEK test gövdesinden geçirir — dört kopya test yazılmaz.

    SENKRON fixture'dır (bilinçli): `request.getfixturevalue` ile ASYNC bir
    fixture'ı çözmek yalnız senkron bağlamdan çalışır; async fixture içinden
    çağrılınca pytest-asyncio setup'ta patlar (ölçüldü: 48 ERROR).
    """
    gorunen_ad, gorunmeyen_ad, spec = _SAHIP_FIXTURE[request.param]
    gorunen = request.getfixturevalue(gorunen_ad)
    gorunmeyen = request.getfixturevalue(gorunmeyen_ad)
    return SahipDurumu(spec=spec, owner_id=gorunen.id, yabanci_owner_id=gorunmeyen.id)


#: Sahip modulunde `none` (_N) seviyesindeki rol — KARSIT KANIT icin.
#: Olculdu (`roles/seed_data.py`, rol sirasi: system_admin · patron · site_chief ·
#: field_engineer · hr_manager · accounting · project_manager · procurement):
#:   projects  = [_A,_F,_LIM,_LIM,_LIM,_FIN,_F,_N ] -> procurement NONE
#:   sales     = [_A,_F,_N,  _N,  _N,  _FIN,_F,_N ] -> procurement NONE
#:   contracts = [_A,_F,_N,  _N,  _N,  _FIN,_F,_N ] -> procurement NONE
#: 🔴 `sites` = [_A,_F,_LIM,_LIM,_LIM,_FIN,_F,_LIM] -> HICBIR rol NONE DEGIL.
#: Yani BOLUM ucu icin "yetkisiz rol" senaryosu YAPISAL OLARAK KURULAMAZ; bu bir
#: eksiklik degil olculmus bir olgudur ve testte adiyla iddia edilir.
NONE_ROLU = {
    "section": None,
    "unit": "procurement",
    "unit_sale": "procurement",
    "subcontractor_contract": "procurement",
}


@pytest.fixture
async def yetkisiz_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`procurement` — `projects`/`sales`/`contracts`te NONE, `sites`te view."""
    return await _scoped_headers(
        client, seeded_db, user_factory, "procurement", "satinalma@bc3.co", proje
    )


@pytest.fixture
async def muhasebe_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`accounting` — dört sahip modülünde de `view` (_FIN): okur, YAZAMAZ.

    Kapsamı `proje` ile verilir: erişim satırı olmayan kullanıcı HİÇBİR projeyi
    görmez ve 403 yerine 404 alırdı (ölçüldü) — yetki testi kapsam testine dönüşürdü.
    """
    return await _scoped_headers(
        client, seeded_db, user_factory, "accounting", "muhasebe@bc3.co", proje
    )

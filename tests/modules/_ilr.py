"""ILR-1/2 ilerleme bekcilerinin PAYLASILAN kurulumu.

`test_ilr_ilerleme.py` 800 satir tavanini asmasin diye ayrildi (`_boq.py` /
`_projects_cost_bindings.py` emsali): yardimcilar KOPYALANMAZ, tek govdeden
cagirilir — iki kopya olsaydi biri guncellenip oteki kalir ve AYNI ismi tasiyan
FARKLI kurulumlar kosardi.

🔑 Kurulumun sekli (bekcilerin hepsi bunun uzerine biner):

    Project ─ ProjectContract ─ EmployerContractGroup ─ EmployerContractItem
        └─ Site ─ BoqGroup ─ BoqItem ─┬─ BoqItemSectionAllocation ─ Section
                                      └─ SiteDiaryLine ─ SiteDiaryEntry
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqGroup, BoqItem, BoqItemSectionAllocation
from app.modules.contracts.models import (
    EmployerContractGroup,
    EmployerContractItem,
    SubcontractorContract,
    SubcontractorContractItem,
)
from app.modules.progress_payments.models import (
    ProgressPayment,
    ProgressPaymentLine,
    ProgressPaymentStatus,
)
from app.modules.projects.models import Project, ProjectContract
from app.modules.site_diary.models import DiaryStatus, SiteDiaryEntry, SiteDiaryLine
from app.modules.sites.models import Section, Site
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
    SubcontractorProgressPaymentLine,
)
from app.modules.users.models import User

VARSAYILAN_TARIH = date(2026, 8, 3)


async def login(client, session: AsyncSession, user_factory, role_key: str, email: str) -> dict:
    """Rolu verilen + TUM projelere gorunurlugu olan kullanicinin baslik sozlugu."""
    user = await user_factory(email=email, password="parola1234", role_key=role_key)
    user.all_projects = True
    await session.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def aktor(seeded_db: AsyncSession, user_factory, email: str) -> User:
    """Gunluk/hakedis kaydinin `created_by`si (NOT NULL) — uctan gecmez, dogrudan yazilir."""
    return await user_factory(email=email, password="parola1234", role_key="system_admin")


async def santiye(session: AsyncSession, project: Project, code: str = "A-BLOK") -> Site:
    site = Site(project_id=project.id, code=code, name=f"{code} Santiyesi")
    session.add(site)
    await session.flush()
    return site


async def bolum(session: AsyncSession, site: Site, ad: str = "Kenar Ayak") -> Section:
    section = Section(site_id=site.id, name=ad)
    session.add(section)
    await session.flush()
    return section


async def grup(session: AsyncSession, site: Site, ad: str = "TOPRAK VE TEMEL") -> BoqGroup:
    boq_grup = BoqGroup(site_id=site.id, name=ad)
    session.add(boq_grup)
    await session.flush()
    return boq_grup


async def poz(
    session: AsyncSession,
    site: Site,
    boq_grup: BoqGroup,
    code: str,
    *,
    quantity: str,
    unit_price: str,
    unit: str = "adet",
    description: str = "Poz",
) -> BoqItem:
    item = BoqItem(
        site_id=site.id,
        group_id=boq_grup.id,
        code=code,
        description=description,
        unit=unit,
        quantity=Decimal(quantity),
        unit_price=Decimal(unit_price),
    )
    session.add(item)
    await session.flush()
    return item


async def tahsis(
    session: AsyncSession, item: BoqItem, section: Section, quantity: str
) -> BoqItemSectionAllocation:
    """Pozun BOLUME tahsis edilen miktari — bolum yuzdesinin PAYDASI."""
    row = BoqItemSectionAllocation(
        boq_item_id=item.id, section_id=section.id, quantity=Decimal(quantity)
    )
    session.add(row)
    await session.flush()
    return row


async def gunluk(
    session: AsyncSession,
    site: Site,
    yazan: User,
    satirlar: list[tuple[BoqItem | None, str]],
    *,
    tarih: date = VARSAYILAN_TARIH,
    status: DiaryStatus = DiaryStatus.submitted,
    section: Section | None = None,
) -> SiteDiaryEntry:
    """Gunluk kayit + poz satirlari. `(None, "12")` = bagi KOPMUS satir.

    UQ (site_id, entry_date) yuzunden ayni santiyede her cagri FARKLI gun ister;
    cagiran `tarih`i acikca verir.
    """
    entry = SiteDiaryEntry(
        site_id=site.id,
        project_id=site.project_id,
        entry_date=tarih,
        section_id=None if section is None else section.id,
        status=status,
        created_by=yazan.id,
    )
    session.add(entry)
    await session.flush()
    for item, miktar in satirlar:
        session.add(
            SiteDiaryLine(
                entry_id=entry.id,
                boq_item_id=None if item is None else item.id,
                code="—" if item is None else item.code,
                description="—" if item is None else item.description,
                unit="adet" if item is None else item.unit,
                # 🔴 Gunluk satirinin KENDI fiyat anlik goruntusu: yuzde bunu
                # KULLANMAZ (canli `BoqItem.unit_price` kullanilir). Kasten
                # SAPTIRILIR ki snapshot'a kayan bir kod bekcilerde kirilsin.
                unit_price=Decimal("1.00"),
                quantity=Decimal(miktar),
            )
        )
    await session.flush()
    return entry


def sonraki_gun(tarih: date, gun: int) -> date:
    return tarih + timedelta(days=gun)


async def isveren_kalemi(
    session: AsyncSession,
    project: Project,
    *,
    quantity: str,
    unit_price: str,
    code: str = "15.150.1002",
) -> EmployerContractItem:
    """Isveren sozlesmesi kalemi — MALI yuzdenin PAYDASI. `ProjectContract` de
    acilir: `progress_payments.project_id` FK'si ona bakar."""
    session.add(ProjectContract(project_id=project.id, amount=Decimal("100000000.00")))
    # 🔴 ONCE flush: `employer_contract_groups.project_id` FK'si `project_contracts`e
    # bakar (projeye DEGIL) — sozlesme kaydi yazilmadan grup acilamaz.
    await session.flush()
    sozlesme_grup = EmployerContractGroup(project_id=project.id, name="TOPRAK")
    session.add(sozlesme_grup)
    await session.flush()
    kalem = EmployerContractItem(
        project_id=project.id,
        group_id=sozlesme_grup.id,
        code=code,
        description="Beton",
        unit="m3",
        quantity=Decimal(quantity),
        unit_price=Decimal(unit_price),
    )
    session.add(kalem)
    await session.flush()
    return kalem


async def isveren_hakedisi(
    session: AsyncSession,
    project: Project,
    yazan: User,
    kalem: EmployerContractItem,
    site: Site,
    *,
    quantity: str,
    status: ProgressPaymentStatus,
    sequence_no: int = 1,
) -> ProgressPayment:
    hakedis = ProgressPayment(
        project_id=project.id,
        sequence_no=sequence_no,
        status=status,
        vat_pct=Decimal("20.00"),
        advance_pct=Decimal("0.00"),
        retainage_pct=Decimal("0.00"),
        created_by=yazan.id,
    )
    session.add(hakedis)
    await session.flush()
    session.add(
        ProgressPaymentLine(
            payment_id=hakedis.id,
            contract_item_id=kalem.id,
            site_id=site.id,
            code=kalem.code,
            description=kalem.description,
            unit=kalem.unit,
            contract_unit_price=kalem.unit_price,
            quantity=Decimal(quantity),
        )
    )
    await session.flush()
    return hakedis


async def taseron_hakedisi(
    session: AsyncSession,
    project: Project,
    yazan: User,
    *,
    quantity: str,
    unit_price: str = "96250.00",
    status: SubcontractorPaymentStatus = SubcontractorPaymentStatus.approved,
) -> SubcontractorProgressPayment:
    """TASERON hakedisi — MALIYET tarafi. Ilerlemeye GIRMEMELIDIR."""
    sozlesme = SubcontractorContract(
        project_id=project.id, subcontractor_name="Demir Ltd.", created_by=yazan.id
    )
    session.add(sozlesme)
    await session.flush()
    session.add(
        SubcontractorContractItem(
            contract_id=sozlesme.id,
            code="A.001",
            description="Demir",
            unit="ton",
            quantity=Decimal("98"),
            unit_price=Decimal(unit_price),
        )
    )
    hakedis = SubcontractorProgressPayment(
        contract_id=sozlesme.id,
        project_id=project.id,
        sequence_no=1,
        status=status,
        vat_pct=Decimal("20"),
        advance_pct=Decimal("0"),
        retainage_pct=Decimal("0"),
        created_by=yazan.id,
        lines=[
            SubcontractorProgressPaymentLine(
                code="A.001",
                description="Demir",
                unit="ton",
                contract_unit_price=Decimal(unit_price),
                coefficient=Decimal("1.000"),
                quantity=Decimal(quantity),
            )
        ],
    )
    session.add(hakedis)
    await session.flush()
    return hakedis


def zarf(node: dict) -> tuple[bool, str | None]:
    """Zarfin OLCULEN ikilisi: `(available, pending_module)`."""
    return (node["available"], node["pending_module"])


def kart(body: dict, project_id: uuid.UUID, alan: str) -> dict:
    item = next(row for row in body["items"] if row["id"] == str(project_id))
    return item["contracting"][alan]

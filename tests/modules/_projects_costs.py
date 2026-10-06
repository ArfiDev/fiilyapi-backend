"""Proje maliyet ucu (`GET /projects/{id}/costs`) testlerinin PAYLAŞILAN kurulumu.

`test_projects_costs_api.py` 800 satır tavanını aşınca bölündü (`_journal.py`
emsali): yardımcılar KOPYALANMADI, buraya alındı — iki kopya olsaydı biri
guncellenip öteki kalır ve iki dosya AYNI ismi taşıyan FARKLI gövdelerle koşardı.

Hiçbir testin iddiası bu bölmeyle değişmedi.
"""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.contracts.models import SubcontractorContract, SubcontractorContractItem
from app.modules.customers.models import Customer, CustomerType
from app.modules.projects.models import Project
from app.modules.sales.models import SaleType, UnitSale, UnitSaleStatus
from app.modules.sites.models import Site
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
    SubcontractorProgressPaymentLine,
)
from app.modules.units.models import Block, Unit, UnitKind, UnitSalesStatus
from app.modules.users.models import ProjectMember, User

_TENTH = Decimal("0.1")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, user_factory, role_key: str, *, email: str | None = None) -> str:
    address = email or f"{role_key}@p10.co"
    await user_factory(email=address, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


async def _scoped_login(client, db_session, user_factory, project: Project | None) -> str:
    """Kapsamlı kullanıcı: yalnız verilen projeye erişir (IDOR kapısı testi)."""
    user = await user_factory(email="kapsamli@p10.co", password="parola1234", role_key="patron")
    if project is not None:
        db_session.add(ProjectMember(user_id=user.id, project_id=project.id, role_id=user.role_id))
    await db_session.flush()
    resp = await client.post(
        "/auth/login", json={"email": "kapsamli@p10.co", "password": "parola1234"}
    )
    return resp.json()["access_token"]


def _set_budget_lines(project: Project, *, material="0", labor="0", sub="0", overhead="0") -> None:
    project.budget_material = Decimal(material)
    project.budget_labor = Decimal(labor)
    project.budget_subcontractor = Decimal(sub)
    project.budget_overhead = Decimal(overhead)


async def _units(session: AsyncSession, project: Project, specs: list[dict]) -> list[Unit]:
    """Blok + üniteler. Ünite `block_id` ZORUNLUDUR, blok da şantiyeye bağlıdır."""
    site = Site(project_id=project.id, code=f"SNT-{project.code}", name="Şantiye")
    session.add(site)
    await session.flush()
    block = Block(project_id=project.id, site_id=site.id, name="A Blok")
    session.add(block)
    await session.flush()
    created: list[Unit] = []
    for index, spec in enumerate(specs, start=1):
        unit = Unit(
            project_id=project.id,
            block_id=block.id,
            unit_no=str(index),
            unit_kind=UnitKind.apartment,
            list_price=spec.get("list_price"),
            appraisal_value=spec.get("appraisal_value"),
            gross_area_m2=spec.get("gross_area_m2"),
            owner_side=spec.get("owner_side"),
        )
        session.add(unit)
        created.append(unit)
    await session.flush()
    return created


async def _customer(session: AsyncSession, name: str = "Mehmet Aydın") -> Customer:
    customer = Customer(customer_type=CustomerType.person, name=name)
    session.add(customer)
    await session.flush()
    return customer


async def _sale(
    session: AsyncSession,
    unit: Unit,
    customer: Customer,
    creator: User,
    status: UnitSaleStatus,
    *,
    price: str,
) -> UnitSale:
    """Satış kaydı + ünitenin satış durumu.

    `sales_status` gerçek yolda servis senkronize eder (P8 T3); burada ORM ile
    yazıldığı için elle kurulur — "kalan stok" ölçütü bu kolondan okur.
    """
    sale = UnitSale(
        unit_id=unit.id,
        project_id=unit.project_id,
        customer_id=customer.id,
        sale_type=SaleType.sale,
        status=status,
        sale_price=Decimal(price),
        created_by=creator.id,
    )
    session.add(sale)
    if status in (UnitSaleStatus.active, UnitSaleStatus.deed_transferred):
        unit.sales_status = UnitSalesStatus.sold
    elif status is UnitSaleStatus.reservation:
        unit.sales_status = UnitSalesStatus.reserved
    await session.flush()
    return sale


async def _contract(
    session: AsyncSession,
    project: Project,
    creator: User,
    *,
    name: str,
    work_category: str | None = None,
    item_quantity: str = "1",
    item_price: str | None = "0",
    contract_no: str | None = None,
    with_item: bool = True,
) -> SubcontractorContract:
    """`with_item=False`: KALEMSİZ sözleşme — üretimde ERİŞİLEBİLİR bir durumdur
    (`SubcontractorContractCreate.items` `default_factory=list`, yani her sözleşme
    kalemleri girilmeden ÖNCE bu hâlden geçer) ve bedeli `0.00` olur."""
    contract = SubcontractorContract(
        project_id=project.id,
        subcontractor_name=name,
        work_category=work_category,
        contract_no=contract_no,
        created_by=creator.id,
    )
    session.add(contract)
    await session.flush()
    if not with_item:
        return contract
    session.add(
        SubcontractorContractItem(
            contract_id=contract.id,
            code="A.001",
            description="Kalem",
            unit="m2",
            quantity=Decimal(item_quantity),
            unit_price=None if item_price is None else Decimal(item_price),
        )
    )
    await session.flush()
    return contract


async def _payment(
    session: AsyncSession,
    contract: SubcontractorContract,
    creator: User,
    status: SubcontractorPaymentStatus,
    *,
    quantity: str,
    sequence_no: int = 1,
    rejected: bool = False,
) -> SubcontractorProgressPayment:
    payment = SubcontractorProgressPayment(
        contract_id=contract.id,
        project_id=contract.project_id,
        sequence_no=sequence_no,
        status=status,
        vat_pct=Decimal("20"),
        advance_pct=Decimal("10"),
        retainage_pct=Decimal("5"),
        created_by=creator.id,
        lines=[
            SubcontractorProgressPaymentLine(
                code="A.001",
                description="Kalem",
                unit="m2",
                contract_unit_price=Decimal("1000"),
                coefficient=Decimal("1.000"),
                quantity=Decimal(quantity),
            )
        ],
    )
    if rejected:
        # "Revize Gerekli" BEŞİNCİ bir durum değildir: `draft` + `rejected_at`.
        payment.rejected_at = payment.created_at
    session.add(payment)
    await session.flush()
    return payment

"""P8 T4 — `POST /sales/installments/{id}/pay` eşzamanlılık kilidi.

`pay_installment` önce okuyup sonra yazar: "tahsilat ≤ taksit tutarı"
doğrulaması ile yazma arasındaki pencerede ikinci bir istek AYNI `paid_amount`ı
okursa ikisi de geçerli sanılır, ikisi de yazar ve toplam tahsilat taksit
tutarını AŞAR (TOCTOU). Kilit bu pencereyi kapatır: taksit satırı
`SELECT … FOR UPDATE` ile kilitlenmeden doğrulamayı besleyen okuma YAPILMAZ.

Neden `client`/`seeded_db` KULLANILMAZ (`tests/contracts/test_distribution_concurrency.py`
ile AYNI gerekçe): kök `tests/conftest.py`deki `db_session` her testi TEK bağlantı
üzerinde SAVEPOINT'e sarar ve dış transaction'ı asla COMMIT ETMEZ — o session
üzerinde iki `asyncio` görevi de AYNI bağlantıyı paylaşır, gerçek satır kilidi
test EDİLEMEZ. Bu dosya bilerek İKİ BAĞIMSIZ bağlantı açar, kurulumu GERÇEKTEN
commit eder ve sonunda GERÇEKTEN temizler.

`seed_reference_data` BİLİNÇLİ OLARAK ÇAĞRILMAZ (aynı dosyadaki ders): commit
edilen referans veri paketin geri kalanına sızar ve `roles` anahtarlarında
`UniqueViolationError` üretir. Yalnız bu testin ihtiyacı kurulur.
"""

import asyncio
import contextlib
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import SiteValidationError
from app.core.security import hash_password
from app.modules.customers.models import Customer, CustomerType
from app.modules.projects.models import Project
from app.modules.roles.models import Role
from app.modules.sales import installments
from app.modules.sales.models import SaleInstallment, SaleType, UnitSale, UnitSaleStatus
from app.modules.sales.schemas import (
    InstallmentPayInput,
    SaleInstallmentInput,
    SaleInstallmentsSave,
)
from app.modules.sites.models import Site
from app.modules.units.models import Block, Unit, UnitKind
from app.modules.users.models import ProjectMember, User
from tests._yaris import YARIS_TAVANI_SN, kilitte_bekleyen_sorgu
from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio

_SessionFactory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

# Taksit 100.000; her istek 60.000 öder — TEK BAŞINA geçerli, İKİSİ BİRDEN aşım
# (120.000 > 100.000). Yarışın tüm kanıtı bu iki sayıdadır.
_AMOUNT = Decimal("100000.00")
_PAYMENT = Decimal("60000.00")

_ROLE_KEY = "p8t4_tahsilat_yarisi"


class _Kurulum:
    def __init__(self, user_id: uuid.UUID, sale_id: uuid.UUID, installment_id: uuid.UUID) -> None:
        self.user_id = user_id
        self.sale_id = sale_id
        self.installment_id = installment_id


@pytest.fixture
async def kurulum():
    veri = await _kur()
    try:
        yield veri
    finally:
        await _temizle(veri)


async def test_esZamanli_tahsilat_taksit_tutarini_asamaz(kurulum: _Kurulum) -> None:
    """KIRMIZI kanıtı (kilit yokken): tx2, tx1 yazarken beklemez; ikisi de
    "tahsil edilen 0" görür, ikisi de 60.000 yazar → 120.000 > 100.000.

    Kilit varken tx2 `SELECT … FOR UPDATE`'te BLOKE olur (`not task2.done()`),
    tx1 commit edince kilidi alır, artık tx1'in 60.000'ini GÖRÜR ve 422 alır.
    """
    lock_acquired = asyncio.Event()
    release_lock = asyncio.Event()

    task1 = asyncio.create_task(_ode_ve_tut(kurulum, lock_acquired, release_lock))
    task2: asyncio.Task[str] | None = None
    try:
        await asyncio.wait_for(lock_acquired.wait(), timeout=YARIS_TAVANI_SN)

        task2 = asyncio.create_task(_ode(kurulum))
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine,
            task2,
            mesaj="tx2, tx1 kilidi serbest bırakmadan ilerleyebildi — `pay_installment` "
            "taksit satırını `SELECT … FOR UPDATE` ile KİLİTLEMİYOR olabilir",
        )
        # `pg_stat_activity.query` 1024 baytta KIRPILIR (`track_activity_query_size`): uzun
        # SELECT'te `FOR UPDATE` görünmeyebilir. Kilitte bekleyen bir SELECT ancak KİLİTLİ
        # okumadır; kilitsiz mutant `UPDATE sale_installments`te bekler (ölçüldü).
        assert bekleyen.startswith("SELECT sale_installments."), bekleyen

        release_lock.set()
        sonuc1 = await asyncio.wait_for(task1, timeout=YARIS_TAVANI_SN)
        sonuc2 = await asyncio.wait_for(task2, timeout=YARIS_TAVANI_SN)
        assert sorted([sonuc1, sonuc2]) == ["exceeds", "paid"]
    finally:
        # Kırmızı, ASILMADAN kırmızı görünmek ZORUNDA: bir iddia patlarsa tx1
        # hâlâ kilidi tutuyor olurdu ve temizlik süresiz beklerdi.
        release_lock.set()
        await _sonlandir(task1, task2)

    async with _SessionFactory() as dogrulama:
        satir = await dogrulama.get(SaleInstallment, kurulum.installment_id)
        assert satir is not None
        assert satir.paid_amount == _PAYMENT, (
            f"tahsil edilen {satir.paid_amount} — taksit tutarı {_AMOUNT} yarışla aşıldı"
        )


async def test_tahsilat_taksit_satirini_kilitleyerek_okur(kurulum: _Kurulum) -> None:
    """Kilidin YERİ (SQL düzeyinde): `FOR UPDATE`, doğrulamayı besleyen okumanın
    KENDİSİ olmalıdır — ayrı bir kilitsiz `SELECT` ile okunursa pencere kapanmaz.

    Kilit kümesi TEK satırdır (`sale_installments.id = :id`), bu yüzden burada
    `ORDER BY` gerekmez: deadlock yalnız çok satırlı kilit kümelerinde doğar
    (`PUT installments` orada `ORDER BY sequence_no` kullanır).
    """
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        async with _SessionFactory() as session:
            actor = await session.get(User, kurulum.user_id)
            await installments.pay_installment(
                session, actor, kurulum.installment_id, InstallmentPayInput(amount=_PAYMENT)
            )
            await session.commit()
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)

    kilitli = [
        ifade for ifade in ifadeler if "FOR UPDATE" in ifade and "FROM sale_installments" in ifade
    ]
    assert kilitli, f"taksit satırı FOR UPDATE ile okunmadı: {ifadeler}"


async def test_plan_kaydi_satirlari_sira_numarasiyla_kilitler(kurulum: _Kurulum) -> None:
    """`PUT installments` kilit SIRASI — deadlock bekçisi (SQL düzeyinde).

    Kilit kümesi ÇOK SATIRLIDIR (satışın tüm plan satırları): iki eşzamanlı
    istek bu satırları FARKLI sıralarda kilitlerse karşılıklı kilitlenme doğar.
    `ORDER BY sequence_no` küresel bir sıra dayatır. Ayrıca kilidin YERİ
    sabitlenir: doğrulamayı besleyen `list_installments` okumasından ÖNCE
    gelmelidir — sonra gelirse TOCTOU penceresi kapanmaz.
    """
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    govde = SaleInstallmentsSave(
        items=[
            SaleInstallmentInput(
                sequence_no=1, label="1 / 1", due_date=date(2026, 9, 1), amount=_AMOUNT
            )
        ]
    )
    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        async with _SessionFactory() as session:
            actor = await session.get(User, kurulum.user_id)
            await installments.save_installments(session, actor, kurulum.sale_id, govde)
            await session.commit()
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)

    kilitli = [
        ifade for ifade in ifadeler if "FOR UPDATE" in ifade and "FROM sale_installments" in ifade
    ]
    assert kilitli, f"plan satırları FOR UPDATE ile okunmadı: {ifadeler}"
    assert all("ORDER BY sale_installments.sequence_no" in ifade for ifade in kilitli), (
        f"kilit sırası deterministik değil (ORDER BY sequence_no yok) — deadlock riski: {kilitli}"
    )

    kilit_index = ifadeler.index(kilitli[0])
    okumalar = [
        i
        for i, ifade in enumerate(ifadeler)
        if "FROM sale_installments" in ifade and "FOR UPDATE" not in ifade
    ]
    assert okumalar, f"plan satırı okuması bulunamadı: {ifadeler}"
    assert kilit_index < okumalar[0], (
        "kilit, doğrulamayı besleyen plan okumasından SONRA alınıyor — TOCTOU penceresi açık"
    )


async def _sonlandir(*tasks: "asyncio.Task[str] | None") -> None:
    for task in tasks:
        if task is None:
            continue
        if not task.done():
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(task), timeout=YARIS_TAVANI_SN)
        if not task.done():
            task.cancel()
        with contextlib.suppress(BaseException):
            await task


async def _ode_ve_tut(
    kurulum: _Kurulum, lock_acquired: asyncio.Event, release_lock: asyncio.Event
) -> str:
    """tx1: ödemeyi tamamlar (kilit + doğrulama + flush) ama sinyal gelene kadar
    COMMIT ETMEZ — satır kilidi bu süre boyunca AÇIK kalır."""
    async with _SessionFactory() as session:
        actor = await session.get(User, kurulum.user_id)
        await installments.pay_installment(
            session, actor, kurulum.installment_id, InstallmentPayInput(amount=_PAYMENT)
        )
        lock_acquired.set()
        await release_lock.wait()
        await session.commit()
        return "paid"


async def _ode(kurulum: _Kurulum) -> str:
    async with _SessionFactory() as session:
        actor = await session.get(User, kurulum.user_id)
        try:
            await installments.pay_installment(
                session, actor, kurulum.installment_id, InstallmentPayInput(amount=_PAYMENT)
            )
            await session.commit()
            return "paid"
        except SiteValidationError:
            await session.rollback()
            return "exceeds"


async def _kur() -> _Kurulum:
    async with _SessionFactory() as session:
        role = Role(key=_ROLE_KEY, name="Tahsilat Yarışı Rolü")
        session.add(role)
        await session.flush()
        user = User(
            email="tahsilat-yaris@sales.co",
            password_hash=hash_password("parola1234"),
            full_name="Tahsilat Yarışı",
            role_id=role.id,
        )
        session.add(user)

        project = Project(code="SL-CONC-001", name="Tahsilat Eşzamanlılık Projesi")
        session.add(project)
        await session.flush()
        session.add(ProjectMember(user_id=user.id, project_id=project.id, role_id=user.role_id))

        site = Site(project_id=project.id, code="SL-C-1", name="Merkez")
        session.add(site)
        await session.flush()
        block = Block(project_id=project.id, site_id=site.id, name="C Blok")
        session.add(block)
        await session.flush()
        unit = Unit(
            project_id=project.id, block_id=block.id, unit_no="1", unit_kind=UnitKind.apartment
        )
        customer = Customer(
            customer_type=CustomerType.person, name="Yarış Alıcı", national_id="98765432101"
        )
        session.add_all([unit, customer])
        await session.flush()

        sale = UnitSale(
            project_id=project.id,
            unit_id=unit.id,
            customer_id=customer.id,
            sale_type=SaleType.sale,
            status=UnitSaleStatus.active,
            sale_price=_AMOUNT,
            created_by=user.id,
        )
        session.add(sale)
        await session.flush()
        installment = SaleInstallment(
            sale_id=sale.id,
            sequence_no=1,
            label="1 / 1",
            due_date=date(2026, 9, 1),
            amount=_AMOUNT,
        )
        session.add(installment)
        await session.commit()
        return _Kurulum(user.id, sale.id, installment.id)


async def _temizle(kurulum: _Kurulum) -> None:
    async with _SessionFactory() as session:
        sale = await session.get(UnitSale, kurulum.sale_id)
        project_id = sale.project_id if sale is not None else None
        unit_id = sale.unit_id if sale is not None else None
        customer_id = sale.customer_id if sale is not None else None
        await session.execute(
            delete(SaleInstallment).where(SaleInstallment.sale_id == kurulum.sale_id)
        )
        await session.execute(delete(UnitSale).where(UnitSale.id == kurulum.sale_id))
        if unit_id is not None:
            await session.execute(delete(Unit).where(Unit.id == unit_id))
        if customer_id is not None:
            await session.execute(delete(Customer).where(Customer.id == customer_id))
        if project_id is not None:
            await session.execute(delete(Block).where(Block.project_id == project_id))
            await session.execute(delete(Site).where(Site.project_id == project_id))
            await session.execute(
                delete(ProjectMember).where(ProjectMember.project_id == project_id)
            )
            await session.execute(delete(Project).where(Project.id == project_id))
        await session.execute(delete(User).where(User.id == kurulum.user_id))
        await session.execute(delete(Role).where(Role.key == _ROLE_KEY))
        await session.commit()

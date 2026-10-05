"""TB1/T2 — `PUT /projects/{id}/contract/distribution` eşzamanlılık kilidi (spec §2).

`save_distribution` önce okuyup sonra yazar: "Σ kota ≤ sözleşme miktarı"
doğrulaması (`_assert_within_contract_quantity`) ile yazma arasındaki pencerede
ikinci bir istek AYNI "kalan"ı okursa ikisi de geçerli sanılır, ikisi de yazar
ve toplam dağıtım sözleşme kalemini AŞAR (TOCTOU). Kilit bu pencereyi kapatır:
sözleşme kalemleri `SELECT … FOR UPDATE` ile kilitlenmeden hiçbir doğrulama
okuması yapılmaz.

Neden `client`/`seeded_db` KULLANILMAZ (`progress_payments/test_concurrency.py`
ile aynı gerekçe): `tests/conftest.py`'deki `db_session` her testi TEK bağlantı
üzerinde SAVEPOINT'e sarar ve dış transaction'ı asla COMMIT ETMEZ — o session
üzerinde iki `asyncio` görevi de AYNI bağlantıyı paylaşır, gerçek satır kilidi
test EDİLEMEZ. Bu dosya bilerek İKİ BAĞIMSIZ bağlantı açar, kurulumu GERÇEKTEN
commit eder ve sonunda GERÇEKTEN temizler.
"""

import asyncio
import contextlib
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import SiteValidationError
from app.core.security import hash_password
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.contracts import distribution, schemas
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.projects.models import Project, ProjectContract
from app.modules.roles.models import Role
from app.modules.sites.models import Site
from app.modules.users.models import ProjectMember, User
from tests._yaris import YARIS_TAVANI_SN, kilitte_bekleyen_sorgu
from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio

_SessionFactory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

# Sözleşme kalemi 100 birim; her istek 60 ister — TEK BAŞINA geçerli, İKİSİ
# BİRDEN aşım (120 > 100). Yarışın tüm kanıtı bu üç sayıdadır.
_ITEM_QUANTITY = Decimal("100")
_ALLOCATION = Decimal("60")

# Seed rollerinden AYRI, yalnız bu dosyaya ait anahtar — paket geneline sızmaz.
_ROLE_KEY = "tb1_dagitim_yarisi"


class _Kurulum:
    """Test verisinin kimlikleri — fixture'lar arası taşıyıcı."""

    def __init__(
        self,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        item_id: uuid.UUID,
        site_a_id: uuid.UUID,
        site_b_id: uuid.UUID,
    ) -> None:
        self.project_id = project_id
        self.user_id = user_id
        self.item_id = item_id
        self.site_a_id = site_a_id
        self.site_b_id = site_b_id


@pytest.fixture
async def kurulum():
    veri = await _kur()
    try:
        yield veri
    finally:
        await _temizle(veri)


async def test_esZamanli_dagitim_sozlesme_miktarini_asamaz(kurulum: _Kurulum) -> None:
    """KIRMIZI kanıtı (kilit yokken): tx2, tx1 yazarken beklemez; ikisi de
    "kalan 100" görür, ikisi de 60 yazar → toplam 120 > 100.

    Kilit varken tx2 `SELECT … FOR UPDATE`'te BLOKE olur (`not task2.done()`),
    tx1 commit edince kilidi alır, artık tx1'in 60'ını GÖRÜR ve 60+60 > 100
    olduğu için `DISTRIBUTION_EXCEEDS` (422) alır. Sonuç: DB'de tek 60 kalır.
    """
    lock_acquired = asyncio.Event()
    release_lock = asyncio.Event()

    task1 = asyncio.create_task(
        _kaydet_ve_tut(kurulum, kurulum.site_a_id, lock_acquired, release_lock)
    )
    task2: asyncio.Task[str] | None = None
    try:
        await asyncio.wait_for(lock_acquired.wait(), timeout=YARIS_TAVANI_SN)

        task2 = asyncio.create_task(_kaydet(kurulum, kurulum.site_b_id))
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine,
            task2,
            mesaj="tx2, tx1 kilidi serbest bırakmadan ilerleyebildi — `save_distribution` "
            "sözleşme kalemlerini `SELECT … FOR UPDATE` ile KİLİTLEMİYOR olabilir",
        )
        assert "FROM employer_contract_items" in bekleyen and "FOR UPDATE" in bekleyen, bekleyen

        release_lock.set()
        sonuc1 = await asyncio.wait_for(task1, timeout=YARIS_TAVANI_SN)
        sonuc2 = await asyncio.wait_for(task2, timeout=YARIS_TAVANI_SN)
        assert sorted([sonuc1, sonuc2]) == ["exceeds", "saved"]
    finally:
        # Bir iddia KIRMIZI döndüğünde tx1 hâlâ `release_lock`'u bekliyor ve
        # commit edilmemiş satırlarıyla kilit tutuyor olurdu; temizlik (fixture
        # teardown) o kilitte SÜRESİZ beklerdi. Kırmızı, ASILMADAN kırmızı
        # görünmek ZORUNDA — bu yüzden görevler burada mutlaka sonlandırılır.
        release_lock.set()
        await _sonlandir(task1, task2)

    async with _SessionFactory() as dogrulama:
        toplam = await _dagitilan_toplam(dogrulama, kurulum.item_id)
    assert toplam == _ALLOCATION, (
        f"dağıtılan toplam {toplam} — sözleşme miktarı {_ITEM_QUANTITY} yarışla aşıldı"
    )


async def test_dagitim_sozlesme_kalemlerini_id_sirasiyla_kilitler(kurulum: _Kurulum) -> None:
    """Kilit SIRASI — deadlock bekçisi (SQL düzeyinde).

    Davranış testi kilidin VARLIĞINI gösterir ama SIRASINI gösteremez. Kilit
    kümesi çok satırlıdır (bir sözleşmenin TÜM kalemleri): iki eşzamanlı istek
    bu satırları FARKLI sıralarda kilitlerse karşılıklı kilitlenme (deadlock)
    doğar — A kalem-1'i, B kalem-2'yi tutarken ikisi de diğerini bekler.
    `ORDER BY id` küresel bir sıra dayatır, dolayısıyla iki istek daima aynı
    satırda buluşur: biri bekler, ikisi birden asılmaz.

    Ayrıca kilidin YERİ sabitlenir: `FOR UPDATE` ifadesi, aşım doğrulamasının
    beslendiği `boq_items` okumasından ÖNCE gelmelidir — sonra gelirse pencere
    kapanmaz.
    """
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        async with _SessionFactory() as session:
            actor = await session.get(User, kurulum.user_id)
            await distribution.save_distribution(
                session,
                actor,
                kurulum.project_id,
                _govde(kurulum.item_id, kurulum.site_a_id),
            )
            await session.commit()
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)

    kilitli = [ifade for ifade in ifadeler if "FOR UPDATE" in ifade]
    kalem_kilidi = [
        ifade for ifade in kilitli if "FROM employer_contract_items" in ifade.replace("\n", " ")
    ]
    assert kalem_kilidi, f"sözleşme kalemleri FOR UPDATE ile okunmadı: {kilitli}"
    assert all("ORDER BY employer_contract_items.id" in ifade for ifade in kalem_kilidi), (
        f"kilit sırası deterministik değil (ORDER BY id yok) — deadlock riski: {kalem_kilidi}"
    )

    kilit_index = next(i for i, ifade in enumerate(ifadeler) if ifade == kalem_kilidi[0])
    boq_okuma = [i for i, ifade in enumerate(ifadeler) if "FROM boq_items" in ifade]
    assert boq_okuma, f"boq_items okuması bulunamadı: {ifadeler}"
    assert kilit_index < boq_okuma[0], (
        "kilit, doğrulamayı besleyen `boq_items` okumasından SONRA alınıyor — "
        "TOCTOU penceresi hâlâ açık"
    )


async def test_tek_istek_davranisi_degismedi(kurulum: _Kurulum) -> None:
    """Regresyon: kilit tek-istek yolunu değiştirmez — 60 yazılır, kalan 40."""
    async with _SessionFactory() as session:
        actor = await session.get(User, kurulum.user_id)
        yanit = await distribution.save_distribution(
            session, actor, kurulum.project_id, _govde(kurulum.item_id, kurulum.site_a_id)
        )
        await session.commit()

    kalem = yanit.groups[0].items[0]
    assert kalem.remaining_quantity == _ITEM_QUANTITY - _ALLOCATION
    assert [(a.site_id, a.quantity) for a in kalem.allocations] == [
        (kurulum.site_a_id, _ALLOCATION)
    ]
    assert yanit.distributed_item_count == 1
    assert yanit.undistributed_item_count == 0


async def _sonlandir(*tasks: "asyncio.Task[str] | None") -> None:
    """Bekleyen görevleri kapat: önce nazikçe (kilit zaten serbest), takılan
    kalırsa iptal — hiçbir transaction açık BIRAKILMAZ."""
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


def _govde(item_id: uuid.UUID, site_id: uuid.UUID) -> schemas.ContractDistributionSave:
    return schemas.ContractDistributionSave(
        allocations=[
            schemas.ContractAllocationInput(
                contract_item_id=item_id, site_id=site_id, quantity=_ALLOCATION
            )
        ]
    )


async def _kaydet_ve_tut(
    kurulum: _Kurulum,
    site_id: uuid.UUID,
    lock_acquired: asyncio.Event,
    release_lock: asyncio.Event,
) -> str:
    """tx1: `save_distribution`'ı tamamlar (kilit + doğrulama + flush) ama sinyal
    gelene kadar COMMIT ETMEZ — satır kilidi bu süre boyunca AÇIK kalır
    (`flush` kilidi bırakmaz, yalnız commit/rollback bırakır)."""
    async with _SessionFactory() as session:
        actor = await session.get(User, kurulum.user_id)
        await distribution.save_distribution(
            session, actor, kurulum.project_id, _govde(kurulum.item_id, site_id)
        )
        lock_acquired.set()
        await release_lock.wait()
        await session.commit()
        return "saved"


async def _kaydet(kurulum: _Kurulum, site_id: uuid.UUID) -> str:
    async with _SessionFactory() as session:
        actor = await session.get(User, kurulum.user_id)
        try:
            await distribution.save_distribution(
                session, actor, kurulum.project_id, _govde(kurulum.item_id, site_id)
            )
            await session.commit()
            return "saved"
        except SiteValidationError:
            await session.rollback()
            return "exceeds"


async def _dagitilan_toplam(session: AsyncSession, item_id: uuid.UUID) -> Decimal:
    rows = (
        (await session.execute(select(BoqItem).where(BoqItem.contract_item_id == item_id)))
        .scalars()
        .all()
    )
    return sum((row.quantity for row in rows), Decimal("0"))


async def _kur() -> _Kurulum:
    """Reel commit'li kurulum: proje + sözleşme + tek poz kalemi (100) + iki şantiye.

    `seed_reference_data` BİLİNÇLİ OLARAK ÇAĞRILMAZ: bu dosyadaki commit'ler
    gerçek ve kalıcıdır, referans veri commit edilseydi `roles` satırları test
    paketinin geri kalanına SIZAR ve (ölçüldü) `tests/modules/test_role_model.py`
    `patron` anahtarında `UniqueViolationError` alırdı. Onun yerine yalnız bu
    testin ihtiyacı kurulur ve tamamı `_temizle`'de geri alınır: kendi rolü,
    kendi kullanıcısı ve `user_project_access` üzerinden AÇIK proje görünürlüğü
    (`visible_projects` admin dalına gerek yok — kapsam satırı yeterli).
    """
    async with _SessionFactory() as session:
        role = Role(key=_ROLE_KEY, name="Dağıtım Yarışı Rolü")
        session.add(role)
        await session.flush()
        user = User(
            email="dagitim-yaris@contracts.co",
            password_hash=hash_password("parola1234"),
            full_name="Dağıtım Yarışı",
            role_id=role.id,
        )
        session.add(user)

        project = Project(code="CD-CONC-001", name="Dağıtım Eşzamanlılık Projesi")
        session.add(project)
        await session.flush()
        session.add(ProjectMember(user_id=user.id, project_id=project.id, role_id=user.role_id))
        session.add(
            ProjectContract(
                project_id=project.id,
                contract_no="SZL-2026-DAG",
                amount=Decimal("1000000"),
                advance_pct=Decimal("10"),
                retainage_pct=Decimal("5"),
                vat_pct=Decimal("20"),
            )
        )
        await session.flush()

        group = EmployerContractGroup(project_id=project.id, name="A Grubu", sort_order=1)
        session.add(group)
        await session.flush()
        item = EmployerContractItem(
            project_id=project.id,
            group_id=group.id,
            code="10.100",
            description="Beton dökümü",
            unit="m3",
            quantity=_ITEM_QUANTITY,
            unit_price=Decimal("1000.00"),
            sort_order=1,
        )
        session.add(item)

        site_a = Site(project_id=project.id, code="CD-A", name="A Şantiyesi")
        site_b = Site(project_id=project.id, code="CD-B", name="B Şantiyesi")
        session.add_all([site_a, site_b])
        await session.commit()
        return _Kurulum(project.id, user.id, item.id, site_a.id, site_b.id)


async def _temizle(kurulum: _Kurulum) -> None:
    async with _SessionFactory() as session:
        site_ids = [kurulum.site_a_id, kurulum.site_b_id]
        await session.execute(delete(BoqItem).where(BoqItem.site_id.in_(site_ids)))
        await session.execute(delete(BoqGroup).where(BoqGroup.site_id.in_(site_ids)))
        await session.execute(delete(Site).where(Site.id.in_(site_ids)))
        await session.execute(
            delete(EmployerContractItem).where(
                EmployerContractItem.project_id == kurulum.project_id
            )
        )
        await session.execute(
            delete(EmployerContractGroup).where(
                EmployerContractGroup.project_id == kurulum.project_id
            )
        )
        await session.execute(
            delete(ProjectContract).where(ProjectContract.project_id == kurulum.project_id)
        )
        await session.execute(delete(Project).where(Project.id == kurulum.project_id))
        await session.execute(delete(User).where(User.id == kurulum.user_id))
        await session.execute(delete(Role).where(Role.key == _ROLE_KEY))
        await session.commit()

"""🔴 BOQ-SEC T2 — EŞİK = KİLİT (WORKFLOW §4, İK-2 dersi) miktar kotasında.

Tahsis toplamı bir KOTADIR: `SUM(tahsisler) <= boq_items.quantity`. Kilit
olmadan iki eşzamanlı istek AYNI toplamı okur, ikisi de "sığıyor" der ve şantiye
kotası AŞILIR — 1.200 m³'lük poz 1.400 m³ dağıtılmış görünür ve hiçbir uç bunu
bir daha fark etmez.

Neden `client`/`seeded_db` KULLANILMAZ: `tests/conftest.py`'deki `db_session`
her testi TEK bağlantı üzerinde SAVEPOINT'e sarar ve dış transaction'ı asla
gerçekten COMMIT ETMEZ — o session üzerindeki iki `asyncio` görevi AYNI
bağlantıyı paylaşır ve gerçek satır kilidi test EDİLEMEZ (FAT-1 dersi: soğuk
havuzda "paralel" iki istek sırayla koşar ve test SAHTE YEŞİL verir). Bu dosya
`tests/modules/payroll/test_payroll_approval_concurrency.py` desenini birebir
izler: İKİ BAĞIMSIZ bağlantı, gerçek commit, sonunda gerçek temizlik.

Bariyer (`asyncio.Event`) BİLİNÇLİDİR: çıplak `asyncio.gather` iki görevi kritik
anda KESİŞTİRMEZ, yarış penceresi hiç açılmayabilir. Burada tx1 kilidi ALIP
TUTARKEN tx2'nin BLOKE olduğu DOĞRUDAN kanıtlanır; kilit kaldırılınca test
KIRMIZI olur (mutasyon denetimi raporda).
"""

import asyncio
import contextlib
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError
from app.core.security import hash_password
from app.modules.boq import service
from app.modules.boq.models import BoqGroup, BoqItem, BoqItemSectionAllocation
from app.modules.boq.schemas import BoqItemAllocationInput, BoqItemAllocationsReplace
from app.modules.projects.models import Project, ProjectStatus, ProjectType
from app.modules.roles.models import Role
from app.modules.sites.models import Section, Site
from app.modules.users.models import ProjectMember, User
from tests._yaris import YARIS_TAVANI_SN, kilitte_bekleyen_sorgu
from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio

_SessionFactory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

#: Rol anahtarı ve e-posta TESTE ÖZELDİR: bu dosya GERÇEKTEN commit ettiği için
#: sızıntı ancak yaratılan satırların tam bilinmesiyle kapanır.
_ROL_ANAHTARI = "boqsec_conc_admin"
_EPOSTA = "boqsec-tahsis@conc.co"
_PROJE_KODU = "BOQSEC-CONC"

#: Poz kotası. İki eşzamanlı istek 700'er ister: tek tek sığar, BİRLİKTE sığmaz.
KOTA = Decimal("1200.000")
YARIM_USTU = Decimal("700.000")


class _Kurulum:
    def __init__(
        self,
        item_id: uuid.UUID,
        section_ids: list[uuid.UUID],
        site_id: uuid.UUID,
        project_id: uuid.UUID,
        actor_id: uuid.UUID,
        role_id: uuid.UUID,
    ) -> None:
        self.item_id = item_id
        self.section_ids = section_ids
        self.site_id = site_id
        self.project_id = project_id
        self.actor_id = actor_id
        self.role_id = role_id


async def _kur() -> _Kurulum:
    async with _SessionFactory() as session:
        role = Role(key=_ROL_ANAHTARI, name="BOQ Tahsis Eşzamanlılık Rolü")
        session.add(role)
        await session.flush()

        aktor = User(
            email=_EPOSTA,
            password_hash=hash_password("parola1234"),
            full_name="Tahsis Aktörü",
            role_id=role.id,
        )
        project = Project(
            code=_PROJE_KODU,
            name="Tahsis Eşzamanlılık Projesi",
            status=ProjectStatus.active,
            budget=Decimal("1000000.00"),
            progress_pct=Decimal("0.00"),
            project_type=ProjectType.taahhut,
        )
        session.add_all([aktor, project])
        await session.flush()
        aktor.all_projects = True

        site = Site(project_id=project.id, code="CONC-BLOK", name="Eşzamanlılık Şantiyesi")
        session.add(site)
        await session.flush()

        group = BoqGroup(site_id=site.id, name="BETON İŞLERİ")
        sections = [
            Section(site_id=site.id, name="Kat 6-10"),
            Section(site_id=site.id, name="Kat 11-15"),
        ]
        session.add_all([group, *sections])
        await session.flush()

        item = BoqItem(
            site_id=site.id,
            group_id=group.id,
            code="01.001",
            description="Beton Dökümü C30",
            unit="m³",
            quantity=KOTA,
            unit_price=Decimal("100.00"),
        )
        session.add(item)
        await session.flush()
        await session.commit()
        return _Kurulum(
            item_id=item.id,
            section_ids=[s.id for s in sections],
            site_id=site.id,
            project_id=project.id,
            actor_id=aktor.id,
            role_id=role.id,
        )


async def _gorevleri_bosalt(*gorevler: asyncio.Task | None) -> None:
    """Temizlikten ÖNCE görevleri sonlandırır — MUTASYON DENETİMİ İÇİN ŞART.

    Kilit kaldırıldığında `not task2.done()` iddiası kırmızıya döner ve test
    gövdesi ORTADA terk edilir; `task1` hâlâ commit etmemiş bir transaction
    içinde satır kilidi TUTUYOR olur. Bu boşaltma olmadan `_temizle`nin DELETE'i
    o kilidi SONSUZA DEK bekler ve kırmızı test SONSUZ ASKIYA dönüşürdü.
    """
    for gorev in gorevler:
        if gorev is None:
            continue
        gorev.cancel()
        with contextlib.suppress(BaseException):
            await gorev


async def _temizle(kurulum: _Kurulum) -> None:
    async with _SessionFactory() as session:
        await session.execute(
            delete(BoqItemSectionAllocation).where(
                BoqItemSectionAllocation.boq_item_id == kurulum.item_id
            )
        )
        await session.execute(delete(BoqItem).where(BoqItem.id == kurulum.item_id))
        await session.execute(delete(Section).where(Section.site_id == kurulum.site_id))
        await session.execute(delete(BoqGroup).where(BoqGroup.site_id == kurulum.site_id))
        await session.execute(delete(Site).where(Site.id == kurulum.site_id))
        await session.execute(
            delete(ProjectMember).where(ProjectMember.user_id == kurulum.actor_id)
        )
        await session.execute(delete(User).where(User.id == kurulum.actor_id))
        await session.execute(delete(Project).where(Project.id == kurulum.project_id))
        await session.execute(delete(Role).where(Role.id == kurulum.role_id))
        await session.commit()


async def _aktor(session: AsyncSession, actor_id: uuid.UUID) -> User:
    return (await session.execute(select(User).where(User.id == actor_id))).scalar_one()


def _govde(section_id: uuid.UUID, quantity: Decimal) -> BoqItemAllocationsReplace:
    return BoqItemAllocationsReplace(
        allocations=[BoqItemAllocationInput(section_id=section_id, quantity=quantity)]
    )


async def _tahsis_et_ve_tut(
    kurulum: _Kurulum,
    section_id: uuid.UUID,
    kilit_alindi: asyncio.Event,
    kilidi_birak: asyncio.Event,
) -> str:
    """tx1: tahsisi tamamlar (kilit + kontrol + flush) ama sinyal gelene kadar
    COMMIT ETMEZ — `SELECT ... FOR UPDATE` kilidi bu süre boyunca AÇIK kalır
    (`flush` kilidi BIRAKMAZ, yalnız commit/rollback bırakır)."""
    async with _SessionFactory() as session:
        actor = await _aktor(session, kurulum.actor_id)
        await service.replace_allocations(
            session, actor, kurulum.item_id, _govde(section_id, YARIM_USTU)
        )
        kilit_alindi.set()
        await kilidi_birak.wait()
        await session.commit()
        return "ok"


async def _tahsis_et(kurulum: _Kurulum, section_id: uuid.UUID) -> str:
    async with _SessionFactory() as session:
        actor = await _aktor(session, kurulum.actor_id)
        try:
            await service.replace_allocations(
                session, actor, kurulum.item_id, _govde(section_id, YARIM_USTU)
            )
            await session.commit()
            return "ok"
        except ConflictError:
            await session.rollback()
            return "conflict"


async def test_iki_esZamanli_REPLACE_serilesir_ve_toplam_kotayi_asmaz() -> None:
    """🔴 ÖLÇÜLMÜŞ BULGU — bu senaryo emirdeki "ikisi de invariantı aşsın"
    kurgusuyla ÇALIŞMAZ ve nedeni semantiktedir, kilitte değil.

    Uç TAM KÜME DEĞİŞTİRMEdir (K4): her gövde o pozun TÜM tahsislerini taşır.
    Dolayısıyla iki eşzamanlı `replace` TOPLANMAZ — ikincisi birincinin
    satırlarını siler. İki isteğin BİRLİKTE kotayı aşması bu uçta YAPISAL OLARAK
    imkânsızdır; aşımın gerçek yolu İKİNCİ KAPIDIR (`PATCH quantity`, bir sonraki
    test).

    Yine de kilit burada da ÖLÇÜLÜR ve GEREKLİDİR:

    1. `not task2.done()` — tx2 GERÇEKTEN bloke olur. Bu aynı zamanda testin iki
       AYRI BAĞLANTI kullandığının kanıtıdır (FAT-1: tek bağlantıda tx2 hiç
       beklemez ve bu satır kırmızıya döner);
    2. Sonuç TUTARLIDIR: son yazan kazanır, toplam 700'dür — kilitsiz hâlde iki
       istek birbirinin sildiği/yazdığı satırlarla kesişir ve ertelenmiş UQ
       altında ızgara ikisinin de olmadığı bir hâlde kalabilirdi.
    """
    kurulum = await _kur()
    kilit_alindi = asyncio.Event()
    kilidi_birak = asyncio.Event()
    task1: asyncio.Task | None = None
    task2: asyncio.Task | None = None
    try:
        task1 = asyncio.create_task(
            _tahsis_et_ve_tut(kurulum, kurulum.section_ids[0], kilit_alindi, kilidi_birak)
        )
        await asyncio.wait_for(kilit_alindi.wait(), timeout=YARIS_TAVANI_SN)

        task2 = asyncio.create_task(_tahsis_et(kurulum, kurulum.section_ids[1]))
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine,
            task2,
            mesaj="ikinci tahsis, birincinin kilidi serbest bırakılmadan ilerleyebildi — "
            "`replace_allocations` poz satırını KİLİTLEMİYOR olabilir "
            "(iki yazma kapısı arasındaki serileşme kaybolur)",
        )
        assert "FROM boq_items" in bekleyen and "FOR UPDATE" in bekleyen, bekleyen

        kilidi_birak.set()
        assert await asyncio.wait_for(task1, timeout=YARIS_TAVANI_SN) == "ok"
        assert await asyncio.wait_for(task2, timeout=YARIS_TAVANI_SN) == "ok"

        async with _SessionFactory() as dogrula:
            satirlar = (
                (
                    await dogrula.execute(
                        select(BoqItemSectionAllocation).where(
                            BoqItemSectionAllocation.boq_item_id == kurulum.item_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            # Son yazan kazanır: TEK satır, tek bölüm — birleşme/çoğalma YOK.
            assert [r.section_id for r in satirlar] == [kurulum.section_ids[1]]
            assert sum((r.quantity for r in satirlar), Decimal("0")) == YARIM_USTU
            assert sum((r.quantity for r in satirlar), Decimal("0")) <= KOTA
    finally:
        await _gorevleri_bosalt(task1, task2)
        await _temizle(kurulum)


async def test_esZamanli_PATCH_kota_dusurmesi_de_serilesir() -> None:
    """🔴 İnvariantın İKİNCİ kapısı da AYNI kilitte serileşir.

    tx1 tahsisi 700'e çıkarıp kilidi tutar; tx2 pozun kotasını 500'e indirmeye
    çalışır ve BEKLER. Serbest bırakılınca tazelenmiş toplamı görür ve 409 alır.
    İki kapı ayrı kilitler kullansaydı bu senaryo geçer ve `SUM > quantity`
    kalıcı olarak yazılırdı — hiçbir tek kapılı test bunu göremezdi.
    """
    from app.modules.boq.schemas import BoqItemUpdate

    kurulum = await _kur()
    kilit_alindi = asyncio.Event()
    kilidi_birak = asyncio.Event()
    task1: asyncio.Task | None = None
    task2: asyncio.Task | None = None

    async def _kotayi_dusur() -> str:
        async with _SessionFactory() as session:
            actor = await _aktor(session, kurulum.actor_id)
            try:
                await service.update_item(
                    session, actor, kurulum.item_id, BoqItemUpdate(quantity=Decimal("500.000"))
                )
                await session.commit()
                return "ok"
            except ConflictError:
                await session.rollback()
                return "conflict"

    try:
        task1 = asyncio.create_task(
            _tahsis_et_ve_tut(kurulum, kurulum.section_ids[0], kilit_alindi, kilidi_birak)
        )
        await asyncio.wait_for(kilit_alindi.wait(), timeout=YARIS_TAVANI_SN)

        task2 = asyncio.create_task(_kotayi_dusur())
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine,
            task2,
            mesaj="kota düşürme, tahsisin kilidi serbest bırakılmadan ilerleyebildi — "
            "`update_item` poz satırını KİLİTLEMİYOR olabilir "
            "(SUM > quantity kalıcı olarak yazılabilir)",
        )
        assert "FROM boq_items" in bekleyen and "FOR UPDATE" in bekleyen, bekleyen

        kilidi_birak.set()
        assert await asyncio.wait_for(task1, timeout=YARIS_TAVANI_SN) == "ok"
        assert await asyncio.wait_for(task2, timeout=YARIS_TAVANI_SN) == "conflict", (
            "kota tahsis toplamının altına indirildi — invariant kırıldı"
        )
    finally:
        await _gorevleri_bosalt(task1, task2)
        await _temizle(kurulum)


async def test_kilit_toplam_okumasindan_ONCE_alinir() -> None:
    """Kilit SQL'de görünür: `boq_items ... FOR UPDATE` → tahsis toplamı okuması.

    İki şeyi çiviler:

    1. `boq_items` satırı GERÇEKTEN `FOR UPDATE` ile okunur (ORM'in normal
       `session.get`i kilit ALMAZ — kilidin varlığı ancak SQL'de görülür);
    2. **toplam okuması kilitten SONRA gelir** — kilitsiz bir `SUM` kilidin
       önüne geçseydi TOCTOU penceresi açık kalırdı ve testin değer iddiaları
       yine yeşil olurdu.
    """
    kurulum = await _kur()
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        async with _SessionFactory() as session:
            actor = await _aktor(session, kurulum.actor_id)
            await service.replace_allocations(
                session, actor, kurulum.item_id, _govde(kurulum.section_ids[0], YARIM_USTU)
            )
            await session.commit()
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)
        await _temizle(kurulum)

    kilit = [
        i for i, ifade in enumerate(ifadeler) if "FOR UPDATE" in ifade and "FROM boq_items" in ifade
    ]
    toplam_okumasi = [
        i
        for i, ifade in enumerate(ifadeler)
        if "sum(boq_item_section_allocations.quantity)" in ifade.lower()
        or "boq_item_section_allocations" in ifade
        and "SELECT" in ifade
    ]

    assert kilit, f"poz satırı FOR UPDATE ile okunmadı: {ifadeler}"
    assert toplam_okumasi, f"tahsis satırları hiç okunmadı: {ifadeler}"
    assert kilit[0] < toplam_okumasi[0], (
        f"tahsis toplamı KİLİTTEN ÖNCE okunmuş — TOCTOU penceresi açık: {ifadeler}"
    )


async def test_TERS_YON_tahsis_TAZELENMIS_kotayi_okur() -> None:
    """🔴 Kilidin ikinci yarısı: tx1 KOTAYI DÜŞÜRÜP kilidi tutar, tx2 tahsis eder.

    `_visible_item` pozu kilitten ÖNCE okur; o ORM nesnesi tx1'in düşürdüğü
    kotadan HABERSİZDİR. `replace_allocations` kontrolü o bayat nesneden
    yapsaydı (ya da `lock_item` `populate_existing` olmasaydı) tx2 hâlâ 1.200
    görür ve 700'ü YAZARDI — kilit alınmış ama okunan sayı eski olurdu, yani
    kilit hiçbir işe yaramazdı. Burada tx2'nin **409** alması, kilidin ardından
    TAZELENMİŞ kotanın okunduğunun kanıtıdır.
    """
    from app.modules.boq.schemas import BoqItemUpdate

    kurulum = await _kur()
    kilit_alindi = asyncio.Event()
    kilidi_birak = asyncio.Event()
    task1: asyncio.Task | None = None
    task2: asyncio.Task | None = None

    async def _kotayi_dusur_ve_tut() -> str:
        async with _SessionFactory() as session:
            actor = await _aktor(session, kurulum.actor_id)
            await service.update_item(
                session, actor, kurulum.item_id, BoqItemUpdate(quantity=Decimal("500.000"))
            )
            kilit_alindi.set()
            await kilidi_birak.wait()
            await session.commit()
            return "ok"

    try:
        task1 = asyncio.create_task(_kotayi_dusur_ve_tut())
        await asyncio.wait_for(kilit_alindi.wait(), timeout=YARIS_TAVANI_SN)

        task2 = asyncio.create_task(_tahsis_et(kurulum, kurulum.section_ids[0]))
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine,
            task2,
            mesaj="tahsis, kota düşürmenin kilidi serbest bırakılmadan ilerleyebildi",
        )
        assert "FROM boq_items" in bekleyen and "FOR UPDATE" in bekleyen, bekleyen

        kilidi_birak.set()
        assert await asyncio.wait_for(task1, timeout=YARIS_TAVANI_SN) == "ok"
        assert await asyncio.wait_for(task2, timeout=YARIS_TAVANI_SN) == "conflict", (
            "tahsis BAYAT kotayı (1.200) okudu — kilit alındı ama tazelenmedi"
        )
    finally:
        await _gorevleri_bosalt(task1, task2)
        await _temizle(kurulum)

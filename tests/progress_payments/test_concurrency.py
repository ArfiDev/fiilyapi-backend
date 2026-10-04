"""Task H4 — eşzamanlılık: `SELECT … FOR UPDATE` kilidi altında D8 + `sequence_no`

üretimi (spec §7 eşzamanlılık notunun oluşturmaya izdüşümü). İki eşzamanlı
`POST` aynı `sequence_no`'yu ÜRETEMEZ; ikisi de D8 kontrolünü boşken GEÇEMEZ —
yalnız biri başarılı olur, diğeri 409 `OPEN_PAYMENT_EXISTS` alır.

Neden `client`/`seeded_db` KULLANILMAZ: `tests/conftest.py`'deki `db_session`
her testi TEK bir bağlantı üzerinde SAVEPOINT'e sarar ve dış transaction'ı asla
gerçekten COMMIT ETMEZ (kendi docstring'i) — o session üzerinde iki `asyncio.gather`
görevi de AYNI bağlantıyı paylaşır, gerçek satır kilidi/eşzamanlılık test EDİLEMEZ.
Bu test bilinçli olarak `test_engine`'den İKİ BAĞIMSIZ bağlantı açar, kurulum
verisini GERÇEKTEN commit eder ve sonunda GERÇEKTEN temizler.
"""

import asyncio
import contextlib
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.access import AccessLevel, Scope
from app.core.errors import ConflictError
from app.core.sayfalar import PageLevel
from app.core.security import hash_password
from app.modules.progress_payments import schemas, service, transitions
from app.modules.progress_payments.models import ProgressPayment, ProgressPaymentStatus
from app.modules.projects.models import Project, ProjectContract
from app.modules.roles.models import Module, ModuleGroup, Role, RolePagePermission, RolePermission
from app.modules.users.models import User
from tests._yaris import YARIS_TAVANI_SN, kilitte_bekleyen_sorgu
from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio

_SessionFactory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def test_iki_esZamanli_olusturma_yalniz_biri_gecer() -> None:
    """`.with_for_update()` kaldırılırsa bu test hâlâ 3/3 yeşil kalabilirdi çünkü
    eski hâliyle `asyncio.gather` iki görevi kritik anda KESİŞTİRMİYORDU — yarış
    penceresi hiç açılmıyordu (H4 denetimi Y3). Burada bilerek bir `asyncio.Event`
    bariyeriyle tx1'in kilidi ALIP TUTARKEN tx2'nin bloke olduğu doğrudan
    kanıtlanır: `asyncio.sleep` sonrası tx2 görevi hâlâ `done()` DEĞİLSE kilit
    tutuyor demektir; kilit YOKSA tx2 hemen ilerler (ya biter ya da başka bir
    hatayla patlar) ve bu iddia KIRMIZI döner.
    """
    project_id, user_id = await _kurulum()
    lock_acquired = asyncio.Event()
    release_lock = asyncio.Event()
    task1: asyncio.Task[str] | None = None
    task2: asyncio.Task[str] | None = None
    try:
        task1 = asyncio.create_task(
            _attempt_create_and_hold(project_id, user_id, lock_acquired, release_lock)
        )
        await asyncio.wait_for(lock_acquired.wait(), timeout=YARIS_TAVANI_SN)

        task2 = asyncio.create_task(_attempt_create(project_id, user_id))
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine,
            task2,
            mesaj="tx2, tx1 kilidi serbest bırakmadan ilerleyebildi — "
            "`get_contract_locked` artık satırı KİLİTLEMİYOR olabilir",
        )
        # Sorgu 1024 baytta kırpılır (`track_activity_query_size`); kilitte bekleyen SELECT ancak
        # KİLİTLİ okumadır — kilitsiz mutant `INSERT`/`UPDATE`te bekler (TEST-B1, ölçüldü).
        assert bekleyen.startswith("SELECT project_contracts."), bekleyen

        release_lock.set()
        result1 = await asyncio.wait_for(task1, timeout=YARIS_TAVANI_SN)
        result2 = await asyncio.wait_for(task2, timeout=YARIS_TAVANI_SN)

        assert sorted([result1, result2]) == ["conflict", "created"]

        async with _SessionFactory() as verify_session:
            actor = await verify_session.get(User, user_id)
            listed = await service.list_payments(
                verify_session, actor, project_id=project_id, site_id=None, status_filter=None
            )
        assert len(listed.items) == 1
        assert listed.items[0].sequence_no == 1
    finally:
        # TEST-B1: iddia ORTADA düşerse tx1 kilidi TUTUYORDU ve `_temizle` süresiz
        # beklerdi (ölçüldü: koşu asılı kaldı). Önce kilit, sonra görevler, sonra temizlik.
        release_lock.set()
        await _gorevleri_bosalt(task1, task2)
        await _temizle(project_id, user_id)


async def _attempt_create_and_hold(
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    lock_acquired: asyncio.Event,
    release_lock: asyncio.Event,
) -> str:
    """tx1: `service.create`'i tamamlar (satır kilidi + D8 + flush) ama
    `release_lock` sinyali gelene kadar COMMIT ETMEZ — `SELECT … FOR UPDATE`
    kilidi bu süre boyunca AÇIK kalır (commit/rollback'e kadar sürer, `flush`
    kilidi bırakmaz)."""
    async with _SessionFactory() as session:
        actor = await session.get(User, actor_id)
        await service.create(session, actor, project_id, schemas.ProgressPaymentCreate())
        lock_acquired.set()
        await release_lock.wait()
        await session.commit()
        return "created"


#: Bu testin aktörünün geçmesi gereken İKİ kapı: proje görünürlüğü
#: (`projects.visible_projects`) ve silme yetkisi (`core.access.can_delete`).
#: Başka modüle gerek YOKTUR — matrisin tamamı kurulmaz.
_REFERANS_MODUL_ANAHTARLARI = ("projects", "progress_payments")

#: Rol anahtarı bilinçli olarak TESTE ÖZELDİR: `seed_reference_data`'nın
#: ürettiği `system_admin`/`patron` gibi üretim anahtarlarıyla çakışmaz.
_REFERANS_ROL_ANAHTARI = "pp_conc_admin"


async def _referans_kur(session: AsyncSession) -> Role:
    """Bu testin İHTİYACI kadar referans satırı — `seed_reference_data` ÇAĞRILMAZ.

    Eski hâl tüm rol/modül/izin matrisini kurup **COMMIT EDİYORDU** ve temizlik
    bu satırları silmediği için hepsi paylaşılan test veritabanına KALICI
    sızıyordu. Sızıntı bugün genelde görünmüyor çünkü `tests/progress_payments/`
    alfabetik olarak `tests/modules/`ten SONRA koşuyor; sıra terse dönünce
    `tests/modules/test_role_model.py::test_role_key_is_unique` sızan `patron`
    rolüne çarpıp KIRILIYOR (TB1'de fiilen yaşandı).

    Satırlar burada commit EDİLİR — bu testin varlık sebebi iki BAĞIMSIZ
    bağlantının aynı veriyi görmesidir, dolayısıyla commit kaçınılmazdır. Sızıntıyı
    kapatan şey commit'in kalkması değil, yaratılan satırların TAM OLARAK bilinmesi
    ve `_referans_temizle` ile geri alınmasıdır.
    """
    role = Role(key=_REFERANS_ROL_ANAHTARI, name="Eşzamanlılık Test Rolü")
    session.add(role)
    await session.flush()

    for sira, anahtar in enumerate(_REFERANS_MODUL_ANAHTARLARI, start=1):
        module = Module(key=anahtar, name=anahtar, group=ModuleGroup.GENEL, sort_order=sira)
        session.add(module)
        await session.flush()
        session.add(
            RolePermission(
                role_id=role.id,
                module_id=module.id,
                access_level=AccessLevel.admin,
                scope=Scope.all,
            )
        )
    # IZN-B2: kapılar SAYFA hücrelerinden karar verir; `visible_projects` admin istisnası
    # "Projeler" sayfası Düzenler'dir (eşik eski `projects:admin`).
    session.add(
        RolePagePermission(
            role_id=role.id, page_key="genel.projeler", level=PageLevel.edit, can_approve=False
        )
    )
    await session.commit()
    return role


async def _referans_temizle(session: AsyncSession) -> None:
    """`_referans_kur`'un yarattığı HER satırı geri alır (izinler CASCADE ile gider)."""
    await session.execute(delete(Role).where(Role.key == _REFERANS_ROL_ANAHTARI))
    await session.execute(delete(Module).where(Module.key.in_(_REFERANS_MODUL_ANAHTARLARI)))


async def _kurulum() -> tuple[uuid.UUID, uuid.UUID]:
    """Reel commit'li kurulum — referans verisi bu testin KENDİ satırlarıdır."""
    async with _SessionFactory() as session:
        role = await _referans_kur(session)

        project = Project(code="PP-CONC-001", name="Eşzamanlılık Projesi")
        session.add(project)
        await session.flush()
        contract = ProjectContract(
            project_id=project.id,
            contract_no="SZL-2026-CONC",
            amount=Decimal("1000000"),
            advance_pct=Decimal("10"),
            retainage_pct=Decimal("5"),
            vat_pct=Decimal("20"),
        )
        session.add(contract)
        user = User(
            email="concurrency@pp-crud.co",
            password_hash=hash_password("parola1234"),
            full_name="Eşzamanlılık Test",
            role_id=role.id,
        )
        session.add(user)
        await session.commit()
        return project.id, user.id


async def _attempt_create(project_id: uuid.UUID, actor_id: uuid.UUID) -> str:
    async with _SessionFactory() as session:
        actor = await session.get(User, actor_id)
        try:
            await service.create(session, actor, project_id, schemas.ProgressPaymentCreate())
            await session.commit()
            return "created"
        except ConflictError:
            await session.rollback()
            return "conflict"


async def test_iki_esZamanli_onay_yalniz_biri_gecer() -> None:
    """H6: `approve` geçişi hakediş satırını `SELECT … FOR UPDATE` ile kilitler.

    Kanıt deseni `test_iki_esZamanli_olusturma_yalniz_biri_gecer` ile aynıdır ve
    aynı gerekçeye dayanır (H4 denetimi Y3): `asyncio.gather` tek başına iki
    görevi kritik anda KESİŞTİRMEZ. Burada tx1 kilidi alıp TUTARKEN tx2'nin
    ilerleyemediği doğrudan gösterilir; kilit kalkınca tx2 kaydı YENİDEN okur
    (`populate_existing`) ve artık `approved` olduğunu görüp 409 alır.

    Kilit YOKSA ya da durum kilit ALTINDA yeniden okunmuyorsa iki senaryo doğar:
    (a) tx2 beklemez → `not task2.done()` iddiası kırmızı; (b) tx2 eski durumu
    okur → ikisi de "approved" döner ve `approved_by` ikinci aktörle ÜZERİNE
    yazılır — son iddia kırmızı.
    """
    project_id, user_id, payment_id, ikinci_user_id = await _onay_kurulumu()
    lock_acquired = asyncio.Event()
    release_lock = asyncio.Event()
    task1: asyncio.Task[str] | None = None
    task2: asyncio.Task[str] | None = None
    try:
        task1 = asyncio.create_task(
            _attempt_approve_and_hold(payment_id, user_id, lock_acquired, release_lock)
        )
        await asyncio.wait_for(lock_acquired.wait(), timeout=YARIS_TAVANI_SN)

        task2 = asyncio.create_task(_attempt_approve(payment_id, ikinci_user_id))
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine,
            task2,
            mesaj="tx2, tx1 kilidi serbest bırakmadan ilerleyebildi — "
            "`approve` artık hakediş satırını KİLİTLEMİYOR olabilir",
        )
        # Onayı SERİLEŞTİREN ilk kilit sözleşme satırıdır (`visible_payment_locked`:
        # sözleşme → hakediş); tx2 orada bekler.
        # Sorgu 1024 baytta kırpılır (`track_activity_query_size`); kilitte bekleyen SELECT ancak
        # KİLİTLİ okumadır — kilitsiz mutant `INSERT`/`UPDATE`te bekler (TEST-B1, ölçüldü).
        assert bekleyen.startswith("SELECT project_contracts."), bekleyen

        release_lock.set()
        result1 = await asyncio.wait_for(task1, timeout=YARIS_TAVANI_SN)
        result2 = await asyncio.wait_for(task2, timeout=YARIS_TAVANI_SN)
        assert sorted([result1, result2]) == ["approved", "conflict"]

        async with _SessionFactory() as verify_session:
            payment = await verify_session.get(ProgressPayment, payment_id)
            assert payment.status is ProgressPaymentStatus.approved
            # Damga TEK kez atıldı: ikinci aktör üzerine YAZAMADI.
            assert payment.approved_by == user_id
    finally:
        # TEST-B1: iddia ORTADA düşerse tx1 kilidi TUTUYORDU ve `_temizle` süresiz
        # beklerdi (ölçüldü: koşu asılı kaldı). Önce kilit, sonra görevler, sonra temizlik.
        release_lock.set()
        await _gorevleri_bosalt(task1, task2)
        await _onay_temizligi(project_id, user_id, ikinci_user_id)


async def test_gecis_once_sozlesmeyi_sonra_hakedisi_for_update_ile_okur() -> None:
    """Kilitlerin VARLIĞI ve SIRASI — SQL düzeyinde.

    Davranış testleri bu iki şeyi ayırt EDEMEZ ve bunu saklamıyoruz: `approve`
    sözleşme satırını da kilitlediği ve sonunda hakedişi zaten `UPDATE` ettiği
    için, hakediş satırındaki `FOR UPDATE` kaldırılsa bile eşzamanlılık
    testleri yeşil kalır (mutasyon denetimi M11'in bulgusu). Kilit yine de
    spec §7'nin açık gereğidir ve savunmanın YERELLİĞİDİR: yarın kilit almayan
    ikinci bir geçiş yolu ya da salt-okuma bir karar eklenirse koruma ancak
    burada durursa ayakta kalır.

    SIRA da burada sabitlenir: **önce `project_contracts`, sonra
    `progress_payments`** — `service.create` ile AYNI sıra. Ters sırada
    kilitleyen bir yol eklenirse karşılıklı kilitlenme (deadlock) doğar; bu
    iddia o günü kırmızıyla karşılar.
    """
    project_id, user_id, payment_id, ikinci_user_id = await _onay_kurulumu()
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        async with _SessionFactory() as session:
            actor = await session.get(User, user_id)
            await transitions.perform(session, actor, payment_id, transitions.PaymentAction.approve)
            await session.commit()
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)
        await _onay_temizligi(project_id, user_id, ikinci_user_id)

    kilitli = [ifade for ifade in ifadeler if "FOR UPDATE" in ifade]
    sozlesme = [i for i, ifade in enumerate(kilitli) if "FROM project_contracts" in ifade]
    hakedis = [i for i, ifade in enumerate(kilitli) if "FROM progress_payments" in ifade]
    assert sozlesme, f"sözleşme satırı FOR UPDATE ile okunmadı: {kilitli}"
    assert hakedis, f"hakediş satırı FOR UPDATE ile okunmadı: {kilitli}"
    assert sozlesme[0] < hakedis[0], f"kilit sırası ters: {kilitli}"


async def test_gecis_hakedis_satirinin_kilidini_bekler() -> None:
    """Yukarıdaki test kilidin VARLIĞINI kanıtlar ama HANGİ satırın kilitlendiğini
    ayırt edemez: `approve` sözleşme satırını da kilitler ve iki `approve`
    yarışında dışlamayı tek başına o kilit de sağlayabilir (mutasyon denetimi
    M11 bunu gösterdi — `progress_payments` satırındaki `FOR UPDATE` kaldırılınca
    test yeşil kalıyordu).

    Bu test tam olarak HAKEDİŞ SATIRINI hedefler: dışarıdaki bir transaction
    yalnız o satırı `FOR UPDATE` ile tutar (sözleşmeye DOKUNMAZ) ve geçişin
    beklediği gösterilir.

    ## Bu testin ÖLÇMEDİĞİ şey (H6 denetimi O2 — eski docstring YANLIŞTI)

    Eskiden burada "`get_payment_locked`'taki `with_for_update` kalkarsa bu
    iddia kırmızıya döner" yazıyordu; denetim ÖLÇTÜ: DÖNMÜYOR. Kilit kalksa bile
    geçiş sonunda hakediş satırını `UPDATE` eder ve o yazma kilidi dışarıdaki
    `FOR UPDATE` tutucusunu beklemek zorundadır — `not task.done()` yine yeşil
    kalır; yalnız SQL-metin testi (`…for_update_ile_okur`) kırılır.

    Yani bu test "geçiş hakediş satırının kilidine TAKILIR" davranışını sabitler
    (ki bu da §7'nin gereğidir), `FOR UPDATE`'in KENDİSİNİ değil.

    T4b denetimi bunu YENİDEN ölçtü ve genişletti: yalnız `get_payment_locked`
    değil, `get_contract_locked` de kaldırılınca — hatta İKİSİ BİRDEN
    kaldırılınca — bu test izole koşuda 3/3 YEŞİL kalıyor. Yani bu dosyada
    hiçbir kilit mutasyonunu öldürmez; kilitlerin regresyon bekçileri
    `…_olusturma_yalniz_biri_gecer`, `…_onay_yalniz_biri_gecer`,
    `test_silinirken_…` ve SQL-metin testidir. Bu test BİLEREK korunur (davranış
    sabiti), bekçi diye SAYILMAZ.

    ## Davranışsal ayrım neden KURULAMIYOR (denendi, ölçüldü)

    O2 denetimi "sözleşmesi olmayan projede iki eşzamanlı `approve`" senaryosunu
    önerdi — orada sıraya sokan sözleşme kilidi olmadığı için hakediş kilidi TEK
    koruma olurdu. Bu senaryo ŞEMA DÜZEYİNDE İMKÂNSIZ: `models.py:68-72`,
    `progress_payments.project_id` FK'sini `project_contracts.project_id`'ye
    bağlar — sözleşmesiz projede hakediş satırı INSERT EDİLEMEZ
    (`ForeignKeyViolationError`, doğrudan DB kurulumuyla dahi). Dolayısıyla HER
    hakedişin sözleşmesi vardır ve her geçiş önce o satırı kilitler; hakediş
    satırındaki `FOR UPDATE` bugün erişilebilir hiçbir yolda TEK koruma değildir.

    Kalan koruma, kilidin YERELLİĞİDİR (yukarıdaki SQL-metin testi bunu bekçiler):
    yarın sözleşme kilidini almayan ikinci bir geçiş yolu eklenirse davranışsal
    fark ancak o zaman doğar — ve o gün bu testler yerine yeni yolun kendi
    yarış testi yazılmalıdır.
    """
    project_id, user_id, payment_id, ikinci_user_id = await _onay_kurulumu()
    task: asyncio.Task[str] | None = None
    try:
        async with _SessionFactory() as tutan:
            await tutan.execute(
                text("SELECT id FROM progress_payments WHERE id = :id FOR UPDATE"),
                {"id": payment_id},
            )

            task = asyncio.create_task(_attempt_approve(payment_id, user_id))
            bekleyen = await kilitte_bekleyen_sorgu(
                test_engine,
                task,
                mesaj="geçiş, hakediş satırı DIŞARIDAN kilitliyken ilerledi — "
                "`get_payment_locked` artık `SELECT … FOR UPDATE` yapmıyor olabilir",
            )
            # Sorgu 1024 baytta kırpılır; kilitte bekleyen SELECT ancak KİLİTLİ okumadır —
            # kilitsiz mutant `UPDATE`te bekler (TEST-B1, ölçüldü).
            assert bekleyen.startswith("SELECT progress_payments."), bekleyen
            await tutan.rollback()

        assert await asyncio.wait_for(task, timeout=YARIS_TAVANI_SN) == "approved"
    finally:
        # TEST-B1: iddia düşerse görev başıboş kalmasın (temizlikten ÖNCE boşalt).
        await _gorevleri_bosalt(task)
        await _onay_temizligi(project_id, user_id, ikinci_user_id)


async def test_silinirken_esZamanli_onay_kazanirsa_409_alir() -> None:
    """H8 denetimi K1 (KRİTİK) — `delete_payment` artık `_visible_payment`
    (kilitsiz) değil `visible_payment_locked` kullanır (spec §7.1, §7 eşzamanlılık
    notu). Düzeltmeden ÖNCE kanıtlanan yarış: tx-A kilitsiz okur (pending_approval),
    tx-B eşzamanlı `approve` FOR UPDATE + commit ile geçer, tx-A hâlâ eski
    `pending_approval` görüşüyle DELETE'i yürütürdü — `approved`/`paid` kaydı
    admin dahil kimse silemez garantisi (K8) TOCTOU ile atlatılırdı.

    Bu test tek-session `client`/`seeded_db` fixture'ıyla KURULAMAZ (`db_session`
    dış transaction'ı asla commit etmez, iki görev aynı bağlantıyı paylaşır,
    gerçek satır kilidi asla test edilmez) — bu yüzden `test_iki_esZamanli_onay_*`
    ile AYNI bariyerli, iki-bağımsız-bağlantılı desen kullanılır.

    Senaryo: tx-B (`approve`) kilidi ALIP TUTARKEN tx-A (`delete_payment`) aynı
    kilit zincirinde (sözleşme → hakediş, `visible_payment_locked`) BEKLER; tx-B
    commit edip kilidi bırakınca tx-A devam eder, satırı ARTIK `approved` olarak
    görür ve katman-1 kontrolüne (§7.1/1) takılıp 409 `PAYMENT_NOT_DELETABLE`
    alır — satır DB'de duruyor kalır.
    """
    project_id, user_id, payment_id, ikinci_user_id = await _onay_kurulumu()
    lock_acquired = asyncio.Event()
    release_lock = asyncio.Event()
    task_approve: asyncio.Task[str] | None = None
    task_delete: asyncio.Task[str] | None = None
    try:
        task_approve = asyncio.create_task(
            _attempt_approve_and_hold(payment_id, user_id, lock_acquired, release_lock)
        )
        await asyncio.wait_for(lock_acquired.wait(), timeout=YARIS_TAVANI_SN)

        task_delete = asyncio.create_task(_attempt_delete(payment_id, ikinci_user_id))
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine,
            task_delete,
            mesaj="silme, onay kilidi serbest bırakılmadan ilerleyebildi — "
            "`delete_payment` artık `visible_payment_locked` KULLANMIYOR olabilir "
            "(K1 regresyonu, H8 denetimi)",
        )
        # Silme de `visible_payment_locked`tan geçer: ilk kilit sözleşme satırı.
        # Sorgu 1024 baytta kırpılır (`track_activity_query_size`); kilitte bekleyen SELECT ancak
        # KİLİTLİ okumadır — kilitsiz mutant `INSERT`/`UPDATE`te bekler (TEST-B1, ölçüldü).
        assert bekleyen.startswith("SELECT project_contracts."), bekleyen

        release_lock.set()
        onay_sonucu = await asyncio.wait_for(task_approve, timeout=YARIS_TAVANI_SN)
        silme_sonucu = await asyncio.wait_for(task_delete, timeout=YARIS_TAVANI_SN)

        assert onay_sonucu == "approved"
        assert silme_sonucu == "conflict", (
            "silme, tazelenmiş `approved` durumunu GÖRMEDİ — K8 katman-1 TOCTOU "
            "ile atlatılmış olabilir"
        )

        async with _SessionFactory() as verify_session:
            payment = await verify_session.get(ProgressPayment, payment_id)
            assert payment is not None, "approved/paid kayıt yarışta silinmiş — K8 ihlali"
            assert payment.status is ProgressPaymentStatus.approved
    finally:
        # TEST-B1: iddia ORTADA düşerse tx1 kilidi TUTUYORDU ve `_temizle` süresiz
        # beklerdi (ölçüldü: koşu asılı kaldı). Önce kilit, sonra görevler, sonra temizlik.
        release_lock.set()
        await _gorevleri_bosalt(task_approve, task_delete)
        await _onay_temizligi(project_id, user_id, ikinci_user_id)


async def _attempt_delete(payment_id: uuid.UUID, actor_id: uuid.UUID) -> str:
    async with _SessionFactory() as session:
        actor = await session.get(User, actor_id)
        try:
            await service.delete_payment(session, actor, payment_id)
            await session.commit()
            return "deleted"
        except ConflictError:
            await session.rollback()
            return "conflict"


async def _attempt_approve_and_hold(
    payment_id: uuid.UUID,
    actor_id: uuid.UUID,
    lock_acquired: asyncio.Event,
    release_lock: asyncio.Event,
) -> str:
    async with _SessionFactory() as session:
        actor = await session.get(User, actor_id)
        await transitions.perform(session, actor, payment_id, transitions.PaymentAction.approve)
        lock_acquired.set()
        await release_lock.wait()
        await session.commit()
        return "approved"


async def _attempt_approve(payment_id: uuid.UUID, actor_id: uuid.UUID) -> str:
    async with _SessionFactory() as session:
        actor = await session.get(User, actor_id)
        try:
            await transitions.perform(session, actor, payment_id, transitions.PaymentAction.approve)
            await session.commit()
            return "approved"
        except ConflictError:
            await session.rollback()
            return "conflict"


async def _onay_kurulumu() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Reel commit'li kurulum: sözleşme + `pending_approval` hakediş + İKİ aktör.

    İki ayrı aktör bilinçlidir: `approved_by`'ın hangi transaction tarafından
    damgalandığı ancak böyle ayırt edilebilir.
    """
    async with _SessionFactory() as session:
        role = await _referans_kur(session)
        project = Project(code="PP-CONC-002", name="Onay Eşzamanlılık Projesi")
        session.add(project)
        await session.flush()
        session.add(
            ProjectContract(
                project_id=project.id,
                contract_no="SZL-2026-CONC2",
                amount=Decimal("1000000"),
                advance_pct=Decimal("10"),
                retainage_pct=Decimal("5"),
                vat_pct=Decimal("20"),
            )
        )
        birinci = User(
            email="onay1@pp-crud.co",
            password_hash=hash_password("parola1234"),
            full_name="Onay Aktörü 1",
            role_id=role.id,
        )
        ikinci = User(
            email="onay2@pp-crud.co",
            password_hash=hash_password("parola1234"),
            full_name="Onay Aktörü 2",
            role_id=role.id,
        )
        session.add_all([birinci, ikinci])
        await session.flush()
        payment = ProgressPayment(
            project_id=project.id,
            sequence_no=1,
            status=ProgressPaymentStatus.pending_approval,
            period_year=2026,
            period_month=3,
            vat_pct=Decimal("20"),
            advance_pct=Decimal("10"),
            retainage_pct=Decimal("5"),
            created_by=birinci.id,
        )
        session.add(payment)
        await session.commit()
        return project.id, birinci.id, payment.id, ikinci.id


async def _onay_temizligi(
    project_id: uuid.UUID, user_id: uuid.UUID, ikinci_user_id: uuid.UUID
) -> None:
    async with _SessionFactory() as session:
        await session.execute(
            delete(ProgressPayment).where(ProgressPayment.project_id == project_id)
        )
        await session.execute(
            delete(ProjectContract).where(ProjectContract.project_id == project_id)
        )
        await session.execute(delete(Project).where(Project.id == project_id))
        await session.execute(delete(User).where(User.id.in_([user_id, ikinci_user_id])))
        await _referans_temizle(session)
        await session.commit()


async def _gorevleri_bosalt(*gorevler: "asyncio.Task[str] | None") -> None:
    """TEST-B1: görevleri HER YOLDA kapat — temizlik bağlantıyı görev çalışırken çekmesin ve
    asıl iddia hatası asyncpg "another operation is in progress" gürültüsüyle ezilmesin."""
    for gorev in gorevler:
        if gorev is None:
            continue
        if not gorev.done():
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(gorev), timeout=YARIS_TAVANI_SN)
        if not gorev.done():
            gorev.cancel()
        with contextlib.suppress(BaseException):
            await gorev


async def _temizle(project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    async with _SessionFactory() as session:
        await session.execute(
            delete(ProgressPayment).where(ProgressPayment.project_id == project_id)
        )
        await session.execute(
            delete(ProjectContract).where(ProjectContract.project_id == project_id)
        )
        await session.execute(delete(Project).where(Project.id == project_id))
        await session.execute(delete(User).where(User.id == user_id))
        await _referans_temizle(session)
        await session.commit()


@pytest.fixture(autouse=True)
async def _mu3d_esleme() -> None:
    """🔴 MU-3D — conftest'teki AYNI ADLI autouse fixture'ı GÖLGELER (kapatır).

    Bu dosya `seeded_db`yi BİLEREK KULLANMAZ (modül docstring'i): iki bağımsız
    bağlantı açar ve GERÇEKTEN COMMIT eder. Conftest'in eşleme fixture'ı
    `seeded_db`ye bağlıdır ve autouse olduğu için onu BU dosyaya da zorla
    sokuyordu; `seed_reference_data`nın commit EDİLMEMİŞ `modules` satırları
    ile buradaki gerçek commit aynı benzersiz anahtarda çakıştı ve tam küme
    koşusu bir DEADLOCK'ta ASILI KALDI — ölçüldü: `pg_stat_activity`de
    `wait_event_type='Lock'`, `wait_event='transactionid'`.

    🔴 Eşlemenin burada BULUNMAMASI bir eksiklik değildir: bu dosyanın
    hakedişleri SATIRSIZDIR, fişin tabanı sıfırdır ve `post_progress_payment`
    `None` dönerek fişi HİÇ AÇMAZ. Yani onay yolu buradan fişleme koduna
    girmez ve eşleme aranmaz.
    """
    return None


# --------------------------------------------------------------------------- #
# 🔴 KRIT-HAKEDIS K5 — `PUT …/lines` DURUM KAPISININ TOCTOU YARIŞI
# --------------------------------------------------------------------------- #


async def _lock_bekleyen_pid(pid_disinda: set[int]) -> int | None:
    """`pg_stat_activity`de gerçekten SATIR KİLİDİ bekleyen bir arka planın pid'i.

    🔴 Bariyer SABİT SÜRELİ DEĞİLDİR (`asyncio.sleep(0.5)` bu depoda FLAKY
    ilan edildi). Beklemenin TÜRÜ de doğrulanır: `wait_event_type='Lock'` +
    `wait_event IN ('transactionid','tuple')` — yani gerçekten bir SATIR
    kilidinde bekliyor. Yalnız "task bitmedi" iddiası, görevin başka bir
    sebeple (ör. yavaş sorgu, havuz beklemesi) ilerlememesini de KİLİT sanardı.
    """
    async with _SessionFactory() as gozlemci:
        rows = (
            await gozlemci.execute(
                text(
                    "SELECT pid, wait_event FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' "
                    "AND wait_event IN ('transactionid', 'tuple')"
                )
            )
        ).all()
    for pid, _wait_event in rows:
        if pid not in pid_disinda:
            return pid
    return None


async def _kilitte_bekledigini_dogrula(task: asyncio.Task, *, mesaj: str) -> None:
    """Görev SATIR KİLİDİNDE bekleyene kadar YOKLAR; bekleme yoksa KIRMIZI.

    🔴 POZİTİF KONTROL: `save_lines`ten kilit kaldırıldığında görev hiç
    beklemez ve buraya `TimeoutError` DEĞİL, `task.done()` ile düşer — mesaj
    tam olarak o mutantı adlandırır.
    """
    # FIX-B3: sayaçla (100 × 0,05 ≈ 5 sn) DEĞİL duvar saatiyle, ortak geniş tavanla.
    loop = asyncio.get_running_loop()
    son = loop.time() + YARIS_TAVANI_SN
    while loop.time() < son:
        if task.done():
            raise AssertionError(mesaj)
        if await _lock_bekleyen_pid(set()) is not None:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{YARIS_TAVANI_SN} sn içinde SATIR KİLİDİ beklemesi GÖRÜLMEDİ: {mesaj}")


async def _attempt_save_lines(payment_id: uuid.UUID, actor_id: uuid.UUID) -> str:
    """`PUT …/lines` — BOŞ gövde, yani "tüm satırları sil".

    Boş gövde bilinçlidir: kapı delinirse hasar ÖLÇÜLEBİLİR olur (onaylanmış
    ve fişlenmiş bir hakedişin satırları tamamen boşalır ve ekrandaki brüt
    sıfıra düşerken fişteki tutar donmuş kalır).
    """
    async with _SessionFactory() as session:
        actor = await session.get(User, actor_id)
        try:
            await service.save_lines(session, actor, payment_id, schemas.ProgressPaymentLinesSave())
            await session.commit()
            return "saved"
        except ConflictError:
            await session.rollback()
            return "conflict"


async def test_satir_yazma_esZamanli_onay_kazanirsa_409_alir() -> None:
    """🔴 K5 — `save_lines`in durum kapısı KİLİTSİZ okunuyordu (TOCTOU).

    Kapının kendisi VARDI (`status != draft → 409`) ve düz *"onaydan sonra
    satır değişir"* iddiası YANLIŞTI. Gerçek açık YARIŞTI: durum kilitsiz
    okunduğu için eşzamanlı bir `approve` ile `PUT /lines` BİRBİRİNİ GÖRMEDEN
    geçebiliyordu — tx-A `draft` okur, tx-B `approve` eder ve commit eder,
    tx-A hâlâ eski görüşüyle satırları yazar. Sonuç: ONAYLANMIŞ VE FİŞLENMİŞ
    bir hakedişin satırları onaydan SONRA değişir; fişin tutarı donmuş
    kaldığı için mizan ile ekran SESSİZCE ayrışır.

    🔴 Taşeron ikizi (`subcontractor_progress_payments.service.save_lines`) bu
    kilidi ZATEN alıyordu (`visible_payment_locked`) — iki aile ayrışmıştı ve
    bu test simetriyi sabitler.

    Desen `test_silinirken_esZamanli_onay_kazanirsa_409_alir` ile aynıdır;
    bariyer ondan FARKLI olarak sabit süreli değil `pg_stat_activity`
    yoklamalıdır (kilidin TÜRÜ de doğrulanır).
    """
    project_id, user_id, payment_id, ikinci_user_id = await _onay_kurulumu()
    release_lock = asyncio.Event()
    gorevler: list[asyncio.Task] = []
    try:
        lock_acquired = asyncio.Event()

        task_approve = asyncio.create_task(
            _attempt_approve_and_hold(payment_id, user_id, lock_acquired, release_lock)
        )
        gorevler.append(task_approve)
        await asyncio.wait_for(lock_acquired.wait(), timeout=YARIS_TAVANI_SN)

        task_lines = asyncio.create_task(_attempt_save_lines(payment_id, ikinci_user_id))
        gorevler.append(task_lines)
        try:
            # 🔴 `release_lock` HER HÂLDE bırakılır: iddia kırmızıya döndüğünde
            #    de tx1'in `FOR UPDATE` kilidi açık kalırsa `finally`deki
            #    temizlik SONSUZA KADAR bekler ve kırmızı bir kusur, ASILI bir
            #    teste dönüşürdü (mutant koşusu böyle ölçülemez hâle gelirdi).
            await _kilitte_bekledigini_dogrula(
                task_lines,
                mesaj=(
                    "`PUT /lines` onay kilidi serbest bırakılmadan ilerledi — "
                    "`save_lines` durum kapısını KİLİTSİZ okuyor (K5 TOCTOU)"
                ),
            )
        finally:
            release_lock.set()

        onay_sonucu = await asyncio.wait_for(task_approve, timeout=YARIS_TAVANI_SN)
        satir_sonucu = await asyncio.wait_for(task_lines, timeout=YARIS_TAVANI_SN)

        assert onay_sonucu == "approved"
        assert satir_sonucu == "conflict", (
            "satır yazma, tazelenmiş `approved` durumunu GÖRMEDİ — onaylanmış "
            "hakedişin satırları TOCTOU ile değiştirildi"
        )

        async with _SessionFactory() as verify_session:
            payment = await verify_session.get(ProgressPayment, payment_id)
            assert payment.status is ProgressPaymentStatus.approved
    finally:
        release_lock.set()
        await asyncio.gather(*gorevler, return_exceptions=True)
        await _onay_temizligi(project_id, user_id, ikinci_user_id)

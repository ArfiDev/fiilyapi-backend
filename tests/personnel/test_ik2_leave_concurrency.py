"""İK-2 onay yolu — eşzamanlılık: `SELECT … FOR UPDATE` altında K3/K4/K5.

FINAL REVIEW bulgusunun regresyon kilidi. Düzeltmeden ÖNCE `approve_leave_request`
hiçbir satır kilidi almıyordu; aynı personelin İKİ bekleyen talebi eşzamanlı
onaylandığında:

* ikisi de AYNI `used` toplamını okuyup **hak aşımı eşiğini (K5) atlatıyordu**,
* ikisi de birbirinin henüz commit'lenmemiş onayını göremediği için **çakışma
  kontrolünü (K3) atlatıyor**, üst üste binen iki onaylı izin doğuruyordu,
* aynı talebe eşzamanlı onay+red **karar damgasını (K4) ezebiliyordu**.

Neden `client`/`seeded_db` KULLANILMAZ: `tests/conftest.py`'deki `db_session`
her testi TEK bir bağlantı üzerinde SAVEPOINT'e sarar ve dış transaction'ı asla
gerçekten COMMIT ETMEZ — o session üzerinde iki `asyncio.gather` görevi de AYNI
bağlantıyı paylaşır, gerçek satır kilidi test EDİLEMEZ. Bu dosya
`tests/progress_payments/test_concurrency.py` desenini birebir izler: `test_engine`
üzerinden İKİ BAĞIMSIZ bağlantı, gerçek commit, sonunda gerçek temizlik.

Bariyer (`asyncio.Event`) BİLİNÇLİDİR: çıplak `asyncio.gather` iki görevi kritik
anda KESİŞTİRMEZ (H4 denetimi Y3 dersi) — yarış penceresi hiç açılmayabilir.
Burada tx1 kilidi ALIP TUTARKEN tx2'nin bloke olduğu doğrudan kanıtlanır.
"""

import asyncio
import contextlib
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import timezone
from app.core.errors import ConflictError
from app.core.security import hash_password
from app.modules.personnel import repository, service
from app.modules.personnel.models import (
    LeaveBalance,
    LeaveRequest,
    LeaveStatus,
    LeaveType,
    Personnel,
)
from app.modules.personnel.schemas import LeaveBalanceUpdate, LeaveRejectRequest
from app.modules.roles.models import Role
from app.modules.site_diary.models import WorkerSource
from app.modules.users.models import User
from tests._yaris import YARIS_TAVANI_SN, kilitte_bekleyen_sorgu
from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio

_SessionFactory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


def _bugun() -> date:
    # Cagri aninda hesaplanir (TMP-FIX #167): import aninda dondurulmaz.
    return timezone.today()


def _yil() -> int:
    return _bugun().year


# ~2 yıl 2 ay kıdem → 4857 birinci kademe: yıllık hak 14 gün.
def _kidemli_giris() -> date:
    return _bugun() - timedelta(days=800)


#: Rol anahtarı TESTE ÖZELDİR: `seed_reference_data`'nın ürettiği üretim
#: anahtarlarıyla (`system_admin`, `patron`) çakışmaz — bu dosya GERÇEKTEN
#: commit ettiği için sızıntı ancak yaratılan satırların tam bilinmesiyle kapanır.
_ROL_ANAHTARI = "ik2_conc_admin"
_EPOSTALAR = ("ik2-onay1@conc.co", "ik2-onay2@conc.co")


class _Kurulum:
    """Bir senaryonun ürettiği ve temizlemesi gereken kimlikler."""

    def __init__(
        self,
        personnel_id: uuid.UUID,
        type_id: uuid.UUID,
        request_ids: list[uuid.UUID],
        actor_ids: list[uuid.UUID],
        role_id: uuid.UUID,
    ) -> None:
        self.personnel_id = personnel_id
        self.type_id = type_id
        self.request_ids = request_ids
        self.actor_ids = actor_ids
        self.role_id = role_id


async def _kur(
    *,
    deducts: bool,
    araliklar: list[tuple[date, date]],
    sahip_aktor: int | None = None,
) -> _Kurulum:
    """Reel commit'li kurulum: kıdemli personel + bir izin tipi + N bekleyen talep
    + İKİ aktör.

    İki ayrı aktör bilinçlidir: karar damgasının (`decided_by`) HANGİ transaction
    tarafından atıldığı ancak böyle ayırt edilebilir.

    `sahip_aktor` (İK-2.2): personel kaydını AKTÖRLERDEN BİRİNE bağlar
    (`Personnel.user_id`). Varsayılanı `None`dır ve o hâlde kurulum ESKİSİYLE
    BİREBİR AYNIDIR — mevcut beş test HİÇ etkilenmez.

    🔴 Parametre ham bir `user_id` DEĞİL, aktör SIRA NUMARASIDIR: aktörler bu
    fonksiyonun İÇİNDE doğar, çağıran onların kimliğini önceden bilemez. Geri
    çekme yolu SAHİPLİK ister (sahip olmayan 404 alır ve yarış hiç ölçülemezdi),
    bu yüzden köprü kurulumda kurulmak zorundadır.
    """
    async with _SessionFactory() as session:
        role = Role(key=_ROL_ANAHTARI, name="İzin Eşzamanlılık Rolü")
        session.add(role)
        await session.flush()

        personnel = Personnel(
            full_name="Eşzamanlılık Personeli",
            source=WorkerSource.company,
            hire_date=_kidemli_giris(),
        )
        leave_type = LeaveType(
            name="Eşzamanlılık Tipi",
            deducts_from_annual=deducts,
            sort_order=99,
        )
        session.add_all([personnel, leave_type])
        aktorler = [
            User(
                email=eposta,
                password_hash=hash_password("parola1234"),
                full_name=f"Karar Aktörü {sira}",
                role_id=role.id,
            )
            for sira, eposta in enumerate(_EPOSTALAR, start=1)
        ]
        session.add_all(aktorler)
        await session.flush()

        if sahip_aktor is not None:
            personnel.user_id = aktorler[sahip_aktor].id
            await session.flush()

        talepler = [
            LeaveRequest(
                personnel_id=personnel.id,
                leave_type_id=leave_type.id,
                start_date=baslangic,
                end_date=bitis,
                days=(bitis - baslangic).days + 1,
                status=LeaveStatus.pending,
            )
            for baslangic, bitis in araliklar
        ]
        session.add_all(talepler)
        await session.flush()
        await session.commit()
        return _Kurulum(
            personnel_id=personnel.id,
            type_id=leave_type.id,
            request_ids=[t.id for t in talepler],
            actor_ids=[a.id for a in aktorler],
            role_id=role.id,
        )


async def _gorevleri_bosalt(*gorevler: asyncio.Task | None) -> None:
    """Temizlikten ÖNCE tüm görevleri sonlandırır — MUTASYON DENETİMİ İÇİN ŞART.

    Kilit kaldırıldığında (mutasyon) `not task2.done()` iddiası kırmızıya döner ve
    test gövdesi ORTADA terk edilir; `task1` hâlâ commit etmemiş bir transaction
    içinde satır kilidi TUTUYOR olur. Bu boşaltma olmadan `_temizle`nin DELETE'i o
    kilidi SONSUZA DEK bekler ve kırmızı bir test SONSUZ ASKIYA dönüşür — mutasyon
    kanıtı okunamaz hâle gelirdi.
    """
    for gorev in gorevler:
        if gorev is None:
            continue
        # Biten görevde `cancel()` etkisizdir (yeşil yol); asılı kalanda ise
        # `async with` çıkışı session'ı kapatır ve tuttuğu kilitleri BIRAKIR.
        gorev.cancel()
        with contextlib.suppress(BaseException):
            await gorev


async def _temizle(kurulum: _Kurulum) -> None:
    """`_kur`un yarattığı HER satırı geri alır (talepler personelle CASCADE gider)."""
    async with _SessionFactory() as session:
        await session.execute(
            delete(LeaveBalance).where(LeaveBalance.personnel_id == kurulum.personnel_id)
        )
        await session.execute(
            delete(LeaveRequest).where(LeaveRequest.personnel_id == kurulum.personnel_id)
        )
        await session.execute(delete(Personnel).where(Personnel.id == kurulum.personnel_id))
        await session.execute(delete(LeaveType).where(LeaveType.id == kurulum.type_id))
        await session.execute(delete(User).where(User.id.in_(kurulum.actor_ids)))
        await session.execute(delete(Role).where(Role.id == kurulum.role_id))
        await session.commit()


async def _onayla_ve_tut(
    request_id: uuid.UUID,
    actor_id: uuid.UUID,
    kilit_alindi: asyncio.Event,
    kilidi_birak: asyncio.Event,
) -> str:
    """tx1: `approve`ı tamamlar (kilit + denetimler + flush) ama sinyal gelene
    kadar COMMIT ETMEZ — `SELECT … FOR UPDATE` kilidi bu süre boyunca AÇIK kalır
    (`flush` kilidi BIRAKMAZ, yalnız commit/rollback bırakır)."""
    async with _SessionFactory() as session:
        actor = await session.get(User, actor_id)
        await service.approve_leave_request(session, actor, request_id, today=_bugun())
        kilit_alindi.set()
        await kilidi_birak.wait()
        await session.commit()
        return "approved"


async def _onayla(request_id: uuid.UUID, actor_id: uuid.UUID) -> str:
    async with _SessionFactory() as session:
        actor = await session.get(User, actor_id)
        try:
            await service.approve_leave_request(session, actor, request_id, today=_bugun())
            await session.commit()
            return "approved"
        except ConflictError:
            await session.rollback()
            return "conflict"


async def _reddet(request_id: uuid.UUID, actor_id: uuid.UUID) -> str:
    async with _SessionFactory() as session:
        actor = await session.get(User, actor_id)
        try:
            await service.reject_leave_request(
                session, actor, request_id, LeaveRejectRequest(reason="yarış testi")
            )
            await session.commit()
            return "rejected"
        except ConflictError:
            await session.rollback()
            return "conflict"


async def _geri_cek(request_id: uuid.UUID, actor_id: uuid.UUID) -> str:
    """İK-2.2 — `_reddet`in birebir kardeşi. Aktör talebin SAHİBİ olmalıdır,
    yoksa servis 404 (`NotFoundError`) atar ve yarış hiç ölçülmezdi."""
    async with _SessionFactory() as session:
        actor = await session.get(User, actor_id)
        try:
            await service.withdraw_leave_request(session, actor, request_id)
            await session.commit()
            return "withdrawn"
        except ConflictError:
            await session.rollback()
            return "conflict"


async def test_iki_esZamanli_onay_hak_asimini_atlatamaz() -> None:
    """🔴 ASIL BULGU (K5): 14 günlük hakka karşı İKİ ayrı 10 günlük talep.

    Talepler ÇAKIŞMAZ (Mart / Haziran) — dolayısıyla K3 hiçbirini durduramaz ve
    tek koruma hak aşımı eşiğidir. Kilit YOKKEN ikisi de `used=0` okur, ikisi de
    `10 <= 14` görüp geçer ve personel 20 gün izin kullanmış olur. Kilit VARKEN
    tx2 sıraya girer, tx1'in commit'ini görür (`used=10`, kalan 4) ve `10 > 4`
    diyerek 409 alır.
    """
    kurulum = await _kur(
        deducts=True,
        araliklar=[
            (date(_yil(), 3, 1), date(_yil(), 3, 10)),
            (date(_yil(), 6, 1), date(_yil(), 6, 10)),
        ],
    )
    kilit_alindi = asyncio.Event()
    kilidi_birak = asyncio.Event()
    task1: asyncio.Task | None = None
    task2: asyncio.Task | None = None
    try:
        task1 = asyncio.create_task(
            _onayla_ve_tut(kurulum.request_ids[0], kurulum.actor_ids[0], kilit_alindi, kilidi_birak)
        )
        await asyncio.wait_for(kilit_alindi.wait(), timeout=YARIS_TAVANI_SN)

        task2 = asyncio.create_task(_onayla(kurulum.request_ids[1], kurulum.actor_ids[1]))
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine,
            task2,
            mesaj="tx2, tx1 kilidi serbest bırakmadan ilerleyebildi — `approve` artık "
            "personel satırını KİLİTLEMİYOR olabilir (K5 yarışı yeniden açık)",
        )
        assert "FROM personnel WHERE" in bekleyen and bekleyen.endswith("FOR UPDATE"), bekleyen

        kilidi_birak.set()
        sonuc1 = await asyncio.wait_for(task1, timeout=YARIS_TAVANI_SN)
        sonuc2 = await asyncio.wait_for(task2, timeout=YARIS_TAVANI_SN)
        assert sorted([sonuc1, sonuc2]) == ["approved", "conflict"]

        async with _SessionFactory() as dogrula:
            onayli = (
                (
                    await dogrula.execute(
                        select(LeaveRequest).where(
                            LeaveRequest.personnel_id == kurulum.personnel_id,
                            LeaveRequest.status == LeaveStatus.approved,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(onayli) == 1, "hak aşımı eşiği atlatıldı: iki izin de onaylandı"
            kullanilan = await repository.sum_deductible_approved_days(
                dogrula, kurulum.personnel_id, _yil()
            )
            assert kullanilan == 10, f"kullanılan gün 14 günlük hakkı aştı: {kullanilan}"
    finally:
        await _gorevleri_bosalt(task1, task2)
        await _temizle(kurulum)


async def test_iki_esZamanli_onay_cakismayi_atlatamaz() -> None:
    """K3: ÜST ÜSTE BİNEN iki talep, `deducts_from_annual=False` tiple.

    Tip yıllık haktan düşmediği için K5 kapısı HİÇ koşmaz — çakışma tek korumadır
    ve kilidi ölçen şey odur. Kilitsiz hâlde ikisi de "çakışan ONAYLI izin yok"
    okur (rakibin onayı henüz commit'li değildir) ve bir gün iki izne birden ait
    olur.
    """
    kurulum = await _kur(
        deducts=False,
        araliklar=[
            (date(_yil(), 4, 5), date(_yil(), 4, 12)),
            (date(_yil(), 4, 8), date(_yil(), 4, 15)),
        ],
    )
    kilit_alindi = asyncio.Event()
    kilidi_birak = asyncio.Event()
    task1: asyncio.Task | None = None
    task2: asyncio.Task | None = None
    try:
        task1 = asyncio.create_task(
            _onayla_ve_tut(kurulum.request_ids[0], kurulum.actor_ids[0], kilit_alindi, kilidi_birak)
        )
        await asyncio.wait_for(kilit_alindi.wait(), timeout=YARIS_TAVANI_SN)

        task2 = asyncio.create_task(_onayla(kurulum.request_ids[1], kurulum.actor_ids[1]))
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine,
            task2,
            mesaj="tx2 beklemedi — `approve` personel satırını KİLİTLEMİYOR olabilir "
            "(K3 yarışı yeniden açık)",
        )
        assert "FROM personnel WHERE" in bekleyen and bekleyen.endswith("FOR UPDATE"), bekleyen

        kilidi_birak.set()
        sonuc1 = await asyncio.wait_for(task1, timeout=YARIS_TAVANI_SN)
        sonuc2 = await asyncio.wait_for(task2, timeout=YARIS_TAVANI_SN)
        assert sorted([sonuc1, sonuc2]) == ["approved", "conflict"]

        async with _SessionFactory() as dogrula:
            onayli = (
                (
                    await dogrula.execute(
                        select(LeaveRequest).where(
                            LeaveRequest.personnel_id == kurulum.personnel_id,
                            LeaveRequest.status == LeaveStatus.approved,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(onayli) == 1, "çakışma kontrolü atlatıldı: üst üste binen iki onaylı izin"
    finally:
        await _gorevleri_bosalt(task1, task2)
        await _temizle(kurulum)


async def test_ayni_talebe_esZamanli_onay_ve_red_tek_damga_birakir() -> None:
    """K4: AYNI talebe eşzamanlı onay + red — damga TEK kez atılır.

    `_assert_decidable` yalnız `pending` talebi karara açar; kilit olmadan ikinci
    transaction birincinin damgasını GÖREMEDEN geçer ve `status`/`decided_by`
    üzerine yazardı. `populate_existing=True` ile durum kilit ALTINDA yeniden
    okunur — ikinci istek 409 alır.
    """
    kurulum = await _kur(deducts=True, araliklar=[(date(_yil(), 5, 4), date(_yil(), 5, 8))])
    talep_id = kurulum.request_ids[0]
    kilit_alindi = asyncio.Event()
    kilidi_birak = asyncio.Event()
    task1: asyncio.Task | None = None
    task2: asyncio.Task | None = None
    try:
        task1 = asyncio.create_task(
            _onayla_ve_tut(talep_id, kurulum.actor_ids[0], kilit_alindi, kilidi_birak)
        )
        await asyncio.wait_for(kilit_alindi.wait(), timeout=YARIS_TAVANI_SN)

        task2 = asyncio.create_task(_reddet(talep_id, kurulum.actor_ids[1]))
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine,
            task2,
            mesaj="red, onay kilidi serbest bırakılmadan ilerleyebildi — `reject` artık "
            "karar satırını KİLİTLEMİYOR olabilir (K4 damga yarışı)",
        )
        assert "FROM personnel WHERE" in bekleyen and bekleyen.endswith("FOR UPDATE"), bekleyen

        kilidi_birak.set()
        sonuc1 = await asyncio.wait_for(task1, timeout=YARIS_TAVANI_SN)
        sonuc2 = await asyncio.wait_for(task2, timeout=YARIS_TAVANI_SN)
        assert sonuc1 == "approved"
        assert sonuc2 == "conflict", "red, tazelenmiş `approved` durumunu GÖRMEDİ"

        async with _SessionFactory() as dogrula:
            talep = await dogrula.get(LeaveRequest, talep_id)
            assert talep.status is LeaveStatus.approved
            assert talep.decided_by == kurulum.actor_ids[0], "karar damgası ÜZERİNE YAZILDI"
            assert talep.reject_reason is None
    finally:
        await _gorevleri_bosalt(task1, task2)
        await _temizle(kurulum)


async def test_ayni_talebe_esZamanli_onay_ve_geri_cekme_tek_damga_birakir() -> None:
    """İK-2.2 K: AYNI talebe eşzamanlı ONAY + GERİ ÇEKME — damga TEK kez atılır.

    Geri çekme de `_lock_decision_scope`u (personel → talep, `populate_existing`)
    kullanmak ZORUNDADIR: kilitsiz hâlde ikinci transaction birincinin damgasını
    GÖREMEDEN geçer, onaylanmış bir izin sessizce "geri çekilmiş" olur ve
    personel izne çıkmışken kayıt kuyruktan düşer.

    ## 🔴 AYRIŞMA NOKTASI "bloke oldu mu" DEĞİL, SONUÇ ÇİFTİdir

    FIN-1 kanonu: kilitsiz kod da kendi `UPDATE`inin ÖRTÜK satır kilidine takılıp
    bloke olabilir — yani `not task2.done()` tek başına kilidin VARLIĞINI
    kanıtlamaz. Ayrışma `sorted([sonuc1, sonuc2]) == ["approved", "conflict"]`
    iddiasındadır: kilitsiz (ya da `populate_existing`siz) hâlde İKİSİ DE başarı
    döner ve `approve`ın kararı sessizce KAYBOLUR. Bloke iddiası yine de durur —
    yarış penceresinin gerçekten açıldığını gösteren korkuluktur.

    Aktör SAHİPLİK gerektirir: `sahip_aktor=1` ile personel ikinci aktöre
    bağlanır. Bağlanmasaydı `_geri_cek` 404 alır, `ConflictError` yakalanmaz ve
    test yarışı DEĞİL kurulum hatasını ölçerdi.

    Birinci aktör (onaylayan) personelin SAHİBİ DEĞİLDİR — OK-1A T5'in "kendi
    talebini onaylayamaz" 403'ü bu yüzden hiç koşmaz ve ölçüm karışmaz.
    """
    kurulum = await _kur(
        deducts=True,
        araliklar=[(date(_yil(), 9, 1), date(_yil(), 9, 5))],
        sahip_aktor=1,
    )
    talep_id = kurulum.request_ids[0]
    kilit_alindi = asyncio.Event()
    kilidi_birak = asyncio.Event()
    task1: asyncio.Task | None = None
    task2: asyncio.Task | None = None
    try:
        task1 = asyncio.create_task(
            _onayla_ve_tut(talep_id, kurulum.actor_ids[0], kilit_alindi, kilidi_birak)
        )
        await asyncio.wait_for(kilit_alindi.wait(), timeout=YARIS_TAVANI_SN)

        task2 = asyncio.create_task(_geri_cek(talep_id, kurulum.actor_ids[1]))
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine,
            task2,
            mesaj="geri çekme, onay kilidi serbest bırakılmadan ilerleyebildi — "
            "`withdraw_leave_request` artık karar satırını KİLİTLEMİYOR olabilir "
            "(K4 damga yarışı geri çekme yolunda açık)",
        )
        assert "FROM personnel WHERE" in bekleyen and bekleyen.endswith("FOR UPDATE"), bekleyen

        kilidi_birak.set()
        sonuc1 = await asyncio.wait_for(task1, timeout=YARIS_TAVANI_SN)
        sonuc2 = await asyncio.wait_for(task2, timeout=YARIS_TAVANI_SN)
        assert sorted([sonuc1, sonuc2]) == ["approved", "conflict"], (
            "iki istek de BAŞARILI döndü: onay kararı sessizce kayboldu "
            f"(sonuçlar: {sonuc1!r}, {sonuc2!r})"
        )

        async with _SessionFactory() as dogrula:
            talep = await dogrula.get(LeaveRequest, talep_id)
            assert talep.status is LeaveStatus.approved
            assert talep.decided_by == kurulum.actor_ids[0], "karar damgası ÜZERİNE YAZILDI"
            assert talep.reject_reason is None
    finally:
        await _gorevleri_bosalt(task1, task2)
        await _temizle(kurulum)


async def test_onay_denetimlerden_ONCE_personel_ve_talep_satirini_kilitler() -> None:
    """Kilidin VARLIĞI, SIRASI ve YERİ — SQL düzeyinde.

    Bu test üç şeyi çiviler:

    1. `personnel` satırı `FOR UPDATE` ile okunur,
    2. `leave_requests` satırı `FOR UPDATE` ile okunur,
    3. **personel kilidi, eşiği besleyen `sum(days)` okumasından ÖNCE gelir** —
       kilit denetimlerden sonra alınırsa (TOCTOU) yarış açık KALIR.

    ## Bu testin NEDEN gerektiği (mutasyon denetimiyle ÖLÇÜLDÜ)

    Davranış testleri kilidin yerini tam ayırt EDEMEZ ve bunu saklamıyoruz:

    * `lock_personnel_for_update` çağrısı kaldırılınca üç test birden kırmızı
      (davranış + bu test) — asıl bulgunun regresyon kilidi ORADADIR;
    * ama **talep satırındaki `with_for_update` tek başına kaldırılınca YALNIZ
      bu test kırmızıya döner**: personel kilidi zaten sıraya soktuğu ve geçiş
      sonunda talep satırını nasıl olsa `UPDATE` ettiği için davranış testleri
      yeşil kalır (`progress_payments` M11 bulgusunun kardeşi). O kilidin kalan
      değeri savunmanın YERELLİĞİDİR: yarın personel kilidini almayan ikinci bir
      karar yolu eklenirse koruma ancak burada durursa ayakta kalır;
    * `populate_existing=True` kaldırılınca `…_tek_damga_birakir` kırmızı —
      yani kilit ALTINDA TAZE okuma iddiası davranışsal olarak da ölçülüyor.

    Sıra (personel → talep) ayrıca `upsert_leave_balance` ile aynıdır; ters
    sırada kilitleyen bir yol eklenirse karşılıklı kilitlenme doğar.
    """
    kurulum = await _kur(deducts=True, araliklar=[(date(_yil(), 7, 1), date(_yil(), 7, 3))])
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        async with _SessionFactory() as session:
            actor = await session.get(User, kurulum.actor_ids[0])
            await service.approve_leave_request(
                session, actor, kurulum.request_ids[0], today=_bugun()
            )
            await session.commit()
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)
        await _temizle(kurulum)

    kilitli = [i for i, ifade in enumerate(ifadeler) if "FOR UPDATE" in ifade]
    personel_kilidi = [i for i in kilitli if "FROM personnel" in ifadeler[i]]
    talep_kilidi = [i for i in kilitli if "FROM leave_requests" in ifadeler[i]]
    esik_okumasi = [i for i, ifade in enumerate(ifadeler) if "sum(leave_requests.days)" in ifade]

    assert personel_kilidi, f"personel satırı FOR UPDATE ile okunmadı: {ifadeler}"
    assert talep_kilidi, f"talep satırı FOR UPDATE ile okunmadı: {ifadeler}"
    assert personel_kilidi[0] < talep_kilidi[0], f"kilit sırası ters: {ifadeler}"
    assert esik_okumasi, f"hak aşımı eşiğini besleyen sum() okuması hiç koşmadı: {ifadeler}"
    assert personel_kilidi[0] < esik_okumasi[0], (
        "kilit, eşiği besleyen okumadan SONRA alınmış — TOCTOU penceresi açık"
    )


async def test_iki_esZamanli_bakiye_putu_tek_satir_birakir() -> None:
    """PUT `/leave-balances/…`: iki eşzamanlı upsert — UQ emniyet ağına DÜŞMEZ.

    `uq_leave_balances_personnel_year` tek başına yalnız ikinci SATIRI engeller;
    iki istek de "satır yok" görüp INSERT ederse ikincisi `IntegrityError` → 409
    alırdı, oysa PUT'un sözleşmesi "gönderdiğin değer yazılır"dır. Personel
    kilidi ikinciyi UPDATE koluna düşürür: iki istek de başarılı, satır TEK.
    """
    kurulum = await _kur(deducts=True, araliklar=[(date(_yil(), 8, 1), date(_yil(), 8, 2))])
    kilit_alindi = asyncio.Event()
    kilidi_birak = asyncio.Event()
    task1: asyncio.Task | None = None
    task2: asyncio.Task | None = None
    try:

        async def yaz_ve_tut() -> Decimal:
            async with _SessionFactory() as session:
                yanit, _ = await service.upsert_leave_balance(
                    session,
                    kurulum.personnel_id,
                    _yil(),
                    LeaveBalanceUpdate(carried_over=Decimal("3")),
                    today=_bugun(),
                )
                kilit_alindi.set()
                await kilidi_birak.wait()
                await session.commit()
                return yanit.carried_over

        async def yaz() -> Decimal:
            async with _SessionFactory() as session:
                yanit, _ = await service.upsert_leave_balance(
                    session,
                    kurulum.personnel_id,
                    _yil(),
                    LeaveBalanceUpdate(carried_over=Decimal("5")),
                    today=_bugun(),
                )
                await session.commit()
                return yanit.carried_over

        task1 = asyncio.create_task(yaz_ve_tut())
        await asyncio.wait_for(kilit_alindi.wait(), timeout=YARIS_TAVANI_SN)

        task2 = asyncio.create_task(yaz())
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine,
            task2,
            mesaj="ikinci PUT beklemedi — `upsert_leave_balance` personel satırını "
            "KİLİTLEMİYOR olabilir (çift INSERT → UQ ihlali penceresi)",
        )
        assert "FROM personnel WHERE" in bekleyen and bekleyen.endswith("FOR UPDATE"), bekleyen

        kilidi_birak.set()
        await asyncio.wait_for(task1, timeout=YARIS_TAVANI_SN)
        assert await asyncio.wait_for(task2, timeout=YARIS_TAVANI_SN) == Decimal("5")

        async with _SessionFactory() as dogrula:
            satirlar = (
                (
                    await dogrula.execute(
                        select(LeaveBalance).where(
                            LeaveBalance.personnel_id == kurulum.personnel_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(satirlar) == 1, "iki eşzamanlı PUT iki bakiye satırı bıraktı"
            assert satirlar[0].carried_over == Decimal("5")
    finally:
        await _gorevleri_bosalt(task1, task2)
        await _temizle(kurulum)

"""MK-2 T3 — 🔴 EŞİK = KİLİT: onay/ödeme damgaları eşzamanlılık altında.

Görev emri: "MK-2'de bariz bir eşik yok gibi görünüyor — **ama `approve`/`pay`
damgaları eşzamanlı çift çağrıda çift ödeme üretebilir**." Buradaki eşik bir kota
değil bir **DURUM KAPISIDIR**: her adım YALNIZ BİR KEZ atılabilir. Kilitsiz iki
eşzamanlı `pay` AYNI `approved` durumunu okur, ikisi de geçer ve fatura İKİ KEZ
"ödendi" damgası alır — tek istekli bir test bunu ASLA görmez (İK-2/İK-3 dersi).

## Niçin `client`/`seeded_db` KULLANILMAZ

`tests/conftest.py`'deki `db_session` her testi TEK bağlantı üzerinde SAVEPOINT'e
sarar ve dış transaction'ı gerçekten COMMIT ETMEZ; o session üzerindeki iki görev
AYNI bağlantıyı paylaşır ve gerçek satır kilidi ölçülemez.
`test_mk1_work_log_concurrency.py` deseni birebir izlenir: `test_engine` üzerinden
İKİ BAĞIMSIZ bağlantı, gerçek commit, sonunda gerçek temizlik.

Bariyer (`asyncio.Event`) BİLİNÇLİDİR: çıplak `gather` iki görevi kritik anda
kesiştirmeyebilir; burada tx1 kilidi ALIP TUTARKEN tx2'nin bloke olduğu DOĞRUDAN
kanıtlanır.

## Kurulumun ŞANTİYESİZ faturası bilinçlidir

Fatura `site_id IS NULL` açılır (K9 "Tüm Projeler" istisnası): kurulum proje,
şantiye ve `user_project_access` satırı YARATMAK ZORUNDA KALMAZ — gerçekten
commit eden bir testte yaratılan her satır bir sızıntı riskidir.
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
from app.modules.equipment import rental_service
from app.modules.equipment.models import (
    EquipmentRentalInvoice,
    EquipmentRentalInvoiceLine,
    RentalInvoiceStatus,
)
from app.modules.invoicing.models import Invoice
from app.modules.procurement.models import PaymentTerms, Supplier
from app.modules.roles.models import Role
from app.modules.treasury.models import BankAccount, Payment
from app.modules.users.models import User
from tests._yaris import YARIS_TAVANI_SN, kilitte_bekleyen_sorgu
from tests.conftest import test_engine

from ._mk2_para_gercek import kira_parasini_yatir

pytestmark = pytest.mark.asyncio

_SessionFactory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

#: Rol anahtarı TESTE ÖZELDİR: `seed_reference_data`'nın ürettiği üretim
#: anahtarlarıyla çakışmaz — bu dosya GERÇEKTEN commit ettiği için sızıntı ancak
#: yaratılan satırların tam bilinmesiyle kapanır.
_ROL_ANAHTARI = "mk2_conc_admin"
_EPOSTALAR = ("mk2-onay1@conc.co", "mk2-onay2@conc.co")


class _Kurulum:
    def __init__(
        self,
        invoice_id: uuid.UUID,
        supplier_id: uuid.UUID,
        actor_ids: list[uuid.UUID],
        role_id: uuid.UUID,
    ) -> None:
        self.invoice_id = invoice_id
        self.supplier_id = supplier_id
        self.actor_ids = actor_ids
        self.role_id = role_id


async def _eslemeyi_kur(session) -> None:
    """MU-3D kira eşlemesi — hesap TÜRÜ TDHP tohumundan okunur, elle YAZILMAZ.

    Bu dosya kendi oturumunu açıp COMMIT ettiği için `conftest`in
    `kira_eslemesi` fixture'ı burada KULLANILAMAZ (o `seeded_db` üzerinde
    çalışır ve bu testin gördüğü transaction'a düşmez).
    """
    from app.modules.accounting.chart_seed_data import CHART_ACCOUNTS
    from app.modules.accounting.models import ChartAccount, JournalSourceType
    from app.modules.equipment.rental_posting import RENTAL_POSTING_RULES
    from app.modules.posting.models import PostingRule

    tohum = {satir.code: satir for satir in CHART_ACCOUNTS}
    hesaplar: dict[str, uuid.UUID] = {}
    for _rol, kod in RENTAL_POSTING_RULES:
        if kod in hesaplar:
            continue
        mevcut = (
            await session.execute(select(ChartAccount).where(ChartAccount.code == kod))
        ).scalar_one_or_none()
        if mevcut is None:
            kart = tohum[kod]
            mevcut = ChartAccount(
                code=kart.code,
                name=kart.name,
                account_type=kart.account_type,
                is_contra=kart.is_contra,
            )
            session.add(mevcut)
            await session.flush()
        hesaplar[kod] = mevcut.id
    for rol, kod in RENTAL_POSTING_RULES:
        session.add(
            PostingRule(
                source_type=JournalSourceType.equipment_rental_invoice,
                role_key=rol,
                account_id=hesaplar[kod],
            )
        )
    await session.flush()


async def _kur(*, status: RentalInvoiceStatus) -> _Kurulum:
    async with _SessionFactory() as session:
        role = Role(key=_ROL_ANAHTARI, name="Kira Hakedişi Eşzamanlılık Rolü")
        supplier = Supplier(name="Eşzamanlılık Kiralama A.Ş.", payment_terms=PaymentTerms.days_30)
        session.add_all([role, supplier])
        await session.flush()

        invoice = EquipmentRentalInvoice(
            supplier_id=supplier.id,
            period_year=2026,
            period_month=7,
            rate_period="hourly",
            invoice_amount=Decimal("100000.00"),
            vat_rate=Decimal("20.00"),
            status=status,
        )
        session.add(invoice)
        # 🔴 MU-3D — `approve_invoice` artık FİŞ YAZAR ve eşlemesiz **422** verir
        #    (fail-closed). Eşleme kurulmasaydı bu dosya kilidi değil eksik
        #    eşlemeyi ölçen bir kırmızı gösterirdi.
        await _eslemeyi_kur(session)
        aktorler = [
            User(
                email=eposta,
                password_hash=hash_password("parola1234"),
                full_name=f"Onay Aktörü {sira}",
                role_id=role.id,
            )
            for sira, eposta in enumerate(_EPOSTALAR, start=1)
        ]
        session.add_all(aktorler)
        await session.flush()
        # 🔴 PARA-GERCEK (kullanıcı kararı 2026-09-19) — `pay_invoice` artık
        #    arkasında GERÇEKLEŞMİŞ para ister. Kurulmasaydı bu dosya kilidi
        #    değil EKSİK PARAYI ölçen bir kırmızı gösterirdi; MU-3D eşlemesinin
        #    (yukarıda) yarattığı tuzakla AYNI sınıftır.
        await kira_parasini_yatir(session, invoice.id)
        await session.commit()
        return _Kurulum(
            invoice_id=invoice.id,
            supplier_id=supplier.id,
            actor_ids=[a.id for a in aktorler],
            role_id=role.id,
        )


async def _gorevleri_bosalt(*gorevler: asyncio.Task | None) -> None:
    """Temizlikten ÖNCE tüm görevleri sonlandırır — MUTASYON DENETİMİ İÇİN ŞART.

    Kilit kaldırıldığında `not task2.done()` iddiası kırmızıya döner ve test
    gövdesi ORTADA terk edilir; `task1` hâlâ commit etmemiş bir transaction
    içinde satır kilidi TUTUYOR olur. Bu boşaltma olmadan `_temizle`nin DELETE'i
    o kilidi SONSUZA DEK bekler ve kırmızı bir test SONSUZ ASKIYA dönüşürdü —
    mutasyon kanıtı okunamaz hâle gelirdi (İK-2 dersi).
    """
    for gorev in gorevler:
        if gorev is None:
            continue
        gorev.cancel()
        with contextlib.suppress(BaseException):
            await gorev


async def _fisleri_temizle(session, invoice_id: uuid.UUID) -> None:
    """MU-3D'nin yazdığı fişleri, kuralları ve hesapları düşürür.

    Bu dosya COMMIT ettiği için kalıntı bırakırsa sonraki koşu
    `uq_chart_of_accounts_code`a çarpardı.
    """
    from app.modules.accounting.models import (
        ChartAccount,
        JournalEntry,
        JournalLine,
        JournalSourceType,
    )
    from app.modules.equipment.rental_posting import RENTAL_POSTING_RULES
    from app.modules.posting.models import PostingRule

    fis_idleri = (
        (
            await session.execute(
                select(JournalEntry.id).where(
                    JournalEntry.source_type == JournalSourceType.equipment_rental_invoice,
                    JournalEntry.source_id == invoice_id,
                )
            )
        )
        .scalars()
        .all()
    )
    # Storno fişleri orijinale `reversal_of_id` ile bağlıdır (RESTRICT).
    if fis_idleri:
        storno_idleri = (
            (
                await session.execute(
                    select(JournalEntry.id).where(JournalEntry.reversal_of_id.in_(fis_idleri))
                )
            )
            .scalars()
            .all()
        )
        hepsi = [*storno_idleri, *fis_idleri]
        await session.execute(delete(JournalLine).where(JournalLine.entry_id.in_(hepsi)))
        for fis_id in hepsi:
            await session.execute(delete(JournalEntry).where(JournalEntry.id == fis_id))
    kodlar = sorted({kod for _rol, kod in RENTAL_POSTING_RULES})
    await session.execute(
        delete(PostingRule).where(
            PostingRule.source_type == JournalSourceType.equipment_rental_invoice
        )
    )
    await session.execute(delete(ChartAccount).where(ChartAccount.code.in_(kodlar)))


async def _temizle(kurulum: _Kurulum) -> None:
    async with _SessionFactory() as session:
        # 🔴 PARA-GERCEK kurulumunun izi ÖNCE düşer: `payments.invoice_id` ve
        #    `invoices.equipment_rental_invoice_id` FK'dır, ters sırada silmek
        #    kira hakedişinin DELETE'ini kırardı.
        _fatura_alt = select(Invoice.id).where(
            Invoice.equipment_rental_invoice_id == kurulum.invoice_id
        )
        await session.execute(delete(Payment).where(Payment.invoice_id.in_(_fatura_alt)))
        await session.execute(
            delete(Invoice).where(Invoice.equipment_rental_invoice_id == kurulum.invoice_id)
        )
        await session.execute(
            delete(EquipmentRentalInvoiceLine).where(
                EquipmentRentalInvoiceLine.invoice_id == kurulum.invoice_id
            )
        )
        await session.execute(
            delete(EquipmentRentalInvoice).where(EquipmentRentalInvoice.id == kurulum.invoice_id)
        )
        # 🔴 MU-3D — fiş ve satırları ÖNCE düşer: `journal_lines.account_id`
        #    RESTRICT'tir, hesaplar ondan sonra silinebilir.
        await _fisleri_temizle(session, kurulum.invoice_id)
        await session.execute(delete(Supplier).where(Supplier.id == kurulum.supplier_id))
        await session.execute(delete(User).where(User.id.in_(kurulum.actor_ids)))
        await session.execute(delete(Role).where(Role.id == kurulum.role_id))
        # 🔴 PARA-GERCEK'in banka hesabı da DÜŞER. Bu dosya GERÇEKTEN commit eder
        #    ve bırakılan satır sonraki koşuda `treasury` liste/sayaç testlerini
        #    kırar — nitekim kırdı (`test_hz1_bank_accounts`: `assert 4 == 3`).
        #    Ödemeler yukarıda silindiği için `bank_account_id` FK'sı serbesttir.
        await session.execute(delete(BankAccount).where(BankAccount.bank_name == "PARA-GERCEK"))
        await session.commit()


async def _eylem_ve_tut(
    kurulum: _Kurulum,
    actor_id: uuid.UUID,
    eylem: str,
    kilit_alindi: asyncio.Event,
    kilidi_birak: asyncio.Event,
) -> str:
    """tx1: damgayı vurur (kilit + durum denetimi + flush) ama sinyal gelene
    kadar COMMIT ETMEZ — `SELECT … FOR UPDATE` kilidi bu süre boyunca AÇIK kalır
    (`flush` kilidi BIRAKMAZ, yalnız commit/rollback bırakır)."""
    async with _SessionFactory() as session:
        actor = await session.get(User, actor_id)
        await getattr(rental_service, eylem)(session, actor, kurulum.invoice_id)
        kilit_alindi.set()
        await kilidi_birak.wait()
        await session.commit()
        return "ok"


async def _eylem(kurulum: _Kurulum, actor_id: uuid.UUID, eylem: str) -> str:
    async with _SessionFactory() as session:
        actor = await session.get(User, actor_id)
        try:
            await getattr(rental_service, eylem)(session, actor, kurulum.invoice_id)
            await session.commit()
            return "ok"
        except ConflictError:
            await session.rollback()
            return "rejected"


async def _fatura(invoice_id: uuid.UUID) -> EquipmentRentalInvoice:
    async with _SessionFactory() as session:
        return (
            await session.execute(
                select(EquipmentRentalInvoice).where(EquipmentRentalInvoice.id == invoice_id)
            )
        ).scalar_one()


async def _yaris(kurulum: _Kurulum, eylem: str) -> tuple[str, str]:
    """tx1 kilidi alıp TUTARKEN tx2'yi başlatır ve BLOKE OLDUĞUNU iddia eder."""
    kilit_alindi = asyncio.Event()
    kilidi_birak = asyncio.Event()
    task1: asyncio.Task | None = None
    task2: asyncio.Task | None = None
    try:
        task1 = asyncio.create_task(
            _eylem_ve_tut(kurulum, kurulum.actor_ids[0], eylem, kilit_alindi, kilidi_birak)
        )
        await asyncio.wait_for(kilit_alindi.wait(), timeout=YARIS_TAVANI_SN)

        task2 = asyncio.create_task(_eylem(kurulum, kurulum.actor_ids[1], eylem))
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine,
            task2,
            mesaj=f"tx2, tx1 kilidi serbest bırakmadan ilerleyebildi — `rental_service.{eylem}` "
            "artık fatura satırını KİLİTLEMİYOR olabilir (çift damga yarışı yeniden açık)",
            sorgu_oneki="SELECT equipment_rental_invoices.",
        )
        # Metin 1024 baytta kırpılır (`FOR UPDATE` görünmez); kilitte bekleyen bir SELECT
        # yalnız kilitli okuma olabilir — kilitsiz mutant UPDATE'te beklerdi.
        assert bekleyen.startswith("SELECT equipment_rental_invoices."), bekleyen

        kilidi_birak.set()
        sonuc1 = await asyncio.wait_for(task1, timeout=YARIS_TAVANI_SN)
        sonuc2 = await asyncio.wait_for(task2, timeout=YARIS_TAVANI_SN)
        return sonuc1, sonuc2
    finally:
        await _gorevleri_bosalt(task1, task2)


async def test_iki_esZamanli_odeme_faturayi_IKI_KEZ_odeyemez() -> None:
    """🔴 ASIL REGRESYON: `approved` bir faturaya İKİ eşzamanlı `pay`.

    Kilit YOKKEN ikisi de `approved` okur, ikisi de geçerli görür ve fatura İKİ
    KEZ ödenir (`paid_at` ikinci kez damgalanır, ödeme emri iki kez çıkar).
    Kilit VARKEN tx2 sıraya girer, tx1'in commit'ini görür (`paid`) ve 409 alır.
    """
    kurulum = await _kur(status=RentalInvoiceStatus.approved)
    try:
        sonuclar = await _yaris(kurulum, "pay_invoice")
        assert sorted(sonuclar) == ["ok", "rejected"]

        fatura = await _fatura(kurulum.invoice_id)
        assert fatura.status is RentalInvoiceStatus.paid
        assert fatura.paid_at is not None
    finally:
        await _temizle(kurulum)


async def test_iki_esZamanli_onay_tek_adim_ilerletir() -> None:
    """İki eşzamanlı `approve` faturayı İKİ ADIM ilerletemez.

    Kilitsiz hâlde ikisi de `pending_verification` okur; biri `approved` yazar,
    öteki de aynı geçişi geçerli sanıp yazar — ya iki onay damgası düşer ya da
    (zincir tek adım ilerlediği için) durum sessizce atlanır. Kilitle ikinci
    çağrı 409 alır.
    """
    kurulum = await _kur(status=RentalInvoiceStatus.pending_verification)
    try:
        sonuclar = await _yaris(kurulum, "approve_invoice")
        assert sorted(sonuclar) == ["ok", "rejected"]

        fatura = await _fatura(kurulum.invoice_id)
        assert fatura.status is RentalInvoiceStatus.approved
        assert fatura.approved_by_id == kurulum.actor_ids[0]
    finally:
        await _temizle(kurulum)


async def test_kilit_DURUM_DENETIMINDEN_once_ve_SATIRLARDAN_once_alinir() -> None:
    """Kilidin VARLIĞI, YERİ ve SIRASI — SQL düzeyinde (TOCTOU).

    Davranış testi kilidin ALINDIĞINI ölçer ama NEREDE alındığını tam ayırt
    edemez. İki iddia birlikte kapatır:

    1. `equipment_rental_invoices`e DEĞEN İLK ifade `FOR UPDATE` taşımalıdır —
       kilitsiz bir ön okuma yapılıp karar ondan verilseydi (ya da kilit
       denetimden sonra alınsaydı) TOCTOU penceresi AÇIK kalırdı;
    2. fatura kilidi satır okumasından ÖNCE gelmelidir — kilit sırası TÜM
       uçlarda SABİT (önce başlık, sonra satırlar) olmalıdır, yoksa satırdan
       başlayan bir yol ile başlıktan başlayan yol karşılıklı kilitlenme
       (deadlock) üretirdi.
    """
    kurulum = await _kur(status=RentalInvoiceStatus.approved)
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        async with _SessionFactory() as session:
            actor = await session.get(User, kurulum.actor_ids[0])
            await rental_service.pay_invoice(session, actor, kurulum.invoice_id)
            await session.commit()
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)
        await _temizle(kurulum)

    basliga_degen = [
        i
        for i, ifade in enumerate(ifadeler)
        if "FROM equipment_rental_invoices" in ifade or "equipment_rental_invoices SET" in ifade
    ]
    kilit = [
        i
        for i, ifade in enumerate(ifadeler)
        if "FOR UPDATE" in ifade and "FROM equipment_rental_invoices" in ifade
    ]
    satir_okumasi = [
        i for i, ifade in enumerate(ifadeler) if "FROM equipment_rental_invoice_lines" in ifade
    ]

    assert kilit, f"fatura başlığı FOR UPDATE ile okunmadı: {ifadeler}"
    assert basliga_degen[0] == kilit[0], (
        "faturaya değen İLK ifade kilitli DEĞİL — durum denetimi kilitsiz bir "
        f"okumadan beslenmiş olabilir (TOCTOU penceresi açık): {ifadeler}"
    )
    assert satir_okumasi, f"fatura satırları hiç okunmadı: {ifadeler}"
    assert kilit[0] < satir_okumasi[0], (
        f"kilit satır okumasından SONRA alınmış — kilit sırası bozuk: {ifadeler}"
    )

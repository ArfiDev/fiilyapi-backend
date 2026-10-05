"""SIL-B2 onarımı (R1) — şantiye silmesi ile eşzamanlı HAKEDİŞ ONAYI yarışı.

Hata: hakedişin bir şantiyedeki satırı silinirken başlık yalnız `approved` ise ağaca giriyordu.
Onay bekleyen başlık KİLİTLENMİYORDU: onay başlığı kilitleyip satırları kilitsiz okuyor, silme
sürerken commit ediyordu. Sonuç: başlık `approved`, elinde yalnız B şantiyesinin satırı, fişi ise
ESKİ (A+B) toplamdan — bayat fiş.

Düzeltme: silme yolu başlığı durumdan BAĞIMSIZ `FOR UPDATE` kilitler; onay bekler ve satırları
gitmiş başlığı görür. Doğru sonuç: başlık satırlarıyla tutarlı, fiş satır toplamına eşit.

R2 (aynı dosya): ÖDEME silmesi ile eşzamanlı `mark-paid`. `mark-paid` başlığı kilitler ama
ödemeleri kilitsiz okur; silme başlığı kilitlemezse ödemesiz `paid` hakediş doğardı.

`db_session` KULLANILMAZ: iki GERÇEK bağlantı, gerçek commit, gerçek temizlik.
"""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError
from app.core.security import hash_password
from app.core.silme.hatalar import DeletePreviewStaleError
from app.modules.accounting.models import (
    ChartAccount,
    JournalEntry,
    JournalLine,
    JournalSourceType,
)
from app.modules.invoicing.models import Invoice
from app.modules.posting.models import PostingRule
from app.modules.progress_payments import calculations, posting, repository, transitions
from app.modules.progress_payments.models import (
    ProgressPayment,
    ProgressPaymentLine,
    ProgressPaymentStatus,
)
from app.modules.projects.models import Project, ProjectContract
from app.modules.silme import service
from app.modules.silme.schemas import DeleteKind
from app.modules.sites.models import Site
from app.modules.treasury import realized
from app.modules.treasury.models import BankAccount, Payment
from app.modules.users.models import User
from tests import _hakedis_esleme
from tests._yaris import YARIS_TAVANI_SN, kilitte_bekleyen_sorgu
from tests.conftest import test_engine
from tests.modules.silme import _dunya as d
from tests.modules.silme import _mali_dunya as m
from tests.progress_payments.test_concurrency import (
    _gorevleri_bosalt,
    _referans_kur,
    _referans_temizle,
)

pytestmark = pytest.mark.asyncio

_Fabrika = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _mu3d_esleme() -> None:
    """Conftest'in `seeded_db` bağlı AYNI ADLI autouse fixture'ını gölgeler (gerçek commit'li test;
    eşleme `_kurulum`da kendi satırlarıyla kurulur — `test_concurrency.py` ile aynı gerekçe)."""
    return None


async def _kurulum(*, odemeli: bool = False) -> dict[str, uuid.UUID]:
    """Proje + iki şantiye + `pending_approval` hakediş (A ve B şantiyesinde birer satır) + fiş
    eşlemesi. `odemeli=True`: `approved` hakediş (tek satır) + faturası + TAM ödemesi (`mark-paid`
    kapısını geçer). HEPSİ gerçekten commit edilir."""
    async with _Fabrika() as oturum:
        rol = await _referans_kur(oturum)
        kullanici = User(
            email="sil-b2-baslik@test.co",
            password_hash=hash_password("parola1234"),
            full_name="Başlık Yarışı",
            role_id=rol.id,
            all_projects=True,
        )
        proje = Project(code="SIL-B2-BASLIK", name="Başlık Yarışı Projesi")
        oturum.add_all([kullanici, proje])
        await oturum.flush()
        snt_a = await d.site(oturum, proje, "YA-A", "A Şantiyesi")
        snt_b = await d.site(oturum, proje, "YA-B", "B Şantiyesi")
        odeme = await d.hakedis_satiri(
            oturum,
            proje,
            snt_a,
            kullanici,
            durum=(
                ProgressPaymentStatus.approved
                if odemeli
                else ProgressPaymentStatus.pending_approval
            ),
        )
        ilk = odeme.lines[0]
        if not odemeli:
            oturum.add(
                ProgressPaymentLine(
                    payment_id=odeme.id,
                    contract_item_id=ilk.contract_item_id,
                    site_id=snt_b.id,
                    code="11.099",
                    description="B kalemi",
                    unit="m³",
                    contract_unit_price=ilk.contract_unit_price,
                    coefficient=ilk.coefficient,
                    quantity=ilk.quantity,
                )
            )
        await oturum.flush()
        # Kalemi silinmiş satır (`contract_item_id` NULL) kota doğrulamasına girmez: yarışın konusu
        # kota değil, başlık kilididir (poz dağılımı kurmaya gerek kalmaz).
        await oturum.execute(
            text("UPDATE progress_payment_lines SET contract_item_id = NULL WHERE payment_id = :i"),
            {"i": odeme.id},
        )
        if not odemeli:
            await _hakedis_esleme.esleme_kur(
                oturum, JournalSourceType.progress_payment, posting.PROGRESS_PAYMENT_POSTING_RULES
            )
        ekstra: dict[str, uuid.UUID] = {}
        if odemeli:
            fatura = await m.fatura(
                oturum, kullanici, proje=proje, hakedis=odeme, toplam=Decimal("10000.00")
            )
            hesap = await m.banka(oturum)
            tahsilat = await m.odeme(oturum, kullanici, fatura, tutar=Decimal("10000.00"))
            ekstra = {"fatura": fatura.id, "hesap": hesap.id, "tahsilat": tahsilat.id}
        kimlikler = {
            **ekstra,
            "kullanici": kullanici.id,
            "proje": proje.id,
            "snt_a": snt_a.id,
            "snt_b": snt_b.id,
            "odeme": odeme.id,
        }
        await oturum.commit()
    return kimlikler


async def _temizle(k: dict[str, uuid.UUID]) -> None:
    async with _Fabrika() as oturum:
        fis_idleri = (
            (
                await oturum.execute(
                    select(JournalEntry.id).where(JournalEntry.source_id == k["odeme"])
                )
            )
            .scalars()
            .all()
        )
        if fis_idleri:
            await oturum.execute(delete(JournalLine).where(JournalLine.entry_id.in_(fis_idleri)))
            await oturum.execute(delete(JournalEntry).where(JournalEntry.id.in_(fis_idleri)))
        await oturum.execute(
            delete(PostingRule).where(PostingRule.source_type == JournalSourceType.progress_payment)
        )
        await oturum.execute(delete(ChartAccount).where(ChartAccount.code.in_(["120", "600"])))
        if "fatura" in k:
            await oturum.execute(delete(Payment).where(Payment.invoice_id == k["fatura"]))
            await oturum.execute(delete(Invoice).where(Invoice.id == k["fatura"]))
            await oturum.execute(delete(BankAccount).where(BankAccount.id == k["hesap"]))
        await oturum.execute(
            delete(ProgressPaymentLine).where(ProgressPaymentLine.payment_id == k["odeme"])
        )
        await oturum.execute(delete(ProgressPayment).where(ProgressPayment.id == k["odeme"]))
        for tablo in ("employer_contract_items", "employer_contract_groups"):
            await oturum.execute(
                text(f"DELETE FROM {tablo} WHERE project_id = :p"), {"p": k["proje"]}
            )
        await oturum.execute(delete(Site).where(Site.project_id == k["proje"]))
        await oturum.execute(
            delete(ProjectContract).where(ProjectContract.project_id == k["proje"])
        )
        await oturum.execute(delete(Project).where(Project.id == k["proje"]))
        await oturum.execute(delete(User).where(User.id == k["kullanici"]))
        await _referans_temizle(oturum)
        await oturum.commit()


async def _onayla(k: dict[str, uuid.UUID]) -> str:
    async with _Fabrika() as oturum:
        aktor = await oturum.get(User, k["kullanici"])
        await transitions.perform(oturum, aktor, k["odeme"], transitions.PaymentAction.approve)
        await oturum.commit()
        return "approved"


async def _beklenen_taban(k: dict[str, uuid.UUID]) -> Decimal:
    """Fişin olması GEREKEN taban: hakedişin O ANKİ satırlarından (uygulamanın kendi hesabı)."""
    async with _Fabrika() as oturum:
        odeme = await repository.get_payment(oturum, k["odeme"])
        await oturum.refresh(odeme, attribute_names=["lines"])
        sozlesme = await oturum.get(ProjectContract, k["proje"])
        avans = calculations.cumulative_state([], sozlesme.amount).advance_recovered
        return posting.posting_base_for(odeme, sozlesme.amount, avans)


async def test_site_silinirken_eszamanli_onay_bekler_bayat_fis_dogmaz(monkeypatch) -> None:
    k = await _kurulum()
    gorev_a: asyncio.Task[str] | None = None
    gorev_b: asyncio.Task[str] | None = None
    devam = asyncio.Event()
    try:
        async with _Fabrika() as oturum:
            token = (await service.onizle(oturum, DeleteKind.site, k["snt_a"])).preview_token

        kilitli = asyncio.Event()
        gercek = service.agaci_sil

        async def bariyerli(oturum, metadata, agac):  # kilitler alındı, silme HENÜZ başlamadı
            kilitli.set()
            await devam.wait()
            return await gercek(oturum, metadata, agac)

        monkeypatch.setattr(service, "agaci_sil", bariyerli)

        async def silici() -> str:
            async with _Fabrika() as oturum:
                try:
                    await service.sil(oturum, "site", k["snt_a"], token)
                    await oturum.commit()
                    return "deleted"
                except DeletePreviewStaleError:
                    await oturum.rollback()
                    return "stale"

        gorev_a = asyncio.create_task(silici())
        await asyncio.wait_for(kilitli.wait(), timeout=YARIS_TAVANI_SN)

        gorev_b = asyncio.create_task(_onayla(k))
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine, gorev_b, mesaj="onay, silmenin tuttuğu hakediş BAŞLIĞINI beklemeli"
        )
        assert bekleyen.startswith("SELECT progress_payments."), bekleyen  # başlık kilidi
        assert not gorev_b.done()  # `not done` bariyeri

        devam.set()
        silme = await asyncio.wait_for(gorev_a, timeout=YARIS_TAVANI_SN)
        onay = await asyncio.wait_for(gorev_b, timeout=YARIS_TAVANI_SN)
        assert silme == "deleted" and onay == "approved"

        # Bağımsız SQL tutarlılık kontrolü: hakediş yalnız B satırını taşır, fiş o toplamdan.
        async with _Fabrika() as oturum:
            durum = (
                await oturum.execute(
                    text("SELECT status::text FROM progress_payments WHERE id = :i"),
                    {"i": k["odeme"]},
                )
            ).scalar_one()
            satir_sayisi = (
                await oturum.execute(
                    text("SELECT count(*) FROM progress_payment_lines WHERE payment_id = :i"),
                    {"i": k["odeme"]},
                )
            ).scalar_one()
            fis_borc = (
                await oturum.execute(
                    text(
                        "SELECT COALESCE(SUM(l.debit), 0) FROM journal_lines l "
                        "JOIN journal_entries e ON e.id = l.entry_id WHERE e.source_id = :i"
                    ),
                    {"i": k["odeme"]},
                )
            ).scalar_one()
            tutarsiz = await m.tutarsizliklar(oturum)
        assert durum == "approved" and satir_sayisi == 1
        assert fis_borc == await _beklenen_taban(k), "BAYAT FİŞ: fiş satır toplamından kesilmedi"
        assert fis_borc > 0
        assert set(tutarsiz.values()) == {0}, tutarsiz
    finally:
        devam.set()
        await _gorevleri_bosalt(gorev_a, gorev_b)
        await _temizle(k)


async def _odendi_isaretle(k: dict[str, uuid.UUID]) -> str:
    async with _Fabrika() as oturum:
        aktor = await oturum.get(User, k["kullanici"])
        try:
            await transitions.perform(
                oturum, aktor, k["odeme"], transitions.PaymentAction.mark_paid
            )
            await oturum.commit()
            return "paid"
        except ConflictError:
            await oturum.rollback()
            return "conflict"


async def test_odeme_silinirken_eszamanli_mark_paid_odemesiz_paid_hakedis_birakmaz(
    monkeypatch,
) -> None:
    """`mark-paid` ödemeyi OKUDU (kapıyı geçti) ve başlığı TUTUYOR; silme aynı anda ödemeyi silmek
    ister. Kaynak başlık kilidi varsa silme BEKLER; mark-paid commit edince silme `paid`i görür ve
    `approved`a geri alır. Kilit YOKSA silme ödemeyi siler, demotion `paid`i göremez ve ödemesiz
    `paid` hakediş kalır."""
    k = await _kurulum(odemeli=True)
    gorev_a: asyncio.Task[str] | None = None
    gorev_b: asyncio.Task[str] | None = None
    okudu = asyncio.Event()
    devam = asyncio.Event()
    try:
        gercek = realized.assert_realized_covers

        async def bariyerli(*args, **kwargs):  # ödeme OKUNDU, `paid` HENÜZ yazılmadı
            sonuc = await gercek(*args, **kwargs)
            okudu.set()
            await devam.wait()
            return sonuc

        monkeypatch.setattr(realized, "assert_realized_covers", bariyerli)

        gorev_a = asyncio.create_task(_odendi_isaretle(k))
        await asyncio.wait_for(okudu.wait(), timeout=YARIS_TAVANI_SN)

        async def silici() -> str:
            async with _Fabrika() as oturum:
                token = (
                    await service.onizle(oturum, DeleteKind.payment, k["tahsilat"])
                ).preview_token
                try:
                    await service.sil(oturum, "payment", k["tahsilat"], token)
                    await oturum.commit()
                    return "deleted"
                except DeletePreviewStaleError:
                    await oturum.rollback()
                    return "stale"

        gorev_b = asyncio.create_task(silici())
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine, gorev_b, mesaj="silme, mark-paid'in tuttuğu hakediş BAŞLIĞINI beklemeli"
        )
        assert bekleyen.startswith("SELECT progress_payments.id"), bekleyen
        assert not gorev_b.done()

        devam.set()
        assert await asyncio.wait_for(gorev_a, timeout=YARIS_TAVANI_SN) == "paid"
        assert await asyncio.wait_for(gorev_b, timeout=YARIS_TAVANI_SN) == "deleted"

        async with _Fabrika() as oturum:
            durum = (
                await oturum.execute(
                    text("SELECT status::text FROM progress_payments WHERE id = :i"),
                    {"i": k["odeme"]},
                )
            ).scalar_one()
            odeme_sayisi = (
                await oturum.execute(
                    text("SELECT count(*) FROM payments WHERE invoice_id = :i"),
                    {"i": k["fatura"]},
                )
            ).scalar_one()
        # Tutarlılık: `paid` ise ödeme VAR; ödeme gittiyse hakediş `approved`.
        assert (durum == "paid" and odeme_sayisi >= 1) or durum == "approved", (durum, odeme_sayisi)
        assert durum == "approved" and odeme_sayisi == 0  # bu sıralamada silme demotion yapar
    finally:
        devam.set()
        await _gorevleri_bosalt(gorev_a, gorev_b)
        await _temizle(k)


def test_her_iki_hakedis_ailesinin_satir_baslik_kancasi_kilit_kosulu_tasir() -> None:
    """Taşeron ikizinin satırları şemada yalnız başlıktan CASCADE ile ağaca girer; o yolda başlık
    zaten ağaçtadır ve kilitlenir. Kanca yine de iki ailede AYNI kuralı taşır: yarın satırı ayrı
    yoldan ağaca sokan bir kenar eklenirse başlık kilidi atlanmaz."""
    from app.core.silme.graf import kayitli_kancalar  # noqa: PLC0415
    from app.modules.silme import kayitlar  # noqa: F401, PLC0415

    kancalar = {k.ad: k for k in kayitli_kancalar()}
    for ad in (
        "progress_payments.lines_header",
        "subcontractor_progress_payments.lines_header",
    ):
        assert kancalar[ad].kilit_kosulu is not None, ad

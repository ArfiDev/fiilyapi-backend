"""PARA-GERCEK — `mark-paid` GERÇEKLEŞMİŞ para ister (taşeron ailesi).

Kullanıcının kuralı birebir: *"Nakit olarak görmeden veya çekin vadesi gelip de
tahsil edilmeden 'ödendi' gözükmemesi gerekiyor."* Canlıda ÜÇ taşeron hakedişi
arkalarında tek kuruş ödeme olmadan `paid` damgası taşıyordu.

## Her iddianın İKİ YARISI vardır

🔴 Her kapı testi ÇİFTTİR: reddedilen hâlin yanında mutlaka *geçen* hâl de
vardır. Yalnız reddi ölçen bir test, kapı "her şeyi reddet" hâline geldiğinde de
YEŞİL kalırdı — yani "kural işliyor"u değil "hiçbir şey olmuyor"u kanıtlardı.

🔴 Ve bu dosyanın EN ÖNEMLİ testi `test_KESINTILI_hakedis_UCTAN_UCA_odenebilir`
dir: ilk turda kapı, kesinti taşıyan hiçbir hakediş için GEÇİLEMİYORDU (eşik
hakediş netiydi, ödemenin tavanı ise fatura `total`i) ve bunu HİÇBİR bekçi
görmedi, çünkü yardımcı faturayı elle kurup iki sayıyı zorla eşitliyordu.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.invoicing.models import InvoiceDirection, InvoiceDocumentType, InvoiceStatus
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
)
from app.modules.treasury.models import FinancialInstrumentStatus, Payment, PaymentMethodKind
from app.modules.treasury.realized import (
    BINDING_INVOICE_INVALID,
    PAYMENT_NOT_REALIZED,
    SOURCE_NOT_INVOICED,
)
from app.modules.users.models import User
from tests._para_gercek import fatura_kes, hakedis_bruttu, odeme_yaz, parayi_yatir
from tests._silme_yardimci import sil_aile
from tests.subcontractor_progress_payments.test_transitions import _satirli_hakedis

pytestmark = pytest.mark.asyncio

_UC = "/subcontractor-progress-payments"


async def _onayli_hakedis(
    seeded_db: AsyncSession, hakedis_fabrikasi, contract, admin_kullanicisi: User
) -> SubcontractorProgressPayment:
    return await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus.approved,
    )


# --------------------------------------------------------------------------- #
# 🔴 BULGU 1 — KESINTILI hakedis ULASILABILIR olmali (asil regresyon)
# --------------------------------------------------------------------------- #


async def test_KESINTILI_hakedis_UCTAN_UCA_odenebilir(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """🔴 DENETİM BULGUSU 1'İN BEKÇİSİ — kapı kullanıcıyı KİLİTLEMİYOR.

    Fixture kesintilidir (avans %10 · teminat %5 · KDV %20) ve iki formül
    YAPISAL olarak ayrışır:

        hakediş neti : KDV **brüt** üzerinden      → 245.175,00
        fatura total : KDV **tax_base** üzerinden  → 238.170,00

    Ödeme yalnız faturaya yazılabilir ve tavanı `total`dir. Eşik hakediş neti
    olsaydı bu test ASLA geçemezdi. Üç GERÇEK uçla kurulur, hiçbir yardımcı
    aradaki hesabı kısa devre yapmaz.
    """
    from app.modules.accounting.models import JournalSourceType
    from app.modules.treasury.posting import PAYMENT_POSTING_RULES
    from tests._hakedis_esleme import esleme_kur

    await esleme_kur(seeded_db, JournalSourceType.payment, PAYMENT_POSTING_RULES)

    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    brut = await hakedis_bruttu(seeded_db, payment.id, taseron=True)

    # Kesintiler GERÇEKTEN dolu — aksi hâlde test kusuru yeniden üretemezdi.
    assert payment.advance_pct > 0 and payment.retainage_pct > 0 and payment.vat_pct > 0

    erken = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)
    assert erken.status_code == 409, erken.text
    assert erken.json()["detail"] == SOURCE_NOT_INVOICED

    fatura = await client.post(
        "/invoices",
        json={
            "direction": "incoming",
            "invoice_no": f"TS{uuid.uuid4().hex[:10].upper()}",
            "document_type": "einvoice",
            "issue_date": "2026-02-01",
            "party_name": "Taşeron A.Ş.",
            "subcontractor_progress_payment_id": str(payment.id),
            "advance_rate": str(payment.advance_pct),
            "retention_rate": str(payment.retainage_pct),
            "lines": [
                {
                    "description": "Hakediş bedeli",
                    "quantity": "1",
                    "unit": "adet",
                    "unit_price": str(brut),
                    "vat_rate": str(payment.vat_pct),
                }
            ],
        },
        headers=admin_headers,
    )
    assert fatura.status_code in (200, 201), fatura.text
    total = Decimal(str(fatura.json()["total"]))
    assert total > 0

    # Faturasi var ama parasi yok -> HALA kapali, ama ARTIK BASKA bir metinle.
    faturali = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)
    assert faturali.status_code == 409, faturali.text
    assert faturali.json()["detail"] == PAYMENT_NOT_REALIZED

    hesap = await client.post(
        "/bank-accounts",
        json={"bank_name": "Uçtan Uca Bank", "account_type": "checking", "iban": None},
        headers=admin_headers,
    )
    assert hesap.status_code in (200, 201), hesap.text

    odeme = await client.post(
        f"/invoices/{fatura.json()['id']}/payments",
        json={
            "bank_account_id": hesap.json()["id"],
            "method": "transfer",
            "amount": str(total),
            "paid_on": "2026-02-05",
        },
        headers=admin_headers,
    )
    assert odeme.status_code in (200, 201), odeme.text

    sonuc = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)
    assert sonuc.status_code == 200, sonuc.text
    assert sonuc.json()["status"] == "paid"


async def test_BULGU2_yardimci_faturayi_URUNUN_PARA_MOTORUNDAN_gecirir(
    seeded_db: AsyncSession,
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """🔴 DENETİM BULGUSU 2'NİN BEKÇİSİ — yardımcı, ölçtüğü yolu KURMASIN.

    İlk hâlinde `fatura_kes` para kolonlarını elle dolduruyor ve tüm oranları
    SIFIRLIYORDU (`subtotal = tax_base = total = tutar`). O yüzden kapının
    karşılaştırdığı iki sayı testte zorla eşitleniyordu ve bulgu 1
    (kesintili hakediş kilitleniyor) DÖRT KAPIDAN DA yeşil geçti.

    Bu test üç şeyi birden çakar:

      1. faturanın kesinti kolonları GERÇEKTEN dolu (oranlar düşmemiş),
      2. `total` ürünün motorunun (`invoicing.amounts.compute`) verdiği sayı,
      3. 🔴 ve o sayı hakediş NETİNDEN FARKLI — iki formülün KDV matrahı
         gerçekten ayrışıyor. Bu üçüncüsü bulgu 1'in sayısal özüdür: eşik net
         olsaydı fark kadar bir tutar ASLA ödenemezdi.
    """
    from app.modules.invoicing import amounts as invoice_amounts
    from app.modules.progress_payments import calculations

    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    assert payment.advance_pct > 0 and payment.retainage_pct > 0 and payment.vat_pct > 0

    fatura = await fatura_kes(seeded_db, payment.id, taseron=True)
    brut = await hakedis_bruttu(seeded_db, payment.id, taseron=True)

    # 1 — kesintiler faturaya TASINDI
    assert fatura.advance_amount > 0
    assert fatura.retention_amount > 0
    assert fatura.vat_amount > 0

    # 2 — total URUNUN motorundan
    beklenen = invoice_amounts.compute(
        [
            invoice_amounts.LineInput(
                quantity=Decimal("1"), unit_price=brut, vat_rate=payment.vat_pct
            )
        ],
        advance_rate=payment.advance_pct,
        retention_rate=payment.retainage_pct,
        withholding_rate=None,
    )
    assert fatura.total == beklenen.total
    assert fatura.tax_base == beklenen.tax_base

    # 3 — iki formul GERCEKTEN ayrisiyor (bulgu 1'in sayisal ozu)
    hakedis_net = calculations.net_amount(
        brut,
        calculations.vat_amount(brut, payment.vat_pct),
        calculations.retention_amount(brut, payment.advance_pct),
        calculations.retention_amount(brut, payment.retainage_pct),
    )
    assert hakedis_net > fatura.total, (
        "Kesintili hakediste net ile fatura total'i AYRISMALI; ayrismiyorsa "
        "bulgu 1'i ureten kosul yok demektir ve bu dosya onu olcmuyordur."
    )


# --------------------------------------------------------------------------- #
# G1 — odemesiz hakedis `paid` OLAMAZ (+ pozitif kontrol)
# --------------------------------------------------------------------------- #


async def test_G1_FATURASIZ_hakedis_odendi_isaretlenemez(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """Canlıdaki kusurun ta kendisi: arkasında hiçbir şey olmayan hakediş."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)

    yanit = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == SOURCE_NOT_INVOICED
    await seeded_db.refresh(payment)
    assert payment.status is SubcontractorPaymentStatus.approved
    assert payment.paid_at is None


async def test_G1_ODEMESIZ_faturali_hakedis_de_isaretlenemez(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """Faturanın VARLIĞI yetmez; iki engelin metinleri de AYRIDIR."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    await fatura_kes(seeded_db, payment.id, taseron=True)

    yanit = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == PAYMENT_NOT_REALIZED


async def test_G1_POZITIF_KONTROL_yeterli_gerceklesmis_odeme_GECER(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """🔴 İddianın ikinci yarısı — kapı HER ŞEYİ reddetmiyor."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    await parayi_yatir(seeded_db, payment.id, taseron=True)

    yanit = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "paid"


# --------------------------------------------------------------------------- #
# G2 — cek TEK BASINA yetmez
# --------------------------------------------------------------------------- #


async def test_G2_PORTFOYDEKI_cek_TEK_BASINA_yetmez(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """Kuralın İKİNCİ yarısı: *"çekin vadesi gelip de tahsil edilmeden"*."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    await parayi_yatir(
        seeded_db, payment.id, taseron=True, evrak_durumu=FinancialInstrumentStatus.portfolio
    )

    yanit = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == PAYMENT_NOT_REALIZED


async def test_G2_POZITIF_KONTROL_cek_ODENINCE_gecer(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """🔴 YÖN BURADA ÖLÇÜLÜR: taşerona biz öderiz → VERİLEN çek → `paid`
    (`collected` DEĞİL; verilen çek tahsil edilmez)."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    await parayi_yatir(
        seeded_db, payment.id, taseron=True, evrak_durumu=FinancialInstrumentStatus.paid
    )

    yanit = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 200, yanit.text


@pytest.mark.parametrize(
    "durum", [FinancialInstrumentStatus.returned, FinancialInstrumentStatus.cancelled]
)
async def test_G2_KARSILIKSIZ_ya_da_IPTAL_cek_para_SAYILMAZ(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
    durum: FinancialInstrumentStatus,
) -> None:
    """Beyaz liste fail-closed: terminal olmak para geçtiği anlamına GELMEZ."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    await parayi_yatir(seeded_db, payment.id, taseron=True, evrak_durumu=durum)

    yanit = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text


# --------------------------------------------------------------------------- #
# 🔴 BULGU 5 — BAGSIZ cek/senet odemesi fail-closed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method", [PaymentMethodKind.cheque, PaymentMethodKind.promissory_note])
async def test_BULGU5_BAGSIZ_cek_odemesi_hakedisi_ODETMEZ(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
    method: PaymentMethodKind,
) -> None:
    """🔴 DENETİM BULGUSU 5.

    Kullanıcı `method='cheque'` yazıp `financial_instrument_id` GÖNDERMEZSE
    (form zorunlu tutmuyor) banka bakiyesi tanımı bunu KOŞULSUZ nakit sayar
    (ODM-1 D1) ve hakediş o an `paid` olurdu — kullanıcının kuralının birebir
    yasakladığı hâl. Kapı bakiyeninkinden DAR bir tanım kullanır ve bağsız
    kıymetli evrak ödemesini SAYMAZ.

    Banka bakiyesinin tanımına DOKUNULMADI: bu iki ayrı sorudur.
    """
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    await parayi_yatir(seeded_db, payment.id, taseron=True, method=method)

    yanit = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == PAYMENT_NOT_REALIZED


@pytest.mark.parametrize("method", [PaymentMethodKind.transfer, PaymentMethodKind.cash])
async def test_BULGU5_POZITIF_KONTROL_bagsiz_HAVALE_ve_NAKIT_sayilir(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
    method: PaymentMethodKind,
) -> None:
    """🔴 Daraltma YALNIZ kıymetli evrak etiketlerini kapsar.

    Bu yarı olmadan bulgu 5'in düzeltmesi, "bağsız her ödemeyi reddet" hâline
    dönüşür ve kullanıcı havaleyle ödediği hakedişi de kapatamazdı.
    """
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    await parayi_yatir(seeded_db, payment.id, taseron=True, method=method)

    yanit = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 200, yanit.text


# --------------------------------------------------------------------------- #
# G3 — kismi odeme yetmez; SINIR (tam esit) gecer
# --------------------------------------------------------------------------- #


async def test_G3_KISMI_odeme_yetmez(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """Faturanın bir KURUŞ altı bile yetmez."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    yatan = await parayi_yatir(seeded_db, payment.id, taseron=True, fark=Decimal("-0.01"))
    assert yatan > 0, "kurulum anlamsız: kısmi ödeme sıfır olamaz"

    yanit = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text


async def test_G3_SINIR_tam_esit_tutar_GECER(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """🔴 SINIR: `<` yerine `<=` yazan mutant YALNIZ burada görünür."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    await parayi_yatir(seeded_db, payment.id, taseron=True, fark=Decimal("0.00"))

    yanit = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 200, yanit.text


# --------------------------------------------------------------------------- #
# G4 — `paid` HALA TERMINAL
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("uc", ["submit", "approve", "reject", "mark-paid", "unapprove"])
async def test_G4_paid_hicbir_gecisin_KAYNAGI_degildir(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
    uc: str,
) -> None:
    """PARA-GERCEK bir KAPI ekledi, tabloya ÇİFT EKLEMEDİ."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus.paid,
    )

    yanit = await client.post(
        f"{_UC}/{payment.id}/{uc}", json={"reason": "gerekçe metni"}, headers=admin_headers
    )

    assert yanit.status_code == 409, yanit.text


# --------------------------------------------------------------------------- #
# 🔴 BULGU 6 — IADE faturasinin odemesi CIKARILIR
# --------------------------------------------------------------------------- #


async def test_BULGU6_IADE_odemesi_gerceklesen_tutari_DUSURUR(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """🔴 DENETİM BULGUSU 6.

    Asıl fatura tam tahsil edildi, sonra bir kısmı İADE faturasıyla geri döndü.
    İlk uygulama iade ödemesini tamamen ATLIYORDU ve kapı geçiyordu — kasada o
    para artık yokken. Aynı PR'ın ODM-2 mantığı bunun tersini söyler.
    """
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    await parayi_yatir(seeded_db, payment.id, taseron=True)

    # Buraya kadar GECER — karsit kanit.
    on_kontrol = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)
    assert on_kontrol.status_code == 200, on_kontrol.text

    # Ayni kurulumu ikinci bir hakedis icin kurup bu kez IADE ekle.
    ikinci = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        sequence_no=2,
        status=SubcontractorPaymentStatus.approved,
    )
    await parayi_yatir(seeded_db, ikinci.id, taseron=True)
    brut = await hakedis_bruttu(seeded_db, ikinci.id, taseron=True)
    iade = await fatura_kes(
        seeded_db,
        ikinci.id,
        taseron=True,
        brut=brut / 2,
        document_type=InvoiceDocumentType.refund,
    )
    await odeme_yaz(seeded_db, iade, tutar=iade.total)

    yanit = await client.post(f"{_UC}/{ikinci.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == PAYMENT_NOT_REALIZED


# --------------------------------------------------------------------------- #
# Toplamanin KAPSAMI
# --------------------------------------------------------------------------- #


async def test_G6_bir_odeme_IKI_hakedise_birden_sayilamaz(
    seeded_db: AsyncSession,
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """🔴 ÇİFT SAYIM YAPISAL OLARAK İMKÂNSIZDIR — kilitle değil ŞEMAYLA."""
    from app.modules.invoicing.models import Invoice
    from app.modules.treasury.realized import realized_total_for_source

    contract, _, _ = taseron_sozlesmesi
    birinci = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    ikinci = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        sequence_no=2,
        status=SubcontractorPaymentStatus.approved,
    )
    yatan = await parayi_yatir(seeded_db, birinci.id, taseron=True)

    birinci_toplam = await realized_total_for_source(
        seeded_db, Invoice.subcontractor_progress_payment_id, birinci.id
    )
    ikinci_toplam = await realized_total_for_source(
        seeded_db, Invoice.subcontractor_progress_payment_id, ikinci.id
    )

    assert birinci_toplam == yatan
    assert ikinci_toplam == Decimal("0")


async def test_FATURA_pending_iken_de_odeme_SAYILIR(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """🔴 Kapı faturanın DURUMUNA bakmaz, PARANIN hareketine bakar.

    Gelen fatura sisteme `pending` girer. Kapıya bir `approved` şartı da
    eklenseydi, parayı gerçekten ödemiş ama faturayı henüz onaylamamış kullanıcı
    hakedişi kapatamazdı.
    """
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    await parayi_yatir(seeded_db, payment.id, taseron=True, status=InvoiceStatus.pending)

    yanit = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 200, yanit.text


async def test_kapi_hakedisin_KENDI_odemesine_bakar(
    seeded_db: AsyncSession,
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """Kaynağa bağlanmamış bir faturanın parası hakedişe SAYILMAZ."""
    from app.modules.invoicing.models import Invoice
    from app.modules.treasury.realized import realized_total_for_source

    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    bagimsiz = await fatura_kes(seeded_db, payment.id, taseron=True, kaynaga_bagla=False)
    await odeme_yaz(seeded_db, bagimsiz, tutar=bagimsiz.total)

    toplam = await realized_total_for_source(
        seeded_db, Invoice.subcontractor_progress_payment_id, payment.id
    )
    assert toplam == Decimal("0")


async def test_kapi_KAPSAM_disindaki_hakedisin_varligini_SIZDIRMAZ(
    client: AsyncClient,
    kisitli_headers: dict[str, str],
    gorunmeyen_hakedis,
) -> None:
    """Kapsam süzgeci kapıdan ÖNCE koşar: görünmeyen kayıt 404/403'tür."""
    yanit = await client.post(f"{_UC}/{gorunmeyen_hakedis}/mark-paid", headers=kisitli_headers)
    assert yanit.status_code in (403, 404), yanit.text


# --------------------------------------------------------------------------- #
# 🔴 G7 — SIFIR TUTARLI baglayici fatura kapiyi BOSTA gecirmemeli (kusur 1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kurulum", "aciklama"),
    [
        ({"kalemsiz": True}, "kalem HIC gonderilmedi (sema default_factory=list)"),
        ({"brut": Decimal("0.00")}, "unit_price=0 kalemi (_UNIT_PRICE ge=0)"),
    ],
)
async def test_G7_SIFIR_tutarli_baglayici_fatura_REDDEDILIR(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
    kurulum: dict,
    aciklama: str,
) -> None:
    """🔴 CANLIDA AÇIK OLAN BYPASS.

    `realized < total` karşılaştırması sıfır tutarlı faturada `0 < 0` → **False**
    verir; yani kapı SIFIR ÖDEMEYLE geçiyordu. İki üretim yolu da meşrudur:
    kalem hiç göndermemek (`lines` `default_factory=list`) ya da `unit_price=0`
    kalem yazmak (`Field(ge=0)`).

    Ağırlaştırıcı: kalemsiz fatura `approve` EDİLEMEZ (`gate_blockers` →
    `LINES_REQUIRED`), üstelik kısmi UNIQUE indeks yüzünden sahte fatura kaynak
    slotunu KALICI işgal eder ve gerçek fatura o hakedişe bir daha bağlanamaz —
    `paid` ise TERMİNAL.
    """
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    fatura = await fatura_kes(seeded_db, payment.id, taseron=True, **kurulum)
    assert fatura.total == Decimal("0.00"), f"kurulum {aciklama} icin total=0 uretmedi"

    yanit = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == BINDING_INVOICE_INVALID
    await seeded_db.refresh(payment)
    assert payment.status is SubcontractorPaymentStatus.approved
    assert payment.paid_at is None


async def test_G7_POZITIF_KONTROL_dolu_fatura_yeterli_odeme_GECER(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """🔴 İddianın ikinci yarısı: `total > 0` şartı MEŞRU faturayı engellemiyor.

    Bu yarı olmadan G7, kapı "her faturayı geçersiz say" hâline geldiğinde de
    yeşil kalırdı — yani kuralı değil, tam kapanmayı ölçerdi.
    """
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    await parayi_yatir(seeded_db, payment.id, taseron=True)

    yanit = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 200, yanit.text


async def test_G7_UC_ENGEL_UC_AYRI_METIN(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """Kullanıcının yapacağı iş üç hâlde farklıdır; metinler de öyle olmalı.

    Tek metin kullanılsaydı, sistemde duran BOZUK faturayı hiç görmeyen kullanıcı
    "fatura kes" mesajı alır ve kısmi UNIQUE indeks yüzünden ikinci bir asıl
    fatura kesemediği için çıkmaza girerdi.
    """
    contract, _, _ = taseron_sozlesmesi

    faturasiz = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    y1 = await client.post(f"{_UC}/{faturasiz.id}/mark-paid", headers=admin_headers)
    assert y1.json()["detail"] == SOURCE_NOT_INVOICED

    bozuk = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        sequence_no=2,
        status=SubcontractorPaymentStatus.approved,
    )
    await fatura_kes(seeded_db, bozuk.id, taseron=True, kalemsiz=True)
    y2 = await client.post(f"{_UC}/{bozuk.id}/mark-paid", headers=admin_headers)
    assert y2.json()["detail"] == BINDING_INVOICE_INVALID

    odemesiz = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        sequence_no=3,
        status=SubcontractorPaymentStatus.approved,
    )
    await fatura_kes(seeded_db, odemesiz.id, taseron=True)
    y3 = await client.post(f"{_UC}/{odemesiz.id}/mark-paid", headers=admin_headers)
    assert y3.json()["detail"] == PAYMENT_NOT_REALIZED

    assert len({y1.json()["detail"], y2.json()["detail"], y3.json()["detail"]}) == 3


# --------------------------------------------------------------------------- #
# 🔴 G8 — faturanin YONU kaynagin para akisina uymali (kusur 2)
# --------------------------------------------------------------------------- #


async def test_G8_YANLIS_YONLU_fatura_REDDEDILIR(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """🔴 DENETİM KUSURU 2.

    Taşerona olan borcumuz GELEN faturayla kapanır. Taşerona kesilmiş bir GİDEN
    fatura (kesinti/ceza) hakedişe bağlanır ve taşeron onu BİZE öderse, yön
    denetlenmediğinde `realized` bir TAHSİLATLA dolar ve kapı geçer — yani bize
    GİREN para, taşerona olan borcumuzu "ödenmiş" gösterir. Taşerona tek kuruş
    çıkmamıştır.

    Ödeme BİLEREK tam tutardır: eksik ödeme de reddedilirdi ve test o zaman yönü
    değil tutarı ölçerdi.
    """
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    ters = await fatura_kes(
        seeded_db, payment.id, taseron=True, direction=InvoiceDirection.outgoing
    )
    assert ters.total > 0, "kurulum: yon testi tutar yuzunden reddedilmemeli"
    await odeme_yaz(seeded_db, ters, tutar=ters.total)

    yanit = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == BINDING_INVOICE_INVALID


async def test_G8_POZITIF_KONTROL_dogru_yon_GECER(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """🔴 İddianın ikinci yarısı: doğru yön (GELEN) aynı tutarla GEÇER.

    G8 ile TEK farkı `direction`tır; bu ikili, reddin yönden geldiğini kanıtlar.
    """
    contract, _, _ = taseron_sozlesmesi
    payment = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    dogru = await fatura_kes(
        seeded_db, payment.id, taseron=True, direction=InvoiceDirection.incoming
    )
    await odeme_yaz(seeded_db, dogru, tutar=dogru.total)

    yanit = await client.post(f"{_UC}/{payment.id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 200, yanit.text


def test_G8_yon_tablosu_UC_kaynak_kolonunu_da_KAPSAR() -> None:
    """`SOURCE_DIRECTION` eksik kalırsa `.get()` `None` döner ve o aile TAMAMEN
    kilitlenirdi (fail-closed ama sessiz). Tablo kolon adlarıyla kurulduğu için
    bir yazım hatası da ancak burada görünür.

    🔴 Kira ailesi 2026-09-19'da EKLENDİ (kullanıcı kararı — KK-ODM kirayı da
    kapsar). Bu iddia ÜYELİĞİ çiviler, sayıyı değil: yeni bir kaynak kolonu
    ekleyen, yönünü BİLEREK sınıflandırmak zorunda kalsın."""
    from app.modules.invoicing.models import Invoice
    from app.modules.treasury.realized import SOURCE_DIRECTION

    assert set(SOURCE_DIRECTION) == {
        "progress_payment_id",
        "subcontractor_progress_payment_id",
        "equipment_rental_invoice_id",
    }
    for kolon in SOURCE_DIRECTION:
        assert hasattr(Invoice, kolon), f"{kolon} Invoice'ta yok"
    # İşveren ailesi ÖTEKİ İKİSİNE TERS yöndedir; eşitlenirse biri sessizce
    # yanlış olurdu. Taşeron ve kira firması BİZE fatura keser (para ÇIKAR).
    assert SOURCE_DIRECTION["progress_payment_id"] is InvoiceDirection.outgoing
    assert SOURCE_DIRECTION["subcontractor_progress_payment_id"] is InvoiceDirection.incoming
    assert SOURCE_DIRECTION["equipment_rental_invoice_id"] is InvoiceDirection.incoming


# --------------------------------------------------------------------------- #
# 🔴 ODM-HAK — ödenmiş hakedişin ALTINDAKİ para ÇEKİLEMEZ (taşeron ikizi)
# --------------------------------------------------------------------------- #


async def test_ODENMIS_hakedisin_odemesi_silinince_hakedis_approved_a_doner(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """SIL-B2 (işveren ikizi): `paid` damgası ödemeye dayanır; ödeme silinince damga `approved`a
    geri alınır, boş bir "Ödendi" rozeti kalmaz. İkiz ZORUNLUDUR: iki ailede de aynı davranış."""
    contract, _, _ = taseron_sozlesmesi
    hakedis = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    fatura = await fatura_kes(seeded_db, hakedis.id, taseron=True)
    odeme = await odeme_yaz(seeded_db, fatura, tutar=fatura.total)
    odeme_id, hakedis_id = odeme.id, hakedis.id

    gecis = await client.post(f"{_UC}/{hakedis_id}/mark-paid", headers=admin_headers)
    assert gecis.status_code == 200, gecis.text

    silme = await sil_aile(client, admin_headers, "payment", odeme_id)

    assert silme.status_code == 204, silme.text
    kalan = await seeded_db.execute(
        select(func.count()).select_from(Payment).where(Payment.id == odeme_id)
    )
    assert kalan.scalar_one() == 0
    seeded_db.expire_all()
    kalan_hakedis = await seeded_db.get(SubcontractorProgressPayment, hakedis_id)
    assert kalan_hakedis is not None
    assert kalan_hakedis.status is SubcontractorPaymentStatus.approved


async def test_POZITIF_KONTROL_ODENMEMIS_hakedisin_odemesi_SILINEBILIR(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """🔴 İddianın ikinci yarısı: AYNI kurulum, TEK fark `mark-paid`in
    koşmamış olması. Kapı "hakedişe bağlı hiçbir ödeme silinmez" hâline
    gelseydi yanlış girilmiş tahsilat da düzeltilemezdi."""
    contract, _, _ = taseron_sozlesmesi
    hakedis = await _onayli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    fatura = await fatura_kes(seeded_db, hakedis.id, taseron=True)
    odeme = await odeme_yaz(seeded_db, fatura, tutar=fatura.total)

    odeme_id = odeme.id
    silme = await sil_aile(client, admin_headers, "payment", odeme_id)

    assert silme.status_code == 204, silme.text
    kalan = await seeded_db.execute(
        select(func.count()).select_from(Payment).where(Payment.id == odeme_id)
    )
    assert kalan.scalar_one() == 0

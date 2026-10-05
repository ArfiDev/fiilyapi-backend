"""PARA-GERCEK — `mark-paid` GERÇEKLEŞMİŞ para ister (İŞVEREN ailesi).

Kapı İKİ ailede de vardır ve bu dosya ikizin ölçüldüğü yerdir. Tek ailede
uygulansaydı `_TRANSITION_SHAPE`i paylaşan iki makine aynı `mark_paid` çifti
için FARKLI davranırdı ve fark hiçbir yerde görünmezdi.

## Yön BURADA TERSTİR ve ölçülür

İşveren hakedişinde para bize GELİR: fatura `outgoing`tur, karşılığında ALINAN
(`received`) bir çek elimize girer ve o çekin "para geçti" durumu `collected`tır
(`paid` DEĞİL — aldığımız çeki biz ödemeyiz).
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.invoicing.models import InvoiceDirection
from app.modules.progress_payments.models import ProgressPayment, ProgressPaymentStatus
from app.modules.treasury.models import FinancialInstrumentStatus, Payment, PaymentMethodKind
from app.modules.treasury.realized import (
    BINDING_INVOICE_INVALID,
    PAYMENT_NOT_REALIZED,
    SOURCE_NOT_INVOICED,
)
from tests._para_gercek import fatura_kes, odeme_yaz, parayi_yatir
from tests._silme_yardimci import sil_aile

pytestmark = pytest.mark.asyncio


async def test_G1_FATURASIZ_isveren_hakedisi_odendi_isaretlenemez(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)

    yanit = await client.post(f"/progress-payments/{payment_id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == SOURCE_NOT_INVOICED


async def test_G1_POZITIF_KONTROL_tahsil_edilmis_hakedis_GECER(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """🔴 İddianın ikinci yarısı: kapı "her şeyi reddet" hâline gelirse kırmızı."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    await parayi_yatir(seeded_db, payment_id, taseron=False)

    yanit = await client.post(f"/progress-payments/{payment_id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "paid"


async def test_KESINTILI_isveren_hakedisi_de_odenebilir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """🔴 DENETİM BULGUSU 1'in bu ailedeki bekçisi.

    Fatura, hakedişin KENDİ oranlarıyla ürünün para motorundan geçirilir
    (`invoicing.amounts.compute`). Kesintiler doluysa `total`, hakediş netinden
    KÜÇÜKTÜR; eşik net olsaydı bu hakediş asla kapanamazdı.
    """
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    from app.modules.progress_payments.models import ProgressPayment

    hakedis = await seeded_db.get(ProgressPayment, payment_id)
    assert hakedis is not None
    hakedis.advance_pct = Decimal("10.00")
    hakedis.retainage_pct = Decimal("5.00")
    hakedis.vat_pct = Decimal("20.00")
    await seeded_db.flush()

    await parayi_yatir(seeded_db, payment_id, taseron=False)

    yanit = await client.post(f"/progress-payments/{payment_id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 200, yanit.text


async def test_G2_PORTFOYDEKI_cek_TEK_BASINA_yetmez(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    await parayi_yatir(
        seeded_db, payment_id, taseron=False, evrak_durumu=FinancialInstrumentStatus.portfolio
    )

    yanit = await client.post(f"/progress-payments/{payment_id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text


async def test_G2_POZITIF_KONTROL_cek_TAHSIL_edilince_gecer(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """🔴 YÖN: işverenden ALINAN çek `collected` olunca para gelmiştir."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    await parayi_yatir(
        seeded_db, payment_id, taseron=False, evrak_durumu=FinancialInstrumentStatus.collected
    )

    yanit = await client.post(f"/progress-payments/{payment_id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 200, yanit.text


async def test_BULGU5_BAGSIZ_cek_odemesi_hakedisi_ODETMEZ(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """Bağsız kıymetli evrak ödemesi bu ailede de fail-closed'dır."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    await parayi_yatir(seeded_db, payment_id, taseron=False, method=PaymentMethodKind.cheque)

    yanit = await client.post(f"/progress-payments/{payment_id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == PAYMENT_NOT_REALIZED


async def test_G3_KISMI_odeme_yetmez(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    await parayi_yatir(seeded_db, payment_id, taseron=False, fark=Decimal("-0.01"))

    yanit = await client.post(f"/progress-payments/{payment_id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text


async def test_G3_SINIR_tam_esit_tutar_GECER(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """Sınır değeri: `<` yerine `<=` yazan mutant YALNIZ burada görünür."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    await parayi_yatir(seeded_db, payment_id, taseron=False, fark=Decimal("0.00"))

    yanit = await client.post(f"/progress-payments/{payment_id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 200, yanit.text


@pytest.mark.parametrize("uc", ["submit", "approve", "reject", "mark-paid", "unapprove"])
async def test_G4_paid_hicbir_gecisin_KAYNAGI_degildir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    hakedis_fabrikasi,
    uc: str,
) -> None:
    """PARA-GERCEK bir KAPI ekledi, tabloya ÇİFT EKLEMEDİ: `paid` TERMİNAL."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.paid)

    yanit = await client.post(
        f"/progress-payments/{payment_id}/{uc}",
        json={"reason": "gerekçe metni"},
        headers=admin_headers,
    )

    assert yanit.status_code == 409, yanit.text


async def test_kapi_TASERON_kolonunu_ISVEREN_hakedisiyle_karistirmaz(
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """🔴 İki ailenin FK'si farklıdır; toplama kolona GERÇEKTEN bağlıdır."""
    from app.modules.invoicing.models import Invoice
    from app.modules.treasury.realized import realized_total_for_source

    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    yatan = await parayi_yatir(seeded_db, payment_id, taseron=False)

    kendi = await realized_total_for_source(seeded_db, Invoice.progress_payment_id, payment_id)
    obur = await realized_total_for_source(
        seeded_db, Invoice.subcontractor_progress_payment_id, payment_id
    )

    assert kendi == yatan
    assert obur == Decimal("0")


async def test_kapi_ILGISIZ_bir_hakedisin_parasini_saymaz(
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """Sabit dönüş bekçisi: başka bir hakedişe yatan para bu hakedişi ödemez."""
    from app.modules.invoicing.models import Invoice
    from app.modules.treasury.realized import realized_total_for_source

    odenen = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    await parayi_yatir(seeded_db, odenen, taseron=False)

    toplam = await realized_total_for_source(seeded_db, Invoice.progress_payment_id, uuid.uuid4())
    assert toplam == Decimal("0")


async def test_G7_SIFIR_tutarli_baglayici_fatura_REDDEDILIR(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """Kalemsiz fatura bu ailede de kapıyı boşta geçiremez (kusur 1)."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    fatura = await fatura_kes(seeded_db, payment_id, taseron=False, kalemsiz=True)
    assert fatura.total == Decimal("0.00")

    yanit = await client.post(f"/progress-payments/{payment_id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == BINDING_INVOICE_INVALID


async def test_G8_YANLIS_YONLU_fatura_REDDEDILIR(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """🔴 YÖN BU AİLEDE TERSTİR ve asimetri burada ölçülür.

    İşveren hakedişinde para BİZE GELİR (`120 receivable` / `600 revenue`), yani
    bağlayıcı belge GİDEN faturadır. Buraya bağlanmış bir GELEN fatura, bizim
    ÖDEDİĞİMİZ bir borcu işverenin bize olan borcunun kapanması gibi gösterirdi.

    `SOURCE_DIRECTION` tablosu iki aileyi tek bir yöne eşitleseydi, taşeron
    testleri yeşil kalırken bu test kırmızıya dönerdi.
    """
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    ters = await fatura_kes(
        seeded_db, payment_id, taseron=False, direction=InvoiceDirection.incoming
    )
    assert ters.total > 0
    await odeme_yaz(seeded_db, ters, tutar=ters.total)

    yanit = await client.post(f"/progress-payments/{payment_id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == BINDING_INVOICE_INVALID


async def test_G8_POZITIF_KONTROL_GIDEN_fatura_GECER(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """Doğru yön (GİDEN) aynı tutarla GEÇER — reddin yönden geldiğinin kanıtı."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    dogru = await fatura_kes(
        seeded_db, payment_id, taseron=False, direction=InvoiceDirection.outgoing
    )
    await odeme_yaz(seeded_db, dogru, tutar=dogru.total)

    yanit = await client.post(f"/progress-payments/{payment_id}/mark-paid", headers=admin_headers)

    assert yanit.status_code == 200, yanit.text


# --------------------------------------------------------------------------- #
# 🔴 ODM-HAK — ödenmiş hakedişin ALTINDAKİ para ÇEKİLEMEZ
# --------------------------------------------------------------------------- #


async def test_ODENMIS_hakedisin_odemesi_silinince_hakedis_approved_a_doner(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """SIL-B2: `paid` damgası ödemeye dayanır. Ödeme (Sistem Yöneticisi, "her koşulda") silinirse
    damganın dayanağı gider; boş bir "Ödendi" rozeti KALMAZ: hakediş `approved`a döner."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    fatura = await fatura_kes(seeded_db, payment_id, taseron=False)
    odeme = await odeme_yaz(seeded_db, fatura, tutar=fatura.total)
    odeme_id = odeme.id

    gecis = await client.post(f"/progress-payments/{payment_id}/mark-paid", headers=admin_headers)
    assert gecis.status_code == 200, gecis.text

    silme = await sil_aile(client, admin_headers, "payment", odeme_id)

    assert silme.status_code == 204, silme.text
    kalan = await seeded_db.execute(
        select(func.count()).select_from(Payment).where(Payment.id == odeme_id)
    )
    assert kalan.scalar_one() == 0
    seeded_db.expire_all()
    hakedis = await seeded_db.get(ProgressPayment, payment_id)
    assert hakedis is not None
    assert hakedis.status is ProgressPaymentStatus.approved
    assert hakedis.paid_at is None


async def test_POZITIF_KONTROL_ODENMEMIS_hakedisin_odemesi_SILINEBILIR(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """🔴 İddianın ikinci yarısı: kapı "hakedişe bağlı hiçbir ödeme silinmez"
    hâline gelirse yanlış girilmiş bir tahsilat da düzeltilemez olurdu.

    AYNI kurulum, TEK fark `mark-paid`in koşmamış olmasıdır.
    """
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    fatura = await fatura_kes(seeded_db, payment_id, taseron=False)
    odeme = await odeme_yaz(seeded_db, fatura, tutar=fatura.total)

    odeme_id = odeme.id
    silme = await sil_aile(client, admin_headers, "payment", odeme_id)

    assert silme.status_code == 204, silme.text
    kalan = await seeded_db.execute(
        select(func.count()).select_from(Payment).where(Payment.id == odeme_id)
    )
    assert kalan.scalar_one() == 0

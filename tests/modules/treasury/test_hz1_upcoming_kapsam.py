"""HZ-1 T5 — sıralama · K10 · KAPSAM (IDOR) · N+1.

`test_hz1_upcoming.py`den TAŞINDI (800 satır tavanı); testler ve iddialar aynı.
Paylaşılan yardımcılar `_hz1_upcoming.py`dedir.
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.invoicing.models import InvoiceDirection, InvoiceStatus
from app.modules.payroll.models import PayrollLineStatus
from tests.modules.treasury._hz1_upcoming import (
    _bordro,
    _gun,
    _liste,
    _onay_zamani,
    _sorgu_sayaci,
)

pytestmark = pytest.mark.asyncio


# --- Sıralama --------------------------------------------------------------


async def test_vadeye_gore_ARTAN_siralama(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers,
    fatura_fabrikasi,
    taseron_hakedisi_fabrikasi,
) -> None:
    """E9:113/117/121 satır sırası vadeye göre artan; İKİ KAYNAK İÇ İÇE geçer.

    Kaynak kaynak sıralansaydı (önce faturalar, sonra hakedişler) en acil borç
    listenin ortasında kalırdı.
    """
    await fatura_fabrikasi(
        direction=InvoiceDirection.incoming, status=InvoiceStatus.approved, due_date=_gun(5)
    )
    await taseron_hakedisi_fabrikasi(approved_at=_onay_zamani(_gun(0)), payment_term_days=1)
    await fatura_fabrikasi(
        direction=InvoiceDirection.incoming, status=InvoiceStatus.approved, due_date=_gun(3)
    )
    items = await _liste(client, admin_headers)
    assert [s["days_remaining"] for s in items] == [1, 3, 5]


# --- K10 -------------------------------------------------------------------


async def test_K10_aciliyet_alani_ACILMAZ(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers,
    fatura_fabrikasi,
    bordro_donemi_fabrikasi,
) -> None:
    """🔴 Renk/aciliyet SUNUCUDA üretilmez (SA "EN HIZLI rozeti" kanonu).

    E9'un kodlaması kendi içinde tutarsızdır (2 gün turuncu, 3 gün KIRMIZI,
    7 gün yeşil); sunucu bir eşik uydurursa mockup'ın hangi yarısının doğru
    olduğuna karar vermiş olur.

    🔴 **TB8: bekçi BORDRO satırını da kapsar.** Alan kümesi satır BAŞINA
    denetlenir, yalnız ilk satırda değil: yeni kaynak kendi zarfını üretir ve
    oraya `urgency`/`period_label` gibi bir alan eklemek (ya da bir alanı
    düşürmek) mevcut iddianın altından kaçardı. Bordro satırı `counterparty`yi
    **`None`** taşır ama alanı YİNE DE taşır — kaynağa göre şekil değiştiren bir
    zarf, istemcide kaynak başına ayrı bir okuma yolu doğururdu.
    """
    await fatura_fabrikasi(
        direction=InvoiceDirection.incoming, status=InvoiceStatus.approved, due_date=_gun(2)
    )
    await bordro_donemi_fabrikasi(year=2026, month=7, payment_due_date=_gun(3))
    items = await _liste(client, admin_headers)
    assert len(_bordro(items)) == 1, items
    for satir in items:
        for yasak in ("urgency", "color", "severity", "badge", "level"):
            assert yasak not in satir
        assert set(satir) == {
            "source_type",
            "source_id",
            "counterparty",
            "document_no",
            "due_date",
            "days_remaining",
            "amount",
            "project_id",  # IZN-B4b onarımı (eklemeli): satır başına maske
        }


# --- Kapsam (IDOR) ---------------------------------------------------------


async def test_gorunmeyen_projenin_faturasi_SIZMAZ(
    client: AsyncClient,
    seeded_db: AsyncSession,
    kapsamli_muhasebe_headers,
    admin_headers,
    fatura_fabrikasi,
    gorunen_proje,
    gorunmeyen_proje,
) -> None:
    """🔴 K3 hesabı şirket geneli yapar; KAYNAKLAR proje kapsamlıdır.

    Süzgeç `invoicing.repository.scope_clause`tır — ikinci bir görünürlük
    tanımı yazılsaydı liste ucu ile bu uç zamanla ayrışırdı.
    """
    await fatura_fabrikasi(
        direction=InvoiceDirection.incoming,
        status=InvoiceStatus.approved,
        due_date=_gun(2),
        project=gorunen_proje,
        party_name="Görünen",
    )
    await fatura_fabrikasi(
        direction=InvoiceDirection.incoming,
        status=InvoiceStatus.approved,
        due_date=_gun(3),
        project=gorunmeyen_proje,
        party_name="Görünmeyen",
    )
    kisitli = await _liste(client, kapsamli_muhasebe_headers)
    assert [s["counterparty"] for s in kisitli] == ["Görünen"]
    tam = await _liste(client, admin_headers)
    assert len(tam) == 2


async def test_projesiz_fatura_GORUNUR(
    client: AsyncClient,
    seeded_db: AsyncSession,
    kapsamli_muhasebe_headers,
    fatura_fabrikasi,
    gorunen_proje,
    gorunmeyen_proje,
) -> None:
    """`project_id` NULL = ŞİRKET GENELİ fatura; modül izniyle görünür
    (`invoicing`in kendi kuralı, burada ikinci kez KARAR VERİLMEZ)."""
    await fatura_fabrikasi(
        direction=InvoiceDirection.incoming,
        status=InvoiceStatus.approved,
        due_date=_gun(2),
        project=None,
    )
    assert len(await _liste(client, kapsamli_muhasebe_headers)) == 1


async def test_gorunmeyen_projenin_hakedisi_SIZMAZ(
    client: AsyncClient,
    seeded_db: AsyncSession,
    kapsamli_muhasebe_headers,
    admin_headers,
    taseron_hakedisi_fabrikasi,
    gorunen_proje,
    gorunmeyen_proje,
) -> None:
    """Hakedişin `project_id`si NOT NULL'dır — "şirket geneli" hâli YOKTUR."""
    await taseron_hakedisi_fabrikasi(
        project=gorunen_proje,
        approved_at=_onay_zamani(_gun(0)),
        payment_term_days=1,
        subcontractor_name="Görünen Taşeron",
    )
    await taseron_hakedisi_fabrikasi(
        project=gorunmeyen_proje,
        approved_at=_onay_zamani(_gun(0)),
        payment_term_days=2,
        subcontractor_name="Görünmeyen Taşeron",
    )
    kisitli = await _liste(client, kapsamli_muhasebe_headers)
    assert [s["counterparty"] for s in kisitli] == ["Görünen Taşeron"]
    assert len(await _liste(client, admin_headers)) == 2


async def test_KAPSAM_bordro_satiri_PAYROLL_izni_ister(
    client: AsyncClient,
    seeded_db: AsyncSession,
    pm_headers,
    kapsamli_pm_headers,
    kapsamli_muhasebe_headers,
    admin_headers,
    bordro_donemi_fabrikasi,
    fatura_fabrikasi,
    taseron_hakedisi_fabrikasi,
    gorunen_proje,
    gorunmeyen_proje,
) -> None:
    """🔴 IDOR — bordro satırının görünürlüğü PROJE kapsamı DEĞİL, MODÜL iznidir.

    `PayrollPeriod`da `project_id` **YOKTUR** (ölçüldü): dönem şirket
    genelindedir, yani iki mevcut kaynağın proje süzgeci buraya UYGULANAMAZ.
    Süzgeç yazılmazsa satır HERKESE açılır ve matris burada AYRIŞIR:

        `"treasury": [_A, _F, _N, _N, _N, _F, _V, _N]`
        `"payroll":  [_A, _F, _N, _N, _F, _F, _N, _N]`

    `project_manager` bu ucu OKUR (`treasury=_V`) ama bordroya **HİÇ** erişimi
    yoktur (`payroll=_N`). Süzgeç düşerse bir proje müdürü şirketin AYLIK TOPLAM
    PERSONEL MALİYETİNİ okur — bu ucun sızdırdığı en hassas tek sayıdır.
    `admin_headers` bunu ASLA gösteremez (`payroll=_A`).

    🔴 **Kapı FAZLA GENİŞ de kapanmamalıdır.** `kapsamli_pm_headers`
    `kapsamli_muhasebe_headers`in ikizidir — proje kapsamları AYNIDIR, tek fark
    `payroll` iznidir. PM'in fatura ve hakediş satırlarını GÖRMEYE DEVAM ettiği
    ayrıca çakılır: `payroll` iznini ucun TAMAMINA uygulayan (ör. 403 döndüren
    ya da listeyi boşaltan) bir uygulama, iki çalışan kaynağı da susturur ve o
    kusur yalnız bu iddiayla yakalanır.
    """
    donem = await bordro_donemi_fabrikasi(
        year=2026,
        month=7,
        payment_due_date=_gun(3),
        lines=((PayrollLineStatus.approved, "892000.00"),),
    )
    await fatura_fabrikasi(
        direction=InvoiceDirection.incoming,
        status=InvoiceStatus.approved,
        due_date=_gun(2),
        project=None,
        party_name="Şirket Geneli",
    )
    await taseron_hakedisi_fabrikasi(
        project=gorunen_proje,
        approved_at=_onay_zamani(_gun(0)),
        payment_term_days=1,
        subcontractor_name="Görünen Taşeron",
    )

    # 1) Kapsamsız PM — bordroyu GÖRMEZ, şirket geneli faturayı görmeye devam eder.
    pm_items = await _liste(client, pm_headers)
    assert _bordro(pm_items) == [], pm_items
    assert "invoice" in {satir["source_type"] for satir in pm_items}

    # 2) Kapsamlı PM — muhasebenin İKİZİ; tek fark `payroll` izni.
    kapsamli_pm_items = await _liste(client, kapsamli_pm_headers)
    assert _bordro(kapsamli_pm_items) == [], kapsamli_pm_items
    assert {satir["source_type"] for satir in kapsamli_pm_items} == {
        "invoice",
        "subcontractor_progress_payment",
    }

    # 3) Muhasebe (`payroll=_F`) — AYNI kapsam, bordro satırı GÖRÜNÜR.
    muhasebe_items = await _liste(client, kapsamli_muhasebe_headers)
    muhasebe_bordro = _bordro(muhasebe_items)
    assert len(muhasebe_bordro) == 1, muhasebe_items
    assert muhasebe_bordro[0]["source_id"] == str(donem.id)
    assert Decimal(muhasebe_bordro[0]["amount"]) == Decimal("892000.00")
    assert {satir["source_type"] for satir in muhasebe_items} == {
        "invoice",
        "subcontractor_progress_payment",
        "payroll",
    }


# --- N+1 -------------------------------------------------------------------


async def test_N_ARTI_1_YAPMAZ(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers,
    fatura_fabrikasi,
    taseron_hakedisi_fabrikasi,
    bordro_donemi_fabrikasi,
) -> None:
    """🔴 Sorgu sayısı SATIR SAYISINDAN bağımsızdır — tahmin değil ÖLÇÜM.

    Hakediş tutarı `amounts.bulk_calculations`tan gelir (iki toplu sorgu);
    hakediş başına `calculation_for` çağıran bir uygulama burada patlar.

    🔴 **TB8: ölçüm ÜÇÜNCÜ kaynağı da kapsar** ve tuzak burada daha derindir.
    Bordronun tutarı bir kolon değil `Σ net_amount` TÜREVİDİR; en kolay
    uygulama dönem başına satırları çekmek ya da `summary.build_period_summary`i
    dönem başına çağırmaktır — ikisi de dönem sayısıyla büyür. Sorgu, dönem
    başına DEĞİL, `GROUP BY payroll_period_id` ile TEK seferde kurulmalıdır.

    On dönemin (yıl, ay)'ı FARKLI olmak zorundadır
    (`uq_payroll_periods_year_month`); fabrika bunu sayaçtan üretir.
    """
    await fatura_fabrikasi(
        direction=InvoiceDirection.incoming, status=InvoiceStatus.approved, due_date=_gun(1)
    )
    await taseron_hakedisi_fabrikasi(approved_at=_onay_zamani(_gun(0)), payment_term_days=1)
    await bordro_donemi_fabrikasi(payment_due_date=_gun(1))
    with _sorgu_sayaci() as az:
        assert len(await _liste(client, admin_headers)) == 3
    for _ in range(9):
        await fatura_fabrikasi(
            direction=InvoiceDirection.incoming, status=InvoiceStatus.approved, due_date=_gun(1)
        )
        await taseron_hakedisi_fabrikasi(approved_at=_onay_zamani(_gun(0)), payment_term_days=1)
        await bordro_donemi_fabrikasi(payment_due_date=_gun(1))
    with _sorgu_sayaci() as cok:
        items = await _liste(client, admin_headers)
    assert len(items) == 30
    assert len(_bordro(items)) == 10, items
    assert len(cok) == len(az), f"az={len(az)} çok={len(cok)}"

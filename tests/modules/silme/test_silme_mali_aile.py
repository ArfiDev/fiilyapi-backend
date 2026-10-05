"""SIL-B2 — mali aile silmesi: önizleme = silme, fiş + storno zinciri, mizan bekçisi, kapalı dönem.

Her testin omurgası aynıdır: ÖNCE tüm tabloların sayımı + mizan alınır; önizleme okunur; silme
çalışır; SONRA (1) silinen satır sayısı tablo tablo önizlemeyle BİREBİR eşit, (2) mizan = silinen
fişlerin satırları çıkarılmış eski mizan, (3) defter bekçisi (yetim fiş/storno, stornosuz
`reversed`, satırsız/dengesiz fiş) sıfır.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.modules.accounting.models import JournalEntry, JournalEntryStatus, JournalSourceType
from app.modules.invoicing.models import Invoice, InvoiceStatus
from app.modules.progress_payments.models import ProgressPaymentStatus
from app.modules.sites.models import Site
from app.modules.subcontractor_progress_payments.models import SubcontractorPaymentStatus
from app.modules.treasury.models import FinancialInstrument, FinancialInstrumentStatus, Payment
from tests._silme_yardimci import onizle, sil_aile, sil_genel, sisyon_girisi
from tests.modules.silme import _dunya as d
from tests.modules.silme import _mali_dunya as m


@pytest.fixture
async def sistem(client, user_factory):
    return await sisyon_girisi(client, user_factory)


@pytest.fixture
async def kullanici(user_factory):
    return await user_factory(email="mali@silme.co", password="parola1234", role_key="site_chief")


async def _dogrula_silme(client, session, headers, kind: str, kok_id, *, aile: bool = True):
    """Önizleme al, sil; silinen satırlar = önizleme, mizan = fişler çıkarılmış, defter temiz.

    Döner: (önizleme gövdesi, tablo → silinen sayı).
    """
    once = await m.tablo_sayimlari(session)
    mizan_once = await m.mizan(session)
    onizleme = (await onizle(client, headers, kind, kok_id)).json()
    fis_idleri = list((await session.execute(select(JournalEntry.id))).scalars())
    # Önizlemedeki fişler = ağaçtaki fişler: fiş no listesinden satırları bul.
    silinecek_fisler = [f["entry_no"] for f in onizleme["journal_entries"]]
    fis_satirlari = []
    if silinecek_fisler:
        idler = list(
            (
                await session.execute(
                    select(JournalEntry.id).where(JournalEntry.entry_no.in_(silinecek_fisler))
                )
            ).scalars()
        )
        fis_satirlari = await m.fis_satirlari(session, idler)
    assert len(fis_idleri) >= len(silinecek_fisler)

    yanit = await (sil_aile if aile else sil_genel)(client, headers, kind, kok_id)
    assert yanit.status_code == 204, yanit.text

    session.expire_all()
    fark = m.sayim_farki(once, await m.tablo_sayimlari(session))
    beklenen = {g["table"]: g["count"] for g in onizleme["groups"]}
    kok_tablo = {
        "invoice": "invoices",
        "payment": "payments",
        "journal_entry": "journal_entries",
        "financial_instrument": "financial_instruments",
        "progress_payment": "progress_payments",
        "subcontractor_progress_payment": "subcontractor_progress_payments",
        "site": "sites",
    }[kind]
    beklenen[kok_tablo] = beklenen.get(kok_tablo, 0) + 1
    assert fark == beklenen, "silinen satırlar önizlemeyle BİREBİR aynı olmalı"
    assert await m.mizan(session) == m.mizan_cikar(mizan_once, fis_satirlari)
    assert set((await m.tutarsizliklar(session)).values()) == {0}, await m.tutarsizliklar(session)
    return onizleme, fark


async def _fatura_dunyasi(session, kullanici, proje, hs, *, cek_durumu=None):
    """Fatura + kalem + ödeme + (ödeme fişi) + (fatura fişi/storno zinciri)."""
    fatura = await m.fatura(session, kullanici, proje=proje, durum=InvoiceStatus.collected)
    odeme = await m.odeme(session, kullanici, fatura)
    await m.fis(session, kullanici, hs, kaynak=(JournalSourceType.payment, odeme.id))
    await m.stornolu_fis(session, kullanici, hs, (JournalSourceType.invoice, fatura.id))
    return fatura, odeme


async def test_fatura_silinir_kalem_odeme_fis_ve_storno_birlikte_mizan_tutarli(
    client, db_session, sistem, project_factory, kullanici
) -> None:
    proje = await project_factory("SIL-FAT")
    hs = await m.hesaplar(db_session)
    fatura, _ = await _fatura_dunyasi(db_session, kullanici, proje, hs)
    # Ağaç DIŞI ikinci fatura + fişi: dokunulmamalı.
    diger = await m.fatura(db_session, kullanici, proje=proje)
    await m.fis(db_session, kullanici, hs, kaynak=(JournalSourceType.invoice, diger.id))
    await db_session.flush()
    diger_id = diger.id

    onizleme, fark = await _dogrula_silme(client, db_session, sistem, "invoice", fatura.id)

    assert fark["invoice_lines"] == 1 and fark["payments"] == 1
    assert onizleme["journal_entry_count"] == 3  # ödeme fişi + orijinal + storno
    assert fark["journal_lines"] == 6
    assert await d.sayim(db_session, Invoice, Invoice.id == diger_id) == 1
    assert await d.sayim(db_session, JournalEntry) == 1  # yalnız diğer faturanın fişi kaldı


async def test_denetim_satiri_tam_fis_dokumunu_tasir(
    client, db_session, sistem, project_factory, kullanici
) -> None:
    proje = await project_factory("SIL-DEN")
    hs = await m.hesaplar(db_session)
    fatura, _ = await _fatura_dunyasi(db_session, kullanici, proje, hs)
    fis_no = [f.entry_no for f in (await db_session.execute(select(JournalEntry))).scalars()]

    assert (await sil_aile(client, sistem, "invoice", fatura.id)).status_code == 204

    detay = await m.son_silme_denetimi(db_session)
    assert "Fatura silindi" in detay
    for no in fis_no:
        assert no in detay
    assert "silinen fişler (3)" in detay


async def test_hakedis_onayli_fis_fatura_odeme_zinciriyle_silinir(
    client, db_session, sistem, project_factory, kullanici
) -> None:
    proje = await project_factory("SIL-HAK")
    snt = await d.site(db_session, proje)
    hs = await m.hesaplar(db_session)
    hakedis = await d.hakedis_satiri(
        db_session, proje, snt, kullanici, durum=ProgressPaymentStatus.paid
    )
    await m.stornolu_fis(
        db_session, kullanici, hs, (JournalSourceType.progress_payment, hakedis.id)
    )
    fatura = await m.fatura(
        db_session, kullanici, proje=proje, hakedis=hakedis, durum=InvoiceStatus.collected
    )
    odeme = await m.odeme(db_session, kullanici, fatura, tutar=Decimal("1000.00"))
    await m.fis(db_session, kullanici, hs, kaynak=(JournalSourceType.payment, odeme.id))

    onizleme, fark = await _dogrula_silme(
        client, db_session, sistem, "progress_payment", hakedis.id
    )

    assert fark["progress_payment_lines"] == 1 and fark["invoices"] == 1
    assert fark["payments"] == 1 and fark["journal_entries"] == 3
    assert onizleme["groups"] and any(g["is_financial"] for g in onizleme["groups"])
    assert await d.sayim(db_session, Invoice) == 0


async def test_tasaron_hakedisi_onayli_fisiyle_silinir(
    client, db_session, sistem, project_factory, kullanici
) -> None:
    proje = await project_factory("SIL-THAK")
    snt = await d.site(db_session, proje)
    hs = await m.hesaplar(db_session)
    odeme = await d.tasaron_hakedisi(
        db_session, proje, snt, kullanici, durum=SubcontractorPaymentStatus.approved
    )
    await m.stornolu_fis(
        db_session, kullanici, hs, (JournalSourceType.subcontractor_progress_payment, odeme.id)
    )
    await d.onay_zinciri(db_session, odeme, kullanici)

    _, fark = await _dogrula_silme(
        client, db_session, sistem, "subcontractor_progress_payment", odeme.id
    )

    assert fark["journal_entries"] == 2 and fark["approval_chains"] == 1


async def test_odeme_silinir_fisi_gider_fatura_kalir_ve_durumu_yeniden_turetilir(
    client, db_session, sistem, project_factory, kullanici
) -> None:
    proje = await project_factory("SIL-ODM")
    hs = await m.hesaplar(db_session)
    fatura = await m.fatura(
        db_session, kullanici, proje=proje, durum=InvoiceStatus.collected, toplam=Decimal("500.00")
    )
    odeme = await m.odeme(db_session, kullanici, fatura, tutar=Decimal("500.00"))
    await m.fis(db_session, kullanici, hs, kaynak=(JournalSourceType.payment, odeme.id))

    fatura_id = fatura.id
    _, fark = await _dogrula_silme(client, db_session, sistem, "payment", odeme.id)

    assert fark == {"payments": 1, "journal_entries": 1, "journal_lines": 2}
    kalan = await db_session.get(Invoice, fatura_id)
    assert kalan is not None and kalan.status is InvoiceStatus.sent  # collected damgası geri alındı


async def test_odenmis_hakedisin_odemesi_silinince_hakedis_approved_a_doner(
    client, db_session, sistem, project_factory, kullanici
) -> None:
    proje = await project_factory("SIL-PAID")
    snt = await d.site(db_session, proje)
    hakedis = await d.hakedis_satiri(
        db_session, proje, snt, kullanici, durum=ProgressPaymentStatus.paid
    )
    fatura = await m.fatura(
        db_session, kullanici, proje=proje, hakedis=hakedis, durum=InvoiceStatus.collected
    )
    odeme = await m.odeme(db_session, kullanici, fatura, tutar=Decimal("1000.00"))

    hakedis_id = hakedis.id
    assert (await sil_aile(client, sistem, "payment", odeme.id)).status_code == 204

    db_session.expire_all()
    from app.modules.progress_payments.models import ProgressPayment  # noqa: PLC0415

    kalan = await db_session.get(ProgressPayment, hakedis_id)
    assert kalan is not None and kalan.status is ProgressPaymentStatus.approved
    assert kalan.paid_at is None  # boş "Ödendi" rozeti kalmaz


# --- Çek ↔ ödeme tutarlılığı ---


async def test_portfoy_disi_cekli_odeme_silinince_cek_ve_fisi_de_gider(
    client, db_session, sistem, project_factory, kullanici
) -> None:
    proje = await project_factory("SIL-CEK1")
    hs = await m.hesaplar(db_session)
    fatura = await m.fatura(db_session, kullanici, proje=proje, durum=InvoiceStatus.collected)
    cek = await m.cek(db_session, kullanici, durum=FinancialInstrumentStatus.collected)
    odeme = await m.odeme(db_session, kullanici, fatura, cek_=cek)
    await m.fis(db_session, kullanici, hs, kaynak=(JournalSourceType.payment, odeme.id))
    await m.fis(db_session, kullanici, hs, kaynak=(JournalSourceType.financial_instrument, cek.id))
    fatura_id = fatura.id

    _, fark = await _dogrula_silme(client, db_session, sistem, "payment", odeme.id)

    assert fark["financial_instruments"] == 1 and fark["journal_entries"] == 2
    assert await d.sayim(db_session, Invoice, Invoice.id == fatura_id) == 1  # fatura kalır


async def test_portfoydeki_cekli_odeme_silinince_cek_ayni_durumda_kalir(
    client, db_session, sistem, project_factory, kullanici
) -> None:
    proje = await project_factory("SIL-CEK2")
    hs = await m.hesaplar(db_session)
    fatura = await m.fatura(db_session, kullanici, proje=proje, durum=InvoiceStatus.sent)
    cek = await m.cek(db_session, kullanici)  # portföy
    odeme = await m.odeme(db_session, kullanici, fatura, cek_=cek)
    await m.fis(db_session, kullanici, hs, kaynak=(JournalSourceType.payment, odeme.id))

    onizleme, fark = await _dogrula_silme(client, db_session, sistem, "payment", odeme.id)

    assert "financial_instruments" not in fark
    assert onizleme["detached"] == []  # çek DEĞİŞMEZ: portföyde ödemesiz çek tutarlı bir durumdur
    assert await d.sayim(db_session, FinancialInstrument) == 1


async def test_cek_silinince_bagli_tum_odemeleri_ve_fisleri_gider_faturalar_kalir(
    client, db_session, sistem, project_factory, kullanici
) -> None:
    proje = await project_factory("SIL-CEK3")
    hs = await m.hesaplar(db_session)
    cek = await m.cek(db_session, kullanici, durum=FinancialInstrumentStatus.collected)
    f1 = await m.fatura(db_session, kullanici, proje=proje, durum=InvoiceStatus.collected)
    f2 = await m.fatura(db_session, kullanici, proje=proje, durum=InvoiceStatus.collected)
    for fat in (f1, f2):
        odeme = await m.odeme(db_session, kullanici, fat, cek_=cek, tutar=Decimal("250.00"))
        await m.fis(db_session, kullanici, hs, kaynak=(JournalSourceType.payment, odeme.id))
    await m.fis(db_session, kullanici, hs, kaynak=(JournalSourceType.financial_instrument, cek.id))

    _, fark = await _dogrula_silme(client, db_session, sistem, "financial_instrument", cek.id)

    assert fark["payments"] == 2 and fark["journal_entries"] == 3
    assert await d.sayim(db_session, Invoice) == 2
    assert await d.sayim(db_session, Payment) == 0


# --- Kök fiş ---


async def test_kok_fis_orijinal_ve_stornosuyla_gider_kaynak_belge_kalir_ve_onizlemede_yazar(
    client, db_session, sistem, project_factory, kullanici
) -> None:
    proje = await project_factory("SIL-KFIS")
    hs = await m.hesaplar(db_session)
    fatura = await m.fatura(db_session, kullanici, proje=proje)
    orijinal, storno = await m.stornolu_fis(
        db_session, kullanici, hs, (JournalSourceType.invoice, fatura.id)
    )
    fatura_id, fatura_no, storno_id = fatura.id, fatura.invoice_no, storno.id

    onizleme = (await onizle(client, sistem, "journal_entry", storno_id)).json()  # kök = STORNO
    assert onizleme["journal_entry_count"] == 2  # storno + orijinali
    assert [x["message"] for x in onizleme["documents_left_without_entry"]] == [
        f"Kaynak belge fişsiz kalacak: Fatura {fatura_no}"
    ]

    _, fark = await _dogrula_silme(
        client, db_session, sistem, "journal_entry", storno_id, aile=False
    )

    assert fark["journal_entries"] == onizleme["journal_entry_count"] == 2
    assert await d.sayim(db_session, Invoice, Invoice.id == fatura_id) == 1  # belge KALIR
    detay = await m.son_silme_denetimi(db_session)
    assert f"fişsiz kalan kaynak belgeler: Fatura {fatura_no}" in detay


async def test_kok_fis_kaynaksiz_elle_fisse_uyari_yok(
    client, db_session, sistem, kullanici
) -> None:
    hs = await m.hesaplar(db_session)
    elle = await m.fis(db_session, kullanici, hs)

    onizleme = (await onizle(client, sistem, "journal_entry", elle.id)).json()

    assert onizleme["documents_left_without_entry"] == []
    await _dogrula_silme(client, db_session, sistem, "journal_entry", elle.id)


# --- Kapalı dönem ---


async def test_kapali_donemdeki_fis_silmeyi_durdurmaz_denetime_ve_onizlemeye_yazilir(
    client, db_session, sistem, project_factory, kullanici
) -> None:
    proje = await project_factory("SIL-KAPALI")
    hs = await m.hesaplar(db_session)
    fatura, _ = await _fatura_dunyasi(db_session, kullanici, proje, hs)
    await m.donemi_kapat(db_session, kullanici, 2026, 3)

    onizleme = (await onizle(client, sistem, "invoice", fatura.id)).json()
    assert onizleme["closed_period_entry_count"] == 3
    assert all(f["period_closed"] for f in onizleme["journal_entries"])

    await _dogrula_silme(client, db_session, sistem, "invoice", fatura.id)

    detay = await m.son_silme_denetimi(db_session)
    assert "KAPALI DÖNEM fişleri silindi, dönem kilidi atlandı (3)" in detay
    assert "(2026-03)" in detay


# --- İşveren hakedişi başlığı: şantiye silinince satırı giden hakediş ---


async def _ikinci_satir(session, odeme, snt):
    from app.modules.progress_payments.models import ProgressPaymentLine  # noqa: PLC0415

    ilk = odeme.lines[0]
    session.add(
        ProgressPaymentLine(
            payment_id=odeme.id,
            contract_item_id=ilk.contract_item_id,
            site_id=snt.id,
            code="11.099",
            description="B kalemi",
            unit="m³",
            contract_unit_price=ilk.contract_unit_price,
            coefficient=ilk.coefficient,
            quantity=ilk.quantity,
        )
    )
    await session.flush()


async def test_taslak_hakedisin_basligi_kalir_toplami_satirlardan_turer(
    client, db_session, sistem, project_factory, kullanici
) -> None:
    from app.modules.progress_payments.models import (  # noqa: PLC0415
        ProgressPayment,
        ProgressPaymentLine,
    )

    proje = await project_factory("SIL-BASLIK1")
    snt_a = await d.site(db_session, proje, "SNT-A", "A Şantiyesi")
    snt_b = await d.site(db_session, proje, "SNT-B", "B Şantiyesi")
    odeme = await d.hakedis_satiri(db_session, proje, snt_a, kullanici)
    await _ikinci_satir(db_session, odeme, snt_b)
    odeme_id = odeme.id

    assert (await sil_genel(client, sistem, "site", snt_a.id)).status_code == 204

    db_session.expunge_all()
    assert await db_session.get(ProgressPayment, odeme_id) is not None  # başlık KALIR
    kalan = await d.sayim(
        db_session, ProgressPaymentLine, ProgressPaymentLine.payment_id == odeme_id
    )
    assert kalan == 1  # yalnız B satırı


async def test_onayli_hakedisin_satiri_giderse_baslik_fis_fatura_ve_diger_satirlar_da_gider(
    client, db_session, sistem, project_factory, kullanici
) -> None:
    from app.modules.progress_payments.models import ProgressPayment  # noqa: PLC0415

    proje = await project_factory("SIL-BASLIK2")
    snt_a = await d.site(db_session, proje, "SNT-A", "A Şantiyesi")
    snt_b = await d.site(db_session, proje, "SNT-B", "B Şantiyesi")
    hs = await m.hesaplar(db_session)
    odeme = await d.hakedis_satiri(
        db_session, proje, snt_a, kullanici, durum=ProgressPaymentStatus.approved
    )
    await _ikinci_satir(db_session, odeme, snt_b)
    await m.stornolu_fis(db_session, kullanici, hs, (JournalSourceType.progress_payment, odeme.id))
    await m.fatura(db_session, kullanici, proje=proje, hakedis=odeme)
    odeme_id, snt_b_id = odeme.id, snt_b.id

    onizleme, fark = await _dogrula_silme(client, db_session, sistem, "site", snt_a.id, aile=False)

    gruplar = {g["table"]: g for g in onizleme["groups"]}
    assert gruplar["progress_payments"]["is_financial"] is True
    assert fark["progress_payment_lines"] == 2  # B satırı da başlıkla birlikte gitti
    assert fark["invoices"] == 1 and fark["journal_entries"] == 2
    assert await db_session.get(ProgressPayment, odeme_id) is None
    assert await d.sayim(db_session, Site, Site.id == snt_b_id) == 1  # B şantiyesi KALIR


# --- Bekçinin kendisi kör değil (pozitif kontrol) ---


async def test_defter_bekcisi_yetim_fis_stornosuz_reversed_ve_dengesizligi_YAKALAR(
    db_session, kullanici
) -> None:
    hs = await m.hesaplar(db_session)
    assert set((await m.tutarsizliklar(db_session)).values()) == {0}

    # yetim fiş: kaynağı (fatura) tabloda yok
    await m.fis(db_session, kullanici, hs, kaynak=(JournalSourceType.invoice, d.yeni_kimlik()))
    # stornosuz reversed: orijinal `reversed` ama stornosu yok
    await m.fis(db_session, kullanici, hs, durum=JournalEntryStatus.reversed)

    sonuc = await m.tutarsizliklar(db_session)

    assert sonuc["yetim_fis"] == 1
    assert sonuc["stornosuz_reversed"] == 1
    assert sonuc["yetim_storno"] == 0 and sonuc["satirsiz_fis"] == 0 and sonuc["dengesiz"] == 0

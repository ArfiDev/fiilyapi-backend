"""SIL-B2 onarımı (kapsam görünürlüğü): zincirleme silme başka projelere uzanır ve ağaç DIŞINDAKİ
kayıtların durumu değişir; ikisi de önizlemede VE denetimde görünür.

Zincir: şantiye → onaylı hakediş satırı → hakediş başlığı → fatura → ödeme → portföy dışı çek →
çekin DİĞER ödemesi (başka projenin faturasında). B projesinin faturası/hakedişi silinmez; yalnız
ödemesi gider, faturanın `collected` damgası ve hakedişin `paid` damgası geri alınır.
"""

from decimal import Decimal

import pytest

from app.modules.invoicing.models import Invoice, InvoiceStatus
from app.modules.progress_payments.models import ProgressPayment, ProgressPaymentStatus
from app.modules.treasury.models import FinancialInstrumentStatus, Payment
from tests._silme_yardimci import onizle, sil_genel, sisyon_girisi
from tests.modules.silme import _dunya as d
from tests.modules.silme import _mali_dunya as m


@pytest.fixture
async def sistem(client, user_factory):
    return await sisyon_girisi(client, user_factory)


@pytest.fixture
async def kullanici(user_factory):
    return await user_factory(email="kapsam@silme.co", password="parola1234", role_key="site_chief")


async def _zincir(db_session, project_factory, kullanici):
    """A projesi (şantiye + onaylı hakediş + faturası + çekli ödemesi) ve B projesi (aynı çeke
    bağlı ödemeli faturası + `paid` hakedişi). Döner: (snt_a, proje_b, fatura_b, hakedis_b)."""
    proje_a = await project_factory("SIL-KPS-A")
    proje_b = await project_factory("SIL-KPS-B")
    snt_a = await d.site(db_session, proje_a, "SNT-A", "A Şantiyesi")
    snt_b = await d.site(db_session, proje_b, "SNT-B", "B Şantiyesi")
    cek = await m.cek(db_session, kullanici, durum=FinancialInstrumentStatus.collected)

    hakedis_a = await d.hakedis_satiri(
        db_session, proje_a, snt_a, kullanici, durum=ProgressPaymentStatus.approved
    )
    fatura_a = await m.fatura(
        db_session, kullanici, proje=proje_a, durum=InvoiceStatus.collected, hakedis=hakedis_a
    )
    await m.odeme(db_session, kullanici, fatura_a, cek_=cek, tutar=Decimal("250.00"))

    hakedis_b = await d.hakedis_satiri(
        db_session, proje_b, snt_b, kullanici, durum=ProgressPaymentStatus.paid
    )
    fatura_b = await m.fatura(
        db_session, kullanici, proje=proje_b, durum=InvoiceStatus.collected, hakedis=hakedis_b
    )
    await m.odeme(db_session, kullanici, fatura_b, cek_=cek, tutar=Decimal("250.00"))
    return snt_a, proje_b, fatura_b, hakedis_b


async def test_zincir_diger_projeye_uzanir_onizleme_ve_denetim_proje_ile_durum_degisimini_yazar(
    client, db_session, sistem, project_factory, kullanici
) -> None:
    snt_a, proje_b, fatura_b, hakedis_b = await _zincir(db_session, project_factory, kullanici)
    proje_b_id, proje_b_ad = proje_b.id, proje_b.name
    fatura_b_id, fatura_b_no, hakedis_b_id = fatura_b.id, fatura_b.invoice_no, hakedis_b.id

    onizleme = (await onizle(client, sistem, "site", snt_a.id)).json()

    # (a) B projesinden yalnız ÖDEME gider (fatura ve hakediş kalır): 1 kayıt.
    assert onizleme["other_projects"] == [
        {"project_id": str(proje_b_id), "name": proje_b_ad, "count": 1}
    ]
    # (b) Ağaç dışı iki durum değişikliği: fatura `collected → sent`, hakediş `paid → approved`.
    degisimler = {x["kind"]: x for x in onizleme["status_changes"]}
    assert set(degisimler) == {"invoice", "progress_payment"}
    assert degisimler["invoice"] == {
        "kind": "invoice",
        "label": f"Fatura {fatura_b_no}",
        "from": "collected",
        "to": "sent",
    }
    assert degisimler["progress_payment"]["from"] == "paid"
    assert degisimler["progress_payment"]["to"] == "approved"
    assert degisimler["progress_payment"]["label"] == f"İşveren hakedişi #1 · {proje_b_ad}"
    # Ödeme örneği yalnız tarih değil: fatura no ve proje adı içerir.
    odeme_grubu = next(g for g in onizleme["groups"] if g["table"] == "payments")
    assert any("F-" in o and "SIL-KPS-A" not in o and " · " in o for o in odeme_grubu["samples"])
    assert not any(o.startswith("2026-") for o in odeme_grubu["samples"])

    assert (await sil_genel(client, sistem, "site", snt_a.id)).status_code == 204

    # Önizlemenin söylediği durum değişiklikleri GERÇEKTEN olur (ağaç dışında kalan kayıtlar).
    db_session.expire_all()
    assert (await db_session.get(Invoice, fatura_b_id)).status is InvoiceStatus.sent
    assert (await db_session.get(ProgressPayment, hakedis_b_id)).status is (
        ProgressPaymentStatus.approved
    )
    assert await d.sayim(db_session, Payment, Payment.invoice_id == fatura_b_id) == 0
    # Denetim satırı ikisini de taşır.
    detay = await m.son_silme_denetimi(db_session)
    assert f"BAŞKA PROJELERDEN silinenler: {proje_b_ad} 1" in detay
    assert f"Fatura {fatura_b_no}: collected → sent" in detay
    assert "paid → approved" in detay
    assert set((await m.tutarsizliklar(db_session)).values()) == {0}


async def test_tek_projede_kalan_silmede_diger_proje_ve_durum_degisikligi_bos(
    client, db_session, sistem, project_factory, kullanici
) -> None:
    proje = await project_factory("SIL-KPS-C")
    snt = await d.site(db_session, proje)
    await d.hakedis_satiri(db_session, proje, snt, kullanici)  # taslak: başlık kalır

    onizleme = (await onizle(client, sistem, "site", snt.id)).json()

    assert onizleme["other_projects"] == [] and onizleme["status_changes"] == []
    assert (await sil_genel(client, sistem, "site", snt.id)).status_code == 204
    detay = await m.son_silme_denetimi(db_session)
    assert "BAŞKA PROJELERDEN" not in detay and "durumu değişen" not in detay

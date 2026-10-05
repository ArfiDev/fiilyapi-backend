"""HZ-1 T4 — ödeme uçları (spec §4, uçlar 6, 7, 8).

| # | Uç | İzin |
|---|---|---|
| 6 | `GET /invoices/{id}/payments` | `invoicing` **view** |
| 7 | `POST /invoices/{id}/payments` | `invoicing` **full** |
| 8 | `DELETE /payments/{id}` | `invoicing` **admin** |

🔴 İzin modülü `treasury` DEĞİL `invoicing`tir: ödeme bir FATURAYA kaydedilir ve
faturanın kapsam süzgecinden (`visible_invoice`) geçer. İki matris satırı birebir
aynı olduğu için aynı başlık fixture'ları kullanılır.

## Bu dosyanın kilitlediği kararlar

1. **K6 — AŞIRI TAHSİLAT 422, TOLERANS YOK.** İki sınır da testlidir: `= total`
   GEÇER, `total + 0.01` REDDEDİLİR. Karşılaştırma `Decimal` üzerindedir; bir
   epsilon toleransı eklenseydi `total + 0.01` testi kırmızıya dönerdi.
2. **K5 — `paid_amount` KOLONU YOKTUR.** `paid_total`/`remaining` her okumada
   Σ payments'ten TÜRETİLİR ve 🔴 **SAYFADAN DEĞİL TÜM SATIRLARDAN** gelir
   (`test_liste_toplamlari_SAYFADAN_DEGIL_TUM_SATIRLARDAN`) — sayfadan
   hesaplansaydı ikinci sayfada "kalan" yanlış büyürdü.
3. **K5 — durum Σ'dan TÜRETİLEREK damgalanır**, uydurulmaz: yalnız matrisin
   TANIDIĞI geçiş (`sent → collected`) damgalanır. `draft` bir giden fatura tam
   ödense bile durumu DEĞİŞMEZ (matriste `(draft, mark-collected)` çifti yoktur)
   ve gelen faturada durum Hazine kapsamında hiç değişmez.
4. **Silme durumu YENİDEN TÜRETİR:** `collected` → `sent`e düşer. Geri düşüşün
   hedefi de uydurulmaz, `OUTGOING_TRANSITIONS`tan türetilir.
5. **Silme yalnız `admin`**; `full` (muhasebe) 403 alır — ön koşulludur: aynı
   kullanıcı POST'u GEÇER, yani 403 seviyeden gelir, faturaya erişememesinden
   değil.
6. **Görünmeyen/olmayan fatura ve ödeme 404** — ödemenin kendisi VARSA bile
   faturası görünmüyorsa cümle AYNIDIR (403 kaydın varlığını ele verirdi).
7. **Gövde içi `bank_account_id` yoksa 404** (ST kanonu); PASİF hesap ise
   **422**dir: kullanımdan kaldırma yolu `is_active=false`tur ve oraya yeni para
   yazılabilseydi o bayrak tamamen süs olurdu.
8. Yeni `AuditAction` üyesi AÇILMADI (TB3/T3 kanonu): ayrım METİNDEDİR.
"""

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.modules.audit.models import AuditAction, AuditLog
from app.modules.invoicing.models import Invoice, InvoiceDirection, InvoiceStatus
from tests._silme_yardimci import sil_aile

_ODEME_YOLU = "/payments"


def _fatura_yolu(invoice: Invoice) -> str:
    return f"/invoices/{invoice.id}/payments"


def _govde(account, amount: str, **fazlasi) -> dict:  # noqa: ANN001
    return {
        "bank_account_id": str(account.id),
        "method": "transfer",
        "amount": amount,
        "paid_on": "2026-08-14",
        **fazlasi,
    }


async def _durum(seeded_db, invoice: Invoice) -> InvoiceStatus:  # noqa: ANN001
    """Durum DB'den TAZE okunur: uçtan dönen gövdeye bakmak, damganın gerçekten
    yazıldığını değil yalnızca hesaplandığını kanıtlardı."""
    await seeded_db.refresh(invoice)
    return invoice.status


# --------------------------------------------------------------------------- #
# Uç 6 — GET /invoices/{id}/payments
# --------------------------------------------------------------------------- #


async def test_liste_odemesiz_faturada_paid_total_SIFIR_remaining_TOTAL(
    client, pm_headers, fatura_fabrikasi
) -> None:
    """Ödemesiz faturada `SUM()` NULL döner; `coalesce` düşerse `paid_total`
    boş/`null` basar ve `remaining` NULL'a çöker."""
    invoice = await fatura_fabrikasi(total="1000.00")
    resp = await client.get(_fatura_yolu(invoice), headers=pm_headers)
    assert resp.status_code == 200, resp.text
    govde = resp.json()
    assert govde["items"] == []
    assert govde["total"] == 0
    assert Decimal(govde["paid_total"]) == Decimal("0.00")
    assert Decimal(govde["remaining"]) == Decimal("1000.00")


async def test_liste_odemeleri_paid_total_ve_remaining_ile_doner(
    client, pm_headers, fatura_fabrikasi, hesap_fabrikasi, fatura_odemesi
) -> None:
    invoice = await fatura_fabrikasi(total="1000.00")
    account = await hesap_fabrikasi()
    await fatura_odemesi(invoice, account, "300.00")
    await fatura_odemesi(invoice, account, "200.00")

    resp = await client.get(_fatura_yolu(invoice), headers=pm_headers)
    assert resp.status_code == 200, resp.text
    govde = resp.json()
    assert govde["total"] == 2
    assert len(govde["items"]) == 2
    assert Decimal(govde["paid_total"]) == Decimal("500.00")
    assert Decimal(govde["remaining"]) == Decimal("500.00")
    assert {satir["bank_account_id"] for satir in govde["items"]} == {str(account.id)}


async def test_liste_toplamlari_SAYFADAN_DEGIL_TUM_SATIRLARDAN(
    client, pm_headers, fatura_fabrikasi, hesap_fabrikasi, fatura_odemesi
) -> None:
    """🔴 `limit=1` verilse bile `paid_total`/`remaining` TÜM satırlardan gelir.

    Sayfadan hesaplansaydı ikinci sayfada "kalan" birdenbire büyür, kullanıcı
    ödenmiş bir faturaya ikinci kez tahsilat girmeye çalışırdı (ve K6 onu 422 ile
    karşılardı — ekran ile sunucu ayrışırdı).
    """
    invoice = await fatura_fabrikasi(total="1000.00")
    account = await hesap_fabrikasi()
    await fatura_odemesi(invoice, account, "300.00")
    await fatura_odemesi(invoice, account, "200.00")

    resp = await client.get(f"{_fatura_yolu(invoice)}?limit=1", headers=pm_headers)
    assert resp.status_code == 200, resp.text
    govde = resp.json()
    assert len(govde["items"]) == 1
    assert govde["total"] == 2
    assert Decimal(govde["paid_total"]) == Decimal("500.00")
    assert Decimal(govde["remaining"]) == Decimal("500.00")


async def test_liste_limit_tavani_asimi_422(client, pm_headers, fatura_fabrikasi) -> None:
    """TB3 kanonu: tavan aşımı sessizce KIRPILMAZ."""
    invoice = await fatura_fabrikasi()
    resp = await client.get(f"{_fatura_yolu(invoice)}?limit=201", headers=pm_headers)
    assert resp.status_code == 422, resp.text


async def test_liste_olmayan_fatura_404(client, pm_headers) -> None:
    resp = await client.get(f"/invoices/{uuid.uuid4()}/payments", headers=pm_headers)
    assert resp.status_code == 404, resp.text


async def test_liste_gorunmeyen_fatura_404(
    client, muhasebe_headers, fatura_fabrikasi, project_factory
) -> None:
    """Muhasebe kullanıcısının `user_project_access` satırı YOKTUR (K3 gereği bu
    pakette kapsam fixture'ı kurulmaz): projeli fatura ona GÖRÜNMEZ."""
    proje = await project_factory(code="HZ-P01", name="Görünmeyen Proje")
    invoice = await fatura_fabrikasi(project=proje)
    resp = await client.get(_fatura_yolu(invoice), headers=muhasebe_headers)
    assert resp.status_code == 404, resp.text


async def test_liste_yetkisiz_rol_403(client, yetkisiz_headers, fatura_fabrikasi) -> None:
    invoice = await fatura_fabrikasi()
    resp = await client.get(_fatura_yolu(invoice), headers=yetkisiz_headers)
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# Uç 7 — POST /invoices/{id}/payments
# --------------------------------------------------------------------------- #


async def test_odeme_ekleme_full_gecer_ve_listede_gorunur(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi, odeme_eslemesi
) -> None:
    invoice = await fatura_fabrikasi(total="1000.00")
    account = await hesap_fabrikasi()
    resp = await client.post(
        _fatura_yolu(invoice), headers=muhasebe_headers, json=_govde(account, "400.00")
    )
    assert resp.status_code == 201, resp.text
    olusan = resp.json()
    assert Decimal(olusan["amount"]) == Decimal("400.00")
    assert olusan["invoice_id"] == str(invoice.id)
    assert olusan["method"] == "transfer"

    liste = await client.get(_fatura_yolu(invoice), headers=muhasebe_headers)
    assert Decimal(liste.json()["remaining"]) == Decimal("600.00")


async def test_odeme_ekleme_hesap_bakiyesine_YANSIR(
    client, muhasebe_headers, admin_headers, fatura_fabrikasi, hesap_fabrikasi, odeme_eslemesi
) -> None:
    """K2 ile K4'ün dikişi: giden faturanın ödemesi hesaba GİRİŞTİR.

    Bakiye saklanmadığı için (K2) bu yansımayı doğrulayan tek şey bu iddiadır.
    """
    invoice = await fatura_fabrikasi(direction=InvoiceDirection.outgoing, total="1000.00")
    account = await hesap_fabrikasi(opening_balance="100.00")
    resp = await client.post(
        _fatura_yolu(invoice), headers=muhasebe_headers, json=_govde(account, "400.00")
    )
    assert resp.status_code == 201, resp.text

    kart = await client.get(f"/bank-accounts/{account.id}", headers=admin_headers)
    assert Decimal(kart.json()["balance"]) == Decimal("500.00")


async def test_odeme_ekleme_view_rolu_403(
    client, pm_headers, fatura_fabrikasi, hesap_fabrikasi
) -> None:
    invoice = await fatura_fabrikasi()
    account = await hesap_fabrikasi()
    resp = await client.post(
        _fatura_yolu(invoice), headers=pm_headers, json=_govde(account, "1.00")
    )
    assert resp.status_code == 403, resp.text


async def test_K6_TAM_TOPLAM_gecer(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi, odeme_eslemesi
) -> None:
    """🔴 SINIR 1: `Σ + yeni == total` GEÇER — eşitlik aşım DEĞİLDİR."""
    invoice = await fatura_fabrikasi(total="1000.00")
    account = await hesap_fabrikasi()
    resp = await client.post(
        _fatura_yolu(invoice), headers=muhasebe_headers, json=_govde(account, "1000.00")
    )
    assert resp.status_code == 201, resp.text


async def test_K6_BIR_KURUS_asim_422(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi
) -> None:
    """🔴 SINIR 2: `total + 0.01` REDDEDİLİR — tolerans YOKTUR.

    Bir epsilon toleransı ya da `float` karşılaştırması eklenseydi bu iddia
    kırmızıya dönerdi.
    """
    invoice = await fatura_fabrikasi(total="1000.00")
    account = await hesap_fabrikasi()
    resp = await client.post(
        _fatura_yolu(invoice), headers=muhasebe_headers, json=_govde(account, "1000.01")
    )
    assert resp.status_code == 422, resp.text


async def test_K6_IKINCI_odeme_kalani_asarsa_422(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi, fatura_odemesi
) -> None:
    """Eşik MEVCUT toplamı içerir: yalnız `yeni.amount > total` denetlenseydi bu
    istek geçer ve fatura 1000,01₺ tahsil edilmiş görünürdü."""
    invoice = await fatura_fabrikasi(total="1000.00")
    account = await hesap_fabrikasi()
    await fatura_odemesi(invoice, account, "999.99")
    resp = await client.post(
        _fatura_yolu(invoice), headers=muhasebe_headers, json=_govde(account, "0.02")
    )
    assert resp.status_code == 422, resp.text


async def test_K5_TAM_tahsilat_giden_faturayi_collected_damgalar(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi, seeded_db, odeme_eslemesi
) -> None:
    invoice = await fatura_fabrikasi(
        direction=InvoiceDirection.outgoing, status=InvoiceStatus.sent, total="1000.00"
    )
    account = await hesap_fabrikasi()
    resp = await client.post(
        _fatura_yolu(invoice), headers=muhasebe_headers, json=_govde(account, "1000.00")
    )
    assert resp.status_code == 201, resp.text
    assert await _durum(seeded_db, invoice) is InvoiceStatus.collected


async def test_K5_KISMI_tahsilat_durumu_DEGISTIRMEZ(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi, seeded_db, odeme_eslemesi
) -> None:
    invoice = await fatura_fabrikasi(status=InvoiceStatus.sent, total="1000.00")
    account = await hesap_fabrikasi()
    resp = await client.post(
        _fatura_yolu(invoice), headers=muhasebe_headers, json=_govde(account, "999.99")
    )
    assert resp.status_code == 201, resp.text
    assert await _durum(seeded_db, invoice) is InvoiceStatus.sent


async def test_K5_GELEN_faturada_durum_DEGISMEZ(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi, seeded_db, odeme_eslemesi
) -> None:
    """Gelen faturanın ödenmesi Hazine kapsamında durum damgalamaz: `collected`
    GİDEN tarafın terminalidir ve gelen makinede karşılığı YOKTUR."""
    invoice = await fatura_fabrikasi(
        direction=InvoiceDirection.incoming, status=InvoiceStatus.approved, total="1000.00"
    )
    account = await hesap_fabrikasi()
    resp = await client.post(
        _fatura_yolu(invoice), headers=muhasebe_headers, json=_govde(account, "1000.00")
    )
    assert resp.status_code == 201, resp.text
    assert await _durum(seeded_db, invoice) is InvoiceStatus.approved


async def test_K5_DRAFT_giden_faturada_durum_DEGISMEZ(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi, seeded_db, odeme_eslemesi
) -> None:
    """🔴 MATRİS DIŞI GEÇİŞ UYDURULMAZ: `(draft, mark-collected)` çifti
    `OUTGOING_TRANSITIONS`ta YOKTUR, dolayısıyla tam ödense bile `draft` kalır."""
    invoice = await fatura_fabrikasi(status=InvoiceStatus.draft, total="1000.00")
    account = await hesap_fabrikasi()
    resp = await client.post(
        _fatura_yolu(invoice), headers=muhasebe_headers, json=_govde(account, "1000.00")
    )
    assert resp.status_code == 201, resp.text
    assert await _durum(seeded_db, invoice) is InvoiceStatus.draft


async def test_odeme_ekleme_gelen_faturada_hesaptan_CIKIS(
    client, muhasebe_headers, admin_headers, fatura_fabrikasi, hesap_fabrikasi, odeme_eslemesi
) -> None:
    """K4: yön ödemenin kendi kolonundan değil FATURANIN yönünden gelir."""
    invoice = await fatura_fabrikasi(
        direction=InvoiceDirection.incoming, status=InvoiceStatus.approved, total="1000.00"
    )
    account = await hesap_fabrikasi(opening_balance="1000.00")
    resp = await client.post(
        _fatura_yolu(invoice), headers=muhasebe_headers, json=_govde(account, "400.00")
    )
    assert resp.status_code == 201, resp.text
    kart = await client.get(f"/bank-accounts/{account.id}", headers=admin_headers)
    assert Decimal(kart.json()["balance"]) == Decimal("600.00")


async def test_odeme_ekleme_olmayan_hesap_404(client, muhasebe_headers, fatura_fabrikasi) -> None:
    """Gövde içi varlık referansı = 404 (ST kanonu)."""
    invoice = await fatura_fabrikasi()
    govde = {
        "bank_account_id": str(uuid.uuid4()),
        "method": "transfer",
        "amount": "1.00",
        "paid_on": "2026-08-14",
    }
    resp = await client.post(_fatura_yolu(invoice), headers=muhasebe_headers, json=govde)
    assert resp.status_code == 404, resp.text


async def test_odeme_ekleme_PASIF_hesap_422(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi
) -> None:
    """Kullanımdan kaldırma yolu `is_active=false`tur; oraya yeni para
    yazılabilseydi bayrak tamamen süs olurdu."""
    invoice = await fatura_fabrikasi()
    account = await hesap_fabrikasi(is_active=False)
    resp = await client.post(
        _fatura_yolu(invoice), headers=muhasebe_headers, json=_govde(account, "1.00")
    )
    assert resp.status_code == 422, resp.text


async def test_odeme_ekleme_olmayan_fatura_404(client, muhasebe_headers, hesap_fabrikasi) -> None:
    account = await hesap_fabrikasi()
    resp = await client.post(
        f"/invoices/{uuid.uuid4()}/payments", headers=muhasebe_headers, json=_govde(account, "1.00")
    )
    assert resp.status_code == 404, resp.text


async def test_odeme_ekleme_gorunmeyen_fatura_404(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi, project_factory
) -> None:
    proje = await project_factory(code="HZ-P02", name="Görünmeyen Proje 2")
    invoice = await fatura_fabrikasi(project=proje)
    account = await hesap_fabrikasi()
    resp = await client.post(
        _fatura_yolu(invoice), headers=muhasebe_headers, json=_govde(account, "1.00")
    )
    assert resp.status_code == 404, resp.text


async def test_odeme_ekleme_SIFIR_tutar_422(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi
) -> None:
    """`ck_payments_amount_positive` ŞEMADA önce yakalanır: sıfır hiçbir şey ifade
    etmez, negatif ise gizli bir İADE olurdu."""
    invoice = await fatura_fabrikasi()
    account = await hesap_fabrikasi()
    resp = await client.post(
        _fatura_yolu(invoice), headers=muhasebe_headers, json=_govde(account, "0.00")
    )
    assert resp.status_code == 422, resp.text


async def test_odeme_ekleme_BILINMEYEN_alan_422(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi
) -> None:
    """`extra="forbid"`: sessiz yok sayma YOK — istemci gönderdiği alanın
    yazıldığını sanmamalıdır."""
    invoice = await fatura_fabrikasi()
    account = await hesap_fabrikasi()
    resp = await client.post(
        _fatura_yolu(invoice),
        headers=muhasebe_headers,
        json=_govde(account, "1.00", cheque_id=str(uuid.uuid4())),
    )
    assert resp.status_code == 422, resp.text


async def test_odeme_ekleme_denetim_satiri_yazilir(
    client,
    muhasebe_headers,
    fatura_fabrikasi,
    hesap_fabrikasi,
    seeded_db,
    kullanici_kimligi,
    odeme_eslemesi,
) -> None:
    invoice = await fatura_fabrikasi()
    account = await hesap_fabrikasi(bank_name="Yapı Kredi")
    resp = await client.post(
        _fatura_yolu(invoice), headers=muhasebe_headers, json=_govde(account, "1.00")
    )
    assert resp.status_code == 201, resp.text
    kayitlar = (
        (await seeded_db.execute(select(AuditLog).where(AuditLog.action == AuditAction.create)))
        .scalars()
        .all()
    )
    assert any(invoice.invoice_no in (k.detail or "") for k in kayitlar), [
        k.detail for k in kayitlar
    ]
    aktor = await kullanici_kimligi("muhasebe@hazine.co")
    assert all(k.actor_user_id == aktor for k in kayitlar)


# --------------------------------------------------------------------------- #
# Uç 8 — DELETE /payments/{id}
# --------------------------------------------------------------------------- #


async def test_silme_admin_204_ve_satir_gider(
    client, admin_headers, fatura_fabrikasi, hesap_fabrikasi, fatura_odemesi
) -> None:
    invoice = await fatura_fabrikasi(total="1000.00")
    account = await hesap_fabrikasi()
    payment = await fatura_odemesi(invoice, account, "400.00")

    resp = await sil_aile(client, admin_headers, "payment", payment.id)
    assert resp.status_code == 204, resp.text

    liste = await client.get(_fatura_yolu(invoice), headers=admin_headers)
    assert liste.json()["total"] == 0
    assert Decimal(liste.json()["remaining"]) == Decimal("1000.00")


async def test_silme_durumu_YENIDEN_TURETIR_collected_SENT_e_duser(
    client, admin_headers, fatura_fabrikasi, hesap_fabrikasi, fatura_odemesi, seeded_db
) -> None:
    """🔴 K5'in geri yönü: tam ödenmiş fatura `collected`tır; ödeme silinince Σ
    total'ın altına düşer ve damga `sent`e GERİ ALINIR.

    Damga tek yönlü olsaydı fatura hiç tahsilatı olmadan `collected` kalır ve
    hiçbir ekran bunu ele vermezdi.
    """
    invoice = await fatura_fabrikasi(status=InvoiceStatus.collected, total="1000.00")
    account = await hesap_fabrikasi()
    payment = await fatura_odemesi(invoice, account, "1000.00")

    resp = await sil_aile(client, admin_headers, "payment", payment.id)
    assert resp.status_code == 204, resp.text
    assert await _durum(seeded_db, invoice) is InvoiceStatus.sent


async def test_silme_TAM_odeme_korunuyorsa_collected_KALIR(
    client, admin_headers, fatura_fabrikasi, hesap_fabrikasi, fatura_odemesi, seeded_db
) -> None:
    """Geri düşüş KOŞULLUDUR: kalan ödemeler hâlâ toplamı karşılıyorsa damga
    düşmez. Koşulsuz `sent` yazan bir uygulama burada kırmızı olur.

    Kurulum fabrikadan gelir, uçtan DEĞİL: K6 zaten aşırı tahsilatı 422 ile
    keser. Yine de bu veri hâli GERÇEKTİR — elle düzeltilmiş ya da politika
    değişmeden önce girilmiş satırlar böyle görünür — ve türetim onu doğru
    okumak zorundadır.
    """
    invoice = await fatura_fabrikasi(status=InvoiceStatus.collected, total="1000.00")
    account = await hesap_fabrikasi()
    await fatura_odemesi(invoice, account, "1000.00")
    silinecek = await fatura_odemesi(invoice, account, "500.00")

    resp = await sil_aile(client, admin_headers, "payment", silinecek.id)
    assert resp.status_code == 204, resp.text
    assert await _durum(seeded_db, invoice) is InvoiceStatus.collected


async def test_silme_DRAFT_faturanin_durumunu_SENT_e_ITMEZ(
    client, admin_headers, fatura_fabrikasi, hesap_fabrikasi, fatura_odemesi, seeded_db
) -> None:
    """🔴 Geri düşüş yalnız `collected`ten olur: silme koşulsuz `sent` yazsaydı
    bir TASLAK fatura ödeme silinerek gönderilmiş sayılırdı."""
    invoice = await fatura_fabrikasi(status=InvoiceStatus.draft, total="1000.00")
    account = await hesap_fabrikasi()
    payment = await fatura_odemesi(invoice, account, "500.00")

    resp = await sil_aile(client, admin_headers, "payment", payment.id)
    assert resp.status_code == 204, resp.text
    assert await _durum(seeded_db, invoice) is InvoiceStatus.draft


async def test_silme_GELEN_faturada_durum_DEGISMEZ(
    client, admin_headers, fatura_fabrikasi, hesap_fabrikasi, fatura_odemesi, seeded_db
) -> None:
    invoice = await fatura_fabrikasi(
        direction=InvoiceDirection.incoming, status=InvoiceStatus.approved, total="1000.00"
    )
    account = await hesap_fabrikasi()
    payment = await fatura_odemesi(invoice, account, "1000.00")

    resp = await sil_aile(client, admin_headers, "payment", payment.id)
    assert resp.status_code == 204, resp.text
    assert await _durum(seeded_db, invoice) is InvoiceStatus.approved


async def test_silme_FULL_rolu_403_ama_ayni_kullanici_POST_gecer(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi, fatura_odemesi, odeme_eslemesi
) -> None:
    """ÖN KOŞULLU test: 403 seviyeden gelir, faturaya erişememesinden değil."""
    invoice = await fatura_fabrikasi(total="1000.00")
    account = await hesap_fabrikasi()
    payment = await fatura_odemesi(invoice, account, "100.00")

    onkosul = await client.post(
        _fatura_yolu(invoice), headers=muhasebe_headers, json=_govde(account, "1.00")
    )
    assert onkosul.status_code == 201, onkosul.text

    resp = await client.delete(f"{_ODEME_YOLU}/{payment.id}", headers=muhasebe_headers)
    assert resp.status_code == 403, resp.text


async def test_silme_olmayan_odeme_404(client, admin_headers) -> None:
    resp = await sil_aile(client, admin_headers, "payment", uuid.uuid4())
    assert resp.status_code == 404, resp.text


async def test_silme_denetim_satiri_yazilir(
    client, admin_headers, fatura_fabrikasi, hesap_fabrikasi, fatura_odemesi, seeded_db
) -> None:
    invoice = await fatura_fabrikasi()
    account = await hesap_fabrikasi()
    payment = await fatura_odemesi(invoice, account, "1.00")

    resp = await sil_aile(client, admin_headers, "payment", payment.id)
    assert resp.status_code == 204, resp.text
    kayitlar = (
        (await seeded_db.execute(select(AuditLog).where(AuditLog.action == AuditAction.delete)))
        .scalars()
        .all()
    )
    assert any(invoice.invoice_no in (k.detail or "") for k in kayitlar), [
        k.detail for k in kayitlar
    ]

"""FIN-PAY — ödeme ↔ çek/senet bağının YAZMA yüzeyi.

`payments.financial_instrument_id` FK'si FIN-1'de açıldı ama yazma yüzeyi
yoktu: bir ödeme hiçbir yoldan bir çeke/senede bağlanamıyordu. Bu dilim var
olan İKİ uca (`POST`/`GET /invoices/{id}/payments`) alan ekler; **yeni yol
açılmaz**.

## Bu dosyanın kilitlediği kararlar

1. **K1 — alan İSTEĞE BAĞLIDIR.** `method='cheque'` iken bile zorunlu değildir:
   etiket (`method`) ile varlık (`financial_instrument_id`) AYRI iki olgudur
   (`models.py:226`). Gönderilmeyen alanla ödeme 201 döner ve kolon `NULL`dur.
2. **K2 — var olmayan YA DA GÖRÜNMEYEN enstrüman → 404**, sessiz `None` DEĞİL.
   Gövdede kimlik gönderilip `NULL` yazılsaydı kullanıcı bağladığını sanırdı;
   ham `IntegrityError`a bırakılsaydı 500 olurdu.
3. 🔴 **K3 — YÖN UYUMU.** Uyumlu çiftler `balance.inflow_condition()`tan ÖLÇÜLDÜ:
   *giden* fatura bizim kestiğimizdir → tahsilat → elimize *alınan* (`received`)
   çek girer; *gelen* fatura bize kesilmiştir → ödeme → *verilen* (`issued`) çek
   çıkar. Ters çift **422**dir.
4. 🔴 **K4 — ÇİFT SAYIM YOK** (ODM-1 D2 ile YENİDEN YAZILDI). Bağ bir para
   türevine girdi EKLEMEZ; ODM-1'den itibaren bağın etkisi TERSİDİR: portföyde
   duran bir çeke bağlı ödeme nakit türevlerini **HİÇ** oynatmaz (para henüz
   bankada değildir, `balance.cash_realized_condition`). Aynı para artık tam
   olarak TEK yüzeyde görünür — çek portföyünde. Çek `collected` olduğunda
   nakde geçer ve o an bağsız bir ödemeyle BİREBİR aynı etkiyi yapar; portföy
   kartlarından da o an düşer. Eski hâli ("bağlı ödeme bağsızla AYNI oynatır")
   parayı İKİ yüzeyde birden sayıyordu ve ODM-1 onu düzeltir. Nakdin tanımının
   tam bekçi kümesi: `test_odm1_cash_definition.py`.
5. **K7 — `PaymentResponse` yalnız SAKLANAN kolonu döndürür**; çek no/vade gibi
   türev alan eklenmez (tek kaynak kuralı).
6. **K8 — denetim günlüğü DEĞİŞMEZ**: satır sayısı da metin de bağdan etkilenmez.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.modules.audit import messages
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.invoicing.models import InvoiceDirection, InvoiceStatus
from app.modules.treasury.instruments import guards as instrument_guards
from app.modules.treasury.models import (
    FinancialInstrumentDirection,
    FinancialInstrumentStatus,
    Payment,
)
from app.modules.treasury.payments_service import (
    PAYMENT_INSTRUMENT_DIRECTION_MISMATCH,
    PAYMENT_INSTRUMENT_NOT_PORTFOLIO,
)


def _yol(invoice) -> str:  # noqa: ANN001
    return f"/invoices/{invoice.id}/payments"


def _govde(account, amount: str, **fazlasi) -> dict:  # noqa: ANN001
    return {
        "bank_account_id": str(account.id),
        "method": "cheque",
        "amount": amount,
        "paid_on": "2026-08-14",
        **fazlasi,
    }


async def _kolon(seeded_db, payment_id: str):  # noqa: ANN001
    """Kolonu DB'den TAZE okur: uçtan dönen gövdeye bakmak alanın gerçekten
    YAZILDIĞINI değil yalnızca hesaplandığını kanıtlardı."""
    satir = (
        await seeded_db.execute(select(Payment).where(Payment.id == uuid.UUID(payment_id)))
    ).scalar_one()
    await seeded_db.refresh(satir)
    return satir.financial_instrument_id


# --------------------------------------------------------------------------- #
# K1 — alan İSTEĞE BAĞLIDIR (geriye uyum)
# --------------------------------------------------------------------------- #


async def test_alan_GONDERILMEDEN_odeme_201_ve_kolon_NULL(
    client, muhasebe_headers, seeded_db, fatura_fabrikasi, hesap_fabrikasi, odeme_eslemesi
) -> None:
    """Mevcut istemciler alanı hiç bilmez; zorunlu kılınsaydı hepsi 422 alırdı."""
    invoice = await fatura_fabrikasi(total="1000.00")
    account = await hesap_fabrikasi()

    resp = await client.post(
        _yol(invoice), json=_govde(account, "100.00"), headers=muhasebe_headers
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["financial_instrument_id"] is None
    assert await _kolon(seeded_db, resp.json()["id"]) is None


async def test_method_cheque_olsa_bile_bag_ZORUNLU_DEGIL(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi, odeme_eslemesi
) -> None:
    """🔴 K1 — etiket varlığı İMA ETMEZ (`models.py:226`)."""
    invoice = await fatura_fabrikasi(total="1000.00")
    account = await hesap_fabrikasi()

    resp = await client.post(
        _yol(invoice), json=_govde(account, "100.00", method="cheque"), headers=muhasebe_headers
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["financial_instrument_id"] is None


async def test_acikca_null_gonderilen_bag_da_201(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi, odeme_eslemesi
) -> None:
    """`null` "bağ yok" demektir; 404'e düşseydi alan fiilen zorunlu olurdu."""
    invoice = await fatura_fabrikasi(total="1000.00")
    account = await hesap_fabrikasi()

    resp = await client.post(
        _yol(invoice),
        json=_govde(account, "100.00", financial_instrument_id=None),
        headers=muhasebe_headers,
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["financial_instrument_id"] is None


# --------------------------------------------------------------------------- #
# Uyumlu bağ — İKİ YÖN de yazılır
# --------------------------------------------------------------------------- #


async def test_GIDEN_faturaya_ALINAN_cek_baglanir_201(
    client,
    muhasebe_headers,
    seeded_db,
    fatura_fabrikasi,
    hesap_fabrikasi,
    cek_fabrikasi,
    odeme_eslemesi,
) -> None:
    """🔴 K3 uyumlu çifti — yön `balance.inflow_condition()`tan ÖLÇÜLDÜ.

    Giden fatura bizim kestiğimizdir → tahsilat → hesaba GİRİŞ → elimize
    **alınan** çek girer.
    """
    invoice = await fatura_fabrikasi(direction=InvoiceDirection.outgoing, total="1000.00")
    account = await hesap_fabrikasi()
    cek = await cek_fabrikasi(direction=FinancialInstrumentDirection.received)

    resp = await client.post(
        _yol(invoice),
        json=_govde(account, "100.00", financial_instrument_id=str(cek.id)),
        headers=muhasebe_headers,
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["financial_instrument_id"] == str(cek.id)
    assert await _kolon(seeded_db, resp.json()["id"]) == cek.id


async def test_GELEN_faturaya_VERILEN_senet_baglanir_201(
    client,
    muhasebe_headers,
    seeded_db,
    fatura_fabrikasi,
    hesap_fabrikasi,
    cek_fabrikasi,
    odeme_eslemesi,
) -> None:
    """K3'ün öbür yarısı: gelen fatura bize kesilmiştir → ödeme → **verilen**."""
    invoice = await fatura_fabrikasi(direction=InvoiceDirection.incoming, total="1000.00")
    account = await hesap_fabrikasi()
    senet = await cek_fabrikasi(direction=FinancialInstrumentDirection.issued)

    resp = await client.post(
        _yol(invoice),
        json=_govde(account, "100.00", financial_instrument_id=str(senet.id)),
        headers=muhasebe_headers,
    )

    assert resp.status_code == 201, resp.text
    assert await _kolon(seeded_db, resp.json()["id"]) == senet.id


@pytest.mark.parametrize(
    "terminal",
    [
        FinancialInstrumentStatus.collected,
        FinancialInstrumentStatus.returned,
        FinancialInstrumentStatus.cancelled,
    ],
)
async def test_PORTFOY_DISI_ceke_odeme_BAGLANAMAZ_422(
    client,
    muhasebe_headers,
    fatura_fabrikasi,
    hesap_fabrikasi,
    cek_fabrikasi,
    odeme_eslemesi,
    terminal,
) -> None:
    """🔴 ODM-1 D4 — FIN-PAY'in *"durum denetimi YOKTUR ve uydurulmaz"* kararı
    TERSİNE ÇEVRİLDİ ve gerekçesi ODM-1'de DOĞDU (o gün yoktu):

    bağlı bir ödemenin nakit bacağı `101`/`103`e yazılır; o ara hesabı
    boşaltan TEK olay `instruments.service.change_status`ın `collected`/`paid`
    geçişidir; `TERMINAL_STATUSES`ten **ÇIKIŞ YOKTUR**. Yani terminal bir
    evraka bağlanan ödeme `101`i sonsuza dek borçlu bırakır — kalıcı bir
    "yolda" para. Doğru yol bağsız ödemedir (para zaten hesaba indi).

    ÜÇ terminal durumun ÜÇÜ de denenir: yalnız `collected` yazılsaydı
    `returned` üzerinden AYNI kalıntı sessizce açılabilirdi.
    """
    invoice = await fatura_fabrikasi(direction=InvoiceDirection.outgoing, total="1000.00")
    account = await hesap_fabrikasi()
    cek = await cek_fabrikasi(
        direction=FinancialInstrumentDirection.received,
        status=terminal,
    )

    resp = await client.post(
        _yol(invoice),
        json=_govde(account, "100.00", financial_instrument_id=str(cek.id)),
        headers=muhasebe_headers,
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == PAYMENT_INSTRUMENT_NOT_PORTFOLIO


async def test_PORTFOYDEKI_ceke_odeme_BAGLANIR_201_POZITIF_KONTROL(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi, cek_fabrikasi, odeme_eslemesi
) -> None:
    """🔴 Yukarıdaki bekçinin POZİTİF KONTROLÜ. Onsuz, bağı KOŞULSUZ reddeden
    bozuk bir kod da yeşil geçerdi (K-IKIZ deseni)."""
    invoice = await fatura_fabrikasi(direction=InvoiceDirection.outgoing, total="1000.00")
    account = await hesap_fabrikasi()
    cek = await cek_fabrikasi(direction=FinancialInstrumentDirection.received)

    resp = await client.post(
        _yol(invoice),
        json=_govde(account, "100.00", financial_instrument_id=str(cek.id)),
        headers=muhasebe_headers,
    )

    assert resp.status_code == 201, resp.text


# --------------------------------------------------------------------------- #
# K2 — var olmayan / görünmeyen enstrüman → 404
# --------------------------------------------------------------------------- #


async def test_VAR_OLMAYAN_enstruman_404_sessiz_NULL_DEGIL(
    client, muhasebe_headers, seeded_db, fatura_fabrikasi, hesap_fabrikasi
) -> None:
    """🔴 K2 — 500 DEĞİL, sessiz `None` DEĞİL.

    Sessiz `None` yazılsaydı kullanıcı bağladığını sanırdı; FK ihlaline
    bırakılsaydı ham `IntegrityError` 500 olarak sızardı.
    """
    invoice = await fatura_fabrikasi(total="1000.00")
    account = await hesap_fabrikasi()

    resp = await client.post(
        _yol(invoice),
        json=_govde(account, "100.00", financial_instrument_id=str(uuid.uuid4())),
        headers=muhasebe_headers,
    )

    assert resp.status_code == 404, resp.text
    # Metin çek/senet uçlarınınkiyle AYNI sabittendir: ikinci bir cümle
    # yazılsaydı aynı olgu iki uçtan iki farklı şekilde bildirilirdi.
    assert resp.json()["detail"] == instrument_guards.INSTRUMENT_MISSING
    # Hiçbir satır yazılmamalı: 404'ten sonra kısmi bir ödeme kalsaydı bağsız
    # bir para kaydı doğar ve kullanıcı onu hiç görmezdi.
    assert (await seeded_db.execute(select(Payment))).scalars().all() == []


async def test_GORUNMEYEN_projenin_ceki_404(
    client,
    kapsamli_muhasebe_headers,
    fatura_fabrikasi,
    hesap_fabrikasi,
    cek_fabrikasi,
    gorunmeyen_proje,
) -> None:
    """Görünmeyen kayıt var olmayanla AYNI 404'ü alır (repo kanonu).

    Sessiz `None` ya da 403 verilseydi, kullanıcı elindeki kimliğin GERÇEK bir
    çeke ait olduğunu öğrenirdi (yan kanal).
    """
    invoice = await fatura_fabrikasi(direction=InvoiceDirection.outgoing, total="1000.00")
    account = await hesap_fabrikasi()
    cek = await cek_fabrikasi(
        direction=FinancialInstrumentDirection.received, project=gorunmeyen_proje
    )

    resp = await client.post(
        _yol(invoice),
        json=_govde(account, "100.00", financial_instrument_id=str(cek.id)),
        headers=kapsamli_muhasebe_headers,
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == instrument_guards.INSTRUMENT_MISSING


# --------------------------------------------------------------------------- #
# 🔴 K3 — TERS YÖN 422
# --------------------------------------------------------------------------- #


async def test_GIDEN_faturaya_VERILEN_cek_422(
    client, muhasebe_headers, seeded_db, fatura_fabrikasi, hesap_fabrikasi, cek_fabrikasi
) -> None:
    """🔴 K3 — giden fatura bir TAHSİLATtır; verilen (`issued`) bir çek oraya
    bağlanamaz, yoksa portföyün yönü ile paranın yönü çelişirdi."""
    invoice = await fatura_fabrikasi(direction=InvoiceDirection.outgoing, total="1000.00")
    account = await hesap_fabrikasi()
    cek = await cek_fabrikasi(direction=FinancialInstrumentDirection.issued)

    resp = await client.post(
        _yol(invoice),
        json=_govde(account, "100.00", financial_instrument_id=str(cek.id)),
        headers=muhasebe_headers,
    )

    assert resp.status_code == 422, resp.text
    # 🔴 Çıplak `422` YETMEZ: bilinmeyen alan/biçim ihlali de 422 verir. Alan
    # eklenmeden ÖNCE bu test `extra="forbid"` sayesinde YEŞİLDİ ve hiçbir şey
    # bekçilik etmiyordu — metin iddiası onu gerçek bir bekçiye çevirir.
    assert resp.json()["detail"] == PAYMENT_INSTRUMENT_DIRECTION_MISMATCH
    assert (await seeded_db.execute(select(Payment))).scalars().all() == []


async def test_GELEN_faturaya_ALINAN_cek_422(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi, cek_fabrikasi
) -> None:
    """K3'ün öbür yarısı — denetim TERS kurulsaydı bu test yeşil kalır, üstteki
    kırmızı olurdu; ikisi birlikte yönü KİLİTLER."""
    invoice = await fatura_fabrikasi(direction=InvoiceDirection.incoming, total="1000.00")
    account = await hesap_fabrikasi()
    cek = await cek_fabrikasi(direction=FinancialInstrumentDirection.received)

    resp = await client.post(
        _yol(invoice),
        json=_govde(account, "100.00", financial_instrument_id=str(cek.id)),
        headers=muhasebe_headers,
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == PAYMENT_INSTRUMENT_DIRECTION_MISMATCH


# --------------------------------------------------------------------------- #
# 🔴 K4 — ÇİFT SAYIM BEKÇİSİ
# --------------------------------------------------------------------------- #


async def _para_goruntusu(client, headers) -> dict:  # noqa: ANN001
    """Üç para yüzeyinin BİRLİKTE anlık görüntüsü.

    Tek yüzeye bakılsaydı, bağın öbür ikisine sızması görülmezdi.
    """
    hesaplar = await client.get("/bank-accounts", headers=headers)
    nakit = await client.get("/treasury/cash-flow?year=2026&month=8", headers=headers)
    ozet = await client.get("/financial-instruments/summary", headers=headers)
    assert hesaplar.status_code == 200, hesaplar.text
    assert nakit.status_code == 200, nakit.text
    assert ozet.status_code == 200, ozet.text
    return {
        "bakiyeler": {k["id"]: k["balance"] for k in hesaplar.json()["items"]},
        "nakit": nakit.json(),
        "ozet": ozet.json(),
    }


async def test_K4_PORTFOYDEKI_ceke_bagli_odeme_nakit_turevlerini_HIC_OYNATMAZ(
    seeded_db,  # noqa: ANN001
    client,  # noqa: ANN001
    admin_headers,  # noqa: ANN001
    fatura_fabrikasi,  # noqa: ANN001
    hesap_fabrikasi,  # noqa: ANN001
    cek_fabrikasi,  # noqa: ANN001
    odeme_eslemesi,  # noqa: ANN001
) -> None:
    """🔴 K4, ODM-1 D2 ile TERSİNE ÇEVRİLDİ — çift sayımın ASIL çözümü.

    ESKİ KARAR: "bağlı ödeme para türevlerini bağsız ödemeyle BİREBİR AYNI
    oynatır." O hâlde aynı para İKİ yüzeyde birden görünüyordu: banka
    bakiyesinde (henüz tahsil edilmemiş bir çek nakitmiş gibi) VE çek
    portföyünde. Kullanıcı elinde olmayan parayı harcanabilir sanıyordu.

    YENİ KARAR (ODM-1 D2): nakit süzgeci `balance.cash_realized_condition`tır.
    Portföydeki çeke bağlı ödeme nakit türevlerine **0** katar; para YALNIZ
    portföy yüzeyinde durur. Çek tahsil edilince (`collected`) aynı satır
    süzgeçten geçer ve bakiyeye O AN girer — ikinci bir ödeme kaydı GEREKMEZ,
    yani hiçbir aşamada çift sayım oluşmaz.

    Yöntem ÜÇ ANLIDIR ve üçü de gereklidir: bağsız ödeme (süzgecin fazla geniş
    olmadığını gösterir), bağlı+portföy (süzgecin VAR olduğunu gösterir),
    bağlı+collected (süzgecin parayı kalıcı olarak yutmadığını gösterir). İkisi
    yazılıp üçüncüsü atlansaydı, hep sayan ya da hiç saymayan bozuk kod yeşil
    geçerdi (K-IKIZ1).

    🔴 `_govde` ödemeleri `method='cheque'` ile gönderir; bağsız olanın yine de
    500 oynatması ODM-1 D1'in uçtan bekçisidir — tetikleyici BAĞDIR, `method`
    ETİKETİ DEĞİL.
    """
    account = await hesap_fabrikasi(opening_balance="0.00")
    cek = await cek_fabrikasi(direction=FinancialInstrumentDirection.received, amount="500.00")
    fatura_a = await fatura_fabrikasi(direction=InvoiceDirection.outgoing, total="1000.00")
    fatura_b = await fatura_fabrikasi(direction=InvoiceDirection.outgoing, total="1000.00")

    baslangic = await _para_goruntusu(client, admin_headers)

    bagsiz = await client.post(
        _yol(fatura_a),
        json=_govde(account, "500.00"),
        headers=admin_headers,
    )
    assert bagsiz.status_code == 201, bagsiz.text
    bagsiz_sonrasi = await _para_goruntusu(client, admin_headers)

    bagli = await client.post(
        _yol(fatura_b),
        json=_govde(account, "500.00", financial_instrument_id=str(cek.id)),
        headers=admin_headers,
    )
    assert bagli.status_code == 201, bagli.text
    assert bagli.json()["financial_instrument_id"] == str(cek.id)
    bagli_sonrasi = await _para_goruntusu(client, admin_headers)

    def _bakiye(goruntu: dict) -> Decimal:
        return Decimal(goruntu["bakiyeler"][str(account.id)])

    def _giris(goruntu: dict) -> Decimal:
        return Decimal(goruntu["nakit"]["inflow_total"])

    # 1) BAĞSIZ ödeme — `method='cheque'` olmasına RAĞMEN tam 500 oynatır (D1).
    assert _bakiye(bagsiz_sonrasi) - _bakiye(baslangic) == Decimal("500.00")
    assert _giris(bagsiz_sonrasi) - _giris(baslangic) == Decimal("500.00")

    # 2) BAĞLI + PORTFÖY — nakit türevleri KILI KIPIRDAMAZ.
    assert _bakiye(bagli_sonrasi) == _bakiye(bagsiz_sonrasi)
    assert _giris(bagli_sonrasi) == _giris(bagsiz_sonrasi)
    assert Decimal(bagli_sonrasi["nakit"]["outflow_total"]) == Decimal(
        baslangic["nakit"]["outflow_total"]
    )

    # 3) ÇEK/SENET ÖZETİ — bağlama anında BİREBİR AYNI. Çek zaten portföydeydi;
    #    bir ödemeye bağlanması onu portföyden ÇIKARMAZ (durum geçişi AYRI uç).
    assert bagsiz_sonrasi["ozet"] == baslangic["ozet"]
    assert bagli_sonrasi["ozet"] == baslangic["ozet"]

    # 4) TAHSİL — süzgeç parayı YUTMAZ; aynı satır bağsız ödemeyle AYNI etkiyi
    #    yapar. Durum kolona doğrudan yazılır: bu dosya bağın PARA etkisini
    #    ölçer, geçiş kapısını değil (`test_fin1_transitions.py`).
    cek.status = FinancialInstrumentStatus.collected
    await seeded_db.flush()
    tahsil_sonrasi = await _para_goruntusu(client, admin_headers)

    assert _bakiye(tahsil_sonrasi) - _bakiye(bagli_sonrasi) == Decimal("500.00")
    assert _giris(tahsil_sonrasi) - _giris(bagli_sonrasi) == Decimal("500.00")


# --------------------------------------------------------------------------- #
# Uç 6 — GET listesi alanı döndürür
# --------------------------------------------------------------------------- #


async def test_liste_ucu_bagi_dondurur_bagsizi_NULL_basar(
    client,
    muhasebe_headers,
    pm_headers,
    fatura_fabrikasi,
    hesap_fabrikasi,
    cek_fabrikasi,
    odeme_eslemesi,
) -> None:
    """Yalnız POST'a eklenip GET'e eklenmeseydi kullanıcı yazdığı bağı hiçbir
    ekranda GÖREMEZDİ (yazılabilen ama okunamayan bir alan sınıfı)."""
    invoice = await fatura_fabrikasi(direction=InvoiceDirection.outgoing, total="1000.00")
    account = await hesap_fabrikasi()
    cek = await cek_fabrikasi(direction=FinancialInstrumentDirection.received)

    bagli = await client.post(
        _yol(invoice),
        json=_govde(account, "100.00", financial_instrument_id=str(cek.id)),
        headers=muhasebe_headers,
    )
    assert bagli.status_code == 201, bagli.text
    bagsiz = await client.post(
        _yol(invoice), json=_govde(account, "200.00"), headers=muhasebe_headers
    )
    assert bagsiz.status_code == 201, bagsiz.text

    resp = await client.get(_yol(invoice), headers=pm_headers)

    assert resp.status_code == 200, resp.text
    baglar = {satir["id"]: satir["financial_instrument_id"] for satir in resp.json()["items"]}
    assert baglar[bagli.json()["id"]] == str(cek.id)
    assert baglar[bagsiz.json()["id"]] is None


async def test_K7_yanit_TUREV_alan_TASIMAZ(
    client, muhasebe_headers, fatura_fabrikasi, hesap_fabrikasi, cek_fabrikasi, odeme_eslemesi
) -> None:
    """🔴 K7 — yalnız SAKLANAN kolon döner. Çek no/vade/kesideci eklenseydi tek
    kaynak bozulur, çek düzeltilince ödeme yanıtı BAYAT kalırdı."""
    invoice = await fatura_fabrikasi(direction=InvoiceDirection.outgoing, total="1000.00")
    account = await hesap_fabrikasi()
    cek = await cek_fabrikasi(direction=FinancialInstrumentDirection.received)

    resp = await client.post(
        _yol(invoice),
        json=_govde(account, "100.00", financial_instrument_id=str(cek.id)),
        headers=muhasebe_headers,
    )

    assert resp.status_code == 201, resp.text
    assert set(resp.json()) == {
        "id",
        "invoice_id",
        "bank_account_id",
        "financial_instrument_id",
        "method",
        "amount",
        "paid_on",
        "note",
        "created_by_id",
        "created_at",
        "updated_at",
    }


# --------------------------------------------------------------------------- #
# Mevcut davranış DEĞİŞMEDİ
# --------------------------------------------------------------------------- #


async def test_GORUNMEYEN_projenin_faturasinda_IDOR_YOK(
    client,
    kapsamli_muhasebe_headers,
    fatura_fabrikasi,
    hesap_fabrikasi,
    cek_fabrikasi,
    gorunmeyen_proje,
) -> None:
    """Fatura kapsamı bağdan ÖNCE denetlenir: geçerli bir çek kimliği vermek
    görünmeyen bir faturaya yazma yolu AÇMAZ."""
    invoice = await fatura_fabrikasi(
        direction=InvoiceDirection.outgoing, total="1000.00", project=gorunmeyen_proje
    )
    account = await hesap_fabrikasi()
    cek = await cek_fabrikasi(direction=FinancialInstrumentDirection.received)

    resp = await client.post(
        _yol(invoice),
        json=_govde(account, "100.00", financial_instrument_id=str(cek.id)),
        headers=kapsamli_muhasebe_headers,
    )

    assert resp.status_code == 404, resp.text


async def test_K8_denetim_gunlugu_satiri_da_metni_de_DEGISMEDI(
    client,
    muhasebe_headers,
    seeded_db,
    fatura_fabrikasi,
    hesap_fabrikasi,
    cek_fabrikasi,
    odeme_eslemesi,
) -> None:
    """🔴 K8 — bağ varlığı `messages.payment_created(...)` metnine SIZMAZ.

    Sızsaydı mevcut testlerin metin iddiaları kırılırdı; bu dilimin iddiası
    "mevcut davranış DEĞİŞMEZ"tir.
    """
    invoice = await fatura_fabrikasi(direction=InvoiceDirection.outgoing, total="1000.00")
    account = await hesap_fabrikasi()
    cek = await cek_fabrikasi(direction=FinancialInstrumentDirection.received)

    bagsiz = await client.post(
        _yol(invoice), json=_govde(account, "100.00"), headers=muhasebe_headers
    )
    assert bagsiz.status_code == 201, bagsiz.text
    bagli = await client.post(
        _yol(invoice),
        json=_govde(account, "100.00", financial_instrument_id=str(cek.id)),
        headers=muhasebe_headers,
    )
    assert bagli.status_code == 201, bagli.text

    kayitlar = (
        (
            await seeded_db.execute(
                select(AuditLog)
                .where(AuditLog.action == AuditAction.create)
                .order_by(AuditLog.occurred_at)
            )
        )
        .scalars()
        .all()
    )
    olusturma = [k for k in kayitlar if k.detail.startswith("Ödeme kaydı eklendi")]
    # Bağ, FAZLADAN bir satır da üretmez: portföy geçişi AYRI bir uçtur.
    assert len(olusturma) == 2
    # Metin `messages.payment_created(...)`ın BUGÜNKÜ üç argümanıyla kurulur.
    beklenen = messages.payment_created(invoice.invoice_no, account.bank_name, account.display_name)
    # 🔴 İki satır BİREBİR AYNIDIR: bağ metne sızsaydı ayrışırlardı.
    assert olusturma[0].detail == beklenen
    assert olusturma[1].detail == beklenen
    assert str(cek.id) not in olusturma[1].detail


async def test_bagli_odeme_silinince_durum_TURETIMI_bozulmadi(
    client,
    admin_headers,
    seeded_db,
    fatura_fabrikasi,
    hesap_fabrikasi,
    cek_fabrikasi,
    odeme_eslemesi,
) -> None:
    """K5 türetimi bağdan ETKİLENMEZ: tam ödeme `collected` damgalar, silme
    `sent`e geri düşürür."""
    invoice = await fatura_fabrikasi(
        direction=InvoiceDirection.outgoing, status=InvoiceStatus.sent, total="1000.00"
    )
    account = await hesap_fabrikasi()
    cek = await cek_fabrikasi(direction=FinancialInstrumentDirection.received)

    olustur = await client.post(
        _yol(invoice),
        json=_govde(account, "1000.00", financial_instrument_id=str(cek.id)),
        headers=admin_headers,
    )
    assert olustur.status_code == 201, olustur.text
    await seeded_db.refresh(invoice)
    assert invoice.status is InvoiceStatus.collected

    sil = await client.delete(f"/payments/{olustur.json()['id']}", headers=admin_headers)

    assert sil.status_code == 204, sil.text
    await seeded_db.refresh(invoice)
    assert invoice.status is InvoiceStatus.sent


async def test_YOL_ve_OPERASYON_sayisi_SABIT_kalir() -> None:
    """🔴 Kapsam: TB6-T1 (`GET/PUT /payroll/tax-brackets`) İKİ YENİ YOL açtı.

    🔴 Kapsam bekçisi (tripwire): "yol" ile "operasyon" AYRI ölçülerdir
    (Kanon 3). Bu dosya kendi başına var olan İKİ uca (`POST`/`GET
    /invoices/{id}/payments`) alan ekler, yeni yol AÇMAZ — ama sayaç dosya
    bazlı değil, TÜM uygulamayı ölçer: başka bir dilim aynı anda gerçekten
    yeni uç eklerse bu test kırmızı olur ve kapsam genişlemesi GÖRÜNÜR hâle
    gelir. Sabitler bilinçli olarak ELLE yazılmıştır, tam da bu yüzden.

    Bugünkü değer FIN-PAY (bu dosya) + TB6'nın (`GET /payroll/tax-brackets`
    ve `PUT /payroll/tax-brackets/{year}/{income_kind}` — İKİ ayrı yol) +
    **OK-1A'nın BEŞ yeni yolu**nun toplamıdır.

    🔴 OK-1A (onay zinciri motoru) BİLEREK ve GEREKÇELİ olarak **+4 yol /
    +5 operasyon** ekler: `GET /approvals` · `GET /approvals/settings` ·
    `PUT /approvals/settings` · `GET /approvals/roles` ·
    `PUT /approvals/roles/{user_id}`.

    🔴 **KANON 3 BURADA ISIRDI:** OK-1A tasarım sözleşmesi bu dilimi "5 yol /
    5 operasyon" diye bağlamıştı; ÖLÇÜM onu çürüttü. `/approvals/settings`
    **TEK YOLDUR** ve üzerinde İKİ operasyon (GET + PUT) taşır. Yani
    226→**230** · 333→**338**. Sayılar ölçülerek yazıldı, sayılarak değil.

    🔴 **İK-2.2 (+1 yol / +1 operasyon):** `POST /leave-requests/{request_id}/withdraw`
    — kullanıcının KENDİ bekleyen izin talebini geri çekmesi. Yeni bir YOLDUR
    (mevcut `{request_id}` yolunun altına ayrı bir alt-yol açar) ve üzerinde TEK
    operasyon (POST) taşır; `approve`/`reject` emsalinin birebir kardeşi. Yani
    230→**231** · 338→**339**.

    🔴 **MK-4 (+1 yol / +1 operasyon):** `GET /equipment/{equipment_id}/detail`
    — Ekipman Detay ekranının TÜREV blokları (bakım penceresi + kümülatif
    ödenen). Yeni bir YOLDUR; `GET /equipment/{equipment_id}` gövdesi
    DEĞİŞMEDİ (türevler künyeye konsaydı LİSTE ucu da her çizilişte hareket
    tablosunu tarardı). `GET /equipment/rental-invoices`a eklenen
    `equipment_id` SÜZGECİ ise **ne yol ne operasyon** açar — Kanon 3'ün
    üçüncü ölçüsü: bir sorgu parametresi sözleşmeyi genişletir ama bu iki
    sayacın hiçbirini oynatmaz (sürüklenmeyi `tests/contract/` yakalar).
    Yani 231→**232** · 339→**340**.

    🔴 **PUAN-SAAT (+1 yol / +1 operasyon):** puantaj gün kodundan adam-saate
    geçti. `GET`+`PUT /sites/{site_id}/timesheet/week` **TEK YENİ YOLDUR** ve
    üzerinde İKİ operasyon taşır; buna karşılık aylık `PUT
    /sites/{site_id}/timesheet` **KALDIRILDI** (bir haftayı kaydetmenin ayın
    geri kalanını silmesi mümkün olan tek yol oydu). Net: +1 yol, +2−1 = +1
    operasyon. Yani 232→**233** · 340→**341**.

    ⚠️ Bu satır Kanon 3'ün dördüncü ölçüsüdür: bir dilim aynı turda hem uç
    EKLEYİP hem uç KALDIRDIĞINDA sayaçların ikisi de oynar ama farklı miktarda;
    "yol sayısı sabit kaldı" ya da "operasyon +2" beklentisinin ikisi de yanlış
    olurdu. Sayılar ölçülerek yazıldı.

    🔴 **STOK-BOLUM (+1 yol / +1 operasyon):** `GET /sections/{section_id}/stock`
    — bölümün malzeme kırılımı (`A1 › Malzeme` sekmesi). TEK yeni yol, TEK
    operasyon. Buna karşılık `GET /sites/{site_id}/stock`a eklenen `section_id`
    SÜZGECİ **ne yol ne operasyon** açar (MK-4'ün `equipment_id` süzgeciyle
    aynı ölçü) ve `stock_entry_lines`a eklenen iki alan da sayaçların hiçbirini
    oynatmaz — o sürüklenmeyi `tests/contract/openapi_baseline.json` yakalar ve
    orada **KIRICI** olarak ölçülmüştür (`StockEntryLineResponse.required`
    genişledi). Yani 233→**234** · 341→**342**.

    🔴 **AI-0b (+2 yol / +2 operasyon):** `GET /ai/tools` + `GET /ai/context`.
    Yani 234→**236** · 342→**344**. Aynı dilim `audit_action` enum'una
    `ai_turn` uyesini de ekler: bu sayaçların HİÇBİRİNİ oynatmaz ama
    sözleşme tabanını KIRICI biçimde değiştirir (`AuditAction.enum`) —
    K-DEVIR2'nin birebir vakası.

    🔴 **AI-1 (+1 yol / +1 operasyon):** `POST /ai/chat` — SSE akışı. Yani
    236→**237** · 344→**345**. İki ayrıntı ölçüldü ve kayda geçiyor:
      * Bu yol **tek operasyonlu** (`post`); `GET /ai/tools` ve `GET /ai/context`
        AI-0b'de zaten sayılmıştı, tekrar sayılmaz.
      * Devir **EKLEMELİDİR, KIRICI DEĞİL**: `openapi_baseline.json` farkı
        ölçüldü ve **72 ekleme / 0 silme**dir. Yani bu turda sayaçların +1/+1
        oynaması ile devrin kırıcı olmaması AYNI ANDA doğru — ama biri
        ötekinin kanıtı DEĞİLDİR (aşağıdaki K-DEVIR2 notu aynen geçerli;
        kırıcılık `git diff --numstat` ile AYRICA ölçüldü).
      * `AuditAction.ai_turn` bu dilimde **ÜRETİCİ** kazandı (`POST /ai/chat`);
        enum AI-0b'de zaten açılmıştı, yani sözleşme tabanı bu kez OYNAMAZ.

    🔴 **Bu iki sayaç sözleşmenin kırıcılığına KÖRDÜR** ve bu turda yeniden
    ölçüldü: yol/operasyon sayısının beklendiği gibi +1/+1 oynaması, devrin
    kırıcı OLMADIĞININ kanıtı DEĞİLDİR (K-DEVIR2). İki kapı birbirinin yerine
    geçmez.

    🔴 **EXPORT-XLSX (+6 yol / +6 operasyon):** alti ekranin Excel disa aktarimi.
    `GET /trial-balance/export.xlsx` · `GET /chart-of-accounts/export.xlsx` ·
    `GET /journal/export.xlsx` · `GET /personnel/export.xlsx` ·
    `GET /payroll/periods/export.xlsx` · `GET /equipment/work-summary/export.xlsx`.
    Her biri TEK yol, TEK operasyon (GET). Dilim ilk yazildiginda taban
    237/345'ti; AI-CHAT-2 araya girip tabani 239/348'e tasidi, dolayisiyla
    yururlukteki sayilar **239→245 · 348→354**tur. Sayilar rebase'ten SONRA
    YENIDEN OLCULDU (app.openapi()), eski degerden TURETILMEDI.

    ⚠️ Bu dilim Kanon 3'un BESINCI olcusunu gosterir: alti ucun hepsi mevcut
    BIR SERVISI ikinci kez basar, yeni bir sorgu yolu ACMAZ. Yani sozlesme
    +6 buyudu ama ARKASINDAKI kume sayisi DEGISMEDI. "Yeni uc = yeni veri
    yuzeyi" varsayimi burada yanlistir; genisleyen yalniz SUNUM'dur.

    ⚠️ Ayni dilim uc mevcut servisin `limit` parametresini `int | None`a
    genisletti (`accounts_service.list_accounts` · `ledger.build_ledger` ·
    `personnel` + `payroll` satir fonksiyonlari). Bu ne yol ne operasyon
    acar ve LISTE uclarinin kendi 200 tavani DEGISMEDI — sadece disa
    aktarma `limit=None` gecebilir.

    🔴 **BC-3 (+9 yol / +17 operasyon, ölçüldü 254/371):** belge ↔ varlık bağı —
    `GET /documents/slot-types` (1 yol) + dört sahip × (`GET/POST
    /<kök>/{owner_id}/documents` · `PATCH/DELETE /<kök>/documents/{link_id}`)
    = 8 yol / 16 operasyon. Kökler `/sections` · `/units` · `/sales` ·
    `/subcontractor-contracts`. 245→**254** · 354→**371**.

    🔴 **PLN-B1 (+20 yol / +25 operasyon, ölçüldü 274/396):** planlama (earned value)
    bütçesi — şirket `/earned-value/{disciplines,catalog}` (5 yol / 8 operasyon) +
    şantiye `/sites/{id}/earned-value/settings` (1 / 2) + `/sites/{id}/earned-value/
    budget…` (14 / 15). Hepsi EK; mevcut yol/şema DEĞİŞMEDİ (baseline farkı yapısal
    olarak ölçüldü). 254→**274** · 371→**396**. ⚠️ `earned-value` YENİ bir BFF kökü:
    frontend `ALLOWED_ROOTS`a eklenmezse şirket uçları YALNIZ CANLIDA 404 döner.

    🔴 **PLN-B2 (+5 yol / +5 operasyon, ölçüldü 279/401):** saha saat dağıtımı —
    `/sites/{id}/earned-value/code-tree` · `…/days/{day}` · `…/days/{day}/allocation` ·
    `…/days/{day}/previous-allocation` · `…/days/{day}/unlock`. Mevcut yol DEĞİŞMEDİ;
    mevcut şemalarda yalnız EK alan + `temperature_c` deprecated (yapısal olarak ölçüldü).
    274→**279** · 396→**401**.

    🔴 **PLN-B3 (+7 yol / +7 operasyon, ölçüldü 286/408):** raporlar —
    `/sites/{id}/earned-value/panel` · `…/reports/daily` · `…/reports/daily/{day}/approve` ·
    `…/reports/weekly` · `…/reports/weekly.xlsx` · `…/settings/preview` ·
    `…/settings/preview/composite`. Mevcut yol DEĞİŞMEDİ; mevcut şemalarda yalnız EK (zorunlu
    olmayan) alan + `SuggestionsOut.history` deprecated (yapısal olarak ölçüldü).
    279→**286** · 401→**408**.

    🔴 **BFF İZİN LİSTESİ — `units` KÖKÜ EKSİK (ölçüldü 2026-09-05).** Dört kökten
    ÜÇÜ frontend'in `ALLOWED_ROOTS`unda VAR (`sections` · `sales` ·
    `subcontractor-contracts`), **`units` YOK**. Ölçüm yöntemi kayda geçirilir —
    *kelime saymak ≠ olgu saymak*: `src/app/api/backend/[...path]/route.ts`
    içindeki `ALLOWED_ROOTS` **dengeli-parantez** ile çıkarılır, **yorumlar
    SİLİNİR**, sonra tırnaklı dizgeler sayılır → **51 eleman**, `units` bunlardan
    biri DEĞİL. (Aynı dosyada naif regex **40**, yorumlar dâhil sayım **111**
    verir; ikisi de yanlış. `command grep -c '"units"'` → **0**.)
    👉 Sonucu: `/units/{owner_id}/documents` ve `/units/documents/{link_id}`
    **YALNIZ CANLIDA 404** döner — jsdom görmez, backend CI yeşil kalır (bu
    deponun kanonik BFF tuzağı). `units` kökünü izin listesine eklemek
    FRONTEND diliminin işidir; backend tarafında yapılacak bir şey yoktur.

    🔴 **DSC-B0 (+1 yol / +2 operasyon):** `GET`/`PUT /users/{user_id}/disciplines` —
    kullanıcı disiplin ataması (EV uzantısı; DISIPLIN-KAPSAMI-SPEC). Tek yol üzerinde iki
    operasyon. Yani 286→**287** · 408→**410**.

    🔴 **BLF-B1.1 (+1 yol / +2 operasyon):** `GET`/`POST /section-types` — şirket geneli
    bölüm tipi listesi. Tek yol üzerinde iki operasyon (ölçüldü: baseline farkı
    `+ EKLENDİ: GET /section-types · POST /section-types`). Yani 287→**288** · 410→**412**.
    """
    from app.main import app

    sema = app.openapi()
    yollar = sema["paths"]
    operasyonlar = sum(
        1
        for uc in yollar.values()
        for metot in uc
        if metot in {"get", "post", "put", "patch", "delete"}
    )
    # AI-CHAT-2: 237→239 yol, 345→348 operasyon. Ek olan İKİ yol
    # (`/ai/conversations`, `/ai/conversations/{conversation_id}`) ve ÜÇ
    # operasyon (GET liste · GET detay · DELETE) sohbet saklamanın (A3)
    # uçlarıdır. Devir TAM EKtir: ortak şemalarda `required` DEĞİŞMEDİ ve
    # hiçbir alanın `type`ı değişmedi — ama bu iki sayaç zaten buna KÖRDÜR
    # (K-DEVIR2), kırıcılığın kanıtı `tests/contract/openapi_baseline.json`
    # farkıdır.
    #
    # EXPORT-XLSX bunun ÜSTÜNE +6 yol / +6 operasyon ekler (aşağıdaki
    # docstring girdisi). Rebase sonrası sayılar YENİDEN ÖLÇÜLDÜ, eski
    # 243/351'den türetilmedi: 239→245 · 348→354.
    # BDG-B1: `GET`/`PUT /sites/{site_id}/boq/section-distribution` = +1 yol / +2 operasyon
    # (287→288 · 410→412).
    # BLF-B1: `GET`/`POST /section-types` = +1 yol / +2 operasyon (288→289 · 412→414).
    # GKS-B1: `GET /sites/{site_id}/diary/skeleton` = +1 yol / +1 operasyon (289→290 · 414→415).
    # TKL-B2: `GET`/`POST /catalog/items` · `PATCH /catalog/items/{item_id}` ·
    # `GET /catalog/disciplines` = +3 yol / +4 operasyon (290→293 · 415→419).
    # TKL-B3: `POST /projects/{project_id}/contract/items/bulk` = +1 yol / +1 operasyon
    # (293→294 · 419→420).
    # TKL-B4.1: `GET`/`PUT /offers/settings` = +1 yol / +2 operasyon (294→295 · 420→422).
    # TKL-B4.2: teklif uçları (`/offers`, revizyonlar, geçişler, gruplar, kalemler)
    # = +13 yol / +19 operasyon (295→308 · 422→441).
    # TKL-B5: teklif şablonları (/offers/templates…) + teklif ve katalog Excel çıktıları
    # = +8 yol / +11 operasyon (308→316 · 441→452).
    # TKL-B6: `POST /offers/{offer_id}/convert` + `POST …/earned-value/budget/fill-from-contract`
    # = +2 yol / +2 operasyon (316→318 · 452→454).
    # KAT-B1: `POST /catalog/items/bulk` (toplu katalog ekleme / fiyat guncelleme; yeni yol,
    # tek operasyon) = +1 yol / +1 operasyon (318→319 · 454→455). Olculdu: openapi baseline
    # 319 yol / 455 operasyon. `source_code`/`ref_price_date` alanlari ve
    # `on_source_conflict` ne yol ne operasyon acar.
    # OKT-B1: `GET /approvals/history` (onay gecmisi) = +1 yol / +1 operasyon
    # (319→320 · 455→456).
    assert len(yollar) == 320
    assert operasyonlar == 456

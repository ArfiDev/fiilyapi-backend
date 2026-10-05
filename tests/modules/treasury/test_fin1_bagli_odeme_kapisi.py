"""FIN-1 — BAĞLI ÖDEMESİ OLAN evrağın SİLİNMESİ ve YÖNÜNÜN ÇEVRİLMESİ.

`delete_instrument` ve `update_instrument` kapılarının TEK ölçütü DURUMDU
(`status is portfolio` / `status in TERMINAL_STATUSES`); evrağa BAĞLI ödeme olup
olmadığına bakılmıyordu. İki kusur da portföydeki bir çekte, `treasury:admin` ve
`treasury:full` ile canlıda koşulabilirdi.

## 1) SİLME — bakiye ŞİŞER, `101` KALICI kalır

`payments.financial_instrument_id` FK'si **`ON DELETE SET NULL`**dur
(`treasury/models.py`): portföydeki çek silinince ona bağlı ödemelerin bağı
NULL'lanır. `balance.cash_realized_condition` bağsız ödemeyi **DAİMA** nakit
sayar (`Payment.financial_instrument_id.is_(None)` dalı) — yani banka bakiyesi
HENÜZ TAHSİL EDİLMEMİŞ çek tutarı kadar ANINDA şişer. Üstelik `101 Alınan
Çekler`i boşaltacak tek olay (`collected` geçişi) artık DOĞAMAZ, çünkü evrak
satırı yoktur: defterde kalıcı bir `101` kalıntısı kalır. Her fiş tek başına
dengeli olduğu için mizan doğru görünür ve hiçbir kolon farkı kusuru ele vermez.

Kardeş kapı BU SINIFI ZATEN KAPATIYOR: `treasury.service.delete_account`
silmeden önce `count_payments_for_account` ön denetimini koşar ve
`RelatedRecordsExistError` atar. Bu kapının karşılığı yoktu.

## 2) YÖN — `101` açılır, `103` kaynaksız borçlanır

Ödeme yazılırken bağın yön uyumu bir kez doğrulanır
(`payments_service._UYUMLU_YON`, 422). PATCH bunu GERİYE DÖNÜK
geçersizleştirirdi ve yeniden doğrulanmazdı: ödeme fişinin nakit bacağı
FATURANIN akışından (`treasury.posting`), tahsil/ödeme fişininki ise
ENSTRÜMANIN `direction`'ından (`instruments.posting`) seçilir. Yön çevrilince
ikisi ayrışır; `101` hiç kapanmaz, `103` kaynağı olmayan bir borç taşır ve
terminal durumdan çıkış olmadığı için düzeltecek ikinci geçiş DOĞAMAZ.

## MEVCUT BEKÇİLER NEDEN KÖR

`test_fin1_api.py::test_DELETE_yalniz_PORTFOYDE` ve
`::test_PATCH_TERMINAL_kayitta_YON_degistirilemez` ikisi de `cek_fabrikasi()`yi
BAĞLI ÖDEME OLMADAN kurar — kusurun tek ön koşulunu hiç oluşturmazlar. İkincisi
son bölümünde portföydeki (ödemesiz) çekte yönün serbest olduğunu AÇIKÇA iddia
eder; o iddia DOĞRUDUR ve burada değişmez: kilitleyen şey durum değil, BAĞDIR.
"""

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.modules.invoicing.models import InvoiceDirection
from app.modules.treasury.instruments import guards
from app.modules.treasury.models import FinancialInstrumentDirection, Payment
from tests._silme_yardimci import sil_aile

KOK = "/financial-instruments"


def _odeme_govdesi(account, amount: str, cek) -> dict:  # noqa: ANN001
    return {
        "bank_account_id": str(account.id),
        "method": "cheque",
        "amount": amount,
        "paid_on": "2026-08-14",
        "financial_instrument_id": str(cek.id),
    }


async def _bakiye(client, headers, account) -> Decimal:  # noqa: ANN001
    """Hesabın UÇTAN okunan türev bakiyesi — kolon değil, KULLANICININ gördüğü sayı."""
    resp = await client.get("/bank-accounts", headers=headers)
    assert resp.status_code == 200, resp.text
    return Decimal(next(k["balance"] for k in resp.json()["items"] if k["id"] == str(account.id)))


async def _bag_var_mi(seeded_db, payment_id: str) -> bool:  # noqa: ANN001
    """Ödeme satırı DB'de hâlâ var mı (taze okuma)."""
    satir = (
        await seeded_db.execute(select(Payment).where(Payment.id == uuid.UUID(payment_id)))
    ).scalar_one_or_none()
    return satir is not None


async def test_bagli_odemesi_olan_PORTFOY_cek_silinince_odemesi_de_gider_ve_bakiye_SISMEZ(
    client,  # noqa: ANN001
    admin_headers,  # noqa: ANN001
    seeded_db,  # noqa: ANN001
    fatura_fabrikasi,  # noqa: ANN001
    hesap_fabrikasi,  # noqa: ANN001
    cek_fabrikasi,  # noqa: ANN001
    odeme_eslemesi,  # noqa: ANN001
) -> None:
    """🔴 ASIL MALİ İDDİA (SIL-B2'de yeniden yazıldı): çek silinirken bağlı ödemesi SESSİZCE
    bağsız bırakılmaz. Eski kusur: `SET NULL` ödemeyi bağsız bırakır, bakiye tahsil edilmemiş çek
    tutarı kadar şişerdi. Artık ödeme (ve fişi) çekle BİRLİKTE gider; bakiye şişmez.
    """
    account = await hesap_fabrikasi(opening_balance="0.00")
    cek = await cek_fabrikasi(direction=FinancialInstrumentDirection.received, amount="500.00")
    # Giden fatura = bizim kestiğimiz → tahsilat → ALINAN çek (yön uyumu, K3).
    fatura = await fatura_fabrikasi(direction=InvoiceDirection.outgoing, total="1000.00")

    odeme = await client.post(
        f"/invoices/{fatura.id}/payments",
        json=_odeme_govdesi(account, "500.00", cek),
        headers=admin_headers,
    )
    assert odeme.status_code == 201, odeme.text
    # Çek portföyde: para HENÜZ bankada değil, bakiye 0 (ODM-1 D2).
    once = await _bakiye(client, admin_headers, account)
    assert once == Decimal("0.00")
    odeme_id, cek_id = odeme.json()["id"], cek.id

    sil = await sil_aile(client, admin_headers, "financial_instrument", cek_id)

    assert sil.status_code == 204, sil.text
    # 1) PARA — tahsil edilmemiş çek tutarı bakiyeye SIZMADI.
    assert await _bakiye(client, admin_headers, account) == once
    # 2) BAĞ — ödeme bağsız ve yaşayan kalmadı: çekle birlikte gitti.
    assert await _bag_var_mi(seeded_db, odeme_id) is False
    # 3) Çek de gitti.
    assert (await client.get(f"{KOK}/{cek_id}", headers=admin_headers)).status_code == 404


async def test_bagli_odemesi_olan_PORTFOY_cekte_YON_degistirilemez_409(
    client,  # noqa: ANN001
    muhasebe_headers,  # noqa: ANN001
    admin_headers,  # noqa: ANN001
    seeded_db,  # noqa: ANN001
    fatura_fabrikasi,  # noqa: ANN001
    hesap_fabrikasi,  # noqa: ANN001
    cek_fabrikasi,  # noqa: ANN001
    odeme_eslemesi,  # noqa: ANN001
) -> None:
    """🔴 Ödeme yazılırken bir kez doğrulanan yön uyumu PATCH ile GERİYE DÖNÜK
    bozulamaz.

    Mesaj `TERMINAL_STATUS_DIRECTION`dan AYRIDIR: kayıt terminal DEĞİLDİR ve
    kullanıcının yapabileceği şey de farklıdır ("önce bağlı ödemeyi sil", "bu
    kayıt kapandı" değil).
    """
    account = await hesap_fabrikasi(opening_balance="0.00")
    cek = await cek_fabrikasi(direction=FinancialInstrumentDirection.received, amount="500.00")
    fatura = await fatura_fabrikasi(direction=InvoiceDirection.outgoing, total="1000.00")

    odeme = await client.post(
        f"/invoices/{fatura.id}/payments",
        json=_odeme_govdesi(account, "500.00", cek),
        headers=admin_headers,
    )
    assert odeme.status_code == 201, odeme.text

    resp = await client.patch(
        f"{KOK}/{cek.id}", json={"direction": "issued"}, headers=muhasebe_headers
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.DIRECTION_LOCKED_BY_PAYMENTS
    # …ve kolon GERÇEKTEN dönmedi: uçtan 409 okunup değerin yazılmış olması,
    # `flush` sırasına bağlı sessiz bir kusur olurdu.
    await seeded_db.refresh(cek)
    assert cek.direction is FinancialInstrumentDirection.received


async def test_bagli_odemesi_olan_cekte_TUR_de_degistirilemez_409(
    client,  # noqa: ANN001
    muhasebe_headers,  # noqa: ANN001
    admin_headers,  # noqa: ANN001
    fatura_fabrikasi,  # noqa: ANN001
    hesap_fabrikasi,  # noqa: ANN001
    cek_fabrikasi,  # noqa: ANN001
    odeme_eslemesi,  # noqa: ANN001
) -> None:
    """`instrument_kind` AYNI demettedir: kapı `direction`a özel yazılsaydı tür
    değişimi açık kalır ve aynı demetin yarısı bekçisiz olurdu."""
    account = await hesap_fabrikasi(opening_balance="0.00")
    cek = await cek_fabrikasi(direction=FinancialInstrumentDirection.received, amount="500.00")
    fatura = await fatura_fabrikasi(direction=InvoiceDirection.outgoing, total="1000.00")

    odeme = await client.post(
        f"/invoices/{fatura.id}/payments",
        json=_odeme_govdesi(account, "500.00", cek),
        headers=admin_headers,
    )
    assert odeme.status_code == 201, odeme.text

    resp = await client.patch(
        f"{KOK}/{cek.id}", json={"instrument_kind": "promissory_note"}, headers=muhasebe_headers
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.DIRECTION_LOCKED_BY_PAYMENTS


async def test_bagli_odemesi_olan_cekte_YON_DISI_alanlar_SERBEST_kalir(
    client,  # noqa: ANN001
    muhasebe_headers,  # noqa: ANN001
    admin_headers,  # noqa: ANN001
    fatura_fabrikasi,  # noqa: ANN001
    hesap_fabrikasi,  # noqa: ANN001
    cek_fabrikasi,  # noqa: ANN001
    odeme_eslemesi,  # noqa: ANN001
) -> None:
    """🔴 KAPI DAR OLMALI. Bağ, kaydın TAMAMINI dondurmaz: kilitlenen tek şey
    fişlemenin okuduğu `direction`/`instrument_kind` demetidir.

    Bu bekçi olmasaydı, kapıyı `update_instrument`ın başına koyan bir uygulama
    da yeşil geçerdi ve kullanıcı bağlı bir çekte keşideci adını bile
    düzeltemezdi.
    """
    account = await hesap_fabrikasi(opening_balance="0.00")
    cek = await cek_fabrikasi(direction=FinancialInstrumentDirection.received, amount="500.00")
    fatura = await fatura_fabrikasi(direction=InvoiceDirection.outgoing, total="1000.00")

    odeme = await client.post(
        f"/invoices/{fatura.id}/payments",
        json=_odeme_govdesi(account, "500.00", cek),
        headers=admin_headers,
    )
    assert odeme.status_code == 201, odeme.text

    resp = await client.patch(
        f"{KOK}/{cek.id}", json={"drawer_name": "Yeni Keşideci A.Ş."}, headers=muhasebe_headers
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["drawer_name"] == "Yeni Keşideci A.Ş."


async def test_ODEMESIZ_portfoy_cek_SILINIR_ve_YONU_CEVRILIR(
    client,  # noqa: ANN001
    muhasebe_headers,  # noqa: ANN001
    admin_headers,  # noqa: ANN001
    cek_fabrikasi,  # noqa: ANN001
) -> None:
    """🔴 POZİTİF KONTROL: kapı BAĞA bakar, duruma değil.

    Sayım hep >0 dönen (ör. yüklemi yanlış kolona kurulmuş) bir uygulama
    yukarıdaki üç bekçiyi yeşil geçirirdi; bu test onu kırmızıya çevirir.
    """
    cevrilecek = await cek_fabrikasi(direction=FinancialInstrumentDirection.received)
    yon = await client.patch(
        f"{KOK}/{cevrilecek.id}", json={"direction": "issued"}, headers=muhasebe_headers
    )
    assert yon.status_code == 200, yon.text

    silinecek = await cek_fabrikasi()
    assert (
        await sil_aile(client, admin_headers, "financial_instrument", silinecek.id)
    ).status_code == 204

"""P8 T5 — `GET /projects/{id}/sales/summary` (spec §4; mockup `Satış Yönetimi`).

Mockup satır numaraları (KPI şeridi, S55-59):
- 55 "Satılan (Tapulu)" · 34 · ₺31,4M   → `sold` (active + deed_transferred)
- 56 "Rezerve" · 5 · ₺4,2M potansiyel   → `reserved`
- 57 "Boş Ünite" · 13 · ₺12,6M stok     → `available_units` (`units.sales_status`)
- 58 "Tahsil Edilen" · ₺24,8M · %79     → `collection`
- 59 "Vadesi Geçen" · ₺840K · 3 taksit  → `overdue`

Oran doğrulaması: mockup'ın tfoot TOPLAM'ı (satır 208-210) 31.420.000 sözleşme
ve 24.820.000 tahsilat gösterir; 24,82 / 31,42 = %79,0 → S58'in "%79 tahsilat"
ifadesi TAM OLARAK bu iki toplamın oranıdır. `collection_pct`in paydası bu
yüzden "tüm açık satışların satış bedeli toplamı"dır.

- 218-234 "Yaklaşan Tahsilatlar (30 Gün)" → `upcoming_collections`
- 223 "Gecikme faizi: ₺4.200" → YALNIZ GÖSTERİM türevi (§8 S5): tahakkuk satırı
  YAZILMAZ, kolon AÇILMAZ.
- 188 "Kapora alındı · 15 gün süre" → `expired_reservations` (§8 S4): otomatik
  iptal YOKTUR, yalnız gösterge.
"""

import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core import timezone
from app.modules.audit.models import AuditLog
from app.modules.sales.models import SaleInstallment
from app.modules.sales.summary import late_fee_amount

pytestmark = pytest.mark.asyncio


def _g(gun: int) -> str:
    # Her cagrida taze: uygulama da istek basina `timezone.today()` okur.
    return str(timezone.today() + timedelta(days=gun))


# TMP-FIX: `BUGUN` import ANINDA donar; uygulama ise her istekte `timezone.today()`
# okur. CI'da import 20:59 UTC, test 21:00+ UTC (TR gece yarisi) olursa test "dun"u,
# uygulama "bugun"u kullanir -> hepsi 1 gun kayar. Saat noktalari: TR ogle (12:00Z)
# ve TR gece yarisini yeni asmis an (21:30Z = 00:30 TR ertesi gun).
# Anlar, fixture CAGRI ANINDAKI TR gunune gore kurulur: 21:30Z'de TR takvimi
# bugun+1'dir -> CI'daki gece yarisi asimi birebir.
_SAAT_NOKTALARI = {"12:00Z": time(12, 0), "21:30Z": time(21, 30)}


@pytest.fixture(autouse=True, params=list(_SAAT_NOKTALARI))
def _dondurulmus_saat(request, monkeypatch):
    """`timezone.today()`i verilen UTC aninda dondurur (uygulama + test ayni saat)."""
    an = datetime.combine(timezone.today(), _SAAT_NOKTALARI[request.param], tzinfo=UTC)

    class _SabitDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return an.astimezone(tz) if tz is not None else an.replace(tzinfo=None)

    monkeypatch.setattr(timezone, "datetime", _SabitDatetime)


async def _satis(client, headers, proje, unite, musteri, **degisiklikler) -> dict:
    govde = {
        "unit_id": str(unite.id),
        "customer_id": str(musteri.id),
        "sale_type": "sale",
        "sale_price": "1000000.00",
        **degisiklikler,
    }
    resp = await client.post(f"/projects/{proje.id}/sales", json=govde, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _plan(client, headers, sale_id, satirlar: list[dict]) -> dict:
    resp = await client.put(
        f"/sales/{sale_id}/installments", json={"items": satirlar}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _satir(sequence_no: int, amount: str, due_date: str) -> dict:
    return {
        "sequence_no": sequence_no,
        "label": "Peşinat" if sequence_no == 0 else f"{sequence_no} / 2",
        "due_date": due_date,
        "amount": amount,
    }


async def _ozet(client, headers, proje) -> dict:
    resp = await client.get(f"/projects/{proje.id}/sales/summary", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- 1) KPI şeridi (S55-59) ---


async def test_kpi_seridi_satilan_rezerve_bos_tahsilat_gecikme(
    client, admin_headers, proje, unite, ikinci_unite, musteri
):
    """S55-59'un beşi de TEK yanıttan okunur."""
    satilan = await _satis(client, admin_headers, proje, unite, musteri)
    assert (
        await client.post(f"/sales/{satilan['id']}/transfer-deed", headers=admin_headers)
    ).status_code == 200
    await _plan(
        client,
        admin_headers,
        satilan["id"],
        [_satir(0, "600000.00", _g(-10)), _satir(1, "400000.00", _g(-5))],
    )
    plan = (await client.get(f"/sales/{satilan['id']}/installments", headers=admin_headers)).json()
    pesinat = plan["items"][0]["id"]
    assert (
        await client.post(
            f"/sales/installments/{pesinat}/pay",
            json={"amount": "600000.00"},
            headers=admin_headers,
        )
    ).status_code == 200

    rezerve = await _satis(
        client,
        admin_headers,
        proje,
        ikinci_unite,
        musteri,
        sale_type="reservation",
        sale_price="800000.00",
        reservation_deposit="50000.00",
        reservation_due_date=_g(-2),
    )
    assert rezerve["status"] == "reservation"

    ozet = await _ozet(client, admin_headers, proje)

    # S55 — `deed_transferred` "Tapulu" alt sayacıyla birlikte döner.
    assert ozet["sold"] == {
        "count": 1,
        "deed_transferred_count": 1,
        "amount": "1000000.00",
    }
    # S56 — rezerve: adet + potansiyel bedel + süresi dolan sayısı (§8 S4).
    assert ozet["reserved"] == {"count": 1, "expired_count": 1, "amount": "800000.00"}
    # S57 — "Boş Ünite": `units.sales_status == listed` (satış kaydından TÜREMİŞ).
    assert ozet["available_units"]["count"] == 0
    # S58 — tahsilat oranı = tahsil edilen / açık satışların satış bedeli.
    assert ozet["collection"]["collected_amount"] == "600000.00"
    assert ozet["collection"]["contracted_amount"] == "1800000.00"
    assert ozet["collection"]["collection_pct"] == "33.33"
    # S59 — vadesi geçen: satır sayısı + KALAN tutar (tahsil edilen düşülür).
    assert ozet["overdue"]["installment_count"] == 1
    assert ozet["overdue"]["amount"] == "400000.00"


async def test_bos_unite_kpisi_listed_unitelerden_gelir(
    client, admin_headers, proje, unite, ikinci_unite, arsa_sahibi_unitesi, musteri
):
    """S57 "13 · ₺12,6M stok": adet ve LİSTE FİYATI toplamı `units`ten okunur."""
    ozet = await _ozet(client, admin_headers, proje)

    # Üç ünite de henüz satılmadı → hepsi `listed`.
    assert ozet["available_units"]["count"] == 3
    # Yalnız `unite` fixture'ının liste fiyatı vardır (1.480.000); NULL'lar 0 sayılır.
    assert ozet["available_units"]["list_price_total"] == "1480000.00"

    await _satis(client, admin_headers, proje, unite, musteri)
    ozet = await _ozet(client, admin_headers, proje)
    assert ozet["available_units"]["count"] == 2
    assert ozet["available_units"]["list_price_total"] == "0.00"


async def test_iptal_edilen_satis_hicbir_kpiye_girmez(client, admin_headers, proje, unite, musteri):
    satis = await _satis(client, admin_headers, proje, unite, musteri)
    await _plan(client, admin_headers, satis["id"], [_satir(0, "1000000.00", _g(-3))])
    assert (
        await client.post(
            f"/sales/{satis['id']}/cancel", json={"reason": "Fesih"}, headers=admin_headers
        )
    ).status_code == 200

    ozet = await _ozet(client, admin_headers, proje)

    assert ozet["sold"]["count"] == 0
    assert ozet["reserved"]["count"] == 0
    assert ozet["collection"]["contracted_amount"] == "0.00"
    assert ozet["collection"]["collection_pct"] is None
    assert ozet["overdue"]["installment_count"] == 0
    assert ozet["upcoming_collections"] == []


async def test_satis_yoksa_kpiler_sifir_doner(client, admin_headers, proje):
    """Sıfır ile "veri yok" ayrımı korunur: anahtarlar HER ZAMAN döner."""
    ozet = await _ozet(client, admin_headers, proje)

    assert ozet["sold"]["count"] == 0
    assert ozet["sold"]["amount"] == "0.00"
    assert ozet["collection"]["collection_pct"] is None
    assert ozet["overdue"]["amount"] == "0.00"
    assert ozet["upcoming_collections"] == []
    assert ozet["expired_reservations"] == []
    # Maliyet/kâr KPI'sı AÇILMAZ (kalıcı karar 3) — dürüst yer tutucu.
    assert ozet["pending_modules"] == ["project_costs"]


# --- 2) Yaklaşan tahsilatlar (S218-234, 30 gün) ---


async def test_yaklasan_tahsilat_penceresi_30_gun(client, admin_headers, proje, unite, musteri):
    """Pencere: vadesi GEÇMİŞ satırlar + bugünden itibaren 30 gün (S219 başlığı).

    S220-223'ün ilk satırı "Vadesi 15 gün geçti" diyor; yani gecikmişler de bu
    listede DURUR — pencere yalnız ileriye bakmaz.
    """
    satis = await _satis(client, admin_headers, proje, unite, musteri, sale_price="1000000.00")
    await _plan(
        client,
        admin_headers,
        satis["id"],
        [
            _satir(0, "100000.00", _g(-15)),  # gecikmiş → listede
            _satir(1, "200000.00", _g(29)),  # pencere İÇİ
            _satir(2, "300000.00", _g(31)),  # pencere DIŞI
            _satir(3, "400000.00", _g(5)),  # pencere içi
        ],
    )

    ozet = await _ozet(client, admin_headers, proje)

    siralar = [row["sequence_no"] for row in ozet["upcoming_collections"]]
    assert siralar == [0, 3, 1]  # vade tarihine göre sıralı
    ilk = ozet["upcoming_collections"][0]
    assert ilk["unit_label"] == "A Blok · 12"
    assert ilk["customer_name"] == musteri.name
    assert ilk["is_overdue"] is True
    assert ilk["days_overdue"] == 15
    assert ilk["remaining_amount"] == "100000.00"
    assert ozet["upcoming_collections"][1]["is_overdue"] is False
    assert ozet["upcoming_collections"][1]["days_overdue"] == 0


async def test_tam_odenmis_taksit_yaklasan_listesinde_yoktur(
    client, admin_headers, proje, unite, musteri
):
    satis = await _satis(client, admin_headers, proje, unite, musteri)
    plan = await _plan(
        client,
        admin_headers,
        satis["id"],
        [_satir(0, "400000.00", _g(3)), _satir(1, "600000.00", _g(10))],
    )
    assert (
        await client.post(
            f"/sales/installments/{plan['items'][0]['id']}/pay",
            json={"amount": "400000.00"},
            headers=admin_headers,
        )
    ).status_code == 200

    ozet = await _ozet(client, admin_headers, proje)

    assert [row["sequence_no"] for row in ozet["upcoming_collections"]] == [1]


# --- 3) Gecikme faizi (S223, §8 S5): YALNIZ gösterim ---


def test_gecikme_faizi_gun_orantili_hesaplanir():
    """Aylık oran (F163) gün sayısına ORANTILANIR — saf fonksiyon.

    Mockup satır 223 yalnız sonucu (₺4.200) gösterir, oranı vermez; bu yüzden
    formül F163'ün "aylık gecikme faizi %" tanımından türetilir: kalan tutar ×
    aylık oran × (gecikme günü / 30). Ay sonu yuvarlaması (gün → tam ay)
    seçilseydi 1 günlük gecikme bir aylık faiz doğururdu.
    """
    assert late_fee_amount(Decimal("248000.00"), Decimal("3.39"), 15) == Decimal("4203.60")
    # Oran girilmemişse (F163 boş) faiz UYGULANMAZ.
    assert late_fee_amount(Decimal("248000.00"), None, 15) == Decimal("0.00")
    # Gecikme yoksa faiz de yoktur.
    assert late_fee_amount(Decimal("248000.00"), Decimal("3.39"), 0) == Decimal("0.00")


async def test_gecikme_faizi_ozette_gosterilir_kayit_YAZILMAZ(
    client, admin_headers, seeded_db, proje, unite, musteri
):
    """§8 S5: faiz gösterilir; ne taksit tutarı şişer ne yeni satır doğar."""
    satis = await _satis(client, admin_headers, proje, unite, musteri, late_fee_monthly_pct="3.00")
    await _plan(client, admin_headers, satis["id"], [_satir(0, "1000000.00", _g(-30))])

    ozet = await _ozet(client, admin_headers, proje)

    # 1.000.000 × %3 × (30/30) = 30.000
    assert ozet["upcoming_collections"][0]["late_fee_amount"] == "30000.00"
    assert ozet["overdue"]["late_fee_amount"] == "30000.00"
    # Tahakkuk YOK: satır sayısı ve tutarı DEĞİŞMEDİ.
    stmt = select(SaleInstallment).where(SaleInstallment.sale_id == uuid.UUID(satis["id"]))
    rows = (await seeded_db.execute(stmt)).scalars().all()
    assert len(rows) == 1
    assert rows[0].amount == Decimal("1000000.00")
    assert rows[0].paid_amount == Decimal("0.00")


async def test_faiz_orani_bos_satista_faiz_sifirdir(client, admin_headers, proje, unite, musteri):
    satis = await _satis(client, admin_headers, proje, unite, musteri)
    await _plan(client, admin_headers, satis["id"], [_satir(0, "1000000.00", _g(-30))])

    ozet = await _ozet(client, admin_headers, proje)

    assert ozet["upcoming_collections"][0]["late_fee_amount"] == "0.00"
    assert ozet["overdue"]["late_fee_amount"] == "0.00"


# --- 4) Rezervasyon süresi doldu türevi (S188, §8 S4) ---


async def test_suresi_dolan_rezervasyon_turevi_iptal_ETMEZ(
    client, admin_headers, seeded_db, proje, unite, ikinci_unite, musteri
):
    dolmus = await _satis(
        client,
        admin_headers,
        proje,
        unite,
        musteri,
        sale_type="reservation",
        reservation_deposit="50000.00",
        reservation_due_date=_g(-4),
    )
    suruyor = await _satis(
        client,
        admin_headers,
        proje,
        ikinci_unite,
        musteri,
        sale_type="reservation",
        reservation_due_date=_g(4),
    )

    ozet = await _ozet(client, admin_headers, proje)

    assert [row["sale_id"] for row in ozet["expired_reservations"]] == [dolmus["id"]]
    assert ozet["expired_reservations"][0]["days_expired"] == 4
    assert ozet["expired_reservations"][0]["reservation_deposit"] == "50000.00"
    assert ozet["expired_reservations"][0]["unit_label"] == "A Blok · 12"
    assert ozet["reserved"]["expired_count"] == 1

    # Otomatik iptal YOK: iki kayıt da `reservation` KALIR.
    for satis in (dolmus, suruyor):
        detay = await client.get(f"/sales/{satis['id']}", headers=admin_headers)
        assert detay.json()["status"] == "reservation"


async def test_vadesi_olmayan_rezervasyon_suresi_dolmus_SAYILMAZ(
    client, admin_headers, proje, unite, musteri
):
    await _satis(client, admin_headers, proje, unite, musteri, sale_type="reservation")

    ozet = await _ozet(client, admin_headers, proje)

    assert ozet["expired_reservations"] == []
    assert ozet["reserved"]["expired_count"] == 0


# --- 5) Görünürlük, yetki, denetim ---


async def test_kapsam_disi_ozet_404(client, kapsam_disi_headers, proje):
    resp = await client.get(f"/projects/{proje.id}/sales/summary", headers=kapsam_disi_headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Proje bulunamadı"


async def test_olmayan_proje_ozeti_404(client, admin_headers):
    resp = await client.get(f"/projects/{uuid.uuid4()}/sales/summary", headers=admin_headers)
    assert resp.status_code == 404


async def test_view_yetkisi_ozeti_okuyabilir(client, view_headers, proje):
    resp = await client.get(f"/projects/{proje.id}/sales/summary", headers=view_headers)
    assert resp.status_code == 200, resp.text


async def test_yetkisiz_ozet_403(client, yetkisiz_headers, proje):
    resp = await client.get(f"/projects/{proje.id}/sales/summary", headers=yetkisiz_headers)
    assert resp.status_code == 403


async def test_ozet_okumasi_denetim_satiri_URETMEZ(client, admin_headers, seeded_db, proje):
    once = (await seeded_db.execute(select(func.count()).select_from(AuditLog))).scalar_one()

    await _ozet(client, admin_headers, proje)

    sonra = (await seeded_db.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    assert sonra == once

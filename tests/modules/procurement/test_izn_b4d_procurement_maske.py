"""IZN-B4d — satınalma hassas alan maskesi: maliyet gizli rolde tutar `null`, miktar görünür,
POST serbest, PATCH'te dolu tutar 403, export hücresi boş; bayraksız rol hepsini görür
(POZİTİF KONTROL). Onay eşiği (sunucu içi) maskeden etkilenmez.
"""

from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.core.sayfalar import HiddenCategory
from app.modules.procurement.models import PurchaseRequestStatus
from tests._hassas_alan import rol_gizle

_YOL = "/purchase-requests"


def _teklif(supplier_id, **alanlar) -> dict:
    govde = {
        "supplier_id": str(supplier_id),
        "unit_price": "1250.00",
        "delivery_time": "3 iş günü",
        "payment_terms": "days_30",
        "shipping_included": False,
        "shipping_cost": "8000.00",
    }
    govde.update(alanlar)
    return govde


@pytest.fixture
async def teklifli_talep(
    client, satinalma_headers, gorunen_proje, talep_fabrikasi, tedarikci_fabrikasi
):
    """`quote_wait` talep (4×1000 + 6×1000, miktar 10) + bir teklif."""
    talep = await talep_fabrikasi(
        gorunen_proje,
        status=PurchaseRequestStatus.quote_wait,
        lines=[("4.000", "1000.00"), ("6.000", "1000.00")],
    )
    tedarikci = await tedarikci_fabrikasi("Demirsan A.Ş.")
    yanit = await client.post(
        f"{_YOL}/{talep.id}/quotes", json=_teklif(tedarikci.id), headers=satinalma_headers
    )
    assert yanit.status_code == 201, yanit.text
    return talep, tedarikci, yanit.json()["id"]


@pytest.mark.asyncio
async def test_bayraksiz_rol_tutarlari_gorur(client, satinalma_headers, teklifli_talep):
    talep, _, _ = teklifli_talep

    detay = (await client.get(f"{_YOL}/{talep.id}", headers=satinalma_headers)).json()
    assert Decimal(detay["estimated_total"]) == Decimal("10000")
    assert Decimal(detay["lines"][0]["estimated_unit_price"]) == Decimal("1000")
    assert Decimal(detay["lines"][0]["line_total"]) == Decimal("4000")

    karsilastirma = (
        await client.get(f"{_YOL}/{talep.id}/quotes", headers=satinalma_headers)
    ).json()
    kart = karsilastirma["items"][0]
    assert kart["unit_price"] == "1250.00"
    assert kart["shipping_cost"] == "8000.00"
    assert Decimal(kart["total_cost"]) == Decimal("20500")


@pytest.mark.asyncio
async def test_maliyet_kar_gizli_rolde_tutar_null_miktar_gorunur(
    client, satinalma_headers, seeded_db, teklifli_talep
):
    talep, tedarikci, _ = teklifli_talep
    await rol_gizle(seeded_db, "procurement", HiddenCategory.maliyet_kar)

    detay = (await client.get(f"{_YOL}/{talep.id}", headers=satinalma_headers)).json()
    assert detay["estimated_total"] is None
    satir = detay["lines"][0]
    assert satir["estimated_unit_price"] is None
    assert satir["line_total"] is None
    assert satir["quantity"] == "4.000"

    karsilastirma = (
        await client.get(f"{_YOL}/{talep.id}/quotes", headers=satinalma_headers)
    ).json()
    kart = karsilastirma["items"][0]
    assert kart["unit_price"] is None
    assert kart["shipping_cost"] is None
    assert kart["total_cost"] is None
    assert kart["supplier_name"] == "Demirsan A.Ş."
    assert karsilastirma["request_quantity_total"] == "10.000"

    kart_tedarikci = (
        await client.get(f"/suppliers/{tedarikci.id}", headers=satinalma_headers)
    ).json()
    assert kart_tedarikci["orders_total_this_year"] is None
    assert kart_tedarikci["phone"] == "0212 555 00 01"  # tedarikçi iletişimi GİZLENMEZ

    ozet = (await client.get("/purchasing/summary", headers=satinalma_headers)).json()
    assert ozet["orders_this_month_total"] is None
    assert ozet["quote_wait_requests"] == 1


@pytest.mark.asyncio
async def test_maliyet_kar_gizli_rolde_siparis_tutari_null(
    client, satinalma_headers, seeded_db, gorunen_proje, tedarikci_fabrikasi
):
    tedarikci = await tedarikci_fabrikasi("Beton A.Ş.")
    olustur = await client.post(
        "/purchase-orders",
        json={
            "project_id": str(gorunen_proje.id),
            "supplier_id": str(tedarikci.id),
            "total_amount": "75000.00",
        },
        headers=satinalma_headers,
    )
    assert olustur.status_code == 201, olustur.text
    assert olustur.json()["total_amount"] == "75000.00"  # pozitif kontrol: bayraksız görür

    await rol_gizle(seeded_db, "procurement", HiddenCategory.maliyet_kar)
    liste = (await client.get("/purchase-orders", headers=satinalma_headers)).json()
    assert liste["total"] == 1
    assert liste["items"][0]["total_amount"] is None


@pytest.mark.asyncio
async def test_maliyet_kar_gizli_rol_teklif_POSTlayabilir_ama_PATCHte_tutar_403(
    client, satinalma_headers, seeded_db, teklifli_talep
):
    talep, tedarikci, teklif_id = teklifli_talep
    await rol_gizle(seeded_db, "procurement", HiddenCategory.maliyet_kar)

    # POST serbest: yanıt maskeli döner.
    yeni = await client.post(
        f"{_YOL}/{talep.id}/quotes",
        json=_teklif(tedarikci.id, unit_price="1300.00"),
        headers=satinalma_headers,
    )
    assert yeni.status_code == 201, yeni.text
    assert yeni.json()["unit_price"] is None

    # PATCH'te dolu tutar → 403, kayıt değişmez.
    reddet = await client.patch(
        f"{_YOL}/{talep.id}/quotes/{teklif_id}",
        json={"unit_price": "1.00"},
        headers=satinalma_headers,
    )
    assert reddet.status_code == 403, reddet.text

    # Tutar içermeyen PATCH geçer.
    gecer = await client.patch(
        f"{_YOL}/{talep.id}/quotes/{teklif_id}",
        json={"delivery_time": "Yarın sabah"},
        headers=satinalma_headers,
    )
    assert gecer.status_code == 200, gecer.text
    assert gecer.json()["unit_price"] is None


@pytest.mark.asyncio
async def test_export_gizli_rolde_tutar_hucreleri_bos_bayraksizda_dolu(
    client, satinalma_headers, seeded_db, teklifli_talep
):
    talep, _, _ = teklifli_talep
    yol = f"{_YOL}/{talep.id}/quotes/export.xlsx"

    acik = await client.get(yol, headers=satinalma_headers)
    assert acik.status_code == 200
    satir = [h.value for h in load_workbook(BytesIO(acik.content)).active[2]]
    assert "20.500,00" in satir[2]  # pozitif kontrol: Toplam dolu

    await rol_gizle(seeded_db, "procurement", HiddenCategory.maliyet_kar)
    gizli = await client.get(yol, headers=satinalma_headers)
    assert gizli.status_code == 200
    satir = [h.value for h in load_workbook(BytesIO(gizli.content)).active[2]]
    assert satir[0] == "Demirsan A.Ş."
    assert satir[1] in ("", None) and satir[2] in ("", None)


async def _esik_davranisi(client, headers, gorunen_proje, talep_fabrikasi, seeded_db) -> dict:
    """Eşik ALTI ve ÜSTÜ birer talebi `headers` ile onaylar; yanıt kodu + durum + ret metni döner.

    Varsayılan eşik ₺500.000 (`DEFAULT_APPROVAL_THRESHOLD_TRY`); 100.000 altında, 900.000 üstünde.
    Talepler zincirsizdir → eşiği koruyan TEK katman `_assert_approver_level`dir."""
    sonuc = {}
    for ad, fiyat in (("alti", "100000.00"), ("ustu", "900000.00")):
        talep = await talep_fabrikasi(
            gorunen_proje,
            status=PurchaseRequestStatus.pending_approval,
            lines=[("1.000", fiyat)],
        )
        yanit = await client.post(f"{_YOL}/{talep.id}/approve", headers=headers)
        await seeded_db.refresh(talep)
        sonuc[ad] = (yanit.status_code, talep.status, yanit.json().get("detail"))
    return sonuc


@pytest.mark.asyncio
async def test_onay_esigi_sunucu_ici_maskeden_etkilenmez(
    client, satinalma_headers, seeded_db, gorunen_proje, talep_fabrikasi, pm_headers
):
    """`maliyet_kar` GİZLİ rolde eşik ALTI/ÜSTÜ iki talep, gizlemesiz rolle BİREBİR aynı davranır.

    Karşılaştırma maskesiz tutarla yapılır (maske yalnız rota sarmalayıcıda). Eşik maskelenmiş
    (`null`) tutarla kıyaslansaydı üst talep "bilinmeyen/0" sayılıp geçebilir ya da alt talep
    reddedilebilirdi; ikisi de durum/yanıt farkı olarak burada görünür. Tek koddan ibaret kontrol,
    zayıf kalırdı: durum geçişi ve ret gerekçesi de eşit olmalı."""
    acik = await _esik_davranisi(client, pm_headers, gorunen_proje, talep_fabrikasi, seeded_db)
    # Beklenen davranışın kendisi (kıyas boş kümede de yeşil kalmasın): alt GEÇER, üst REDDEDİLİR.
    assert acik["alti"][0] == 200, acik["alti"]
    assert acik["alti"][1] != PurchaseRequestStatus.pending_approval
    assert acik["ustu"][0] == 403, acik["ustu"]
    assert acik["ustu"][1] == PurchaseRequestStatus.pending_approval

    await rol_gizle(seeded_db, "project_manager", HiddenCategory.maliyet_kar)
    gizli = await _esik_davranisi(client, pm_headers, gorunen_proje, talep_fabrikasi, seeded_db)
    assert gizli == acik

    # Eşik üstünü onaylayabilen rol (full) maske olmadan da, maskeyle de AYNI sonucu alır.
    await rol_gizle(seeded_db, "project_manager")
    tam = await _esik_davranisi(
        client, satinalma_headers, gorunen_proje, talep_fabrikasi, seeded_db
    )
    assert tam["ustu"][0] == 200, tam["ustu"]

"""SA T3 — teklif alt-kaynağı + `select-and-order` (spec §3, §4; mockup TEK).

## Alt-kaynak kanonu

Teklif TALEBİN ALTINDA yaşar (`/purchase-requests/{id}/quotes/{quote_id}`) ve
yol ÇAPRAZI 404'tür: başka bir talebin teklifi bu talep için YOKTUR. Ayrı bir
`/quotes/{id}` kökü AÇILMADI — açılsaydı teklif, talebin kapsam süzgecinden
bağımsız bir giriş kapısı kazanırdı.

## `select-and-order` ATOMİKTİR

Tek işlem üç şey yapar: teklifi işaretler · siparişi üretir · talebi `ordered`
yapar. Ara adımda hata çıkarsa HİÇBİRİ kalıcı olmaz — testi bunu numara
üreticisine kasıtlı hata enjekte ederek kanıtlar.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import timezone
from app.modules.procurement.models import (
    PurchaseOrder,
    PurchaseQuote,
    PurchaseRequest,
    PurchaseRequestStatus,
)

_YOL = "/purchase-requests"


def _teklif_govdesi(supplier_id, **alanlar) -> dict:
    govde = {
        "supplier_id": str(supplier_id),
        "unit_price": "1250.00",
        "delivery_time": "3 iş günü",
        "payment_terms": "days_30",
        "shipping_included": True,
    }
    govde.update(alanlar)
    return govde


@pytest.fixture
async def teklif_bekleyen_talep(gorunen_proje, talep_fabrikasi, sef_headers):
    """`quote_wait` durumunda, toplam miktarı 10 olan talep."""
    return await talep_fabrikasi(
        gorunen_proje,
        status=PurchaseRequestStatus.quote_wait,
        lines=[("4.000", "1000.00"), ("6.000", "1000.00")],
    )


# --- Yazma yalnız `quote_wait`te ---


async def test_teklif_eklenir_ve_listelenir(
    client, satinalma_headers, teklif_bekleyen_talep, tedarikci_fabrikasi
):
    tedarikci = await tedarikci_fabrikasi("Demirsan A.Ş.")

    olustur = await client.post(
        f"{_YOL}/{teklif_bekleyen_talep.id}/quotes",
        json=_teklif_govdesi(tedarikci.id),
        headers=satinalma_headers,
    )

    assert olustur.status_code == 201, olustur.text
    govde = olustur.json()
    assert govde["supplier_id"] == str(tedarikci.id)
    assert govde["supplier_name"] == "Demirsan A.Ş."
    assert govde["delivery_time"] == "3 iş günü"
    assert govde["is_selected"] is False

    liste = await client.get(f"{_YOL}/{teklif_bekleyen_talep.id}/quotes", headers=satinalma_headers)
    assert liste.status_code == 200, liste.text
    assert liste.json()["total"] == 1


@pytest.mark.parametrize(
    "durum",
    [
        PurchaseRequestStatus.draft,
        PurchaseRequestStatus.pending_approval,
        PurchaseRequestStatus.ordered,
        PurchaseRequestStatus.delivered,
        PurchaseRequestStatus.rejected,
    ],
)
async def test_teklif_yazimi_yalniz_quote_waitte_409(
    client, satinalma_headers, gorunen_proje, talep_fabrikasi, tedarikci_fabrikasi, durum
):
    """Durum engeli 409'dur, 403 DEĞİL: kullanıcının yetkisi VARDIR."""
    talep = await talep_fabrikasi(gorunen_proje, status=durum, lines=[("1.000", "10.00")])
    tedarikci = await tedarikci_fabrikasi("Beton A.Ş.")

    yanit = await client.post(
        f"{_YOL}/{talep.id}/quotes",
        json=_teklif_govdesi(tedarikci.id),
        headers=satinalma_headers,
    )

    assert yanit.status_code == 409, (durum, yanit.text)
    assert yanit.json()["detail"] == "Teklifler yalnızca teklif bekleyen talebe eklenebilir"


async def test_teklif_okuma_her_durumda_acik(
    client, satinalma_headers, gorunen_proje, talep_fabrikasi, tedarikci_fabrikasi, teklif_fabrikasi
):
    """Sipariş verilmiş talebin teklifleri OKUNABİLİR — karşılaştırma geçmişi
    silinmez; kapalı olan yalnız YAZMADIR."""
    talep = await talep_fabrikasi(
        gorunen_proje, status=PurchaseRequestStatus.ordered, lines=[("1.000", "10.00")]
    )
    await teklif_fabrikasi(talep, await tedarikci_fabrikasi("Beton A.Ş."))

    yanit = await client.get(f"{_YOL}/{talep.id}/quotes", headers=satinalma_headers)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["total"] == 1


# --- Gövde kuralları ---


async def test_nakliye_dahilken_tutar_verilemez_422(
    client, satinalma_headers, teklif_bekleyen_talep, tedarikci_fabrikasi
):
    """TEK 90 iki hâli ayırır: "Dahil" ya da "Hariç (+₺8.000)". İkisi birden
    gönderilirse hangisinin geçerli olduğu belirsizdir — biçim ihlali, 422."""
    tedarikci = await tedarikci_fabrikasi("Demirsan A.Ş.")

    yanit = await client.post(
        f"{_YOL}/{teklif_bekleyen_talep.id}/quotes",
        json=_teklif_govdesi(tedarikci.id, shipping_included=True, shipping_cost="8000.00"),
        headers=satinalma_headers,
    )

    assert yanit.status_code == 422, yanit.text


async def test_delivery_time_serbest_metindir(
    client, satinalma_headers, teklif_bekleyen_talep, tedarikci_fabrikasi
):
    """TEK 67: "Yarın sabah" gün SAYISINA zorlanmaz."""
    tedarikci = await tedarikci_fabrikasi("Demirsan A.Ş.")

    yanit = await client.post(
        f"{_YOL}/{teklif_bekleyen_talep.id}/quotes",
        json=_teklif_govdesi(tedarikci.id, delivery_time="Yarın sabah"),
        headers=satinalma_headers,
    )

    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["delivery_time"] == "Yarın sabah"


async def test_govdedeki_tedarikci_yoksa_404(client, satinalma_headers, teklif_bekleyen_talep):
    """ST §4b kanonu: gövde içi VARLIK referansı 404 (biçim ihlali değil)."""
    yanit = await client.post(
        f"{_YOL}/{teklif_bekleyen_talep.id}/quotes",
        json=_teklif_govdesi("00000000-0000-0000-0000-000000000000"),
        headers=satinalma_headers,
    )

    assert yanit.status_code == 404, yanit.text
    assert yanit.json()["detail"] == "Tedarikçi bulunamadı"


async def test_kanon_karsi_ornegi_bicim_ihlali_422dir(
    client, satinalma_headers, teklif_bekleyen_talep, tedarikci_fabrikasi
):
    """Kanonun DİĞER yarısı: aynı uçta biçim/kural ihlali 404 DEĞİL 422'dir."""
    tedarikci = await tedarikci_fabrikasi("Demirsan A.Ş.")

    yanit = await client.post(
        f"{_YOL}/{teklif_bekleyen_talep.id}/quotes",
        json=_teklif_govdesi(tedarikci.id, unit_price="-5.00"),
        headers=satinalma_headers,
    )

    assert yanit.status_code == 422, yanit.text


# --- Yol çaprazı + kapsam ---


async def test_baska_talebin_teklifi_404(
    client,
    satinalma_headers,
    admin_headers,
    gorunen_proje,
    teklif_bekleyen_talep,
    talep_fabrikasi,
    tedarikci_fabrikasi,
    teklif_fabrikasi,
):
    """Yol ÇAPRAZI: teklif var ve görünür ama BU talebin altında değil."""
    oteki = await talep_fabrikasi(
        gorunen_proje, status=PurchaseRequestStatus.quote_wait, lines=[("1.000", "10.00")]
    )
    teklif = await teklif_fabrikasi(oteki, await tedarikci_fabrikasi("Beton A.Ş."))

    for metot, kwargs, baslik in (
        ("patch", {"json": {"unit_price": "99.00"}}, satinalma_headers),
        ("delete", {}, admin_headers),  # SIL-B1: DELETE yalnız Sistem Yöneticisi
    ):
        yanit = await getattr(client, metot)(
            f"{_YOL}/{teklif_bekleyen_talep.id}/quotes/{teklif.id}",
            headers=baslik,
            **kwargs,
        )
        assert yanit.status_code == 404, (metot, yanit.text)
        assert yanit.json()["detail"] == "Teklif bulunamadı"


async def test_gorunmeyen_projenin_teklifleri_404(
    client, satinalma_headers, gorunmeyen_proje, talep_fabrikasi
):
    talep = await talep_fabrikasi(
        gorunmeyen_proje,
        status=PurchaseRequestStatus.quote_wait,
        lines=[("1.000", "10.00")],
        created_by_email="admin@satinalma.co",
    )

    yanit = await client.get(f"{_YOL}/{talep.id}/quotes", headers=satinalma_headers)

    assert yanit.status_code == 404, yanit.text
    assert yanit.json()["detail"] == "Satın alma talebi bulunamadı"


async def test_teklif_yazimi_katalog_kapisi_ister(
    client, sef_headers, teklif_bekleyen_talep, tedarikci_fabrikasi
):
    """Şef (`_REQ`) talebi açar ama TEKLİF GİRMEZ — teklif toplama satınalmanın
    işidir (tedarikçi kataloğuyla aynı kapı: `full`)."""
    tedarikci = await tedarikci_fabrikasi("Demirsan A.Ş.")

    yanit = await client.post(
        f"{_YOL}/{teklif_bekleyen_talep.id}/quotes",
        json=_teklif_govdesi(tedarikci.id),
        headers=sef_headers,
    )

    assert yanit.status_code == 403, yanit.text


# --- PATCH / DELETE ---


async def test_teklif_guncellenir_ve_silinir(
    client,
    satinalma_headers,
    admin_headers,
    seeded_db,
    teklif_bekleyen_talep,
    tedarikci_fabrikasi,
    teklif_fabrikasi,
):
    teklif = await teklif_fabrikasi(
        teklif_bekleyen_talep, await tedarikci_fabrikasi("Demirsan A.Ş.")
    )

    guncelle = await client.patch(
        f"{_YOL}/{teklif_bekleyen_talep.id}/quotes/{teklif.id}",
        json={"unit_price": "1180.00", "delivery_time": "Yarın sabah"},
        headers=satinalma_headers,
    )
    assert guncelle.status_code == 200, guncelle.text
    assert Decimal(guncelle.json()["unit_price"]) == Decimal("1180.00")
    assert guncelle.json()["delivery_time"] == "Yarın sabah"

    yasak = await client.delete(
        f"{_YOL}/{teklif_bekleyen_talep.id}/quotes/{teklif.id}", headers=satinalma_headers
    )
    assert yasak.status_code == 403, yasak.text  # SIL-B1: `full` satınalma silemez
    sil = await client.delete(
        f"{_YOL}/{teklif_bekleyen_talep.id}/quotes/{teklif.id}", headers=admin_headers
    )
    assert sil.status_code == 204, sil.text
    assert (
        await seeded_db.execute(select(PurchaseQuote).where(PurchaseQuote.id == teklif.id))
    ).scalar_one_or_none() is None


async def test_patchte_nakliye_kurali_BIRLESIK_degerlerde_kosar(
    client, satinalma_headers, teklif_bekleyen_talep, tedarikci_fabrikasi, teklif_fabrikasi
):
    """Kısmi gövde tuzağı: yalnız `shipping_cost` gönderilir, `shipping_included`
    DB'de `true`dur. Kural yalnız gövdeye baksaydı ihlal sessizce geçerdi."""
    teklif = await teklif_fabrikasi(
        teklif_bekleyen_talep,
        await tedarikci_fabrikasi("Demirsan A.Ş."),
        shipping_included=True,
    )

    yanit = await client.patch(
        f"{_YOL}/{teklif_bekleyen_talep.id}/quotes/{teklif.id}",
        json={"shipping_cost": "8000.00"},
        headers=satinalma_headers,
    )

    assert yanit.status_code == 422, yanit.text


# --- "EN İYİ FİYAT" türevi ---


async def test_en_iyi_fiyat_TOPLAM_maliyetten_hesaplanir(
    client, satinalma_headers, teklif_bekleyen_talep, tedarikci_fabrikasi, teklif_fabrikasi
):
    """🛑 Yalnız `unit_price`e bakmak YANLIŞ olurdu.

    Talebin toplam miktarı 10'dur. Ucuz görünen teklifin nakliyesi HARİÇTİR:
      * A: 1.000 × 10 = 10.000 + 8.000 nakliye = **18.000**
      * B: 1.200 × 10 = 12.000, nakliye dahil  = **12.000**
    Birim fiyata bakan bir rozet A'yı seçerdi; doğru cevap B'dir.
    """
    ucuz_gorunen = await teklif_fabrikasi(
        teklif_bekleyen_talep,
        await tedarikci_fabrikasi("Nakliyesiz A.Ş."),
        unit_price="1000.00",
        shipping_included=False,
        shipping_cost="8000.00",
    )
    gercekten_ucuz = await teklif_fabrikasi(
        teklif_bekleyen_talep,
        await tedarikci_fabrikasi("Dahilci A.Ş."),
        unit_price="1200.00",
        shipping_included=True,
    )

    yanit = await client.get(f"{_YOL}/{teklif_bekleyen_talep.id}/quotes", headers=satinalma_headers)

    assert yanit.status_code == 200, yanit.text
    kartlar = {kart["id"]: kart for kart in yanit.json()["items"]}
    assert Decimal(kartlar[str(ucuz_gorunen.id)]["total_cost"]) == Decimal("18000.00")
    assert Decimal(kartlar[str(gercekten_ucuz.id)]["total_cost"]) == Decimal("12000.00")
    assert kartlar[str(ucuz_gorunen.id)]["is_best_price"] is False
    assert kartlar[str(gercekten_ucuz.id)]["is_best_price"] is True


# --- select-and-order ---


async def test_select_and_order_atomik_uclusu_yapar(
    client,
    satinalma_headers,
    seeded_db,
    teklif_bekleyen_talep,
    tedarikci_fabrikasi,
    teklif_fabrikasi,
):
    """Tek işlemde: teklif işaretlenir · sipariş üretilir · talep `ordered` olur."""

    secilecek = await teklif_fabrikasi(
        teklif_bekleyen_talep,
        await tedarikci_fabrikasi("Demirsan A.Ş."),
        unit_price="1200.00",
        shipping_included=False,
        shipping_cost="8000.00",
    )
    rakip = await teklif_fabrikasi(
        teklif_bekleyen_talep, await tedarikci_fabrikasi("Beton A.Ş."), is_selected=True
    )

    yanit = await client.post(
        f"{_YOL}/{teklif_bekleyen_talep.id}/quotes/{secilecek.id}/select-and-order",
        headers=satinalma_headers,
    )

    assert yanit.status_code == 201, yanit.text
    govde = yanit.json()
    # 1.200 × 10 (talebin toplam miktarı) + 8.000 nakliye.
    assert Decimal(govde["total_amount"]) == Decimal("20000.00")
    assert govde["order_no"].startswith(f"SP-{timezone.today().year}-")
    assert govde["request_id"] == str(teklif_bekleyen_talep.id)
    assert govde["quote_id"] == str(secilecek.id)
    assert govde["supplier_id"] == str(secilecek.supplier_id)
    assert govde["status"] == "approved"

    await seeded_db.refresh(secilecek)
    await seeded_db.refresh(rakip)
    await seeded_db.refresh(teklif_bekleyen_talep)
    assert secilecek.is_selected is True
    assert rakip.is_selected is False, "aynı talepteki öteki teklifler işaretsizleşir"
    assert teklif_bekleyen_talep.status is PurchaseRequestStatus.ordered


async def test_select_and_order_ATOMIKLIK_ara_adim_hatasinda_hicbiri_kalmaz(
    client,
    satinalma_headers,
    seeded_db,
    teklif_bekleyen_talep,
    tedarikci_fabrikasi,
    teklif_fabrikasi,
    monkeypatch,
):
    """🛑 Kasıtlı hata enjeksiyonu: numara üreticisi patlatılır.

    Numara EN SONDA üretilir, yani teklif işareti ve rakiplerin sıfırlanması o
    ana kadar ZATEN yazılmıştır. Servis bu üçlüyü bir SAVEPOINT içinde
    koşturmasaydı işaretler kalıcı olur ve sipariş üretilmemiş bir "seçili
    teklif" ortada kalırdı — ekran o talebi bir daha asla düzeltemezdi.
    """
    from app.modules.procurement import numbering

    async def _patla(*args, **kwargs):
        raise RuntimeError("numara üretilemedi (enjekte edilmiş hata)")

    monkeypatch.setattr(numbering, "generate_order_number", _patla)

    secilecek = await teklif_fabrikasi(
        teklif_bekleyen_talep, await tedarikci_fabrikasi("Demirsan A.Ş.")
    )
    rakip = await teklif_fabrikasi(
        teklif_bekleyen_talep, await tedarikci_fabrikasi("Beton A.Ş."), is_selected=True
    )

    # Kimlikler ÖNCEDEN okunur: `expire_all()` sonrasında bir ORM özniteliğine
    # dokunmak tembel yükleme tetikler ve async oturumda `MissingGreenlet` = 500
    # verir (P11 dersi) — testin kendisi de aynı tuzağa düşebilir.
    talep_id = teklif_bekleyen_talep.id
    secilecek_id, rakip_id = secilecek.id, rakip.id

    with pytest.raises(RuntimeError):
        await client.post(
            f"{_YOL}/{talep_id}/quotes/{secilecek_id}/select-and-order",
            headers=satinalma_headers,
        )

    # `rollback()` DEĞİL `expire_all()`: testin dış transaction'ı fixture
    # verisini de taşır, geri alınsaydı talep ve teklifler de yok olurdu.
    # Amaç kimlik haritasını boşaltıp DB'nin GERÇEK hâlini yeniden okumaktır.
    seeded_db.expire_all()
    tazele = (
        await seeded_db.execute(select(PurchaseRequest).where(PurchaseRequest.id == talep_id))
    ).scalar_one()
    teklifler = {
        kayit.id: kayit
        for kayit in (
            await seeded_db.execute(
                select(PurchaseQuote).where(PurchaseQuote.request_id == talep_id)
            )
        )
        .scalars()
        .all()
    }
    assert tazele.status is PurchaseRequestStatus.quote_wait, "talep durumu değişmemeli"
    assert teklifler[secilecek_id].is_selected is False, "seçim işareti kalmamalı"
    assert teklifler[rakip_id].is_selected is True, "rakibin işareti geri gelmeli"
    assert (
        await seeded_db.execute(select(PurchaseOrder).where(PurchaseOrder.request_id == talep_id))
    ).scalar_one_or_none() is None, "sipariş üretilmemeli"


@pytest.mark.parametrize(
    "durum",
    [
        PurchaseRequestStatus.draft,
        PurchaseRequestStatus.pending_approval,
        PurchaseRequestStatus.ordered,
        PurchaseRequestStatus.rejected,
    ],
)
async def test_select_and_order_quote_wait_disinda_409(
    client,
    satinalma_headers,
    gorunen_proje,
    talep_fabrikasi,
    tedarikci_fabrikasi,
    teklif_fabrikasi,
    durum,
):
    talep = await talep_fabrikasi(gorunen_proje, status=durum, lines=[("1.000", "10.00")])
    teklif = await teklif_fabrikasi(talep, await tedarikci_fabrikasi("Beton A.Ş."))

    yanit = await client.post(
        f"{_YOL}/{talep.id}/quotes/{teklif.id}/select-and-order", headers=satinalma_headers
    )

    assert yanit.status_code == 409, (durum, yanit.text)
    assert yanit.json()["detail"] == "Satın alma talebinin durumu bu işleme uygun değil"


async def test_select_and_order_denetim_satiri_yazar(
    client,
    satinalma_headers,
    seeded_db,
    teklif_bekleyen_talep,
    tedarikci_fabrikasi,
    teklif_fabrikasi,
):
    from app.modules.audit.models import AuditLog

    teklif = await teklif_fabrikasi(
        teklif_bekleyen_talep, await tedarikci_fabrikasi("Demirsan A.Ş.")
    )
    once = len((await seeded_db.execute(select(AuditLog))).scalars().all())

    yanit = await client.post(
        f"{_YOL}/{teklif_bekleyen_talep.id}/quotes/{teklif.id}/select-and-order",
        headers=satinalma_headers,
    )

    assert yanit.status_code == 201, yanit.text
    assert len((await seeded_db.execute(select(AuditLog))).scalars().all()) == once + 1

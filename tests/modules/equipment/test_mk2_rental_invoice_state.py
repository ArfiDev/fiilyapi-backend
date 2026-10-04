"""MK-2 — kira faturası: DURUM MAKİNESİ (K5) · fark rozeti (K6) · görünürlük (K9) ·
tedarikçi eşleşmesi (K8) · liste/sayfalama/yetki.

`test_mk2_rental_invoice_api.py`nin ikinci parçası (800 satır tavanı bölmesi);
paylaşılan yardımcılar `_mk2_rental_invoice.py`dedir.

HER YASAK GEÇİŞ AYRI TESTTİR: durum makinesinde bir geçişi tek bir toplu testle
ölçmek, o geçişin hangi yönde açıldığını gizler.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.equipment.models import (
    EquipmentOwnership,
)
from app.modules.sites.models import Site

from ._mk2_para_gercek import kira_parasini_yatir
from ._mk2_rental_invoice import (
    _AY,
    _BEDEL,
    _YIL,
    _detay,
    _durum_ilerlet,
    _fatura_kur,
    _govde,
    _kayit,
    _satir,
    _tedarikci,
)

pytestmark = pytest.mark.asyncio


async def test_K6_fark_sunucu_damgasidir_ve_odemeyi_BLOKE_ETMEZ(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    ekipman_fabrikasi,
    gorunen_santiye: Site,
    kira_eslemesi,
) -> None:
    """K6 — `variance_status` SUNUCUDAN gelir; fark varken de onay/ödeme AKAR."""
    supplier = await _tedarikci(seeded_db, "Liebherr Türkiye A.Ş.")
    kiralik = await ekipman_fabrikasi(
        "Ekskavatör CAT 320",
        site=gorunen_santiye,
        ownership=EquipmentOwnership.rented,
        supplier_id=supplier.id,
        rate_amount=_BEDEL,
    )
    await _kayit(seeded_db, kiralik, hours="152", ilk_gun=1, site=gorunen_santiye)
    fatura = await _fatura_kur(client, admin_headers, supplier, invoice_amount="48640.00")
    satir = _satir(await _detay(client, admin_headers, fatura["id"]), "rented", kiralik.id)
    assert satir["variance_status"] == "unknown"

    resp = await client.patch(
        f"/equipment/rental-invoice-lines/{satir['id']}",
        json={"invoiced_hours": "158.00"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["variance_status"] == "over"
    assert Decimal(resp.json()["hours_variance"]) == Decimal("6.00")

    await _durum_ilerlet(client, admin_headers, fatura["id"], 2)
    # PARA-GERCEK (2026-09-19) — fark rozeti ödemeyi bloke ETMEZ, ama para
    # kapısı ayrı bir olgudur ve o da sağlanmalıdır.
    await kira_parasini_yatir(seeded_db, fatura["id"])
    resp = await client.post(
        f"/equipment/rental-invoices/{fatura['id']}/pay", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "paid"


# --- K5 durum makinesi (her yasak geçiş AYRI test) ---


@pytest.fixture
async def akis_faturasi(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    ekipman_fabrikasi,
    gorunen_santiye: Site,
) -> dict:
    """Tek `rented` satırlı, `draft` bir fatura + satırı."""
    supplier = await _tedarikci(seeded_db, "Liebherr Türkiye A.Ş.")
    kiralik = await ekipman_fabrikasi(
        "Tower Crane TC-48",
        site=gorunen_santiye,
        ownership=EquipmentOwnership.rented,
        supplier_id=supplier.id,
        rate_amount=_BEDEL,
    )
    await _kayit(seeded_db, kiralik, hours="100", ilk_gun=1, site=gorunen_santiye)
    fatura = await _fatura_kur(client, admin_headers, supplier, invoice_amount="30000.00")
    detay = await _detay(client, admin_headers, fatura["id"])
    return {"id": fatura["id"], "line_id": _satir(detay, "rented", kiralik.id)["id"]}


async def test_K5_ileri_zincir_ADIM_ATLAMAZ(
    client: AsyncClient,
    admin_headers: dict[str, str],
    akis_faturasi: dict,
    kira_eslemesi,
) -> None:
    """`draft → pending_verification → approved`; her `approve` TEK adımdır."""
    resp = await client.post(
        f"/equipment/rental-invoices/{akis_faturasi['id']}/approve", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending_verification"

    resp = await client.post(
        f"/equipment/rental-invoices/{akis_faturasi['id']}/approve", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    assert resp.json()["approved_at"] is not None


async def test_K5_taslak_fatura_ODENEMEZ(
    client: AsyncClient, admin_headers: dict[str, str], akis_faturasi: dict
) -> None:
    """`draft → paid` onay zincirini ATLARDI — 409."""
    resp = await client.post(
        f"/equipment/rental-invoices/{akis_faturasi['id']}/pay", headers=admin_headers
    )
    assert resp.status_code == 409, resp.text


async def test_K5_dogrulama_bekleyen_fatura_ODENEMEZ(
    client: AsyncClient, admin_headers: dict[str, str], akis_faturasi: dict
) -> None:
    resp = await client.post(
        f"/equipment/rental-invoices/{akis_faturasi['id']}/approve", headers=admin_headers
    )
    assert resp.status_code == 200
    resp = await client.post(
        f"/equipment/rental-invoices/{akis_faturasi['id']}/pay", headers=admin_headers
    )
    assert resp.status_code == 409, resp.text


async def test_K5_odenmis_fatura_IKINCI_KEZ_odenemez(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    akis_faturasi: dict,
    kira_eslemesi,
) -> None:
    """🔴 `paid` bir UÇ DAMGADIR: ikinci çağrı 409 (çift ödeme kapısı)."""
    await _durum_ilerlet(client, admin_headers, akis_faturasi["id"], 2)
    # PARA-GERCEK (2026-09-19): `paid` damgası artık gerçekleşmiş para ister.
    await kira_parasini_yatir(seeded_db, akis_faturasi["id"])
    resp = await client.post(
        f"/equipment/rental-invoices/{akis_faturasi['id']}/pay", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["paid_at"] is not None

    resp = await client.post(
        f"/equipment/rental-invoices/{akis_faturasi['id']}/pay", headers=admin_headers
    )
    assert resp.status_code == 409, resp.text


async def test_K5_onaylanmis_fatura_TEKRAR_onaylanamaz(
    client: AsyncClient,
    admin_headers: dict[str, str],
    akis_faturasi: dict,
    kira_eslemesi,
) -> None:
    """`approved`ın ileri komşusu `paid`tir ve onun KENDİ ucu vardır: `approve`
    ikinci kez basıldığında ödeme damgası VURULMAZ, 409 döner."""
    await _durum_ilerlet(client, admin_headers, akis_faturasi["id"], 2)
    resp = await client.post(
        f"/equipment/rental-invoices/{akis_faturasi['id']}/approve", headers=admin_headers
    )
    assert resp.status_code == 409, resp.text


async def test_K5_odenmis_fatura_ONAYLANAMAZ(
    client: AsyncClient,
    admin_headers: dict[str, str],
    akis_faturasi: dict,
    kira_eslemesi,
) -> None:
    await _durum_ilerlet(client, admin_headers, akis_faturasi["id"], 2)
    await client.post(
        f"/equipment/rental-invoices/{akis_faturasi['id']}/pay", headers=admin_headers
    )
    resp = await client.post(
        f"/equipment/rental-invoices/{akis_faturasi['id']}/approve", headers=admin_headers
    )
    assert resp.status_code == 409, resp.text


async def test_K5_red_onayi_DOGRULAMA_BEKLIYORA_geri_alir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    akis_faturasi: dict,
    kira_eslemesi,
) -> None:
    """🔴 Ayrı bir `rejected` durumu YOKTUR: red `approved → pending_verification`
    geri geçişidir ve fatura yeniden DÜZENLENEBİLİR hâle gelir."""
    await _durum_ilerlet(client, admin_headers, akis_faturasi["id"], 2)
    resp = await client.post(
        f"/equipment/rental-invoices/{akis_faturasi['id']}/reject", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending_verification"
    assert resp.json()["approved_at"] is None

    resp = await client.patch(
        f"/equipment/rental-invoice-lines/{akis_faturasi['line_id']}",
        json={"invoiced_hours": "101.00"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text


async def test_K5_taslak_fatura_REDDEDILEMEZ(
    client: AsyncClient, admin_headers: dict[str, str], akis_faturasi: dict
) -> None:
    """Red YALNIZ `approved`ın geri alınmasıdır; `draft`tan red anlamsızdır."""
    resp = await client.post(
        f"/equipment/rental-invoices/{akis_faturasi['id']}/reject", headers=admin_headers
    )
    assert resp.status_code == 409, resp.text


async def test_K5_odenmis_fatura_REDDEDILEMEZ(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    akis_faturasi: dict,
    kira_eslemesi,
) -> None:
    """Banka çıkışı olmuş bir kaydı geri sarmak, kayıt ile para hareketi
    arasındaki bağı koparırdı."""
    await _durum_ilerlet(client, admin_headers, akis_faturasi["id"], 2)
    await kira_parasini_yatir(seeded_db, akis_faturasi["id"])
    await client.post(
        f"/equipment/rental-invoices/{akis_faturasi['id']}/pay", headers=admin_headers
    )
    resp = await client.post(
        f"/equipment/rental-invoices/{akis_faturasi['id']}/reject", headers=admin_headers
    )
    assert resp.status_code == 409, resp.text


async def test_K5_onaylanmis_faturada_BASLIK_PATCHi_409(
    client: AsyncClient,
    admin_headers: dict[str, str],
    akis_faturasi: dict,
    kira_eslemesi,
) -> None:
    await _durum_ilerlet(client, admin_headers, akis_faturasi["id"], 2)
    resp = await client.patch(
        f"/equipment/rental-invoices/{akis_faturasi['id']}",
        json={"invoice_amount": "1.00"},
        headers=admin_headers,
    )
    assert resp.status_code == 409, resp.text


async def test_K5_onaylanmis_faturada_SATIR_PATCHi_409(
    client: AsyncClient,
    admin_headers: dict[str, str],
    akis_faturasi: dict,
    kira_eslemesi,
) -> None:
    """🔴 İK-3 S5 emsali: `approved`ta HİÇBİR ŞEY düzenlenemez — satır dahil.

    Satır kapısı ayrı ayrı testlenir çünkü başlık kapısı kapalıyken satır
    kapısının açık kalması, onaylanmış bir ödemenin tutarını sessizce
    değiştirmenin en kolay yoludur.
    """
    await _durum_ilerlet(client, admin_headers, akis_faturasi["id"], 2)
    resp = await client.patch(
        f"/equipment/rental-invoice-lines/{akis_faturasi['line_id']}",
        json={"rate_amount": "1.00"},
        headers=admin_headers,
    )
    assert resp.status_code == 409, resp.text


async def test_K5_onaylanmis_faturada_SATIR_SILME_409(
    client: AsyncClient,
    admin_headers: dict[str, str],
    akis_faturasi: dict,
    kira_eslemesi,
) -> None:
    await _durum_ilerlet(client, admin_headers, akis_faturasi["id"], 2)
    resp = await client.delete(
        f"/equipment/rental-invoice-lines/{akis_faturasi['line_id']}", headers=admin_headers
    )
    assert resp.status_code == 409, resp.text


async def test_K5_dogrulama_bekleyende_SATIR_SILME_409_ama_PATCH_serbest(
    client: AsyncClient, admin_headers: dict[str, str], akis_faturasi: dict
) -> None:
    """Satır SİLME yalnız `draft`tadır (spec §4); satır DÜZENLEME
    `pending_verification`ta da açıktır (K5) — doğrulama tam olarak budur."""
    await _durum_ilerlet(client, admin_headers, akis_faturasi["id"], 1)
    resp = await client.delete(
        f"/equipment/rental-invoice-lines/{akis_faturasi['line_id']}", headers=admin_headers
    )
    assert resp.status_code == 409, resp.text

    resp = await client.patch(
        f"/equipment/rental-invoice-lines/{akis_faturasi['line_id']}",
        json={"rate_amount": "400.00"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text


async def test_taslakta_satir_silinir(
    client: AsyncClient, admin_headers: dict[str, str], akis_faturasi: dict
) -> None:
    resp = await client.delete(
        f"/equipment/rental-invoice-lines/{akis_faturasi['line_id']}", headers=admin_headers
    )
    assert resp.status_code == 204, resp.text
    assert (await _detay(client, admin_headers, akis_faturasi["id"]))["lines"] == []


async def test_satir_PATCHi_YALNIZ_iki_alani_kabul_eder(
    client: AsyncClient, admin_headers: dict[str, str], akis_faturasi: dict
) -> None:
    """Spec §4: satır PATCH'i `rate_amount` + `invoiced_hours` DIŞINDA bir alan
    taşıyamaz — `worked_hours` gövdeden yazılabilseydi K2 snapshot'ı bir PATCH
    ile delinirdi."""
    resp = await client.patch(
        f"/equipment/rental-invoice-lines/{akis_faturasi['line_id']}",
        json={"worked_hours": "1.00"},
        headers=admin_headers,
    )
    assert resp.status_code == 422, resp.text


# --- K9 görünürlük ---


async def test_K9_gorunmeyen_santiyenin_faturasi_HER_UCTA_404(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    sef_headers: dict[str, str],
    ekipman_fabrikasi,
    gorunmeyen_santiye: Site,
) -> None:
    """🔴 K9 — hiçbir uç atlanmaz: detay, PATCH, satır uçları ve durum uçları."""
    supplier = await _tedarikci(seeded_db, "Liebherr Türkiye A.Ş.")
    kiralik = await ekipman_fabrikasi(
        "Tower Crane TC-48",
        site=gorunmeyen_santiye,
        ownership=EquipmentOwnership.rented,
        supplier_id=supplier.id,
        rate_amount=_BEDEL,
    )
    await _kayit(seeded_db, kiralik, hours="100", ilk_gun=1, site=gorunmeyen_santiye)
    fatura = await _fatura_kur(client, admin_headers, supplier, site_id=str(gorunmeyen_santiye.id))
    detay = await _detay(client, admin_headers, fatura["id"])
    line_id = _satir(detay, "rented", kiralik.id)["id"]

    fatura_id = fatura["id"]
    assert (
        await client.get(f"/equipment/rental-invoices/{fatura_id}", headers=sef_headers)
    ).status_code == 404
    assert (
        await client.patch(
            f"/equipment/rental-invoices/{fatura_id}",
            json={"invoice_no": "X"},
            headers=sef_headers,
        )
    ).status_code == 404
    for eylem in ("reload", "approve", "pay", "reject"):
        resp = await client.post(
            f"/equipment/rental-invoices/{fatura_id}/{eylem}", headers=sef_headers
        )
        assert resp.status_code == 404, f"{eylem}: {resp.text}"
    assert (
        await client.patch(
            f"/equipment/rental-invoice-lines/{line_id}",
            json={"rate_amount": "1.00"},
            headers=sef_headers,
        )
    ).status_code == 404
    # SIL-B1: DELETE kapısı (yalnız Sistem Yöneticisi) önce koşar → 403; varlık yine sızmaz.
    assert (
        await client.delete(f"/equipment/rental-invoice-lines/{line_id}", headers=sef_headers)
    ).status_code == 403

    liste = await client.get("/equipment/rental-invoices", headers=sef_headers)
    assert liste.status_code == 200, liste.text
    assert liste.json()["total"] == 0


async def test_K9_santiyesiz_fatura_HERKESE_gorunur(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    sef_headers: dict[str, str],
) -> None:
    """K9 — `site_id IS NULL` ("Tüm Projeler") fatura kapsam süzgecine TABİ
    DEĞİLDİR: hiçbir projeye ait değildir ve gizlenseydi hiç kimse göremezdi."""
    supplier = await _tedarikci(seeded_db, "Liebherr Türkiye A.Ş.")
    fatura = await _fatura_kur(client, admin_headers, supplier)

    resp = await client.get(f"/equipment/rental-invoices/{fatura['id']}", headers=sef_headers)
    assert resp.status_code == 200, resp.text


async def test_gorunmeyen_santiyeye_fatura_ACILAMAZ(
    client: AsyncClient,
    seeded_db: AsyncSession,
    sef_headers: dict[str, str],
    gorunmeyen_santiye: Site,
) -> None:
    """Gövdedeki varlık referansı görünmüyorsa 404 (ST kanonu): aksi hâlde
    kullanıcı faturayı görmediği bir projeye taşıyıp kendinden gizlerdi."""
    supplier = await _tedarikci(seeded_db, "Liebherr Türkiye A.Ş.")
    resp = await client.post(
        "/equipment/rental-invoices",
        json=_govde(supplier, site_id=str(gorunmeyen_santiye.id)),
        headers=sef_headers,
    )
    assert resp.status_code == 404, resp.text


async def test_olmayan_tedarikci_404(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/equipment/rental-invoices",
        json={
            "supplier_id": str(uuid.uuid4()),
            "period_year": _YIL,
            "period_month": _AY,
            "rate_period": "hourly",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 404, resp.text


# --- K8 tedarikçi eşleşmesi (422) ---


async def test_K8_tedarikci_degistirmek_kiralik_satirlarla_celisirse_422(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    ekipman_fabrikasi,
    gorunen_santiye: Site,
) -> None:
    """🔴 K8 — `rented` satırların ekipmanı faturanın tedarikçisiyle EŞLEŞMELİDİR.

    İhlal 422'dir (404 değil: tedarikçi vardır ve görünür; 409 da değil: engel
    kaydın DURUMU değil GÖVDEDEKİ düzeltilebilir alan değeridir).
    """
    bizim = await _tedarikci(seeded_db, "Liebherr Türkiye A.Ş.")
    baskasi = await _tedarikci(seeded_db, "CAT Finans")
    kiralik = await ekipman_fabrikasi(
        "Tower Crane TC-48",
        site=gorunen_santiye,
        ownership=EquipmentOwnership.rented,
        supplier_id=bizim.id,
        rate_amount=_BEDEL,
    )
    await _kayit(seeded_db, kiralik, hours="100", ilk_gun=1, site=gorunen_santiye)
    fatura = await _fatura_kur(client, admin_headers, bizim)

    resp = await client.patch(
        f"/equipment/rental-invoices/{fatura['id']}",
        json={"supplier_id": str(baskasi.id)},
        headers=admin_headers,
    )
    assert resp.status_code == 422, resp.text


async def test_K8_kiralik_satiri_olmayan_faturada_tedarikci_degisir(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    ekipman_fabrikasi,
    gorunen_santiye: Site,
) -> None:
    """`owned` satırlarda tedarikçi ARANMAZ (K8) — kendi makinemizin kirası yok."""
    bizim = await _tedarikci(seeded_db, "Liebherr Türkiye A.Ş.")
    baskasi = await _tedarikci(seeded_db, "CAT Finans")
    kendi = await ekipman_fabrikasi(
        "Damperli Kamyon",
        site=gorunen_santiye,
        ownership=EquipmentOwnership.owned,
        purchase_amount=Decimal("500000.00"),
        rate_amount=Decimal("100.00"),
    )
    await _kayit(seeded_db, kendi, hours="50", ilk_gun=1, site=gorunen_santiye)
    fatura = await _fatura_kur(client, admin_headers, bizim)

    resp = await client.patch(
        f"/equipment/rental-invoices/{fatura['id']}",
        json={"supplier_id": str(baskasi.id)},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text


# --- Liste + sayfalama + tekillik + yetki ---


async def test_liste_sayfalamasi_total_ve_tavan(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers: dict[str, str]
) -> None:
    """TB3 kanonu: `total` SÜZÜLMÜŞ kümeyi sayar, `limit ≤ 200`."""
    supplier = await _tedarikci(seeded_db, "Liebherr Türkiye A.Ş.")
    for ay in (5, 6, 7):
        await _fatura_kur(client, admin_headers, supplier, period_month=ay)

    resp = await client.get("/equipment/rental-invoices?limit=2", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 3
    assert len(resp.json()["items"]) == 2

    resp = await client.get(
        f"/equipment/rental-invoices?period_month=7&supplier_id={supplier.id}",
        headers=admin_headers,
    )
    assert resp.json()["total"] == 1

    assert (
        await client.get("/equipment/rental-invoices?limit=201", headers=admin_headers)
    ).status_code == 422


async def test_ayni_tedarikcide_ayni_fatura_no_409(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers: dict[str, str]
) -> None:
    """UQ `(supplier_id, invoice_no)` — aynı faturayı iki kez ödemenin YAPISAL
    engeli. `invoice_no` NULL iken taslaklar serbesttir (NULLS DISTINCT)."""
    supplier = await _tedarikci(seeded_db, "Liebherr Türkiye A.Ş.")
    await _fatura_kur(client, admin_headers, supplier, invoice_no="LT-2026-07-0184")
    await _fatura_kur(client, admin_headers, supplier)
    await _fatura_kur(client, admin_headers, supplier)

    resp = await client.post(
        "/equipment/rental-invoices",
        json=_govde(supplier, invoice_no="LT-2026-07-0184"),
        headers=admin_headers,
    )
    assert resp.status_code == 409, resp.text


async def test_yetki_okuma_yazma_ayrimi(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    muhendis_headers: dict[str, str],
    yetkisiz_headers: dict[str, str],
) -> None:
    """Okuma `view`, yazma `full`; `equipment=_N` taşıyan rol okumada bile 403."""
    supplier = await _tedarikci(seeded_db, "Liebherr Türkiye A.Ş.")
    fatura = await _fatura_kur(client, admin_headers, supplier)

    assert (
        await client.get("/equipment/rental-invoices", headers=muhendis_headers)
    ).status_code == 200
    assert (
        await client.post(
            "/equipment/rental-invoices", json=_govde(supplier), headers=muhendis_headers
        )
    ).status_code == 403
    assert (
        await client.post(
            f"/equipment/rental-invoices/{fatura['id']}/approve", headers=muhendis_headers
        )
    ).status_code == 403
    assert (
        await client.get("/equipment/rental-invoices", headers=yetkisiz_headers)
    ).status_code == 403


async def test_rota_sirasi_rental_invoices_UUID_SANILMAZ(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """🔴 BEKÇİ: `/equipment/rental-invoices` `/equipment/{equipment_id}`den ÖNCE
    tanımlanmalıdır; sonra kalsaydı FastAPI onu bir UUID sanıp 422'ye düşürürdü.
    """
    resp = await client.get("/equipment/rental-invoices", headers=admin_headers)
    assert resp.status_code == 200, resp.text

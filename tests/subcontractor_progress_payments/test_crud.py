"""T2 — taşeron hakedişi çekirdek CRUD (spec §2, §6; plan T2).

Kapsam: oluşturma (kalemler sözleşmeden otomatik), liste + filtreler + sayfalama,
detay, PATCH (yalnız draft), DELETE (draft + `can_delete`).
Kapsam DIŞI (T3/T4): `PUT …/lines`, kota, hesap, durum geçişleri, `summary`.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.subcontractor_progress_payments import guards
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
)
from tests._silme_yardimci import sil_aile
from tests.subcontractor_progress_payments.conftest import GRUP_ADI

pytestmark = pytest.mark.asyncio


async def _olustur(client: AsyncClient, headers: dict[str, str], contract_id, **govde):
    return await client.post(
        f"/subcontractor-contracts/{contract_id}/progress-payments",
        json=govde,
        headers=headers,
    )


# --- Oluşturma (O66: kalemler sözleşmeden OTOMATİK yüklenir) ---


async def test_olusturma_kalemleri_sozlesmeden_otomatik_yukler(
    client: AsyncClient, admin_headers: dict[str, str], taseron_sozlesmesi
) -> None:
    contract, _, _ = taseron_sozlesmesi
    yanit = await _olustur(client, admin_headers, contract.id)
    assert yanit.status_code == 201, yanit.text
    govde = yanit.json()
    assert govde["sequence_no"] == 1
    assert govde["status"] == "draft"
    assert len(govde["lines"]) == 2

    satir = govde["lines"][0]
    kalem = sorted(contract.items, key=lambda item: item.sort_order)[0]
    assert satir["code"] == kalem.code
    assert satir["description"] == kalem.description
    assert satir["unit"] == kalem.unit
    assert Decimal(satir["contract_unit_price"]) == kalem.unit_price
    # `source_contract_item_id -> employer_contract_groups` zincirinden snapshot.
    assert satir["group_name"] == GRUP_ADI
    assert Decimal(satir["quantity"]) == Decimal("0")
    assert Decimal(satir["coefficient"]) == Decimal("1.000")
    assert satir["quantity_source"] == "manual"
    assert satir["contract_item_id"] == str(kalem.id)


async def test_snapshot_yuzdeler_sozlesmeden_kopyalanir(
    client: AsyncClient, admin_headers: dict[str, str], taseron_sozlesmesi
) -> None:
    """Spec §2/§8 S1: `vat_pct` (yeni kolon) + `advance_pct` + `retainage_pct`."""
    contract, _, _ = taseron_sozlesmesi
    govde = (await _olustur(client, admin_headers, contract.id)).json()
    assert Decimal(govde["vat_pct"]) == Decimal("20")
    assert Decimal(govde["advance_pct"]) == Decimal("10")
    assert Decimal(govde["retainage_pct"]) == Decimal("5")


async def test_fiyatsiz_kalem_varsa_422(
    client: AsyncClient, admin_headers: dict[str, str], fiyatsiz_sozlesme
) -> None:
    """ "Girilmedi ≠ 0 TL" (spec §2): `unit_price IS NULL` kalemi hakedişe alınamaz."""
    yanit = await _olustur(client, admin_headers, fiyatsiz_sozlesme.id)
    assert yanit.status_code == 422, yanit.text
    assert yanit.json()["detail"] == guards.ITEM_PRICE_REQUIRED


async def test_fiyatsiz_kalem_reddi_kismi_yazma_birakmaz(
    client: AsyncClient,
    admin_headers: dict[str, str],
    fiyatsiz_sozlesme,
    seeded_db: AsyncSession,
) -> None:
    assert (await _olustur(client, admin_headers, fiyatsiz_sozlesme.id)).status_code == 422
    kayitlar = (
        (
            await seeded_db.execute(
                select(SubcontractorProgressPayment).where(
                    SubcontractorProgressPayment.contract_id == fiyatsiz_sozlesme.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert kayitlar == []


async def test_sequence_no_sozlesme_kapsamli(
    client: AsyncClient,
    admin_headers: dict[str, str],
    taseron_sozlesmesi,
    taseron_sozlesmesi_fabrikasi,
    seeded_db: AsyncSession,
) -> None:
    """Mockup #47/#48: sayaç SÖZLEŞME içidir (işverendeki proje içi sayacın karşılığı).

    Aynı sözleşmede ikinci hakediş 2 alır; BAŞKA sözleşme yeniden 1'den başlar.
    """
    contract, project, _ = taseron_sozlesmesi
    ilk = await _olustur(client, admin_headers, contract.id)
    assert ilk.json()["sequence_no"] == 1

    # Durum geçişi ucu T4'te — açık hakediş kilidini açmak için doğrudan DB.
    kayit = await seeded_db.get(SubcontractorProgressPayment, uuid.UUID(ilk.json()["id"]))
    kayit.status = SubcontractorPaymentStatus.approved
    await seeded_db.flush()

    ikinci = await _olustur(client, admin_headers, contract.id)
    assert ikinci.json()["sequence_no"] == 2

    # AYNI projede ikinci bir sözleşme: sayaç yeniden 1.
    diger, _, _ = await taseron_sozlesmesi_fabrikasi("THK-101", project=project)
    ucuncu = await _olustur(client, admin_headers, diger.id)
    assert ucuncu.status_code == 201, ucuncu.text
    assert ucuncu.json()["sequence_no"] == 1


async def test_acik_hakedis_varken_ikincisi_409(
    client: AsyncClient, admin_headers: dict[str, str], taseron_sozlesmesi
) -> None:
    contract, _, _ = taseron_sozlesmesi
    assert (await _olustur(client, admin_headers, contract.id)).status_code == 201
    ikinci = await _olustur(client, admin_headers, contract.id)
    assert ikinci.status_code == 409, ikinci.text
    assert ikinci.json()["detail"] == guards.OPEN_PAYMENT_EXISTS


async def test_olusturmada_donem_ve_bolum_kaydedilir(
    client: AsyncClient, admin_headers: dict[str, str], taseron_sozlesmesi, bolum
) -> None:
    """O58 "Bölüm" seçici — bilgi alanı (spec §8 S2), NULL = "Tüm Bölümler"."""
    contract, _, _ = taseron_sozlesmesi
    yanit = await _olustur(
        client,
        admin_headers,
        contract.id,
        period_year=2026,
        period_month=8,
        description="Ağustos hakedişi",
        section_id=str(bolum.id),
    )
    assert yanit.status_code == 201, yanit.text
    govde = yanit.json()
    assert govde["period_year"] == 2026
    assert govde["period_month"] == 8
    assert govde["description"] == "Ağustos hakedişi"
    assert govde["section_id"] == str(bolum.id)


async def test_bolumsuz_olusturma_tum_bolumler(
    client: AsyncClient, admin_headers: dict[str, str], taseron_sozlesmesi
) -> None:
    contract, _, _ = taseron_sozlesmesi
    govde = (await _olustur(client, admin_headers, contract.id)).json()
    assert govde["section_id"] is None


async def test_baska_santiyenin_bolumu_422(
    client: AsyncClient, admin_headers: dict[str, str], taseron_sozlesmesi, yabanci_bolum
) -> None:
    contract, _, _ = taseron_sozlesmesi
    yanit = await _olustur(client, admin_headers, contract.id, section_id=str(yabanci_bolum.id))
    assert yanit.status_code == 422, yanit.text
    assert yanit.json()["detail"] == guards.SECTION_MISMATCH


async def test_olmayan_bolum_422(
    client: AsyncClient, admin_headers: dict[str, str], taseron_sozlesmesi
) -> None:
    contract, _, _ = taseron_sozlesmesi
    yanit = await _olustur(client, admin_headers, contract.id, section_id=str(uuid.uuid4()))
    assert yanit.status_code == 422, yanit.text
    assert yanit.json()["detail"] == guards.SECTION_MISMATCH


async def test_olusturma_denetim_gunlugune_yazar(
    client: AsyncClient, admin_headers: dict[str, str], taseron_sozlesmesi
) -> None:
    contract, _, _ = taseron_sozlesmesi
    assert (await _olustur(client, admin_headers, contract.id)).status_code == 201
    gunluk = await client.get("/audit-log", headers=admin_headers)
    assert gunluk.status_code == 200, gunluk.text
    detaylar = [item["detail"] for item in gunluk.json()["items"]]
    assert any("Taşeron hakedişi oluşturuldu" in detay for detay in detaylar)


# --- Liste (L83-101 filtreleri + sayfalama) ---


async def test_liste_filtreleri_ve_sayfalama(
    client: AsyncClient,
    admin_headers: dict[str, str],
    taseron_sozlesmesi,
    taseron_sozlesmesi_fabrikasi,
    admin_kullanicisi,
    hakedis_fabrikasi,
) -> None:
    contract, project, _ = taseron_sozlesmesi
    diger, _, _ = await taseron_sozlesmesi_fabrikasi("THK-201", subcontractor_name="Beta Yapı A.Ş.")

    await hakedis_fabrikasi(
        contract, admin_kullanicisi, sequence_no=1, period_year=2026, period_month=7
    )
    await hakedis_fabrikasi(
        diger,
        admin_kullanicisi,
        sequence_no=1,
        status=SubcontractorPaymentStatus.approved,
        period_year=2026,
        period_month=8,
    )

    hepsi = await client.get("/subcontractor-progress-payments", headers=admin_headers)
    assert hepsi.status_code == 200, hepsi.text
    assert hepsi.json()["total"] == 2
    assert hepsi.json()["limit"] == 50
    assert hepsi.json()["offset"] == 0

    proje_suzgeci = await client.get(
        "/subcontractor-progress-payments",
        params={"project_id": str(project.id)},
        headers=admin_headers,
    )
    assert [item["contract_id"] for item in proje_suzgeci.json()["items"]] == [str(contract.id)]

    donem = await client.get(
        "/subcontractor-progress-payments",
        params={"period_year": 2026, "period_month": 8},
        headers=admin_headers,
    )
    assert donem.json()["total"] == 1
    assert donem.json()["items"][0]["contract_id"] == str(diger.id)

    durum = await client.get(
        "/subcontractor-progress-payments", params={"status": "approved"}, headers=admin_headers
    )
    assert durum.json()["total"] == 1
    assert durum.json()["items"][0]["status"] == "approved"

    arama = await client.get(
        "/subcontractor-progress-payments", params={"q": "beta"}, headers=admin_headers
    )
    assert arama.json()["total"] == 1
    assert arama.json()["items"][0]["subcontractor_name"] == "Beta Yapı A.Ş."

    sayfa = await client.get(
        "/subcontractor-progress-payments",
        params={"limit": 1, "offset": 1},
        headers=admin_headers,
    )
    assert sayfa.json()["total"] == 2
    assert len(sayfa.json()["items"]) == 1
    assert sayfa.json()["limit"] == 1
    assert sayfa.json()["offset"] == 1


async def test_liste_satiri_proje_ve_taseron_adini_tasir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    taseron_sozlesmesi,
    admin_kullanicisi,
    hakedis_fabrikasi,
) -> None:
    contract, project, _ = taseron_sozlesmesi
    await hakedis_fabrikasi(contract, admin_kullanicisi)
    yanit = await client.get("/subcontractor-progress-payments", headers=admin_headers)
    satir = yanit.json()["items"][0]
    assert satir["project_name"] == project.name
    assert satir["subcontractor_name"] == contract.subcontractor_name
    assert satir["contract_no"] == contract.contract_no
    assert satir["sequence_no"] == 1


# --- Detay ---


async def test_detay_satirlariyla_doner(
    client: AsyncClient, admin_headers: dict[str, str], taseron_sozlesmesi
) -> None:
    contract, _, _ = taseron_sozlesmesi
    olusan = (await _olustur(client, admin_headers, contract.id)).json()
    yanit = await client.get(
        f"/subcontractor-progress-payments/{olusan['id']}", headers=admin_headers
    )
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["id"] == olusan["id"]
    assert govde["contract_id"] == str(contract.id)
    assert len(govde["lines"]) == 2
    assert [satir["sort_order"] for satir in govde["lines"]] == [0, 1]


# --- PATCH (yalnız draft) ---


async def test_patch_baslik_alanlarini_gunceller(
    client: AsyncClient, admin_headers: dict[str, str], taseron_sozlesmesi, bolum
) -> None:
    contract, _, _ = taseron_sozlesmesi
    olusan = (await _olustur(client, admin_headers, contract.id)).json()
    yanit = await client.patch(
        f"/subcontractor-progress-payments/{olusan['id']}",
        json={
            "period_year": 2026,
            "period_month": 9,
            "description": "Eylül",
            "default_coefficient": "1.250",
            "section_id": str(bolum.id),
        },
        headers=admin_headers,
    )
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["period_year"] == 2026
    assert govde["period_month"] == 9
    assert govde["description"] == "Eylül"
    assert Decimal(govde["default_coefficient"]) == Decimal("1.250")
    assert govde["section_id"] == str(bolum.id)


async def test_patch_yabanci_bolum_422(
    client: AsyncClient, admin_headers: dict[str, str], taseron_sozlesmesi, yabanci_bolum
) -> None:
    contract, _, _ = taseron_sozlesmesi
    olusan = (await _olustur(client, admin_headers, contract.id)).json()
    yanit = await client.patch(
        f"/subcontractor-progress-payments/{olusan['id']}",
        json={"section_id": str(yabanci_bolum.id)},
        headers=admin_headers,
    )
    assert yanit.status_code == 422
    assert yanit.json()["detail"] == guards.SECTION_MISMATCH


async def test_draft_disinda_patch_409(
    client: AsyncClient,
    admin_headers: dict[str, str],
    taseron_sozlesmesi,
    admin_kullanicisi,
    hakedis_fabrikasi,
) -> None:
    contract, _, _ = taseron_sozlesmesi
    payment = await hakedis_fabrikasi(
        contract, admin_kullanicisi, status=SubcontractorPaymentStatus.approved
    )
    yanit = await client.patch(
        f"/subcontractor-progress-payments/{payment.id}",
        json={"description": "x"},
        headers=admin_headers,
    )
    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == guards.INVALID_STATUS_TRANSITION


# --- DELETE (draft + `can_delete`) ---


async def test_taslak_silinir(
    client: AsyncClient, admin_headers: dict[str, str], taseron_sozlesmesi, seeded_db: AsyncSession
) -> None:
    contract, _, _ = taseron_sozlesmesi
    olusan = (await _olustur(client, admin_headers, contract.id)).json()
    yanit = await sil_aile(client, admin_headers, "subcontractor_progress_payment", olusan["id"])
    assert yanit.status_code == 204, yanit.text
    assert await seeded_db.get(SubcontractorProgressPayment, uuid.UUID(olusan["id"])) is None


async def test_onayli_hakedis_da_silinir_sisyon_her_kosulda(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    taseron_sozlesmesi,
    admin_kullanicisi,
    hakedis_fabrikasi,
) -> None:
    """SIL-B2: `approved`/`paid` hakediş de Sistem Yöneticisi tarafından silinir (K2)."""
    contract, _, _ = taseron_sozlesmesi
    payment = await hakedis_fabrikasi(
        contract, admin_kullanicisi, status=SubcontractorPaymentStatus.approved
    )
    payment_id = payment.id
    yanit = await sil_aile(client, admin_headers, "subcontractor_progress_payment", payment_id)
    assert yanit.status_code == 204, yanit.text
    assert await seeded_db.get(SubcontractorProgressPayment, payment_id) is None


async def test_sef_baskasinin_taslagini_silemez_403(
    client: AsyncClient,
    sef_headers: dict[str, str],
    kisitli_proje,
    taseron_sozlesmesi_fabrikasi,
    admin_kullanicisi,
    hakedis_fabrikasi,
) -> None:
    """SIL-B1 (K4): silme YALNIZ Sistem Yöneticisi'nindir; başkasının taslağı 403."""
    contract, _, _ = await taseron_sozlesmesi_fabrikasi("THK-301", project=kisitli_proje)
    payment = await hakedis_fabrikasi(contract, admin_kullanicisi)
    yanit = await client.delete(
        f"/subcontractor-progress-payments/{payment.id}", headers=sef_headers
    )
    assert yanit.status_code == 403, yanit.text
    assert yanit.json()["detail"] == "Bu işlemi yalnızca Sistem Yöneticisi yapabilir"


async def test_sef_kendi_taslagini_da_silemez_istisna_yok(
    client: AsyncClient,
    sef_headers: dict[str, str],
    kisitli_proje,
    taseron_sozlesmesi_fabrikasi,
    sef_kullanicisi,
    hakedis_fabrikasi,
) -> None:
    contract, _, _ = await taseron_sozlesmesi_fabrikasi("THK-302", project=kisitli_proje)
    payment = await hakedis_fabrikasi(contract, sef_kullanicisi)
    yanit = await client.delete(
        f"/subcontractor-progress-payments/{payment.id}", headers=sef_headers
    )
    # SIL-B1 (K4): eski "kendi taslağını sahibi siler" istisnası KALDIRILDI.
    assert yanit.status_code == 403, yanit.text


async def test_silme_denetim_gunlugune_yazar(
    client: AsyncClient, admin_headers: dict[str, str], taseron_sozlesmesi
) -> None:
    contract, _, _ = taseron_sozlesmesi
    olusan = (await _olustur(client, admin_headers, contract.id)).json()
    assert (
        await sil_aile(client, admin_headers, "subcontractor_progress_payment", olusan["id"])
    ).status_code == 204
    gunluk = await client.get("/audit-log", headers=admin_headers)
    detaylar = [item["detail"] for item in gunluk.json()["items"]]
    assert any("Taşeron hakedişi silindi" in detay for detay in detaylar)

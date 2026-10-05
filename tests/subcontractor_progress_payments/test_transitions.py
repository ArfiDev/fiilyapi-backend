"""T4 — taşeron hakedişi durum makinesi (spec §5; plan T4).

İşveren `tests/progress_payments/test_transitions.py` deseninin taşeron
karşılığıdır. Aynı geçiş tablosu, aynı beş uç; **İKİ FARK** bilinçlidir ve
burada doğrudan doğrulanır:

1. **`reject` gerekçesi ZORUNLUDUR** ve `rejected_at`/`rejection_reason`
   kolonlarına DAMGALANIR (işverende gövde opsiyoneldir ve kolon yoktur):
   L177 "Revize Gerekli" rozetinin kaynağı budur.
2. **`is_revision_required` TÜREVDİR** (`draft AND rejected_at IS NOT NULL`),
   beşinci bir durum DEĞİLDİR — ve yeniden `submit`te damga TEMİZLENİR.

Üçüncü kural ailesi işverenle aynıdır ve karıştırılmaz: onay anındaki kota
bekçisi (spec §4) — taslakta sığan bir hakediş, araya giren BAŞKA bir onay
yüzünden onay anında aşabilir; o an 422 döner ve onay GERÇEKLEŞMEZ.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditLog
from app.modules.contracts.models import SubcontractorContract, SubcontractorContractItem
from app.modules.progress_payments import guards as employer_guards
from app.modules.subcontractor_progress_payments import guards
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
    SubcontractorProgressPaymentLine,
)
from app.modules.users.models import User
from tests._para_gercek import parayi_yatir
from tests.modules.approvals.conftest import rol_sahipleri_dolgusu  # noqa: F401  (fixture)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("rol_sahipleri_dolgusu")]

DURUMLAR = [durum.value for durum in SubcontractorPaymentStatus]
UCLAR = ["submit", "approve", "reject", "mark-paid", "unapprove"]

# Spec §5 tablosu — BU BEŞ ÇİFT geçerlidir, başka hiçbir çift değildir.
GECERLI: dict[tuple[str, str], str] = {
    ("draft", "submit"): "pending_approval",
    ("pending_approval", "approve"): "approved",
    ("pending_approval", "reject"): "draft",
    ("approved", "mark-paid"): "paid",
    ("approved", "unapprove"): "pending_approval",
}
TUM_CIFTLER = [(durum, uc) for durum in DURUMLAR for uc in UCLAR]
GECERSIZ_CIFTLER = [cift for cift in TUM_CIFTLER if cift not in GECERLI]

GEREKCE = {"reason": "Metrajlar eksik, revize edin"}


def _kalemler(contract: SubcontractorContract) -> list[SubcontractorContractItem]:
    return sorted(contract.items, key=lambda item: item.sort_order)


async def _satirli_hakedis(
    session: AsyncSession,
    hakedis_fabrikasi,
    contract: SubcontractorContract,
    creator: User,
    *,
    sequence_no: int = 1,
    status: SubcontractorPaymentStatus = SubcontractorPaymentStatus.draft,
    miktarlar: list[Decimal] | None = None,
    period_year: int | None = 2026,
    period_month: int | None = 7,
) -> SubcontractorProgressPayment:
    """Durum geçişinin öznesi: dönemi dolu, en az bir miktarı olan hakediş."""
    payment = await hakedis_fabrikasi(
        contract,
        creator,
        sequence_no=sequence_no,
        status=status,
        period_year=period_year,
        period_month=period_month,
    )
    kalemler = _kalemler(contract)
    quantities = miktarlar if miktarlar is not None else [Decimal("10")] * len(kalemler)
    for sort_order, (item, quantity) in enumerate(zip(kalemler, quantities, strict=False)):
        session.add(
            SubcontractorProgressPaymentLine(
                payment_id=payment.id,
                contract_item_id=item.id,
                code=item.code,
                description=item.description,
                unit=item.unit,
                contract_unit_price=item.unit_price,
                coefficient=Decimal("1.000"),
                quantity=quantity,
                sort_order=sort_order,
            )
        )
    await session.flush()
    await session.refresh(payment)
    return payment


async def _oku(session: AsyncSession, payment_id: uuid.UUID) -> SubcontractorProgressPayment:
    payment = await session.get(SubcontractorProgressPayment, payment_id)
    await session.refresh(payment)
    return payment


# --- 1. Geçiş tablosu (spec §5) ---


@pytest.mark.parametrize("durum,uc", GECERSIZ_CIFTLER)
async def test_gecersiz_gecis_409(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
    durum: str,
    uc: str,
) -> None:
    """Tanımsız HER çift 409 `INVALID_STATUS_TRANSITION` — `paid → unapprove`
    dahil (ödenmiş hakedişin geri dönüşü YOKTUR).

    Aktör `system_admin`: kapı TÜM uçlarda açıktır, bu yüzden 409'un kaynağı
    yetki değil YALNIZ geçiş tablosudur.
    """
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus(durum),
    )
    yanit = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/{uc}",
        json=GEREKCE,
        headers=admin_headers,
    )
    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == guards.INVALID_STATUS_TRANSITION


@pytest.mark.parametrize("cift,hedef", list(GECERLI.items()))
async def test_gecerli_gecis_hedef_duruma_goturur(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
    cift: tuple[str, str],
    hedef: str,
) -> None:
    durum, uc = cift
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus(durum),
    )
    if uc == "mark-paid":
        # 🔴 PARA-GERCEK: `paid` artık arkasında GERÇEKLEŞMİŞ para ister.
        await parayi_yatir(seeded_db, payment.id, taseron=True)
    yanit = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/{uc}",
        json=GEREKCE,
        headers=admin_headers,
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == hedef


# --- 2. Damgalar (spec §5) ---


async def test_submit_damgasi(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    yanit = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/submit", headers=admin_headers
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["submitted_at"] is not None


async def test_approve_damgasi_ve_unapprove_temizligi(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus.pending_approval,
    )
    onay = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/approve", headers=admin_headers
    )
    assert onay.status_code == 200, onay.text
    assert onay.json()["approved_at"] is not None
    assert onay.json()["approved_by"] == str(admin_kullanicisi.id)

    geri = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/unapprove", headers=admin_headers
    )
    assert geri.status_code == 200, geri.text
    assert geri.json()["approved_at"] is None
    assert geri.json()["approved_by"] is None


async def test_mark_paid_damgasi(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus.approved,
    )
    await parayi_yatir(seeded_db, payment.id, taseron=True)
    yanit = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/mark-paid", headers=admin_headers
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["paid_at"] is not None


# --- 3. Ret + "Revize Gerekli" türevi (spec §5) ---


async def test_reject_draft_a_dondurur_ve_damgalar(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """Ret BEŞİNCİ durum değildir: kayıt `draft`a döner, damga rozetin kaynağıdır."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus.pending_approval,
    )
    yanit = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/reject",
        json=GEREKCE,
        headers=admin_headers,
    )
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["status"] == "draft"
    assert govde["rejected_at"] is not None
    assert govde["rejection_reason"] == GEREKCE["reason"]
    assert govde["is_revision_required"] is True


@pytest.mark.parametrize("govde", [{}, {"reason": ""}, {"reason": "   "}])
async def test_reject_gerekcesi_zorunlu_422(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
    govde: dict,
) -> None:
    """Boş/whitespace gerekçe 422 — ret KALICI bir damgadır, gerekçesiz bırakılmaz."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus.pending_approval,
    )
    yanit = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/reject",
        json=govde,
        headers=admin_headers,
    )
    assert yanit.status_code == 422, yanit.text
    assert (await _oku(seeded_db, payment.id)).status == (
        SubcontractorPaymentStatus.pending_approval
    )


async def test_yeniden_submit_revize_damgasini_temizler(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus.pending_approval,
    )
    ret = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/reject",
        json=GEREKCE,
        headers=admin_headers,
    )
    assert ret.status_code == 200, ret.text

    yeniden = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/submit", headers=admin_headers
    )
    assert yeniden.status_code == 200, yeniden.text
    govde = yeniden.json()
    assert govde["status"] == "pending_approval"
    assert govde["rejected_at"] is None
    assert govde["rejection_reason"] is None
    assert govde["is_revision_required"] is False


async def test_is_revision_required_liste_ucunda_da_doner(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """L177 rozeti LİSTE ekranındadır — türev orada da okunabilmelidir."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus.pending_approval,
    )
    await client.post(
        f"/subcontractor-progress-payments/{payment.id}/reject",
        json=GEREKCE,
        headers=admin_headers,
    )
    liste = await client.get(
        "/subcontractor-progress-payments",
        params={"project_id": str(contract.project_id)},
        headers=admin_headers,
    )
    assert liste.status_code == 200, liste.text
    satir = next(i for i in liste.json()["items"] if i["id"] == str(payment.id))
    assert satir["is_revision_required"] is True


async def test_onaylanmis_hakedis_revize_gerekli_degildir(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """Türev `draft` şartını da taşır: damgalı ama onaylanmış kayıt rozet ALMAZ."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus.pending_approval,
    )
    # Kimlik EN BAŞTA alınır: aradaki HTTP çağrıları ORM nesnesini expire eder ve
    # `payment.id` sonrasında senkron bir tembel yükleme (`MissingGreenlet`) tetikler.
    payment_id = payment.id
    await client.post(
        f"/subcontractor-progress-payments/{payment_id}/reject",
        json=GEREKCE,
        headers=admin_headers,
    )
    await client.post(
        f"/subcontractor-progress-payments/{payment_id}/submit", headers=admin_headers
    )
    # Damgayı elle geri koy: onaylı kayıtta rozet çıkmamalı.
    kayit = await seeded_db.get(SubcontractorProgressPayment, payment_id)
    await seeded_db.refresh(kayit)
    kayit.status = SubcontractorPaymentStatus.approved
    kayit.rejected_at = datetime.now(UTC)
    kayit.rejection_reason = "Eski ret"
    await seeded_db.flush()
    # `updated_at` server `onupdate` ile yenilendiği için expire olur; testin
    # paylaşılan session'ında açık refresh olmadan uç `MissingGreenlet` verir.
    await seeded_db.refresh(kayit)

    detay = await client.get(
        f"/subcontractor-progress-payments/{payment_id}", headers=admin_headers
    )
    assert detay.status_code == 200, detay.text
    assert detay.json()["is_revision_required"] is False


# --- 4. Onaya gönderme zorunlulukları (spec §5) ---


async def test_donemsiz_hakedis_onaya_gonderilemez_422(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        period_year=None,
        period_month=None,
    )
    yanit = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/submit", headers=admin_headers
    )
    assert yanit.status_code == 422, yanit.text
    assert yanit.json()["detail"] == employer_guards.PERIOD_REQUIRED


async def test_sifir_miktarli_hakedis_onaya_gonderilemez_422(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        miktarlar=[Decimal("0"), Decimal("0")],
    )
    yanit = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/submit", headers=admin_headers
    )
    assert yanit.status_code == 422, yanit.text
    assert yanit.json()["detail"] == employer_guards.LINES_REQUIRED


# --- 5. Onay anındaki kota bekçisi (spec §4) ---


async def test_onayda_kota_yeniden_dogrulanir_422(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """Taslakta SIĞAN hakediş, araya giren BAŞKA bir onay yüzünden onay anında AŞAR.

    Kalem miktarı 200. İki hakediş de 120 taşır: yazma anında ikisi de geçer
    (kümülatif henüz 0'dır), ama ikisi birden onaylanırsa 240 > 200 olur.
    İkinci onay 422 döner ve durum DEĞİŞMEZ.
    """
    contract, _, _ = taseron_sozlesmesi
    ilk = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        sequence_no=1,
        status=SubcontractorPaymentStatus.pending_approval,
        miktarlar=[Decimal("120"), Decimal("0")],
    )
    ikinci = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        sequence_no=2,
        status=SubcontractorPaymentStatus.pending_approval,
        miktarlar=[Decimal("120"), Decimal("0")],
    )

    birinci_onay = await client.post(
        f"/subcontractor-progress-payments/{ilk.id}/approve", headers=admin_headers
    )
    assert birinci_onay.status_code == 200, birinci_onay.text

    ikinci_onay = await client.post(
        f"/subcontractor-progress-payments/{ikinci.id}/approve", headers=admin_headers
    )
    assert ikinci_onay.status_code == 422, ikinci_onay.text
    assert "sözleşme miktarını aşamaz" in ikinci_onay.json()["detail"]
    assert (await _oku(seeded_db, ikinci.id)).status == (
        SubcontractorPaymentStatus.pending_approval
    )


async def test_onayda_KAYNAK_kalem_tavani_da_yeniden_dogrulanir_422(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    ikiz_sozlesme_fabrikasi,
    hakedis_fabrikasi,
) -> None:
    """🔴 ÇİFT SAYIM (TH-PRJGENEL): aynı işveren kalemine bağlı İKİ sözleşme.

    Kaynak (işveren) kalem 200 birimdir. Birinci sözleşme 150, ikinci sözleşme
    60 taşır; İKİSİ DE kendi sözleşme kalem miktarının (200) altında olduğu için
    YAZMA anında ikisi de geçer. Tavan yalnız yazma yolunda olsaydı iki taslak
    önce yazılıp sonra ikisi de onaylanarak 210 birim ödenirdi. İkinci ONAY 422
    döner ve durum DEĞİŞMEZ.
    """
    contract, _, _ = taseron_sozlesmesi
    kalem = _kalemler(contract)[0]
    ilk = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        sequence_no=1,
        status=SubcontractorPaymentStatus.pending_approval,
        miktarlar=[Decimal("150"), Decimal("0")],
    )
    ikiz = await ikiz_sozlesme_fabrikasi(contract, kalem.source_contract_item_id)
    ikinci = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        ikiz,
        admin_kullanicisi,
        sequence_no=1,
        status=SubcontractorPaymentStatus.pending_approval,
        miktarlar=[Decimal("60")],
    )

    birinci_onay = await client.post(
        f"/subcontractor-progress-payments/{ilk.id}/approve", headers=admin_headers
    )
    assert birinci_onay.status_code == 200, birinci_onay.text

    ikinci_onay = await client.post(
        f"/subcontractor-progress-payments/{ikinci.id}/approve", headers=admin_headers
    )
    assert ikinci_onay.status_code == 422, ikinci_onay.text
    detay = ikinci_onay.json()["detail"]
    assert "işveren sözleşmesindeki miktarı aşamaz" in detay
    assert contract.contract_no not in detay, "karşı sözleşmenin kimliği sızmamalı"
    assert (await _oku(seeded_db, ikinci.id)).status == (
        SubcontractorPaymentStatus.pending_approval
    )


async def test_onayda_kota_kendi_miktarini_iki_kez_saymaz(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """`unapprove` + yeniden `approve` aynı miktarı iki kez saymamalıdır."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus.pending_approval,
        miktarlar=[Decimal("200"), Decimal("0")],
    )
    ilk = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/approve", headers=admin_headers
    )
    assert ilk.status_code == 200, ilk.text
    geri = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/unapprove", headers=admin_headers
    )
    assert geri.status_code == 200, geri.text
    yeniden = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/approve", headers=admin_headers
    )
    assert yeniden.status_code == 200, yeniden.text


async def test_bagi_kopmus_satir_onayi_engellemez(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """Kalemi silinmiş satırın kotası da yoktur; onayı engellemek evrağı kilitlerdi."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus.pending_approval,
    )
    seeded_db.add(
        SubcontractorProgressPaymentLine(
            payment_id=payment.id,
            contract_item_id=None,
            code="SILINMIS",
            description="Kalemi silinmiş satır",
            unit="Ton",
            contract_unit_price=Decimal("100"),
            coefficient=Decimal("1.000"),
            quantity=Decimal("5"),
            sort_order=9,
        )
    )
    await seeded_db.flush()

    yanit = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/approve", headers=admin_headers
    )
    assert yanit.status_code == 200, yanit.text


# --- 6. İzin kapıları + kapsam (spec §6, §9.0) ---


@pytest.mark.parametrize("uc", UCLAR)
async def test_izinsiz_rol_403(
    client: AsyncClient,
    seeded_db: AsyncSession,
    hr_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
    uc: str,
) -> None:
    """`hr_manager` matriste `progress_payments=_N` — kapı GÖRÜNÜRLÜKTEN ÖNCE."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    yanit = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/{uc}",
        json=GEREKCE,
        headers=hr_headers,
    )
    assert yanit.status_code == 403, yanit.text


@pytest.mark.parametrize("uc", [uc for uc in UCLAR if uc != "unapprove"])
async def test_kapsam_disi_hakedis_404(
    client: AsyncClient,
    kisitli_headers: dict[str, str],
    gorunmeyen_hakedis: uuid.UUID,
    uc: str,
) -> None:
    """Görünmeyen kayıt ile var olmayan kayıt AYIRT EDİLEMEZ 404 (spec §9.0).

    `unapprove` DIŞARIDA: kapısı `_ADMIN`dir ve KAPI GÖRÜNÜRLÜKTEN ÖNCE çalışır,
    `project_manager` (`_APR`) o uçta kapsamdan bağımsız 403 alır — sıra
    bilinçlidir (`test_unapprove_yalniz_admin` onu ayrıca doğrular).
    """
    yanit = await client.post(
        f"/subcontractor-progress-payments/{gorunmeyen_hakedis}/{uc}",
        json=GEREKCE,
        headers=kisitli_headers,
    )
    assert yanit.status_code == 404, yanit.text
    assert yanit.json()["detail"] == guards.PAYMENT_MISSING


async def test_unapprove_yalniz_admin(
    client: AsyncClient,
    seeded_db: AsyncSession,
    kisitli_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi_fabrikasi,
    hakedis_fabrikasi,
    kisitli_proje,
) -> None:
    """`project_manager` (`_APR`) onayı GERİ ÇEKEMEZ — kapı `_ADMIN` (işveren §7)."""
    contract, _, _ = await taseron_sozlesmesi_fabrikasi("THK-U01", project=kisitli_proje)
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus.approved,
    )
    yanit = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/unapprove", headers=kisitli_headers
    )
    assert yanit.status_code == 403, yanit.text


async def test_taslak_seviyesi_onaylayamaz(
    client: AsyncClient,
    seeded_db: AsyncSession,
    sef_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi_fabrikasi,
    hakedis_fabrikasi,
    kisitli_proje,
) -> None:
    """`site_chief` (`_DRF`) onaya GÖNDEREBİLİR ama ONAYLAYAMAZ (§5 asgari seviye)."""
    contract, _, _ = await taseron_sozlesmesi_fabrikasi("THK-U02", project=kisitli_proje)
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus.pending_approval,
    )
    yanit = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/approve", headers=sef_headers
    )
    assert yanit.status_code == 403, yanit.text


# --- 7. Denetim günlüğü (spec §6) ---


@pytest.mark.parametrize(
    "durum,uc,beklenen",
    [
        ("draft", "submit", "Taşeron hakedişi onaya gönderildi"),
        ("pending_approval", "approve", "Taşeron hakedişi onaylandı"),
        ("pending_approval", "reject", "Taşeron hakedişi reddedildi"),
        ("approved", "mark-paid", "Taşeron hakedişi ödendi olarak işaretlendi"),
        ("approved", "unapprove", "Taşeron hakediş onayı geri çekildi"),
    ],
)
async def test_denetim_kaydi_yazilir(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
    durum: str,
    uc: str,
    beklenen: str,
) -> None:
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(
        seeded_db,
        hakedis_fabrikasi,
        contract,
        admin_kullanicisi,
        status=SubcontractorPaymentStatus(durum),
    )
    if uc == "mark-paid":
        # 🔴 PARA-GERCEK: `paid` artık arkasında GERÇEKLEŞMİŞ para ister.
        await parayi_yatir(seeded_db, payment.id, taseron=True)
    yanit = await client.post(
        f"/subcontractor-progress-payments/{payment.id}/{uc}",
        json=GEREKCE,
        headers=admin_headers,
    )
    assert yanit.status_code == 200, yanit.text

    kayitlar = (await seeded_db.execute(select(AuditLog))).scalars().all()
    assert any(beklenen in kayit.detail for kayit in kayitlar), [k.detail for k in kayitlar]

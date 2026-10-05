"""OK-1A T1/T2 — zincir KURULUMU: tanım · eşik sınırı · fail-closed · snapshot.

Zincir TANIMI KODDADIR (`approvals/definitions.py`), ÖRNEĞİ veritabanında.
Mockup kanıtı: `projedesign/Onay Kutusu.dc.html:120-144` (taşeron hakedişi) ·
`:150-178` (satınalma talebi) · `:210-240` (işveren hakedişi) · `:60-66` (eşik).

🔴 Bu dosya zincirin İKİ çarpanını da (tutar + eşik) donmuş kabul eder —
MK-2 N-çarpanlı snapshot kanonu: adım listesi İKİSİNİN türevidir.
"""

from decimal import Decimal

import pytest

from app.core.errors import ConflictError
from app.modules.approvals import definitions, guards, service
from app.modules.approvals.models import ApprovalDocumentType, ApprovalRole
from tests.modules.approvals.conftest import adim_rolleri

_TASERON = ApprovalDocumentType.subcontractor_progress_payment
_SATINALMA = ApprovalDocumentType.purchase_request
_ISVEREN = ApprovalDocumentType.progress_payment


async def _zincir(
    seeded_db, evrak_fabrikasi, yaratan, *, document_type=_TASERON, amount=Decimal("100.00")
):
    """GERÇEK evrak + zincir (IZN-B3b: adım sahipliği belgenin PROJESİNDEN çözülür)."""
    document_id, _ = await evrak_fabrikasi(document_type, creator=yaratan)
    return await service.create_chain(
        seeded_db,
        document_type=document_type,
        document_id=document_id,
        amount=amount,
        created_by_user_id=yaratan.id,
    )


# --- Tanım (mockup'tan okunur, seçilmez) ---


async def test_uc_evragin_zincir_tanimi_MOCKUPTAN(seeded_db, aktor_fabrikasi, evrak_fabrikasi):
    """Üç evrağın eşik ALTI zinciri — Patron adımı YOK, son adım Muhasebe."""
    yaratan = await aktor_fabrikasi("yaratan-tanim@ok1a.co")

    beklenen = {
        _TASERON: [
            ApprovalRole.site_chief,
            ApprovalRole.project_manager,
            ApprovalRole.accounting,
        ],
        _SATINALMA: [
            ApprovalRole.procurement,
            ApprovalRole.project_manager,
            ApprovalRole.accounting,
        ],
        _ISVEREN: [ApprovalRole.accounting],
    }
    for tip, roller in beklenen.items():
        zincir = await _zincir(
            seeded_db, evrak_fabrikasi, yaratan, document_type=tip, amount=Decimal("1.00")
        )
        olculen = await adim_rolleri(seeded_db, zincir.id)
        assert olculen == roller, tip
        assert ApprovalRole.patron not in olculen, tip
        assert olculen[-1] is ApprovalRole.accounting, tip


async def test_esik_ustunde_PATRON_adimi_SONA_eklenir(seeded_db, aktor_fabrikasi, evrak_fabrikasi):
    yaratan = await aktor_fabrikasi("yaratan-patron@ok1a.co")

    zincir = await _zincir(seeded_db, evrak_fabrikasi, yaratan, amount=Decimal("1250000.00"))

    roller = await adim_rolleri(seeded_db, zincir.id)
    assert roller == [
        ApprovalRole.site_chief,
        ApprovalRole.project_manager,
        ApprovalRole.accounting,
        ApprovalRole.patron,
    ]


# --- 🔴 ÜÇ NOKTALI SINIR (sözleşme Y1/R4: `amount >= threshold`) ---


@pytest.mark.parametrize(
    ("tutar", "patron_var"),
    [
        # Sınır ALTI — tek kuruş aşağısı Patron İSTEMEZ.
        ("499999.99", False),
        # 🔴 SINIR GÜNÜ: tam ₺500.000 ÜSTE düşer (`>=`). MU-2'de bu kör bekçi
        # olarak yakalanmıştı; mevcut kod (`procurement/transitions.py`
        # `if total < THRESHOLD ... return`) da bu yönü uyguluyor.
        ("500000.00", True),
        ("500000.01", True),
    ],
)
async def test_esik_SINIRI_uc_nokta(seeded_db, aktor_fabrikasi, tutar, patron_var, evrak_fabrikasi):
    yaratan = await aktor_fabrikasi(f"sinir-{tutar}@ok1a.co")

    zincir = await _zincir(seeded_db, evrak_fabrikasi, yaratan, amount=Decimal(tutar))

    roller = await adim_rolleri(seeded_db, zincir.id)
    assert (ApprovalRole.patron in roller) is patron_var, (tutar, roller)


# --- 🔴 NULL-EŞİK / FAIL-CLOSED (SA kanonu) ---


async def test_tutar_BELIRLENEMEZSE_esigin_USTU_sayilir(
    seeded_db, aktor_fabrikasi, evrak_fabrikasi
):
    """Bilinmeyen BÜYÜK sayılır: fiyatsız kalem / satırsız hakediş / NULL bedel.

    Küçük sayılsaydı ₺2M'lik bir evrak tek alan boş bırakılarak Patron adımını
    ATLARDI (SA'da bu yol fiilen bulunmuştu).
    """
    yaratan = await aktor_fabrikasi("belirlenemez@ok1a.co")

    zincir = await _zincir(seeded_db, evrak_fabrikasi, yaratan, amount=None)

    assert ApprovalRole.patron in await adim_rolleri(seeded_db, zincir.id)
    # 🔴 `amount_snapshot` NULL KALIR: 0 yazılsaydı "eksik veri" ile "sıfır
    # tutar" denetim yüzeyinde ayırt EDİLEMEZDİ (SA kanonunun ta kendisi).
    assert zincir.amount_snapshot is None


# --- 🔴 SNAPSHOT: açık zincir DEĞİŞMEZ, yeni evrak YENİ eşikle kurulur ---


async def test_ESIK_SNAPSHOTI_acik_zinciri_DEGISTIRMEZ_yeni_evrak_YENI_esikle(
    seeded_db, aktor_fabrikasi, evrak_fabrikasi
):
    """İKİ çarpan da donar (MK-2 kanonu) ve ikisi AYNI testte kanıtlanır."""
    yaratan = await aktor_fabrikasi("snapshot@ok1a.co")
    tutar = Decimal("400000.00")

    eski = await _zincir(seeded_db, evrak_fabrikasi, yaratan, amount=tutar)
    assert ApprovalRole.patron not in await adim_rolleri(seeded_db, eski.id)
    assert eski.threshold_snapshot == definitions.DEFAULT_APPROVAL_THRESHOLD_TRY
    assert eski.amount_snapshot == tutar

    # Ayar DÜŞÜRÜLÜR: aynı tutar artık eşiğin ÜSTÜNDE.
    await service.set_threshold(seeded_db, Decimal("300000.00"))

    # (a) AÇIK zincir hiç değişmez — adım listesi de, İKİ snapshot da.
    await seeded_db.refresh(eski)
    assert ApprovalRole.patron not in await adim_rolleri(seeded_db, eski.id)
    assert eski.threshold_snapshot == Decimal("500000.00")
    assert eski.amount_snapshot == tutar

    # (b) YENİ evrak YENİ eşikle kurulur.
    yeni = await _zincir(seeded_db, evrak_fabrikasi, yaratan, amount=tutar)
    assert ApprovalRole.patron in await adim_rolleri(seeded_db, yeni.id)
    assert yeni.threshold_snapshot == Decimal("300000.00")


async def test_ayni_evraga_IKINCI_acik_zincir_kurulamaz(
    seeded_db, aktor_fabrikasi, evrak_fabrikasi
):
    """`UNIQUE(document_type, document_id)` — bir evrağın EN FAZLA BİR açık zinciri."""
    yaratan = await aktor_fabrikasi("cift-zincir@ok1a.co")
    document_id, _ = await evrak_fabrikasi(_TASERON, creator=yaratan)

    await service.create_chain(
        seeded_db,
        document_type=_TASERON,
        document_id=document_id,
        amount=Decimal("10.00"),
        created_by_user_id=yaratan.id,
    )

    with pytest.raises(ConflictError) as hata:
        await service.create_chain(
            seeded_db,
            document_type=_TASERON,
            document_id=document_id,
            amount=Decimal("10.00"),
            created_by_user_id=yaratan.id,
        )
    assert str(hata.value) == guards.CHAIN_ALREADY_EXISTS


async def test_adim_numaralari_BIRDEN_baslar_ve_bosluksuzdur(
    seeded_db, aktor_fabrikasi, evrak_fabrikasi
):
    from sqlalchemy import select

    from app.modules.approvals.models import ApprovalStep

    yaratan = await aktor_fabrikasi("adim-no@ok1a.co")
    zincir = await _zincir(seeded_db, evrak_fabrikasi, yaratan, amount=Decimal("900000.00"))

    numaralar = [
        satir.step_no
        for satir in (
            await seeded_db.execute(
                select(ApprovalStep)
                .where(ApprovalStep.chain_id == zincir.id)
                .order_by(ApprovalStep.step_no)
            )
        ).scalars()
    ]
    assert numaralar == [1, 2, 3, 4]

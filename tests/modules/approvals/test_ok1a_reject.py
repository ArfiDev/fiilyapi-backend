"""OK-1A T1/T2 — RET (K2 · sözleşme Y3) · OKT-B1 ile GÜNCELLENDİ.

Ret zinciri BİTİRİR ama artık SİLMEZ (KARARLAR 62ae58a, eski K2'nin üstüne yazar):
zincir `rejected_*` ile damgalanır, adımlar DURUR. Ret kaydının asıl bekçileri
`test_okt_b1_ret_kaydi.py`dedir. Evrağın DURUMU bu dilimin işi DEĞİLDİR (T3);
burada ölçülen şey motorun kendisidir.

Gerekçe ZORUNLU metindir; tavan `core/text.py::FREE_TEXT_MAX_LENGTH` — alanın
TÜM giriş noktaları aynı sabitten okur (BC dersi).
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.errors import ApprovalNotAllowedError, ApprovalValidationError
from app.core.text import FREE_TEXT_MAX_LENGTH
from app.modules.approvals import guards, service
from app.modules.approvals.models import (
    ApprovalDocumentType,
    ApprovalRole,
    ApprovalStep,
)
from tests.modules.approvals.conftest import adim_rolleri, zincir_getir

_TASERON = ApprovalDocumentType.subcontractor_progress_payment


async def _zincir_kur(seeded_db, yaratan, document_id=None, *, amount=Decimal("100.00")):
    document_id = document_id or uuid.uuid4()
    await service.create_chain(
        seeded_db,
        document_type=_TASERON,
        document_id=document_id,
        amount=amount,
        created_by_user_id=yaratan.id,
    )
    return document_id


async def _adim_sayisi(seeded_db, chain_id) -> int:
    return await seeded_db.scalar(
        select(func.count()).select_from(ApprovalStep).where(ApprovalStep.chain_id == chain_id)
    )


async def test_RET_zinciri_SILMEZ_damgalar_adimlar_DURUR(seeded_db, aktor_fabrikasi):
    """Ret 2. adımda verilir: zincir ve adımlar (1. adımın ONAYI dahil) DURUR."""
    yaratan = await aktor_fabrikasi("ret-yaratan@ok1a.co")
    sef = await aktor_fabrikasi(
        "ret-sef@ok1a.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    pm = await aktor_fabrikasi(
        "ret-pm@ok1a.co",
        role_key="project_manager",
        approval_roles=[ApprovalRole.project_manager],
    )
    document_id = await _zincir_kur(seeded_db, yaratan)
    zincir = await zincir_getir(seeded_db, _TASERON, document_id)
    chain_id = zincir.id

    await service.approve_next_step(
        seeded_db, actor=sef, document_type=_TASERON, document_id=document_id
    )
    assert await _adim_sayisi(seeded_db, chain_id) == 3

    sonuc = await service.reject_chain(
        seeded_db,
        actor=pm,
        document_type=_TASERON,
        document_id=document_id,
        reason="Metraj sayfası eksik",
    )

    assert sonuc.step_no == 2
    assert sonuc.approval_role is ApprovalRole.project_manager
    # Zincir artık "açık" değildir ...
    assert await service.open_chain(seeded_db, _TASERON, document_id) is None
    # ... ama kayıt olarak DURUR; adımlar da (1. adımın onayı dahil).
    zincir = await zincir_getir(seeded_db, _TASERON, document_id)
    assert zincir is not None and zincir.id == chain_id
    assert zincir.rejected_by_user_id == pm.id
    assert zincir.rejected_at is not None
    assert zincir.rejection_reason == "Metraj sayfası eksik"
    assert await _adim_sayisi(seeded_db, chain_id) == 3


@pytest.mark.parametrize("gerekce", ["", "   ", "\n\t "])
async def test_GEREKCESIZ_ret_422(seeded_db, aktor_fabrikasi, gerekce):
    """Boş VE yalnız-boşluk gerekçe reddedilir; zincir AYAKTA kalır."""
    yaratan = await aktor_fabrikasi(f"gerekcesiz-{len(gerekce)}@ok1a.co")
    sef = await aktor_fabrikasi(
        f"gerekcesiz-sef-{len(gerekce)}@ok1a.co",
        role_key="site_chief",
        approval_roles=[ApprovalRole.site_chief],
    )
    document_id = await _zincir_kur(seeded_db, yaratan)

    with pytest.raises(ApprovalValidationError) as hata:
        await service.reject_chain(
            seeded_db,
            actor=sef,
            document_type=_TASERON,
            document_id=document_id,
            reason=gerekce,
        )

    assert str(hata.value) == guards.REJECT_REASON_REQUIRED
    assert await zincir_getir(seeded_db, _TASERON, document_id) is not None


async def test_gerekce_TAVANI_paylasilan_sabittendir(seeded_db, aktor_fabrikasi):
    """Tavan `FREE_TEXT_MAX_LENGTH`tir — modüle ayrı bir sayı YAZILMAZ."""
    yaratan = await aktor_fabrikasi("tavan-yaratan@ok1a.co")
    sef = await aktor_fabrikasi(
        "tavan-sef@ok1a.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    document_id = await _zincir_kur(seeded_db, yaratan)

    with pytest.raises(ApprovalValidationError) as hata:
        await service.reject_chain(
            seeded_db,
            actor=sef,
            document_type=_TASERON,
            document_id=document_id,
            reason="x" * (FREE_TEXT_MAX_LENGTH + 1),
        )

    assert str(hata.value) == guards.REJECT_REASON_TOO_LONG
    assert await zincir_getir(seeded_db, _TASERON, document_id) is not None


async def test_ret_bekcileri_ONAYLA_AYNIDIR_kendi_evragini_reddedemez(seeded_db, aktor_fabrikasi):
    """Ret de bir KARARDIR: bekçi 4/5/6 onaydakiyle aynı huniden geçer.

    Ayrı bırakılsaydı evrağın sahibi kendi evrağını reddederek zinciri
    silebilir ve onay izini yok edebilirdi.
    """
    yaratan = await aktor_fabrikasi(
        "ret-kendi@ok1a.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    document_id = await _zincir_kur(seeded_db, yaratan)

    with pytest.raises(ApprovalNotAllowedError) as hata:
        await service.reject_chain(
            seeded_db,
            actor=yaratan,
            document_type=_TASERON,
            document_id=document_id,
            reason="Kendi evrakım",
        )

    assert str(hata.value) == guards.OWN_DOCUMENT
    assert await zincir_getir(seeded_db, _TASERON, document_id) is not None


async def test_retten_sonra_YENIDEN_gonderim_ADIM_1den_YENI_esikle_baslar(
    seeded_db, aktor_fabrikasi
):
    """🔴 K2 + K3 birlikte: yeni zincir adım 1'den kurulur ve YENİ eşiği donar."""
    yaratan = await aktor_fabrikasi("yeniden-yaratan@ok1a.co")
    sef = await aktor_fabrikasi(
        "yeniden-sef@ok1a.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    document_id = await _zincir_kur(seeded_db, yaratan, amount=Decimal("400000.00"))

    await service.reject_chain(
        seeded_db,
        actor=sef,
        document_type=_TASERON,
        document_id=document_id,
        reason="Fiyat farkı hesabı hatalı",
    )
    await service.set_threshold(seeded_db, Decimal("300000.00"))

    yeni = await service.create_chain(
        seeded_db,
        document_type=_TASERON,
        document_id=document_id,
        amount=Decimal("400000.00"),
        created_by_user_id=yaratan.id,
    )

    roller = await adim_rolleri(seeded_db, yeni.id)
    assert roller[0] is ApprovalRole.site_chief
    assert ApprovalRole.patron in roller
    assert yeni.threshold_snapshot == Decimal("300000.00")
    # Adım 1 SIFIRDAN: hiçbir adım karara bağlanmamış.
    adimlar = list(
        (
            await seeded_db.execute(select(ApprovalStep).where(ApprovalStep.chain_id == yeni.id))
        ).scalars()
    )
    assert all(adim.decided_at is None for adim in adimlar)

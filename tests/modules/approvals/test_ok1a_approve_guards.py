"""OK-1A T1/T2 — `approve_next_step` BEKÇİ SIRASI (sözleşme Y2).

Sıra BAĞLAYICIDIR ve testler bu sırayı iddia eder:

1. evrak satırı `FOR UPDATE`  (çağıranın işi — T3)
2. zincir satırı `FOR UPDATE` (`test_ok1a_lock.py`)
3. zincir açık mı / adım SIRADAKİ adım mı        → **409**
4. aktör adımın onay rolünü taşıyor mu            → **403**
5. 🔴 KENDİ EVRAKI (tek istisna: evrağın izin modülünde `admin`) → **403**
6. 🔴 GÖREVLER AYRILIĞI (burada admin İSTİSNASI YOKTUR)          → **403**

İzin matrisi gerçeği (`roles/seed_data.py` `progress_payments` satırı):
system_admin=**admin** · patron=**full** · accounting=approve · PM=approve.
Yani "kendi evrakı" istisnasını YALNIZ `system_admin` kullanabilir; `patron`
sistem rolü `full`dur ve `full`, `admin`i KARŞILAMAZ.
"""

import uuid
from decimal import Decimal

import pytest

from app.core.errors import ApprovalNotAllowedError, ConflictError
from app.modules.approvals import guards, service
from app.modules.approvals.models import ApprovalDocumentType, ApprovalRole
from app.modules.audit import messages

_TASERON = ApprovalDocumentType.subcontractor_progress_payment


async def _zincir_kur(seeded_db, evrak_fabrikasi, yaratan, *, amount=Decimal("100.00")):
    """GERÇEK evrak + zincir (IZN-B3b: adım sahipliği belgenin PROJESİNDEN çözülür)."""
    document_id, _ = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await service.create_chain(
        seeded_db,
        document_type=_TASERON,
        document_id=document_id,
        amount=amount,
        created_by_user_id=yaratan.id,
    )
    return document_id


async def _proje_of(seeded_db, document_id):
    from app.modules.projects.models import Project
    from app.modules.subcontractor_progress_payments.models import SubcontractorProgressPayment

    odeme = await seeded_db.get(SubcontractorProgressPayment, document_id)
    return await seeded_db.get(Project, odeme.project_id)


async def _onayla(seeded_db, aktor, document_id, *, step_no=None):
    return await service.approve_next_step(
        seeded_db,
        actor=aktor,
        document_type=_TASERON,
        document_id=document_id,
        step_no=step_no,
    )


# --- Bekçi 3: SIRA ---


async def test_IKINCI_adim_birinci_onaylanmadan_409(seeded_db, aktor_fabrikasi, evrak_fabrikasi):
    """Adımlar SIRAYLA işler. 2. adımı ELİNDE ROLÜ OLAN biri bile ilerletemez."""
    yaratan = await aktor_fabrikasi("sira-yaratan@ok1a.co")
    pm = await aktor_fabrikasi(
        "sira-pm@ok1a.co",
        role_key="project_manager",
        approval_roles=[ApprovalRole.project_manager],
    )
    document_id = await _zincir_kur(seeded_db, evrak_fabrikasi, yaratan)

    with pytest.raises(ConflictError) as hata:
        await _onayla(seeded_db, pm, document_id, step_no=2)

    assert str(hata.value) == guards.STEP_NOT_CURRENT


async def test_zincir_TAMAMLANINCA_yeni_onay_409(seeded_db, aktor_fabrikasi, evrak_fabrikasi):
    yaratan = await aktor_fabrikasi("tamam-yaratan@ok1a.co")
    sef = await aktor_fabrikasi(
        "tamam-sef@ok1a.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    pm = await aktor_fabrikasi(
        "tamam-pm@ok1a.co",
        role_key="project_manager",
        approval_roles=[ApprovalRole.project_manager],
    )
    muhasebe = await aktor_fabrikasi(
        "tamam-muh@ok1a.co", role_key="accounting", approval_roles=[ApprovalRole.accounting]
    )
    document_id = await _zincir_kur(seeded_db, evrak_fabrikasi, yaratan)

    assert (await _onayla(seeded_db, sef, document_id)).is_complete is False
    assert (await _onayla(seeded_db, pm, document_id)).is_complete is False
    son = await _onayla(seeded_db, muhasebe, document_id)
    assert son.is_complete is True
    assert son.step_no == 3

    with pytest.raises(ConflictError) as hata:
        await _onayla(seeded_db, muhasebe, document_id)
    assert str(hata.value) == guards.CHAIN_COMPLETED


async def test_zinciri_OLMAYAN_evrak_409(seeded_db, aktor_fabrikasi):
    sef = await aktor_fabrikasi(
        "zincirsiz@ok1a.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )

    with pytest.raises(ConflictError) as hata:
        await _onayla(seeded_db, sef, uuid.uuid4())

    assert str(hata.value) == guards.NO_OPEN_CHAIN


# --- Bekçi 4: ROL ---


async def test_adimin_ROLUNU_tasimayan_403(seeded_db, aktor_fabrikasi, evrak_fabrikasi):
    yaratan = await aktor_fabrikasi("rol-yaratan@ok1a.co")
    # Onay rolü VAR ama SIRADAKİ adımın rolü DEĞİL (sıradaki: site_chief).
    muhasebe = await aktor_fabrikasi(
        "rol-muh@ok1a.co", role_key="accounting", approval_roles=[ApprovalRole.accounting]
    )
    document_id = await _zincir_kur(seeded_db, evrak_fabrikasi, yaratan)

    with pytest.raises(ApprovalNotAllowedError) as hata:
        await _onayla(seeded_db, muhasebe, document_id)

    assert str(hata.value) == guards.APPROVAL_ROLE_MISSING


async def test_HIC_onay_rolu_olmayan_sistem_admini_bile_403(
    seeded_db, aktor_fabrikasi, evrak_fabrikasi
):
    """🔴 Sistem yöneticiliği onay rolü YERİNE GEÇMEZ (K1'in ayrımı).

    `admin` istisnası YALNIZ "kendi evrakı" bekçisine (5) verilmiştir; rol
    bekçisini (4) atlatmaz — atlatsaydı zincir, sistemin en yetkilisi
    tarafından tek başına tamamlanabilirdi.
    """
    yaratan = await aktor_fabrikasi("rolsuz-yaratan@ok1a.co")
    sysadmin = await aktor_fabrikasi(
        "rolsuz-admin@ok1a.co", role_key="system_admin", approval_roles=[]
    )
    document_id = await _zincir_kur(seeded_db, evrak_fabrikasi, yaratan)

    with pytest.raises(ApprovalNotAllowedError) as hata:
        await _onayla(seeded_db, sysadmin, document_id)

    assert str(hata.value) == guards.APPROVAL_ROLE_MISSING


# --- Bekçi 5: KENDİ EVRAKI (+ admin istisnası) ---


async def test_KENDI_evragini_onaylayamaz_403(seeded_db, aktor_fabrikasi, evrak_fabrikasi):
    """Aktörün onay rolü VAR, sırası da GELMİŞ — engel yalnız evrağın SAHİPLİĞİ."""
    yaratan = await aktor_fabrikasi(
        "kendi-sef@ok1a.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    document_id = await _zincir_kur(seeded_db, evrak_fabrikasi, yaratan)

    with pytest.raises(ApprovalNotAllowedError) as hata:
        await _onayla(seeded_db, yaratan, document_id)

    assert str(hata.value) == guards.OWN_DOCUMENT


async def test_patron_SISTEM_rolu_full_oldugu_icin_kendi_evragini_ONAYLAYAMAZ(
    seeded_db, aktor_fabrikasi, evrak_fabrikasi
):
    """🔴 İstisnanın sınırı: `full`, `admin`i KARŞILAMAZ (`access.satisfies`).

    Bu bekçi olmasaydı istisna sessizce `patron` sistem rolüne de açılırdı ve
    "tek kişilik ekipte kilitlenmeyi önle" gerekçesi, kendi evrağını onaylayan
    ikinci bir sınıfa dönüşürdü.
    """
    patron = await aktor_fabrikasi(
        "kendi-patron@ok1a.co", role_key="patron", approval_roles=[ApprovalRole.site_chief]
    )
    document_id = await _zincir_kur(seeded_db, evrak_fabrikasi, patron)

    with pytest.raises(ApprovalNotAllowedError) as hata:
        await _onayla(seeded_db, patron, document_id)

    assert str(hata.value) == guards.OWN_DOCUMENT


async def test_ADMIN_kendi_evragini_onaylar_ve_denetim_VEKALETEN_isaretini_tasir(
    seeded_db, aktor_fabrikasi, evrak_fabrikasi
):
    """🔴 K1'in tek istisnası + izi. İşaret MESAJ SABİTİYLE iddia edilir.

    Karşıt iddia AYNI testtedir: aynı zincirin BAŞKA bir adımını, evrağın
    sahibi OLMAYAN bir aktör onayladığında metin işareti TAŞIMAZ. Tek yönlü
    iddia, işareti her metne koyan bir hatayı göremezdi.
    """
    sysadmin = await aktor_fabrikasi(
        "vekaleten-admin@ok1a.co",
        role_key="system_admin",
        approval_roles=[ApprovalRole.site_chief],
    )
    pm = await aktor_fabrikasi(
        "vekaleten-pm@ok1a.co",
        role_key="project_manager",
        approval_roles=[ApprovalRole.project_manager],
    )
    document_id = await _zincir_kur(seeded_db, evrak_fabrikasi, sysadmin)

    kendi = await _onayla(seeded_db, sysadmin, document_id)
    assert kendi.on_behalf is True
    assert messages.APPROVAL_ON_BEHALF_MARK in kendi.audit_detail

    baskasi = await _onayla(seeded_db, pm, document_id)
    assert baskasi.on_behalf is False
    assert messages.APPROVAL_ON_BEHALF_MARK not in baskasi.audit_detail


# --- Bekçi 6: GÖREVLER AYRILIĞI (K1'in kalbi) ---


async def test_GOREVLER_AYRILIGI_rolu_degisen_kullanici_ikinci_adimda_403(
    seeded_db, aktor_fabrikasi, evrak_fabrikasi
):
    """🔴 K1'in kalbi. IZN-B3b: bir kişi bir projede TEK rol taşır; ayrılık bekçisi, zincir
    sürerken ROLÜ DEĞİŞEN kişide ısırır (şef iken 1. adımı imzaladı, sonra o projede PM oldu).
    Evrak KENDİSİNİN DEĞİL; tek engel, aynı zincirin bir adımını ZATEN karara bağlamış olması.
    """
    from tests.modules.approvals.conftest import proje_rolu_ver

    yaratan = await aktor_fabrikasi("ayrilik-yaratan@ok1a.co")
    cift_rollu = await aktor_fabrikasi(
        "ayrilik-cift@ok1a.co",
        role_key="project_manager",
        approval_roles=[ApprovalRole.site_chief],
    )
    document_id = await _zincir_kur(seeded_db, evrak_fabrikasi, yaratan)

    birinci = await _onayla(seeded_db, cift_rollu, document_id)
    assert birinci.step_no == 1
    assert birinci.approval_role is ApprovalRole.site_chief
    await proje_rolu_ver(
        seeded_db, cift_rollu, await _proje_of(seeded_db, document_id), "project_manager"
    )

    with pytest.raises(ApprovalNotAllowedError) as hata:
        await _onayla(seeded_db, cift_rollu, document_id)

    assert str(hata.value) == guards.SEPARATION_OF_DUTIES


async def test_GOREVLER_AYRILIGI_ADMINE_de_uygulanir(seeded_db, aktor_fabrikasi, evrak_fabrikasi):
    """🔴 `admin` istisnası YALNIZ bekçi 5'e verilmiştir, bekçi 6'ya DEĞİL.

    Kurulum ikisini AYNI aktörde birleştirir: `system_admin` hem evrağın
    SAHİBİDİR (bekçi 5 — istisnayla geçer) hem de 1. adımı karara bağlamıştır
    (bekçi 6 — istisna YOK). İkinci adım 403'tür ve mesaj GÖREVLER
    AYRILIĞINI söyler, "kendi evrakı"nı değil: bekçi 5 ile 6 aynı anda
    geçerliyken 5 ÖNCE ateşlenir ve orada GEÇER.
    """
    from tests.modules.approvals.conftest import proje_rolu_ver

    sysadmin = await aktor_fabrikasi(
        "ayrilik-admin@ok1a.co",
        role_key="system_admin",
        approval_roles=[ApprovalRole.site_chief],
    )
    document_id = await _zincir_kur(seeded_db, evrak_fabrikasi, sysadmin)

    birinci = await _onayla(seeded_db, sysadmin, document_id)
    assert birinci.on_behalf is True
    await proje_rolu_ver(
        seeded_db, sysadmin, await _proje_of(seeded_db, document_id), "project_manager"
    )

    with pytest.raises(ApprovalNotAllowedError) as hata:
        await _onayla(seeded_db, sysadmin, document_id)

    assert str(hata.value) == guards.SEPARATION_OF_DUTIES


async def test_onaylanan_adim_KARAR_BILGISINI_yazar(seeded_db, aktor_fabrikasi, evrak_fabrikasi):
    from sqlalchemy import select

    from app.modules.approvals.models import ApprovalChain, ApprovalStep

    yaratan = await aktor_fabrikasi("damga-yaratan@ok1a.co")
    sef = await aktor_fabrikasi(
        "damga-sef@ok1a.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    document_id = await _zincir_kur(seeded_db, evrak_fabrikasi, yaratan)

    await _onayla(seeded_db, sef, document_id)

    zincir = await seeded_db.scalar(
        select(ApprovalChain).where(ApprovalChain.document_id == document_id)
    )
    adimlar = list(
        (
            await seeded_db.execute(
                select(ApprovalStep)
                .where(ApprovalStep.chain_id == zincir.id)
                .order_by(ApprovalStep.step_no)
            )
        ).scalars()
    )
    assert adimlar[0].decided_by_user_id == sef.id
    assert adimlar[0].decided_at is not None
    assert adimlar[1].decided_by_user_id is None
    assert adimlar[1].decided_at is None

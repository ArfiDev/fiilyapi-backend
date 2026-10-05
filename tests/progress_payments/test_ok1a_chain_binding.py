"""OK-1A T3 — İŞVEREN hakedişi onay ZİNCİRİNE bağlandı.

Mockup zinciri (`projedesign/Onay Kutusu.dc.html:210-240`): **Muhasebe** —
eşik aşılırsa sona **Patron** eklenir (`:60-66`).

## Bu dosyanın üç ağır sorumluluğu

1. **`/approve`nin ANLAMI değişti, YOLU değişmedi.** Uç artık zincirin sıradaki
   adımını ilerletir; evrak ancak SON adımda `approved` olur. Ara adımda
   `pending_approval`da KALIR ve bunu ölçen test durum kodunu DEĞİL DURUMU
   iddia eder (200 dönen bir uç yanlış durumu da yazabilirdi).
2. **`/reject` KIRICI değişti** (K2, kullanıcı kararı 2026-08-21): gerekçe artık
   ZORUNLUDUR. Eski hâlinde gövde `RejectBody | None` idi ve gerekçe hiçbir
   kolona yazılmıyordu — gerekçe DEPOLAMASI değişmedi (hâlâ yalnız denetim
   günlüğü), ZORUNLULUĞU değişti.
3. **`/unapprove` ile `/reject` AYRI şeylerdir** ve farkları TEK ayrımla
   ölçülür: ret zinciri SİLER, geri alma SON ADIMI GERİ SARAR. İkisi de evrağı
   `pending_approval`/`draft`a döndürdüğü için yalnız duruma bakan bir test bu
   farkı GÖREMEZ; zincirin varlığı ayrıca iddia edilir.

🔴 Onay rolü ≠ sistem rolü: bir adımı onaylayacak aktörün İKİSİ de gerekir
(uçun izin kapısı `progress_payments ≥ approve` + adımın onay rolü). Matriste
`site_chief` `progress_payments=_DRF`tir, yani gerçek şantiye şefi bu uçtan
GEÇEMEZ — bu, zincirin değil izin matrisinin bugünkü hâlidir ve DEĞİŞTİRİLMEDİ.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.text import FREE_TEXT_MAX_LENGTH
from app.modules.approvals import guards as approval_guards
from app.modules.approvals.models import ApprovalDocumentType, ApprovalRole
from app.modules.audit import messages
from app.modules.audit.models import AuditLog
from app.modules.progress_payments.models import ProgressPayment, ProgressPaymentStatus
from app.modules.projects.models import Project
from app.modules.users.models import User
from tests.modules.approvals.conftest import (
    adim_durumlari,
    adim_rolleri,
    kullanici,
    onay_rolu_ver,
    rol_sahipleri_dolgusu,  # noqa: F401  (fixture)
    zincir_getir,
)

pytestmark = pytest.mark.usefixtures("rol_sahipleri_dolgusu")

_TIP = ApprovalDocumentType.progress_payment
_GEREKCE = {"reason": "Metrajlar eksik"}

# `gecerli_taslak`ın brütü: 100 m³ × ₺1.850 × 1,000 = ₺185.000 (conftest
# `hakedis_kalemi` + `hakedis_fabrikasi` varsayılanları).
_BRUT = Decimal("185000.00")


async def _onaycı(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    *,
    email: str,
    role_key: str,
    approval_roles: tuple[ApprovalRole, ...],
) -> dict[str, str]:
    """Sistem rolü + proje erişimi + ONAY ROLÜ taşıyan aktör kurar ve giriş yapar."""
    user = await user_factory(email=email, password="parola1234", role_key=role_key)
    user.all_projects = True
    await onay_rolu_ver(seeded_db, user, *approval_roles)
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
async def muhasebe_onaycisi(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`accounting` sistem rolü (`progress_payments=_APR`) + `accounting` onay rolü."""
    return await _onaycı(
        client,
        seeded_db,
        user_factory,
        email="zincir-muhasebe@pp-ok1a.co",
        role_key="accounting",
        approval_roles=(ApprovalRole.accounting,),
    )


@pytest.fixture
async def patron_onaycisi(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`patron` sistem rolü (`progress_payments=_F`) + `patron` onay rolü."""
    return await _onaycı(
        client,
        seeded_db,
        user_factory,
        email="zincir-patron@pp-ok1a.co",
        role_key="patron",
        approval_roles=(ApprovalRole.patron,),
    )


async def _esik(client: AsyncClient, admin_headers: dict[str, str], deger: str) -> None:
    yanit = await client.put(
        "/approvals/settings", json={"approval_threshold_try": deger}, headers=admin_headers
    )
    assert yanit.status_code == 200, yanit.text


async def _gonder(
    client: AsyncClient, headers: dict[str, str], payment_id: uuid.UUID
) -> dict[str, object]:
    yanit = await client.post(f"/progress-payments/{payment_id}/submit", headers=headers)
    assert yanit.status_code == 200, yanit.text
    return yanit.json()


async def _durum(session: AsyncSession, payment_id: uuid.UUID) -> ProgressPaymentStatus:
    payment = await session.get(ProgressPayment, payment_id)
    await session.refresh(payment)
    return payment.status


async def _denetim_metinleri(session: AsyncSession) -> list[str]:
    rows = (await session.execute(select(AuditLog.detail))).scalars()
    return list(rows)


# --------------------------------------------------------------------------- #
# 1. `submit` ZİNCİR KURAR
# --------------------------------------------------------------------------- #


async def test_submit_ZINCIRI_KURAR_esik_altinda_PATRON_YOK(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    gecerli_taslak: uuid.UUID,
) -> None:
    """Eşik altı (₺185.000 < ₺500.000): zincir TEK adımlıdır — Muhasebe."""
    await _gonder(client, admin_headers, gecerli_taslak)

    zincir = await zincir_getir(seeded_db, _TIP, gecerli_taslak)
    assert zincir is not None, "submit zincir AÇMADI"
    assert await adim_rolleri(seeded_db, zincir.id) == [ApprovalRole.accounting]
    # 🔴 İKİ ÇARPAN DA DONAR (MK-2 kanonu): tutar BRÜTtür (R5), eşik ayardandır.
    assert zincir.amount_snapshot == _BRUT
    assert zincir.threshold_snapshot == Decimal("500000.00")


async def test_submit_esik_USTUNDE_PATRON_adimi_SONA_eklenir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    gecerli_taslak: uuid.UUID,
) -> None:
    await _esik(client, admin_headers, "100000.00")

    await _gonder(client, admin_headers, gecerli_taslak)

    zincir = await zincir_getir(seeded_db, _TIP, gecerli_taslak)
    assert await adim_rolleri(seeded_db, zincir.id) == [
        ApprovalRole.accounting,
        ApprovalRole.patron,
    ]
    assert zincir.threshold_snapshot == Decimal("100000.00")


# --------------------------------------------------------------------------- #
# 2. `/approve` — ARA adım vs SON adım
# --------------------------------------------------------------------------- #


async def test_ARA_adim_evragi_PENDINGDE_birakir_SON_adim_APPROVED_yapar(
    client: AsyncClient,
    admin_headers: dict[str, str],
    muhasebe_onaycisi: dict[str, str],
    patron_onaycisi: dict[str, str],
    seeded_db: AsyncSession,
    gecerli_taslak: uuid.UUID,
) -> None:
    """🔴 T3'ün kalbi. Durum kodu DEĞİL DURUM iddia edilir."""
    await _esik(client, admin_headers, "100000.00")
    await _gonder(client, admin_headers, gecerli_taslak)

    ara = await client.post(
        f"/progress-payments/{gecerli_taslak}/approve", headers=muhasebe_onaycisi
    )
    assert ara.status_code == 200, ara.text
    assert ara.json()["status"] == "pending_approval", "ara adım evrağı ONAYLADI"
    assert ara.json()["approved_at"] is None
    zincir = await zincir_getir(seeded_db, _TIP, gecerli_taslak)
    assert await adim_durumlari(seeded_db, zincir.id) == [True, False]

    son = await client.post(f"/progress-payments/{gecerli_taslak}/approve", headers=patron_onaycisi)
    assert son.status_code == 200, son.text
    assert son.json()["status"] == "approved"
    assert son.json()["approved_at"] is not None
    assert await adim_durumlari(seeded_db, zincir.id) == [True, True]


async def test_zincir_TAMAMLANINCA_ucuncu_onay_409(
    client: AsyncClient,
    admin_headers: dict[str, str],
    muhasebe_onaycisi: dict[str, str],
    seeded_db: AsyncSession,
    gecerli_taslak: uuid.UUID,
) -> None:
    """Tek adımlı zincir bitince evrak `approved`dır; ikinci onay GEÇİŞ
    TABLOSUNDAN 409 alır (`approved → approve` tabloda yok)."""
    await _gonder(client, admin_headers, gecerli_taslak)
    ilk = await client.post(
        f"/progress-payments/{gecerli_taslak}/approve", headers=muhasebe_onaycisi
    )
    assert ilk.json()["status"] == "approved", ilk.text

    ikinci = await client.post(
        f"/progress-payments/{gecerli_taslak}/approve", headers=muhasebe_onaycisi
    )
    assert ikinci.status_code == 409, ikinci.text


# --------------------------------------------------------------------------- #
# 3. Bekçiler (sözleşme Y2) — mesaj SABİTİYLE iddia edilir
# --------------------------------------------------------------------------- #


async def test_onay_ROLU_olmayan_aktor_403(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    user_factory,
    gecerli_taslak: uuid.UUID,
) -> None:
    """Proje Müdürü uç kapısını (`progress_payments=_APR`) GEÇER ama işveren zincirinin
    (Muhasebe) adım SAHİBİ DEĞİLDİR. IZN-B3b: ana rolü `accounting` olan "Tüm projeler" kişi
    artık adımın sahibidir; bu yüzden rolsüz aktör ana rolü adım rolü olmayan biridir."""
    pm = await _onaycı(
        client,
        seeded_db,
        user_factory,
        email="rolsuz-pm@pp-ok1a.co",
        role_key="project_manager",
        approval_roles=(),
    )
    await _gonder(client, admin_headers, gecerli_taslak)

    yanit = await client.post(f"/progress-payments/{gecerli_taslak}/approve", headers=pm)

    assert yanit.status_code == 403, yanit.text
    assert yanit.json()["detail"] == approval_guards.APPROVAL_ROLE_MISSING


async def test_zincirin_SAHIBI_evragin_YARATICISIDIR_gonderen_DEGIL(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    gecerli_taslak: uuid.UUID,
) -> None:
    """Bekci 5'in oznesi `payment.created_by`dir, `submit` eden aktor DEGIL.

    Ikisi burada BILINCLI olarak FARKLIDIR (`gecerli_taslak`i `hakedis_
    olusturan` acar, `admin_headers` gonderir): "kendi evragi" yasagi evragin
    YAZARINI baglar — bir baskasi adina gondermek yasagi devretmez.
    """
    olusturan = await kullanici(seeded_db, "olusturan@progress-payments.co")
    gonderen = await kullanici(seeded_db, "admin@pp-crud.co")
    assert olusturan.id != gonderen.id

    await _gonder(client, admin_headers, gecerli_taslak)

    zincir = await zincir_getir(seeded_db, _TIP, gecerli_taslak)
    assert zincir.created_by_user_id == olusturan.id


async def test_KENDI_EVRAKI_403_ama_ADMIN_VEKALETEN_gecer(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    hakedis_fabrikasi,
) -> None:
    """İki dallı TEK kural: `accounting` yaratıcı 403, `system_admin` yaratıcı GEÇER.

    Admin dalında denetim satırı **"admin vekâleten"** işaretini TAŞIR — istisna
    izsiz kalsaydı tek kişilik ekipte kimse farkı göremezdi.
    """
    # (a) yaratıcı `accounting`: kendi evrağını onaylayamaz.
    muhasebeci = await user_factory(
        email="kendi-muhasebe@pp-ok1a.co", password="parola1234", role_key="accounting"
    )
    muhasebeci.all_projects = True
    await onay_rolu_ver(seeded_db, muhasebeci, ApprovalRole.accounting)
    giris = await client.post(
        "/auth/login", json={"email": muhasebeci.email, "password": "parola1234"}
    )
    basliklar = {"Authorization": f"Bearer {giris.json()['access_token']}"}

    kendi = await hakedis_fabrikasi(ProgressPaymentStatus.draft)
    payment = await seeded_db.get(ProgressPayment, kendi)
    payment.created_by = muhasebeci.id
    await seeded_db.flush()
    await _gonder(client, basliklar, kendi)

    red = await client.post(f"/progress-payments/{kendi}/approve", headers=basliklar)
    assert red.status_code == 403, red.text
    assert red.json()["detail"] == approval_guards.OWN_DOCUMENT

    # (b) yaratıcı `system_admin` (`progress_payments=_A`): istisna AÇIKTIR.
    yonetici = await user_factory(
        email="kendi-admin@pp-ok1a.co", password="parola1234", role_key="system_admin"
    )
    await onay_rolu_ver(seeded_db, yonetici, ApprovalRole.accounting)
    giris2 = await client.post(
        "/auth/login", json={"email": yonetici.email, "password": "parola1234"}
    )
    admin_basliklar = {"Authorization": f"Bearer {giris2.json()['access_token']}"}

    admin_hakedisi = await hakedis_fabrikasi(ProgressPaymentStatus.draft)
    payment2 = await seeded_db.get(ProgressPayment, admin_hakedisi)
    payment2.created_by = yonetici.id
    await seeded_db.flush()
    await _gonder(client, admin_basliklar, admin_hakedisi)

    gecer = await client.post(
        f"/progress-payments/{admin_hakedisi}/approve", headers=admin_basliklar
    )
    assert gecer.status_code == 200, gecer.text
    assert gecer.json()["status"] == "approved"
    metinler = await _denetim_metinleri(seeded_db)
    assert any(messages.APPROVAL_ON_BEHALF_MARK in metin for metin in metinler), metinler


async def test_GOREVLER_AYRILIGI_403_ADMIN_ISTISNASI_YOK(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    user_factory,
    gecerli_taslak: uuid.UUID,
) -> None:
    """Adımı imzaladıktan sonra projedeki ROLÜ DEĞİŞEN `system_admin` bile AYNI evrağın ikinci
    adımını onaylayamaz — K1 istisnayı YALNIZ "kendi evrakı"na verdi. (IZN-B3b: bir kişi bir
    projede TEK rol taşır; ayrılık bekçisi rol değişiminde ısırır.)"""
    from tests.modules.approvals.conftest import proje_rolu_ver

    await _esik(client, admin_headers, "100000.00")
    cift_rollu = await _onaycı(
        client,
        seeded_db,
        user_factory,
        email="cift-rol@pp-ok1a.co",
        role_key="system_admin",
        approval_roles=(ApprovalRole.accounting,),
    )
    await _gonder(client, admin_headers, gecerli_taslak)

    ilk = await client.post(f"/progress-payments/{gecerli_taslak}/approve", headers=cift_rollu)
    assert ilk.status_code == 200, ilk.text
    assert ilk.json()["status"] == "pending_approval"
    kisi = await kullanici(seeded_db, "cift-rol@pp-ok1a.co")
    odeme = await seeded_db.get(ProgressPayment, gecerli_taslak)
    proje = await seeded_db.get(Project, odeme.project_id)
    await proje_rolu_ver(seeded_db, kisi, proje, "patron")

    ikinci = await client.post(f"/progress-payments/{gecerli_taslak}/approve", headers=cift_rollu)
    assert ikinci.status_code == 403, ikinci.text
    assert ikinci.json()["detail"] == approval_guards.SEPARATION_OF_DUTIES


# --------------------------------------------------------------------------- #
# 4. `/reject` — K2 (KIRICI değişiklik)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("govde", [None, {}, {"reason": ""}, {"reason": "   "}])
async def test_reject_GEREKCESIZ_422(
    client: AsyncClient,
    admin_headers: dict[str, str],
    gecerli_taslak: uuid.UUID,
    govde: dict[str, str] | None,
) -> None:
    """🔴 KIRICI: eskiden gövde İSTEĞE BAĞLIYDI (`RejectBody | None`)."""
    await _gonder(client, admin_headers, gecerli_taslak)

    yanit = await client.post(
        f"/progress-payments/{gecerli_taslak}/reject", json=govde, headers=admin_headers
    )

    assert yanit.status_code == 422, (govde, yanit.text)


async def test_reject_TAVANI_asan_gerekce_422(
    client: AsyncClient, admin_headers: dict[str, str], gecerli_taslak: uuid.UUID
) -> None:
    await _gonder(client, admin_headers, gecerli_taslak)

    yanit = await client.post(
        f"/progress-payments/{gecerli_taslak}/reject",
        json={"reason": "x" * (FREE_TEXT_MAX_LENGTH + 1)},
        headers=admin_headers,
    )

    assert yanit.status_code == 422, yanit.text


async def test_reject_ZINCIRI_DAMGALAR_ve_yeniden_gonderim_YENI_zincir_ADIM_1DEN(
    client: AsyncClient,
    admin_headers: dict[str, str],
    muhasebe_onaycisi: dict[str, str],
    patron_onaycisi: dict[str, str],
    seeded_db: AsyncSession,
    gecerli_taslak: uuid.UUID,
) -> None:
    """K2 (OKT-B1 ile güncellendi): ret zinciri BİTİRİR ama SİLMEZ (damgalar);
    yeniden gönderim YENİ zincirle ADIM 1'den.

    Yeniden gönderilen evrak YENİ eşikle kurulur (yeni snapshot) — bu, açık
    zincirin donmasıyla ÇELİŞMEZ: donan şey AÇIK zincirdir, yenisi değil.
    """
    await _esik(client, admin_headers, "100000.00")
    await _gonder(client, admin_headers, gecerli_taslak)
    ara = await client.post(
        f"/progress-payments/{gecerli_taslak}/approve", headers=muhasebe_onaycisi
    )
    assert ara.json()["status"] == "pending_approval", ara.text

    # 🔴 Ret de bir KARARDIR ve AYNI bekçi hunisinden geçer: reddeden aktör
    # SIRADAKİ adımın rolünü taşımalıdır (burada Patron). Ayrı bırakılsaydı
    # evrağın sahibi kendi evrağını REDDEDEREK zinciri silebilir ve onay izini
    # yok edebilirdi.
    ret = await client.post(
        f"/progress-payments/{gecerli_taslak}/reject", json=_GEREKCE, headers=patron_onaycisi
    )
    assert ret.status_code == 200, ret.text
    assert ret.json()["status"] == "draft"
    eski = await zincir_getir(seeded_db, _TIP, gecerli_taslak)
    assert eski is not None and eski.rejected_at is not None, "ret zinciri DAMGALAMALI"
    assert eski.rejection_reason == _GEREKCE["reason"]

    # Eşik yükseltilir: YENİ zincir YENİ eşikle kurulur ve adım 1 boştadır.
    await _esik(client, admin_headers, "500000.00")
    await _gonder(client, admin_headers, gecerli_taslak)
    yeni = await zincir_getir(seeded_db, _TIP, gecerli_taslak)
    assert yeni.id != eski.id and yeni.rejected_at is None
    assert await adim_rolleri(seeded_db, yeni.id) == [ApprovalRole.accounting]
    assert await adim_durumlari(seeded_db, yeni.id) == [False]
    assert yeni.threshold_snapshot == Decimal("500000.00")


# --------------------------------------------------------------------------- #
# 5. `/unapprove` — RET'TEN FARKI
# --------------------------------------------------------------------------- #


async def test_unapprove_SON_ADIMI_GERI_SARAR_zincir_SILINMEZ(
    client: AsyncClient,
    admin_headers: dict[str, str],
    muhasebe_onaycisi: dict[str, str],
    seeded_db: AsyncSession,
    gecerli_taslak: uuid.UUID,
) -> None:
    """🔴 RET'TEN FARKI: zincir AYAKTA kalır, yalnız SON karar geri sarılır."""
    await _gonder(client, admin_headers, gecerli_taslak)
    onay = await client.post(
        f"/progress-payments/{gecerli_taslak}/approve", headers=muhasebe_onaycisi
    )
    assert onay.json()["status"] == "approved", onay.text
    zincir = await zincir_getir(seeded_db, _TIP, gecerli_taslak)
    assert await adim_durumlari(seeded_db, zincir.id) == [True]

    geri = await client.post(
        f"/progress-payments/{gecerli_taslak}/unapprove", headers=admin_headers
    )

    assert geri.status_code == 200, geri.text
    assert geri.json()["status"] == "pending_approval"
    hala = await zincir_getir(seeded_db, _TIP, gecerli_taslak)
    assert hala is not None, "geri alma zinciri SİLDİ — bu RET davranışıdır"
    assert await adim_durumlari(seeded_db, hala.id) == [False]

    # Geri sarılan adım yeniden onaylanabilir (aynı aktör: zincirde artık
    # KARARI YOKTUR, görevler ayrılığı ona kapı kapatmaz).
    tekrar = await client.post(
        f"/progress-payments/{gecerli_taslak}/approve", headers=muhasebe_onaycisi
    )
    assert tekrar.status_code == 200, tekrar.text
    assert tekrar.json()["status"] == "approved"


async def test_unapprove_denetim_metni_ESKI_ONAYLAYANI_ve_ADIM_ROLUNU_tasir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    muhasebe_onaycisi: dict[str, str],
    seeded_db: AsyncSession,
    gecerli_taslak: uuid.UUID,
) -> None:
    await _gonder(client, admin_headers, gecerli_taslak)
    await client.post(f"/progress-payments/{gecerli_taslak}/approve", headers=muhasebe_onaycisi)

    await client.post(f"/progress-payments/{gecerli_taslak}/unapprove", headers=admin_headers)

    metinler = await _denetim_metinleri(seeded_db)
    geri_alma = [metin for metin in metinler if "geri" in metin.lower()]
    assert geri_alma, metinler
    # ESKİ iz KORUNDU (onaylayanın adı) + ÜSTÜNE adımın rolü eklendi.
    assert any("Test Kullanıcı" in metin for metin in geri_alma), geri_alma
    assert any(messages.APPROVAL_ROLE_LABELS["accounting"] in metin for metin in geri_alma), (
        geri_alma
    )


async def test_paid_unapprove_409_DEGISMEDI(
    client: AsyncClient, admin_headers: dict[str, str], hakedis_fabrikasi
) -> None:
    """K7 bugünkü davranış: ödenmiş hakedişin geri dönüşü YOKTUR."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.paid)

    yanit = await client.post(f"/progress-payments/{payment_id}/unapprove", headers=admin_headers)

    assert yanit.status_code == 409, yanit.text


# --------------------------------------------------------------------------- #
# 6. ESKİ (zincirsiz) kayıt — DAR ve ÖLÇÜLMÜŞ geri uyumluluk yolu
# --------------------------------------------------------------------------- #


async def test_ZINCIRSIZ_eski_kayit_BUGUNKU_yolla_onaylanir(
    client: AsyncClient,
    muhasebe_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """Dağıtımdan ÖNCE `pending_approval`da kalmış kayıtların zinciri YOKTUR.

    Zincir zorunlu kılınsaydı bu kayıtlar ne onaylanabilir ne reddedilebilirdi
    (ikisi de zincirden geçiyor) — yani canlıdaki her uçuş hâlindeki evrak
    KİLİTLENİRDİ. İkinci bir migration (geri doldurma) bu dilimde AÇILMADI.
    """
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.pending_approval)
    assert await zincir_getir(seeded_db, _TIP, payment_id) is None

    yanit = await client.post(f"/progress-payments/{payment_id}/approve", headers=muhasebe_headers)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "approved"


async def test_SUBMIT_HER_ZAMAN_zincir_acar_eski_yol_URETILEMEZ(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """🔴 Yukarıdaki geri uyumluluk yolunun SINIRI: `pending_approval`a giden TEK
    uç `submit`tir ve o HER ZAMAN zincir açar. Yani zincirsiz bir evrak API ile
    ÜRETİLEMEZ — eski kayıt yolu yalnız GEÇMİŞE bakar."""
    ilk = await hakedis_fabrikasi(ProgressPaymentStatus.draft)
    ikinci = await hakedis_fabrikasi(ProgressPaymentStatus.draft)

    for payment_id in (ilk, ikinci):
        await _gonder(client, admin_headers, payment_id)
        assert await zincir_getir(seeded_db, _TIP, payment_id) is not None, payment_id


async def test_reddedilen_evrak_yeniden_gonderilince_ESKI_ZINCIR_ENGEL_DEGIL(
    client: AsyncClient,
    admin_headers: dict[str, str],
    muhasebe_onaycisi: dict[str, str],
    seeded_db: AsyncSession,
    gecerli_taslak: uuid.UUID,
) -> None:
    """`UNIQUE(document_type, document_id)` yeniden gönderimi kilitlemez: ret
    zinciri SİLDİĞİ için ikinci `submit` temiz bir zemin bulur."""
    await _gonder(client, admin_headers, gecerli_taslak)
    ret = await client.post(
        f"/progress-payments/{gecerli_taslak}/reject", json=_GEREKCE, headers=muhasebe_onaycisi
    )
    assert ret.status_code == 200, ret.text

    ikinci = await client.post(f"/progress-payments/{gecerli_taslak}/submit", headers=admin_headers)

    assert ikinci.status_code == 200, ikinci.text
    assert await _durum(seeded_db, gecerli_taslak) is ProgressPaymentStatus.pending_approval


async def test_onaycinin_kendisi_KULLANICI_kaydini_dogrular(
    seeded_db: AsyncSession, muhasebe_onaycisi: dict[str, str], gecerli_taslak: uuid.UUID
) -> None:
    """Fixture gerçekten PROJE ROLÜ yazdı mı — sahte-yeşil bekçisi (IZN-B3b: onay rolü = ekip
    rolü; kişinin her ekip satırı `accounting` rolünde olmalı ve en az bir satır bulunmalı)."""
    from sqlalchemy import select

    from app.modules.roles.models import Role
    from app.modules.users.models import ProjectMember

    user = await kullanici(seeded_db, "zincir-muhasebe@pp-ok1a.co")
    assert isinstance(user, User)
    roller = (
        (
            await seeded_db.execute(
                select(Role.key)
                .join(ProjectMember, ProjectMember.role_id == Role.id)
                .where(ProjectMember.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert roller and set(roller) == {ApprovalRole.accounting.value}


async def test_MU3D_ARA_adim_FIS_YAZMAZ_fis_SON_adimda_dogar(
    client: AsyncClient,
    admin_headers: dict[str, str],
    muhasebe_onaycisi: dict[str, str],
    patron_onaycisi: dict[str, str],
    seeded_db: AsyncSession,
    gecerli_taslak: uuid.UUID,
) -> None:
    """🔴 MU-3D — fiş, EYLEMDEN değil evrağın `approved` OLMASINDAN doğar.

    ## Bu testin GERÇEKTEN bekçilik ettiği şey

    `transitions.perform` onay zinciri tamamlanmadığında **ERKEN DÖNER**
    (`chain_step is not None and not chain_step.is_complete`) ve `_fisle`ye
    HİÇ ULAŞMAZ. O erken dönüş kaldırılırsa ara adım evrağı `approved` yapar
    VE fişini yazar — yani muhasebe imzaladığı anda, patron daha imzalamadan,
    hasılat deftere girer.

    MU-3D öncesi bu erken dönüşü ölçen testler yalnız DURUMU iddia ediyordu
    (`status == "pending_approval"`). Durum iddiası fişi görmez: fişleme başka
    bir yerden (örneğin bir router kancasından) çağrılsaydı durum yine
    `pending_approval` kalır ama defter DOLARDI. Bu yüzden burada defterin
    KENDİSİ ölçülür.

    ⚠️ `_fisle`nin içindeki `new_status is approved` denetimi bugün bu erken
    dönüşle EŞDEĞERDİR (matriste `approve` eyleminin TEK hedefi `approved`tır)
    ve mutasyonla ölçüldüğünde eşdeğer mutant verir. Bilerek duruyor: geçiş
    matrisine `approved`a varan ikinci bir eylem eklendiğinde YAPISAL olarak
    doğru kalan denetim odur. Bekçilik eden ise BU TESTTİR.
    """
    from app.modules.accounting.models import JournalEntry, JournalSourceType

    async def _fis_sayisi() -> int:
        return len(
            (
                await seeded_db.execute(
                    select(JournalEntry.id)
                    .where(JournalEntry.source_type == JournalSourceType.progress_payment)
                    .where(JournalEntry.source_id == gecerli_taslak)
                )
            )
            .scalars()
            .all()
        )

    await _esik(client, admin_headers, "100000.00")
    await _gonder(client, admin_headers, gecerli_taslak)

    ara = await client.post(
        f"/progress-payments/{gecerli_taslak}/approve", headers=muhasebe_onaycisi
    )
    assert ara.status_code == 200, ara.text
    assert ara.json()["status"] == "pending_approval"
    assert await _fis_sayisi() == 0, (
        "ARA ADIMDA FİŞ YAZILDI — patron imzalamadan hasılat deftere girdi"
    )

    son = await client.post(f"/progress-payments/{gecerli_taslak}/approve", headers=patron_onaycisi)
    assert son.status_code == 200, son.text
    assert son.json()["status"] == "approved"
    assert await _fis_sayisi() == 1, "SON adımda fiş DOĞMADI"

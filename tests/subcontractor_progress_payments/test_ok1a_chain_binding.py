"""OK-1A T3 — TAŞERON hakedişi onay ZİNCİRİNE bağlandı.

Mockup zinciri (`projedesign/Onay Kutusu.dc.html:120-144`):
**Şantiye Şefi → Proje Müdürü → Muhasebe**, eşik aşılırsa sona **Patron**
(`:60-66`).

## İşveren hakedişinden İKİ fark BURADA da korunur

1. **Ret gerekçesi KOLONA yazılır** (`rejected_at`/`rejection_reason`) — zincir
   silinse bile "Revize Gerekli" rozeti ayakta kalır. İşverende gerekçenin tek
   kalıcı izi denetim günlüğüdür (kolon YOK, K2 kolon İSTEMEZ).
2. **`submit` damgayı temizler** — yeniden gönderilen hakediş artık revize
   bekleyen değildir. Zincir de yeniden ADIM 1'den kurulur; ikisi aynı olguyu
   iki katmanda anlatır.

🔴 ÜÇ ADIMLI zincirde her adımı BAŞKA aktör atar (görevler ayrılığı) ve her
aktörün `progress_payments ≥ approve` izni OLMAK ZORUNDADIR: onay rolü izin
VERMEZ. Matriste `site_chief` sistem rolü `_DRF`tir — bu yüzden `site_chief`
ONAY ROLÜNÜ taşıyan aktör burada `project_manager` SİSTEM rolündedir. İkisinin
ayrı olduğu tam olarak budur.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.approvals import guards as approval_guards
from app.modules.approvals.models import ApprovalDocumentType, ApprovalRole
from app.modules.contracts.models import SubcontractorContract
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
    SubcontractorProgressPaymentLine,
)
from app.modules.users.models import User
from tests.modules.approvals.conftest import (
    adim_durumlari,
    adim_rolleri,
    onay_rolu_ver,
    rol_sahipleri_dolgusu,  # noqa: F401  (fixture)
    zincir_getir,
)

pytestmark = pytest.mark.usefixtures("rol_sahipleri_dolgusu")

_TIP = ApprovalDocumentType.subcontractor_progress_payment
_YOL = "/subcontractor-progress-payments"
_GEREKCE = {"reason": "Metrajlar eksik, revize edin"}


async def _satirli_hakedis(
    session: AsyncSession,
    hakedis_fabrikasi,
    contract: SubcontractorContract,
    creator: User,
    *,
    sequence_no: int = 1,
    status: SubcontractorPaymentStatus = SubcontractorPaymentStatus.draft,
    miktar: Decimal = Decimal("10"),
) -> SubcontractorProgressPayment:
    payment = await hakedis_fabrikasi(
        contract,
        creator,
        sequence_no=sequence_no,
        status=status,
        period_year=2026,
        period_month=7,
    )
    for sort_order, item in enumerate(sorted(contract.items, key=lambda kalem: kalem.sort_order)):
        session.add(
            SubcontractorProgressPaymentLine(
                payment_id=payment.id,
                contract_item_id=item.id,
                code=item.code,
                description=item.description,
                unit=item.unit,
                contract_unit_price=item.unit_price,
                coefficient=Decimal("1.000"),
                quantity=miktar,
                sort_order=sort_order,
            )
        )
    await session.flush()
    await session.refresh(payment)
    return payment


async def _onaycı(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    *,
    email: str,
    approval_roles: tuple[ApprovalRole, ...],
    role_key: str = "project_manager",
) -> dict[str, str]:
    user = await user_factory(email=email, password="parola1234", role_key=role_key)
    # IZN-B3b: onay rolü = PROJE rolü → ekip kişisi `all_projects=False` (üretimde "Tüm projeler"
    # kişide ekip satırı yoktur; `step_owner_clause` o satırı yok sayar).
    user.all_projects = not approval_roles
    await onay_rolu_ver(seeded_db, user, *approval_roles)
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _esik(client: AsyncClient, admin_headers: dict[str, str], deger: str) -> None:
    yanit = await client.put(
        "/approvals/settings", json={"approval_threshold_try": deger}, headers=admin_headers
    )
    assert yanit.status_code == 200, yanit.text


async def _gonder(
    client: AsyncClient, headers: dict[str, str], payment_id: uuid.UUID
) -> dict[str, object]:
    yanit = await client.post(f"{_YOL}/{payment_id}/submit", headers=headers)
    assert yanit.status_code == 200, yanit.text
    return yanit.json()


# --------------------------------------------------------------------------- #
# 1. `submit` zincir kurar — MOCKUP sırası
# --------------------------------------------------------------------------- #


async def test_submit_UC_ADIMLI_zincir_kurar_MOCKUP_SIRASIYLA(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)

    await _gonder(client, admin_headers, payment.id)

    zincir = await zincir_getir(seeded_db, _TIP, payment.id)
    assert zincir is not None, "submit zincir AÇMADI"
    assert await adim_rolleri(seeded_db, zincir.id) == [
        ApprovalRole.site_chief,
        ApprovalRole.project_manager,
        ApprovalRole.accounting,
    ]
    # Tutar BRÜTtür (R5) ve `amounts.build_block(...).gross` ile AYNI tabandan
    # gelir: `calculations.gross_total(payment.lines)`.
    assert zincir.amount_snapshot is not None and zincir.amount_snapshot > 0


async def test_submit_esik_USTUNDE_PATRON_SONA_eklenir(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    await _esik(client, admin_headers, "1.00")

    await _gonder(client, admin_headers, payment.id)

    zincir = await zincir_getir(seeded_db, _TIP, payment.id)
    assert await adim_rolleri(seeded_db, zincir.id) == [
        ApprovalRole.site_chief,
        ApprovalRole.project_manager,
        ApprovalRole.accounting,
        ApprovalRole.patron,
    ]


# --------------------------------------------------------------------------- #
# 2. Üç adım SIRAYLA ilerler
# --------------------------------------------------------------------------- #


async def test_UC_ADIM_SIRAYLA_ilerler_son_adimda_APPROVED(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
    user_factory,
) -> None:
    """İlk İKİ adım evrağı `pending_approval`da BIRAKIR; yalnız ÜÇÜNCÜ onaylar."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    sef = await _onaycı(
        client,
        seeded_db,
        user_factory,
        email="tsr-sef@ok1a.co",
        approval_roles=(ApprovalRole.site_chief,),
    )
    pm = await _onaycı(
        client,
        seeded_db,
        user_factory,
        email="tsr-pm@ok1a.co",
        approval_roles=(ApprovalRole.project_manager,),
    )
    muhasebe = await _onaycı(
        client,
        seeded_db,
        user_factory,
        email="tsr-muhasebe@ok1a.co",
        approval_roles=(ApprovalRole.accounting,),
        role_key="accounting",
    )
    await _gonder(client, admin_headers, payment.id)
    zincir = await zincir_getir(seeded_db, _TIP, payment.id)

    for sira, basliklar in enumerate((sef, pm), start=1):
        yanit = await client.post(f"{_YOL}/{payment.id}/approve", headers=basliklar)
        assert yanit.status_code == 200, (sira, yanit.text)
        assert yanit.json()["status"] == "pending_approval", sira

    son = await client.post(f"{_YOL}/{payment.id}/approve", headers=muhasebe)
    assert son.status_code == 200, son.text
    assert son.json()["status"] == "approved"
    assert await adim_durumlari(seeded_db, zincir.id) == [True, True, True]


async def test_SIRASIZ_aktor_403_rol_eslesmiyorsa(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
    user_factory,
) -> None:
    """Zincirin İKİNCİ adımının rolünü taşıyan aktör BİRİNCİ adımı ilerletemez.

    Adım SIRASI 409 değil 403 üretir çünkü ilerletilecek adım DAİMA sıradaki
    adımdır (uç gövdesinde adım numarası TAŞIMAZ); engel aktörün o adımın
    ROLÜNÜ taşımamasıdır.
    """
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    pm = await _onaycı(
        client,
        seeded_db,
        user_factory,
        email="tsr-sirasiz-pm@ok1a.co",
        approval_roles=(ApprovalRole.project_manager,),
    )
    await _gonder(client, admin_headers, payment.id)

    yanit = await client.post(f"{_YOL}/{payment.id}/approve", headers=pm)

    assert yanit.status_code == 403, yanit.text
    assert yanit.json()["detail"] == approval_guards.APPROVAL_ROLE_MISSING


# --------------------------------------------------------------------------- #
# 3. Ret — zincir SİLİNİR, damga KOLONDA kalır
# --------------------------------------------------------------------------- #


async def test_ret_ZINCIRI_DAMGALAR_damga_KOLONDA_kalir_yeniden_gonderim_YENI_zincir_ADIM_1DEN(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
    user_factory,
) -> None:
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    sef = await _onaycı(
        client,
        seeded_db,
        user_factory,
        email="tsr-ret-sef@ok1a.co",
        approval_roles=(ApprovalRole.site_chief,),
    )
    pm = await _onaycı(
        client,
        seeded_db,
        user_factory,
        email="tsr-ret-pm@ok1a.co",
        approval_roles=(ApprovalRole.project_manager,),
    )
    await _gonder(client, admin_headers, payment.id)
    await client.post(f"{_YOL}/{payment.id}/approve", headers=sef)

    # 🔴 Ret de bir KARARDIR ve AYNI bekçi hunisinden geçer: reddeden aktör
    # SIRADAKİ adımın (burada Proje Müdürü) rolünü taşımalıdır.
    ret = await client.post(f"{_YOL}/{payment.id}/reject", json=_GEREKCE, headers=pm)

    assert ret.status_code == 200, ret.text
    govde = ret.json()
    assert govde["status"] == "draft"
    # İşverenden FARK: gerekçe KOLONDA yaşar.
    assert govde["rejection_reason"] == _GEREKCE["reason"]
    assert govde["rejected_at"] is not None
    eski = await zincir_getir(seeded_db, _TIP, payment.id)
    assert eski is not None and eski.rejected_at is not None, "ret zinciri DAMGALAMALI"

    await _gonder(client, admin_headers, payment.id)
    yeni = await zincir_getir(seeded_db, _TIP, payment.id)
    assert yeni.id != eski.id and yeni.rejected_at is None
    assert await adim_durumlari(seeded_db, yeni.id) == [False, False, False]


async def test_ret_GEREKCESIZ_422_ZINCIR_AYAKTA_kalir(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
) -> None:
    """Gerekçe reddedilirse zincir SİLİNMEMİŞ olmalıdır — yarım bir ret, tüm
    onayları silip evrağı zincirsiz bırakırdı."""
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    await _gonder(client, admin_headers, payment.id)

    yanit = await client.post(
        f"{_YOL}/{payment.id}/reject", json={"reason": "   "}, headers=admin_headers
    )

    assert yanit.status_code == 422, yanit.text
    assert await zincir_getir(seeded_db, _TIP, payment.id) is not None


# --------------------------------------------------------------------------- #
# 4. `/unapprove` — zincir SİLİNMEZ (ret'ten FARKI)
# --------------------------------------------------------------------------- #


async def test_unapprove_SON_ADIMI_GERI_SARAR_zincir_AYAKTA(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers: dict[str, str],
    admin_kullanicisi: User,
    taseron_sozlesmesi,
    hakedis_fabrikasi,
    user_factory,
) -> None:
    """🔴 Geri alma zinciri SİLSEYDİ (ya da HİÇ dokunmasaydı) evrak KİLİTLENİRDİ:

    dokunmayan bir geri alma, tamamlanmış zincirli bir evrağı `pending_approval`a
    döndürür ve sonraki `/approve` "zincir tamamlanmış" 409'u alırdı.
    """
    contract, _, _ = taseron_sozlesmesi
    payment = await _satirli_hakedis(seeded_db, hakedis_fabrikasi, contract, admin_kullanicisi)
    aktorler = [
        await _onaycı(
            client,
            seeded_db,
            user_factory,
            email=f"tsr-geri-{i}@ok1a.co",
            approval_roles=(rol,),
            role_key="accounting" if rol is ApprovalRole.accounting else "project_manager",
        )
        for i, rol in enumerate(
            (ApprovalRole.site_chief, ApprovalRole.project_manager, ApprovalRole.accounting)
        )
    ]
    await _gonder(client, admin_headers, payment.id)
    for basliklar in aktorler:
        await client.post(f"{_YOL}/{payment.id}/approve", headers=basliklar)
    zincir = await zincir_getir(seeded_db, _TIP, payment.id)
    assert await adim_durumlari(seeded_db, zincir.id) == [True, True, True]

    geri = await client.post(f"{_YOL}/{payment.id}/unapprove", headers=admin_headers)

    assert geri.status_code == 200, geri.text
    assert geri.json()["status"] == "pending_approval"
    assert await zincir_getir(seeded_db, _TIP, payment.id) is not None, "zincir SİLİNDİ"
    assert await adim_durumlari(seeded_db, zincir.id) == [True, True, False]

    # KİLİTLENMEDİ: geri sarılan adım yeniden onaylanabilir.
    tekrar = await client.post(f"{_YOL}/{payment.id}/approve", headers=aktorler[2])
    assert tekrar.status_code == 200, tekrar.text
    assert tekrar.json()["status"] == "approved"

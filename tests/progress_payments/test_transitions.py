"""Task H6 — durum geçişleri: submit/approve/reject/mark-paid/unapprove
(spec §7 tamamı, §9.4, §9.7).

Üç ayrı kural ailesi burada koşar ve BİRBİRİNE KARIŞTIRILMAZ:

1. **Geçiş tablosu** (§7): tanımlı beş çift dışındaki HER çift 409
   `INVALID_STATUS_TRANSITION` — tam matris parametrize edilir (20 çift, 5 geçerli,
   15 geçersiz), böylece tabloya kaçak bir çift eklendiğinde test kırmızıya döner.
2. **İzin kapıları** (§7 tablosu): kapı GÖRÜNÜRLÜKTEN ÖNCE çalışır — yetkisiz rol,
   GÖRDÜĞÜ bir kayıtta bile 403 alır (404 değil), görmediği kayıtta da 403 alır
   (varlık sızdırmaz).
3. **Onay anındaki kota bekçisi** (H5 denetimi O2): kota yalnız yazma anında
   kontrol edilseydi, aynı anda açık iki taslak kotayı ayrı ayrı geçip toplamda
   aşardı. `test_ikinci_onay_kotayi_asinca_422` bu deliği kapatır.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.progress_payments import guards, lines, service, transitions
from app.modules.progress_payments.models import ProgressPayment, ProgressPaymentStatus
from app.modules.users.models import User
from tests._para_gercek import parayi_yatir
from tests.modules.approvals.conftest import rol_sahipleri_dolgusu  # noqa: F401  (fixture)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("rol_sahipleri_dolgusu")]

DURUMLAR = [durum.value for durum in ProgressPaymentStatus]
UCLAR = ["submit", "approve", "reject", "mark-paid", "unapprove"]

# Spec §7 tablosu — BU BEŞ ÇİFT geçerlidir, başka hiçbir çift değildir.
GECERLI: dict[tuple[str, str], str] = {
    ("draft", "submit"): "pending_approval",
    ("pending_approval", "approve"): "approved",
    ("pending_approval", "reject"): "draft",
    ("approved", "mark-paid"): "paid",
    # §7 tablosu: "approved → pending_approval (geri çek)". Taslağa DÖNMEZ —
    # taslağa dönüş iki adımdır (unapprove + reject), her adımı denetim izli.
    ("approved", "unapprove"): "pending_approval",
}
TUM_CIFTLER = [(durum, uc) for durum in DURUMLAR for uc in UCLAR]
GECERSIZ_CIFTLER = [cift for cift in TUM_CIFTLER if cift not in GECERLI]

# 🔴 OK-1A K2 (KIRICI, kullanıcı kararı 2026-08-21): `/reject` gövdesi ve
# gerekçesi ZORUNLU oldu (eskiden `RejectBody | None` idi ve gövdesiz istek
# geçerliydi). Gövde şema katmanında doğrulandığı için EKSİK gövde 422'dir ve
# 409/404 gibi ASIL iddiaları GÖLGELER — bu yüzden matris testleri artık her
# `reject` çağrısında geçerli bir gerekçe gönderir (taşeron ikizinin `GEREKCE`
# sabitiyle aynı desen).
GEREKCE = {"reason": "Metrajlar eksik, revize edin"}


def _govde(uc: str) -> dict[str, str] | None:
    return GEREKCE if uc == "reject" else None


async def _durum(session: AsyncSession, payment_id: uuid.UUID) -> ProgressPayment:
    payment = await session.get(ProgressPayment, payment_id)
    await session.refresh(payment)
    return payment


# --- 1. Geçiş tablosu (spec §7) ---


@pytest.mark.parametrize("durum,uc", GECERSIZ_CIFTLER)
async def test_gecersiz_gecis_409(
    client: AsyncClient,
    admin_headers: dict[str, str],
    hakedis_fabrikasi,
    durum: str,
    uc: str,
) -> None:
    """Tanımsız HER çift 409 `INVALID_STATUS_TRANSITION` — `paid → unapprove`
    dahil (K7: `paid` sonrası düzeltme yolu YOK).

    Aktör `system_admin`: kapı TÜM uçlarda açıktır, bu yüzden 409'un kaynağı
    yetki değil YALNIZ geçiş tablosudur.
    """
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus(durum))
    yanit = await client.post(
        f"/progress-payments/{payment_id}/{uc}", json=_govde(uc), headers=admin_headers
    )
    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == guards.INVALID_STATUS_TRANSITION


@pytest.mark.parametrize("cift,hedef", list(GECERLI.items()))
async def test_gecerli_gecis_hedef_duruma_goturur(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
    cift: tuple[str, str],
    hedef: str,
) -> None:
    """Beş geçerli çiftin HEDEFİ de doğrulanır: tablo doğru çifti kabul edip
    YANLIŞ duruma götürseydi (ör. unapprove → draft) yalnız "409 gelmedi"
    kontrolü bunu kaçırırdı."""
    durum, uc = cift
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus(durum))
    if uc == "mark-paid":
        # 🔴 PARA-GERCEK: `paid` artık arkasında GERÇEKLEŞMİŞ para ister.
        await parayi_yatir(seeded_db, payment_id, taseron=False)
    yanit = await client.post(
        f"/progress-payments/{payment_id}/{uc}", json=_govde(uc), headers=admin_headers
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == hedef


# --- 2. Damgalar (spec §7 tablosu) ---


async def test_submit_damga_ve_durum(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    gecerli_taslak: uuid.UUID,
) -> None:
    """E15 71 / OLU 25 "Onaya Gönder": `submitted_at` damgalanır."""
    onces = datetime.now(UTC)
    yanit = await client.post(f"/progress-payments/{gecerli_taslak}/submit", headers=admin_headers)
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "pending_approval"
    assert yanit.json()["submitted_at"] is not None

    payment = await _durum(seeded_db, gecerli_taslak)
    assert payment.status is ProgressPaymentStatus.pending_approval
    assert payment.submitted_at >= onces
    assert payment.approved_at is None and payment.approved_by is None and payment.paid_at is None


async def test_approve_damgalari(
    client: AsyncClient,
    muhasebe_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """`approved_by` = ONAYLAYAN aktör (oluşturan değil), `approved_at` dolu."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.pending_approval)
    yanit = await client.post(f"/progress-payments/{payment_id}/approve", headers=muhasebe_headers)
    assert yanit.status_code == 200, yanit.text

    onaylayan = (
        await seeded_db.execute(select(User).where(User.email == "muhasebe@pp-transitions.co"))
    ).scalar_one()
    payment = await _durum(seeded_db, payment_id)
    assert payment.status is ProgressPaymentStatus.approved
    assert payment.approved_by == onaylayan.id
    assert payment.approved_at is not None
    assert payment.paid_at is None


async def test_mark_paid_damgasi(
    client: AsyncClient,
    muhasebe_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """K11: `mark-paid` onay seviyesindedir; `paid_at` damgalanır, onay damgaları
    KORUNUR (ödeme onayı geçersiz kılmaz)."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.pending_approval)
    await client.post(f"/progress-payments/{payment_id}/approve", headers=muhasebe_headers)
    await parayi_yatir(seeded_db, payment_id, taseron=False)
    yanit = await client.post(
        f"/progress-payments/{payment_id}/mark-paid", headers=muhasebe_headers
    )
    assert yanit.status_code == 200, yanit.text

    payment = await _durum(seeded_db, payment_id)
    assert payment.status is ProgressPaymentStatus.paid
    assert payment.paid_at is not None
    assert payment.approved_at is not None and payment.approved_by is not None


async def test_unapprove_onay_damgalarini_siler(
    client: AsyncClient,
    admin_headers: dict[str, str],
    muhasebe_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """Onay GERİ ÇEKİLİR: kayıt `pending_approval`'a dönerken `approved_at`/
    `approved_by` TEMİZLENİR — aksi hâlde onay bekleyen bir kayıt "onaylayan"
    bilgisi taşır ve denetimde yanlış kişiyi işaret ederdi."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.pending_approval)
    await client.post(f"/progress-payments/{payment_id}/approve", headers=muhasebe_headers)

    yanit = await client.post(f"/progress-payments/{payment_id}/unapprove", headers=admin_headers)
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "pending_approval"

    payment = await _durum(seeded_db, payment_id)
    assert payment.approved_at is None
    assert payment.approved_by is None


async def test_reject_drafta_dondurur_ve_yeniden_duzenlenir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    hakedis_fabrikasi,
    hakedis_santiyesi,
    hakedis_kalemi,
) -> None:
    """Ret sonrası taslak YENİDEN DÜZENLENEBİLİR olmalıdır: `PUT …/lines`
    yalnız `draft`'ta çalıştığı için bu, geçişin gerçekten `draft` hedeflediğinin
    uçtan uca kanıtıdır."""
    item, _ = hakedis_kalemi
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.pending_approval)

    ret = await client.post(
        f"/progress-payments/{payment_id}/reject", json=GEREKCE, headers=admin_headers
    )
    assert ret.status_code == 200, ret.text
    assert ret.json()["status"] == "draft"

    duzenleme = await client.put(
        f"/progress-payments/{payment_id}/lines",
        json={
            "lines": [
                {
                    "contract_item_id": str(item.id),
                    "site_id": str(hakedis_santiyesi.id),
                    "quantity": "5",
                }
            ]
        },
        headers=admin_headers,
    )
    assert duzenleme.status_code == 200, duzenleme.text
    assert Decimal(duzenleme.json()["lines"][0]["quantity"]) == Decimal("5")


async def test_reject_gerekcesi_kabul_edilir_kolon_acilmaz(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """K12: gövdedeki `reason` kabul edilir (denetim günlüğüne H10'da taşınır),
    hakediş tablosuna AYRI KOLON açılmaz — yanıt şemasında da yer almaz."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.pending_approval)
    yanit = await client.post(
        f"/progress-payments/{payment_id}/reject",
        json={"reason": "Miktarlar şantiye tutanağıyla uyuşmuyor"},
        headers=admin_headers,
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "draft"
    assert "reason" not in yanit.json()
    assert not hasattr(await _durum(seeded_db, payment_id), "reason")


async def test_reject_reason_500_karakteri_asinca_422(
    client: AsyncClient,
    admin_headers: dict[str, str],
    hakedis_fabrikasi,
) -> None:
    """H10 denetimi Y3 (spec §10/6): `RejectBody.reason` sunucu sınırı
    `max_length=500` — DB kolonu yok, sınırsız gövde günlüğe keyfi uzunlukta
    metin yazardı. 501 karakter Pydantic doğrulamasında 422 döner."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.pending_approval)
    yanit = await client.post(
        f"/progress-payments/{payment_id}/reject",
        json={"reason": "a" * 501},
        headers=admin_headers,
    )
    assert yanit.status_code == 422, yanit.text


# --- 3. Submit zorunluluk kuralları (spec §7, `guards.validate_submit`) ---


async def test_submit_donemsiz_422(
    client: AsyncClient, admin_headers: dict[str, str], hakedis_fabrikasi
) -> None:
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.draft, donem=False)
    yanit = await client.post(f"/progress-payments/{payment_id}/submit", headers=admin_headers)
    assert yanit.status_code == 422, yanit.text
    assert yanit.json()["detail"] == guards.PERIOD_REQUIRED


async def test_submit_satirsiz_422(
    client: AsyncClient, admin_headers: dict[str, str], hakedis_fabrikasi
) -> None:
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.draft, miktar=None)
    yanit = await client.post(f"/progress-payments/{payment_id}/submit", headers=admin_headers)
    assert yanit.status_code == 422, yanit.text
    assert yanit.json()["detail"] == guards.LINES_REQUIRED


async def test_submit_sifir_miktarli_satirla_422(
    client: AsyncClient, admin_headers: dict[str, str], hakedis_fabrikasi
) -> None:
    """Satır VAR ama Σ miktar = 0: taslakta meşru (OLU 172), onaya gönderilemez."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.draft, miktar=Decimal("0"))
    yanit = await client.post(f"/progress-payments/{payment_id}/submit", headers=admin_headers)
    assert yanit.status_code == 422, yanit.text
    assert yanit.json()["detail"] == guards.LINES_REQUIRED


async def test_submit_sozlesme_bedelsizken_422(
    client: AsyncClient, admin_headers: dict[str, str], bedelsiz_sozlesmede_taslak: uuid.UUID
) -> None:
    """§6.3: avans tavanı `contract.amount` olmadan uygulanamaz."""
    yanit = await client.post(
        f"/progress-payments/{bedelsiz_sozlesmede_taslak}/submit", headers=admin_headers
    )
    assert yanit.status_code == 422, yanit.text
    assert yanit.json()["detail"] == guards.CONTRACT_AMOUNT_REQUIRED


async def test_submit_basarisizsa_durum_degismez(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """Zorunluluk kuralı GEÇİŞTEN ÖNCE koşar: 422 alan hakediş `draft` KALIR."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.draft, donem=False)
    await client.post(f"/progress-payments/{payment_id}/submit", headers=admin_headers)
    payment = await _durum(seeded_db, payment_id)
    assert payment.status is ProgressPaymentStatus.draft
    assert payment.submitted_at is None


# --- 4. İzin kapıları (spec §7 tablosu; kapı GÖRÜNÜRLÜKTEN ÖNCE) ---


async def test_sef_approve_edemez_403(
    client: AsyncClient,
    site_chief_headers: dict[str, str],
    kisitli_projede_onay_bekleyen: uuid.UUID,
) -> None:
    """Matris `progress_payments=_DRF`: şef taslak üretir, ONAYLAYAMAZ.

    Kayıt şefin GÖRDÜĞÜ projededir — dönen 403 kapsam değil YETKİ kararıdır.
    """
    yanit = await client.post(
        f"/progress-payments/{kisitli_projede_onay_bekleyen}/approve", headers=site_chief_headers
    )
    assert yanit.status_code == 403, yanit.text


async def test_saha_submit_edebilir(
    client: AsyncClient, saha_headers: dict[str, str], gecerli_taslak: uuid.UUID
) -> None:
    """Matris `progress_payments=_DRF`: saha mühendisi kendi projesinde onaya
    gönderebilir (`submit` taslak seviyesindedir)."""
    yanit = await client.post(f"/progress-payments/{gecerli_taslak}/submit", headers=saha_headers)
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "pending_approval"


async def test_saha_approve_edemez_403(
    client: AsyncClient, saha_headers: dict[str, str], hakedis_fabrikasi
) -> None:
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.pending_approval)
    yanit = await client.post(f"/progress-payments/{payment_id}/approve", headers=saha_headers)
    assert yanit.status_code == 403, yanit.text


async def test_muhasebe_reject_ve_mark_paid_yapabilir(
    client: AsyncClient,
    muhasebe_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """`reject` ve `mark-paid` de ONAY seviyesindedir (§7 tablosu, K11)."""
    ret_edilecek = await hakedis_fabrikasi(ProgressPaymentStatus.pending_approval)
    ret = await client.post(
        f"/progress-payments/{ret_edilecek}/reject", json=GEREKCE, headers=muhasebe_headers
    )
    assert ret.status_code == 200, ret.text

    odenecek = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    await parayi_yatir(seeded_db, odenecek, taseron=False)
    odeme = await client.post(f"/progress-payments/{odenecek}/mark-paid", headers=muhasebe_headers)
    assert odeme.status_code == 200, odeme.text


async def test_admin_disinda_unapprove_403(
    client: AsyncClient, muhasebe_headers: dict[str, str], hakedis_fabrikasi
) -> None:
    """`unapprove` YALNIZ `admin`: onay seviyesindeki muhasebe kendi onayını bile
    geri çekemez (§7 tablosu — geri çekme silme yolunun ön adımıdır)."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    yanit = await client.post(
        f"/progress-payments/{payment_id}/unapprove", headers=muhasebe_headers
    )
    assert yanit.status_code == 403, yanit.text


async def test_patron_unapprove_edemez_403(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, hakedis_fabrikasi
) -> None:
    """`full` seviyesi (patron) `admin`'i KAPSAMAZ (`access.py` sırası) — kapı
    `_ADMIN` seçildiği için patron da geri çekemez."""
    await user_factory(email="patron@pp-transitions.co", password="parola1234", role_key="patron")
    giris = await client.post(
        "/auth/login", json={"email": "patron@pp-transitions.co", "password": "parola1234"}
    )
    headers = {"Authorization": f"Bearer {giris.json()['access_token']}"}
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    yanit = await client.post(f"/progress-payments/{payment_id}/unapprove", headers=headers)
    assert yanit.status_code == 403, yanit.text


@pytest.mark.parametrize("uc", UCLAR)
async def test_izinsiz_rol_403(
    client: AsyncClient, hr_headers: dict[str, str], hakedis_fabrikasi, uc: str
) -> None:
    """İK matris satırı `_N`: kapı GÖRÜNÜRLÜKTEN ÖNCE çalışır → her uçta 403."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.draft)
    yanit = await client.post(f"/progress-payments/{payment_id}/{uc}", headers=hr_headers)
    assert yanit.status_code == 403, yanit.text


# --- 5. Kapsam / IDOR (spec §9.0) ---


@pytest.mark.parametrize("uc", ["submit", "approve", "reject", "mark-paid"])
async def test_gorunmeyen_hakedis_404_olmayanla_ayni(
    client: AsyncClient, kisitli_headers: dict[str, str], gorunmeyen_hakedis: uuid.UUID, uc: str
) -> None:
    """Görünmeyen projedeki GERÇEK hakediş ile var OLMAYAN kimlik AYIRT EDİLEMEZ.

    `unapprove` bu listede YOKTUR: kapısı `_ADMIN` olduğu için `project_manager`
    oraya 403 ile takılır (kapı görünürlükten önce) — ayrı testi aşağıdadır.
    """
    gercek = await client.post(
        f"/progress-payments/{gorunmeyen_hakedis}/{uc}", json=_govde(uc), headers=kisitli_headers
    )
    sahte = await client.post(
        f"/progress-payments/{uuid.uuid4()}/{uc}", json=_govde(uc), headers=kisitli_headers
    )
    assert gercek.status_code == sahte.status_code == 404, gercek.text
    assert gercek.json() == sahte.json() == {"detail": guards.PAYMENT_MISSING}


async def test_gorunmeyen_hakediste_unapprove_kapida_403(
    client: AsyncClient, kisitli_headers: dict[str, str], gorunmeyen_hakedis: uuid.UUID
) -> None:
    """Kapı görünürlükten ÖNCE: yetkisi olmayan rol 404 ile 403 arasındaki farktan
    kaydın varlığını çıkaramaz, çünkü GÖRDÜĞÜ kayıtta da aynı 403'ü alır."""
    yanit = await client.post(
        f"/progress-payments/{gorunmeyen_hakedis}/unapprove", headers=kisitli_headers
    )
    assert yanit.status_code == 403, yanit.text


async def test_capraz_proje_kapsami_gecise_sizmaz(
    client: AsyncClient,
    kisitli_headers: dict[str, str],
    kisitli_projede_onay_bekleyen: uuid.UUID,
    gorunmeyen_hakedis: uuid.UUID,
) -> None:
    """Aynı aktör: GÖRDÜĞÜ projede geçiş 200, GÖRMEDİĞİ projede 404."""
    gorunen = await client.post(
        f"/progress-payments/{kisitli_projede_onay_bekleyen}/approve", headers=kisitli_headers
    )
    assert gorunen.status_code == 200, gorunen.text

    gorunmeyen = await client.post(
        f"/progress-payments/{gorunmeyen_hakedis}/submit", headers=kisitli_headers
    )
    assert gorunmeyen.status_code == 404, gorunmeyen.text


# --- 6. H5'ten DEVREDİLEN: onay anında kota yeniden doğrulanır (denetim O2) ---


async def test_ikinci_onay_kotayi_asinca_422(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    kota_bolusen_iki_hakedis: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Kotanın nihai bekçisi YAZMA değil ONAY anıdır (H5 denetimi O2).

    İki açık hakediş de 600 birimle kotaya (1.000) tek başına sığar; birincisi
    onaylanınca kümülatif kümeye girer ve ikincinin onayı 1.200 > 1.000 ile
    422 `QUANTITY_EXCEEDS_QUOTA` alır. Kontrol yalnız `PUT …/lines`'ta kalsaydı
    aşım SESSİZCE gerçekleşirdi.
    """
    birinci, ikinci = kota_bolusen_iki_hakedis

    ilk_onay = await client.post(f"/progress-payments/{birinci}/approve", headers=admin_headers)
    assert ilk_onay.status_code == 200, ilk_onay.text

    ikinci_onay = await client.post(f"/progress-payments/{ikinci}/approve", headers=admin_headers)
    assert ikinci_onay.status_code == 422, ikinci_onay.text
    assert ikinci_onay.json()["detail"] == guards.QUANTITY_EXCEEDS_QUOTA

    # Reddedilen onay durumu DEĞİŞTİRMEZ.
    assert (await _durum(seeded_db, ikinci)).status is ProgressPaymentStatus.pending_approval


async def test_kota_sigiyorsa_ikinci_onay_gecer(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    kota_bolusen_iki_hakedis: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Kural ASIMDA koşar, her onayda değil: ikinci hakedişin miktarı kotaya
    sığacak şekilde düşürülürse onay GEÇER (aksi hâlde yukarıdaki test, kotayı
    hiç okumayan bir "ikinci onayı hep reddet" hatasıyla da yeşil kalırdı)."""
    birinci, ikinci = kota_bolusen_iki_hakedis
    ikinci_kayit = await seeded_db.get(ProgressPayment, ikinci)
    ikinci_kayit.lines[0].quantity = Decimal("400")
    await seeded_db.flush()

    assert (
        await client.post(f"/progress-payments/{birinci}/approve", headers=admin_headers)
    ).status_code == 200
    yanit = await client.post(f"/progress-payments/{ikinci}/approve", headers=admin_headers)
    assert yanit.status_code == 200, yanit.text


async def test_onay_kota_kontrolu_tek_toplama_yolunu_kullanir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    kota_bolusen_iki_hakedis: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Kümülatif toplam `lines.completed_totals`'tan okunur — İKİNCİ bir
    toplama yolu açılmaz (P5'in "iki farklı doğruluk tanımı" bulgusu).

    Casus, gerçek fonksiyonu sarar (davranış değişmez) ve `approve` sırasında
    hangi MODLA çağrıldığını da sabitler: kota SIRASIZ tam kümeden okunmalıdır
    (`exclude_payment_id` verilir, `before_sequence_no` VERİLMEZ — H6 denetimi K1).
    """
    birinci, _ = kota_bolusen_iki_hakedis
    cagrilar: list[dict] = []
    gercek = lines.completed_totals

    async def casus(session, project_id, **kwargs):
        cagrilar.append(kwargs)
        return await gercek(session, project_id, **kwargs)

    monkeypatch.setattr(transitions.lines, "completed_totals", casus)

    # H9 (O1) sonrası §6.6 "Önceki" kolonu geçmişi ARTIK `completed_totals` ile
    # DEĞİL, `service.build_detail`in TEK toplu çekimiyle okur (aynı sorgu iki
    # kez koşmasın diye) ve toplamayı `lines.totals_from_payments`'ın AYNI
    # gövdesinden alır. Mod ayrımının gösterim tarafı bu yüzden repository
    # çağrısında ölçülür — kural değişmedi, ölçüm noktası değişti.
    gecmis_cagrilari: list[dict] = []
    gercek_gecmis = service.repository.list_completed_payments

    async def gecmis_casusu(session, project_id, **kwargs):
        gecmis_cagrilari.append(kwargs)
        return await gercek_gecmis(session, project_id, **kwargs)

    monkeypatch.setattr(service.repository, "list_completed_payments", gecmis_casusu)
    yanit = await client.post(f"/progress-payments/{birinci}/approve", headers=admin_headers)
    assert yanit.status_code == 200, yanit.text
    assert cagrilar, "approve, kota kontrolünü `lines.completed_totals` üzerinden yapmıyor"
    # İSTEK BOYUNCA iki çağrı olur ve MODLARI FARKLIDIR: önce geçişin kota
    # doğrulaması (tam küme), sonra yanıt gövdesini kuran `service._line_rows`'un
    # §6.6 "Önceki" kolonu (sıra tabanlı). Sıra bu yüzden anlamlıdır — ilk çağrı
    # kotanınkidir.
    kota_cagrisi = cagrilar[0]
    assert kota_cagrisi.get("before_sequence_no") is None, (
        f"kota tavanı SIRA TABANLI kümeden okunuyor (K1 açığı geri geldi): {kota_cagrisi}"
    )
    assert kota_cagrisi.get("exclude_payment_id") == birinci, (
        f"kota tavanı kaydın KENDİSİNİ dışlamıyor: {kota_cagrisi}"
    )
    assert any(c.get("before_sequence_no") is not None for c in gecmis_cagrilari), (
        "§6.6 gösterim kolonları da sırasız kümeye kaymış olabilir (mod ayrımı kayboldu): "
        f"{gecmis_cagrilari}"
    )


# --- 7. D8 zinciri (spec §9.2) ---


async def test_approve_sonrasi_yeni_hakedis_acilabilir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    sozlesmeli_proje: uuid.UUID,
    hakedis_fabrikasi,
) -> None:
    """D8: açık hakediş kalmayınca yeni hakediş açılabilir, `sequence_no` +1."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.pending_approval)
    acikken = await client.post(
        f"/projects/{sozlesmeli_proje}/progress-payments", json={}, headers=admin_headers
    )
    assert acikken.status_code == 409, acikken.text

    onay = await client.post(f"/progress-payments/{payment_id}/approve", headers=admin_headers)
    assert onay.status_code == 200, onay.text

    yeni = await client.post(
        f"/projects/{sozlesmeli_proje}/progress-payments", json={}, headers=admin_headers
    )
    assert yeni.status_code == 201, yeni.text
    assert yeni.json()["sequence_no"] == 2


# --- 8. H6 denetimi K1: kota tavanı ONAY SIRASINDAN bağımsızdır (KRİTİK) ---
#
# Bulunan açık (2026-07-31): kota "önceki" kümesi §6.6'nın `sequence_no <` tanımıyla
# okunuyordu. Büyük sıra numaralı hakediş ÖNCE onaylanırsa küçük numaralı olan onu
# görmez; onay sırası değiştirilerek tavan MEŞRU uçlarla aşılabiliyordu. Çözüm:
# kota tavanı sırasız TAM kümeden (kendisi hariç tüm `approved|paid`) okur;
# §6.6'nın GÖSTERİM kolonları sıra tabanlı kalır.


async def test_ters_sirada_onayda_ikincisi_kotayi_asinca_422(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    kota_bolusen_iki_hakedis: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """`test_ikinci_onay_kotayi_asinca_422`'nin TERS SIRALISI (K1).

    Aynı kurulum (kota 1.000, iki hakediş × 600) önce seq 2, sonra seq 1
    onaylanarak koşar. Sıra tabanlı kota okumasında seq 1 kendinden BÜYÜK
    numaralı onaylı kaydı "önceki" saymaz ve İKİSİ DE 200 alırdı (toplam 1.200).
    """
    birinci, ikinci = kota_bolusen_iki_hakedis

    onay_ikinci = await client.post(f"/progress-payments/{ikinci}/approve", headers=admin_headers)
    assert onay_ikinci.status_code == 200, onay_ikinci.text

    onay_birinci = await client.post(f"/progress-payments/{birinci}/approve", headers=admin_headers)
    assert onay_birinci.status_code == 422, onay_birinci.text
    assert onay_birinci.json()["detail"] == guards.QUANTITY_EXCEEDS_QUOTA
    assert (await _durum(seeded_db, birinci)).status is ProgressPaymentStatus.pending_approval


async def test_ters_sirada_onay_sigiyorsa_gecer(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    kota_bolusen_iki_hakedis: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Karşı-test: ters sırada da kural yalnız AŞIMDA koşar.

    Bu olmadan yukarıdaki test, kotayı hiç okumayan bir "sıra bozuksa hep
    reddet" hatasıyla da yeşil kalırdı.
    """
    birinci, ikinci = kota_bolusen_iki_hakedis
    birinci_kayit = await seeded_db.get(ProgressPayment, birinci)
    birinci_kayit.lines[0].quantity = Decimal("400")
    await seeded_db.flush()

    assert (
        await client.post(f"/progress-payments/{ikinci}/approve", headers=admin_headers)
    ).status_code == 200
    yanit = await client.post(f"/progress-payments/{birinci}/approve", headers=admin_headers)
    assert yanit.status_code == 200, yanit.text


async def _onayli_toplam(session: AsyncSession, project_id: uuid.UUID) -> Decimal:
    """Projedeki `approved|paid` hakedişlerin satır miktarı toplamı."""
    kayitlar = (
        (
            await session.execute(
                select(ProgressPayment).where(
                    ProgressPayment.project_id == project_id,
                    ProgressPayment.status.in_(
                        (ProgressPaymentStatus.approved, ProgressPaymentStatus.paid)
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    return sum((line.quantity for kayit in kayitlar for line in kayit.lines), Decimal("0"))


async def test_onay_sirasi_degistirerek_kota_asma_zinciri_kapali(
    client: AsyncClient,
    admin_headers: dict[str, str],
    zincir_onaycilari: None,
    seeded_db: AsyncSession,
    sozlesmeli_proje: uuid.UUID,
    hakedis_santiyesi,
    hakedis_kalemi,
) -> None:
    """K1'in KANITLANMIŞ SÖMÜRÜ ZİNCİRİ — yedi adım, hepsi meşru uçlarla (kota 1.000).

    🔴 OK-1A T3 UYARLAMASI: zincir artık UÇTAN kurulduğu için (`submit` → gerçek
    `approval_chain`) aktörün ONAY ROLÜ de olmalıdır (`zincir_onaycilari`).
    Sömürü zincirinin KENDİSİ değişmedi; yalnız aktör gerçekçileşti — hakedişi
    `admin` AÇTIĞI için "kendi evrakı" bekçisi de devrededir ve `system_admin`in
    `progress_payments=_A` istisnasıyla ("vekâleten") geçer. Yani bu test artık
    kota bekçisini AYNI ANDA zincirin en gevşek dalında sınıyor.

    Zincir (denetim raporundaki hâliyle):
      1. P1 (seq 1) satır 600 → submit → approve
      2. P2 (seq 2) satır 400 → submit  (600+400 = 1.000, sınırda geçer)
      3. `unapprove` P1 → `reject` P1  (P1 taslak)
      4. P1 satırı 1.000'e yükseltilir
      5. submit P1
      6. approve P2
      7. approve P1  ← ESKİDEN 200; toplam 1.400 > 1.000, hiçbir uç hata vermezdi

    Artık 7. adım 422 verir ve onaylı toplam kotayı AŞMAZ. D8 zinciri boyunca
    korunur: P2 açılırken P1 onaylıdır (açık hakediş yoktur), `unapprove` ise
    D8'i denetlemez — zincirin tamamı uçlardan geçer.
    """
    item, _ = hakedis_kalemi
    site = hakedis_santiyesi
    kota = Decimal("1000")

    def _govde(miktar: str) -> dict:
        return {
            "period_year": 2026,
            "period_month": 1,
            "lines": [
                {
                    "contract_item_id": str(item.id),
                    "site_id": str(site.id),
                    "quantity": miktar,
                }
            ],
        }

    # 🔴 OK-1A T3: eşik bilerek YÜKSELTİLİR. Bu testin konusu KOTA'dır, eşik
    # değil; varsayılan ₺500.000 ile 600 × ₺1.850 = ₺1.110.000 zincire bir
    # PATRON adımı ekler ve tek aktör görevler ayrılığı yüzünden ikinci adımı
    # atamaz — senaryo kota bekçisine HİÇ ULAŞAMADAN takılırdı (ölçüldü).
    ayar = await client.put(
        "/approvals/settings",
        json={"approval_threshold_try": "99000000.00"},
        headers=admin_headers,
    )
    assert ayar.status_code == 200, ayar.text

    # 1 — P1: 600, onaya gönder, onayla.
    p1_yanit = await client.post(
        f"/projects/{sozlesmeli_proje}/progress-payments", json=_govde("600"), headers=admin_headers
    )
    assert p1_yanit.status_code == 201, p1_yanit.text
    p1 = p1_yanit.json()["id"]
    assert (
        await client.post(f"/progress-payments/{p1}/submit", headers=admin_headers)
    ).status_code == 200
    assert (
        await client.post(f"/progress-payments/{p1}/approve", headers=admin_headers)
    ).status_code == 200

    # 2 — P2: 400 (600+400 = 1.000, sınırda), onaya gönder.
    p2_yanit = await client.post(
        f"/projects/{sozlesmeli_proje}/progress-payments", json=_govde("400"), headers=admin_headers
    )
    assert p2_yanit.status_code == 201, p2_yanit.text
    p2 = p2_yanit.json()["id"]
    assert (
        await client.post(f"/progress-payments/{p2}/submit", headers=admin_headers)
    ).status_code == 200

    # 3 — P1'i geri çek ve reddet: taslağa döner.
    assert (
        await client.post(f"/progress-payments/{p1}/unapprove", headers=admin_headers)
    ).status_code == 200
    assert (
        await client.post(f"/progress-payments/{p1}/reject", json=GEREKCE, headers=admin_headers)
    ).status_code == 200

    # 4 — P1 satırı 1.000'e yükseltilir. Yazma anındaki kontrol yalnız
    # `approved|paid` kümesine baktığı için (P2 henüz `pending_approval`) bu adım
    # HER İKİ tasarımda da geçer — sızıntı 7. adımda kapanır.
    yukselt = await client.put(
        f"/progress-payments/{p1}/lines",
        json={
            "lines": [
                {"contract_item_id": str(item.id), "site_id": str(site.id), "quantity": "1000"}
            ]
        },
        headers=admin_headers,
    )
    assert yukselt.status_code == 200, yukselt.text

    # 5-6 — P1 yeniden onaya, P2 onaylanır (tek başına kotaya sığar).
    assert (
        await client.post(f"/progress-payments/{p1}/submit", headers=admin_headers)
    ).status_code == 200
    assert (
        await client.post(f"/progress-payments/{p2}/approve", headers=admin_headers)
    ).status_code == 200

    # 7 — ZİNCİRİN KIRILDIĞI ADIM.
    son_onay = await client.post(f"/progress-payments/{p1}/approve", headers=admin_headers)
    assert son_onay.status_code == 422, son_onay.text
    assert son_onay.json()["detail"] == guards.QUANTITY_EXCEEDS_QUOTA

    assert (await _durum(seeded_db, p1)).status is ProgressPaymentStatus.pending_approval
    seeded_db.expire_all()
    assert await _onayli_toplam(seeded_db, sozlesmeli_proje) <= kota

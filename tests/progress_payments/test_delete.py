"""Task H8 — silme + `sites` RESTRICT korkuluğu (spec §7.1, §9.5, §9.7).

K8 (bağlayıcı kullanıcı kararı): P5'in `DELETE /subcontractor-contracts/{id}`
istisnasının bir adım ÖTESİ. Orada `can_delete` TEK katmandı (`_FULL` kapı +
taslak istisnası); burada İKİ KATMAN var:

1. (SIL-B2'de KALDIRILDI) `approved|paid` artık Sistem Yöneticisi için silinebilir: fişi,
   faturası ve ödemeleri birlikte gider.
2. `status ∈ {draft, pending_approval}` → `can_delete(actor, level, record)`:
   admin koşulsuz; taslak istisnası yalnız KENDİ taslağını açan aktöre.
   `pending_approval` (`is_draft=False`) admin dışında kimseye açık değildir.

Kapı `_DRAFT`dir (draft seviyesindeki roller kendi taslaklarını silebilsin diye)
ama KESİN karar serviste verilir — kapı görünürlükten/durumdan önce yalnız
"bu modüle hiç erişimi yok" (403) durumunu eler.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.progress_payments import guards
from app.modules.progress_payments.models import ProgressPayment, ProgressPaymentStatus
from tests._silme_yardimci import sil_aile
from tests.discipline_scope._b2_yardim import SISYON_ONLY

pytestmark = pytest.mark.asyncio


async def _var_mi(session: AsyncSession, payment_id: uuid.UUID) -> bool:
    return (
        await session.execute(select(ProgressPayment).where(ProgressPayment.id == payment_id))
    ).scalar_one_or_none() is not None


# --- 1. Onaylı/ödenmiş hakediş de silinir (SIL-B2: iş kuralı 409'u yalnız bu yolda kalktı) ---


@pytest.mark.parametrize("durum", [ProgressPaymentStatus.approved, ProgressPaymentStatus.paid])
async def test_onayli_ve_odenmis_hakedis_admin_tarafindan_silinir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
    durum: ProgressPaymentStatus,
) -> None:
    payment_id = await hakedis_fabrikasi(durum)
    yanit = await sil_aile(client, admin_headers, "progress_payment", payment_id)
    assert yanit.status_code == 204, yanit.text
    assert not await _var_mi(seeded_db, payment_id)


async def test_silmek_icin_onizleme_zorunlu_428(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.approved)
    yanit = await client.delete(f"/progress-payments/{payment_id}", headers=admin_headers)
    assert yanit.status_code == 428, yanit.text
    assert yanit.json()["code"] == "preview_required"
    assert await _var_mi(seeded_db, payment_id)


# --- 2. Katman 2: draft/pending_approval — can_delete çapraz tablosu ---


async def test_sef_kendi_taslagini_da_silemez_403_istisna_yok(
    client: AsyncClient,
    site_chief_headers: dict[str, str],
    seeded_db: AsyncSession,
    kendi_taslagi: uuid.UUID,
) -> None:
    """SIL-B1 (K4): eski "kendi taslağını sahibi siler" (`can_delete`) istisnası KALDIRILDI."""
    yanit = await client.delete(f"/progress-payments/{kendi_taslagi}", headers=site_chief_headers)
    assert yanit.status_code == 403, yanit.text
    assert yanit.json() == SISYON_ONLY
    assert await _var_mi(seeded_db, kendi_taslagi)


async def test_sef_baskasinin_taslagini_silemez_403(
    client: AsyncClient,
    site_chief_headers: dict[str, str],
    seeded_db: AsyncSession,
    baskasinin_taslagi: uuid.UUID,
) -> None:
    yanit = await client.delete(
        f"/progress-payments/{baskasinin_taslagi}", headers=site_chief_headers
    )
    assert yanit.status_code == 403, yanit.text
    assert yanit.json() == SISYON_ONLY
    assert await _var_mi(seeded_db, baskasinin_taslagi)


async def test_pending_admin_disinda_silinemez_403(
    client: AsyncClient,
    muhasebe_headers: dict[str, str],
    seeded_db: AsyncSession,
    kisitli_projede_onay_bekleyen: uuid.UUID,
) -> None:
    """`is_draft=False` → taslak istisnası kapalı; `approve` seviyesi (muhasebe)

    bile 403 alır — admin OLMAYAN hiç kimse `pending_approval` silemez.
    """
    yanit = await client.delete(
        f"/progress-payments/{kisitli_projede_onay_bekleyen}", headers=muhasebe_headers
    )
    assert yanit.status_code == 403, yanit.text
    assert yanit.json() == SISYON_ONLY
    assert await _var_mi(seeded_db, kisitli_projede_onay_bekleyen)


async def test_pending_admin_silebilir_204(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    kisitli_projede_onay_bekleyen: uuid.UUID,
) -> None:
    yanit = await sil_aile(client, admin_headers, "progress_payment", kisitli_projede_onay_bekleyen)
    assert yanit.status_code == 204, yanit.text
    assert not await _var_mi(seeded_db, kisitli_projede_onay_bekleyen)


# --- 3. IDOR: görünmeyen proje ↔ var olmayan kimlik ayırt edilemez (spec §9.0) ---


async def test_gorunmeyen_projedeki_hakedis_403_varlik_sizmaz(
    client: AsyncClient,
    site_chief_headers: dict[str, str],
    seeded_db: AsyncSession,
    gorunmeyen_hakedis: uuid.UUID,
) -> None:
    """`site_chief_headers` Sistem Yöneticisi DEĞİL: kapı handler'dan ÖNCE koşar (SIL-B1) —
    `gorunmeyen_hakedis` GERÇEKTEN var olsa da var olmayan kimlikle AYNI 403 gövdesini alır."""
    yanit = await client.delete(
        f"/progress-payments/{gorunmeyen_hakedis}", headers=site_chief_headers
    )
    assert yanit.status_code == 403, yanit.text
    assert yanit.json() == SISYON_ONLY
    assert await _var_mi(seeded_db, gorunmeyen_hakedis)


async def test_var_olmayan_kimlik_404_ayni_govde(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    yanit = await sil_aile(client, admin_headers, "progress_payment", uuid.uuid4())
    assert yanit.status_code == 404, yanit.text
    assert yanit.json()["detail"] == guards.PAYMENT_MISSING


# --- 4. Silme başka kayıtları ETKİLEMEZ (H6/H7 dersi: "ne yapmamalı" testleri) ---


async def test_silme_satirlari_birlikte_siler_baska_kaydi_etkilemez(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
) -> None:
    """CASCADE (H1) doğrulaması: silinen hakedişin satırları da gider, AYNI

    sözleşmedeki (farklı sequence_no) DİĞER hakediş dokunulmadan kalır.
    """
    from app.modules.progress_payments.models import ProgressPaymentLine

    silinecek = await hakedis_fabrikasi(ProgressPaymentStatus.draft)
    dokunulmayan = await hakedis_fabrikasi(ProgressPaymentStatus.draft)

    onceki_satir_sayisi = len(
        (
            await seeded_db.execute(
                select(ProgressPaymentLine).where(ProgressPaymentLine.payment_id == silinecek)
            )
        )
        .scalars()
        .all()
    )
    assert onceki_satir_sayisi > 0

    yanit = await sil_aile(client, admin_headers, "progress_payment", silinecek)
    assert yanit.status_code == 204, yanit.text

    kalan_satirlar = (
        (
            await seeded_db.execute(
                select(ProgressPaymentLine).where(ProgressPaymentLine.payment_id == silinecek)
            )
        )
        .scalars()
        .all()
    )
    assert kalan_satirlar == []
    assert await _var_mi(seeded_db, dokunulmayan)


async def test_silme_sozlesmeyi_etkilemez(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
    hakedis_sozlesmesi,
) -> None:
    from app.modules.projects.models import ProjectContract

    project, _ = hakedis_sozlesmesi
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.draft)
    yanit = await sil_aile(client, admin_headers, "progress_payment", payment_id)
    assert yanit.status_code == 204, yanit.text

    contract = (
        await seeded_db.execute(
            select(ProjectContract).where(ProjectContract.project_id == project.id)
        )
    ).scalar_one_or_none()
    assert contract is not None


async def test_silme_santiyeyi_etkilemez(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
    hakedis_santiyesi,
) -> None:
    from app.modules.sites.models import Site

    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.draft)
    yanit = await sil_aile(client, admin_headers, "progress_payment", payment_id)
    assert yanit.status_code == 204, yanit.text

    site = await seeded_db.get(Site, hakedis_santiyesi.id)
    assert site is not None


# --- 5. `sites` RESTRICT korkuluğu (spec §4.2, §7.1 dipnotu) ---


async def test_taslak_hakedis_satirli_santiye_silinir_satirlar_gider_baslik_kalir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedisli_santiye: uuid.UUID,
) -> None:
    """SIL-B1: eski SITE_HAS_PROGRESS_PAYMENTS 409 korkuluğu Sistem Yöneticisi için BYPASS edilir.

    `progress_payment_lines.site_id` RESTRICT: satırlar önce silinir. TASLAK hakediş mali
    sayılmaz (fişi yok) → silme geçer. Başlık KALIR (bayat kalır: SIL-B2'ye devir, bkz.
    `tests/modules/silme/test_silme_api.py::test_hakedis_baslik_bayat_kalir_SIL_B2`)."""
    from app.modules.progress_payments.models import ProgressPaymentLine

    yanit = await sil_aile(client, admin_headers, "site", hakedisli_santiye)
    assert yanit.status_code == 204, yanit.text
    satirlar = await seeded_db.execute(
        select(ProgressPaymentLine.id).where(ProgressPaymentLine.site_id == hakedisli_santiye)
    )
    assert satirlar.all() == []


async def test_hakedissiz_santiye_silinebilir_204(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    project_factory,
) -> None:
    """Mevcut `sites` silme yolu kırılmadı: hakediş satırı OLMAYAN bir şantiye

    hâlâ normal şekilde silinebilir (RESTRICT korkuluğu yalnız GERÇEKTEN
    bağlı satır varken devreye girer).
    """
    from app.modules.sites.models import Site

    project = await project_factory(code="PP-H8-01", name="Hakedişsiz Şantiye Projesi")
    site = Site(project_id=project.id, code="SNT-H8-01", name="Boş Şantiye")
    seeded_db.add(site)
    await seeded_db.flush()

    yanit = await sil_aile(client, admin_headers, "site", site.id)
    assert yanit.status_code == 204, yanit.text

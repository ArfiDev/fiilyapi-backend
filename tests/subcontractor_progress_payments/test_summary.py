"""T4 — `GET /subcontractor-progress-payments/summary` (mockup L105-122; plan T4).

Mockup'ın DÖRT KPI kartı birebir karşılanır:

| Kart (L107-121)  | Alan                          | Tanım (spec §3)                              |
|------------------|-------------------------------|----------------------------------------------|
| Toplam Hakediş   | `total_gross`                 | Süzgeçteki TÜM hakedişlerin brütü            |
| Onay Bekliyor    | `pending_gross`               | `pending_approval` durumundakilerin brütü    |
| Bu Ay Ödenen     | `paid_period_gross`           | `paid` + ETKİN DÖNEM'deki hakedişlerin brütü |
| Aktif Taşeron    | `active_subcontractor_count`  | Süzgeçteki farklı taşeron sözleşmesi sayısı  |

L118 "Onay Bekliyor ₺1,24M" liste ekranındaki tek `pending_approval` satırının
**brüt** tutarıdır (L143 ₺1.240.000) — para KPI'larının brüt olduğu mockup'tan
BÖYLE okunur, tahmin edilmez.

Kapsam (§9.0) ayrıca ölçülür: özet toplu (batch) sorgu kullanır, toplu çekimde
kapsam sızıntısı klasik hatadır.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import timezone
from app.modules.contracts.models import SubcontractorContract, SubcontractorContractItem
from app.modules.subcontractor_progress_payments import summary
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
    SubcontractorProgressPaymentLine,
)
from app.modules.users.models import User

pytestmark = pytest.mark.asyncio

UC = "/subcontractor-progress-payments/summary"


def _kalemler(contract: SubcontractorContract) -> list[SubcontractorContractItem]:
    return sorted(contract.items, key=lambda item: item.sort_order)


async def _hakedis(
    session: AsyncSession,
    hakedis_fabrikasi,
    contract: SubcontractorContract,
    creator: User,
    *,
    sequence_no: int,
    status: SubcontractorPaymentStatus,
    miktar: Decimal,
    kalem_index: int = 0,
    period_year: int | None = 2026,
    period_month: int | None = 7,
) -> SubcontractorProgressPayment:
    payment = await hakedis_fabrikasi(
        contract,
        creator,
        sequence_no=sequence_no,
        status=status,
        period_year=period_year,
        period_month=period_month,
    )
    item = _kalemler(contract)[kalem_index]
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
            sort_order=0,
        )
    )
    await session.flush()
    return payment


@pytest.fixture
async def kpi_verisi(
    seeded_db: AsyncSession,
    taseron_sozlesmesi_fabrikasi,
    hakedis_fabrikasi,
    admin_kullanicisi: User,
) -> dict:
    """Tek projede İKİ taşeron sözleşmesi, üç farklı durumda hakediş.

    Kalem birim fiyatları fixture'dan: kalem#1 = 21.500, kalem#2 = 1.850.
    """
    a, proje, _ = await taseron_sozlesmesi_fabrikasi("THK-S01", subcontractor_name="Akın İnşaat")
    b, _, _ = await taseron_sozlesmesi_fabrikasi(
        "THK-S02", project=proje, subcontractor_name="Yılmaz Elektrik"
    )

    # A: onaylı 10×21.500 = 215.000 · onay bekleyen 5×1.850 = 9.250
    await _hakedis(
        seeded_db,
        hakedis_fabrikasi,
        a,
        admin_kullanicisi,
        sequence_no=1,
        status=SubcontractorPaymentStatus.approved,
        miktar=Decimal("10"),
    )
    await _hakedis(
        seeded_db,
        hakedis_fabrikasi,
        a,
        admin_kullanicisi,
        sequence_no=2,
        status=SubcontractorPaymentStatus.pending_approval,
        miktar=Decimal("5"),
        kalem_index=1,
    )
    # B: ödenmiş 2×21.500 = 43.000 (2026/7) · BAŞKA dönemde ödenmiş 1×21.500 = 21.500
    await _hakedis(
        seeded_db,
        hakedis_fabrikasi,
        b,
        admin_kullanicisi,
        sequence_no=1,
        status=SubcontractorPaymentStatus.paid,
        miktar=Decimal("2"),
    )
    await _hakedis(
        seeded_db,
        hakedis_fabrikasi,
        b,
        admin_kullanicisi,
        sequence_no=2,
        status=SubcontractorPaymentStatus.paid,
        miktar=Decimal("1"),
        period_year=2026,
        period_month=6,
    )
    return {"proje": proje, "a": a, "b": b}


# --- 1. Dört KPI (mockup L107-121) ---


async def test_dort_kpi_altin_senaryo(
    client: AsyncClient, admin_headers: dict[str, str], kpi_verisi: dict
) -> None:
    yanit = await client.get(
        UC,
        params={
            "project_id": str(kpi_verisi["proje"].id),
            "period_year": 2026,
            "period_month": 7,
        },
        headers=admin_headers,
    )
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    # Dönem süzgeci 2026/7 → 2026/6'daki ödenmiş hakediş kümenin DIŞINDA.
    assert Decimal(govde["total_gross"]) == Decimal("267250.00")  # 215.000+9.250+43.000
    assert Decimal(govde["pending_gross"]) == Decimal("9250.00")
    assert Decimal(govde["paid_period_gross"]) == Decimal("43000.00")
    assert govde["active_subcontractor_count"] == 2
    assert govde["period_year"] == 2026
    assert govde["period_month"] == 7


async def test_donem_suzgeci_yoksa_tum_donemler_toplanir(
    client: AsyncClient, admin_headers: dict[str, str], kpi_verisi: dict
) -> None:
    """Süzgeçsiz "Toplam Hakediş" tüm dönemleri kapsar; "Bu Ay Ödenen" ise
    İÇİNDE BULUNULAN aya iner (kartın etiketi "Bu Ay"dır — etkin dönem yanıtta
    ECHO edilir ki ekran hangi ayı gösterdiğini bilsin)."""
    yanit = await client.get(
        UC, params={"project_id": str(kpi_verisi["proje"].id)}, headers=admin_headers
    )
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert Decimal(govde["total_gross"]) == Decimal("288750.00")  # +21.500 (2026/6)
    # Uygulama "bu ay"ı GÖRÜNTÜLEME saat diliminden (İstanbul) okur; UTC'yle kıyaslanırsa
    # TR 00:00-03:00 arasında ayın 1'inde test bir önceki ayı bekleyip kırmızı olur.
    bugun = timezone.today()
    assert govde["period_year"] == bugun.year
    assert govde["period_month"] == bugun.month
    # Fixture verisi 2026/7 ve 2026/6 dönemlerinde; bugünün ayına ait ödeme yoksa 0.
    beklenen = (
        Decimal("43000.00")
        if (bugun.year, bugun.month) == (2026, 7)
        else Decimal("21500.00")
        if (bugun.year, bugun.month) == (2026, 6)
        else Decimal("0.00")
    )
    assert Decimal(govde["paid_period_gross"]) == beklenen


def _saati_dondur(monkeypatch: pytest.MonkeyPatch, anlik_utc: datetime) -> None:
    """`summary.datetime.now(tz)` sabit bir UTC anını istenen saat dilimine çevirir."""

    class _DonukSaat(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206 — datetime.now imzası
            return anlik_utc.astimezone(tz) if tz is not None else anlik_utc

    monkeypatch.setattr(summary, "datetime", _DonukSaat)


@pytest.mark.parametrize(
    ("anlik_utc", "beklenen"),
    [
        # İstanbul 30.09 23:59 — UTC'de de eylül: iki saat dilimi aynı ayda.
        (datetime(2026, 9, 30, 20, 59, tzinfo=UTC), (2026, 9)),
        # İstanbul 01.10 00:30 — UTC hâlâ 30.09: etkin dönem İSTANBUL'un ayı olmalı.
        (datetime(2026, 9, 30, 21, 30, tzinfo=UTC), (2026, 10)),
    ],
)
def test_etkin_donem_ay_sinirinda_istanbul_ayini_izler(
    monkeypatch: pytest.MonkeyPatch, anlik_utc: datetime, beklenen: tuple[int, int]
) -> None:
    """Ay sınırında iki yön: 21:00 UTC öncesi eylül, sonrası ekim (TR = UTC+3)."""
    _saati_dondur(monkeypatch, anlik_utc)
    assert summary.effective_period(None, None) == beklenen


async def test_bos_kume_sifir_doner(
    client: AsyncClient, admin_headers: dict[str, str], taseron_sozlesmesi
) -> None:
    """Hiç hakedişi olmayan proje 404 DEĞİL, sıfırlı özet döner (zarif düşüş)."""
    contract, proje, _ = taseron_sozlesmesi
    yanit = await client.get(UC, params={"project_id": str(proje.id)}, headers=admin_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert Decimal(govde["total_gross"]) == Decimal("0.00")
    assert Decimal(govde["pending_gross"]) == Decimal("0.00")
    assert Decimal(govde["paid_period_gross"]) == Decimal("0.00")
    assert govde["active_subcontractor_count"] == 0


# --- 2. Süzgeçler liste ucuyla AYNI (spec §6) ---


async def test_taseron_aramasi_kumeyi_daraltir(
    client: AsyncClient, admin_headers: dict[str, str], kpi_verisi: dict
) -> None:
    yanit = await client.get(
        UC,
        params={"project_id": str(kpi_verisi["proje"].id), "q": "Akın"},
        headers=admin_headers,
    )
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert Decimal(govde["total_gross"]) == Decimal("224250.00")  # 215.000 + 9.250
    assert govde["active_subcontractor_count"] == 1


async def test_durum_suzgeci_uygulanir(
    client: AsyncClient, admin_headers: dict[str, str], kpi_verisi: dict
) -> None:
    """Durum süzgeci liste ucuyla AYNI kümeyi verir — KPI'lar o kümeden okunur."""
    yanit = await client.get(
        UC,
        params={"project_id": str(kpi_verisi["proje"].id), "status": "pending_approval"},
        headers=admin_headers,
    )
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert Decimal(govde["total_gross"]) == Decimal("9250.00")
    assert Decimal(govde["pending_gross"]) == Decimal("9250.00")
    assert Decimal(govde["paid_period_gross"]) == Decimal("0.00")


async def test_proje_suzgeci_baska_projeyi_disarida_birakir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    kpi_verisi: dict,
    seeded_db: AsyncSession,
    taseron_sozlesmesi_fabrikasi,
    hakedis_fabrikasi,
    admin_kullanicisi: User,
) -> None:
    baska, _, _ = await taseron_sozlesmesi_fabrikasi("THK-S03")
    await _hakedis(
        seeded_db,
        hakedis_fabrikasi,
        baska,
        admin_kullanicisi,
        sequence_no=1,
        status=SubcontractorPaymentStatus.approved,
        miktar=Decimal("100"),
    )
    yanit = await client.get(
        UC, params={"project_id": str(kpi_verisi["proje"].id)}, headers=admin_headers
    )
    assert yanit.status_code == 200, yanit.text
    assert Decimal(yanit.json()["total_gross"]) == Decimal("288750.00")


# --- 3. Kapsam + kapı (spec §9.0, §6) ---


async def test_kapsam_disi_proje_ozete_girmez(
    client: AsyncClient,
    kisitli_headers: dict[str, str],
    kpi_verisi: dict,
) -> None:
    """`kisitli_headers` yalnız `kisitli_proje`yi görür — süzgeçsiz çağrıda bile
    görünmeyen projenin hakedişleri toplama GİRMEZ."""
    yanit = await client.get(UC, headers=kisitli_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert Decimal(govde["total_gross"]) == Decimal("0.00")
    assert govde["active_subcontractor_count"] == 0


async def test_izinsiz_rol_403(
    client: AsyncClient, hr_headers: dict[str, str], kpi_verisi: dict
) -> None:
    yanit = await client.get(UC, headers=hr_headers)
    assert yanit.status_code == 403, yanit.text


async def test_summary_yolu_detay_ucuyla_carpismaz(
    client: AsyncClient, admin_headers: dict[str, str], gorunmeyen_hakedis: uuid.UUID
) -> None:
    """`/summary` sabiti `/{payment_id}` şablonundan ÖNCE tanımlanmalıdır; aksi
    hâlde UUID ayrıştırma hatasıyla 422 döner (rota sırası tuzağı)."""
    ozet = await client.get(UC, headers=admin_headers)
    assert ozet.status_code == 200, ozet.text
    detay = await client.get(
        f"/subcontractor-progress-payments/{gorunmeyen_hakedis}", headers=admin_headers
    )
    assert detay.status_code == 200, detay.text

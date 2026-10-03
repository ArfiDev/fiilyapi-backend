"""KAT-B2.4 (K5/K8/K11) — isveren hakedis satirinda Bakanlik poz no SNAPSHOT'i.

`progress_payment_lines.source_code` bagli isveren sozlesme kaleminin kodunun KOPYASIDIR
(snapshot besliyi altiliya cikarir): olusturma (POST iç içe `lines[]`) ve PUT AYNI kurucudan gecer;
sonradan kalem kodu degisse satir eski kodu korur; yalniz `refresh-prices` tazeler (K8). Istemciden
ALINMAZ (422). Detay grup toplami yanitinda alan YOKTUR (K11).
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.contracts.models import EmployerContractItem
from app.modules.progress_payments.models import ProgressPaymentStatus
from app.modules.projects.models import Project, ProjectContract
from app.modules.sites.models import Site

from ._lines import _satir

pytestmark = pytest.mark.asyncio

KOD = "15.100.1001"
YENI_KOD = "15.100.2002"


@pytest.fixture
async def kodlu_kalem(
    seeded_db: AsyncSession, hakedis_kalemi: tuple[EmployerContractItem, str]
) -> EmployerContractItem:
    item, _ = hakedis_kalemi
    item.source_code = KOD
    await seeded_db.flush()
    return item


async def _put(client, headers, payment_id, item, site, quantity="10", **ekstra):
    govde = _satir(item.id, site.id, quantity) | ekstra
    return await client.put(
        f"/progress-payments/{payment_id}/lines", json={"lines": [govde]}, headers=headers
    )


async def test_put_yeni_satir_kalemin_kodunu_snapshot_alir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    taslak_hakedis: uuid.UUID,
    hakedis_santiyesi: Site,
    kodlu_kalem: EmployerContractItem,
) -> None:
    yanit = await _put(client, admin_headers, taslak_hakedis, kodlu_kalem, hakedis_santiyesi)
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["lines"][0]["source_code"] == KOD


async def test_olusturma_ic_ice_satir_kalemin_kodunu_snapshot_alir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    hakedis_sozlesmesi: tuple[Project, ProjectContract],
    hakedis_santiyesi: Site,
    kodlu_kalem: EmployerContractItem,
) -> None:
    """Olusturma ve PUT ayni `_new_line` kurucusundan gecer: ikisi de kodu tasir."""
    project, _ = hakedis_sozlesmesi
    yanit = await client.post(
        f"/projects/{project.id}/progress-payments",
        json={"lines": [_satir(kodlu_kalem.id, hakedis_santiyesi.id, "5")]},
        headers=admin_headers,
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["lines"][0]["source_code"] == KOD


async def test_kodsuz_kalemden_satir_null_kod_tasir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    taslak_hakedis: uuid.UUID,
    hakedis_santiyesi: Site,
    hakedis_kalemi: tuple[EmployerContractItem, str],
) -> None:
    item, _ = hakedis_kalemi
    assert item.source_code is None
    yanit = await _put(client, admin_headers, taslak_hakedis, item, hakedis_santiyesi)
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["lines"][0]["source_code"] is None


async def test_kalem_kodu_sonradan_degisince_mevcut_satir_eski_kodu_korur(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    taslak_hakedis: uuid.UUID,
    hakedis_santiyesi: Site,
    kodlu_kalem: EmployerContractItem,
) -> None:
    ilk = await _put(client, admin_headers, taslak_hakedis, kodlu_kalem, hakedis_santiyesi)
    satir_id = ilk.json()["lines"][0]["id"]

    kodlu_kalem.source_code = YENI_KOD  # DB'de dogrudan degisim (API'de immutable)
    await seeded_db.flush()

    ikinci = await _put(client, admin_headers, taslak_hakedis, kodlu_kalem, hakedis_santiyesi, "20")
    satir = ikinci.json()["lines"][0]
    assert satir["id"] == satir_id
    assert satir["source_code"] == KOD  # PUT snapshot'i TAZELEMEZ


async def test_refresh_kod_degisimini_tazeler_ve_tek_basina_degisti_sayilir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    hakedis_fabrikasi,
    kodlu_kalem: EmployerContractItem,
) -> None:
    """K8: beşli → altılı. Hiçbir başka alan değişmeden YALNIZ kod değişince satır sayılır."""
    payment_id = await hakedis_fabrikasi(ProgressPaymentStatus.draft)
    # fabrika kodu kopyalamaz (satir NULL) → ilk tazeleme NULL→KOD'u doldurur
    yanit = await client.post(
        f"/progress-payments/{payment_id}/refresh-prices", headers=admin_headers
    )
    assert yanit.json() == {"refreshed_count": 1}
    detay = (await client.get(f"/progress-payments/{payment_id}", headers=admin_headers)).json()
    assert detay["lines"][0]["source_code"] == KOD

    kodlu_kalem.source_code = YENI_KOD
    await seeded_db.flush()
    yanit = await client.post(
        f"/progress-payments/{payment_id}/refresh-prices", headers=admin_headers
    )
    assert yanit.json() == {"refreshed_count": 1}
    detay = (await client.get(f"/progress-payments/{payment_id}", headers=admin_headers)).json()
    assert detay["lines"][0]["source_code"] == YENI_KOD
    # degismediyse no-op: sayac 0
    yanit = await client.post(
        f"/progress-payments/{payment_id}/refresh-prices", headers=admin_headers
    )
    assert yanit.json() == {"refreshed_count": 0}


async def test_istemci_govdesinde_source_code_422(
    client: AsyncClient,
    admin_headers: dict[str, str],
    hakedis_sozlesmesi: tuple[Project, ProjectContract],
    taslak_hakedis: uuid.UUID,
    hakedis_santiyesi: Site,
    kodlu_kalem: EmployerContractItem,
) -> None:
    project, _ = hakedis_sozlesmesi
    for deger in ("99.999", None):
        put = await _put(
            client, admin_headers, taslak_hakedis, kodlu_kalem, hakedis_santiyesi,
            source_code=deger,
        )  # fmt: skip
        assert put.status_code == 422, put.text
        post = await client.post(
            f"/projects/{project.id}/progress-payments",
            json={
                "lines": [
                    _satir(kodlu_kalem.id, hakedis_santiyesi.id, "5") | {"source_code": deger}
                ]
            },
            headers=admin_headers,
        )
        assert post.status_code == 422, post.text


async def test_detay_grup_toplami_yanitinda_source_code_YOK_satirda_var(
    client: AsyncClient,
    admin_headers: dict[str, str],
    taslak_hakedis: uuid.UUID,
    hakedis_santiyesi: Site,
    kodlu_kalem: EmployerContractItem,
) -> None:
    """K11: grup toplami ekrani/yaniti Bakanlik no GOSTERMEZ; satir snapshot'i okuma
    semasinda durur (tuketici: ileride cikti)."""
    await _put(client, admin_headers, taslak_hakedis, kodlu_kalem, hakedis_santiyesi)
    detay = (await client.get(f"/progress-payments/{taslak_hakedis}", headers=admin_headers)).json()
    assert detay["groups"], "grup toplami bos olmamali"
    for grup in detay["groups"]:
        assert "source_code" not in grup
    assert "source_code" not in detay
    assert detay["lines"][0]["source_code"] == KOD


async def test_snapshot_kodu_para_alanlarini_etkilemez(
    client: AsyncClient,
    admin_headers: dict[str, str],
    taslak_hakedis: uuid.UUID,
    hakedis_santiyesi: Site,
    kodlu_kalem: EmployerContractItem,
) -> None:
    satir = (
        await _put(client, admin_headers, taslak_hakedis, kodlu_kalem, hakedis_santiyesi, "10")
    ).json()["lines"][0]
    assert Decimal(satir["line_total"]) == Decimal("18500.00")
    assert satir["code"] == "03.001"  # hakedis `code`u (poz no) DEGISMEZ

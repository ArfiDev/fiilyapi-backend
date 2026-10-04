"""T2 — `site_diary` izin kapısı + `visible_projects` kapsamı (IDOR).

İKİ KATMAN ayrı ayrı kanıtlanır (spec §3):
1. **İzin kapısı** (`require_permission("site_diary", …)`) — rol seviyesi yetmezse
   403; kapsam sorgusuna hiç GİRİLMEZ. PM (`_V`) salt okur, İK (`_N`) hiç göremez.
2. **Kapsam süzgeci** (`visible_projects`) — görünmeyen projenin GERÇEK kaydı ile
   var OLMAYAN kimlik AYIRT EDİLEMEZ 404 döner (403 DEĞİL).
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.modules.site_diary import guards
from tests.site_diary.conftest import VARSAYILAN_TARIH

pytestmark = pytest.mark.asyncio

_GOVDE = {"entry_date": VARSAYILAN_TARIH.isoformat()}


# --- Katman 1: izin kapısı ---


async def test_ik_rolu_okumada_da_403(
    client: AsyncClient, hr_headers: dict[str, str], santiye
) -> None:
    """`hr_manager` matriste `site_diary=_N` — liste bile göremez."""
    site, _, _ = santiye
    yanit = await client.get(f"/sites/{site.id}/diary", headers=hr_headers)
    assert yanit.status_code == 403, yanit.text


async def test_ik_rolu_yazmada_403(
    client: AsyncClient, hr_headers: dict[str, str], santiye
) -> None:
    site, _, _ = santiye
    yanit = await client.post(f"/sites/{site.id}/diary", json=_GOVDE, headers=hr_headers)
    assert yanit.status_code == 403, yanit.text


async def test_pm_okuyabilir(
    client: AsyncClient, pm_headers: dict[str, str], santiye, gunluk_fabrikasi, admin_kullanicisi
) -> None:
    """PM (`site_diary=_V`) kendi projesinin günlüklerini GÖRÜR."""
    site, _, _ = santiye
    entry = await gunluk_fabrikasi(
        site, admin_kullanicisi, lines=[("01.001", Decimal("1.000"), Decimal("10.00"))]
    )
    liste = await client.get(f"/sites/{site.id}/diary", headers=pm_headers)
    assert liste.status_code == 200, liste.text
    assert liste.json()["total"] == 1
    detay = await client.get(f"/diary/{entry.id}", headers=pm_headers)
    assert detay.status_code == 200, detay.text


async def test_pm_salt_okur_post_403(
    client: AsyncClient, pm_headers: dict[str, str], santiye
) -> None:
    site, _, _ = santiye
    yanit = await client.post(f"/sites/{site.id}/diary", json=_GOVDE, headers=pm_headers)
    assert yanit.status_code == 403, yanit.text


async def test_pm_salt_okur_patch_403(
    client: AsyncClient, pm_headers: dict[str, str], santiye, gunluk_fabrikasi, admin_kullanicisi
) -> None:
    site, _, _ = santiye
    entry = await gunluk_fabrikasi(site, admin_kullanicisi)
    yanit = await client.patch(f"/diary/{entry.id}", json={"work_done": "x"}, headers=pm_headers)
    assert yanit.status_code == 403, yanit.text


async def test_pm_salt_okur_delete_403(
    client: AsyncClient, pm_headers: dict[str, str], santiye, gunluk_fabrikasi, admin_kullanicisi
) -> None:
    site, _, _ = santiye
    entry = await gunluk_fabrikasi(site, admin_kullanicisi)
    yanit = await client.delete(f"/diary/{entry.id}", headers=pm_headers)
    assert yanit.status_code == 403, yanit.text


async def test_sef_yazabilir(client: AsyncClient, sef_headers: dict[str, str], santiye) -> None:
    site, _, _ = santiye
    yanit = await client.post(f"/sites/{site.id}/diary", json=_GOVDE, headers=sef_headers)
    assert yanit.status_code == 201, yanit.text


# --- Katman 2: kapsam süzgeci (IDOR) ---


async def test_gorunmeyen_santiyenin_listesi_404(
    client: AsyncClient, sef_headers: dict[str, str], gorunmeyen_santiye
) -> None:
    yanit = await client.get(f"/sites/{gorunmeyen_santiye.id}/diary", headers=sef_headers)
    assert yanit.status_code == 404, yanit.text
    assert yanit.json()["detail"] == guards.SITE_MISSING


async def test_gorunmeyen_santiyeye_post_404(
    client: AsyncClient, sef_headers: dict[str, str], gorunmeyen_santiye
) -> None:
    """403 DEĞİL 404: yetki reddi, kaydın VARLIĞINI sızdırırdı."""
    yanit = await client.post(
        f"/sites/{gorunmeyen_santiye.id}/diary", json=_GOVDE, headers=sef_headers
    )
    assert yanit.status_code == 404, yanit.text
    assert yanit.json()["detail"] == guards.SITE_MISSING


async def test_olmayan_santiye_ile_ayni_404_govdesi(
    client: AsyncClient, sef_headers: dict[str, str], gorunmeyen_santiye
) -> None:
    """Var olmayan şantiye ile görünmeyen şantiye AYNI gövdeyi döner."""
    olmayan = await client.get(f"/sites/{uuid.uuid4()}/diary", headers=sef_headers)
    gorunmeyen = await client.get(f"/sites/{gorunmeyen_santiye.id}/diary", headers=sef_headers)
    assert olmayan.status_code == gorunmeyen.status_code == 404
    assert olmayan.json() == gorunmeyen.json()


async def test_gorunmeyen_gunluk_detayi_404(
    client: AsyncClient, sef_headers: dict[str, str], gorunmeyen_gunluk: uuid.UUID
) -> None:
    yanit = await client.get(f"/diary/{gorunmeyen_gunluk}", headers=sef_headers)
    assert yanit.status_code == 404, yanit.text
    assert yanit.json()["detail"] == guards.ENTRY_MISSING


async def test_gorunmeyen_gunluge_patch_404(
    client: AsyncClient, sef_headers: dict[str, str], gorunmeyen_gunluk: uuid.UUID
) -> None:
    yanit = await client.patch(
        f"/diary/{gorunmeyen_gunluk}", json={"work_done": "x"}, headers=sef_headers
    )
    assert yanit.status_code == 404, yanit.text
    assert yanit.json()["detail"] == guards.ENTRY_MISSING


async def test_gorunmeyen_gunluge_delete_403_kapi_once_kosar(
    client: AsyncClient, sef_headers: dict[str, str], gorunmeyen_gunluk: uuid.UUID
) -> None:
    yanit = await client.delete(f"/diary/{gorunmeyen_gunluk}", headers=sef_headers)
    # SIL-B1: DELETE kapısı (yalnız Sistem Yöneticisi) önce koşar → 403, varlık sızmaz.
    assert yanit.status_code == 403, yanit.text


async def test_izin_kapisi_kapsamdan_ONCE_kosar(
    client: AsyncClient, hr_headers: dict[str, str], gorunmeyen_gunluk: uuid.UUID
) -> None:
    """İK için cevap 404 değil 403'tür: kapı, kapsam sorgusuna girilmeden kapanır."""
    yanit = await client.get(f"/diary/{gorunmeyen_gunluk}", headers=hr_headers)
    assert yanit.status_code == 403, yanit.text

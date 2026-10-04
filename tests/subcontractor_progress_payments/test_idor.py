"""T2 — spec §9.0 negatif seti (işveren `tests/progress_payments/test_idor.py` deseni).

Görünmeyen kayıt ile OLMAYAN kimlik AYIRT EDİLEMEZ 404 döner; modül izni
yetersizse 403 (kapı, görünürlükten ÖNCE koşar).
"""

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_gorunmeyen_hakedis_ile_olmayan_id_ayni_yanit(
    client: AsyncClient, kisitli_headers: dict[str, str], gorunmeyen_hakedis: uuid.UUID
) -> None:
    gercek = await client.get(
        f"/subcontractor-progress-payments/{gorunmeyen_hakedis}", headers=kisitli_headers
    )
    sahte = await client.get(
        f"/subcontractor-progress-payments/{uuid.uuid4()}", headers=kisitli_headers
    )
    assert gercek.status_code == sahte.status_code == 404
    assert gercek.json() == sahte.json()


async def test_gorunmeyen_hakedis_patch_404(
    client: AsyncClient, kisitli_headers: dict[str, str], gorunmeyen_hakedis: uuid.UUID
) -> None:
    yanit = await client.patch(
        f"/subcontractor-progress-payments/{gorunmeyen_hakedis}",
        json={"description": "x"},
        headers=kisitli_headers,
    )
    assert yanit.status_code == 404


async def test_gorunmeyen_hakedis_delete_403_kapi_once_kosar(
    client: AsyncClient, kisitli_headers: dict[str, str], gorunmeyen_hakedis: uuid.UUID
) -> None:
    """SIL-B1: kapı (yalnız Sistem Yöneticisi) önce koşar; görünmeyen ve var olmayan kimlik AYNI
    403'ü alır (varlık sızmaz)."""
    gercek = await client.delete(
        f"/subcontractor-progress-payments/{gorunmeyen_hakedis}", headers=kisitli_headers
    )
    sahte = await client.delete(
        f"/subcontractor-progress-payments/{uuid.uuid4()}", headers=kisitli_headers
    )
    assert gercek.status_code == sahte.status_code == 403
    assert gercek.json() == sahte.json()


async def test_sef_gorunmeyen_sozlesmede_olusturamaz_404(
    client: AsyncClient, sef_headers: dict[str, str], gorunmeyen_sozlesme: uuid.UUID
) -> None:
    """Şef `scope=project`: atanmamış projenin sözleşmesi 403 DEĞİL 404."""
    yanit = await client.post(
        f"/subcontractor-contracts/{gorunmeyen_sozlesme}/progress-payments",
        json={},
        headers=sef_headers,
    )
    assert yanit.status_code == 404


async def test_olmayan_sozlesmede_olusturma_404(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    yanit = await client.post(
        f"/subcontractor-contracts/{uuid.uuid4()}/progress-payments",
        json={},
        headers=admin_headers,
    )
    assert yanit.status_code == 404


async def test_yetkisiz_rol_403(
    client: AsyncClient, hr_headers: dict[str, str], gorunmeyen_hakedis: uuid.UUID
) -> None:
    """İK matris satırı `_N`: modül izni yetersiz -> 403 (görünürlükten ÖNCE)."""
    yanit = await client.get(
        f"/subcontractor-progress-payments/{gorunmeyen_hakedis}", headers=hr_headers
    )
    assert yanit.status_code == 403


async def test_yetkisiz_rol_olusturma_403(
    client: AsyncClient, hr_headers: dict[str, str], gorunmeyen_sozlesme: uuid.UUID
) -> None:
    yanit = await client.post(
        f"/subcontractor-contracts/{gorunmeyen_sozlesme}/progress-payments",
        json={},
        headers=hr_headers,
    )
    assert yanit.status_code == 403


async def test_liste_gorunmeyen_projeyi_gostermez(
    client: AsyncClient, kisitli_headers: dict[str, str], gorunmeyen_hakedis: uuid.UUID
) -> None:
    yanit = await client.get("/subcontractor-progress-payments", headers=kisitli_headers)
    assert yanit.status_code == 200
    ids = [item["id"] for item in yanit.json()["items"]]
    assert str(gorunmeyen_hakedis) not in ids
    assert yanit.json()["total"] == 0

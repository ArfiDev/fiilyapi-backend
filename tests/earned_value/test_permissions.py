"""PLN-B1.3 — izin matrisi (spec §3.9 B1-8): `earned_value` = [A, F, APR, DRF, N, V, F, N].

Kapilar (`access.py`): VIEW=view · WRITE=draft · CATALOG=full · ADMIN=admin. Beklenen
kume seed satirindan elle turetildi (`conftest.CAN_*`); seed degisirse bu test kirmizi
olur — sessizce yeni matrise uymaz.

Ornek kararlar: muhasebe okur ama ayar yazamaz · saha muhendisi ayar yazar ama katalog
yazamaz · santiye sefi (approve < full) katalog yazamaz · disiplini yalniz sistem
yoneticisi siler.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from app.modules.catalog.models import EvDiscipline
from app.modules.sites.models import Site

from .conftest import (
    CAN_ADMIN,
    CAN_CATALOG,
    CAN_VIEW,
    CAN_WRITE,
    ROLES,
    LoginFn,
    settings_url,
)
from .test_settings import body as settings_body


def _expect(role: str, allowed: set[str], ok: int) -> int:
    return ok if role in allowed else 403


@pytest.mark.parametrize("role", ROLES)
async def test_okuma_uclari(
    client: AsyncClient, login: LoginFn, role: str, santiye: Site, kab: EvDiscipline
) -> None:
    headers = await login(role)
    expected = _expect(role, CAN_VIEW, 200)
    for url in ("/earned-value/disciplines", "/earned-value/catalog", settings_url(santiye.id)):
        resp = await client.get(url, headers=headers)
        assert resp.status_code == expected, (role, url, resp.text)


@pytest.mark.parametrize("role", ROLES)
async def test_ayar_yazma_draft_yeter(
    client: AsyncClient, login: LoginFn, role: str, santiye: Site
) -> None:
    headers = await login(role)
    resp = await client.put(settings_url(santiye.id), json=settings_body(), headers=headers)
    assert resp.status_code == _expect(role, CAN_WRITE, 200), (role, resp.text)


@pytest.mark.parametrize("role", ROLES)
async def test_katalog_ve_disiplin_yazma_full_ister(
    client: AsyncClient, login: LoginFn, role: str, kab: EvDiscipline, katalog_fabrikasi
) -> None:
    headers = await login(role)
    item = await katalog_fabrikasi(kab, "Kalıp", uom="m²")
    tag = uuid.uuid4().hex[:6].upper()
    calls = [
        (
            "post",
            "/earned-value/disciplines",
            {
                "code": f"D{tag}",
                "name": "Yeni",
                "color": "#000000",
                "default_contractor_type": "own",
            },
            201,
        ),
        ("patch", f"/earned-value/disciplines/{kab.id}", {"name": "Kaba"}, 200),
        (
            "post",
            "/earned-value/catalog",
            {
                "discipline_id": str(kab.id),
                "name": f"İş {tag}",
                "uom": "m³",
                "standard_unit_mhr": "1.5",
                "default_contractor_type": "own",
            },
            201,
        ),
        ("patch", f"/earned-value/catalog/{item.id}", {"standard_unit_mhr": "0.9"}, 200),
        # Kapiyi gecen icin B1'de 409 (gerceklesen yok); gecemeyen 403.
        ("post", f"/earned-value/catalog/{item.id}/adopt-actual", None, 409),
    ]
    for method, url, payload, ok in calls:
        kwargs = {} if payload is None else {"json": payload}
        resp = await getattr(client, method)(url, headers=headers, **kwargs)
        assert resp.status_code == _expect(role, CAN_CATALOG, ok), (role, method, url, resp.text)


@pytest.mark.parametrize("role", ROLES)
async def test_disiplin_silme_yalniz_admin(
    client: AsyncClient, login: LoginFn, role: str, disiplin_fabrikasi
) -> None:
    headers = await login(role)
    row = await disiplin_fabrikasi(f"S{uuid.uuid4().hex[:6].upper()}")
    resp = await client.delete(f"/earned-value/disciplines/{row.id}", headers=headers)
    assert resp.status_code == _expect(role, CAN_ADMIN, 204), (role, resp.text)


async def test_kimliksiz_istek_401(client: AsyncClient, santiye: Site) -> None:
    for url in ("/earned-value/disciplines", "/earned-value/catalog", settings_url(santiye.id)):
        assert (await client.get(url)).status_code == 401

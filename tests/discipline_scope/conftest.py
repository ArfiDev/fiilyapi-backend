"""DSC-B1 fikstürleri — ortak "disiplin dünyası" (`tests/_disiplin_dunyasi.py`).

Kök `tests/conftest.py`in `client`/`seeded_db`/`user_factory`/`project_factory` fikstürleri
üzerine kurulur; her test kendi dünyasını kurar ve kök savepoint'te geri alınır.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests._disiplin_dunyasi import Dunya, kur


@pytest.fixture
async def dunya(
    request: pytest.FixtureRequest,
    seeded_db: AsyncSession,
    client: AsyncClient,
    user_factory,
    project_factory,
) -> Dunya:
    """Modülde `YAZANLAR = True` varsa (B2) F1 yazan aktörleri de kurulur."""
    yazanlar = bool(getattr(request.module, "YAZANLAR", False))
    return await kur(seeded_db, client, user_factory, project_factory, yazanlar=yazanlar)


@pytest.fixture
def atamasiz(dunya: Dunya) -> dict[str, str]:
    return dunya.baslik["atamasiz"]


@pytest.fixture
def civil(dunya: Dunya) -> dict[str, str]:
    return dunya.baslik["civil"]


@pytest.fixture
def elek(dunya: Dunya) -> dict[str, str]:
    return dunya.baslik["elek"]


@pytest.fixture
def civil_yazar(dunya: Dunya) -> dict[str, str]:
    return dunya.baslik["civil_yazar"]


@pytest.fixture
def elek_yazar(dunya: Dunya) -> dict[str, str]:
    return dunya.baslik["elek_yazar"]


@pytest.fixture
def yazar_atamasiz(dunya: Dunya) -> dict[str, str]:
    return dunya.baslik["yazar_atamasiz"]


@pytest.fixture
def admin_kisitli(dunya: Dunya) -> dict[str, str]:
    return dunya.baslik["admin_kisitli"]

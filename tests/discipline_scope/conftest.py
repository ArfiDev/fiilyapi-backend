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
    seeded_db: AsyncSession, client: AsyncClient, user_factory, project_factory
) -> Dunya:
    return await kur(seeded_db, client, user_factory, project_factory)


@pytest.fixture
def atamasiz(dunya: Dunya) -> dict[str, str]:
    return dunya.baslik["atamasiz"]


@pytest.fixture
def civil(dunya: Dunya) -> dict[str, str]:
    return dunya.baslik["civil"]


@pytest.fixture
def elek(dunya: Dunya) -> dict[str, str]:
    return dunya.baslik["elek"]

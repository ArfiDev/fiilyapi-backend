"""DSC-B1 fikstürleri — ortak "disiplin dünyası" (`tests/_disiplin_dunyasi.py`).

Kök `tests/conftest.py`in `client`/`seeded_db`/`user_factory`/`project_factory` fikstürleri
üzerine kurulur; her test kendi dünyasını kurar ve kök savepoint'te geri alınır.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests._disiplin_dunyasi import Dunya, kur
from tests.discipline_scope._b3_dunya import FIXED_TODAY, genislet


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


@pytest.fixture
def sabit_bugun(monkeypatch: pytest.MonkeyPatch):
    """B3: `today()` her yerde `FIXED_TODAY` (QURR varsayılan haftası, ayar önizlemesi)."""
    import app.core.timezone as tz
    import app.modules.earned_value.report_qurr as qurr
    import app.modules.earned_value.settings_router as ayar

    for modul in (tz, qurr, ayar):
        monkeypatch.setattr(modul, "today", lambda: FIXED_TODAY)
    return FIXED_TODAY


@pytest.fixture
async def dunya_b3(
    sabit_bugun,
    seeded_db: AsyncSession,
    client: AsyncClient,
    user_factory,
    project_factory,
) -> Dunya:
    """B3 dünyası: F1 yazan aktörler + `genislet()` (B3 modülleri `YAZANLAR = True` koymaz)."""
    d = await kur(seeded_db, client, user_factory, project_factory, yazanlar=True)
    return await genislet(seeded_db, client, user_factory, d)


@pytest.fixture
def iki_disiplin(dunya_b3: Dunya) -> dict[str, str]:
    return dunya_b3.baslik["iki_disiplin"]


@pytest.fixture
def pm_atamasiz(dunya_b3: Dunya) -> dict[str, str]:
    return dunya_b3.baslik["pm_atamasiz"]


# --- DSC-B4 (dünya ayrı modülde; varsayılan `kur`a eklenmez) ---
from tests.discipline_scope._b4_dunya import (  # noqa: E402,F401
    dunya_b4,
    pm_atamasiz_b4,
    sabit_bugun_b4,
)

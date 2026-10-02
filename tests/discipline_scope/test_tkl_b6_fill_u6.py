"""TKL-B6.4 — "Sozlesmeden doldur" KISITLIYA 403 (`RequireUnrestricted`, DSC-B2 U6); ayni roldeki
ATAMASIZ es 2xx alir (F1 pozitif kontrol). Baglayici bekci `U6_ROTALARI` (rota listesi)."""

from __future__ import annotations

from httpx import AsyncClient

from tests._disiplin_dunyasi import Dunya
from tests.discipline_scope.test_b2_ev_u6 import _ev, _iki_yon

YAZANLAR = True  # F1 yazan aktorler (conftest `dunya`)


async def test_sozlesmeden_doldur_kisitliya_403(
    client: AsyncClient, dunya: Dunya, civil_yazar, yazar_atamasiz
) -> None:
    await _iki_yon(
        client, "POST", f"{_ev(dunya, '/budget')}/fill-from-contract", civil_yazar, yazar_atamasiz
    )

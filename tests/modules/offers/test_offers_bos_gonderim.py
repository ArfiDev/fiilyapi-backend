"""TKL-B4.3 — kalemsiz revizyon gonderilemez (SO-9); fiyatsiz kalemli revizyon gonderilebilir."""

from __future__ import annotations

import pytest

from ._offers import gecis, grup, kalem, revizyon, teklif

pytestmark = pytest.mark.asyncio


async def test_kalemsiz_revizyon_send_422_durum_degismez(client, admin, isveren) -> None:
    o = await teklif(client, admin, isveren)
    resp = await gecis(client, admin, o["id"], "send", dolu=False)
    assert resp.status_code == 422, resp.text
    assert "Teklifte kalem yok" in resp.text
    rev = await revizyon(client, admin, o["id"])
    assert rev["status"] == "draft" and rev["sent_at"] is None


async def test_bos_grup_kalem_sayilmaz_422(client, admin, isveren) -> None:
    o = await teklif(client, admin, isveren)
    await grup(client, admin, o["id"])
    resp = await gecis(client, admin, o["id"], "send", dolu=False)
    assert resp.status_code == 422, resp.text


async def test_tek_fiyatsiz_kalemle_send_200(client, admin, isveren, katalog) -> None:
    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    await kalem(client, admin, o["id"], g["id"], katalog[1].id, cost_unit_price=None)
    resp = await gecis(client, admin, o["id"], "send", dolu=False)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "sent"

"""IZN-B4c — puantaj modülü hassas alan maskesi.

GECE KARARI: puantaj bir SAAT kaydıdır, tutar değil. Saat, adam-gün, normal/FM/toplam saat ve
haftalık normal saat `Hassas.yok`tur; modülde ücret/maliyet/kişisel kimlik alanı YOKTUR. Gizlenecek
alan olmadığı için "gizli rolde null" testi yoktur: bunun yerine EN KISITLAYICI rolde (altı
kategorinin hepsi gizli, `tum_tutarlar` dahil) yanıtın DEĞİŞMEDİĞİ ve yazmanın 403 almadığı
sabitlenir.
Modüle ileride etiketli bir alan eklenirse bu testler kırılır ve maske testi yazılması gerekir.
"""

from io import BytesIO

import openpyxl
import pytest
from httpx import AsyncClient

from app.core.sayfalar import HiddenCategory
from app.modules.timesheet.export import SHEET_TITLE
from tests._hassas_alan import rol_gizli
from tests.timesheet.conftest import (
    AY,
    ISO_HAFTA,
    ISO_YIL,
    YIL,
    _scoped_headers,
    hafta_gunu,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def gizli_headers(client, seeded_db, user_factory, proje) -> dict[str, str]:
    """Tüm hassas kategorileri gizleyen özel rol (puantaj sayfası düzenler)."""
    await rol_gizli(seeded_db, "puantaj_gizli", set(HiddenCategory))
    return await _scoped_headers(
        client, seeded_db, user_factory, "puantaj_gizli", "gizli@ts-b4c.co", proje
    )


@pytest.fixture
async def dolu_hafta(hucre_fabrikasi, santiye, admin_kullanicisi, mehmet, ali):
    for ofset, saat in enumerate([9, 11, 9, 9, 9, 6]):
        await hucre_fabrikasi(santiye, mehmet, hafta_gunu(ofset), admin_kullanicisi, hours=saat)
    await hucre_fabrikasi(santiye, ali, hafta_gunu(0), admin_kullanicisi, hours=8)


async def _json(client: AsyncClient, yol: str, headers, **params) -> dict:
    yanit = await client.get(yol, params=params, headers=headers)
    assert yanit.status_code == 200, yanit.text
    return yanit.json()


async def test_gizli_rolde_hafta_saatleri_GORUNUR(client, gizli_headers, santiye, dolu_hafta):
    """Pozitif kontrol: saat alanları `Hassas.yok` — en kısıtlayıcı rolde bile null olmaz."""
    veri = await _json(
        client,
        f"/sites/{santiye.id}/timesheet/week",
        gizli_headers,
        iso_year=ISO_YIL,
        iso_week=ISO_HAFTA,
    )
    mehmet = next(s for s in veri["rows"] if s["full_name"] == "Mehmet Yılmaz")
    assert mehmet["cells"][0]["hours"] == "9.0"
    assert mehmet["totals"]["total_hours"] == "53.0"
    assert mehmet["totals"]["overtime_hours"] == "8.0"
    assert veri["totals"]["total_hours"] is not None
    assert veri["weekly_normal_hours"] is not None
    assert veri["normal_day_hours"] is not None
    assert veri["month_total_hours"] is not None
    assert veri["month_man_days"] is not None
    # İşçi adı/meslek GİZLENMEZ.
    assert {s["full_name"] for s in veri["rows"]} == {"Mehmet Yılmaz", "Ali Kaya"}


async def test_maskeli_rolde_hafta_yaniti_DEGISMEZ(
    client, admin_headers, gizli_headers, santiye, dolu_hafta
):
    yol = f"/sites/{santiye.id}/timesheet/week"
    sorgu = {"iso_year": ISO_YIL, "iso_week": ISO_HAFTA}
    assert await _json(client, yol, gizli_headers, **sorgu) == await _json(
        client, yol, admin_headers, **sorgu
    )


async def test_maskeli_rolde_aylik_matris_yaniti_DEGISMEZ(
    client, admin_headers, gizli_headers, santiye, dolu_hafta
):
    yol = f"/sites/{santiye.id}/timesheet"
    sorgu = {"year": YIL, "month": AY}
    gizli = await _json(client, yol, gizli_headers, **sorgu)
    assert gizli["total_hours"] is not None
    assert gizli["total_man_days"] is not None
    assert gizli == await _json(client, yol, admin_headers, **sorgu)


async def test_maskeli_rolde_excel_hucreleri_DEGISMEZ(
    client, admin_headers, gizli_headers, santiye, dolu_hafta
):
    yol = f"/sites/{santiye.id}/timesheet/export.xlsx"

    async def hucreler(headers):
        yanit = await client.get(yol, params={"year": YIL, "month": AY}, headers=headers)
        assert yanit.status_code == 200, yanit.text
        sayfa = openpyxl.load_workbook(BytesIO(yanit.content))[SHEET_TITLE]
        return [[c.value for c in satir] for satir in sayfa.iter_rows()]

    gizli = await hucreler(gizli_headers)
    assert gizli == await hucreler(admin_headers)
    assert any("9" in str(h) for satir in gizli for h in satir if h is not None)


async def test_maskeli_rol_hafta_yazarken_403_ALMAZ(client, gizli_headers, santiye, mehmet):
    """Gövdede hassas alan YOKTUR (saat `yok`): maske yazma kapısı bu rolü reddetmemeli."""
    yanit = await client.put(
        f"/sites/{santiye.id}/timesheet/week",
        params={"iso_year": ISO_YIL, "iso_week": ISO_HAFTA},
        json={
            "cells": [
                {
                    "personnel_id": str(mehmet.id),
                    "work_date": hafta_gunu(0).isoformat(),
                    "hours": "9",
                }
            ]
        },
        headers=gizli_headers,
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["rows"][0]["cells"][0]["hours"] == "9.0"

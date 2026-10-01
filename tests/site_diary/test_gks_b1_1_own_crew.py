"""GKS-B1.1 — iskelet yanıtında puantajdan türeyen ekip (`own_crew_from_timesheet`).

Kayıtsız günde frontend "puantaj girilmemiş" basmasın: iskelet ucu, detay ucuyla AYNI
türetmeyi (`read.own_crew_from_timesheet`) döndürür.
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.personnel.models import Personnel
from app.modules.site_diary.models import WorkerSource
from app.modules.timesheet.models import TimesheetEntry
from tests.site_diary._gks_b1 import GUN, olustur, onizleme

pytestmark = pytest.mark.asyncio


async def _puantaj_kur(seeded_db: AsyncSession, site, actor) -> None:  # noqa: ANN001
    kisiler = [
        ("Ali", "Kalıpçı", WorkerSource.company, "9"),
        ("Veli", "Kalıpçı", WorkerSource.company, "8"),
        ("Hasan", "Duvarcı", WorkerSource.subcontractor, "8"),
    ]
    for ad, meslek, kaynak, saat in kisiler:
        kisi = Personnel(full_name=ad, trade=meslek, source=kaynak, is_active=True, is_draft=False)
        seeded_db.add(kisi)
        await seeded_db.flush()
        seeded_db.add(
            TimesheetEntry(
                personnel_id=kisi.id,
                site_id=site.id,
                project_id=site.project_id,
                work_date=GUN,
                hours=Decimal(saat),
                created_by=actor.id,
            )
        )
    await seeded_db.flush()


def _ozet(ekip: list[dict]) -> list[tuple[str, str, int, Decimal]]:
    return [(c["trade"], c["source"], c["headcount"], Decimal(c["hours"])) for c in ekip]


BEKLENEN = [
    ("Duvarcı", "subcontractor", 1, Decimal(8)),
    ("Kalıpçı", "company", 2, Decimal(17)),
]


async def test_kayitsiz_gunde_puantaj_doluyken_iskelet_ekibi_doner(
    client: AsyncClient, admin_headers, admin_kullanicisi, santiye, seeded_db: AsyncSession
) -> None:
    site, _, _ = santiye
    await _puantaj_kur(seeded_db, site, admin_kullanicisi)

    yanit = await onizleme(client, admin_headers, site.id)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["existing_entry_id"] is None
    assert _ozet(yanit.json()["own_crew_from_timesheet"]) == BEKLENEN


async def test_puantaj_bosken_iskelet_ekibi_bos_liste(
    client: AsyncClient, admin_headers, santiye
) -> None:
    site, _, _ = santiye

    yanit = await onizleme(client, admin_headers, site.id)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["own_crew_from_timesheet"] == []


async def test_iskelet_ekibi_gunluk_detayiyla_birebir_esit(
    client: AsyncClient, admin_headers, admin_kullanicisi, santiye, seeded_db: AsyncSession
) -> None:
    site, _, _ = santiye
    await _puantaj_kur(seeded_db, site, admin_kullanicisi)
    iskelet = (await onizleme(client, admin_headers, site.id)).json()

    olusan = await olustur(client, admin_headers, site.id)
    assert olusan.status_code == 201, olusan.text
    detay = await client.get(f"/diary/{olusan.json()['id']}", headers=admin_headers)
    assert detay.status_code == 200, detay.text

    assert detay.json()["own_crew_from_timesheet"] == iskelet["own_crew_from_timesheet"]
    assert _ozet(detay.json()["own_crew_from_timesheet"]) == BEKLENEN


async def test_saha_muhendisi_iskelette_detaydakiyle_ayni_ekibi_gorur(
    client: AsyncClient,
    admin_headers,
    saha_headers,
    admin_kullanicisi,
    santiye,
    seeded_db: AsyncSession,
) -> None:
    site, _, _ = santiye
    await _puantaj_kur(seeded_db, site, admin_kullanicisi)
    olusan = await olustur(client, admin_headers, site.id)
    assert olusan.status_code == 201, olusan.text

    detay = await client.get(f"/diary/{olusan.json()['id']}", headers=saha_headers)
    iskelet = await onizleme(client, saha_headers, site.id)

    assert detay.status_code == 200, detay.text
    assert iskelet.status_code == 200, iskelet.text
    assert iskelet.json()["own_crew_from_timesheet"] == detay.json()["own_crew_from_timesheet"]
    assert _ozet(iskelet.json()["own_crew_from_timesheet"]) == BEKLENEN

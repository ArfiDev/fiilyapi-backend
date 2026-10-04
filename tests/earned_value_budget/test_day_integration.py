"""EV adaptörü ↔ ÇEKİRDEK uçları — GERÇEK kayıtla (sahte port DEĞİL).

Aynı kurgu `test_day_allocation.py` (aktif baseline + Ali 9 · Veli 8 puantaj + günlük satırı).
* Gönder (çekirdek `POST /diary/{id}/submit`) EV nedenleriyle 422 döner (B2-3, B2-4, B2-8).
* Rapor onayı günlük PATCH'ini ve puantaj yazmasını 409'la keser (B2-6).
* Taşeron firma satırı dağıtım ızgarasına kişi × saat olarak girer (B2-5).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.core.access import AccessLevel, Scope
from app.core.day_hooks import registered
from app.modules.contracts.models import Subcontractor
from app.modules.earned_value import diary_adapter
from app.modules.earned_value.models import EvReportApproval
from app.modules.roles import service as role_service
from app.modules.roles.schemas import RoleCreate
from app.modules.site_diary.models import SiteDiaryWorkerCount, WorkerSource
from tests._legacy_permission_yardimcisi import update_role_permission

from .conftest import DAY
from .test_day_allocation import _body, _day

D = Decimal


def test_ev_adapter_is_registered_on_the_core_port() -> None:
    locks, guards = registered()
    assert diary_adapter.day_lock in locks
    assert diary_adapter.submit_blockers in guards


async def _submit(client, headers, diary):  # noqa: ANN001
    return await client.post(f"/diary/{diary.id}/submit", headers=headers)


async def test_submit_is_blocked_with_ev_reasons(
    client, saha, santiye, baseline, saha_gunu
) -> None:
    resp = await _submit(client, saha, saha_gunu["diary"])
    assert resp.status_code == 422, resp.text
    reasons = resp.json()["reasons"]
    assert any("Hava" in r for r in reasons)
    assert any("dağıtılmamış" in r for r in reasons)


async def test_submit_passes_when_weather_and_allocation_complete(
    client, saha, seeded_db, santiye, boq, baseline, saha_gunu
) -> None:
    diary = saha_gunu["diary"]
    diary.weather, diary.temp_min_c, diary.temp_max_c, diary.wind_ms = (
        "sunny",
        D("12"),
        D("24"),
        D("3.5"),
    )
    await seeded_db.flush()
    body = _body(boq, saha_gunu["ali"], saha_gunu["veli"])
    assert (
        await client.put(_day(santiye, "/allocation"), headers=saha, json=body)
    ).status_code == 200
    view = (await client.get(_day(santiye), headers=saha)).json()
    assert view["submit"] == {"can_submit": True, "reasons": [], "reason_items": []}
    assert (await _submit(client, saha, diary)).status_code == 200


async def test_unallocated_with_reason_does_not_block(
    client, saha, seeded_db, santiye, boq, baseline, saha_gunu
) -> None:
    diary = saha_gunu["diary"]
    diary.weather, diary.temp_min_c, diary.temp_max_c, diary.wind_ms = (
        "cloudy",
        D("10"),
        D("18"),
        D("2"),
    )
    await seeded_db.flush()
    body = {
        **_body(boq, saha_gunu["ali"], saha_gunu["veli"]),
        "unallocated_reason": "Veli eğitimde",
    }
    body["cells"] = body["cells"][:1]  # Veli'nin 8 saati dağıtılmadı
    assert (
        await client.put(_day(santiye, "/allocation"), headers=saha, json=body)
    ).status_code == 200
    assert (await _submit(client, saha, diary)).status_code == 200


async def test_overrun_without_reason_blocks(
    client, saha, seeded_db, santiye, boq, baseline, saha_gunu
) -> None:
    diary = saha_gunu["diary"]
    diary.weather, diary.temp_min_c, diary.temp_max_c, diary.wind_ms = (
        "sunny",
        D("12"),
        D("24"),
        D("3"),
    )
    line = saha_gunu["line"]
    line.quantity = D(15)  # Bölümsüz planlı 10 → aşım
    await seeded_db.flush()
    body = _body(boq, saha_gunu["ali"], saha_gunu["veli"])
    await client.put(_day(santiye, "/allocation"), headers=saha, json=body)
    resp = await _submit(client, saha, diary)
    assert resp.status_code == 422
    assert any("aşan" in r for r in resp.json()["reasons"])
    line.overrun_reason = "Ek döküm"
    await seeded_db.flush()
    assert (await _submit(client, saha, diary)).status_code == 200


async def test_formen_cannot_submit_on_ev_site(
    client, seeded_db, user_factory, proje, santiye, baseline, saha_gunu
) -> None:
    from .conftest import _headers

    role = await role_service.create_custom_role(
        seeded_db, RoleCreate(key="formen", name="Formen", emoji="", description="")
    )
    await update_role_permission(seeded_db, role.id, "site_diary", AccessLevel.full, Scope.all)
    formen = await _headers(client, seeded_db, user_factory, "formen", "formen@ev-b2.co", proje)
    resp = await _submit(client, formen, saha_gunu["diary"])
    assert resp.status_code == 422
    assert any("formen" in r for r in resp.json()["reasons"])


async def test_report_approval_locks_core_diary_patch(
    client, saha, seeded_db, santiye, baseline, saha_gunu
) -> None:
    seeded_db.add(
        EvReportApproval(site_id=santiye.id, report_date=DAY, approved_at=datetime.now(UTC))
    )
    await seeded_db.flush()
    resp = await client.patch(
        f"/diary/{saha_gunu['diary'].id}", headers=saha, json={"work_done": "düzeltme"}
    )
    assert resp.status_code == 409
    assert "raporuyla kilitli" in resp.json()["detail"]


async def test_report_approval_locks_timesheet_change_of_that_day(
    client, sef, seeded_db, santiye, baseline, saha_gunu
) -> None:
    seeded_db.add(
        EvReportApproval(site_id=santiye.id, report_date=DAY, approved_at=datetime.now(UTC))
    )
    await seeded_db.flush()
    iso_year, iso_week, _ = DAY.isocalendar()
    cells = [
        {"personnel_id": str(saha_gunu["ali"].id), "work_date": DAY.isoformat(), "hours": "11"},
        {"personnel_id": str(saha_gunu["veli"].id), "work_date": DAY.isoformat(), "hours": "8"},
    ]
    resp = await client.put(
        f"/sites/{santiye.id}/timesheet/week",
        params={"iso_year": iso_year, "iso_week": iso_week},
        json={"cells": cells},
        headers=sef,
    )
    assert resp.status_code == 409, resp.text


async def test_subcontractor_row_enters_allocation_grid(
    client, saha, seeded_db, santiye, boq, baseline, saha_gunu
) -> None:
    firm = Subcontractor(name="Yıldız Sıva Ltd.")
    seeded_db.add(firm)
    await seeded_db.flush()
    seeded_db.add(
        SiteDiaryWorkerCount(
            entry_id=saha_gunu["diary"].id,
            trade="Sıvacı",
            source=WorkerSource.subcontractor,
            count=3,
            subcontractor_id=firm.id,
            hours=D("8"),
        )
    )
    await seeded_db.flush()
    view = (await client.get(_day(santiye), headers=saha)).json()
    row = next(r for r in view["rows"] if r["kind"] == "subcontractor")
    assert (row["label"], row["headcount"], D(row["hours"])) == ("Yıldız Sıva Ltd.", 3, 24)
    assert D(view["totals"]["source_hours"]) == 17 + 24
    firm_rows = (
        (await seeded_db.execute(select(Subcontractor).where(Subcontractor.id == firm.id)))
        .scalars()
        .all()
    )
    assert firm_rows

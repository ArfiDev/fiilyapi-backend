"""İK-2 T3 — izin BAKİYE uçları (`GET`/`PUT /leave-balances/{personnel_id}/{year}`).

`test_ik2_leave_decision_api.py`den TAŞINDI (800 satır tavanı); testler ve
iddialar aynı. Paylaşılan fikstürler `_ik2_leave_decision.py`dedir.
"""

import uuid

import pytest
from sqlalchemy import select

from app.modules.audit.models import AuditLog
from app.modules.personnel.models import (
    LeaveBalance,
)
from tests.personnel._ik2_leave_decision import (
    _gun,
    _post_talep,
    _yeni_denetim_metinleri,
    _yil,
    arsiv_belgesi_fixture,
    hastalik_fixture,
    kidemsiz_personel_fixture,
    personel_fixture,
    proje_fixture,
    tarihsiz_personel_fixture,
    yillik_fixture,
)

__all__ = [
    "arsiv_belgesi_fixture",
    "hastalik_fixture",
    "kidemsiz_personel_fixture",
    "personel_fixture",
    "proje_fixture",
    "tarihsiz_personel_fixture",
    "yillik_fixture",
]


# --- GET /leave-balances/{personnel_id}/{year} ------------------------------


@pytest.mark.asyncio
async def test_bakiye_mockup_satiri(client, ik_headers, seeded_db, personel, yillik):
    """İZ 134: hak 14 · devreden 3 · kullanılan 6 · kalan 11 · %35."""
    seeded_db.add(LeaveBalance(personnel_id=personel.id, year=_yil(), carried_over=3))
    await seeded_db.flush()
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(5, 1), _gun(5, 6))
    assert (
        await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    ).status_code == 200

    yanit = await client.get(f"/leave-balances/{personel.id}/{_yil()}", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["annual_entitlement"] == 14
    assert float(govde["carried_over"]) == 3
    assert govde["used"] == 6
    assert float(govde["remaining"]) == 11
    assert govde["usage_pct"] == 35
    assert govde["personnel_name"] == "Ayşe Demir"
    assert govde["year"] == _yil()


@pytest.mark.asyncio
async def test_bakiye_kaydi_yoksa_anlamli_yanit(client, ik_headers, personel):
    """Bakiye SATIRI olmayan personel için de anlamlı yanıt: devreden 0 (404 DEĞİL).

    Satır MANUEL devreden içindir (İZ 137); yokluğu "devreden yok" demektir, veri
    eksikliği değil — türevler yine hesaplanabilir.
    """
    yanit = await client.get(f"/leave-balances/{personel.id}/{_yil()}", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert float(govde["carried_over"]) == 0
    assert govde["annual_entitlement"] == 14
    assert govde["used"] == 0
    assert float(govde["remaining"]) == 14
    assert govde["usage_pct"] == 0


@pytest.mark.asyncio
async def test_bakiye_kidemsiz_personel_hak_yok(client, ik_headers, kidemsiz_personel):
    """İZ 163 "1 yıl dolunca hak kazanır": hak/kalan/yüzde NULL, `used` 0."""
    yanit = await client.get(f"/leave-balances/{kidemsiz_personel.id}/{_yil()}", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["annual_entitlement"] is None
    assert govde["remaining"] is None
    assert govde["usage_pct"] is None
    assert govde["used"] == 0


@pytest.mark.asyncio
async def test_bakiye_hire_date_null_hak_none(client, ik_headers, tarihsiz_personel):
    yanit = await client.get(f"/leave-balances/{tarihsiz_personel.id}/{_yil()}", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["annual_entitlement"] is None
    assert govde["remaining"] is None
    assert govde["seniority_years"] is None


@pytest.mark.asyncio
async def test_bakiye_var_olmayan_personel_404(client, ik_headers):
    yanit = await client.get(f"/leave-balances/{uuid.uuid4()}/{_yil()}", headers=ik_headers)
    assert yanit.status_code == 404, yanit.text


@pytest.mark.asyncio
async def test_bakiye_sef_okur(client, sef_headers, personel):
    yanit = await client.get(f"/leave-balances/{personel.id}/{_yil()}", headers=sef_headers)
    assert yanit.status_code == 200, yanit.text


@pytest.mark.asyncio
async def test_bakiye_yetkisiz_403(client, yetkisiz_headers, personel):
    yanit = await client.get(f"/leave-balances/{personel.id}/{_yil()}", headers=yetkisiz_headers)
    assert yanit.status_code == 403, yanit.text


# --- PUT /leave-balances/{personnel_id}/{year} — YALNIZ `carried_over` ------


@pytest.mark.asyncio
async def test_put_bakiye_upsert_olusturur(client, ik_headers, seeded_db, personel):
    yanit = await client.put(
        f"/leave-balances/{personel.id}/{_yil()}", json={"carried_over": 3}, headers=ik_headers
    )
    assert yanit.status_code == 200, yanit.text
    assert float(yanit.json()["carried_over"]) == 3
    satirlar = (
        await seeded_db.execute(
            select(LeaveBalance).where(LeaveBalance.personnel_id == personel.id)
        )
    ).scalars()
    assert len(list(satirlar)) == 1


@pytest.mark.asyncio
async def test_put_bakiye_ikinci_kez_gunceller_kopya_acmaz(client, ik_headers, seeded_db, personel):
    await client.put(
        f"/leave-balances/{personel.id}/{_yil()}", json={"carried_over": 3}, headers=ik_headers
    )
    yanit = await client.put(
        f"/leave-balances/{personel.id}/{_yil()}", json={"carried_over": 5}, headers=ik_headers
    )
    assert yanit.status_code == 200, yanit.text
    assert float(yanit.json()["carried_over"]) == 5
    satirlar = (
        await seeded_db.execute(
            select(LeaveBalance).where(LeaveBalance.personnel_id == personel.id)
        )
    ).scalars()
    assert len(list(satirlar)) == 1


@pytest.mark.asyncio
async def test_put_bakiye_turevleri_gunceller(client, ik_headers, personel):
    """Devreden değişince `remaining` TÜREVİ de değişir (kolon yok, tek kaynak)."""
    yanit = await client.put(
        f"/leave-balances/{personel.id}/{_yil()}", json={"carried_over": 6}, headers=ik_headers
    )
    assert float(yanit.json()["remaining"]) == 20


@pytest.mark.asyncio
async def test_put_bakiye_turev_alan_gonderilirse_422(client, ik_headers, personel):
    """YALNIZ `carried_over` yazılabilir — `annual_entitlement` KOLON DEĞİLDİR (K1)."""
    yanit = await client.put(
        f"/leave-balances/{personel.id}/{_yil()}",
        json={"carried_over": 3, "annual_entitlement": 30},
        headers=ik_headers,
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_put_bakiye_used_gonderilirse_422(client, ik_headers, personel):
    yanit = await client.put(
        f"/leave-balances/{personel.id}/{_yil()}",
        json={"carried_over": 3, "used": 0},
        headers=ik_headers,
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_put_bakiye_document_id_gonderilirse_422(client, ik_headers, personel, arsiv_belgesi):
    """K6 sızıntı kapısı: bakiye yolu BC bağı KURDURMAZ."""
    yanit = await client.put(
        f"/leave-balances/{personel.id}/{_yil()}",
        json={"carried_over": 3, "document_id": str(arsiv_belgesi.id)},
        headers=ik_headers,
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_put_bakiye_negatif_devreden_422(client, ik_headers, personel):
    yanit = await client.put(
        f"/leave-balances/{personel.id}/{_yil()}", json={"carried_over": -1}, headers=ik_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_put_bakiye_var_olmayan_personel_404(client, ik_headers):
    yanit = await client.put(
        f"/leave-balances/{uuid.uuid4()}/{_yil()}", json={"carried_over": 3}, headers=ik_headers
    )
    assert yanit.status_code == 404, yanit.text


@pytest.mark.asyncio
async def test_put_bakiye_sef_403(client, sef_headers, personel):
    yanit = await client.put(
        f"/leave-balances/{personel.id}/{_yil()}", json={"carried_over": 3}, headers=sef_headers
    )
    assert yanit.status_code == 403, yanit.text


@pytest.mark.asyncio
async def test_put_bakiye_denetime_yazilir(client, ik_headers, seeded_db, personel):
    onceki = set(await seeded_db.scalars(select(AuditLog.id)))
    await client.put(
        f"/leave-balances/{personel.id}/{_yil()}", json={"carried_over": 3}, headers=ik_headers
    )
    metinler = await _yeni_denetim_metinleri(seeded_db, onceki)
    assert len(metinler) == 1
    assert "Ayşe Demir" in metinler[0]

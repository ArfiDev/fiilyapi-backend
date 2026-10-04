"""Denetim yakalama: roles + company uclari (plan Task 3).

Task 2 ile ayni sozlesme: her yazma islemi TAM OLARAK bir denetim satiri uretir;
negatif testler (kilitli izin, /settings/* tercih uclari, GET) satir olusmadigini kilitler.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditAction, AuditLog
from app.modules.roles.models import Role

_IP = "203.0.113.42"
_HEADERS = {"x-forwarded-for": _IP}
_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64  # kucuk sahte PNG govdesi


async def _auth(client, user_factory, role_key: str) -> dict[str, str]:
    await user_factory(email=f"{role_key}@t.co", password="parola1234", role_key=role_key)
    resp = await client.post(
        "/auth/login",
        json={"email": f"{role_key}@t.co", "password": "parola1234"},
        headers=_HEADERS,
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}", **_HEADERS}


async def _rows(session: AsyncSession, action: AuditAction | None = None) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.occurred_at)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list((await session.execute(stmt)).scalars().all())


async def _role_id(session: AsyncSession, key: str) -> str:
    return str((await session.execute(select(Role).where(Role.key == key))).scalar_one().id)


async def test_rol_olusturma_denetim_satiri_yazar(client, user_factory, seeded_db):
    headers = await _auth(client, user_factory, "system_admin")

    resp = await client.post(
        "/roles",
        json={"key": "kalite_sefi", "name": "Kalite Şefi", "emoji": "🔎", "description": ""},
        headers=headers,
    )
    assert resp.status_code == 201

    rows = await _rows(seeded_db, AuditAction.create)
    assert len(rows) == 1
    assert rows[0].detail == "Özel rol oluşturuldu: Kalite Şefi"
    assert str(rows[0].ip_address) == _IP
    assert rows[0].actor_user_id is not None


async def test_rol_yeniden_adlandirma_eski_ve_yeni_adi_icerir(client, user_factory, seeded_db):
    headers = await _auth(client, user_factory, "system_admin")
    rid = await _role_id(seeded_db, "accounting")  # seed adi: "Muhasebe"

    resp = await client.patch(
        f"/roles/{rid}",
        json={"name": "Mali İşler", "emoji": "📒", "description": ""},
        headers=headers,
    )
    assert resp.status_code == 200

    rows = await _rows(seeded_db, AuditAction.update)
    assert len(rows) == 1
    # Eski ad islemden ONCE okunmali; sonra okunursa yeni ad iki kez yazilir.
    assert rows[0].detail == "Rol yeniden adlandırıldı: Muhasebe → Mali İşler"


async def test_rol_silme_silinen_adi_icerir(client, user_factory, seeded_db):
    headers = await _auth(client, user_factory, "system_admin")
    created = await client.post(
        "/roles",
        json={"key": "gecici_rol", "name": "Geçici Rol", "emoji": "", "description": ""},
        headers=headers,
    )
    assert created.status_code == 201

    resp = await client.delete(f"/roles/{created.json()['id']}", headers=headers)
    assert resp.status_code == 204

    rows = await _rows(seeded_db, AuditAction.delete)
    assert len(rows) == 1
    assert rows[0].detail == "Rol silindi: Geçici Rol"


async def test_sayfa_izni_degisikligi_sayfa_adini_ve_duzeyi_icerir(client, user_factory, seeded_db):
    headers = await _auth(client, user_factory, "system_admin")
    rid = await _role_id(seeded_db, "field_engineer")  # seed adi: "Saha Mühendisi"
    mevcut = (await client.get(f"/roles/{rid}/pages", headers=headers)).json()["pages"]
    mevcut["mali.yevmiye"] = {"level": "edit", "approve": True}

    resp = await client.put(
        f"/roles/{rid}/pages", json={"pages": mevcut, "hidden_fields": []}, headers=headers
    )
    assert resp.status_code == 200

    rows = await _rows(seeded_db, AuditAction.update)
    # Sayfa degisikligi ve gizli alan degisikligi AYRI satirlardir (saha muh. `limited` -> bayrak).
    assert rows[0].detail == (
        "Sayfa izinleri değişti: Saha Mühendisi (1 sayfa) · Muhasebe › Yevmiye: Düzenler + Onaylar"
    )


async def test_kilitli_sayfa_izni_degisikligi_denetim_satiri_yazmaz(
    client, user_factory, seeded_db
):
    headers = await _auth(client, user_factory, "system_admin")
    rid = await _role_id(seeded_db, "system_admin")
    mevcut = (await client.get(f"/roles/{rid}/pages", headers=headers)).json()["pages"]

    resp = await client.put(
        f"/roles/{rid}/pages", json={"pages": mevcut, "hidden_fields": []}, headers=headers
    )
    assert resp.status_code == 403

    assert await _rows(seeded_db, AuditAction.update) == []


async def test_sirket_guncelleme_denetim_satiri_yazar(client, user_factory, seeded_db):
    headers = await _auth(client, user_factory, "system_admin")

    resp = await client.put("/company", json={"name": "FİİL Yapı A.Ş."}, headers=headers)
    assert resp.status_code == 200

    rows = await _rows(seeded_db, AuditAction.update)
    assert len(rows) == 1
    assert rows[0].detail == "Şirket bilgileri güncellendi"
    assert str(rows[0].ip_address) == _IP


async def test_logo_yukleme_denetim_satiri_yazar(client, user_factory, seeded_db):
    headers = await _auth(client, user_factory, "system_admin")

    resp = await client.post(
        "/company/logo",
        files={"file": ("logo.png", _PNG, "image/png")},
        headers=headers,
    )
    assert resp.status_code == 200

    rows = await _rows(seeded_db, AuditAction.update)
    assert len(rows) == 1
    assert rows[0].detail == "Şirket logosu güncellendi"


async def test_logo_silme_denetim_satiri_yazar(client, user_factory, seeded_db):
    headers = await _auth(client, user_factory, "system_admin")

    resp = await client.delete("/company/logo", headers=headers)
    assert resp.status_code == 204

    rows = await _rows(seeded_db, AuditAction.update)
    assert len(rows) == 1
    assert rows[0].detail == "Şirket logosu kaldırıldı"


async def test_tercih_uclari_denetim_satiri_yazmaz(client, user_factory, seeded_db):
    """Kapsam-disi kuralinin regresyon kilidi (plan §Kapsam disi)."""
    headers = await _auth(client, user_factory, "system_admin")

    prefs = await client.put("/settings/preferences", json={"density": "compact"}, headers=headers)
    assert prefs.status_code == 200
    notifs = await client.put(
        "/settings/notifications",
        json={"items": []},
        headers=headers,
    )
    assert notifs.status_code == 200

    assert await _rows(seeded_db, AuditAction.update) == []


async def test_roller_ve_sirket_get_uclari_denetim_satiri_yazmaz(client, user_factory, seeded_db):
    headers = await _auth(client, user_factory, "system_admin")

    assert (await client.get("/roles", headers=headers)).status_code == 200
    assert (await client.get("/modules", headers=headers)).status_code == 200
    assert (await client.get("/company", headers=headers)).status_code == 200

    # Yalnizca yukaridaki login satiri kalmali.
    rows = await _rows(seeded_db)
    assert [r.action for r in rows] == [AuditAction.login]

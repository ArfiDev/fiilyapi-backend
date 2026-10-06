"""IZN-B5a — `UserResponse.role_name` (ana rolün görünen adı; salt okuma, maskeye tabi değil).

`GET /roles` Kullanıcılar sayfasında yalnız Görür kişiye 403 olduğundan liste rol sütunu bu
alandan dolar. Çakılanlar: liste + detay + oluşturma + güncelleme yanıtı rol adını taşır, rol
yeniden adlandırılınca yeni ad döner, liste sorgu sayısı kullanıcı sayısından BAĞIMSIZ.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import event, select

from app.modules.roles.models import Role
from tests.conftest import test_engine

PASSWORD = "parola1234"


async def _admin(client: AsyncClient, user_factory) -> dict[str, str]:
    email = f"admin.{uuid.uuid4().hex[:6]}@izn-b5a-rol.co"
    await user_factory(
        email=email, password=PASSWORD, role_key="system_admin", full_name="Yönetici"
    )
    login = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _rol(session, key: str) -> Role:
    return (await session.execute(select(Role).where(Role.key == key))).scalar_one()


async def test_liste_ve_detay_rol_adini_tasir(client, user_factory, seeded_db) -> None:
    admin = await _admin(client, user_factory)
    sef = await _rol(seeded_db, "site_chief")
    kisi = await user_factory(email="a@b5a.co", password=PASSWORD, role_key="site_chief")

    liste = await client.get("/users?limit=200", headers=admin)
    satir = next(u for u in liste.json()["items"] if u["id"] == str(kisi.id))
    detay = await client.get(f"/users/{kisi.id}", headers=admin)

    assert satir["role_name"] == sef.name
    assert detay.json()["role_name"] == sef.name
    assert satir["role_key"] == "site_chief"
    assert detay.json()["role_key"] == "site_chief"


async def test_rol_yeniden_adlandirilinca_yeni_ad_doner(client, user_factory, seeded_db) -> None:
    admin = await _admin(client, user_factory)
    sef = await _rol(seeded_db, "site_chief")
    kisi = await user_factory(email="b@b5a.co", password=PASSWORD, role_key="site_chief")
    sef.name = "Yeni Şantiye Adı"
    await seeded_db.flush()

    liste = await client.get("/users?limit=200", headers=admin)
    detay = await client.get(f"/users/{kisi.id}", headers=admin)

    satir = next(u for u in liste.json()["items"] if u["id"] == str(kisi.id))
    assert satir["role_name"] == "Yeni Şantiye Adı"
    assert detay.json()["role_name"] == "Yeni Şantiye Adı"


async def test_olusturma_ve_guncelleme_yaniti_rol_adini_tasir(
    client, user_factory, seeded_db
) -> None:
    admin = await _admin(client, user_factory)
    sef = await _rol(seeded_db, "site_chief")
    baska = await _rol(seeded_db, "accounting")

    yeni = await client.post(
        "/users",
        json={
            "email": "c@b5a.co",
            "password": PASSWORD,
            "full_name": "Yeni Kişi",
            "role_id": str(sef.id),
        },
        headers=admin,
    )
    assert yeni.status_code == 201, yeni.text
    guncel = await client.patch(
        f"/users/{yeni.json()['id']}", json={"role_id": str(baska.id)}, headers=admin
    )
    assert guncel.status_code == 200, guncel.text

    assert yeni.json()["role_name"] == sef.name
    assert guncel.json()["role_name"] == baska.name
    assert yeni.json()["role_key"] == "site_chief"
    assert guncel.json()["role_key"] == "accounting"


async def test_liste_sorgu_sayisi_kullanici_sayisindan_bagimsiz(
    client, user_factory, seeded_db
) -> None:
    admin = await _admin(client, user_factory)

    async def sorgu_sayisi() -> int:
        ifadeler: list[str] = []

        def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
            ifadeler.append(statement)

        event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
        try:
            assert (await client.get("/users?limit=200", headers=admin)).status_code == 200
        finally:
            event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)
        return len(ifadeler)

    await sorgu_sayisi()  # ısınma: ilk istek ek önbellek sorguları yapabilir
    ilk = await sorgu_sayisi()
    for i, anahtar in enumerate(["site_chief", "accounting", "site_chief", "accounting"] * 3):
        await user_factory(email=f"n{i}@b5a.co", password=PASSWORD, role_key=anahtar)
    assert await sorgu_sayisi() == ilk

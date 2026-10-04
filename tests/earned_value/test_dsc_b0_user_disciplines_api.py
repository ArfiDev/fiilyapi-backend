"""DSC-B0 → IZN-B3 — `GET/PUT /users/{id}/disciplines` KALDIRILDI (410).

`/auth/me.disciplines` alanı da kalktı.

Global kullanıcı → disiplin ataması proje ekibine taşındı (`PUT /users/{id}/access`,
`projects[].discipline_ids`; testler `tests/modules/test_izn_b3_access_api.py`). Burada yalnız:
eski uçlar 410 döner ve HİÇBİR satır yazmaz, kapıları eskisiyle AYNIDIR (yetkisiz 403, kimliksiz
401), `/auth/me.disciplines` alanı KALKTI (yerine `projects[].discipline_ids`).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import EvDiscipline
from app.modules.users.models import ProjectMemberDiscipline, User

PASSWORD = "parola1234"


async def _headers(client: AsyncClient, user_factory, role_key: str) -> dict[str, str]:
    email = f"{role_key}.{uuid.uuid4().hex[:6]}@dsc-b0-api.co"
    await user_factory(email=email, password=PASSWORD, role_key=role_key)
    login = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.fixture
async def hedef(seeded_db: AsyncSession, user_factory) -> User:
    return await user_factory(
        email=f"hedef.{uuid.uuid4().hex[:6]}@dsc-b0-api.co",
        password=PASSWORD,
        role_key="site_chief",
        full_name="Hedef Kişi",
    )


@pytest.fixture
async def civ(disiplin_fabrikasi) -> EvDiscipline:
    return await disiplin_fabrikasi("CIV", "İnşaat")


def _url(user_id: uuid.UUID) -> str:
    return f"/users/{user_id}/disciplines"


async def test_get_ve_put_410_ve_hicbir_satir_yazilmaz(
    client, user_factory, hedef, civ, seeded_db
) -> None:
    admin = await _headers(client, user_factory, "system_admin")
    get = await client.get(_url(hedef.id), headers=admin)
    put = await client.put(_url(hedef.id), json={"discipline_ids": [str(civ.id)]}, headers=admin)
    assert get.status_code == put.status_code == 410
    assert "projects[].discipline_ids" in put.json()["detail"]
    kalan = (await seeded_db.execute(select(ProjectMemberDiscipline))).scalars().all()
    assert kalan == []


async def test_kapi_eskisiyle_ayni_yetkisiz_403_kimliksiz_401(
    client, user_factory, hedef, civ
) -> None:
    """site_chief `user_management` = none: GET (view) da PUT (full) da 403 (410'a ulaşmaz)."""
    headers = await _headers(client, user_factory, "site_chief")
    assert (await client.get(_url(hedef.id), headers=headers)).status_code == 403
    put = await client.put(_url(hedef.id), json={"discipline_ids": []}, headers=headers)
    assert put.status_code == 403
    assert (await client.get(_url(hedef.id))).status_code == 401


async def test_me_global_disiplin_alani_kalkti(client, seeded_db, user_factory) -> None:
    email = f"me.{uuid.uuid4().hex[:6]}@dsc-b0-api.co"
    await user_factory(email=email, password=PASSWORD, role_key="site_chief")
    token = (await client.post("/auth/login", json={"email": email, "password": PASSWORD})).json()[
        "access_token"
    ]
    seeded_db.expunge_all()  # ortak test oturumu; gerçek istekte oturum ayrıdır
    me = (await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})).json()
    assert "disciplines" not in me
    assert me["projects"] == [] and me["all_projects"] is False

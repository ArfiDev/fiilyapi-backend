"""DSC-B0b — `GET /earned-value/disciplines` kapisi: `earned_value:view` VEYA
`user_management:view` (izin MATRISI degismez; yalniz bu ucun kapisi "herhangi biri yeter").

Pozitif kontrol cifti: iki izinden BIRI olan aktor 200; IKISI de olmayan 403 + govde birebir
`require_permission` govdesi. Rol izinleri testte ozel roller uzerinden kurulur (seed
matrisine dokunulmaz).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel, Scope
from app.modules.catalog.models import EvDiscipline
from app.modules.roles import service as roles_service
from app.modules.roles.models import Role
from app.modules.roles.schemas import RoleCreate
from tests._legacy_permission_yardimcisi import update_role_permission
from tests._proje_ekibi import disiplin_ata

PASSWORD = "parola1234"
URL = "/earned-value/disciplines"
YETKISIZ = {"detail": "Bu işlem için yetkiniz yok"}


async def _rol(session: AsyncSession, key: str, izinler: dict[str, AccessLevel]) -> Role:
    """Ozel rol (tum modullere `none`) + verilen modul izinleri."""
    role = await roles_service.create_custom_role(
        session, RoleCreate(key=key, name=key, emoji="", description="")
    )
    for module_key, level in izinler.items():
        await update_role_permission(session, role.id, module_key, level, Scope.all)
    return role


async def _aktor(client: AsyncClient, user_factory, role_key: str):
    email = f"{role_key}.{uuid.uuid4().hex[:6]}@dsc-b0b.co"
    user = await user_factory(email=email, password=PASSWORD, role_key=role_key)
    login = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, user


@pytest.fixture
async def civ(disiplin_fabrikasi) -> EvDiscipline:
    return await disiplin_fabrikasi("CIV", "İnşaat")


@pytest.fixture
async def elk(disiplin_fabrikasi) -> EvDiscipline:
    return await disiplin_fabrikasi("ELK", "Elektrik")


@pytest.fixture
async def roller(seeded_db: AsyncSession) -> dict[str, str]:
    await _rol(seeded_db, "b0b_ym", {"user_management": AccessLevel.view})
    await _rol(seeded_db, "b0b_ev", {"earned_value": AccessLevel.view})
    await _rol(seeded_db, "b0b_hicbiri", {"projects": AccessLevel.view})
    return {"ym": "b0b_ym", "ev": "b0b_ev", "hicbiri": "b0b_hicbiri"}


async def test_user_management_view_ev_izni_olmadan_200_tum_liste(
    client, user_factory, roller, civ, elk
) -> None:
    baslik, _ = await _aktor(client, user_factory, roller["ym"])
    resp = await client.get(URL, headers=baslik)
    assert resp.status_code == 200, resp.text
    assert {x["id"] for x in resp.json()} == {str(civ.id), str(elk.id)}


async def test_earned_value_view_200(client, user_factory, roller, civ, elk) -> None:
    baslik, _ = await _aktor(client, user_factory, roller["ev"])
    resp = await client.get(URL, headers=baslik)
    assert resp.status_code == 200, resp.text
    assert {x["id"] for x in resp.json()} == {str(civ.id), str(elk.id)}


async def test_iki_izin_de_yoksa_403_govde_birebir(client, user_factory, roller, civ) -> None:
    baslik, _ = await _aktor(client, user_factory, roller["hicbiri"])
    resp = await client.get(URL, headers=baslik)
    assert resp.status_code == 403
    assert resp.json() == YETKISIZ


async def test_403_dogru_kapidan_gelir_yazma_ucu_ev_kapisinda_kalir(
    client, user_factory, roller
) -> None:
    """`user_management:view` yalniz OKUMAYI acar: ayni URL'ye POST hala EV `full` ister."""
    baslik, _ = await _aktor(client, user_factory, roller["ym"])
    resp = await client.post(
        URL,
        headers=baslik,
        json={"code": "ZZ", "name": "Z", "color": "#112233", "default_contractor_type": "own"},
    )
    assert resp.status_code == 403
    assert resp.json() == YETKISIZ


async def test_proje_ekibinde_disiplinli_aktor_sirket_disiplin_listesini_TAM_gorur(
    client, user_factory, roller, seeded_db, civ, elk, project_factory
) -> None:
    """IZN-B3: disiplin PROJE BAŞINA; şirket disiplin listesi kullanıcı kapsamıyla SÜZÜLMEZ."""
    baslik, user = await _aktor(client, user_factory, roller["ym"])
    await disiplin_ata(seeded_db, user, (await project_factory("DSC-LK")).id, civ.id)

    resp = await client.get(URL, headers=baslik)

    assert resp.status_code == 200, resp.text
    assert {x["id"] for x in resp.json()} >= {str(civ.id), str(elk.id)}


async def test_auth_me_disiplinleri_PROJE_ekibinde_izin_kapisi_yok(
    client, user_factory, roller, seeded_db, civ, project_factory
) -> None:
    """`/auth/me.projects[].discipline_ids` izin kapisi ISTEMEZ: hicbir modul izni olmayan rol de
    kendi proje atamasini gorur; global `disciplines` alani KALKTI (IZN-B3)."""
    baslik, user = await _aktor(client, user_factory, roller["hicbiri"])
    proje = await project_factory("DSC-ME")
    await disiplin_ata(seeded_db, user, proje.id, civ.id)
    seeded_db.expunge_all()

    resp = await client.get("/auth/me", headers=baslik)

    assert resp.status_code == 200, resp.text
    assert "disciplines" not in resp.json()
    assert [(p["project_id"], p["discipline_ids"]) for p in resp.json()["projects"]] == [
        (str(proje.id), [str(civ.id)])
    ]

"""TKL-B6.2 — donusturme ucunun izin kapisi (SO-42, GORUNUR NOT).

Uc `projects:admin` + `contracts:full` + `RequireUnrestricted` ister. Bugun seed matrisinde
`projects:admin` YALNIZ `system_admin`dedir → patron (projects=full) bile 403 alir. Bu testler o
gercegi KANITLAR; izin turu matrisi degistirdiginde burasi bilerek guncellenir.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.core.access import AccessLevel, Scope
from app.modules.catalog.models import ContractorType, EvDiscipline
from app.modules.projects.models import Project
from app.modules.roles.seed_data import MATRIX, ROLE_ORDER
from app.modules.users.models import ProjectMember, User
from tests._proje_ekibi import baska_projede_disiplinli

from .._boq import _auth, _login_with_access, _set_permission
from ._convert import govde, kazanilmis_teklif, url

pytestmark = pytest.mark.usefixtures("tohum_kancasi")


@pytest.fixture
async def kz(client, admin, isveren, katalog):
    return await kazanilmis_teklif(client, admin, isveren, katalog)


async def _giris(client, db_session, user_factory, role_key: str) -> dict[str, str]:
    token = await _login_with_access(
        client, db_session, user_factory, role_key, f"{role_key}.{uuid.uuid4().hex[:6]}@cnv.co"
    )
    return _auth(token)


async def _proje_sayisi(session) -> int:
    return await session.scalar(select(func.count()).select_from(Project)) or 0


async def test_kimliksiz_401(client, kz) -> None:
    assert (await client.post(url(kz.offer_id), json=govde(kz))).status_code == 401


async def test_seed_matrisi_projects_admin_yalniz_system_admin() -> None:
    """Ön koşul (SO-42): kirmizilanirsa patron/403 beklentisi ve SO-42 notu yeniden okunur."""
    admin_roller = [
        rol
        for rol, (seviye, _kapsam) in zip(ROLE_ORDER, MATRIX["projects"], strict=True)
        if seviye == AccessLevel.admin
    ]
    assert admin_roller == ["system_admin"]


async def test_patron_403_SO42(client, db_session, user_factory, kz) -> None:
    patron = await _giris(client, db_session, user_factory, "patron")

    resp = await client.post(url(kz.offer_id), json=govde(kz), headers=patron)

    assert resp.status_code == 403, resp.text
    assert await _proje_sayisi(db_session) == 0


@pytest.mark.parametrize("rol", ["site_chief", "field_engineer", "accounting"])
async def test_diger_roller_403(client, db_session, user_factory, kz, rol) -> None:
    kisi = await _giris(client, db_session, user_factory, rol)
    resp = await client.post(url(kz.offer_id), json=govde(kz), headers=kisi)
    assert resp.status_code == 403, resp.text
    assert await _proje_sayisi(db_session) == 0


async def test_projects_admin_tek_basina_yetmez_contracts_full_da_ister(
    client, db_session, user_factory, kz
) -> None:
    await _set_permission(db_session, "accounting", "projects", AccessLevel.admin, Scope.all)
    await _set_permission(db_session, "accounting", "contracts", AccessLevel.view, Scope.all)
    kisi = await _giris(client, db_session, user_factory, "accounting")
    resp = await client.post(url(kz.offer_id), json=govde(kz), headers=kisi)
    assert resp.status_code == 403, resp.text
    assert await _proje_sayisi(db_session) == 0


async def test_contracts_full_tek_basina_yetmez_projects_admin_de_ister(
    client, db_session, user_factory, kz
) -> None:
    await _set_permission(db_session, "accounting", "projects", AccessLevel.full, Scope.all)
    await _set_permission(db_session, "accounting", "contracts", AccessLevel.full, Scope.all)
    kisi = await _giris(client, db_session, user_factory, "accounting")
    resp = await client.post(url(kz.offer_id), json=govde(kz), headers=kisi)
    assert resp.status_code == 403, resp.text


async def test_iki_izin_de_tamsa_proje_basina_disiplinli_kullanici_da_gecer(
    client, db_session, user_factory, kz
) -> None:
    """IZN-B3: `RequireUnrestricted` KALKTI (disiplin proje basina, teklif sirket geneli):
    bir projede disiplinle kisitli kullanici AYNI izinlerle donusturmeyi YAPAR."""
    for rol in ("project_manager",):
        await _set_permission(db_session, rol, "projects", AccessLevel.admin, Scope.all)
        await _set_permission(db_session, rol, "contracts", AccessLevel.full, Scope.all)
    kisitli = await _giris(client, db_session, user_factory, "project_manager")
    disiplin = EvDiscipline(
        code="KIS", name="Kisitli", color="#2563EB", default_contractor_type=ContractorType.OWN
    )
    db_session.add(disiplin)
    await db_session.flush()
    uid = (
        await db_session.execute(select(User.id).where(User.email.like("project_manager.%@cnv.co")))
    ).scalar_one()
    await baska_projede_disiplinli(db_session, uid, disiplin.id)

    resp = await client.post(url(kz.offer_id), json=govde(kz), headers=kisitli)

    assert resp.status_code == 200, resp.text
    assert await _proje_sayisi(db_session) == 2  # kisit projesi + donusturulen proje


async def test_donusturen_kisi_yeni_projeye_ANA_rolunun_ekip_uyesi_yazilir_ve_gorur(
    client, db_session, user_factory, kz
) -> None:
    """IZN-B3: ekip satiri OLMAYAN (Tum projeler DEGIL) donusturen kisi yeni projeyi gorebilmeli."""
    from .._boq import _login

    for rol in ("project_manager",):
        await _set_permission(db_session, rol, "projects", AccessLevel.admin, Scope.all)
        await _set_permission(db_session, rol, "contracts", AccessLevel.full, Scope.all)
    token = await _login(client, user_factory, "project_manager", "donusturen@cnv.co")
    kisi = (
        await db_session.execute(select(User).where(User.email == "donusturen@cnv.co"))
    ).scalar_one()
    kisi_id, rol_id = kisi.id, kisi.role_id
    db_session.expunge_all()

    resp = await client.post(url(kz.offer_id), json=govde(kz), headers=_auth(token))

    assert resp.status_code == 200, resp.text
    uyeler = (
        (await db_session.execute(select(ProjectMember).where(ProjectMember.user_id == kisi_id)))
        .scalars()
        .all()
    )
    assert len(uyeler) == 1 and uyeler[0].role_id == rol_id
    proje = (await db_session.execute(select(Project))).scalars().one()
    assert uyeler[0].project_id == proje.id
    assert (await client.get(f"/projects/{proje.id}", headers=_auth(token))).status_code == 200

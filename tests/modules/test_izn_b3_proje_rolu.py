"""IZN-B3 — rol PROJE BAŞINA (KARARLAR §1.7): etkin rol, "Tüm projeler", otomatik üyelik.

* Aynı kişi A'da Proje Müdürü (yazar), B'de Görüntüleyici (yazamaz): A'da PATCH 200, B'de 404 (B bu
  istek için görünmez), B'yi OKUMAK 200. Ana rolü ne olursa olsun (🔴 mutasyon (b): proje bağlamında
  ana rolü kullanmak bu testi kırar): ana rolü Proje Müdürü olup B'de Görüntüleyici olan kişi B'yi
  yazamaz.
* "Tüm projeler" kişi proje içinde de ANA rolle çalışır: ana rol Görüntüleyici → hiçbir projede
  yazamaz (403); ana rol Proje Müdürü → her projede yazar.
* Ekip satırı OLMAYAN kişi ana rolü ne kadar güçlü olursa olsun projeyi göremez (404).
* Proje oluşturan ve tekliften dönüştüren kişi ana rolüyle ekibe yazılır; Sistem Yöneticisi ve
  "Tüm projeler" kişisi yazılmaz.
* `GET /users/{id}/access`: "Tüm projeler" kişide bayat ekip satırı olsa bile `projects=[]`.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sayfalar import PageLevel
from app.modules.roles import seed_data
from app.modules.roles import service as roles_service
from app.modules.roles.models import Role, RolePagePermission
from app.modules.roles.schemas import RoleCreate
from app.modules.users.models import ProjectMember, User
from tests._proje_ekibi import ekibe_ekle, tum_projeler
from tests.modules._boq import _site

PASSWORD = "parola1234"


async def _rol(session: AsyncSession, key: str) -> Role:
    return (await session.execute(select(Role).where(Role.key == key))).scalar_one()


async def _kisi(client: AsyncClient, user_factory, email: str, role_key: str):
    user = await user_factory(email=email, password=PASSWORD, role_key=role_key)
    return user


async def _giris(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
async def iki_proje(seeded_db: AsyncSession, project_factory):
    await seed_data.seed_izn_reference_data(seeded_db)  # `viewer` rolü
    a = await project_factory("ROL-A", name="Proje A")
    b = await project_factory("ROL-B", name="Proje B")
    return (
        a,
        b,
        await _site(seeded_db, a, code="ROL-A-S"),
        await _site(seeded_db, b, code="ROL-B-S"),
    )


def _gunluk(gun: int) -> dict:
    return {"entry_date": f"2026-05-{gun:02d}"}


def _gunluk_yolu(site) -> str:  # noqa: ANN001
    return f"/sites/{site.id}/diary"


async def test_ayni_kisi_A_da_yazar_B_de_goruntuleyici_yazamaz(
    client: AsyncClient, seeded_db, user_factory, iki_proje
) -> None:
    a, b, site_a, site_b = iki_proje
    karma = await _kisi(client, user_factory, "karma@izn.co", "viewer")  # ana rol: Görüntüleyici
    await ekibe_ekle(seeded_db, karma, a.id, (await _rol(seeded_db, "field_engineer")).id)
    await ekibe_ekle(seeded_db, karma, b.id, (await _rol(seeded_db, "viewer")).id)
    baslik = await _giris(client, "karma@izn.co")
    seeded_db.expunge_all()

    ok = await client.post(_gunluk_yolu(site_a), json=_gunluk(4), headers=baslik)
    assert ok.status_code == 201, ok.text
    # B'de rol Görüntüleyici: yazma ucu için B GÖRÜNMEZ (404); okuma 200.
    yok = await client.post(_gunluk_yolu(site_b), json=_gunluk(4), headers=baslik)
    assert yok.status_code == 404, yok.text
    assert (await client.get(_gunluk_yolu(site_b), headers=baslik)).status_code == 200
    assert (await client.get(_gunluk_yolu(site_a), headers=baslik)).status_code == 200


async def test_ana_rolu_yazar_olan_kisi_B_de_goruntuleyiciyse_B_yi_yazamaz(
    client: AsyncClient, seeded_db, user_factory, iki_proje
) -> None:
    """🔴 Mutasyon (b) bekçisi: ana rol (Saha Müh.) yazar ama B'deki PROJE rolü Görüntüleyici —
    proje bağlamında etkin rol ANA rol olsaydı bu yazma geçerdi."""
    a, b, site_a, site_b = iki_proje
    ters = await _kisi(client, user_factory, "ters@izn.co", "field_engineer")
    await ekibe_ekle(seeded_db, ters, a.id)  # A: ana rolle (Saha Müh.)
    await ekibe_ekle(seeded_db, ters, b.id, (await _rol(seeded_db, "viewer")).id)
    baslik = await _giris(client, "ters@izn.co")
    seeded_db.expunge_all()

    assert (
        await client.post(_gunluk_yolu(site_a), json=_gunluk(5), headers=baslik)
    ).status_code == 201
    yok = await client.post(_gunluk_yolu(site_b), json=_gunluk(5), headers=baslik)
    assert yok.status_code == 404, yok.text
    assert (await client.get(_gunluk_yolu(site_b), headers=baslik)).status_code == 200


async def test_tum_projeler_kisisi_proje_icinde_de_ana_rolle_calisir(
    client: AsyncClient, seeded_db, user_factory, iki_proje
) -> None:
    a, b, site_a, site_b = iki_proje
    yazar = await _kisi(client, user_factory, "tum-yazar@izn.co", "field_engineer")
    okur = await _kisi(client, user_factory, "tum-okur@izn.co", "viewer")
    # Bayat ekip satırı (olmamalı) yok sayılır: yazan kişide B'de Görüntüleyici satırı DURSA bile
    # ana rolle çalışır.
    await ekibe_ekle(seeded_db, yazar, b.id, (await _rol(seeded_db, "viewer")).id)
    await tum_projeler(seeded_db, yazar)
    await tum_projeler(seeded_db, okur)
    yazar_b = await _giris(client, "tum-yazar@izn.co")
    okur_b = await _giris(client, "tum-okur@izn.co")
    seeded_db.expunge_all()

    for site in (site_a, site_b):
        assert (
            await client.post(_gunluk_yolu(site), json=_gunluk(6), headers=yazar_b)
        ).status_code == 201
        assert (await client.get(_gunluk_yolu(site), headers=okur_b)).status_code == 200
        assert (
            await client.post(_gunluk_yolu(site), json=_gunluk(7), headers=okur_b)
        ).status_code == 403


async def test_ekibi_olmayan_guclu_ana_rol_projeyi_goremez(
    client: AsyncClient, seeded_db, user_factory, iki_proje
) -> None:
    _a, _b, site_a, _site_b = iki_proje
    await _kisi(client, user_factory, "ekipsiz-pm@izn.co", "field_engineer")
    baslik = await _giris(client, "ekipsiz-pm@izn.co")
    seeded_db.expunge_all()
    assert (await client.get(_gunluk_yolu(site_a), headers=baslik)).status_code == 404
    assert (
        await client.post(_gunluk_yolu(site_a), json=_gunluk(8), headers=baslik)
    ).status_code == 404


# --- otomatik üyelik ----------------------------------------------------------------------


async def _proje_olusturan_rol(session: AsyncSession) -> Role:
    """`Projeler` sayfası Düzenler + Proje Müdürü'nün sayfaları: kendi yarattığı projeyi görmeli."""
    rol = await roles_service.create_custom_role(
        session, RoleCreate(key="proje_kurucu", name="Proje Kurucu", emoji="", description="")
    )
    pm = await _rol(session, "project_manager")
    pm_hucreler = (
        await session.execute(select(RolePagePermission).where(RolePagePermission.role_id == pm.id))
    ).scalars()
    for hucre in pm_hucreler:
        yazilan = await session.get(RolePagePermission, (rol.id, hucre.page_key))
        yazilan.level, yazilan.can_approve = hucre.level, hucre.can_approve
    kurucu_hucre = await session.get(RolePagePermission, (rol.id, "genel.projeler"))
    kurucu_hucre.level = PageLevel.edit
    await session.flush()
    return rol


_PROJE_GOVDESI = {
    "name": "Yeni Proje",
    "project_type": "taahhut",
    "status": "active",
    "is_draft": True,
}


async def test_proje_olusturan_ana_rolle_ekibe_yazilir_ve_projeyi_gorur(
    client: AsyncClient, seeded_db, user_factory
) -> None:
    await _proje_olusturan_rol(seeded_db)
    kurucu = await _kisi(client, user_factory, "kurucu@izn.co", "proje_kurucu")
    kurucu_id, rol_id = kurucu.id, kurucu.role_id
    baslik = await _giris(client, "kurucu@izn.co")
    seeded_db.expunge_all()

    resp = await client.post("/projects", json=_PROJE_GOVDESI, headers=baslik)
    assert resp.status_code == 201, resp.text
    proje_id = uuid.UUID(resp.json()["id"])
    satirlar = (
        (await seeded_db.execute(select(ProjectMember).where(ProjectMember.user_id == kurucu_id)))
        .scalars()
        .all()
    )
    assert [(m.project_id, m.role_id) for m in satirlar] == [(proje_id, rol_id)]
    assert (await client.get(f"/projects/{proje_id}", headers=baslik)).status_code == 200


async def test_sistem_yoneticisi_ve_tum_projeler_kisisi_olusturunca_ekip_satiri_yazilmaz(
    client: AsyncClient, seeded_db, user_factory
) -> None:
    await _proje_olusturan_rol(seeded_db)
    admin = await _kisi(client, user_factory, "yon@izn.co", "system_admin")
    gezgin = await _kisi(client, user_factory, "gezgin@izn.co", "proje_kurucu")
    await tum_projeler(seeded_db, gezgin)
    kimlikler = (admin.id, gezgin.id)
    basliklar = [await _giris(client, "yon@izn.co"), await _giris(client, "gezgin@izn.co")]
    seeded_db.expunge_all()
    for baslik in basliklar:
        resp = await client.post(
            "/projects",
            json={**_PROJE_GOVDESI, "name": f"P {uuid.uuid4().hex[:4]}"},
            headers=baslik,
        )
        assert resp.status_code == 201, resp.text
    kalan = (
        (await seeded_db.execute(select(ProjectMember).where(ProjectMember.user_id.in_(kimlikler))))
        .scalars()
        .all()
    )
    assert kalan == []


# --- GET /users/{id}/access bayat satır ---------------------------------------------------


async def test_get_access_tum_projeler_kisisinde_bayat_ekip_satirini_gostermez(
    client: AsyncClient, seeded_db, user_factory, project_factory
) -> None:
    admin = await _kisi(client, user_factory, "ad@izn.co", "system_admin")
    hedef = await _kisi(client, user_factory, "bayat@izn.co", "patron")
    proje = await project_factory("BAYAT")
    await ekibe_ekle(seeded_db, hedef, proje.id)
    await tum_projeler(seeded_db, hedef)
    hedef_id = hedef.id
    assert admin.id
    baslik = await _giris(client, "ad@izn.co")
    seeded_db.expunge_all()
    resp = await client.get(f"/users/{hedef_id}/access", headers=baslik)
    assert resp.status_code == 200, resp.text
    assert resp.json()["all_projects"] is True and resp.json()["projects"] == []
    assert isinstance(await seeded_db.get(User, hedef_id), User)

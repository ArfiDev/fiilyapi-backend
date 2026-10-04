"""IZN-B3 — `GET /users?q=`, `UserResponse.all_projects/project_count`, `RoleResponse.user_count`.

Çakılanlar: arama (ad / e-posta / ana rol adı, büyük-küçük harf, Türkçe `İ ı I i Ş ş Ğ ğ Ü ü Ö ö
Ç ç` katlaması, LIKE metakarakterleri LİTERAL), `total` süzgeci izler, `project_count` doğru
ve LİSTE SORGU SAYISI kullanıcı sayısından BAĞIMSIZ (N+1 yok); rol `user_count`: ana rol + proje
rolü, FARKLI kullanıcı (iki yoldan bağlı kişi tek sayılır), rol silme 409 bu sayımı kullanır.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.roles.models import Role
from app.modules.roles.service import role_responses
from app.modules.users.models import ProjectMember, User
from app.modules.users.repository import fold_search_text
from tests.conftest import test_engine

PASSWORD = "parola1234"


async def _kisi(user_factory, email: str, role_key: str, full_name: str) -> User:
    return await user_factory(
        email=email, password=PASSWORD, role_key=role_key, full_name=full_name
    )


async def _admin(client: AsyncClient, user_factory) -> dict[str, str]:
    email = f"admin.{uuid.uuid4().hex[:6]}@izn-b3-liste.co"
    await user_factory(
        email=email, password=PASSWORD, role_key="system_admin", full_name="Yönetici"
    )
    login = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _role(session: AsyncSession, key: str) -> Role:
    return (await session.execute(select(Role).where(Role.key == key))).scalar_one()


async def _isimler(client: AsyncClient, admin, q: str) -> list[str]:
    resp = await client.get("/users", params={"q": q}, headers=admin)
    assert resp.status_code == 200, resp.text
    return sorted(u["full_name"] for u in resp.json()["items"])


# --- arama --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("girdi", "beklenen"),
    [
        ("İlker Işık", "ilker isik"),
        ("ILKER ISIK", "ilker isik"),
        ("ılker ısık", "ilker isik"),
        ("ŞÖĞÜÇ şöğüç", "soguc soguc"),
    ],
)
def test_katlama_turkce_harfleri_ascii_karsiligina_indirir(girdi: str, beklenen: str) -> None:
    assert fold_search_text(girdi) == beklenen


async def test_arama_turkce_ve_buyuk_kucuk_harf_duyarsiz(client, user_factory, seeded_db) -> None:
    admin = await _admin(client, user_factory)
    await _kisi(user_factory, "a@t.co", "site_chief", "İlker Işık")
    await _kisi(user_factory, "b@t.co", "site_chief", "Şule Öztürk")
    await _kisi(user_factory, "c@t.co", "site_chief", "Ali Veli")

    # "İ"/"ı"/"I"/"i" hepsi aynı; yazılış biçimi fark etmez.
    for q in ("ilker", "İLKER", "ılker", "ILKER", "ısık", "ISIK"):
        assert await _isimler(client, admin, q) == ["İlker Işık"], q
    assert await _isimler(client, admin, "sule ozturk") == ["Şule Öztürk"]
    assert await _isimler(client, admin, "ŞULE") == ["Şule Öztürk"]
    assert await _isimler(client, admin, "zzz") == []


async def test_arama_eposta_ve_ana_rol_adinda_da_bulur(client, user_factory, seeded_db) -> None:
    admin = await _admin(client, user_factory)
    muhasebe = await _role(seeded_db, "accounting")
    await _kisi(user_factory, "muh.kisi@sirket.co", "accounting", "Kişi Bir")
    await _kisi(user_factory, "x@t.co", "site_chief", "Kişi İki")

    assert await _isimler(client, admin, "muh.kisi@") == ["Kişi Bir"]
    assert await _isimler(client, admin, muhasebe.name.upper()) == ["Kişi Bir"]


async def test_arama_like_metakarakterleri_literaldir(client, user_factory, seeded_db) -> None:
    admin = await _admin(client, user_factory)
    await _kisi(user_factory, "p@t.co", "site_chief", "Yüzde 100%")
    await _kisi(user_factory, "q@t.co", "site_chief", "Alt_Çizgi")
    await _kisi(user_factory, "r@t.co", "site_chief", "Altaçizgi")

    assert await _isimler(client, admin, "%") == ["Yüzde 100%"]  # joker DEĞİL: hepsini getirmez
    assert await _isimler(client, admin, "alt_c") == ["Alt_Çizgi"]  # `_` tek-karakter jokeri DEĞİL
    assert await _isimler(client, admin, "   ") != []  # boşluk = süzgeç yok


async def test_total_aramayi_izler_ve_sayfalama_calisir(client, user_factory, seeded_db) -> None:
    admin = await _admin(client, user_factory)
    for i in range(5):
        await user_factory(
            email=f"s{i}@t.co", password=PASSWORD, role_key="site_chief", full_name=f"Sayfa {i}"
        )
    resp = await client.get("/users", params={"q": "sayfa", "limit": 2, "offset": 2}, headers=admin)
    body = resp.json()
    assert body["total"] == 5 and body["limit"] == 2 and body["offset"] == 2
    assert [u["full_name"] for u in body["items"]] == ["Sayfa 2", "Sayfa 3"]


# --- all_projects / project_count ---------------------------------------------------------


async def test_liste_all_projects_ve_project_count_dogru(
    client, user_factory, seeded_db, project_factory
) -> None:
    admin = await _admin(client, user_factory)
    a = await project_factory("A")
    b = await project_factory("B")
    sef = await _role(seeded_db, "site_chief")
    iki = await _kisi(user_factory, "iki@t.co", "site_chief", "İki Proje")
    tum = await _kisi(user_factory, "tum@t.co", "patron", "Tüm Proje")
    await _kisi(user_factory, "yok@t.co", "site_chief", "Projesiz")
    seeded_db.add_all(
        [
            ProjectMember(user_id=iki.id, project_id=a.id, role_id=sef.id),
            ProjectMember(user_id=iki.id, project_id=b.id, role_id=sef.id),
        ]
    )
    tum.all_projects = True
    await seeded_db.flush()

    items = {
        u["full_name"]: u
        for u in (await client.get("/users", params={"q": "proje"}, headers=admin)).json()["items"]
    }
    assert (items["İki Proje"]["all_projects"], items["İki Proje"]["project_count"]) == (False, 2)
    assert (items["Tüm Proje"]["all_projects"], items["Tüm Proje"]["project_count"]) == (True, 0)
    assert (items["Projesiz"]["all_projects"], items["Projesiz"]["project_count"]) == (False, 0)
    tek = (await client.get(f"/users/{iki.id}", headers=admin)).json()
    assert (tek["all_projects"], tek["project_count"]) == (False, 2)


async def test_liste_sorgu_sayisi_kullanici_sayisindan_bagimsiz_N_arti_1_yok(
    client, user_factory, seeded_db, project_factory
) -> None:
    admin = await _admin(client, user_factory)
    sef = await _role(seeded_db, "site_chief")
    a = await project_factory("A")

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

    ilk = await sorgu_sayisi()
    for i in range(12):
        u = await user_factory(email=f"n{i}@t.co", password=PASSWORD, role_key="site_chief")
        seeded_db.add(ProjectMember(user_id=u.id, project_id=a.id, role_id=sef.id))
    await seeded_db.flush()
    assert await sorgu_sayisi() == ilk


# --- RoleResponse.user_count --------------------------------------------------------------


async def _sayilar(session: AsyncSession) -> dict[str, int]:
    roles = list((await session.execute(select(Role))).scalars())
    return {r.key: r.user_count for r in await role_responses(session, roles)}


async def test_user_count_ana_rol_artı_proje_rolu_farkli_kullanici(
    seeded_db, user_factory, project_factory
) -> None:
    a = await project_factory("A")
    b = await project_factory("B")
    sef = await _role(seeded_db, "site_chief")
    saha = await _role(seeded_db, "field_engineer")
    muh = await _role(seeded_db, "accounting")
    once = await _sayilar(seeded_db)

    k1 = await user_factory(email="k1@t.co", password=PASSWORD, role_key="site_chief")  # ana: sef
    k2 = await user_factory(email="k2@t.co", password=PASSWORD, role_key="accounting")  # ana: muh
    seeded_db.add_all(
        [
            # k1: ana rol sef VE iki projede sef → tek kişi
            ProjectMember(user_id=k1.id, project_id=a.id, role_id=sef.id),
            ProjectMember(user_id=k1.id, project_id=b.id, role_id=sef.id),
            # k1: bir projede saha → saha'da bir kişi
            # k2: iki projede saha → aynı kişi tek sayılır
            ProjectMember(user_id=k2.id, project_id=a.id, role_id=saha.id),
            ProjectMember(user_id=k2.id, project_id=b.id, role_id=saha.id),
        ]
    )
    await seeded_db.flush()
    sonra = await _sayilar(seeded_db)
    assert sonra["site_chief"] - once["site_chief"] == 1  # k1 (ana + 2 proje) = 1
    assert sonra["accounting"] - once["accounting"] == 1  # k2 ana rolü
    assert sonra["field_engineer"] - once["field_engineer"] == 1  # k2 (2 proje) = 1
    assert muh.key == "accounting"


async def test_user_count_yalniz_proje_rolu_olan_kisi_de_sayilir_rol_silme_409(
    client, user_factory, seeded_db, project_factory
) -> None:
    """Ana rolü başka olan kişi, bu rolü yalnız PROJE ekibinde taşıyorsa rol silinemez."""
    from app.modules.roles.models import Role as R

    admin = await _admin(client, user_factory)
    a = await project_factory("A")
    ozel = R(key="ozel_proje_rolu", name="Özel", emoji="", description="", is_system=False)
    seeded_db.add(ozel)
    await seeded_db.flush()
    kisi = await user_factory(email="pr@t.co", password=PASSWORD, role_key="site_chief")
    assert (await _sayilar(seeded_db))["ozel_proje_rolu"] == 0
    seeded_db.add(ProjectMember(user_id=kisi.id, project_id=a.id, role_id=ozel.id))
    await seeded_db.flush()
    assert (await _sayilar(seeded_db))["ozel_proje_rolu"] == 1

    resp = await client.delete(f"/roles/{ozel.id}", headers=admin)
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "Bu role atanmış kullanıcılar var; önce onları başka role taşıyın"
    )

    await seeded_db.delete(
        (
            await seeded_db.execute(select(ProjectMember).where(ProjectMember.role_id == ozel.id))
        ).scalar_one()
    )
    await seeded_db.flush()
    assert (await client.delete(f"/roles/{ozel.id}", headers=admin)).status_code == 204


async def test_roles_listesi_user_count_alanini_tasir(client, user_factory, seeded_db) -> None:
    admin = await _admin(client, user_factory)
    resp = await client.get("/roles", headers=admin)
    assert resp.status_code == 200
    assert all(isinstance(r["user_count"], int) for r in resp.json())

"""IZN-B3 — `/auth/me` proje ekibi yükü: `all_projects`, `projects[]`, `role_pages`.

Çakılanlar: ekip satırı başına rol anahtarı + disiplin kimlikleri (kimliğe göre sıralı),
`role_pages` YALNIZ ana rolden farklı ekip rollerini taşır (rol başına bir kez), `all_projects`
kişide `projects`/`role_pages` BOŞ (ekip satırı yok sayılır), sorgu sayısı sabit, yük boyutu.
"""

from __future__ import annotations

import json

from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sayfalar import SAYFA_ANAHTARLARI
from app.modules.catalog.models import EvDiscipline
from app.modules.earned_value.models import ContractorType
from app.modules.roles.models import Role
from app.modules.users.models import ProjectMember, ProjectMemberDiscipline, User
from tests.conftest import test_engine

PASSWORD = "parola1234"


async def _role(session: AsyncSession, key: str) -> Role:
    return (await session.execute(select(Role).where(Role.key == key))).scalar_one()


async def _me(client: AsyncClient, session: AsyncSession, user: User) -> dict:
    login = await client.post("/auth/login", json={"email": user.email, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    session.expunge_all()  # ortak test oturumu: gerçek istekte oturum ayrıdır
    resp = await client.get("/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _disiplin(session: AsyncSession, code: str) -> EvDiscipline:
    row = EvDiscipline(
        code=code, name=code, color="#2563EB", default_contractor_type=ContractorType.OWN
    )
    session.add(row)
    await session.flush()
    return row


async def test_ekipsiz_kisi_bos_projects_ve_role_pages(client, user_factory, seeded_db) -> None:
    kisi = await user_factory(email="e@t.co", password=PASSWORD, role_key="site_chief")
    me = await _me(client, seeded_db, kisi)
    assert me["all_projects"] is False
    assert me["projects"] == []
    assert me["role_pages"] == {}


async def test_projects_rol_anahtari_ve_disiplin_kimlikleri_ve_role_pages_yalniz_farkli_roller(
    client, user_factory, seeded_db, project_factory
) -> None:
    a = await project_factory("A")
    b = await project_factory("B")
    c = await project_factory("C")
    civ = await _disiplin(seeded_db, "CIV")
    elk = await _disiplin(seeded_db, "ELK")
    sef = await _role(seeded_db, "site_chief")
    saha = await _role(seeded_db, "field_engineer")
    muh = await _role(seeded_db, "accounting")
    kisi = await user_factory(email="p@t.co", password=PASSWORD, role_key="site_chief")
    uyeler = {
        a.id: ProjectMember(user_id=kisi.id, project_id=a.id, role_id=sef.id),  # ana rolle aynı
        b.id: ProjectMember(user_id=kisi.id, project_id=b.id, role_id=saha.id),
        c.id: ProjectMember(user_id=kisi.id, project_id=c.id, role_id=muh.id),
    }
    seeded_db.add_all(uyeler.values())
    await seeded_db.flush()
    seeded_db.add_all(
        [
            ProjectMemberDiscipline(member_id=uyeler[b.id].id, discipline_id=elk.id),
            ProjectMemberDiscipline(member_id=uyeler[b.id].id, discipline_id=civ.id),
        ]
    )
    await seeded_db.flush()

    me = await _me(client, seeded_db, kisi)
    by_project = {p["project_id"]: p for p in me["projects"]}
    assert [p["project_id"] for p in me["projects"]] == sorted(by_project)  # kimliğe göre sıralı
    assert by_project[str(a.id)] == {
        "project_id": str(a.id),
        "role_key": "site_chief",
        "discipline_ids": [],
    }
    assert by_project[str(b.id)]["role_key"] == "field_engineer"
    assert by_project[str(b.id)]["discipline_ids"] == sorted([str(civ.id), str(elk.id)])
    assert by_project[str(c.id)]["role_key"] == "accounting"
    # `site_chief` ana rol: role_pages'e GİRMEZ; diğer iki rol kendi haritasıyla girer.
    assert set(me["role_pages"]) == {"field_engineer", "accounting"}
    for bundle in me["role_pages"].values():
        assert set(bundle["pages"]) <= set(SAYFA_ANAHTARLARI)
        assert set(bundle) == {"pages", "hidden_fields"}
    # Rolün KENDİ haritası: muhasebe fatura sayfasını düzenler, saha mühendisi görmez.
    assert me["role_pages"]["accounting"]["pages"]["mali.yevmiye"]["level"] == "edit"
    assert me["role_pages"]["field_engineer"]["pages"]["mali.yevmiye"]["level"] == "none"


async def test_all_projects_kisi_ekip_satiri_yok_sayilir(
    client, user_factory, seeded_db, project_factory
) -> None:
    a = await project_factory("A")
    saha = await _role(seeded_db, "field_engineer")
    kisi = await user_factory(email="t@t.co", password=PASSWORD, role_key="patron")
    kisi.all_projects = True
    seeded_db.add(ProjectMember(user_id=kisi.id, project_id=a.id, role_id=saha.id))  # bayat satır
    await seeded_db.flush()
    me = await _me(client, seeded_db, kisi)
    assert me["all_projects"] is True
    assert me["projects"] == [] and me["role_pages"] == {}


async def test_sorgu_sayisi_proje_ve_rol_sayisindan_bagimsiz_ve_yuk_boyutu_sinirli(
    client, user_factory, seeded_db, project_factory
) -> None:
    kisi = await user_factory(email="b@t.co", password=PASSWORD, role_key="site_chief")
    muh = await _role(seeded_db, "accounting")
    saha = await _role(seeded_db, "field_engineer")
    login = await client.post("/auth/login", json={"email": kisi.email, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    async def olc() -> tuple[int, int]:
        ifadeler: list[str] = []

        def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
            ifadeler.append(statement)

        seeded_db.expunge_all()
        event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
        try:
            resp = await client.get("/auth/me", headers=headers)
        finally:
            event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)
        assert resp.status_code == 200
        return len(ifadeler), len(json.dumps(resp.json(), separators=(",", ":")).encode())

    async def proje_ekle(adet: int, role: Role, onek: str) -> None:
        for i in range(adet):
            p = await project_factory(f"{onek}{i}")
            seeded_db.add(ProjectMember(user_id=kisi.id, project_id=p.id, role_id=role.id))
        await seeded_db.flush()

    await proje_ekle(1, muh, "M")
    sorgu_bir, boyut_bir = await olc()
    await proje_ekle(30, muh, "X")  # AYNI rolde 30 proje daha
    sorgu_otuz, boyut_otuz = await olc()
    await proje_ekle(1, saha, "S")  # YENİ bir rol
    sorgu_rol, boyut_rol = await olc()

    # Sorgu sayısı proje ve rol sayısından BAĞIMSIZ (N+1 yok): ekip, disiplin, hücre, gizli alan.
    assert sorgu_bir == sorgu_otuz == sorgu_rol
    # Aynı rolde proje başına yük ≈ (proje kimliği + rol anahtarı + boş disiplin listesi) ≤ 120 B;
    # rol haritası proje sayısıyla DEĞİL rol sayısıyla büyür.
    assert boyut_otuz - boyut_bir <= 30 * 120, (boyut_bir, boyut_otuz)
    # Yeni rol ≈ bir sayfa haritası (100 sayfa × ~55 B ≈ 5.5 KB; ölçüldü) + 1 proje satırı.
    assert 3_000 < boyut_rol - boyut_otuz < 8_000, (boyut_otuz, boyut_rol)

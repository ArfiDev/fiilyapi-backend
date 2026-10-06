"""IZN-B3 — `GET/PUT /users/{id}/access`: ana rol + "Tüm projeler" + proje ekibi (rol + disiplin).

Çakılanlar: şekil (GET = PUT yanıtı), tam değiştirme farkı, `all_projects` kuralı, 422 sınıfları
(bilinmeyen proje/rol/disiplin, yinelenen proje, proje rolü Sistem Yöneticisi), atomiklik (hata →
HİÇBİR satır değişmez), kullanıcı satırı kilidi (SQL metni), yetki (rol atama karşılaştırması
proje rollerine de), son Sistem Yöneticisi, denetim satırı, eski uçların 410'u.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sayfalar import PageLevel
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.catalog.models import EvDiscipline
from app.modules.roles import service as roles_service
from app.modules.roles.models import Role, RolePagePermission
from app.modules.roles.schemas import RoleCreate
from app.modules.users import access_service
from app.modules.users.models import ProjectMember, ProjectMemberDiscipline, User
from app.modules.users.schemas import ProjectMemberInput, UserAccessInput
from tests.conftest import test_engine

PASSWORD = "parola1234"
ALL_PROJECTS_WITH_TEAM = access_service.ALL_PROJECTS_WITH_TEAM


async def _headers(client: AsyncClient, user_factory, role_key: str) -> dict[str, str]:
    email = f"{role_key}.{uuid.uuid4().hex[:6]}@izn-b3.co"
    await user_factory(email=email, password=PASSWORD, role_key=role_key)
    login = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _role(session: AsyncSession, key: str) -> Role:
    return (await session.execute(select(Role).where(Role.key == key))).scalar_one()


async def _discipline(session: AsyncSession, code: str, name: str) -> EvDiscipline:
    from app.modules.earned_value.models import ContractorType

    row = EvDiscipline(
        code=code,
        name=name,
        color="#2563EB",
        default_contractor_type=ContractorType.OWN,
        sort_order=0,
    )
    session.add(row)
    await session.flush()
    return row


def _put_body(role: Role, *entries: tuple, all_projects: bool = False) -> dict:
    """entries: (project, role, [disiplinler])."""
    return {
        "role_id": str(role.id),
        "all_projects": all_projects,
        "projects": [
            {
                "project_id": str(project.id),
                "role_id": str(member_role.id),
                "discipline_ids": [str(d.id) for d in disciplines],
            }
            for project, member_role, disciplines in entries
        ],
    }


@pytest.fixture
async def admin(client: AsyncClient, user_factory) -> dict[str, str]:
    return await _headers(client, user_factory, "system_admin")


@pytest.fixture
async def hedef(seeded_db: AsyncSession, user_factory) -> User:
    return await user_factory(
        email=f"hedef.{uuid.uuid4().hex[:6]}@izn-b3.co",
        password=PASSWORD,
        role_key="site_chief",
        full_name="Hedef Kişi",
    )


async def _members(session: AsyncSession, user_id: uuid.UUID) -> dict[uuid.UUID, ProjectMember]:
    rows = (
        await session.execute(select(ProjectMember).where(ProjectMember.user_id == user_id))
    ).scalars()
    return {m.project_id: m for m in rows}


async def _member_disciplines(session: AsyncSession, member_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await session.execute(
        select(ProjectMemberDiscipline.discipline_id).where(
            ProjectMemberDiscipline.member_id == member_id
        )
    )
    return set(rows.scalars())


async def _fresh_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    await session.refresh(await session.get(User, user_id))
    return await session.get(User, user_id)  # type: ignore[return-value]


# --- GET ----------------------------------------------------------------------------------


async def test_get_ekipsiz_kullanici_bos_ekip_doner(client, admin, hedef) -> None:
    resp = await client.get(f"/users/{hedef.id}/access", headers=admin)
    assert resp.status_code == 200
    assert resp.json() == {"role_id": str(hedef.role_id), "all_projects": False, "projects": []}


async def test_get_olmayan_kullanici_404(client, admin) -> None:
    resp = await client.get(f"/users/{uuid.uuid4()}/access", headers=admin)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Kullanıcı bulunamadı"


# --- PUT: şekil ve fark -------------------------------------------------------------------


async def test_put_ana_rol_ekip_rol_ve_disiplin_yazar_get_ayni_sekli_doner(
    client, admin, hedef, seeded_db, project_factory
) -> None:
    kule = await project_factory("KULE", name="Kule")
    kopru = await project_factory("KOPRU", name="Köprü")
    civ = await _discipline(seeded_db, "CIV", "İnşaat")
    elk = await _discipline(seeded_db, "ELK", "Elektrik")
    patron = await _role(seeded_db, "patron")
    saha = await _role(seeded_db, "field_engineer")
    sef = await _role(seeded_db, "site_chief")

    body = _put_body(patron, (kule, saha, [elk, civ]), (kopru, sef, []))
    resp = await client.put(f"/users/{hedef.id}/access", json=body, headers=admin)
    assert resp.status_code == 200, resp.text
    beklenen = {
        "role_id": str(patron.id),
        "all_projects": False,
        "projects": [  # proje ADINA göre sıralı: Köprü, Kule
            {
                "project_id": str(kopru.id),
                "project_name": "Köprü",
                "role_id": str(sef.id),
                "disciplines": [],
            },
            {
                "project_id": str(kule.id),
                "project_name": "Kule",
                "role_id": str(saha.id),
                "disciplines": [  # kod sırasıyla: CIV, ELK
                    {"id": str(civ.id), "code": "CIV", "name": "İnşaat", "color": "#2563EB"},
                    {"id": str(elk.id), "code": "ELK", "name": "Elektrik", "color": "#2563EB"},
                ],
            },
        ],
    }
    assert resp.json() == beklenen
    assert (await client.get(f"/users/{hedef.id}/access", headers=admin)).json() == beklenen
    hedef_taze = await _fresh_user(seeded_db, hedef.id)
    assert hedef_taze.role_id == patron.id and hedef_taze.all_projects is False


async def test_put_tam_degistirir_kalan_uyenin_kimligi_korunur_cikan_gider(
    client, admin, hedef, seeded_db, project_factory
) -> None:
    a = await project_factory("A", name="A")
    b = await project_factory("B", name="B")
    c = await project_factory("C", name="C")
    civ = await _discipline(seeded_db, "CIV", "İnşaat")
    elk = await _discipline(seeded_db, "ELK", "Elektrik")
    sef = await _role(seeded_db, "site_chief")
    saha = await _role(seeded_db, "field_engineer")

    await client.put(
        f"/users/{hedef.id}/access",
        json=_put_body(sef, (a, sef, [civ]), (b, sef, [])),
        headers=admin,
    )
    once = await _members(seeded_db, hedef.id)
    a_uye_id = once[a.id].id

    resp = await client.put(
        f"/users/{hedef.id}/access",
        json=_put_body(sef, (a, saha, [elk]), (c, sef, [civ, elk])),
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    sonra = await _members(seeded_db, hedef.id)
    assert set(sonra) == {a.id, c.id}  # B çıktı, C girdi
    assert sonra[a.id].id == a_uye_id  # kalan üye: aynı satır (sil-yeniden-ekle DEĞİL)
    assert sonra[a.id].role_id == saha.id  # rol güncellendi
    assert await _member_disciplines(seeded_db, a_uye_id) == {elk.id}  # civ düştü, elk girdi
    assert await _member_disciplines(seeded_db, sonra[c.id].id) == {civ.id, elk.id}
    # Çıkan üyenin disiplin satırları CASCADE ile gitti.
    kalan = (
        (
            await seeded_db.execute(
                select(ProjectMemberDiscipline).where(
                    ProjectMemberDiscipline.member_id == once[b.id].id
                )
            )
        )
        .scalars()
        .all()
    )
    assert kalan == []


async def test_yinelenen_disiplin_kimligi_tekillesir(
    client, admin, hedef, seeded_db, project_factory
) -> None:
    a = await project_factory("A")
    civ = await _discipline(seeded_db, "CIV", "İnşaat")
    sef = await _role(seeded_db, "site_chief")
    resp = await client.put(
        f"/users/{hedef.id}/access",
        json=_put_body(sef, (a, sef, [civ, civ])),
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert [d["code"] for d in resp.json()["projects"][0]["disciplines"]] == ["CIV"]


async def test_all_projects_true_ekip_satirlarini_siler(
    client, admin, hedef, seeded_db, project_factory
) -> None:
    a = await project_factory("A")
    sef = await _role(seeded_db, "site_chief")
    patron = await _role(seeded_db, "patron")
    await client.put(f"/users/{hedef.id}/access", json=_put_body(sef, (a, sef, [])), headers=admin)
    assert await _members(seeded_db, hedef.id)

    resp = await client.put(
        f"/users/{hedef.id}/access", json=_put_body(patron, all_projects=True), headers=admin
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"role_id": str(patron.id), "all_projects": True, "projects": []}
    assert await _members(seeded_db, hedef.id) == {}
    assert (await _fresh_user(seeded_db, hedef.id)).all_projects is True


async def test_bos_put_ekibi_ve_all_projects_i_temizler(client, admin, hedef, seeded_db) -> None:
    sef = await _role(seeded_db, "site_chief")
    resp = await client.put(
        f"/users/{hedef.id}/access", json=_put_body(sef, all_projects=False), headers=admin
    )
    assert resp.status_code == 200
    assert resp.json()["projects"] == []


# --- PUT: 422 sınıfları ve atomiklik ------------------------------------------------------


async def test_all_projects_ile_ekip_satiri_422(
    client, admin, hedef, seeded_db, project_factory
) -> None:
    a = await project_factory("A")
    sef = await _role(seeded_db, "site_chief")
    resp = await client.put(
        f"/users/{hedef.id}/access",
        json=_put_body(sef, (a, sef, []), all_projects=True),
        headers=admin,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == ALL_PROJECTS_WITH_TEAM


async def test_ayni_proje_iki_kez_422(client, admin, hedef, seeded_db, project_factory) -> None:
    a = await project_factory("A")
    sef = await _role(seeded_db, "site_chief")
    saha = await _role(seeded_db, "field_engineer")
    resp = await client.put(
        f"/users/{hedef.id}/access",
        json=_put_body(sef, (a, sef, []), (a, saha, [])),
        headers=admin,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Aynı proje birden fazla kez eklenmiş"


@pytest.mark.parametrize("neyi", ["proje", "rol_ana", "rol_proje", "disiplin"])
async def test_bilinmeyen_proje_rol_disiplin_422_ve_hicbir_sey_yazilmaz(
    client, admin, hedef, seeded_db, project_factory, neyi
) -> None:
    """Geçerli bir kısım (ana rol değişimi + geçerli proje) ile geçersiz bir kısım BİRLİKTE
    gelir: 422 dönmeli ve geçerli kısım da YAZILMAMALI (atomik)."""
    gecerli = await project_factory("GECERLI")
    patron = await _role(seeded_db, "patron")
    sef = await _role(seeded_db, "site_chief")
    once_rol = hedef.role_id
    govde = _put_body(patron, (gecerli, sef, []))
    bilinmeyen = str(uuid.uuid4())
    if neyi == "proje":
        govde["projects"].append({"project_id": bilinmeyen, "role_id": str(sef.id)})
        mesaj = f"Bilinmeyen proje: {bilinmeyen}"
    elif neyi == "rol_ana":
        govde["role_id"] = bilinmeyen
        mesaj = f"Bilinmeyen rol: {bilinmeyen}"
    elif neyi == "rol_proje":
        govde["projects"][0]["role_id"] = bilinmeyen
        mesaj = f"Bilinmeyen rol: {bilinmeyen}"
    else:
        govde["projects"][0]["discipline_ids"] = [bilinmeyen]
        mesaj = f"Bilinmeyen disiplin: {bilinmeyen}"

    resp = await client.put(f"/users/{hedef.id}/access", json=govde, headers=admin)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == mesaj
    assert (await _fresh_user(seeded_db, hedef.id)).role_id == once_rol
    assert await _members(seeded_db, hedef.id) == {}


async def test_proje_rolu_sistem_yoneticisi_olamaz_422(
    client, admin, hedef, seeded_db, project_factory
) -> None:
    a = await project_factory("A")
    sisyon = await _role(seeded_db, "system_admin")
    sef = await _role(seeded_db, "site_chief")
    resp = await client.put(
        f"/users/{hedef.id}/access", json=_put_body(sef, (a, sisyon, [])), headers=admin
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == access_service.SYSTEM_ADMIN_AS_PROJECT_ROLE


async def test_disiplin_listesi_ust_siniri_422(client, admin, hedef, seeded_db, project_factory):
    a = await project_factory("A")
    sef = await _role(seeded_db, "site_chief")
    govde = _put_body(sef, (a, sef, []))
    govde["projects"][0]["discipline_ids"] = [str(uuid.uuid4()) for _ in range(101)]
    resp = await client.put(f"/users/{hedef.id}/access", json=govde, headers=admin)
    assert resp.status_code == 422


async def test_olmayan_kullanici_put_404(client, admin, seeded_db) -> None:
    sef = await _role(seeded_db, "site_chief")
    resp = await client.put(f"/users/{uuid.uuid4()}/access", json=_put_body(sef), headers=admin)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Kullanıcı bulunamadı"


async def test_servis_kendi_transaction_i_acmaz_savepoint_geri_alinca_hicbir_sey_kalmaz(
    seeded_db, user_factory, project_factory
) -> None:
    """ATOMİKLİK: tüm yazma çağıranın transaction'ındadır. Servis çağrısı bittikten sonra bir
    hata (örn. denetim satırı yazımı) yazmaları geri alabilmeli: savepoint geri alınınca ana rol,
    `all_projects`, ekip ve disiplinler HEP BİRLİKTE eski hâline döner."""
    admin_user = await user_factory(email="a@izn-b3.co", password=PASSWORD, role_key="system_admin")
    hedef = await user_factory(email="h@izn-b3.co", password=PASSWORD, role_key="site_chief")
    a = await project_factory("A")
    civ = await _discipline(seeded_db, "CIV", "İnşaat")
    patron = await _role(seeded_db, "patron")
    sef = await _role(seeded_db, "site_chief")
    once_rol = hedef.role_id
    hedef_id = hedef.id

    class _Patla(RuntimeError):
        pass

    with pytest.raises(_Patla):
        async with seeded_db.begin_nested():
            await access_service.replace_access(
                seeded_db,
                admin_user,
                hedef_id,
                UserAccessInput(
                    role_id=patron.id,
                    projects=[
                        ProjectMemberInput(project_id=a.id, role_id=sef.id, discipline_ids=[civ.id])
                    ],
                ),
            )
            # Yazmalar gerçekten yapıldı (aynı transaction'da görünür) …
            assert await _members(seeded_db, hedef_id)
            raise _Patla
    # … ve savepoint geri alınınca HİÇBİRİ kalmadı.
    seeded_db.expire_all()
    assert (await seeded_db.get(User, hedef_id)).role_id == once_rol
    assert await _members(seeded_db, hedef_id) == {}
    kalan = (await seeded_db.execute(select(ProjectMemberDiscipline))).scalars().all()
    assert kalan == []


async def test_put_kullanici_satirini_for_update_ile_kilitler(
    seeded_db, user_factory, project_factory
) -> None:
    """Bekçi SQL-METİN testidir (davranış testi kilit kalksa da yeşil kalabilir)."""
    admin_user = await user_factory(email="a@izn-b3.co", password=PASSWORD, role_key="system_admin")
    hedef = await user_factory(email="h@izn-b3.co", password=PASSWORD, role_key="site_chief")
    a = await project_factory("A")
    sef = await _role(seeded_db, "site_chief")
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        await access_service.replace_access(
            seeded_db,
            admin_user,
            hedef.id,
            UserAccessInput(
                role_id=sef.id,
                projects=[ProjectMemberInput(project_id=a.id, role_id=sef.id)],
            ),
        )
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)
    kilitli = [i for i in ifadeler if "FOR UPDATE" in i and "FROM users" in i]
    assert kilitli, f"tam-değiştirme kullanıcı satırını FOR UPDATE ile okumadı: {ifadeler}"


# --- PUT: yetki ---------------------------------------------------------------------------


async def _custom_actor(
    client: AsyncClient, user_factory, session: AsyncSession, cells: dict[str, PageLevel]
) -> dict[str, str]:
    """Verilen sayfa düzeylerine sahip özel rolde bir kullanıcı açıp giriş yapar."""
    key = f"ozel_{uuid.uuid4().hex[:8]}"
    role = await roles_service.create_custom_role(
        session, RoleCreate(key=key, name=key, emoji="", description="")
    )
    for page_key, level in cells.items():
        await session.execute(
            update(RolePagePermission)
            .where(RolePagePermission.role_id == role.id, RolePagePermission.page_key == page_key)
            .values(level=level)
        )
    await session.flush()
    return await _headers(client, user_factory, key)


async def test_kullanicilar_sayfasi_duzenler_olmayan_put_403_get_icin_gorur_yeter(
    client, user_factory, hedef, seeded_db
) -> None:
    sef = await _role(seeded_db, "site_chief")
    muhasebe = await _headers(client, user_factory, "accounting")
    assert (
        await client.put(f"/users/{hedef.id}/access", json=_put_body(sef), headers=muhasebe)
    ).status_code == 403
    assert (await client.get(f"/users/{hedef.id}/access", headers=muhasebe)).status_code == 403

    goruntuleyen = await _custom_actor(
        client, user_factory, seeded_db, {"ayarlar.kullanicilar": PageLevel.view}
    )
    assert (await client.get(f"/users/{hedef.id}/access", headers=goruntuleyen)).status_code == 200
    assert (
        await client.put(f"/users/{hedef.id}/access", json=_put_body(sef), headers=goruntuleyen)
    ).status_code == 403


async def test_kimliksiz_401(client, hedef) -> None:
    assert (await client.get(f"/users/{hedef.id}/access")).status_code == 401
    assert (
        await client.put(f"/users/{hedef.id}/access", json={"role_id": str(uuid.uuid4())})
    ).status_code == 401


async def test_sistem_yoneticisi_rolunu_yalniz_sistem_yoneticisi_atar(
    client, user_factory, hedef, seeded_db
) -> None:
    yonetici = await _custom_actor(
        client, user_factory, seeded_db, {"ayarlar.kullanicilar": PageLevel.edit}
    )
    sisyon = await _role(seeded_db, "system_admin")
    resp = await client.put(f"/users/{hedef.id}/access", json=_put_body(sisyon), headers=yonetici)
    assert resp.status_code == 403
    assert (
        resp.json()["detail"]
        == "Sistem Yöneticisi rolü yalnızca Sistem Yöneticisi tarafından atanabilir"
    )


async def test_aktorun_yetkisini_asan_rol_proje_rolu_olarak_da_atanamaz_ama_degismeyen_rol_sorulmaz(
    client, user_factory, hedef, seeded_db, project_factory
) -> None:
    """Hücre karşılaştırması PROJE ROLLERİNE de uygulanır; yalnız YENİ verilen role (değişmeyen
    rol bir yetki vermez, yoksa yüksek rollü kişinin disiplini bile düzenlenemezdi)."""
    a = await project_factory("A")
    civ = await _discipline(seeded_db, "CIV", "İnşaat")
    muhasebe = await _role(seeded_db, "accounting")  # aktörün hücrelerini aşar
    sef = await _role(seeded_db, "site_chief")
    yonetici = await _custom_actor(
        client,
        user_factory,
        seeded_db,
        {"ayarlar.kullanicilar": PageLevel.edit, "santiye.puantaj": PageLevel.view},
    )

    # Ana rol sef (aktör kendi hücrelerini aşıyor mu? sef'in hücreleri aktörünkini aşar) → 403.
    resp = await client.put(
        f"/users/{hedef.id}/access", json=_put_body(muhasebe, (a, sef, [])), headers=yonetici
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Sahip olmadığınız yetkileri içeren bir rol atayamazsınız"

    # Aynı rolleri bir Sistem Yöneticisi atadı; yönetici yalnız DİSİPLİNİ düzenliyor: değişmeyen
    # roller (ana + proje) yeniden sorulmaz → 200.
    admin = await _headers(client, user_factory, "system_admin")
    await client.put(
        f"/users/{hedef.id}/access", json=_put_body(muhasebe, (a, sef, [])), headers=admin
    )
    resp = await client.put(
        f"/users/{hedef.id}/access",
        json=_put_body(muhasebe, (a, sef, [civ])),
        headers=yonetici,
    )
    assert resp.status_code == 200, resp.text
    assert [d["code"] for d in resp.json()["projects"][0]["disciplines"]] == ["CIV"]

    # Proje rolünü aşan bir role DEĞİŞTİRMEK yine yasak.
    resp = await client.put(
        f"/users/{hedef.id}/access",
        json=_put_body(muhasebe, (a, muhasebe, [civ])),
        headers=yonetici,
    )
    assert resp.status_code == 403  # sef → muhasebe: YENİ verilen proje rolü aktörü aşıyor


async def test_son_aktif_sistem_yoneticisi_dusurulemez_400(
    client, admin, seeded_db, user_factory
) -> None:
    # `admin` fixture'ı tek aktif Sistem Yöneticisi: onu başka role çekmek reddedilir.
    sef = await _role(seeded_db, "site_chief")
    ben = (
        await seeded_db.execute(
            select(User).where(User.role_id == (await _role(seeded_db, "system_admin")).id)
        )
    ).scalar_one()
    resp = await client.put(f"/users/{ben.id}/access", json=_put_body(sef), headers=admin)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Son aktif Sistem Yöneticisi düşürülemez"

    # İkinci aktif Sistem Yöneticisi varsa düşürülebilir.
    await user_factory(email="ikinci@izn-b3.co", password=PASSWORD, role_key="system_admin")
    resp = await client.put(f"/users/{ben.id}/access", json=_put_body(sef), headers=admin)
    assert resp.status_code == 200, resp.text


# --- denetim ve eski uçlar ----------------------------------------------------------------


async def test_put_denetim_satiri_yazar(client, admin, hedef, seeded_db, project_factory) -> None:
    a = await project_factory("A")
    patron = await _role(seeded_db, "patron")
    sef = await _role(seeded_db, "site_chief")
    resp = await client.put(
        f"/users/{hedef.id}/access", json=_put_body(patron, (a, sef, [])), headers=admin
    )
    assert resp.status_code == 200
    satirlar = (
        (await seeded_db.execute(select(AuditLog).where(AuditLog.action == AuditAction.update)))
        .scalars()
        .all()
    )
    assert [s.detail for s in satirlar] == [
        "Kullanıcı erişimi güncellendi: Hedef Kişi · ana rol Patron · 1 projede ekip"
    ]
    resp = await client.put(
        f"/users/{hedef.id}/access", json=_put_body(patron, all_projects=True), headers=admin
    )
    satirlar = (
        (await seeded_db.execute(select(AuditLog).where(AuditLog.action == AuditAction.update)))
        .scalars()
        .all()
    )
    assert satirlar[-1].detail == (
        "Kullanıcı erişimi güncellendi: Hedef Kişi · ana rol Patron · tüm projeler"
    )


async def test_get_denetim_satiri_yazmaz(client, admin, hedef, seeded_db) -> None:
    await client.get(f"/users/{hedef.id}/access", headers=admin)
    satirlar = (
        (await seeded_db.execute(select(AuditLog).where(AuditLog.action == AuditAction.update)))
        .scalars()
        .all()
    )
    assert satirlar == []


@pytest.mark.parametrize(
    ("yontem", "yol"),
    [
        ("GET", "project-access"),
        ("PUT", "project-access"),
    ],
)
async def test_eski_uclar_410_ve_hicbir_sey_yazmaz(
    client, admin, hedef, seeded_db, yontem, yol
) -> None:
    resp = await client.request(yontem, f"/users/{hedef.id}/{yol}", json={}, headers=admin)
    assert resp.status_code == 410
    assert "/users/{id}/access" in resp.json()["detail"]
    assert await _members(seeded_db, hedef.id) == {}


async def test_eski_uclar_yetkisiz_aktore_403_gorunur(client, user_factory, hedef) -> None:
    muhasebe = await _headers(client, user_factory, "accounting")
    resp = await client.put(f"/users/{hedef.id}/project-access", json={}, headers=muhasebe)
    assert resp.status_code == 403


@pytest.mark.parametrize("yontem", ["GET", "PUT"])
async def test_disiplin_uclari_B6b_de_sokuldu(client, admin, hedef, seeded_db, yontem) -> None:
    """IZN-B6b: `/users/{id}/disciplines` (410) tamamen söküldü; yol artık yok."""
    resp = await client.request(yontem, f"/users/{hedef.id}/disciplines", json={}, headers=admin)
    assert resp.status_code in (404, 405)
    assert await _members(seeded_db, hedef.id) == {}

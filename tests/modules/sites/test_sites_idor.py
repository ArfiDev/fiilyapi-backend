"""T15 — IDOR negatif seti, spec §12.5'in 21-33 numarali satirlarinin TAMAMI.

## Neden ayri bir dosya

P2'de IDOR tam bu noktada yakalandi: bolum kimligi santiye ve proje kimliginden
BAGIMSIZ gorunur, dolayisiyla gorunurluk suzgeci yukari dogru cozulmezse baska
bir projenin kaydina dolayli erisim acilir. Silme uclari (§7.1) bu dilimde acilan
YENI bir yuzeydir ve ayni tuzagi tasir — ustelik orada hata GERI ALINAMAZ.

## Bu dosyanin sabitledigi uc ayri karar

1. **Gorunmeyen kayit 404'tur, 403 degil** — ve govdesi var olmayan bir UUID'nin
   govdesiyle BIREBIR AYNIDIR. Aksi hâlde elinde kimlik olan bir kullanici
   kaydin var oldugunu ogrenirdi.
2. **`sites:full` SILMEYI KAPSAMAZ** (2026-07-30 kullanici karari). 29 ve 30
   numarali vakalar bu kararin TEK testidir; silinirse karar sessizce kaybolur.
3. **Yetki gorunurlugun ONUNE GECMEZ** (33): `sites:admin` tasiyan ama projeye
   erisimi olmayan kullanici da 404 alir. "Yetkiliyse soyleyebiliriz" kestirmesi
   yetkili hesabi bir kesif araci hâline getirirdi.
"""

import uuid

from sqlalchemy import func, select

from app.core.access import AccessLevel, Scope
from app.modules.roles.models import Module, Role, RolePermission
from app.modules.sites.models import Section, Site
from tests._legacy_permission_yardimcisi import sync_page_cells
from tests._silme_yardimci import sil_aile

SITE_MISSING = "Şantiye bulunamadı"
SYSTEM_ADMIN_ONLY = "Bu işlemi yalnızca Sistem Yöneticisi yapabilir"
SECTION_MISSING = "Bölüm bulunamadı"
PROJECT_MISSING = "Proje bulunamadı"
USER_MISSING = "Seçilen kullanıcı bulunamadı"

WRITE_ROLE = "patron"  # sites=full
VIEW_ROLE = "site_chief"  # sites=view
ADMIN_ROLE = "system_admin"  # sites=admin (silme)
NONE_ROLE = "procurement"  # izni testte acikca none'a cekilir


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, session, user_factory, role_key: str, *, grant_all: bool) -> str:
    address = f"{role_key}-{uuid.uuid4().hex[:6]}@t.co"
    user = await user_factory(email=address, password="parola1234", role_key=role_key)
    if grant_all:
        user.all_projects = True
        await session.flush()
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


async def _set_permission(
    session, role_key: str, module_key: str, level: AccessLevel, scope: Scope = Scope.all
) -> None:
    """Izin kapisini seed matrisinden BAGIMSIZ kilar.

    Matris kullanici tarafindan duzenlenebilir; testin dayanagi seed degeri
    olsaydi matris degistigi gun test sessizce anlamsizlasirdi.
    """
    role_id = (await session.execute(select(Role.id).where(Role.key == role_key))).scalar_one()
    module_id = (
        await session.execute(select(Module.id).where(Module.key == module_key))
    ).scalar_one()
    permission = (
        await session.execute(
            select(RolePermission).where(
                RolePermission.role_id == role_id, RolePermission.module_id == module_id
            )
        )
    ).scalar_one()
    permission.access_level = level
    permission.scope = scope
    await session.flush()
    await sync_page_cells(session, permission.role_id)


async def _tree(session, project_factory, code: str = "IDOR") -> tuple[Site, Section]:
    project = await project_factory(f"{code}-{uuid.uuid4().hex[:6]}")
    site = Site(project_id=project.id, code=f"SNT-{uuid.uuid4().hex[:6]}", name="Gizli Şantiye")
    session.add(site)
    await session.flush()
    section = Section(site_id=site.id, name="Gizli Bölüm")
    session.add(section)
    await session.flush()
    return site, section


async def _exists(session, model, identifier) -> bool:
    stmt = select(func.count()).select_from(model).where(model.id == identifier)
    return int((await session.execute(stmt)).scalar_one()) == 1


# --- 21: gorunmeyen projeye POST ---


async def test_post_to_invisible_project_returns_404(
    client, db_session, user_factory, project_factory
):
    """Yazma izni VAR, proje gorunur DEGIL -> 404 `Proje bulunamadı` (403 degil)."""
    project = await project_factory("IDOR-21")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=False)
    sites_before = int(
        (await db_session.execute(select(func.count()).select_from(Site))).scalar_one()
    )

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json={"name": "Sızıntı Şantiyesi", "is_draft": True},
        headers=_auth(token),
    )

    assert resp.status_code == 404, resp.text
    assert resp.json() == {"detail": PROJECT_MISSING}
    unknown = await client.post(
        f"/projects/{uuid.uuid4()}/sites",
        json={"name": "Sızıntı Şantiyesi", "is_draft": True},
        headers=_auth(token),
    )
    assert unknown.json() == resp.json()
    assert (
        int((await db_session.execute(select(func.count()).select_from(Site))).scalar_one())
        == sites_before
    )


# --- 22: gorunmeyen santiyeye GET / PATCH ---


async def test_get_invisible_site_returns_404(client, db_session, user_factory, project_factory):
    site, _ = await _tree(db_session, project_factory, "IDOR-22a")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=False)

    invisible = await client.get(f"/sites/{site.id}", headers=_auth(token))
    unknown = await client.get(f"/sites/{uuid.uuid4()}", headers=_auth(token))

    assert invisible.status_code == unknown.status_code == 404
    assert invisible.json() == unknown.json() == {"detail": SITE_MISSING}


async def test_patch_invisible_site_returns_404_same_body_as_unknown_uuid(
    client, db_session, user_factory, project_factory
):
    """Gorunmeyen GERCEK kayit ile var olmayan UUID AYIRT EDILEMEZ olmali."""
    site, _ = await _tree(db_session, project_factory, "IDOR-22b")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=False)

    invisible = await client.patch(
        f"/sites/{site.id}", json={"name": "Sızıntı"}, headers=_auth(token)
    )
    unknown = await client.patch(
        f"/sites/{uuid.uuid4()}", json={"name": "Sızıntı"}, headers=_auth(token)
    )

    assert invisible.status_code == unknown.status_code == 404
    assert invisible.json() == unknown.json() == {"detail": SITE_MISSING}
    await db_session.refresh(site)
    assert site.name == "Gizli Şantiye"


# --- 23: gorunmeyen santiyenin bolumune PATCH ---


async def test_patch_section_of_invisible_site_returns_404(
    client, db_session, user_factory, project_factory
):
    """EN KOLAY ATLANACAK NOKTA: bolum -> santiye -> proje zinciri cozulmezse
    baska bir projenin bolumu sessizce duzenlenebilir."""
    _, section = await _tree(db_session, project_factory, "IDOR-23")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=False)

    invisible = await client.patch(
        f"/sections/{section.id}", json={"name": "Sızıntı"}, headers=_auth(token)
    )
    unknown = await client.patch(
        f"/sections/{uuid.uuid4()}", json={"name": "Sızıntı"}, headers=_auth(token)
    )

    assert invisible.status_code == unknown.status_code == 404
    assert invisible.json() == unknown.json() == {"detail": SECTION_MISSING}
    await db_session.refresh(section)
    assert section.name == "Gizli Bölüm"


# --- 24: sites:view yazamaz ---


async def test_view_permission_rejects_writes_403(
    client, db_session, user_factory, project_factory
):
    """Okuma izni yazmaya DONUSMEZ; kapi servise ulasmadan kapanir (403)."""
    await _set_permission(db_session, VIEW_ROLE, "sites", AccessLevel.view)
    site, _ = await _tree(db_session, project_factory, "IDOR-24")
    token = await _login(client, db_session, user_factory, VIEW_ROLE, grant_all=True)

    post = await client.post(
        f"/projects/{site.project_id}/sites",
        json={"name": "Sızıntı Şantiyesi", "is_draft": True},
        headers=_auth(token),
    )
    patch = await client.patch(f"/sites/{site.id}", json={"name": "Sızıntı"}, headers=_auth(token))
    post_section = await client.post(
        f"/sites/{site.id}/sections", json={"name": "Sızıntı"}, headers=_auth(token)
    )

    assert post.status_code == patch.status_code == post_section.status_code == 403
    assert (await client.get(f"/sites/{site.id}", headers=_auth(token))).status_code == 200
    await db_session.refresh(site)
    assert site.name == "Gizli Şantiye"


# --- 25: izinsiz kullanici ---


async def test_no_permission_returns_403(client, db_session, user_factory, project_factory):
    """Seed matrisinde `sites:none` tasiyan rol YOK; izin satiri testte acikca
    none'a cekilir (matris kullanici tarafindan duzenlenebilir)."""
    await _set_permission(db_session, NONE_ROLE, "sites", AccessLevel.none)
    site, section = await _tree(db_session, project_factory, "IDOR-25")
    token = await _login(client, db_session, user_factory, NONE_ROLE, grant_all=True)

    calls = [
        await client.get(f"/projects/{site.project_id}/sites", headers=_auth(token)),
        await client.get(f"/sites/{site.id}", headers=_auth(token)),
        await client.get(f"/sites/{site.id}/sections", headers=_auth(token)),
        await client.post(
            f"/projects/{site.project_id}/sites",
            json={"name": "Sızıntı", "is_draft": True},
            headers=_auth(token),
        ),
        await client.patch(f"/sites/{site.id}", json={"name": "Sızıntı"}, headers=_auth(token)),
        await client.patch(
            f"/sections/{section.id}", json={"name": "Sızıntı"}, headers=_auth(token)
        ),
    ]

    assert [call.status_code for call in calls] == [403] * 6


# --- 26: rastgele kullanici UUID'si ---


async def test_random_manager_user_uuid_returns_422_and_writes_nothing(
    client, db_session, user_factory, project_factory
):
    """Kaynak SANTIYEDIR, kullanici degil -> 404 degil 422; ve HICBIR SEY yazilmaz."""
    project = await project_factory("IDOR-26")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)
    sites_before = int(
        (await db_session.execute(select(func.count()).select_from(Site))).scalar_one()
    )
    sections_before = int(
        (await db_session.execute(select(func.count()).select_from(Section))).scalar_one()
    )

    site_manager = await client.post(
        f"/projects/{project.id}/sites",
        json={
            "name": "Hayalet Şef",
            "is_draft": True,
            "site_manager_user_id": str(uuid.uuid4()),
        },
        headers=_auth(token),
    )
    section_manager = await client.post(
        f"/projects/{project.id}/sites",
        json={
            "name": "Hayalet Bölüm Şefi",
            "is_draft": True,
            "sections": [{"name": "Kaba İnşaat", "manager_user_id": str(uuid.uuid4())}],
        },
        headers=_auth(token),
    )

    assert site_manager.status_code == section_manager.status_code == 422
    assert site_manager.json() == {"detail": USER_MISSING}
    assert section_manager.json() == {"detail": USER_MISSING}
    assert (
        int((await db_session.execute(select(func.count()).select_from(Site))).scalar_one())
        == sites_before
    )
    assert (
        int((await db_session.execute(select(func.count()).select_from(Section))).scalar_one())
        == sections_before
    )


# --- 27/28: gorunmeyen kayda DELETE ---


async def test_delete_site_invisible_returns_404_and_record_survives(
    client, db_session, user_factory, project_factory
):
    """27. `sites:admin` VAR ama Sistem Yoneticisi DEGIL -> 403 (SIL-B1: kapi once kosar);
    gorunmeyen ve var olmayan kimlik AYNI yaniti alir (varlik sizmaz) + kayit YERINDE."""
    site, _ = await _tree(db_session, project_factory, "IDOR-27")
    await _set_permission(db_session, WRITE_ROLE, "sites", AccessLevel.admin)
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=False)

    invisible = await sil_aile(client, _auth(token), "site", site.id)
    unknown = await sil_aile(client, _auth(token), "site", uuid.uuid4())

    assert invisible.status_code == unknown.status_code == 403
    assert invisible.json() == unknown.json() == {"detail": SYSTEM_ADMIN_ONLY}
    assert await _exists(db_session, Site, site.id)


async def test_delete_section_invisible_returns_404_and_record_survives(
    client, db_session, user_factory, project_factory
):
    """28. Bolum kimligi uzerinden dolayli silme de gorunurluk suzgecinden gecer."""
    _, section = await _tree(db_session, project_factory, "IDOR-28")
    await _set_permission(db_session, WRITE_ROLE, "sites", AccessLevel.admin)
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=False)

    invisible = await sil_aile(client, _auth(token), "section", section.id)
    unknown = await sil_aile(client, _auth(token), "section", uuid.uuid4())

    assert invisible.status_code == unknown.status_code == 403
    assert invisible.json() == unknown.json() == {"detail": SYSTEM_ADMIN_ONLY}
    assert await _exists(db_session, Section, section.id)


# --- 29/30: `sites:full` SILEMEZ (ATLANAMAZ) ---


async def test_delete_site_with_full_permission_returns_403(
    client, db_session, user_factory, project_factory
):
    """29 — bu dilimin EN KRITIK vakasi.

    2026-07-30 karari: `full` yazmayi kapsar, SILMEYI KAPSAMAZ. Bu test o
    kararin TEK kanitidir; kapi kazara `_FULL`'e dusurulurse yalniz burada
    goruluyor olur. Kullanicinin projeye erisimi VARDIR — yani 404 degil,
    dogrudan izin kapisindan 403 beklenir.
    """
    await _set_permission(db_session, WRITE_ROLE, "sites", AccessLevel.full)
    site, _ = await _tree(db_session, project_factory, "IDOR-29")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    # On kosul: ayni kullanici YAZABILIYOR — reddin sebebi gorunurluk degil, seviye.
    writable = await client.patch(
        f"/sites/{site.id}", json={"name": "Yazma İzni Var"}, headers=_auth(token)
    )
    assert writable.status_code == 200, writable.text

    resp = await sil_aile(client, _auth(token), "site", site.id)

    assert resp.status_code == 403, resp.text
    assert await _exists(db_session, Site, site.id)


async def test_delete_section_with_full_permission_returns_403(
    client, db_session, user_factory, project_factory
):
    """30 — bolum silme de `admin` ister; bolum AYRI izin modulu degildir."""
    await _set_permission(db_session, WRITE_ROLE, "sites", AccessLevel.full)
    _, section = await _tree(db_session, project_factory, "IDOR-30")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    writable = await client.patch(
        f"/sections/{section.id}", json={"name": "Yazma İzni Var"}, headers=_auth(token)
    )
    assert writable.status_code == 200, writable.text

    resp = await sil_aile(client, _auth(token), "section", section.id)

    assert resp.status_code == 403, resp.text
    assert await _exists(db_session, Section, section.id)


# --- 31/32: view ve izinsiz kullanici SILEMEZ ---


async def test_delete_both_with_view_permission_returns_403(
    client, db_session, user_factory, project_factory
):
    await _set_permission(db_session, VIEW_ROLE, "sites", AccessLevel.view)
    site, section = await _tree(db_session, project_factory, "IDOR-31")
    token = await _login(client, db_session, user_factory, VIEW_ROLE, grant_all=True)

    site_delete = await sil_aile(client, _auth(token), "site", site.id)
    section_delete = await sil_aile(client, _auth(token), "section", section.id)

    assert site_delete.status_code == section_delete.status_code == 403
    assert await _exists(db_session, Site, site.id)
    assert await _exists(db_session, Section, section.id)


async def test_delete_both_with_no_permission_returns_403(
    client, db_session, user_factory, project_factory
):
    await _set_permission(db_session, NONE_ROLE, "sites", AccessLevel.none)
    site, section = await _tree(db_session, project_factory, "IDOR-32")
    token = await _login(client, db_session, user_factory, NONE_ROLE, grant_all=True)

    site_delete = await sil_aile(client, _auth(token), "site", site.id)
    section_delete = await sil_aile(client, _auth(token), "section", section.id)

    assert site_delete.status_code == section_delete.status_code == 403
    assert await _exists(db_session, Site, site.id)
    assert await _exists(db_session, Section, section.id)


# --- 33: yetki gorunurlugun ONUNE GECMEZ ---


async def test_admin_without_project_access_delete_returns_404(
    client, db_session, user_factory, project_factory
):
    """33 — `sites:admin` VAR, proje erisimi YOK, Sistem Yoneticisi DEGIL -> 403 (SIL-B1).

    Eskiden 404'tu (once gorunurluk). Artik kapi (Sistem Yoneticisi) handler'dan ONCE kosar ve
    gorunmeyen / var olmayan kayit AYNI 403'u alir: varlik yine sizmaz. Kayit YERINDE kalir.
    """
    await _set_permission(db_session, WRITE_ROLE, "sites", AccessLevel.admin)
    site, section = await _tree(db_session, project_factory, "IDOR-33")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=False)

    site_delete = await sil_aile(client, _auth(token), "site", site.id)
    section_delete = await sil_aile(client, _auth(token), "section", section.id)

    assert site_delete.status_code == 403, site_delete.text
    assert section_delete.status_code == 403, section_delete.text
    assert site_delete.json() == section_delete.json() == {"detail": SYSTEM_ADMIN_ONLY}
    assert await _exists(db_session, Site, site.id)
    assert await _exists(db_session, Section, section.id)


# --- Govde sizinti taramasi ---


async def test_error_bodies_do_not_leak_record_existence(
    client, db_session, user_factory, project_factory
):
    """Her negatif yanit govdesi kayit kimligi/adi/sayisi TASIMAZ.

    Tek bir f-string ("Şantiye X bulunamadı", "3 bölüm var") bu dilimin tum
    404 disiplinini bosa cikarirdi; tarama alan adiyla degil ICERIKLE calisir.
    """
    site, section = await _tree(db_session, project_factory, "IDOR-LEAK")
    await _set_permission(db_session, WRITE_ROLE, "sites", AccessLevel.admin)
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=False)

    responses = [
        await client.get(f"/sites/{site.id}", headers=_auth(token)),
        await client.get(f"/sites/{site.id}/sections", headers=_auth(token)),
        await client.get(f"/projects/{site.project_id}/sites", headers=_auth(token)),
        await client.patch(f"/sites/{site.id}", json={"name": "X"}, headers=_auth(token)),
        await client.patch(f"/sections/{section.id}", json={"name": "X"}, headers=_auth(token)),
        await sil_aile(client, _auth(token), "site", site.id),
        await sil_aile(client, _auth(token), "section", section.id),
    ]

    forbidden = (
        str(site.id),
        str(section.id),
        str(site.project_id),
        site.name,
        section.name,
        site.code,
    )
    for sira, response in enumerate(responses):
        # ilk bes uc 404 (gorunurluk), son iki silme ucu 403 (Sistem Yoneticisi kapisi)
        assert response.status_code == (404 if sira < 5 else 403), response.text
        body = response.text
        for needle in forbidden:
            assert needle not in body, (needle, body)
        assert not any(character.isdigit() for character in response.json()["detail"])

"""URL-2 — okunabilir slug: üretim, değişmezlik, UUID+slug çözümleme, IDOR.

KÖK OLAY: kullanıcı canlıda `/projeler/049e058b-42d9-4e46-aafe-4bcf629e80cd`
gördü. Kullanıcı kararı (2026-08-29): AD SLUG'I -> `/projeler/kopru-guclendirme`.

Bu dosyanın sabitlediği **üç bağlayıcı karar**:

1. **Slug OLUŞTURULURKEN üretilir, AD DEĞİŞİNCE DEĞİŞMEZ.** Paylaşılmış bir
   bağlantı proje yeniden adlandırıldı diye ölmez; v1'de yönlendirme/geçmiş
   tablosu GEREKMEZ. Testi: `test_ad_degisince_slug_DEGISMEZ`.
2. **Eski UUID bağlantıları çalışmaya devam eder.** Çözümleyici İKİSİNİ de
   kabul eder; kullanıcının yer imleri bozulmaz.
3. **Tekillik kapsamı iç içedir**: proje global · şantiye proje içi · bölüm
   şantiye içi.

🔴 **IDOR**: slug TAHMİN EDİLEBİLİR, UUID değil. Bu yüzden slug yolunun kendi
BAĞIMSIZ bekçisi vardır (`_visible_project` / `_visible_site` içinde, görünür
kümenin İÇİNDE eşleştirme) ve her uç için ayrı ayrı kanıtlanır — "test var" ile
"ölçütü savunan bekçi var" aynı şey değildir.
"""

import uuid

from sqlalchemy import select

from app.modules.projects.models import Project
from app.modules.sites.models import Section, Site
from app.modules.users.models import ProjectMember

PROJECT_MISSING = "Proje bulunamadı"
SITE_MISSING = "Şantiye bulunamadı"
SECTION_MISSING = "Bölüm bulunamadı"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, user_factory, role_key: str = "system_admin") -> str:
    address = f"{role_key}-{uuid.uuid4().hex[:6]}@t.co"
    await user_factory(email=address, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


async def _create_project(client, token, name: str, **extra) -> dict:
    resp = await client.post(
        "/projects",
        json={"name": name, "project_type": "taahhut", "is_draft": True, **extra},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_site(client, token, project_id, name: str) -> dict:
    resp = await client.post(
        f"/projects/{project_id}/sites",
        json={"name": name, "is_draft": True},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_section(client, token, site_id, name: str) -> dict:
    resp = await client.post(
        f"/sites/{site_id}/sections",
        json={"name": name, "is_draft": True},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# =========================================================================== #
# 1. ÜRETİM
# =========================================================================== #


async def test_proje_olustururken_turkce_ad_sluglanir(client, user_factory):
    token = await _login(client, user_factory)
    body = await _create_project(client, token, "Köprü Güçlendirme")
    assert body["slug"] == "kopru-guclendirme"


async def test_santiye_ve_bolum_de_sluglanir(client, user_factory):
    token = await _login(client, user_factory)
    project = await _create_project(client, token, "Ana Proje")
    site = await _create_site(client, token, project["id"], "Şişli Şantiyesi")
    assert site["slug"] == "sisli-santiyesi"
    section = await _create_section(client, token, site["id"], "İnce İşler")
    assert section["slug"] == "ince-isler"


async def test_sluglanamayan_ad_slugu_NULL_birakir_ama_kayit_ACILIR(client, user_factory):
    """🔴 Adı tamamen noktalama olan kayıt 422 ALMAZ; yalnız slug'sız kalır."""
    token = await _login(client, user_factory)
    body = await _create_project(client, token, "???")
    assert body["slug"] is None
    # Ve UUID yolu çalışmaya devam eder — karar 2 bunu garanti eder.
    resp = await client.get(f"/projects/{body['id']}", headers=_auth(token))
    assert resp.status_code == 200


async def test_cakisan_ad_sayi_eki_alir_SESSIZCE_CAKISMAZ(client, user_factory):
    """`Köprü A` ile `Kopru A` AYNI tabana düşer; ikincisi `-2` alır."""
    token = await _login(client, user_factory)
    first = await _create_project(client, token, "Köprü A")
    second = await _create_project(client, token, "Kopru A")
    third = await _create_project(client, token, "KÖPRÜ A")

    assert first["slug"] == "kopru-a"
    assert second["slug"] == "kopru-a-2"
    assert third["slug"] == "kopru-a-3"
    assert len({first["slug"], second["slug"], third["slug"]}) == 3


# =========================================================================== #
# 2. DEĞİŞMEZLİK (karar 1)
# =========================================================================== #


async def test_ad_degisince_slug_DEGISMEZ(client, user_factory):
    """🔴 BAĞLAYICI KARAR: paylaşılmış bağlantı yeniden adlandırmayla ÖLMEZ."""
    token = await _login(client, user_factory)
    project = await _create_project(client, token, "Köprü Güçlendirme")

    patched = await client.patch(
        f"/projects/{project['id']}",
        json={"name": "Viyadük Güçlendirme"},
        headers=_auth(token),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Viyadük Güçlendirme"
    assert patched.json()["slug"] == "kopru-guclendirme"

    # ESKİ slug HÂLÂ açar — kararın asıl ölçütü budur.
    resp = await client.get("/projects/kopru-guclendirme", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["id"] == project["id"]

    # YENİ ada göre türeyecek slug ise HİÇBİR ŞEY açmaz (v1'de yönlendirme yok).
    assert (
        await client.get("/projects/viyaduk-guclendirme", headers=_auth(token))
    ).status_code == 404


async def test_santiye_ve_bolum_adi_degisince_de_slug_DEGISMEZ(client, user_factory):
    token = await _login(client, user_factory)
    project = await _create_project(client, token, "Ana Proje 2")
    site = await _create_site(client, token, project["id"], "Şişli Şantiyesi")
    section = await _create_section(client, token, site["id"], "İnce İşler")

    site_patch = await client.patch(
        f"/sites/{site['id']}", json={"name": "Beşiktaş Şantiyesi"}, headers=_auth(token)
    )
    assert site_patch.status_code == 200, site_patch.text
    assert site_patch.json()["slug"] == "sisli-santiyesi"

    section_patch = await client.patch(
        f"/sections/{section['id']}", json={"name": "Kaba İşler"}, headers=_auth(token)
    )
    assert section_patch.status_code == 200, section_patch.text
    assert section_patch.json()["slug"] == "ince-isler"


# =========================================================================== #
# 3. ÇÖZÜMLEME — UUID **ve** slug (karar 2)
# =========================================================================== #


async def test_proje_uuid_ve_slug_AYNI_govdeyi_doner(client, user_factory):
    token = await _login(client, user_factory)
    project = await _create_project(client, token, "Köprü Güçlendirme")

    by_uuid = await client.get(f"/projects/{project['id']}", headers=_auth(token))
    by_slug = await client.get("/projects/kopru-guclendirme", headers=_auth(token))

    assert by_uuid.status_code == by_slug.status_code == 200
    assert by_uuid.json() == by_slug.json()


async def test_santiye_slugu_KAPSAMLA_cozulur(client, user_factory):
    token = await _login(client, user_factory)
    project = await _create_project(client, token, "Kapsam Projesi")
    site = await _create_site(client, token, project["id"], "Şişli Şantiyesi")

    scoped = await client.get(
        f"/sites/sisli-santiyesi?project={project['slug']}", headers=_auth(token)
    )
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["id"] == site["id"]

    # Kapsam UUID olarak da verilebilir.
    by_uuid_scope = await client.get(
        f"/sites/sisli-santiyesi?project={project['id']}", headers=_auth(token)
    )
    assert by_uuid_scope.json()["id"] == site["id"]


async def test_bolum_slugu_KAPSAMLA_cozulur(client, user_factory):
    token = await _login(client, user_factory)
    project = await _create_project(client, token, "Bölüm Kapsamı")
    site = await _create_site(client, token, project["id"], "Merkez Şantiye")
    section = await _create_section(client, token, site["id"], "İnce İşler")

    resp = await client.get(
        f"/sections/ince-isler?site={site['slug']}&project={project['slug']}",
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == section["id"]


# =========================================================================== #
# 4. KAPSAM (karar 3) + BELİRSİZLİKTE FAIL-CLOSED
# =========================================================================== #


async def test_iki_projede_AYNI_santiye_slugu_serbesttir(client, user_factory, db_session):
    """🔴 Kapsam PROJE İÇİDİR: ikisi de EKSİZ `merkez` alır, `-2` YOKTUR."""
    token = await _login(client, user_factory)
    first = await _create_project(client, token, "Birinci Proje")
    second = await _create_project(client, token, "İkinci Proje")

    a = await _create_site(client, token, first["id"], "Merkez")
    b = await _create_site(client, token, second["id"], "Merkez")

    assert a["slug"] == b["slug"] == "merkez"


async def test_kapsamsiz_belirsiz_santiye_slugu_404_FAIL_CLOSED(client, user_factory):
    """İki görünür projede aynı slug varsa RASTGELE biri SEÇİLMEZ."""
    token = await _login(client, user_factory)
    first = await _create_project(client, token, "Belirsiz A")
    second = await _create_project(client, token, "Belirsiz B")
    await _create_site(client, token, first["id"], "Merkez")
    await _create_site(client, token, second["id"], "Merkez")

    ambiguous = await client.get("/sites/merkez", headers=_auth(token))
    assert ambiguous.status_code == 404
    assert ambiguous.json() == {"detail": SITE_MISSING}

    # POZİTİF KONTROL: kapsam verilince AYNI slug 200 döner.
    ok = await client.get(f"/sites/merkez?project={second['slug']}", headers=_auth(token))
    assert ok.status_code == 200, ok.text
    assert ok.json()["project"]["id"] == second["id"]


async def test_tek_aday_varsa_kapsamsiz_da_cozulur(client, user_factory):
    """POZİTİF KONTROL: belirsizlik YOKSA kapsam parametresi zorunlu DEĞİLDİR."""
    token = await _login(client, user_factory)
    project = await _create_project(client, token, "Tek Aday")
    site = await _create_site(client, token, project["id"], "Yalnız Şantiye")

    resp = await client.get("/sites/yalniz-santiye", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == site["id"]


async def test_ayni_santiyede_ayni_bolum_adi_sayi_eki_alir(client, user_factory):
    """Kapsam ŞANTİYE İÇİ: aynı şantiyede ikinci `İnce İşler` `-2` olur."""
    token = await _login(client, user_factory)
    project = await _create_project(client, token, "Bölüm Çakışması")
    site = await _create_site(client, token, project["id"], "Tek Şantiye")

    first = await _create_section(client, token, site["id"], "İnce İşler")
    second = await _create_section(client, token, site["id"], "Ince Isler")

    assert first["slug"] == "ince-isler"
    assert second["slug"] == "ince-isler-2"


# =========================================================================== #
# 5. 🔴 IDOR — slug yolu UUID yoluyla AYNI kapıdan geçer
# =========================================================================== #


async def _invisible_tree(db_session, project_factory) -> tuple[Project, Site, Section]:
    """Aktörün GÖRMEDİĞİ, ama slug'ları TAHMİN EDİLEBİLİR bir ağaç."""
    project = await project_factory(f"IDOR-{uuid.uuid4().hex[:6]}", name="Gizli Proje")
    project.slug = "gizli-proje"
    site = Site(
        project_id=project.id,
        code=f"SNT-{uuid.uuid4().hex[:6]}",
        slug="gizli-santiye",
        name="Gizli Şantiye",
    )
    db_session.add(site)
    await db_session.flush()
    section = Section(site_id=site.id, slug="gizli-bolum", name="Gizli Bölüm")
    db_session.add(section)
    await db_session.flush()
    return project, site, section


async def _restricted_token(client, db_session, user_factory) -> str:
    """`projects`/`sites` YETKİSİ olan ama HİÇBİR projeye erişimi olmayan aktör.

    Yetki görünürlüğün önüne GEÇMEZ (T15/33 kararının slug'daki aynası).
    """
    address = f"idor-{uuid.uuid4().hex[:6]}@t.co"
    await user_factory(email=address, password="parola1234", role_key="patron")
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


async def test_gorunmeyen_projenin_SLUGU_404_ve_govde_AYIRT_EDICI_DEGIL(
    client, db_session, user_factory, project_factory
):
    project, _, _ = await _invisible_tree(db_session, project_factory)
    token = await _restricted_token(client, db_session, user_factory)

    invisible = await client.get("/projects/gizli-proje", headers=_auth(token))
    unknown = await client.get("/projects/hic-olmayan-slug", headers=_auth(token))
    unknown_uuid = await client.get(f"/projects/{uuid.uuid4()}", headers=_auth(token))

    assert invisible.status_code == unknown.status_code == unknown_uuid.status_code == 404
    assert invisible.json() == unknown.json() == unknown_uuid.json() == {"detail": PROJECT_MISSING}
    # Kayıt GERÇEKTEN duruyor — 404 varlığın yokluğundan değil görünmezlikten.
    assert (await db_session.execute(select(Project).where(Project.id == project.id))).scalar_one()


async def test_gorunmeyen_santiyenin_SLUGU_404(client, db_session, user_factory, project_factory):
    _, site, _ = await _invisible_tree(db_session, project_factory)
    token = await _restricted_token(client, db_session, user_factory)

    invisible = await client.get("/sites/gizli-santiye", headers=_auth(token))
    scoped = await client.get("/sites/gizli-santiye?project=gizli-proje", headers=_auth(token))
    unknown = await client.get("/sites/yok-boyle-bir-slug", headers=_auth(token))

    assert invisible.status_code == scoped.status_code == unknown.status_code == 404
    assert invisible.json() == scoped.json() == unknown.json() == {"detail": SITE_MISSING}
    assert site.id is not None


async def test_gorunmeyen_bolumun_SLUGU_404(client, db_session, user_factory, project_factory):
    await _invisible_tree(db_session, project_factory)
    token = await _restricted_token(client, db_session, user_factory)

    invisible = await client.get("/sections/gizli-bolum", headers=_auth(token))
    scoped = await client.get(
        "/sections/gizli-bolum?site=gizli-santiye&project=gizli-proje", headers=_auth(token)
    )
    unknown = await client.get("/sections/yok-boyle-bir-slug", headers=_auth(token))

    assert invisible.status_code == scoped.status_code == unknown.status_code == 404
    assert invisible.json() == scoped.json() == unknown.json() == {"detail": SECTION_MISSING}


async def test_erisim_verilince_AYNI_slug_ACILIR_POZITIF_KONTROL(
    client, db_session, user_factory, project_factory
):
    """🔴 POZİTİF KONTROL: 404'ler slug'ın çalışmamasından DEĞİL, görünürlükten.

    Bu iddia olmasaydı bekçi "her slug 404" diye kırılabilir ve yukarıdaki üç
    test yine yeşil kalırdı (eşdeğer mutant).
    """
    project, site, section = await _invisible_tree(db_session, project_factory)
    address = f"grant-{uuid.uuid4().hex[:6]}@t.co"
    user = await user_factory(email=address, password="parola1234", role_key="patron")
    db_session.add(ProjectMember(user_id=user.id, project_id=project.id, role_id=user.role_id))
    await db_session.flush()
    login = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    token = login.json()["access_token"]

    assert (await client.get("/projects/gizli-proje", headers=_auth(token))).status_code == 200
    assert (await client.get("/sites/gizli-santiye", headers=_auth(token))).status_code == 200
    assert (await client.get("/sections/gizli-bolum", headers=_auth(token))).status_code == 200
    assert site.id is not None and section.id is not None


async def test_slug_NULL_olan_kayit_bos_slugla_ACILMAZ(
    client, db_session, user_factory, project_factory
):
    """`project.slug is None` iken `slug == ref` NULL tarafta YANLIŞTIR.

    Aksi hâlde slug'sız her kayıt boş/`None` bir referansla eşleşebilirdi.
    """
    project = await project_factory(f"NOSLUG-{uuid.uuid4().hex[:6]}", name="Slugsuz")
    assert project.slug is None
    address = f"noslug-{uuid.uuid4().hex[:6]}@t.co"
    user = await user_factory(email=address, password="parola1234", role_key="patron")
    db_session.add(ProjectMember(user_id=user.id, project_id=project.id, role_id=user.role_id))
    await db_session.flush()
    login = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    token = login.json()["access_token"]

    # UUID yolu ÇALIŞIR (pozitif kontrol) …
    assert (await client.get(f"/projects/{project.id}", headers=_auth(token))).status_code == 200
    # … ama slug uzayından hiçbir şey bu kaydı açmaz.
    assert (await client.get("/projects/slugsuz", headers=_auth(token))).status_code == 404


# =========================================================================== #
# 6. YAZMA UÇLARI SLUG KABUL ETMEZ (karar 2 "OKUMA uçları" der)
# =========================================================================== #


async def test_PATCH_slug_kabul_ETMEZ_422(client, user_factory):
    """Yazma yüzeyi tahmin edilebilir bir anahtara AÇILMAZ — 422 (`uuid_parsing`)."""
    token = await _login(client, user_factory)
    await _create_project(client, token, "Yazma Testi")

    resp = await client.patch("/projects/yazma-testi", json={"name": "Yeni"}, headers=_auth(token))
    assert resp.status_code == 422, resp.text


async def test_slug_govdeden_YAZILAMAZ(client, user_factory, db_session):
    """İstemci `slug` göndererek başka bir kaydın URL'ini çalamaz."""
    token = await _login(client, user_factory)
    project = await _create_project(client, token, "Gövde Testi")

    resp = await client.patch(
        f"/projects/{project['id']}", json={"slug": "calinmis-slug"}, headers=_auth(token)
    )
    assert resp.status_code in (200, 422)
    row = (
        await db_session.execute(select(Project).where(Project.id == uuid.UUID(project["id"])))
    ).scalar_one()
    assert row.slug == "govde-testi"

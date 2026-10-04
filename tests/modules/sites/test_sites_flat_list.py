"""SITE-1a — düz `GET /sites` ucu (BOR-TEMIZ T4).

Ölçülen boşluk: şantiye listeleyen tek yol `/projects/{project_id}/sites` idi;
proje seçmeden şantiye listeleyen bir uç YOKTU. Mockup'ların "şantiye seç"
dropdown'ları bunu istiyor.

Testler UÇTAN (gerçek HTTP) yazılır — kanon: "ŞEMA KATMANI BEKÇİLERİ TEST
SUITE'İNE GÖRÜNMEZ"; şemayı doğrudan çağıran bir test, router'ın o şemayı
gerçekten kullanıp kullanmadığını ölçmez.
"""

import uuid

from sqlalchemy import select

from app.core.access import AccessLevel, Scope
from app.modules.roles.models import Module, Role, RolePermission
from app.modules.sites.models import Site
from app.modules.users.models import UserProjectAccess
from tests._legacy_permission_yardimcisi import sync_page_cells

# Düz ucun yanıt alanları — K3 gereği YALIN küme. `SiteCard` alanları (status,
# budget, facilities, ...) BİLEREK yoktur: `SiteCard`a `project_id` eklemek
# `/projects/{id}/sites` tüketen tüm frontend fikstürlerini kırardı.
EXPECTED_FIELDS = {"id", "code", "name", "project_id", "project_name"}


async def _login(client, user_factory, role_key: str, email: str | None = None) -> str:
    address = email or f"{role_key}@t.co"
    await user_factory(email=address, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


async def _login_scoped(client, session, user_factory, role_key: str, email: str, projects):
    """`user_project_access` satırlarıyla SINIRLI görünürlük (all_projects YOK)."""
    user = await user_factory(email=email, password="parola1234", role_key=role_key)
    for project in projects:
        session.add(UserProjectAccess(user_id=user.id, project_id=project.id))
    await session.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _site(session, project, code: str, name: str | None = None) -> Site:
    site = Site(project_id=project.id, code=code, name=name or f"{code} Şantiyesi")
    session.add(site)
    await session.flush()
    return site


async def _set_permission(
    session, role_key: str, module_key: str, level: AccessLevel, scope: Scope = Scope.all
) -> None:
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


# --- Kimlik/izin kapıları ---


async def test_kimliksiz_401(client):
    resp = await client.get("/sites")
    assert resp.status_code == 401


async def test_izinsiz_rol_403(client, db_session, user_factory, project_factory):
    project = await project_factory(f"P-{uuid.uuid4().hex[:6]}")
    await _site(db_session, project, "A-BLOK")
    # Seed matrisinde HİÇBİR rol sites=none taşımıyor; test kapıyı seed
    # değerinden BAĞIMSIZ doğrulamak için izni açıkça none'a çeker.
    await _set_permission(db_session, "procurement", "sites", AccessLevel.none)
    token = await _login(client, user_factory, "procurement")

    resp = await client.get("/sites", headers=_auth(token))

    assert resp.status_code == 403


# --- Yalın şema + varsayılan yol ---


async def test_parametresiz_cagri_varsayilanlari_doner(
    client, db_session, user_factory, project_factory
):
    """🔴 MU-2 kanonu: her test limit/offset'i açıkça geçerse VARSAYILAN YOL
    bekçisizdir. Bu test ucu PARAMETRESİZ çağırır ve 50/0'ı ölçer."""
    project = await project_factory(f"P-{uuid.uuid4().hex[:6]}", name="Kuzey Konut")
    await _site(db_session, project, "A-BLOK")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get("/sites", headers=_auth(token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["total"] == 1


async def test_yanit_alanlari_yalin_kume_ve_proje_kimligi_tasir(
    client, db_session, user_factory, project_factory
):
    project = await project_factory(f"P-{uuid.uuid4().hex[:6]}", name="Kuzey Konut")
    site = await _site(db_session, project, "A-BLOK", name="A-Blok Şantiyesi")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get("/sites", headers=_auth(token))

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert set(item) == EXPECTED_FIELDS
    assert item == {
        "id": str(site.id),
        "code": "A-BLOK",
        "name": "A-Blok Şantiyesi",
        "project_id": str(project.id),
        "project_name": "Kuzey Konut",
    }


async def test_siralama_code_artan_deterministik(client, db_session, user_factory, project_factory):
    """Sıralama `code` artan (ikincil `id`) — kararsız sıra sayfalama
    testlerini flaky yapar, bu yüzden burada KİLİTLENİR."""
    first = await project_factory(f"P1-{uuid.uuid4().hex[:6]}", name="Kuzey")
    second = await project_factory(f"P2-{uuid.uuid4().hex[:6]}", name="Güney")
    await _site(db_session, second, "C-BLOK")
    await _site(db_session, first, "A-BLOK")
    await _site(db_session, second, "B-BLOK")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get("/sites", headers=_auth(token))

    assert [s["code"] for s in resp.json()["items"]] == ["A-BLOK", "B-BLOK", "C-BLOK"]


# --- Sayfalama sınır değerleri ---


async def test_limit_bir_ilk_sayfa(client, db_session, user_factory, project_factory):
    project = await project_factory(f"P-{uuid.uuid4().hex[:6]}")
    await _site(db_session, project, "A-BLOK")
    await _site(db_session, project, "B-BLOK")
    await _site(db_session, project, "C-BLOK")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get("/sites?limit=1", headers=_auth(token))

    body = resp.json()
    assert [s["code"] for s in body["items"]] == ["A-BLOK"]
    assert (body["total"], body["limit"], body["offset"]) == (3, 1, 0)


async def test_son_sayfa_tek_eleman(client, db_session, user_factory, project_factory):
    project = await project_factory(f"P-{uuid.uuid4().hex[:6]}")
    await _site(db_session, project, "A-BLOK")
    await _site(db_session, project, "B-BLOK")
    await _site(db_session, project, "C-BLOK")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get("/sites?limit=10&offset=2", headers=_auth(token))

    body = resp.json()
    assert [s["code"] for s in body["items"]] == ["C-BLOK"]
    assert body["total"] == 3


async def test_offset_total_bos_sayfa_ama_total_dogru(
    client, db_session, user_factory, project_factory
):
    project = await project_factory(f"P-{uuid.uuid4().hex[:6]}")
    await _site(db_session, project, "A-BLOK")
    await _site(db_session, project, "B-BLOK")
    await _site(db_session, project, "C-BLOK")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get("/sites?offset=3", headers=_auth(token))

    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 3
    assert body["offset"] == 3


async def test_tavan_asimi_sessizce_kirpilmaz_422(client, user_factory):
    """K7 standardı: `le=200`, aşım KIRPILMAZ, 422 döner."""
    token = await _login(client, user_factory, "system_admin")
    resp = await client.get("/sites?limit=201", headers=_auth(token))
    assert resp.status_code == 422


async def test_limit_sifir_422(client, user_factory):
    token = await _login(client, user_factory, "system_admin")
    resp = await client.get("/sites?limit=0", headers=_auth(token))
    assert resp.status_code == 422


async def test_negatif_offset_422(client, user_factory):
    token = await _login(client, user_factory, "system_admin")
    resp = await client.get("/sites?offset=-1", headers=_auth(token))
    assert resp.status_code == 422


# --- Görünürlük süzgeci: `/projects/{id}/sites` ile AYNI kaynak ---


async def test_gorunmeyen_proje_santiyeleri_listede_yok_ve_total_saymaz(
    client, db_session, user_factory, project_factory
):
    """🔴 Klasik kusur: `total` süzgeçsiz SAYAR. Burada açıkça ölçülür."""
    gorunen = await project_factory(f"V-{uuid.uuid4().hex[:6]}", name="Görünen")
    gizli = await project_factory(f"H-{uuid.uuid4().hex[:6]}", name="Gizli")
    await _site(db_session, gorunen, "A-BLOK")
    await _site(db_session, gizli, "B-BLOK")
    await _site(db_session, gizli, "C-BLOK")
    token = await _login_scoped(
        client, db_session, user_factory, "patron", "kisitli@t.co", [gorunen]
    )

    resp = await client.get("/sites", headers=_auth(token))

    body = resp.json()
    assert [s["code"] for s in body["items"]] == ["A-BLOK"]
    # Süzgeç LIMIT/OFFSET'ten ÖNCE uygulanmalı: `total` gizli iki şantiyeyi
    # saymaz. 3 gelirse süzgeç yalnız sayfaya uygulanmış demektir.
    assert body["total"] == 1


async def test_erisimi_olmayan_kullanici_bos_liste_gorur(
    client, db_session, user_factory, project_factory
):
    project = await project_factory(f"P-{uuid.uuid4().hex[:6]}")
    await _site(db_session, project, "A-BLOK")
    token = await _login_scoped(client, db_session, user_factory, "patron", "erisimsiz@t.co", [])

    resp = await client.get("/sites", headers=_auth(token))

    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


async def test_suzgec_projects_id_sites_ucuyla_ayni(
    client, db_session, user_factory, project_factory
):
    """🔴 Süzgecin AYNI kaynaktan (`projects.service.visible_projects`) geldiğinin
    uçtan kanıtı: düz uç, kullanıcının görebildiği projelerin
    `/projects/{id}/sites` çıktılarının BİRLEŞİMİNİ verir; görünmeyen projenin
    kendi ucu ise 404'tür (403 DEĞİL — varlık sızdırılmaz)."""
    gorunen = await project_factory(f"V-{uuid.uuid4().hex[:6]}", name="Görünen")
    gizli = await project_factory(f"H-{uuid.uuid4().hex[:6]}", name="Gizli")
    await _site(db_session, gorunen, "A-BLOK")
    await _site(db_session, gizli, "B-BLOK")
    token = await _login_scoped(
        client, db_session, user_factory, "patron", "esitlik@t.co", [gorunen]
    )

    duz = await client.get("/sites", headers=_auth(token))
    kapsamli = await client.get(f"/projects/{gorunen.id}/sites", headers=_auth(token))
    gizli_resp = await client.get(f"/projects/{gizli.id}/sites", headers=_auth(token))

    assert gizli_resp.status_code == 404
    assert {s["id"] for s in duz.json()["items"]} == {s["id"] for s in kapsamli.json()["items"]}


async def test_ayni_KODLU_santiyelerde_sayfa_sinirlari_KAYMAZ(
    client, db_session, user_factory, project_factory
):
    """T6 MUTASYON TURUNDA açılan boşluk: `order_by(Site.code, Site.id)`deki
    EŞİTLİK BOZUCU `Site.id` kaldırıldığında hiçbir test kırılmıyordu.

    🔴 Neden kördü: mevcut sıralama testi (`test_siralama_code_artan_...`) ÜÇ
    FARKLI kod kullanır — eşitlik hiç OLUŞMAZ, dolayısıyla eşitlik bozucuyu hiç
    sınamaz. Oysa `uq_sites_project_code` kodu YALNIZ PROJE İÇİNDE tekil kılar;
    iki ayrı projede AYNI kod meşrudur. İkincil anahtar olmadan ORDER BY
    kararsızdır: `LIMIT/OFFSET` ile art arda çekilen sayfalar aynı satırı iki
    kez verebilir ya da bir satırı hiç vermeyebilir.

    Test bu yüzden İKİ kod × İKİ proje kurar ve sayfaları tek tek gezip
    "her satır TAM BİR KEZ" iddiasını ölçer.
    """
    kuzey = await project_factory(f"P1-{uuid.uuid4().hex[:6]}", name="Kuzey Konut")
    guney = await project_factory(f"P2-{uuid.uuid4().hex[:6]}", name="Güney Konut")
    beklenen = {
        str((await _site(db_session, proje, kod)).id)
        for proje in (kuzey, guney)
        for kod in ("A-BLOK", "B-BLOK")
    }
    token = await _login(client, user_factory, "system_admin")

    toplanan: list[str] = []
    for offset in range(4):
        resp = await client.get(f"/sites?limit=1&offset={offset}", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 4
        toplanan.extend(satir["id"] for satir in resp.json()["items"])

    assert len(toplanan) == 4, toplanan
    assert sorted(toplanan) == sorted(beklenen), (
        "sayfa sınırı kaydı — aynı `code` taşıyan satırlar için sıralama "
        "kararsız; `order_by`da eşitlik bozucu ikincil anahtar yok."
    )


async def test_siralama_ESITLIK_BOZUCU_ikincil_anahtar_TASIR():
    """Yukarıdaki uçtan-uca ikizin DETERMİNİSTİK tamamlayıcısı.

    🔴 Niçin uçtan-uca test YETMİYOR (T6'da ölçüldü): eşitlik bozucu
    kaldırıldığında PostgreSQL küçük kümeleri yine de aynı sırada döndürebilir —
    davranış testi ŞANSA bağlı YEŞİL kalır ve bekçilik ETMEZ. Kararsızlık
    plan/veri hacmi değiştiğinde CANLIDA ortaya çıkar.

    Bu yüzden karar SORGU BİÇİMİ düzeyinde kilitlenir: `ORDER BY` hem `code`
    hem `id` taşımalıdır. Modül docstring'indeki "şema katmanı testi uçtan-uca
    testin YERİNE GEÇMEZ" kanonuyla çelişmez — buradaki test onun YERİNE değil,
    YANINA konur.
    """

    class _YakalayanSession:
        """`session.execute`e verilen SELECT'i yakalar, DB'ye hiç gitmez."""

        def __init__(self) -> None:
            self.stmt = None

        async def execute(self, stmt):
            self.stmt = stmt
            return _BosSonuc()

    class _BosSonuc:
        def all(self):
            return []

    from app.modules.sites import repository

    session = _YakalayanSession()
    await repository.list_site_options(session, [uuid.uuid4()], 50, 0)

    sql = str(session.stmt)
    order_by = sql.split("ORDER BY", 1)[1]
    assert "sites.code" in order_by, sql
    assert "sites.id" in order_by, (
        "ORDER BY eşitlik bozucu ikincil anahtar taşımıyor: `code` yalnız proje "
        "içinde tekildir, iki projede aynı kod meşrudur ve sıralama kararsız kalır."
    )


async def test_projects_admin_rolu_tum_santiyeleri_gorur(
    client, db_session, user_factory, project_factory
):
    """`visible_projects`in admin istisnası düz uçta da geçerli: `system_admin`
    `user_project_access` satırı OLMADAN tüm şantiyeleri görür."""
    first = await project_factory(f"P1-{uuid.uuid4().hex[:6]}")
    second = await project_factory(f"P2-{uuid.uuid4().hex[:6]}")
    await _site(db_session, first, "A-BLOK")
    await _site(db_session, second, "B-BLOK")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get("/sites", headers=_auth(token))

    assert resp.json()["total"] == 2

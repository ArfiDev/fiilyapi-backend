"""SITE-1b — `GET /projects` sayfalaması (BOR-TEMIZ T5).

Ölçülen boşluk: uç ne `limit`/`offset` alıyordu ne `total` dönüyordu; liste
görünür kümenin TAMAMINI basıyor, pahalı toplu türev sorguları (puantaj
sayıları + maliyet kartları) da tüm küme için koşuyordu.

KARAR K4: `counts` ve `items` AYNEN korunur (geriye dönük kırıcı değişiklik
yasak); zarfa `total` · `limit` · `offset` EKLENİR.

🔴 İKİ SAYAÇ FARKLI ŞEYLERİ SAYAR:
  * `counts` — SÜZGEÇTEN ETKİLENMEZ, hep tüm görünür kümeyi sayar (spec §5.1;
    mockup sekmeleri "Tümü / Taahhüt / ..." rakamlarını buradan basar).
  * `total` — SÜZGEÇLENMİŞ kümenin boyutudur (sayfalamadan ÖNCE); sayfa
    çubuğunun sayfa sayısı buradan çıkar.
Bu iki sayacın kurulumları BİLEREK farklı sonuç verecek şekilde seçilmiştir —
eşit olsalardı test hiçbir şey kanıtlamazdı.

Testler UÇTAN (gerçek HTTP) yazılır — kanon: "ŞEMA KATMANI BEKÇİLERİ TEST
SUITE'İNE GÖRÜNMEZ".
"""

import pytest

from app.modules.projects import service as projects_service


async def _login(client, user_factory, role_key: str = "system_admin") -> str:
    address = f"{role_key}@t5.co"
    await user_factory(email=address, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _mixed_portfolio(project_factory) -> None:
    """3 taahhüt + 2 kendi_yatirim = 5 görünür proje.

    `?type=taahhut` süzgeci `total=3` verirken `counts.all` 5 kalmalı — iki
    sayacın FARKLI şeyleri saydığını kanıtlayan kurulum budur.
    Kodlar küresel tekil (`projects.code` UNIQUE) ve sıralama `code` artan
    olduğu için beklenen sıra sabittir: T5-A .. T5-E.
    """
    await project_factory("T5-A", project_type="taahhut")
    await project_factory("T5-B", project_type="taahhut")
    await project_factory("T5-C", project_type="taahhut")
    await project_factory("T5-D", project_type="kendi_yatirim")
    await project_factory("T5-E", project_type="kendi_yatirim")


# --- Varsayılan yol (MU-2 kanonu: parametresiz çağrı bekçisiz kalmaz) ---


async def test_parametresiz_cagri_varsayilanlari_ve_tum_kumeyi_doner(
    client, user_factory, project_factory
):
    """🔴 MU-2 kanonu: her test bayrağı açıkça geçerse VARSAYILAN YOL bekçisizdir.
    Bu uç `limit`/`offset` HİÇ geçmeden çağrılır."""
    await _mixed_portfolio(project_factory)
    token = await _login(client, user_factory)

    resp = await client.get("/projects", headers=_auth(token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["total"] == 5
    assert [p["code"] for p in body["items"]] == ["T5-A", "T5-B", "T5-C", "T5-D", "T5-E"]


async def test_counts_ve_items_alanlari_korunur(client, user_factory, project_factory):
    """K4: geriye dönük uyum — mevcut iki alan kaldırılmadı/yeniden adlandırılmadı."""
    await _mixed_portfolio(project_factory)
    token = await _login(client, user_factory)

    body = (await client.get("/projects", headers=_auth(token))).json()

    assert set(body) == {"counts", "items", "total", "limit", "offset"}
    assert body["counts"]["all"] == 5
    assert body["counts"]["taahhut"] == 3
    assert body["counts"]["kendi_yatirim"] == 2


# --- (a) counts sayfalamadan ETKİLENMEZ ---


async def test_counts_sayfa_boyutundan_etkilenmez(client, user_factory, project_factory):
    """(a) `limit` eleman sayısından küçükken `counts` DEĞİŞMEDEN tüm görünür
    kümeyi sayar — sekme rakamları sayfa çubuğuyla oynamaz."""
    await _mixed_portfolio(project_factory)
    token = await _login(client, user_factory)

    body = (await client.get("/projects?limit=2", headers=_auth(token))).json()

    assert len(body["items"]) == 2
    assert body["counts"]["all"] == 5
    assert body["counts"]["taahhut"] == 3
    assert body["counts"]["kendi_yatirim"] == 2


async def test_counts_suzgecten_etkilenmez(client, user_factory, project_factory):
    """`counts` süzgeçten de etkilenmez (spec §5.1 davranışı korunuyor)."""
    await _mixed_portfolio(project_factory)
    token = await _login(client, user_factory)

    body = (await client.get("/projects?type=kendi_yatirim", headers=_auth(token))).json()

    assert body["counts"]["all"] == 5
    assert body["counts"]["taahhut"] == 3


# --- (b) total SÜZGEÇLENMİŞ kümeyi sayar ---


async def test_total_suzgeclenmis_kumeyi_sayar(client, user_factory, project_factory):
    """(b) `?type=taahhut` → `total=3`, ama `counts.all=5`. İkisi FARKLI."""
    await _mixed_portfolio(project_factory)
    token = await _login(client, user_factory)

    body = (await client.get("/projects?type=taahhut", headers=_auth(token))).json()

    assert body["total"] == 3
    assert body["counts"]["all"] == 5
    assert body["total"] != body["counts"]["all"]
    assert [p["code"] for p in body["items"]] == ["T5-A", "T5-B", "T5-C"]


async def test_total_status_suzgecinden_de_gecer(client, user_factory, project_factory):
    await project_factory("T5-S1", status="active")
    await project_factory("T5-S2", status="completed")
    await project_factory("T5-S3", status="completed")
    token = await _login(client, user_factory)

    body = (await client.get("/projects?status=completed", headers=_auth(token))).json()

    assert body["total"] == 2
    assert body["counts"]["all"] == 3


# --- (c) total sayfa boyutuna DEĞİL, süzgeçlenmiş kümeye eşittir ---


async def test_total_sayfa_boyutuna_esit_degildir(client, user_factory, project_factory):
    """(c) Sayfalama uygulanınca `total` sayfa boyutuna değil süzgeçlenmiş
    kümenin TAMAMINA eşit kalır — yoksa sayfa çubuğu tek sayfa gösterir."""
    await _mixed_portfolio(project_factory)
    token = await _login(client, user_factory)

    body = (await client.get("/projects?type=taahhut&limit=1", headers=_auth(token))).json()

    assert len(body["items"]) == 1
    assert body["total"] == 3
    assert body["limit"] == 1
    assert body["offset"] == 0


# --- Sınır değerleri ---


async def test_limit_1_ilk_elemani_verir(client, user_factory, project_factory):
    await _mixed_portfolio(project_factory)
    token = await _login(client, user_factory)

    body = (await client.get("/projects?limit=1", headers=_auth(token))).json()

    assert [p["code"] for p in body["items"]] == ["T5-A"]
    assert body["total"] == 5


async def test_son_sayfa_tek_eleman(client, user_factory, project_factory):
    """`offset=total-1` → son eleman tek başına."""
    await _mixed_portfolio(project_factory)
    token = await _login(client, user_factory)

    body = (await client.get("/projects?limit=2&offset=4", headers=_auth(token))).json()

    assert [p["code"] for p in body["items"]] == ["T5-E"]
    assert body["total"] == 5
    assert body["offset"] == 4


async def test_offset_total_bos_items_ama_total_dogru(client, user_factory, project_factory):
    """`offset=total` → `items` boş, ama `total` ve `counts` DOĞRU kalır."""
    await _mixed_portfolio(project_factory)
    token = await _login(client, user_factory)

    body = (await client.get("/projects?offset=5", headers=_auth(token))).json()

    assert body["items"] == []
    assert body["total"] == 5
    assert body["counts"]["all"] == 5


async def test_sayfalar_ortusmez_ve_tum_kumeyi_kapsar(client, user_factory, project_factory):
    """Deterministik sıra (code artan, küresel tekil) → sayfalar ayrık."""
    await _mixed_portfolio(project_factory)
    token = await _login(client, user_factory)

    ilk = (await client.get("/projects?limit=2&offset=0", headers=_auth(token))).json()
    ikinci = (await client.get("/projects?limit=2&offset=2", headers=_auth(token))).json()
    ucuncu = (await client.get("/projects?limit=2&offset=4", headers=_auth(token))).json()

    kodlar = [p["code"] for sayfa in (ilk, ikinci, ucuncu) for p in sayfa["items"]]
    assert kodlar == ["T5-A", "T5-B", "T5-C", "T5-D", "T5-E"]


@pytest.mark.parametrize(
    "sorgu",
    ["limit=201", "limit=0", "limit=-1", "offset=-1"],
    ids=["tavan_asimi", "sifir_limit", "negatif_limit", "negatif_offset"],
)
async def test_gecersiz_sayfalama_422(client, user_factory, project_factory, sorgu):
    """K7 standardı: tavan aşımı SESSİZCE KIRPILMAZ → 422."""
    await _mixed_portfolio(project_factory)
    token = await _login(client, user_factory)

    resp = await client.get(f"/projects?{sorgu}", headers=_auth(token))

    assert resp.status_code == 422


async def test_limit_200_kabul_edilir(client, user_factory, project_factory):
    """Tavanın KENDİSİ geçerlidir (`le=200`) — sınır günü `<` mü `<=` mi."""
    await _mixed_portfolio(project_factory)
    token = await _login(client, user_factory)

    resp = await client.get("/projects?limit=200", headers=_auth(token))

    assert resp.status_code == 200
    assert resp.json()["limit"] == 200


# --- Performans: pahalı toplu sorgular YALNIZ SAYFA için koşar ---


async def test_pahali_toplu_sorgular_yalniz_sayfadaki_projeler_icin_kosar(
    client, user_factory, project_factory, monkeypatch
):
    """🔴 Sayfalama maliyeti düşürmezse anlamsızdır.

    `timesheet_counts.by_project` ve `cost_cards.by_projects` sayfalamadan ÖNCE
    tüm süzgeçlenmiş küme için koşuyordu. Bu test ikisine giden kimlik
    listesinin UZUNLUĞUNU ölçer: sayfa boyutu kadar olmalı, 5 değil.
    """
    await _mixed_portfolio(project_factory)
    token = await _login(client, user_factory)

    puantaj_cagrilari: list[int] = []
    maliyet_cagrilari: list[int] = []
    gercek_puantaj = projects_service.timesheet_counts.by_project
    gercek_maliyet = projects_service.cost_cards.by_projects

    async def _puantaj_casusu(session, project_ids):
        puantaj_cagrilari.append(len(project_ids))
        return await gercek_puantaj(session, project_ids)

    async def _maliyet_casusu(session, projects):
        maliyet_cagrilari.append(len(projects))
        return await gercek_maliyet(session, projects)

    monkeypatch.setattr(
        projects_service.timesheet_counts, "by_project", _puantaj_casusu, raising=True
    )
    monkeypatch.setattr(projects_service.cost_cards, "by_projects", _maliyet_casusu, raising=True)

    resp = await client.get("/projects?limit=2", headers=_auth(token))

    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 2
    assert puantaj_cagrilari == [2], puantaj_cagrilari
    assert maliyet_cagrilari == [2], maliyet_cagrilari


async def test_bos_sayfada_toplu_sorgular_bos_liste_alir(
    client, user_factory, project_factory, monkeypatch
):
    """`offset=total` → sayfa boş; pahalı sorgular BOŞ kimlik listesiyle koşar."""
    await _mixed_portfolio(project_factory)
    token = await _login(client, user_factory)

    puantaj_cagrilari: list[int] = []
    gercek_puantaj = projects_service.timesheet_counts.by_project

    async def _puantaj_casusu(session, project_ids):
        puantaj_cagrilari.append(len(project_ids))
        return await gercek_puantaj(session, project_ids)

    monkeypatch.setattr(
        projects_service.timesheet_counts, "by_project", _puantaj_casusu, raising=True
    )

    resp = await client.get("/projects?offset=5", headers=_auth(token))

    assert resp.status_code == 200
    assert puantaj_cagrilari == [0], puantaj_cagrilari


# --- Görünürlük: total göremediği projeyi saymaz ---


async def test_total_gorunur_olmayan_projeyi_saymaz(
    client, db_session, user_factory, project_factory
):
    from app.modules.users.models import ProjectMember

    izinli = await project_factory("T5-V1")
    await project_factory("T5-V2")
    user = await user_factory(email="kisitli@t5.co", password="parola1234", role_key="patron")
    db_session.add(ProjectMember(user_id=user.id, project_id=izinli.id, role_id=user.role_id))
    await db_session.flush()
    login = await client.post(
        "/auth/login", json={"email": "kisitli@t5.co", "password": "parola1234"}
    )
    token = login.json()["access_token"]

    body = (await client.get("/projects", headers=_auth(token))).json()

    assert body["total"] == 1
    assert body["counts"]["all"] == 1
    assert [p["code"] for p in body["items"]] == ["T5-V1"]

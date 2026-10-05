"""T7 — `PATCH /sites/{id}` genislemesi + `is_draft` yayina gecis kurali.

Spec: §5.3 (yayina gecis), §6.1/§6.2 (semalar), §7 (uclar), §11.3/3 (canli veri korkulugu).

Bu dosyanin IKI kilit testi test_patch_does_not_run_full_validation ve
test_patch_publish_with_missing_fields_returns_422_and_stays_draft'tir. Birlikte
tek bir kurali kilitlerler: **PATCH gevsek, YAYIN siki.**

Gevsekligi kaybetmek CANLI VERIYI DUZENLENEMEZ hale getirir: canlida sefsiz /
il bilgisi olmayan santiyeler var (P2 ve P1.1a akislarindan) ve kullanici yalnizca
adi degistirmek isterken "Şantiye şefi seçiniz." duvarina carpar.

Ayrica bu dilimde kapanan SESSIZ VERI KAYBI: `SiteUpdate` `facilities` tasiyordu
ama `update_site` duz `setattr` yapiyordu — istemci `facilities` gonderirse ORM
nesnesine BASIBOS bir Python ozniteligi yazilir, DB'ye HICBIR SEY gitmez ve hata
da olusmazdi. `test_patch_facilities_partial_merge` bunun kilididir.
"""

import uuid
from datetime import date

from sqlalchemy import select

from app.modules.sites.models import Section, Site

WRITE_ROLE = "patron"
VIEW_ONLY_ROLE = "site_chief"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(
    client, session, user_factory, role_key: str = WRITE_ROLE, *, grant_all: bool = True
) -> str:
    address = f"{role_key}-{uuid.uuid4().hex[:6]}@t.co"
    user = await user_factory(email=address, password="parola1234", role_key=role_key)
    if grant_all:
        user.all_projects = True
        await session.flush()
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


async def _site(session, project, **kwargs) -> Site:
    """Canlidaki gibi EKSIK bir santiye satiri (sefsiz, ilsiz, tarihsiz) uretir."""
    site = Site(
        project_id=project.id,
        code=kwargs.pop("code", f"A-{uuid.uuid4().hex[:6]}"),
        name=kwargs.pop("name", "A-Blok Şantiyesi"),
    )
    for field, value in kwargs.items():
        setattr(site, field, value)
    session.add(site)
    await session.flush()
    return site


async def _reload(session, site: Site) -> Site:
    """Yaniti degil DB'yi okur: "taslak kaldi" iddiasinin tek gecerli kaniti.

    `refresh` kullanilir; `expire` + `select` async oturumda `MissingGreenlet`
    atar (suresi dolmus iliskiler senkron tembel yuklemeye kayar).
    """
    await session.refresh(site)
    return site


def _publish_ready(**overrides) -> dict:
    """Yayina gecis icin gereken alanlar (§5.1/7-10)."""
    body = {
        "site_manager_name": "Selim Öz",
        "city": "Ankara",
        "construction_area_m2": "12000.00",
        "start_date": "2026-03-01",
        "end_date": "2027-03-01",
    }
    body.update(overrides)
    return body


# --- Temel PATCH davranisi ---


async def test_patch_single_field_changes_only_that_field(
    client, db_session, user_factory, project_factory
):
    """`exclude_unset`: gonderilmeyen alan DOKUNULMAZ, null'a cekilmez."""
    project = await project_factory("T7-1")
    site = await _site(db_session, project, city="Bursa", parcel="1234/5")
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(f"/sites/{site.id}", json={"name": "Yeni Ad"}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Yeni Ad"
    assert body["city"] == "Bursa"
    assert body["parcel"] == "1234/5"


async def test_patch_all_new_fields_round_trip(client, db_session, user_factory, project_factory):
    project = await project_factory("T7-2")
    site = await _site(db_session, project)
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sites/{site.id}",
        json={
            "neighborhood": "Kuyubaşı Mah.",
            "parcel": "1234/5",
            "gps_coordinates": "39.9208, 32.8541",
            "land_area_m2": "8000.50",
            "construction_area_m2": "12000.00",
            "floor_info": "2 bodrum + 10 normal",
            "budget": "45000000.00",
            "electricity_subscription_no": "EL-99",
            "water_subscription_no": "SU-88",
            "planned_worker_count": 120,
        },
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["neighborhood"] == "Kuyubaşı Mah."
    assert body["gps_coordinates"] == "39.9208, 32.8541"
    assert body["floor_info"] == "2 bodrum + 10 normal"
    assert body["planned_worker_count"] == 120
    row = await _reload(db_session, site)
    assert row.electricity_subscription_no == "EL-99"
    assert row.water_subscription_no == "SU-88"


async def test_patch_facilities_partial_merge(client, db_session, user_factory, project_factory):
    """SESSIZ VERI KAYBI KILIDI: gruplu `facilities` SEKIZ kolona yazilmali.

    Duz `setattr` DB'ye hicbir sey yazmadan 200 doner — kullanici kaydettigini
    sanir, veri yoktur. Gonderilmezse grup DOKUNULMAZ.
    """
    project = await project_factory("T7-3")
    site = await _site(db_session, project, has_dormitory=True)
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sites/{site.id}",
        json={"facilities": {"closed_warehouse": True, "canteen": True}},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    row = await _reload(db_session, site)
    assert row.has_closed_warehouse is True
    assert row.has_canteen is True
    # Grup BUTUN olarak yazilir: gonderilmeyen anahtarlar semanin varsayilanina
    # (False) duser — "kismi anahtar birlestirme" YOKTUR (§4.1).
    assert row.has_dormitory is False

    # Ikinci istek `facilities` GONDERMIYOR -> gruba DOKUNULMAZ.
    again = await client.patch(f"/sites/{site.id}", json={"name": "X"}, headers=_auth(token))
    assert again.status_code == 200, again.text
    row = await _reload(db_session, site)
    assert row.has_closed_warehouse is True
    assert row.has_canteen is True


async def test_patch_does_not_run_full_validation(
    client, db_session, user_factory, project_factory
):
    """§11.3/3 KILIDI: sefsiz + ilsiz + tarihsiz CANLI kayitta yalniz `name` -> 200."""
    project = await project_factory("T7-4")
    site = await _site(
        db_session,
        project,
        site_manager_name=None,
        city=None,
        construction_area_m2=None,
        start_date=None,
        end_date=None,
    )
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(f"/sites/{site.id}", json={"name": "Yeni Ad"}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Yeni Ad"


async def test_patch_still_runs_consistency_rules(
    client, db_session, user_factory, project_factory
):
    """Tutarlilik HER ZAMAN kosar: gevseklik yalniz ZORUNLULUK kurallarindadir."""
    project = await project_factory("T7-5")
    site = await _site(db_session, project, start_date=None, end_date=None)
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sites/{site.id}",
        json={"start_date": "2027-01-01", "end_date": "2026-01-01"},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "Planlanan bitiş tarihi başlangıçtan önce olamaz."


async def test_patch_consistency_rule_sees_existing_row(
    client, db_session, user_factory, project_factory
):
    """Tutarlilik BIRLESIK kayit uzerinde kosar: `start_date` mevcut satirdan gelir."""
    project = await project_factory("T7-5b")
    site = await _site(db_session, project, start_date=date(2027, 1, 1))
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sites/{site.id}", json={"end_date": "2026-01-01"}, headers=_auth(token)
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "Planlanan bitiş tarihi başlangıçtan önce olamaz."


async def test_patch_safety_officer_mutual_exclusion_enforced(
    client, db_session, user_factory, project_factory
):
    """ISG YA sistem kullanicisi YA OSGB olabilir — DB `CHECK` oncesi Turkce 422."""
    project = await project_factory("T7-6")
    site = await _site(db_session, project)
    officer = await user_factory(
        email=f"isg-{uuid.uuid4().hex[:6]}@t.co", password="parola1234", role_key="site_chief"
    )
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sites/{site.id}",
        json={"safety_officer_user_id": str(officer.id), "safety_officer_is_outsourced": True},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert (
        resp.json()["detail"] == "İSG uzmanı ya sistem kullanıcısı ya dış kaynak (OSGB) olabilir."
    )


# --- Taslaktan yayina gecis (§5.3) ---


async def test_patch_publish_with_complete_record_returns_200(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("T7-7")
    site = await _site(
        db_session,
        project,
        is_draft=True,
        site_manager_name="Selim Öz",
        city="Ankara",
        construction_area_m2="12000.00",
        start_date=date(2026, 3, 1),
        end_date=date(2027, 3, 1),
    )
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(f"/sites/{site.id}", json={"is_draft": False}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    assert resp.json()["is_draft"] is False
    assert (await _reload(db_session, site)).is_draft is False


async def test_patch_publish_with_missing_fields_returns_422_and_stays_draft(
    client, db_session, user_factory, project_factory
):
    """Yayin SIKI: eksik kayit yayina gecemez ve satir TASLAK KALIR (DB'den okunur)."""
    project = await project_factory("T7-8")
    site = await _site(db_session, project, is_draft=True, city=None)
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(f"/sites/{site.id}", json={"is_draft": False}, headers=_auth(token))

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "Şantiye şefi seçiniz."
    assert (await _reload(db_session, site)).is_draft is True


async def test_patch_publish_merges_existing_row_with_patch(
    client, db_session, user_factory, project_factory
):
    """Eksik alan PATCH'te geliyorsa yayina gecis BASARILI — birlesik kayit kurali."""
    project = await project_factory("T7-9")
    site = await _site(db_session, project, is_draft=True, city=None)
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sites/{site.id}", json=_publish_ready(is_draft=False), headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    row = await _reload(db_session, site)
    assert row.is_draft is False
    assert row.city == "Ankara"


async def test_patch_draft_true_to_true_no_publish_rule(
    client, db_session, user_factory, project_factory
):
    """Taslak taslak KALIYORSA yayin kurallari KOSMAZ — eksik taslak duzenlenebilir."""
    project = await project_factory("T7-10")
    site = await _site(db_session, project, is_draft=True, city=None)
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sites/{site.id}", json={"is_draft": True, "name": "Yarım Taslak"}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Yarım Taslak"
    assert (await _reload(db_session, site)).is_draft is True


# --- Sozlesme siniri: govdede kabul EDILMEYEN alanlar ---


async def test_patch_has_no_project_id(client, db_session, user_factory, project_factory):
    """Santiye baska projeye TASINAMAZ: `project_id` govdede gelirse YOK SAYILIR."""
    project = await project_factory("T7-11")
    other = await project_factory("T7-11b")
    site = await _site(db_session, project)
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sites/{site.id}",
        json={"name": "Taşınmaz", "project_id": str(other.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    assert (await _reload(db_session, site)).project_id == project.id


async def test_patch_sections_not_accepted(client, db_session, user_factory, project_factory):
    """Bolumler P2 uclariyla yonetilir (§7.3): PATCH govdesindeki `sections` YOK SAYILIR."""
    project = await project_factory("T7-12")
    site = await _site(db_session, project)
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sites/{site.id}",
        json={"name": "Bölümsüz", "sections": [{"name": "Sızıntı Bölüm"}]},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    rows = (
        (await db_session.execute(select(Section).where(Section.site_id == site.id)))
        .scalars()
        .all()
    )
    assert rows == []


async def test_patch_manager_user_updates_name_snapshot(
    client, db_session, user_factory, project_factory
):
    """FK degisince ad anlik goruntusu de GUNCELLENIR — yoksa eski ad kalir ve yalan soyler."""
    project = await project_factory("T7-13")
    site = await _site(db_session, project, site_manager_name="Eski Şef")
    chief = await user_factory(
        email=f"sef-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        full_name="Ayşe Kaya",
    )
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sites/{site.id}", json={"site_manager_user_id": str(chief.id)}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["site_manager_name"] == "Ayşe Kaya"


async def test_patch_unknown_manager_user_returns_422(
    client, db_session, user_factory, project_factory
):
    """Bilinmeyen kullanici 404 DEGIL 422; kayit degismez."""
    project = await project_factory("T7-13b")
    site = await _site(db_session, project, name="Değişmedi")
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sites/{site.id}",
        json={"name": "Değişti", "site_manager_user_id": str(uuid.uuid4())},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "Seçilen kullanıcı bulunamadı"
    assert (await _reload(db_session, site)).name == "Değişmedi"


# --- Kod cakismasi (karar 2026-07-30) ---
#
# PATCH'te kod cakismasi POST ile AYNI Turkce mesaji verir. Onceden yalniz
# `uq_sites_project_code` -> IntegrityError -> genel "Veri bütünlüğü hatası"
# donuyordu; kullanici hangi ALANIN sorunlu oldugunu goremiyordu. On kontrol
# eklendi, kisit YARIS DURUMU emniyet agi olarak KALIR (§8.3).


async def test_patch_duplicate_code_returns_409_with_field_message(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("T7-18")
    await _site(db_session, project, code="SNT-2026-001")
    target = await _site(db_session, project, code="SNT-2026-002")
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sites/{target.id}", json={"code": "SNT-2026-001"}, headers=_auth(token)
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Bu şantiye kodu bu projede zaten kullanılıyor"
    assert (await _reload(db_session, target)).code == "SNT-2026-002"


async def test_patch_same_code_on_itself_is_not_a_conflict(
    client, db_session, user_factory, project_factory
):
    """Kendi kodunu yeniden gondermek CAKISMA DEGILDIR — aksi hâlde formun tum
    alanlari birlikte gonderildiginde her PATCH 409 verirdi."""
    project = await project_factory("T7-19")
    site = await _site(db_session, project, code="SNT-2026-003")
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sites/{site.id}",
        json={"code": "SNT-2026-003", "name": "Yeni Ad"},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Yeni Ad"


async def test_patch_code_conflict_is_scoped_to_project(
    client, db_session, user_factory, project_factory
):
    """Kisit PROJE ICI tekildir (§0.3/2): baska projedeki ayni kod engel DEGILDIR."""
    project = await project_factory("T7-20")
    other = await project_factory("T7-21")
    await _site(db_session, other, code="SNT-2026-004")
    site = await _site(db_session, project, code="SNT-2026-005")
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sites/{site.id}", json={"code": "SNT-2026-004"}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["code"] == "SNT-2026-004"


# --- Izin / gorunurluk ---


async def test_patch_requires_full_permission(client, db_session, user_factory, project_factory):
    project = await project_factory("T7-14")
    site = await _site(db_session, project)
    token = await _login(client, db_session, user_factory, VIEW_ONLY_ROLE)

    resp = await client.patch(f"/sites/{site.id}", json={"name": "X"}, headers=_auth(token))

    assert resp.status_code == 403, resp.text


async def test_patch_invisible_site_returns_404(client, db_session, user_factory, project_factory):
    """Gorunmeyen santiye ile VAR OLMAYAN UUID birebir AYNI govdeyi dondurur."""
    project = await project_factory("T7-15")
    site = await _site(db_session, project)
    token = await _login(client, db_session, user_factory, grant_all=False)

    hidden = await client.patch(f"/sites/{site.id}", json={"name": "X"}, headers=_auth(token))
    missing = await client.patch(f"/sites/{uuid.uuid4()}", json={"name": "X"}, headers=_auth(token))

    assert hidden.status_code == 404
    assert hidden.json() == missing.json() == {"detail": "Şantiye bulunamadı"}


# --- Bolum uclari: `manager_user_id` (spec §7 tablosu, 5. ve 6. satir) ---


async def test_post_section_manager_user_id(client, db_session, user_factory, project_factory):
    project = await project_factory("T7-16")
    site = await _site(db_session, project)
    manager = await user_factory(
        email=f"bs-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        full_name="Mehmet Yıldız",
    )
    token = await _login(client, db_session, user_factory)

    resp = await client.post(
        f"/sites/{site.id}/sections",
        # `is_draft` P6 T3'te eklendi: burada olculen sey SORUMLU FK -> ad anlik
        # goruntusudur, `Form - Bolum Ekle`in zorunluluklari degil.
        json={"name": "Kaba İnşaat", "manager_user_id": str(manager.id), "is_draft": True},
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["manager_user_id"] == str(manager.id)
    assert resp.json()["manager_name"] == "Mehmet Yıldız"


async def test_patch_section_manager_user_id(client, db_session, user_factory, project_factory):
    project = await project_factory("T7-17")
    site = await _site(db_session, project)
    section = Section(site_id=site.id, name="Kaba İnşaat", manager_name="Eski Sorumlu")
    db_session.add(section)
    await db_session.flush()
    manager = await user_factory(
        email=f"bs-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        full_name="Mehmet Yıldız",
    )
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={"manager_user_id": str(manager.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["manager_user_id"] == str(manager.id)
    assert resp.json()["manager_name"] == "Mehmet Yıldız"

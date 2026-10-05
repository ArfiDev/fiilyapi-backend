"""T6 — `POST /projects/{id}/sites` genislemesi, bolumlerin ATOMIK yazimi, sef/ISG cozumu.

Spec: §6.1 (giris semasi), §8.1 (dokuz adimlik akis), §8.2 (atomiklik), §9 (izin/IDOR),
§12.4 (atomiklik test seti 18-20).

Bu dosyanin en kritik uc testi 15/16/17'dir: kismi yazim SESSIZ bir veri hatasidir —
kullaniciya 422/409 doner ama DB'de yarim bir santiye kalir ve kimse fark etmez.
Bu yuzden kanit HTTP kodu DEGIL, islem oncesi/sonrasi `count(*)` esitligidir.
"""

import uuid
from decimal import Decimal

from sqlalchemy import func, select

from app.core.timezone import today
from app.modules.sites.models import Section, Site

# `patron` sites=full (yazar); `site_chief` sites=view (yazamaz) — seed matrisi.
WRITE_ROLE = "patron"
VIEW_ONLY_ROLE = "site_chief"

# Spec §3.3: OSGB secilince yazilan SABIT etiket. Firma adi alani ICAT EDILMEZ.
OUTSOURCED_LABEL = "Dış Kaynak — OSGB"


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


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _site_row(session, site_id) -> Site:
    return (await session.execute(select(Site).where(Site.id == site_id))).scalar_one()


def _publishable_body(**overrides) -> dict:
    """Taslak-DISI POST'un gecmesi icin gereken asgari zorunlu alanlar (§5.1/7-10)."""
    body = {
        "name": "A-Blok Şantiyesi",
        "site_manager_name": "Selim Öz",
        "city": "Ankara",
        "construction_area_m2": "12000.00",
        "start_date": "2026-03-01",
        "end_date": "2027-03-01",
    }
    body.update(overrides)
    return body


# --- Mutlu yol + alanlar ---


async def test_post_minimum_body_draft_returns_201(
    client, db_session, user_factory, project_factory
):
    """Taslak: yalniz `name`. Kod uretilir, SEKIZ tesis de `false` (§14.2)."""
    project = await project_factory("T6-1")
    token = await _login(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json={"name": "Taslak Şantiye", "is_draft": True},
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["code"] == f"SNT-{today().year}-001"
    assert body["is_draft"] is True
    assert body["facilities"] == {
        "closed_warehouse": False,
        "open_storage": False,
        "cold_storage": False,
        "site_office": False,
        "canteen": False,
        "changing_room_wc": False,
        "dormitory": False,
        "infirmary": False,
    }


async def test_post_full_mockup_body_returns_201(client, db_session, user_factory, project_factory):
    """Mockup'in TUM alanlari yazilir ve GET ile birebir geri okunur."""
    project = await project_factory("T6-2")
    token = await _login(client, db_session, user_factory)
    body = _publishable_body(
        code="SNT-2026-777",
        status="on_hold",
        neighborhood="Kuyubaşı Mah.",
        parcel="1234/5",
        address="Kuyubaşı Mah. No:12",
        gps_coordinates="39.9208, 32.8541",
        land_area_m2="8000.50",
        floor_info="2 bodrum + 10 normal",
        budget="45000000.00",
        delivery_date="2027-06-01",
        electricity_subscription_no="EL-99",
        water_subscription_no="SU-88",
        planned_worker_count=120,
    )

    resp = await client.post(f"/projects/{project.id}/sites", json=body, headers=_auth(token))

    assert resp.status_code == 201, resp.text
    site_id = resp.json()["id"]
    read = await client.get(f"/sites/{site_id}", headers=_auth(token))
    got = read.json()
    assert got["code"] == "SNT-2026-777"
    assert got["status"] == "on_hold"
    assert got["neighborhood"] == "Kuyubaşı Mah."
    assert got["parcel"] == "1234/5"
    assert got["address"] == "Kuyubaşı Mah. No:12"
    assert got["gps_coordinates"] == "39.9208, 32.8541"
    assert Decimal(got["land_area_m2"]) == Decimal("8000.50")
    assert Decimal(got["construction_area_m2"]) == Decimal("12000.00")
    assert got["floor_info"] == "2 bodrum + 10 normal"
    assert got["city"] == "Ankara"
    assert got["start_date"] == "2026-03-01"
    assert got["end_date"] == "2027-03-01"
    assert got["delivery_date"] == "2027-06-01"
    assert Decimal(got["budget"]) == Decimal("45000000.00")
    assert got["electricity_subscription_no"] == "EL-99"
    assert got["water_subscription_no"] == "SU-88"
    assert got["planned_worker_count"] == 120
    assert got["site_manager_name"] == "Selim Öz"
    assert got["is_draft"] is False


async def test_post_status_preparation_returns_201(
    client, db_session, user_factory, project_factory
):
    """T1'de eklenen yeni enum degeri yazma yolundan gecebiliyor (mockup 71)."""
    project = await project_factory("T6-3")
    token = await _login(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable_body(status="preparation"),
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "preparation"


async def test_post_construction_area_round_trips(
    client, db_session, user_factory, project_factory
):
    """§13/16 regresyonu: kolon P1.1a'da vardi ama semada YOKTU — sessizce dusuyordu."""
    project = await project_factory("T6-4")
    token = await _login(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable_body(construction_area_m2="9876.54"),
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    site = await _site_row(db_session, uuid.UUID(resp.json()["id"]))
    assert site.construction_area_m2 == Decimal("9876.54")


async def test_post_facilities_grouped_body_maps_to_columns(
    client, db_session, user_factory, project_factory
):
    """API GRUPLU, DB DUZ (§4.1): sekiz anahtar sekiz `has_*` kolonuna dusmeli."""
    project = await project_factory("T6-5")
    token = await _login(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable_body(
            facilities={
                "closed_warehouse": True,
                "open_storage": False,
                "cold_storage": True,
                "site_office": True,
                "canteen": False,
                "changing_room_wc": True,
                "dormitory": False,
                "infirmary": True,
            }
        ),
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    site = await _site_row(db_session, uuid.UUID(resp.json()["id"]))
    assert (site.has_closed_warehouse, site.has_open_storage) == (True, False)
    assert (site.has_cold_storage, site.has_site_office) == (True, True)
    assert (site.has_canteen, site.has_changing_room_wc) == (False, True)
    assert (site.has_dormitory, site.has_infirmary) == (False, True)


async def test_post_omitted_facilities_all_false(client, db_session, user_factory, project_factory):
    """§14.2 regresyonu: mockup'taki on-isaretler ORNEK VERIDIR, varsayilan degil."""
    project = await project_factory("T6-6")
    token = await _login(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/sites", json=_publishable_body(), headers=_auth(token)
    )

    assert resp.status_code == 201, resp.text
    site = await _site_row(db_session, uuid.UUID(resp.json()["id"]))
    assert not any(
        (
            site.has_closed_warehouse,
            site.has_open_storage,
            site.has_cold_storage,
            site.has_site_office,
            site.has_canteen,
            site.has_changing_room_wc,
            site.has_dormitory,
            site.has_infirmary,
        )
    )


# --- Bolumler ---


async def test_post_with_three_sections_assigns_sort_order_0_1_2(
    client, db_session, user_factory, project_factory
):
    """`sort_order` GOVDEDEN gelmez, dizi sirasindan atanir (§6.1)."""
    project = await project_factory("T6-7")
    token = await _login(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable_body(
            sections=[
                {"name": "Kaba İnşaat"},
                {"name": "İnce İşler", "start_date": "2026-06-01", "end_date": "2026-12-01"},
                {"name": "Çevre Düzenleme"},
            ]
        ),
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    sections = resp.json()["sections"]
    assert [s["name"] for s in sections] == ["Kaba İnşaat", "İnce İşler", "Çevre Düzenleme"]
    assert [s["sort_order"] for s in sections] == [0, 1, 2]
    assert sections[1]["start_date"] == "2026-06-01"


async def test_post_section_manager_user_id_persisted_with_name_snapshot(
    client, db_session, user_factory, project_factory
):
    """Bolum sorumlusu FK + ad anlik goruntusu birlikte yazilir (spec §3.4/models)."""
    project = await project_factory("T6-8")
    manager = await user_factory(
        email=f"bolum-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        full_name="Mehmet Yıldız",
    )
    token = await _login(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable_body(
            sections=[{"name": "Kaba İnşaat", "manager_user_id": str(manager.id)}]
        ),
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    section = resp.json()["sections"][0]
    assert section["manager_user_id"] == str(manager.id)
    assert section["manager_name"] == "Mehmet Yıldız"


async def test_post_section_estimated_amount_silently_ignored(
    client, db_session, user_factory, project_factory
):
    """§3.4: "Tahmini Bedel" SAKLANMAZ; `budget` BOQ tahsislerinden TURER.

    🔴 BLM-SAY: eski iddia yer tutucu bekliyordu. Bag acildi — yeni acilan
    bolumun tahsisi olmadigi icin turev `0.00`dir. Testin ASIL iddiasi
    (govdedeki `estimated_amount` SESSIZCE YUTULUR, yanita girmez) DEGISMEDI.
    """
    project = await project_factory("T6-9")
    token = await _login(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable_body(
            sections=[{"name": "Kaba İnşaat", "estimated_amount": "1840000.00"}]
        ),
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    section = resp.json()["sections"][0]
    assert "estimated_amount" not in section
    assert section["budget"] == {"available": True, "value": "0.00", "pending_module": None}


async def test_post_without_sections_creates_none(
    client, db_session, user_factory, project_factory
):
    """Bolum ISTEGE BAGLIDIR: otomatik "Genel" bolumu ACILMAZ (P2 §2.4)."""
    project = await project_factory("T6-10")
    token = await _login(client, db_session, user_factory)
    before = await _count(db_session, Section)

    resp = await client.post(
        f"/projects/{project.id}/sites", json=_publishable_body(), headers=_auth(token)
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["sections"] == []
    assert resp.json()["section_count"] == 0
    assert await _count(db_session, Section) == before


# --- Sef / ISG cozumu ---


async def test_site_manager_name_snapshot_overwritten_from_user(
    client, db_session, user_factory, project_factory
):
    """FK doluysa `users.full_name` govdedeki serbest metnin UZERINE yazilir (§6.1)."""
    project = await project_factory("T6-11")
    chief = await user_factory(
        email=f"sef-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        full_name="Ayşe Kaya",
    )
    token = await _login(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable_body(
            site_manager_user_id=str(chief.id), site_manager_name="Elle Yazilan Ad"
        ),
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["site_manager_user_id"] == str(chief.id)
    assert body["site_manager_name"] == "Ayşe Kaya"


async def test_safety_officer_outsourced_writes_fixed_label(
    client, db_session, user_factory, project_factory
):
    """OSGB secilince SABIT etiket yazilir; firma adi alani YOKTUR (§3.3)."""
    project = await project_factory("T6-12")
    token = await _login(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable_body(safety_officer_is_outsourced=True),
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["safety_officer_is_outsourced"] is True
    assert body["safety_officer_user_id"] is None
    assert body["safety_officer_name"] == OUTSOURCED_LABEL


async def test_safety_officer_user_writes_name_snapshot(
    client, db_session, user_factory, project_factory
):
    """ISG sistem kullanicisiysa ad anlik goruntusu ondan gelir (§3.3)."""
    project = await project_factory("T6-12b")
    officer = await user_factory(
        email=f"isg-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        full_name="Emre Şahin",
    )
    token = await _login(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable_body(safety_officer_user_id=str(officer.id)),
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["safety_officer_name"] == "Emre Şahin"


async def test_unknown_manager_user_returns_422(client, db_session, user_factory, project_factory):
    """404 DEGIL 422: istenen kaynak santiyedir, kullanici bir ALAN DEGERIDIR (§9).

    Ayrica HICBIR SEY yazilmaz — cozum yazmadan ONCE kosar (§8.1/3).
    """
    project = await project_factory("T6-13")
    token = await _login(client, db_session, user_factory)
    before = await _count(db_session, Site)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable_body(site_manager_user_id=str(uuid.uuid4())),
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "Seçilen kullanıcı bulunamadı"
    assert await _count(db_session, Site) == before


async def test_on_leave_user_can_be_site_manager(client, db_session, user_factory, project_factory):
    """IZINLI personel ATANABILIR (karar 2026-07-30).

    Izin GECICI bir durumdur; yillik izindeki sef hâlâ o santiyenin sefidir.
    Reddedilseydi sef tatildeyken santiye ACILAMAZDI.
    """
    project = await project_factory("T6-13b")
    on_leave = await user_factory(
        email=f"izinli-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        status="on_leave",
        full_name="İzinli Şef",
    )
    token = await _login(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable_body(site_manager_user_id=str(on_leave.id)),
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["site_manager_name"] == "İzinli Şef"


async def test_inactive_user_returns_422(client, db_session, user_factory, project_factory):
    """Pasif kullanici santiye sefi olarak ATANAMAZ (§9)."""
    project = await project_factory("T6-14")
    passive = await user_factory(
        email=f"pasif-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        status="passive",
    )
    token = await _login(client, db_session, user_factory)
    before = await _count(db_session, Site)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable_body(site_manager_user_id=str(passive.id)),
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "Seçilen kullanıcı bulunamadı"
    assert await _count(db_session, Site) == before


# --- ATOMIKLIK (spec §12.4/18-20) — kanit SAYIM esitligidir, HTTP kodu DEGIL ---


async def test_post_three_sections_second_invalid_writes_nothing(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("T6-15")
    token = await _login(client, db_session, user_factory)
    sites_before = await _count(db_session, Site)
    sections_before = await _count(db_session, Section)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable_body(
            sections=[
                {"name": "Kaba İnşaat"},
                {"name": "İnce İşler", "start_date": "2026-12-01", "end_date": "2026-06-01"},
                {"name": "Çevre Düzenleme"},
            ]
        ),
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "2. bölüm: bitiş tarihi başlangıçtan önce olamaz."
    assert await _count(db_session, Site) == sites_before
    assert await _count(db_session, Section) == sections_before


async def test_post_duplicate_code_writes_no_sections(
    client, db_session, user_factory, project_factory
):
    """Kod cakismasi TUM olusturmayi geri alir: santiye de bolum de yazilmaz."""
    project = await project_factory("T6-16")
    existing = Site(project_id=project.id, code="A-BLOK", name="Mevcut Şantiye")
    db_session.add(existing)
    await db_session.flush()
    token = await _login(client, db_session, user_factory)
    sites_before = await _count(db_session, Site)
    sections_before = await _count(db_session, Section)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable_body(
            code="A-BLOK", sections=[{"name": "Kaba İnşaat"}, {"name": "İnce İşler"}]
        ),
        headers=_auth(token),
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Bu şantiye kodu bu projede zaten kullanılıyor"
    assert await _count(db_session, Site) == sites_before
    assert await _count(db_session, Section) == sections_before


async def test_post_blank_section_name_writes_nothing(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("T6-17")
    token = await _login(client, db_session, user_factory)
    sites_before = await _count(db_session, Site)
    sections_before = await _count(db_session, Section)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable_body(sections=[{"name": "Kaba İnşaat"}, {"name": "   "}]),
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "2. bölüm: bölüm adı zorunludur."
    assert await _count(db_session, Site) == sites_before
    assert await _count(db_session, Section) == sections_before


# --- Izin / gorunurluk ---


async def test_post_requires_full_permission(client, db_session, user_factory, project_factory):
    project = await project_factory("T6-18")
    token = await _login(client, db_session, user_factory, VIEW_ONLY_ROLE)

    resp = await client.post(
        f"/projects/{project.id}/sites", json=_publishable_body(), headers=_auth(token)
    )

    assert resp.status_code == 403, resp.text


async def test_post_invisible_project_returns_404(
    client, db_session, user_factory, project_factory
):
    """Gorunmeyen proje 403 DEGIL 404 dondurur; govde ayirt edici DEGILDIR."""
    project = await project_factory("T6-19")
    token = await _login(client, db_session, user_factory, grant_all=False)

    resp = await client.post(
        f"/projects/{project.id}/sites", json=_publishable_body(), headers=_auth(token)
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Proje bulunamadı"

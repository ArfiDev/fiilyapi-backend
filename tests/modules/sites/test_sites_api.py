"""Task 7 — santiye/bolum uclari ve denetim gunlugu (spec §4, §7)."""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.core.timezone import today
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.sites.models import Section, Site, SiteStatus


async def _login(client, user_factory, role_key: str, email: str | None = None) -> str:
    address = email or f"{role_key}@t.co"
    await user_factory(email=address, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


async def _login_with_access(client, session, user_factory, role_key: str) -> str:
    """system_admin disindaki roller icin gorunurluk user_project_access'ten gelir."""
    address = f"{role_key}@t.co"
    user = await user_factory(email=address, password="parola1234", role_key=role_key)
    user.all_projects = True
    await session.flush()
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _site(session, project, code: str = "A-BLOK", **kwargs) -> Site:
    site = Site(project_id=project.id, code=code, name=kwargs.pop("name", "A-Blok Şantiyesi"))
    for field, value in kwargs.items():
        setattr(site, field, value)
    session.add(site)
    await session.flush()
    return site


async def _audit_details(session, action: AuditAction) -> list[str]:
    rows = (
        (await session.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()
    )
    return [row.detail for row in rows]


async def test_list_sites_unauthenticated(client, project_factory):
    project = await project_factory("A-1")
    resp = await client.get(f"/projects/{project.id}/sites")
    assert resp.status_code == 401


async def test_list_sites_happy_path(client, db_session, user_factory, project_factory):
    project = await project_factory("A-2", city="Ankara")
    await _site(db_session, project, "A-BLOK", address="Kuyubaşı Mah.", site_manager_name="S. Ö.")
    await _site(db_session, project, "B-BLOK", name="B-Blok Şantiyesi")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get(f"/projects/{project.id}/sites", headers=_auth(token))

    assert resp.status_code == 200
    body = resp.json()
    assert [s["code"] for s in body["items"]] == ["A-BLOK", "B-BLOK"]
    # `draft` T4'te eklendi (§5.2) — mevcut sayaclar aynen KALDI.
    assert body["counts"] == {"all": 2, "active": 2, "on_hold": 0, "completed": 0, "draft": 0}
    assert body["items"][0]["city"] == "Ankara"
    assert body["items"][0]["city_inherited"] is True
    assert body["items"][0]["site_manager_name"] == "S. Ö."
    assert body["totals"]["average_margin"] == {
        "available": False,
        "value": None,
        "pending_module": "project_costs",
    }


async def test_list_sites_empty_project(client, user_factory, project_factory):
    project = await project_factory("A-3")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get(f"/projects/{project.id}/sites", headers=_auth(token))

    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert resp.json()["counts"]["all"] == 0


async def test_get_site_detail(client, db_session, user_factory, project_factory):
    project = await project_factory("A-4", name="Güneşkent", employer_name="GK A.Ş.")
    site = await _site(db_session, project)
    db_session.add(Section(site_id=site.id, name="Kat 6-10", sort_order=1))
    await db_session.flush()
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["project"]["name"] == "Güneşkent"
    assert body["project"]["employer_name"] == "GK A.Ş."
    assert body["section_count"] == 1
    assert body["section_status_counts"] == {"planned": 1, "active": 0, "completed": 0}
    assert body["sections"][0]["name"] == "Kat 6-10"
    assert body["contract_amount"]["pending_module"] == "contracts"
    assert "delay_risk" not in resp.text


async def test_get_site_missing_returns_404(client, user_factory):
    token = await _login(client, user_factory, "system_admin")
    resp = await client.get(f"/sites/{uuid.uuid4()}", headers=_auth(token))
    assert resp.status_code == 404


async def test_list_sections(client, db_session, user_factory, project_factory):
    project = await project_factory("A-5")
    site = await _site(db_session, project)
    db_session.add(Section(site_id=site.id, name="İkinci", sort_order=2))
    db_session.add(Section(site_id=site.id, name="Birinci", sort_order=1))
    await db_session.flush()
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get(f"/sites/{site.id}/sections", headers=_auth(token))

    assert resp.status_code == 200
    body = resp.json()
    assert [s["name"] for s in body["items"]] == ["Birinci", "İkinci"]
    assert body["counts"]["planned"] == 2
    assert body["items"][0]["boq_item_count"]["pending_module"] == "boq"


async def test_create_site_and_audit(client, db_session, user_factory, project_factory):
    project = await project_factory("A-6")
    token = await _login_with_access(client, db_session, user_factory, "patron")

    resp = await client.post(
        f"/projects/{project.id}/sites",
        # `is_draft` T6'da eklendi: taslak-disi POST artik sef/il/insaat alani/tarih
        # zorunlulugunu kosar (spec §5.1/7-10). Bu test KOD + DENETIM GUNLUGUNU
        # sinar; taslak yolu ikisini de aynen kullanir.
        json={"name": "A-Blok Şantiyesi", "address": "Kuyubaşı Mah.", "is_draft": True},
        headers=_auth(token),
    )

    assert resp.status_code == 201
    body = resp.json()
    # Kod SNT-{YYYY}-{NNN} ureticisinden gelir (spec §3.2), addan TURETILMEZ.
    assert body["code"] == f"SNT-{today().year}-001"
    assert body["status"] == "active"
    assert body["section_count"] == 0
    assert body["sections"] == []
    details = await _audit_details(db_session, AuditAction.create)
    assert any("A-Blok Şantiyesi" in d for d in details)


async def test_create_site_duplicate_code_returns_409(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("A-7")
    await _site(db_session, project, "A-BLOK")
    token = await _login_with_access(client, db_session, user_factory, "patron")

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json={"name": "Kopya", "code": "A-BLOK", "is_draft": True},
        headers=_auth(token),
    )

    assert resp.status_code == 409


async def test_patch_site_and_audit(client, db_session, user_factory, project_factory):
    project = await project_factory("A-8")
    site = await _site(db_session, project, name="Eski Ad")
    token = await _login_with_access(client, db_session, user_factory, "project_manager")

    resp = await client.patch(
        f"/sites/{site.id}", json={"name": "Yeni Ad", "status": "on_hold"}, headers=_auth(token)
    )

    assert resp.status_code == 200
    assert resp.json()["name"] == "Yeni Ad"
    assert resp.json()["status"] == "on_hold"
    assert any("Yeni Ad" in d for d in await _audit_details(db_session, AuditAction.update))


async def test_create_section_and_audit(client, db_session, user_factory, project_factory):
    project = await project_factory("A-9")
    site = await _site(db_session, project)
    token = await _login_with_access(client, db_session, user_factory, "patron")

    resp = await client.post(
        f"/sites/{site.id}/sections",
        # `is_draft` P6 T3'te eklendi: bu test DURUM VARSAYILANINI ve denetim
        # satirini olcer, `Form - Bolum Ekle`in zorunluluklarini DEGIL — taslak
        # govdesi o kurallari kapsam disi birakir (zorunluluklari
        # `test_section_create.py` sabitler).
        json={"name": "Kat 6-10 Kaba İnşaat", "sort_order": 2, "is_draft": True},
        headers=_auth(token),
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "planned"
    assert body["sort_order"] == 2
    # BLM-SAY: yeni acilan bolumun tahsisi YOKTUR — turev bedel `0.00`dir ve
    # dolu zarf `pending_module` TASIMAZ (P10 T3). Eski iddia (`== "boq"`) bu
    # dilimde CURUDU.
    assert body["budget"] == {"available": True, "value": "0.00", "pending_module": None}
    details = await _audit_details(db_session, AuditAction.create)
    assert any("Kat 6-10 Kaba İnşaat" in d and "A-Blok Şantiyesi" in d for d in details)


async def test_patch_section_and_audit(client, db_session, user_factory, project_factory):
    project = await project_factory("A-10")
    site = await _site(db_session, project)
    section = Section(site_id=site.id, name="Eski Bölüm")
    db_session.add(section)
    await db_session.flush()
    token = await _login_with_access(client, db_session, user_factory, "patron")

    resp = await client.patch(
        f"/sections/{section.id}",
        json={"name": "Yeni Bölüm", "status": "active"},
        headers=_auth(token),
    )

    assert resp.status_code == 200
    assert resp.json()["name"] == "Yeni Bölüm"
    assert resp.json()["status"] == "active"
    assert any("Yeni Bölüm" in d for d in await _audit_details(db_session, AuditAction.update))


async def test_patch_section_missing_returns_404(client, db_session, user_factory):
    token = await _login_with_access(client, db_session, user_factory, "patron")
    resp = await client.patch(f"/sections/{uuid.uuid4()}", json={"name": "X"}, headers=_auth(token))
    assert resp.status_code == 404


# --- T8: okuma uclarinin genislemesi (spec §4.1, §5.2, §6.2) ---
#
# Bu blogun tasidigi tek kural: **YALNIZ EKLEME**. P2'nin dondurdugu hicbir alan
# kaldirilmadi/yeniden adlandirilmadi (test_p2_contract_fields_still_present bunun
# kilididir) ve donusturuculere eklenen 16 alan hem listede hem detayda gorunur.

# `SiteCard`e §6.2 ile EKLENEN alanlar. Liste burada ACIKCA yazilir: donusturucuden
# bir alan sessizce dusesse sema `ValidationError` atardi, ama dusen alan HIC
# eklenmemis olsaydi (ornegin yeni bir kolon icin) bu test onu yakalar.
_NEW_CARD_FIELDS = (
    "is_draft",
    "site_manager_user_id",
    "safety_officer_user_id",
    "safety_officer_name",
    "safety_officer_is_outsourced",
    "neighborhood",
    "parcel",
    "gps_coordinates",
    "land_area_m2",
    "construction_area_m2",
    "floor_info",
    "budget",
    "facilities",
    "electricity_subscription_no",
    "water_subscription_no",
    "planned_worker_count",
)

# P2'nin (ve P1.1a'nin) sozlesmesi — bu dilimde hicbirine DOKUNULMADI.
_P2_CARD_FIELDS = (
    "id",
    "code",
    "name",
    "status",
    "address",
    "city",
    "city_inherited",
    "site_manager_name",
    "start_date",
    "end_date",
    "delivery_date",
    "remaining_days",
    "section_count",
    "worker_count",
    "progress_pct",
)


async def _full_site(session, project, code: str = "A-BLOK", **kwargs):
    """Mockup'in tum yeni alanlari DOLU bir santiye satiri."""
    defaults = {
        "neighborhood": "Kuyubaşı Mah.",
        "parcel": "1234/5",
        "gps_coordinates": "39.9208, 32.8541",
        "land_area_m2": Decimal("8000.50"),
        "construction_area_m2": Decimal("12000.00"),
        "floor_info": "2 bodrum + 10 normal",
        "budget": Decimal("45000000.00"),
        "electricity_subscription_no": "EL-99",
        "water_subscription_no": "SU-88",
        "planned_worker_count": 120,
        "safety_officer_is_outsourced": True,
        "safety_officer_name": "Dış Kaynak — OSGB",
        "has_closed_warehouse": True,
        "has_infirmary": True,
    }
    defaults.update(kwargs)
    return await _site(session, project, code, **defaults)


async def test_list_response_includes_new_fields(client, db_session, user_factory, project_factory):
    project = await project_factory("A-12")
    await _full_site(db_session, project)
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get(f"/projects/{project.id}/sites", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    card = resp.json()["items"][0]
    assert all(field in card for field in _NEW_CARD_FIELDS)
    assert card["neighborhood"] == "Kuyubaşı Mah."
    assert card["parcel"] == "1234/5"
    assert card["gps_coordinates"] == "39.9208, 32.8541"
    assert Decimal(card["land_area_m2"]) == Decimal("8000.50")
    assert Decimal(card["construction_area_m2"]) == Decimal("12000.00")
    assert card["floor_info"] == "2 bodrum + 10 normal"
    assert Decimal(card["budget"]) == Decimal("45000000.00")
    assert card["electricity_subscription_no"] == "EL-99"
    assert card["water_subscription_no"] == "SU-88"
    assert card["planned_worker_count"] == 120
    assert card["safety_officer_is_outsourced"] is True
    assert card["safety_officer_name"] == "Dış Kaynak — OSGB"
    assert card["is_draft"] is False


async def test_detail_response_includes_new_fields(
    client, db_session, user_factory, project_factory
):
    """Detay `SiteCard`'i MIRAS ALIR — iki zarf tek donusturucuden beslenir."""
    project = await project_factory("A-13")
    site = await _full_site(db_session, project)
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert all(field in body for field in _NEW_CARD_FIELDS)
    assert body["planned_worker_count"] == 120
    assert body["facilities"]["closed_warehouse"] is True


async def test_list_counts_draft_is_correct(client, db_session, user_factory, project_factory):
    project = await project_factory("A-14")
    await _site(db_session, project, "T-1", is_draft=True)
    await _site(db_session, project, "T-2", is_draft=True)
    await _site(db_session, project, "Y-1")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get(f"/projects/{project.id}/sites", headers=_auth(token))

    assert resp.json()["counts"]["draft"] == 2


async def test_draft_sites_visible_to_everyone_with_project_access(
    client, db_session, user_factory, project_factory
):
    """§13/13: "yalniz benim taslaklarim" kavrami YOKTUR.

    Taslagi kimin yazdigi SAKLANMAZ; projeye erisimi olan herkes gorur. Aksi
    hâlde sefin yarim biraktigi santiyeyi mudur tamamlayamazdi.
    """
    project = await project_factory("A-15")
    site = await _site(db_session, project, "T-3", is_draft=True)
    token = await _login_with_access(client, db_session, user_factory, "site_chief")

    listed = await client.get(f"/projects/{project.id}/sites", headers=_auth(token))
    detail = await client.get(f"/sites/{site.id}", headers=_auth(token))

    assert [s["code"] for s in listed.json()["items"]] == ["T-3"]
    assert detail.status_code == 200, detail.text
    assert detail.json()["is_draft"] is True


async def test_draft_not_subtracted_from_status_counts(
    client, db_session, user_factory, project_factory
):
    """§5.2: taslak, DURUMU NE ISE o sayacta KALIR; `draft` AYRICA artar.

    Dusseydi sayaclarin toplami `all`i tutmaz, ekranda "3 şantiye" yazip
    kirilimda 2 gosterilirdi.
    """
    project = await project_factory("A-16")
    await _site(db_session, project, "T-4", is_draft=True)
    await _site(db_session, project, "T-5", is_draft=True, status=SiteStatus.on_hold)
    await _site(db_session, project, "Y-2")
    token = await _login(client, user_factory, "system_admin")

    counts = (await client.get(f"/projects/{project.id}/sites", headers=_auth(token))).json()[
        "counts"
    ]

    assert counts == {"all": 3, "active": 2, "on_hold": 1, "completed": 0, "draft": 2}
    assert counts["active"] + counts["on_hold"] + counts["completed"] == counts["all"]


async def test_section_response_includes_manager_user_id(
    client, db_session, user_factory, project_factory
):
    """FK ve ad anlik goruntusu BIRLIKTE doner: ad, kullanici silinse bile kalir."""
    project = await project_factory("A-17")
    site = await _site(db_session, project)
    manager = await user_factory(
        email=f"bs-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        full_name="Mehmet Yıldız",
    )
    db_session.add(
        Section(
            site_id=site.id,
            name="Kaba İnşaat",
            manager_user_id=manager.id,
            manager_name="Mehmet Yıldız",
        )
    )
    await db_session.flush()
    token = await _login(client, user_factory, "system_admin")

    listed = await client.get(f"/sites/{site.id}/sections", headers=_auth(token))
    detail = await client.get(f"/sites/{site.id}", headers=_auth(token))

    assert listed.json()["items"][0]["manager_user_id"] == str(manager.id)
    assert listed.json()["items"][0]["manager_name"] == "Mehmet Yıldız"
    assert detail.json()["sections"][0]["manager_user_id"] == str(manager.id)


async def test_section_budget_TAHSIS_YOKKEN_sifir(
    client, db_session, user_factory, project_factory
):
    """Bolum bedeli SAKLANMAZ (spec §2.2) — BOQ tahsislerinin turevidir.

    🔴 BLM-SAY: eski ad `..._placeholder_pending_boq`ti ve yer tutucu bekliyordu.
    Bag ACILDI; tahsisi olmayan bolum artik OLCULMUS `0.00` doner, yer tutucu
    DEGIL (K-MKD3: bos kume burada gercek bir sifirdir, cunku birlesim
    anahtarlarinin ikisi de NOT NULL — gerekce `boq/counts.py`de).
    """
    project = await project_factory("A-18")
    site = await _site(db_session, project)
    db_session.add(Section(site_id=site.id, name="Kaba İnşaat"))
    await db_session.flush()
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get(f"/sites/{site.id}/sections", headers=_auth(token))

    assert resp.json()["items"][0]["budget"] == {
        "available": True,
        "value": "0.00",
        "pending_module": None,
    }
    assert resp.json()["items"][0]["boq_item_count"] == {
        "available": True,
        "count": 0,
        "pending_module": "boq",
    }


async def test_no_duration_days_in_response(client, db_session, user_factory, project_factory):
    """§3.6: sure TUREVDIR, saklanmaz ve dondurulmez — iki gercek kaynak olmaz."""
    project = await project_factory("A-19")
    site = await _full_site(db_session, project)
    token = await _login(client, user_factory, "system_admin")

    listed = await client.get(f"/projects/{project.id}/sites", headers=_auth(token))
    detail = await client.get(f"/sites/{site.id}", headers=_auth(token))

    assert "duration_days" not in listed.text
    assert "duration_days" not in detail.text


async def test_p2_contract_fields_still_present(client, db_session, user_factory, project_factory):
    """GERIYE UYUM AGI (§6.2): P2 frontend'inin okudugu hicbir alan kaybolmadi."""
    project = await project_factory("A-20", city="Ankara")
    site = await _full_site(db_session, project, city=None, end_date=date(2027, 3, 1))
    token = await _login(client, user_factory, "system_admin")

    listed = await client.get(f"/projects/{project.id}/sites", headers=_auth(token))
    card = listed.json()["items"][0]
    detail = (await client.get(f"/sites/{site.id}", headers=_auth(token))).json()

    assert all(field in card for field in _P2_CARD_FIELDS)
    assert all(field in detail for field in _P2_CARD_FIELDS)
    # P2 mirasi iki turetim BOZULMADI: sehir devri + kalan gun.
    assert (card["city"], card["city_inherited"]) == ("Ankara", True)
    assert card["remaining_days"] == (date(2027, 3, 1) - today()).days
    # Detayin P2 ek alanlari da yerinde.
    assert {"project", "section_status_counts", "sections", "total_progress_payment"} <= set(detail)


async def test_facilities_returned_grouped(client, db_session, user_factory, project_factory):
    """§4.1: DB'de SEKIZ duz Boolean kolon, API'de TEK ic ice nesne.

    Duz `has_*` anahtarlari yanitta GORUNMEZ — gruplama gorsel kumelenmeyi tasir.
    """
    project = await project_factory("A-21")
    site = await _full_site(db_session, project)
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get(f"/sites/{site.id}", headers=_auth(token))

    assert resp.json()["facilities"] == {
        "closed_warehouse": True,
        "open_storage": False,
        "cold_storage": False,
        "site_office": False,
        "canteen": False,
        "changing_room_wc": False,
        "dormitory": False,
        "infirmary": True,
    }
    assert "has_closed_warehouse" not in resp.text


async def test_no_password_hash_leaks(client, db_session, user_factory, project_factory):
    project = await project_factory("A-11")
    await _site(db_session, project)
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get(f"/projects/{project.id}/sites", headers=_auth(token))

    assert "password_hash" not in resp.text

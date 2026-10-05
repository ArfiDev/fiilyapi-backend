"""T13 — spec §12.1/§12.2/§12.4 listesinde T3-T8'de KARSILANMAYAN maddeler.

Bu dosya yeni davranis TANIMLAMAZ; T3-T8'in birakti bosluklari kapatir ve
dilimin regresyon agini tamamlar. Buradaki testler kirmizi verirse cozum
ILGILI TASK'IN UYGULAMA KODUDUR — test gevsetilmez (plan T13 uygulama notu).

Kapsanan satirlar:

* §12.1/5   `duration_days` hicbir yanitta YOK (liste + detay + bolum)
* §12.2/15  sef/ISG ad anlik goruntusu kullanici SILININCE de yasiyor
* §12.2/16  P1.1a satir ici santiye akisi calisiyor ve artik `SNT-` kodu uretiyor
* §12.2/17  P2 doneminden kalma yanit sozlesmesi alan alan yerinde
* §12.4/18-20  atomikligin T6'da karsilanmayan varyantlari
"""

import uuid
from decimal import Decimal

from sqlalchemy import func, select

from app.modules.sites.models import Section, Site
from app.modules.users.models import User

WRITE_ROLE = "patron"
ADMIN_ROLE = "system_admin"

# P2 (2026-07-2x) doneminde frontend'in okudugu alanlar. Bu liste DARALTILAMAZ:
# her bir eleman o donemin ekranlarindan birinin bagimliligidir.
P2_CARD_FIELDS = (
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
P2_DETAIL_EXTRA_FIELDS = (
    "project",
    "section_status_counts",
    "sections",
    "total_progress_payment",
    "contract_amount",
)
P2_SECTION_FIELDS = (
    "id",
    "code",
    "name",
    "status",
    "manager_name",
    "start_date",
    "end_date",
    "sort_order",
    "progress_pct",
    "boq_item_count",
    "budget",
    "worker_count",
)
P2_COUNT_FIELDS = ("all", "active", "on_hold", "completed")
P2_TOTALS_FIELDS = (
    "total_progress_payment",
    "subcontractor_count",
    "active_worker_count",
    "average_margin",
)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, session, user_factory, role_key: str = WRITE_ROLE) -> str:
    address = f"{role_key}-{uuid.uuid4().hex[:6]}@t.co"
    user = await user_factory(email=address, password="parola1234", role_key=role_key)
    user.all_projects = True
    await session.flush()
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


async def _count(session, model) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


def _walk(payload) -> list[dict]:
    """Yanit agacindaki TUM sozlukleri duzlestirir (ic ice bolum/proje dahil)."""
    found: list[dict] = []
    if isinstance(payload, dict):
        found.append(payload)
        for value in payload.values():
            found.extend(_walk(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_walk(item))
    return found


# --- §12.2/15: anlik goruntu kullanici silinince de yasiyor ---


async def test_site_manager_snapshot_survives_user_deletion(
    client, db_session, user_factory, project_factory
):
    """FK `ON DELETE SET NULL` -> kimlik gider, AD KALIR.

    Anlik goruntu olmasaydi personel islerden ayrildiginda gecmis santiye
    kayitlarinda sef alani sessizce boslanirdi.
    """
    project = await project_factory("C13-1")
    token = await _login(client, db_session, user_factory)
    manager = await user_factory(
        email=f"sef-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        full_name="Selim Öz",
    )

    created = await client.post(
        f"/projects/{project.id}/sites",
        json={
            "name": "Anıtkabir Şantiyesi",
            "is_draft": True,
            "site_manager_user_id": str(manager.id),
        },
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    site_id = created.json()["id"]
    assert created.json()["site_manager_name"] == "Selim Öz"

    await db_session.delete(await db_session.get(User, manager.id))
    await db_session.flush()
    db_session.expire_all()

    detail = await client.get(f"/sites/{site_id}", headers=_auth(token))

    assert detail.status_code == 200, detail.text
    assert detail.json()["site_manager_user_id"] is None
    assert detail.json()["site_manager_name"] == "Selim Öz"


async def test_safety_officer_snapshot_survives_user_deletion(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("C13-2")
    token = await _login(client, db_session, user_factory)
    officer = await user_factory(
        email=f"isg-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="field_engineer",
        full_name="Ayşe Kaya",
    )

    created = await client.post(
        f"/projects/{project.id}/sites",
        json={
            "name": "B-Blok Şantiyesi",
            "is_draft": True,
            "safety_officer_user_id": str(officer.id),
        },
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    site_id = created.json()["id"]
    assert created.json()["safety_officer_name"] == "Ayşe Kaya"

    await db_session.delete(await db_session.get(User, officer.id))
    await db_session.flush()
    db_session.expire_all()

    detail = await client.get(f"/sites/{site_id}", headers=_auth(token))

    assert detail.status_code == 200, detail.text
    assert detail.json()["safety_officer_user_id"] is None
    assert detail.json()["safety_officer_name"] == "Ayşe Kaya"
    assert detail.json()["safety_officer_is_outsourced"] is False


async def test_section_manager_snapshot_survives_user_deletion(
    client, db_session, user_factory, project_factory
):
    """Bolum sefi de ayni kurala tabidir (`sections.manager_user_id`, T2)."""
    project = await project_factory("C13-3")
    token = await _login(client, db_session, user_factory)
    manager = await user_factory(
        email=f"bolum-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        full_name="Murat Demir",
    )

    created = await client.post(
        f"/projects/{project.id}/sites",
        json={
            "name": "C-Blok Şantiyesi",
            "is_draft": True,
            "sections": [{"name": "Kaba İnşaat", "manager_user_id": str(manager.id)}],
        },
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    site_id = created.json()["id"]

    await db_session.delete(await db_session.get(User, manager.id))
    await db_session.flush()
    db_session.expire_all()

    sections = await client.get(f"/sites/{site_id}/sections", headers=_auth(token))

    assert sections.status_code == 200, sections.text
    row = sections.json()["items"][0]
    assert row["manager_user_id"] is None
    assert row["manager_name"] == "Murat Demir"


# --- §12.2/16: P1.1a satir ici santiye akisi ---


async def test_inline_site_flow_still_works_end_to_end(client, db_session, user_factory):
    """Proje formunun satir ici santiyeleri (P1.1a) kirilmadi ve `SNT-` uretiyor.

    T3 ad-turevi ureticiyi kaldirdi; satir ici akis kendi kopya ureticisini
    tasisaydi bugun iki farkli kod deseni yan yana yasiyor olurdu.
    """
    token = await _login(client, db_session, user_factory, ADMIN_ROLE)

    created = await client.post(
        "/projects",
        json={
            "name": "Satır İçi Proje",
            "project_type": "taahhut",
            "is_draft": True,
            "sites": [
                {
                    "name": "Satır İçi Şantiye 1",
                    "site_manager_name": "Selim Öz",
                    "construction_area_m2": "12000.00",
                },
                {"name": "Satır İçi Şantiye 2"},
            ],
        },
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]

    listing = await client.get(f"/projects/{project_id}/sites", headers=_auth(token))

    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert len(items) == 2
    assert all(item["code"].startswith("SNT-") for item in items), items
    assert {item["name"] for item in items} == {"Satır İçi Şantiye 1", "Satır İçi Şantiye 2"}
    first = next(item for item in items if item["name"] == "Satır İçi Şantiye 1")
    assert first["site_manager_name"] == "Selim Öz"
    assert Decimal(first["construction_area_m2"]) == Decimal("12000.00")
    # Satir ici santiye TASLAK degildir: proje formunun santiyesi yayindadir.
    assert first["is_draft"] is False
    assert listing.json()["counts"]["draft"] == 0


async def test_inline_site_codes_continue_the_global_counter(client, db_session, user_factory):
    """Satir ici uretici ile `POST /sites` ureticisi AYNI sayaci paylasiyor."""
    token = await _login(client, db_session, user_factory, ADMIN_ROLE)

    first = await client.post(
        "/projects",
        json={
            "name": "Sayaç A",
            "project_type": "taahhut",
            "is_draft": True,
            "sites": [{"name": "Şantiye A"}],
        },
        headers=_auth(token),
    )
    second = await client.post(
        "/projects",
        json={
            "name": "Sayaç B",
            "project_type": "taahhut",
            "is_draft": True,
            "sites": [{"name": "Şantiye B"}],
        },
        headers=_auth(token),
    )
    assert first.status_code == second.status_code == 201

    codes = sorted(
        (await db_session.execute(select(Site.code).order_by(Site.code))).scalars().all()
    )
    assert len(codes) == 2
    assert codes[0] != codes[1], codes
    assert [code.rsplit("-", 1)[1] for code in codes] == ["001", "002"]


# --- §12.2/17: P2 sozlesmesi kirilmadi ---


async def test_p2_response_contract_unbroken(client, db_session, user_factory, project_factory):
    """P2 doneminin HER alani liste/detay/bolum yanitlarinda hâlâ duruyor.

    Genisleme YALNIZ EKLEME idi: bu test alanlarin kaldirilmadigini ve yeniden
    ADLANDIRILMADIGINI (ad degisikligi de kirici bir degisikliktir) sabitler.
    """
    project = await project_factory("C13-4")
    token = await _login(client, db_session, user_factory)
    created = await client.post(
        f"/projects/{project.id}/sites",
        json={
            "name": "P2 Şantiyesi",
            "is_draft": True,
            "sections": [{"name": "Kaba İnşaat"}],
        },
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text

    listing = await client.get(f"/projects/{project.id}/sites", headers=_auth(token))
    detail = await client.get(f"/sites/{created.json()['id']}", headers=_auth(token))
    sections = await client.get(f"/sites/{created.json()['id']}/sections", headers=_auth(token))

    assert listing.status_code == detail.status_code == sections.status_code == 200
    listing_body = listing.json()
    assert set(P2_COUNT_FIELDS) <= set(listing_body["counts"])
    assert set(P2_TOTALS_FIELDS) <= set(listing_body["totals"])
    assert set(P2_CARD_FIELDS) <= set(listing_body["items"][0])
    assert set(P2_CARD_FIELDS) <= set(detail.json())
    assert set(P2_DETAIL_EXTRA_FIELDS) <= set(detail.json())
    assert set(P2_SECTION_FIELDS) <= set(detail.json()["sections"][0])
    assert set(P2_SECTION_FIELDS) <= set(sections.json()["items"][0])
    assert {"planned", "active", "completed"} <= set(sections.json()["counts"])


# --- §12.1/5: `duration_days` hicbir yerde yok ---


async def test_duration_days_absent_from_all_responses(
    client, db_session, user_factory, project_factory
):
    """Sure TUREVDIR (§3.6): saklanmaz, dondurulmez.

    Yanitin herhangi bir katmaninda gorunmesi, birinin turevi kolona yazdiginin
    isareti olurdu — o an itibariyla iki gercek kaynagi olurdu.
    """
    project = await project_factory("C13-5")
    token = await _login(client, db_session, user_factory)
    created = await client.post(
        f"/projects/{project.id}/sites",
        json={
            "name": "Süre Şantiyesi",
            "is_draft": True,
            "start_date": "2026-03-01",
            "end_date": "2027-03-01",
            "sections": [
                {"name": "Kaba İnşaat", "start_date": "2026-03-01", "end_date": "2026-09-01"}
            ],
        },
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    site_id = created.json()["id"]

    payloads = [
        created.json(),
        (await client.get(f"/projects/{project.id}/sites", headers=_auth(token))).json(),
        (await client.get(f"/sites/{site_id}", headers=_auth(token))).json(),
        (await client.get(f"/sites/{site_id}/sections", headers=_auth(token))).json(),
    ]

    for payload in payloads:
        for node in _walk(payload):
            assert "duration_days" not in node, node


# --- §12.4: atomikligin kalan varyantlari ---


async def test_post_last_section_invalid_writes_nothing(
    client, db_session, user_factory, project_factory
):
    """§12.4/18 varyanti: hata SON bolumde. Santiye zaten flush edilmis olur —
    yine de geri alinmali. Ilk bolumde patlayan varyantla ayni degildir."""
    project = await project_factory("C13-6")
    token = await _login(client, db_session, user_factory)
    sites_before = await _count(db_session, Site)
    sections_before = await _count(db_session, Section)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json={
            "name": "Atomik Şantiye",
            "is_draft": True,
            "sections": [
                {"name": "Kaba İnşaat"},
                {"name": "İnce İşler"},
                {"name": "Peyzaj", "start_date": "2027-01-01", "end_date": "2026-01-01"},
            ],
        },
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert "3. bölüm" in resp.text
    assert await _count(db_session, Site) == sites_before
    assert await _count(db_session, Section) == sections_before


async def test_post_unknown_section_manager_writes_nothing(
    client, db_session, user_factory, project_factory
):
    """§12.4/18-20 varyanti: hata DOGRULAMADA degil KULLANICI COZUMUNDE.

    Bu yol 422'yi baska bir noktadan uretir; santiye satiri o ana kadar
    yazilmis olabilir, geri alindigi ayrica kanitlanmalidir.
    """
    project = await project_factory("C13-7")
    token = await _login(client, db_session, user_factory)
    sites_before = await _count(db_session, Site)
    sections_before = await _count(db_session, Section)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json={
            "name": "Hayalet Şef Şantiyesi",
            "is_draft": True,
            "sections": [
                {"name": "Kaba İnşaat"},
                {"name": "İnce İşler", "manager_user_id": str(uuid.uuid4())},
            ],
        },
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert await _count(db_session, Site) == sites_before
    assert await _count(db_session, Section) == sections_before


async def test_failed_post_does_not_consume_a_site_code(
    client, db_session, user_factory, project_factory
):
    """Geri alinan POST sayaci ILERLETMEZ: bir sonraki basarili kayit ayni
    numarayi alir. Aksi hâlde her hatali form denemesi kod dizisinde delik acardi."""
    project = await project_factory("C13-8")
    token = await _login(client, db_session, user_factory)

    failed = await client.post(
        f"/projects/{project.id}/sites",
        json={
            "name": "Başarısız Şantiye",
            "is_draft": True,
            "sections": [
                {"name": "Kaba İnşaat", "start_date": "2027-01-01", "end_date": "2026-01-01"}
            ],
        },
        headers=_auth(token),
    )
    assert failed.status_code == 422, failed.text

    ok = await client.post(
        f"/projects/{project.id}/sites",
        json={"name": "Başarılı Şantiye", "is_draft": True},
        headers=_auth(token),
    )

    assert ok.status_code == 201, ok.text
    assert ok.json()["code"].endswith("-001"), ok.json()["code"]

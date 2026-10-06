"""P6 · T2 — `GET /sections/{section_id}` + genisleyen `PATCH /sections/{section_id}`.

## Neden ayri bir dosya

T2 bolum yuzeyine YENI BIR OKUMA UCU acar. Yeni okuma ucu = yeni IDOR yuzeyi:
bolum kimligi santiye ve proje kimliginden BAGIMSIZ gorunur, dolayisiyla
gorunurluk suzgeci `bolum -> santiye -> proje` zinciriyle yukari cozulmezse
baska bir projenin bolumu dogrudan okunabilir. Bu dosya o zinciri ve
`SectionUpdate`in yeni alanlarini (spec §3) BIRLIKTE sabitler.

## Sabitlenen kararlar

1. **Gorunmeyen bolum 404'tur** ve govdesi var olmayan bir UUID'ninkiyle
   BIREBIR AYNIDIR (`test_sites_idor.py` disiplininin GET ucuna tasinmasi).
2. **Yer tutucular KALIR** (spec §6): `progress_pct` / `boq_item_count` /
   `budget` / `worker_count` detay govdesinde de `pending_module` tasir.
   BLF-B1: eski elle `budget_amount` yanittan KALKTI; bedel yalniz turev `budget`.
3. **Yardimci sorumlu `manager_user_id` deseninin birebiridir**: FK verilirse ad
   anlik goruntusu govdedeki serbest metnin UZERINE yazilir ve IZINLI
   (`on_leave`) personel de atanabilir (kalici karar 5).
"""

import uuid
from decimal import Decimal

from sqlalchemy import delete, func, select

from app.core.access import AccessLevel
from app.modules.audit.messages import section_updated
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.roles.models import Role, RoleHiddenField
from app.modules.sites.models import Section, SectionStatus, Site
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz
from tests._section_types import seed_section_types
from tests._sites_sayfalari import sites_sayfalarini_kapat

SECTION_MISSING = "Bölüm bulunamadı"
USER_MISSING = "Seçilen kullanıcı bulunamadı"

WRITE_ROLE = "patron"  # sites=full
VIEW_ROLE = "site_chief"  # sites=view
NONE_ROLE = "procurement"  # izin testte acikca none'a cekilir

# Detay govdesinde bulunmasi gereken DORT ZARF ALANI. 🔴 Ad "PLACEHOLDER" kaldi
# ama artik hepsi BOS demek DEGILDIR (BLM-SAY): `worker_count` T4'te,
# `boq_item_count` + `budget` BLM-SAY'de BAGLANDI; yalniz `progress_pct` bos.
# Alan alan hangisinin bos oldugu asagida ayrica cakilidir.
PLACEHOLDER_FIELDS = ("progress_pct", "boq_item_count", "budget", "worker_count")
#: Bu dilimden sonra HÂLÂ bos kalan tek zarf (hakediş turevi, ayri is).
STILL_EMPTY_FIELDS = ("progress_pct",)

# T1'in actigi kolonlarin API karsiligi (spec §3 tablosu; BLF-B1: `budget_amount` kalkti).
NEW_SECTION_FIELDS = (
    "section_type",
    "description",
    "deputy_manager_user_id",
    "deputy_manager_name",
    "planned_worker_count",
    "is_draft",
)


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


async def _tree(session, project_factory, slug: str, **section_fields) -> tuple[Site, Section]:
    project = await project_factory(f"{slug}-{uuid.uuid4().hex[:6]}")
    site = Site(project_id=project.id, code=f"SNT-{uuid.uuid4().hex[:6]}", name="Gizli Şantiye")
    session.add(site)
    await session.flush()
    section = Section(site_id=site.id, name="Kaba İnşaat", **section_fields)
    session.add(section)
    await session.flush()
    return site, section


# --- GET: mutlu yol ---


async def test_get_section_returns_every_new_column(
    client, db_session, user_factory, project_factory
):
    """T1'in actigi YEDI alanin tamami detay govdesinde doner."""
    tipler = await seed_section_types(db_session)
    deputy = await user_factory(
        email=f"yrd-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        full_name="Yardımcı Sorumlu",
    )
    _, section = await _tree(
        db_session,
        project_factory,
        "P6T2-GET",
        code="BLM-01",
        status=SectionStatus.on_hold,
        section_type_id=tipler["structural"].id,
        description="Kaba inşaat kapsamı",
        deputy_manager_user_id=deputy.id,
        deputy_manager_name="Yardımcı Sorumlu",
        planned_worker_count=42,
        is_draft=True,
    )
    # Bu testler KOLON VARLIĞINI ölçer, kapsam maskesini değil; `site_chief`in
    # seed kapsamı `limited` olduğu için izin AÇIKÇA kurulur (varsayılan `all`).
    await modul_duzeyi_yaz(db_session, VIEW_ROLE, "sites", AccessLevel.view)
    token = await _login(client, db_session, user_factory, VIEW_ROLE, grant_all=True)

    resp = await client.get(f"/sections/{section.id}", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(section.id)
    assert body["code"] == "BLM-01"
    assert body["name"] == "Kaba İnşaat"
    assert body["status"] == "on_hold"
    assert body["section_type"] == {"id": str(tipler["structural"].id), "name": "Kaba İnşaat"}
    assert body["description"] == "Kaba inşaat kapsamı"
    assert body["deputy_manager_user_id"] == str(deputy.id)
    assert body["deputy_manager_name"] == "Yardımcı Sorumlu"
    assert body["planned_worker_count"] == 42
    assert "budget_amount" not in body
    assert body["is_draft"] is True


async def test_get_section_keeps_placeholder_metrics(
    client, db_session, user_factory, project_factory
):
    """Zarf ALANLARI durur, ama BLM-SAY sonrasi hepsi bos DEGILDIR.

    🔴 Bu testin eski iddiasi (`hepsinin pending_module'u dolu`) BLM-SAY'de
    CURUDU: dolu bir `MetricPlaceholder` `pending_module` TASIYAMAZ (P10 T3
    zarf kurali, pydantic duzeyinde bagli). Iddia zayiflatilmadi, DARALTILDI:
    zarf SEKLI hâlâ alan alan cakilir, bosluk ise yalniz gercekten bos kalan
    alanda aranir.

    BLF-B1: eski elle `budget_amount` kolonu DOLU olsa bile yanitta YOKTUR ve turev
    `budget`i etkilemez — tahsis yokken turev `0.00`dir.
    """
    _, section = await _tree(db_session, project_factory, "P6T2-PH", budget_amount=Decimal("10.00"))
    # Bu testler KOLON VARLIĞINI ölçer, kapsam maskesini değil; `site_chief`in
    # seed kapsamı `limited` olduğu için izin AÇIKÇA kurulur (varsayılan `all`).
    await modul_duzeyi_yaz(db_session, VIEW_ROLE, "sites", AccessLevel.view)
    # IZN-B4: bu test BÜTÇE DEĞERİNİ okur; seed rolün gizli kategorileri (maliyet_kar) maskelerdi.
    role_id = (await db_session.execute(select(Role.id).where(Role.key == VIEW_ROLE))).scalar_one()
    await db_session.execute(delete(RoleHiddenField).where(RoleHiddenField.role_id == role_id))
    await db_session.flush()
    token = await _login(client, db_session, user_factory, VIEW_ROLE, grant_all=True)

    body = (await client.get(f"/sections/{section.id}", headers=_auth(token))).json()

    for field in PLACEHOLDER_FIELDS:
        assert isinstance(body[field], dict), (field, body[field])
    for field in STILL_EMPTY_FIELDS:
        assert body[field]["available"] is False, field
        assert body[field]["pending_module"], field
    assert "budget_amount" not in body
    assert body["budget"] == {"available": True, "value": "0.00", "pending_module": None}


async def test_get_section_nullable_columns_default_to_null(
    client, db_session, user_factory, project_factory
):
    """Taslak destegi (kalici karar 4): T1 kolonlari `is_draft` DISINDA nullable."""
    _, section = await _tree(db_session, project_factory, "P6T2-NULL")
    # Bu testler KOLON VARLIĞINI ölçer, kapsam maskesini değil; `site_chief`in
    # seed kapsamı `limited` olduğu için izin AÇIKÇA kurulur (varsayılan `all`).
    await modul_duzeyi_yaz(db_session, VIEW_ROLE, "sites", AccessLevel.view)
    token = await _login(client, db_session, user_factory, VIEW_ROLE, grant_all=True)

    body = (await client.get(f"/sections/{section.id}", headers=_auth(token))).json()

    assert body["section_type"] is None
    assert body["description"] is None
    assert body["deputy_manager_user_id"] is None
    assert body["deputy_manager_name"] is None
    assert body["planned_worker_count"] is None
    assert body["is_draft"] is False


# --- GET: IDOR ---


async def test_get_invisible_section_and_unknown_uuid_are_indistinguishable(
    client, db_session, user_factory, project_factory
):
    """YENI OKUMA UCU = YENI IDOR YUZEYI. Gorunmeyen GERCEK bolum ile var olmayan
    UUID ayni durum kodunu VE ayni govdeyi doner."""
    _, section = await _tree(db_session, project_factory, "P6T2-IDOR")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=False)

    invisible = await client.get(f"/sections/{section.id}", headers=_auth(token))
    unknown = await client.get(f"/sections/{uuid.uuid4()}", headers=_auth(token))

    assert invisible.status_code == unknown.status_code == 404
    assert invisible.json() == unknown.json() == {"detail": SECTION_MISSING}
    assert str(section.id) not in invisible.text
    assert section.name not in invisible.text


async def test_get_section_without_permission_returns_403(
    client, db_session, user_factory, project_factory
):
    await modul_duzeyi_yaz(db_session, NONE_ROLE, "sites", AccessLevel.none)
    await sites_sayfalarini_kapat(db_session, NONE_ROLE)  # IZN-B5c: dar görme genişlemesi
    _, section = await _tree(db_session, project_factory, "P6T2-403")
    token = await _login(client, db_session, user_factory, NONE_ROLE, grant_all=True)

    resp = await client.get(f"/sections/{section.id}", headers=_auth(token))

    assert resp.status_code == 403, resp.text


# --- PATCH: yeni alanlar ---


async def test_patch_writes_every_new_field(client, db_session, user_factory, project_factory):
    tipler = await seed_section_types(db_session)
    _, section = await _tree(db_session, project_factory, "P6T2-PATCH")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={
            "section_type_id": str(tipler["mep"].id),
            "description": "Mekanik ve elektrik",
            "planned_worker_count": 17,
            "is_draft": True,
        },
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(section)
    assert section.section_type_id == tipler["mep"].id
    assert section.description == "Mekanik ve elektrik"
    assert section.planned_worker_count == 17
    assert section.is_draft is True


async def test_patch_can_set_on_hold_status(client, db_session, user_factory, project_factory):
    """`on_hold` P6'da eklendi (spec §4 / S1 onayi) — uctan uca kabul edilmeli."""
    _, section = await _tree(db_session, project_factory, "P6T2-HOLD")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    resp = await client.patch(
        f"/sections/{section.id}", json={"status": "on_hold"}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "on_hold"
    await db_session.refresh(section)
    assert section.status is SectionStatus.on_hold


async def test_patch_deputy_fk_overwrites_name_snapshot(
    client, db_session, user_factory, project_factory
):
    """`manager_name` deseninin birebiri: ad FK'nin TUREVIDIR, ikinci bir gercek
    kaynak degildir — govdedeki serbest metin yok sayilir."""
    deputy = await user_factory(
        email=f"yrd-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        full_name="Ayşe Yılmaz",
    )
    _, section = await _tree(db_session, project_factory, "P6T2-SNAP")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={"deputy_manager_user_id": str(deputy.id), "deputy_manager_name": "YANLIŞ AD"},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(section)
    assert section.deputy_manager_user_id == deputy.id
    assert section.deputy_manager_name == "Ayşe Yılmaz"


async def test_patch_deputy_accepts_on_leave_user(
    client, db_session, user_factory, project_factory
):
    """Kalici karar 5: IZINLI personel de atanabilir — izin GECICI bir durumdur,
    yillik izindeki yardimci sorumlu hâlâ o bolumun yardimci sorumlusudur."""
    deputy = await user_factory(
        email=f"izin-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        status="on_leave",
        full_name="İzindeki Sorumlu",
    )
    _, section = await _tree(db_session, project_factory, "P6T2-LEAVE")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={"deputy_manager_user_id": str(deputy.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(section)
    assert section.deputy_manager_name == "İzindeki Sorumlu"


async def test_patch_deputy_passive_user_returns_422(
    client, db_session, user_factory, project_factory
):
    """Pasif kullanici `manager_user_id` ile AYNI sekilde reddedilir (422, 404 degil:
    istenen kaynak BOLUMDUR, kullanici burada bir ALAN DEGERIDIR)."""
    deputy = await user_factory(
        email=f"pasif-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        status="passive",
    )
    _, section = await _tree(db_session, project_factory, "P6T2-PASSIVE")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={"deputy_manager_user_id": str(deputy.id), "description": "Yazılmamalı"},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": USER_MISSING}
    await db_session.refresh(section)
    assert section.deputy_manager_user_id is None
    assert section.description is None


async def test_patch_unknown_deputy_uuid_returns_422(
    client, db_session, user_factory, project_factory
):
    _, section = await _tree(db_session, project_factory, "P6T2-GHOST")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={"deputy_manager_user_id": str(uuid.uuid4())},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": USER_MISSING}


async def test_patch_negative_numbers_return_422(client, db_session, user_factory, project_factory):
    """Pydantic `ge=0` — kural DB CHECK'inden ONCE, alan bazli hatayla karsilanir."""
    _, section = await _tree(db_session, project_factory, "P6T2-NEG")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    workers = await client.patch(
        f"/sections/{section.id}", json={"planned_worker_count": -1}, headers=_auth(token)
    )

    assert workers.status_code == 422, workers.text
    await db_session.refresh(section)
    assert section.planned_worker_count is None


async def test_patch_new_fields_rejected_for_view_permission(
    client, db_session, user_factory, project_factory
):
    """Yeni alanlar yazma kapisini GEVSETMEZ: `sites:view` yine 403 alir."""
    await modul_duzeyi_yaz(db_session, VIEW_ROLE, "sites", AccessLevel.view)
    _, section = await _tree(db_session, project_factory, "P6T2-VIEW")
    # Bu testler KOLON VARLIĞINI ölçer, kapsam maskesini değil; `site_chief`in
    # seed kapsamı `limited` olduğu için izin AÇIKÇA kurulur (varsayılan `all`).
    await modul_duzeyi_yaz(db_session, VIEW_ROLE, "sites", AccessLevel.view)
    token = await _login(client, db_session, user_factory, VIEW_ROLE, grant_all=True)

    resp = await client.patch(
        f"/sections/{section.id}", json={"description": "Sızıntı"}, headers=_auth(token)
    )

    assert resp.status_code == 403, resp.text
    await db_session.refresh(section)
    assert section.description is None


async def test_patch_invisible_section_with_new_fields_returns_404(
    client, db_session, user_factory, project_factory
):
    _, section = await _tree(db_session, project_factory, "P6T2-PIDOR")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=False)

    invisible = await client.patch(
        f"/sections/{section.id}", json={"description": "Sızıntı"}, headers=_auth(token)
    )
    unknown = await client.patch(
        f"/sections/{uuid.uuid4()}", json={"description": "Sızıntı"}, headers=_auth(token)
    )

    assert invisible.status_code == unknown.status_code == 404
    assert invisible.json() == unknown.json() == {"detail": SECTION_MISSING}
    await db_session.refresh(section)
    assert section.description is None


# --- Denetim ---


async def test_patch_new_fields_write_single_section_updated_audit_row(
    client, db_session, user_factory, project_factory
):
    """Yeni alanlar MEVCUT denetim akisina girer: yeni `AuditAction` acilmaz,
    yeni metin uretilmez — bolum guncellemesi TEK satirdir."""
    tipler = await seed_section_types(db_session)
    site, section = await _tree(db_session, project_factory, "P6T2-AUDIT")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={"section_type_id": str(tipler["finishing"].id)},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    rows = list(
        (await db_session.execute(select(AuditLog).where(AuditLog.action == AuditAction.update)))
        .scalars()
        .all()
    )
    assert [row.detail for row in rows] == [section_updated(site.name, section.name)]


async def test_rejected_patch_writes_no_audit_row(
    client, db_session, user_factory, project_factory
):
    """Denetim GERCEKLESEN olayi kaydeder, denemeyi degil."""
    _, section = await _tree(db_session, project_factory, "P6T2-NOAUDIT")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)
    before = int(
        (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    )

    resp = await client.patch(
        f"/sections/{section.id}",
        json={"deputy_manager_user_id": str(uuid.uuid4())},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    after = int((await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one())
    assert after == before


# --- OpenAPI sozlesmesi ---


def test_openapi_exposes_section_detail_endpoint_and_new_fields():
    """Frontend sozlesmeyi semadan uretir: uc ya da alan duserse hata BACKEND
    testlerinde degil frontend derlemesinde ortaya cikardi."""
    from app.main import app

    schema = app.openapi()

    def resolve(node: dict) -> dict:
        if "$ref" in node:
            return schema["components"]["schemas"][node["$ref"].rsplit("/", 1)[1]]
        return node

    assert "get" in schema["paths"]["/sections/{section_id}"]
    detail = resolve(
        schema["paths"]["/sections/{section_id}"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
    )["properties"]
    update = resolve(
        schema["paths"]["/sections/{section_id}"]["patch"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
    )["properties"]

    for field in NEW_SECTION_FIELDS:
        assert field in detail, field
        # BLF-B1: yanitta `section_type` ({id,name}), istekte `section_type_id`.
        assert ("section_type_id" if field == "section_type" else field) in update, field
    assert "budget_amount" not in detail
    assert "budget_amount" not in update
    for field in PLACEHOLDER_FIELDS:
        assert field in detail, field
    # BOQ-bolum bagi ACILMAZ (kalici karar 1): atanacak is kalemleri govdeye girmez.
    assert "boq_item_ids" not in update
    assert "site_id" not in update

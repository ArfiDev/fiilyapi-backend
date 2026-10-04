import uuid
from decimal import Decimal

from sqlalchemy import select

from app.core.access import AccessLevel, Scope
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.projects.models import ProjectContract
from app.modules.roles.models import Module, Role, RolePermission
from app.modules.users.models import ProjectMember
from tests._legacy_permission_yardimcisi import sync_page_cells


async def _set_permission(
    session, role_key: str, module_key: str, level: AccessLevel, scope: Scope = Scope.all
) -> None:
    """Bir rolun modul iznini dogrudan ayarlar.

    Yetki kapisi testleri seed degerine BAGIMLI olmamali: matris degistiginde
    test sessizce anlamsizlasmasin diye ilgili hucre testte acikca kurulur.
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


async def _login(client, user_factory, role_key: str) -> str:
    await user_factory(email=f"{role_key}@t.co", password="parola1234", role_key=role_key)
    resp = await client.post(
        "/auth/login", json={"email": f"{role_key}@t.co", "password": "parola1234"}
    )
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_list_projects_unauthenticated(client):
    resp = await client.get("/projects")
    assert resp.status_code == 401


async def test_list_projects_forbidden_for_procurement(client, user_factory):
    """seed: projects satirinda procurement = none."""
    token = await _login(client, user_factory, "procurement")
    resp = await client.get("/projects", headers=_auth(token))
    assert resp.status_code == 403


async def test_system_admin_without_access_rows_sees_all(client, user_factory, project_factory):
    """KRITIK GECIS REGRESYONU (spec §5.4): Ayarlar'daki kullanici-proje erisim
    ekrani bu ucu tuketir; erisim satiri olmayan system_admin tum projeleri gormeli."""
    await project_factory("GK-A", name="Güneşkent A-Blok")
    await project_factory("OSB-1", name="Çelik OSB Fabrika")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get("/projects", headers=_auth(token))

    assert resp.status_code == 200
    assert [p["code"] for p in resp.json()["items"]] == ["GK-A", "OSB-1"]
    assert "password_hash" not in resp.text


async def test_patron_now_allowed_and_scoped(client, db_session, user_factory, project_factory):
    """Eski test patron'a 403 bekliyordu (user_management kapisi); artik projects=full."""
    granted = await project_factory("GK-A")
    await project_factory("OSB-1")
    user = await user_factory(email="patron@t.co", password="parola1234", role_key="patron")
    db_session.add(ProjectMember(user_id=user.id, project_id=granted.id, role_id=user.role_id))
    await db_session.flush()
    login = await client.post(
        "/auth/login", json={"email": "patron@t.co", "password": "parola1234"}
    )
    token = login.json()["access_token"]

    resp = await client.get("/projects", headers=_auth(token))

    assert resp.status_code == 200
    body = resp.json()
    assert [p["code"] for p in body["items"]] == ["GK-A"]
    assert body["counts"]["all"] == 1


async def test_list_filters_and_counts(client, user_factory, project_factory):
    await project_factory("T-1", project_type="taahhut", status="active")
    await project_factory("KY-1", project_type="kendi_yatirim", status="completed")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get("/projects?type=taahhut", headers=_auth(token))

    body = resp.json()
    assert [p["code"] for p in body["items"]] == ["T-1"]
    assert body["counts"] == {
        "all": 2,
        "taahhut": 1,
        "kendi_yatirim": 1,
        "kat_karsiligi": 0,
        "completed": 1,
        "draft": 0,
    }


async def test_get_project_not_found(client, user_factory):
    token = await _login(client, user_factory, "system_admin")
    resp = await client.get(f"/projects/{uuid.uuid4()}", headers=_auth(token))
    assert resp.status_code == 404


async def test_create_forbidden_for_view_level(client, user_factory):
    """site_chief projects=view tasir; POST admin ister."""
    token = await _login(client, user_factory, "site_chief")
    resp = await client.post(
        "/projects",
        json={"code": "X-1", "name": "X", "project_type": "taahhut"},
        headers=_auth(token),
    )
    assert resp.status_code == 403


async def test_create_requires_admin_not_full(client, db_session, user_factory):
    """Kullanici karari 2026-07-28: proje olusturma ADMIN isidir.

    `full` yetmez. Bu, olusturana otomatik UserProjectAccess yazmadan da
    tutarli kalmasini saglar: admin gorunurluk suzgecini zaten atlar (P1 spec
    §5.2), dolayisiyla yarattigi projeyi gorebilir. `full` seviyesine izin
    verilseydi, kapsamli bir kullanici goremedigi bir proje yaratirdi.
    """
    # is_draft: taahhüt zorunluluklarına takılmadan izin kapısını test etmek için (B4).
    body = {"code": "ADM-1", "name": "Admin Testi", "project_type": "taahhut", "is_draft": True}

    await _set_permission(db_session, "patron", "projects", AccessLevel.full)
    full_token = await _login(client, user_factory, "patron")
    forbidden = await client.post("/projects", json=body, headers=_auth(full_token))
    assert forbidden.status_code == 403

    admin_token = await _login(client, user_factory, "system_admin")
    allowed = await client.post("/projects", json=body, headers=_auth(admin_token))
    assert allowed.status_code == 201
    assert allowed.json()["code"] == "ADM-1"


async def test_create_kat_karsiligi_and_audit(client, db_session, user_factory):
    token = await _login(client, user_factory, "system_admin")

    resp = await client.post(
        "/projects",
        json={
            "code": "KK-1",
            "name": "Bahçelievler Konut",
            "project_type": "kat_karsiligi",
            "category": "Konut",
            "city": "Ankara",
            "land_share": {
                "landowner_name": "Yılmaz Ailesi",
                "our_share_pct": "55.00",
                "owner_share_pct": "45.00",
                "shareholders": [
                    {"name": "A. Yılmaz", "share_pct": "60.00"},
                    {"name": "B. Yılmaz", "share_pct": "40.00"},
                ],
            },
        },
        headers=_auth(token),
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["project_type"] == "kat_karsiligi"
    assert body["land_share"]["landowner_name"] == "Yılmaz Ailesi"
    assert body["land_share"]["land_cost"] == "0"
    assert body["land_share"]["shareholder_count"] == 2
    # DEGISTI (KK-TARAF): `pending_module` KORUNDU — dolu `CountPlaceholder`in
    # modul adini tasimasi `_worker_count` emsaliyle BILINCLIDIR, bu yuzden eski
    # iddia AYNEN kalir. Yanina eklenen iki satir bugunku gercegi olcer: `units`
    # modulu CANLI, yeni acilan projede henuz unite yok ve `0` GERCEK cevaptir —
    # bos yer tutucu degil. Eski hâlinde bu test `available=False`u da sessizce
    # geciriyordu.
    assert body["land_share"]["our_unit_count"]["pending_module"] == "units"
    assert body["land_share"]["our_unit_count"]["available"] is True
    assert body["land_share"]["our_unit_count"]["count"] == 0
    assert body["land_share"]["owner_unit_count"]["count"] == 0
    assert body["contracting"] is None
    assert body["investment"] is None

    audit_rows = (
        (await db_session.execute(select(AuditLog).where(AuditLog.action == AuditAction.create)))
        .scalars()
        .all()
    )
    assert any("Bahçelievler Konut" in row.detail for row in audit_rows)


async def test_create_type_mismatch_returns_422(client, user_factory):
    token = await _login(client, user_factory, "system_admin")
    resp = await client.post(
        "/projects",
        json={
            "code": "T-9",
            "name": "Yanlış",
            "project_type": "taahhut",
            "investment": {"sales_target": "1.00"},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 422


async def test_create_duplicate_code_returns_409(client, user_factory, project_factory):
    await project_factory("GK-A")
    token = await _login(client, user_factory, "system_admin")
    resp = await client.post(
        "/projects",
        json={"code": "GK-A", "name": "Kopya", "project_type": "taahhut", "is_draft": True},
        headers=_auth(token),
    )
    assert resp.status_code == 409


async def _login_with_all_access(client, db_session, user_factory, role_key: str) -> str:
    user = await user_factory(email=f"{role_key}@t.co", password="parola1234", role_key=role_key)
    user.all_projects = True
    await db_session.flush()
    resp = await client.post(
        "/auth/login", json={"email": f"{role_key}@t.co", "password": "parola1234"}
    )
    return resp.json()["access_token"]


async def test_patch_project_outside_access_is_404_and_changes_nothing(
    client, db_session, user_factory, project_factory
):
    """IDOR: GET 404 verirken PATCH gecirirse yetki suzgeci YARIM demektir.

    Yalnizca "GK-A" projesine erisimi olan bir patron, "OSB-1"i GET ile
    goremiyor; ayni kimlikle PATCH atarsa da goremiyor olmali. Aksi halde
    kullanici, listede hic gormedigi bir projenin adini/sozlesme bedelini
    yalnizca UUID'sini bilerek degistirebilir.
    """
    granted = await project_factory("GK-A")
    hidden = await project_factory("OSB-1", name="Dokunulmamis Ad")
    user = await user_factory(email="scoped-pm@t.co", password="parola1234", role_key="patron")
    db_session.add(ProjectMember(user_id=user.id, project_id=granted.id, role_id=user.role_id))
    await db_session.flush()
    login = await client.post(
        "/auth/login", json={"email": "scoped-pm@t.co", "password": "parola1234"}
    )
    token = login.json()["access_token"]

    assert (await client.get(f"/projects/{hidden.id}", headers=_auth(token))).status_code == 404

    resp = await client.patch(
        f"/projects/{hidden.id}", json={"name": "ELE GEÇİRİLDİ"}, headers=_auth(token)
    )

    assert resp.status_code == 404
    await db_session.refresh(hidden)
    assert hidden.name == "Dokunulmamis Ad"
    # Erisimi olan proje etkilenmemeli — duzeltme fazla genis olmamali.
    allowed = await client.patch(
        f"/projects/{granted.id}", json={"name": "Yeni Ad"}, headers=_auth(token)
    )
    assert allowed.status_code == 200


async def test_patch_updates_and_audits(client, db_session, user_factory, project_factory):
    project = await project_factory("T-1", name="Eski Ad")
    token = await _login_with_all_access(client, db_session, user_factory, "project_manager")

    resp = await client.patch(
        f"/projects/{project.id}", json={"name": "Yeni Ad"}, headers=_auth(token)
    )

    assert resp.status_code == 200
    assert resp.json()["name"] == "Yeni Ad"
    audit_rows = (
        (await db_session.execute(select(AuditLog).where(AuditLog.action == AuditAction.update)))
        .scalars()
        .all()
    )
    assert any("Yeni Ad" in row.detail for row in audit_rows)


async def test_patch_ignores_project_type(client, db_session, user_factory, project_factory):
    """ProjectUpdate'te alan yok — gonderilirse sessizce yok sayilir (extra alan)."""
    project = await project_factory("T-2", project_type="taahhut")
    token = await _login_with_all_access(client, db_session, user_factory, "patron")

    resp = await client.patch(
        f"/projects/{project.id}",
        json={"project_type": "kendi_yatirim", "name": "Ad"},
        headers=_auth(token),
    )

    assert resp.status_code == 200
    assert resp.json()["project_type"] == "taahhut"


async def test_sozlesme_no_patchi_otoriteyi_de_gunceller(
    client, db_session, user_factory, project_factory
):
    """`project_contracts.contract_no/amount` OTORİTEDİR (projects/models.py:199-200);

    `projects.contract_no/contract_amount` onun anlık görüntüsüdür. PATCH yalnız
    anlık görüntüyü yazarsa iki kaynak ayrışır: sözleşme listesi ve `GET
    /projects/{id}/contract` eskiyi basar, `?q=` süzgeci otoriteye baktığı için
    kullanıcının ekranda GÖRDÜĞÜ numarayla arama SONUÇ VERMEZ ve işveren
    hakediş yüzdesinin paydası (`contract.amount`) hiçbir uçtan düzeltilemez.
    """
    project = await project_factory(
        "SZL-OTO",
        name="Otorite Projesi",
        contract_no="SZL-2026-001",
        contract_amount="20000000.00",
    )
    db_session.add(
        ProjectContract(
            project_id=project.id,
            contract_no="SZL-2026-001",
            amount=Decimal("20000000.00"),
            advance_pct=Decimal("20"),
        )
    )
    await db_session.flush()
    token = await _login(client, user_factory, "system_admin")

    resp = await client.patch(
        f"/projects/{project.id}",
        json={"contract_no": "SZL-2026-009", "contract_amount": "25000000.00"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text

    detay = await client.get(f"/projects/{project.id}/contract", headers=_auth(token))
    assert detay.status_code == 200, detay.text
    assert detay.json()["contract_no"] == "SZL-2026-009"
    assert Decimal(detay.json()["amount"]) == Decimal("25000000.00")

    liste = await client.get(
        "/contracts", params={"type": "employer", "q": "SZL-2026-009"}, headers=_auth(token)
    )
    assert liste.status_code == 200, liste.text
    assert [item["id"] for item in liste.json()["items"]] == [str(project.id)]
    assert liste.json()["items"][0]["amount"] == "25000000.00"

    detay_proje = await client.get(f"/projects/{project.id}", headers=_auth(token))
    assert detay_proje.json()["contract_no"] == "SZL-2026-009"

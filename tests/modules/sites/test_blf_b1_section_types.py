"""BLF-B1.1 — sirket geneli bolum tipi listesi (`GET`/`POST /section-types`) ve bolum bagi.

Sabitlenen kararlar (BOLUM-FORMU-SPEC.md, F-b/F-c):

* Liste `(sort_order, id)` sirali; yeni tip `max + 1` ile sona eklenir.
* Tekillik normalize anahtardadir (`app.core.labels.normalize_label`): yazim farki
  ("ince isler" / "INCE ISLER" / bosluk / NFKC / sifir genislik) AYNI tiptir -> 409 ve
  mesaj MEVCUT tipin adini tasir.
* POST kapisi bolum OLUSTURMA ucuyla AYNI (`sites:full`); GET `sites:view`.
* Bolumde `section_type_id`; yanitta `{id, name}`; olmayan id 422; tip zorunlulugu
  (taslak disi) KALIR.
* Kullanilan tip DB'den silinemez (FK RESTRICT).
* Detay yolunda tip bolum SELECT'iyle (JOIN) gelir; ayri SELECT yok (`lazy="joined"`).
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.exc import IntegrityError

from app.core.access import AccessLevel
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.roles.models import Module, Role, RolePermission
from app.modules.sites.models import Section, SectionType, Site
from app.modules.users.models import UserProjectAccess
from tests._legacy_permission_yardimcisi import sync_page_cells
from tests._section_types import SEED_SECTION_TYPES, SEED_TYPE_IDS, seed_section_types
from tests.conftest import test_engine

WRITE_ROLE = "patron"  # sites=full  (bolum olusturabilen)
VIEW_ROLE = "site_chief"  # sites=view

TAKEN_PREFIX = "Bu bölüm tipi zaten var: "


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, session, user_factory, role_key: str) -> str:
    address = f"{role_key}-{uuid.uuid4().hex[:6]}@t.co"
    user = await user_factory(email=address, password="parola1234", role_key=role_key)
    session.add(UserProjectAccess(user_id=user.id, project_id=None, all_projects=True))
    await session.flush()
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


async def _set_sites_level(session, role_key: str, level: AccessLevel) -> None:
    role_id = (await session.execute(select(Role.id).where(Role.key == role_key))).scalar_one()
    module_id = (await session.execute(select(Module.id).where(Module.key == "sites"))).scalar_one()
    permission = (
        await session.execute(
            select(RolePermission).where(
                RolePermission.role_id == role_id, RolePermission.module_id == module_id
            )
        )
    ).scalar_one()
    permission.access_level = level
    await session.flush()
    await sync_page_cells(session, permission.role_id)


@pytest.fixture
async def tipler(db_session):
    return await seed_section_types(db_session)


# --- GET ---


async def test_get_lists_types_in_sort_order_with_id_and_name_only(
    client, db_session, user_factory, tipler
):
    await _set_sites_level(db_session, VIEW_ROLE, AccessLevel.view)
    token = await _login(client, db_session, user_factory, VIEW_ROLE)

    resp = await client.get("/section-types", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["name"] for row in body] == [
        name for name, _ in sorted(SEED_SECTION_TYPES.values(), key=lambda x: x[1])
    ]
    assert all(set(row) == {"id", "name"} for row in body)
    assert body[0]["id"] == str(SEED_TYPE_IDS["foundation_infra"])


async def test_get_requires_sites_view(client, db_session, user_factory, tipler):
    await _set_sites_level(db_session, "procurement", AccessLevel.none)
    token = await _login(client, db_session, user_factory, "procurement")

    resp = await client.get("/section-types", headers=_auth(token))

    assert resp.status_code == 403, resp.text


async def test_get_without_token_is_401(client):
    assert (await client.get("/section-types")).status_code == 401


# --- POST ---


async def test_post_creates_type_appended_last_and_audits(client, db_session, user_factory, tipler):
    token = await _login(client, db_session, user_factory, WRITE_ROLE)

    resp = await client.post(
        "/section-types", json={"name": "  Çevre Düzenleme  "}, headers=_auth(token)
    )

    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert set(created) == {"id", "name"}
    assert created["name"] == "Çevre Düzenleme"  # kirpildi
    row = await db_session.get(SectionType, uuid.UUID(created["id"]))
    assert row is not None
    assert row.sort_order == 8  # 7 tohum + 1
    listed = (await client.get("/section-types", headers=_auth(token))).json()
    assert listed[-1] == created
    audit = (
        (await db_session.execute(select(AuditLog).where(AuditLog.action == AuditAction.create)))
        .scalars()
        .all()
    )
    assert [a.detail for a in audit] == ["Bölüm tipi eklendi: Çevre Düzenleme"]


async def test_post_on_empty_table_starts_at_one(client, db_session, user_factory):
    token = await _login(client, db_session, user_factory, WRITE_ROLE)

    resp = await client.post("/section-types", json={"name": "İlk"}, headers=_auth(token))

    assert resp.status_code == 201, resp.text
    row = await db_session.get(SectionType, uuid.UUID(resp.json()["id"]))
    assert row.sort_order == 1


@pytest.mark.parametrize(
    "variant",
    [
        "ince işler",
        "İNCE İŞLER",
        "İnce  İşler",
        "  İnce İşler  ",
        "İnce İ\u200bşler",  # sifir genislik (Cf)
        "\uff49nce işler",  # NFKC: tam-genislik "ｉ"
    ],
)
async def test_post_same_name_other_spelling_is_409_naming_existing(
    client, db_session, user_factory, tipler, variant
):
    token = await _login(client, db_session, user_factory, WRITE_ROLE)

    resp = await client.post("/section-types", json={"name": variant}, headers=_auth(token))

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["detail"] == TAKEN_PREFIX + "İnce İşler"
    assert body["existing"]["name"] == "İnce İşler"
    count = (await client.get("/section-types", headers=_auth(token))).json()
    assert len(count) == 7


async def test_post_conflict_body_carries_existing_id_and_stored_name(
    client, db_session, user_factory, tipler
):
    """409 govdesi: `detail` mevcut metin, `existing` = DB'deki tipin id'si ve MEVCUT yazimi
    (gonderilen "ince işler" degil)."""
    token = await _login(client, db_session, user_factory, WRITE_ROLE)
    stored = (
        await db_session.execute(select(SectionType).where(SectionType.name == "İnce İşler"))
    ).scalar_one()

    resp = await client.post("/section-types", json={"name": "ince işler"}, headers=_auth(token))

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert set(body) == {"detail", "existing"}
    assert body["detail"] == TAKEN_PREFIX + "İnce İşler"
    assert set(body["existing"]) == {"id", "name"}
    assert body["existing"]["id"] == str(stored.id)
    assert body["existing"]["name"] == "İnce İşler"


async def test_post_race_integrity_error_is_same_409(
    client, db_session, user_factory, tipler, monkeypatch
):
    """Yarista on-kontrol bos doner, UQ yakalar; yanit yine alana ozel 409."""
    from app.modules.sites.service import section_types as svc

    real = svc.repository.get_section_type_by_key
    calls = {"n": 0}

    async def blind_first(session, name_key):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real(session, name_key)

    monkeypatch.setattr(svc.repository, "get_section_type_by_key", blind_first)
    token = await _login(client, db_session, user_factory, WRITE_ROLE)

    resp = await client.post("/section-types", json={"name": "peyzaj"}, headers=_auth(token))

    assert resp.status_code == 409, resp.text
    assert resp.json() == {
        "detail": TAKEN_PREFIX + "Peyzaj",
        "existing": {"id": str(tipler["landscape"].id), "name": "Peyzaj"},
    }
    assert calls["n"] == 2
    listed = (await client.get("/section-types", headers=_auth(token))).json()
    assert len(listed) == 7  # oturum savepoint sonrasi saglam, tekrar yazilmadi


async def test_post_created_201_body_has_no_existing_key(client, db_session, user_factory, tipler):
    token = await _login(client, db_session, user_factory, WRITE_ROLE)

    resp = await client.post("/section-types", json={"name": "Yeni Tip"}, headers=_auth(token))

    assert resp.status_code == 201, resp.text
    assert set(resp.json()) == {"id", "name"}
    assert "existing" not in resp.json()


def test_openapi_post_409_references_conflict_schema_with_existing():
    from app.main import app

    spec = app.openapi()
    response_409 = spec["paths"]["/section-types"]["post"]["responses"]["409"]
    ref = response_409["content"]["application/json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/SectionTypeConflict"
    conflict = spec["components"]["schemas"]["SectionTypeConflict"]
    assert {"detail", "existing"} <= set(conflict["properties"])
    assert conflict["properties"]["existing"]["$ref"] == "#/components/schemas/SectionTypeRead"
    assert {"detail", "existing"} <= set(conflict["required"])


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
async def test_post_blank_name_is_422(client, db_session, user_factory, name):
    token = await _login(client, db_session, user_factory, WRITE_ROLE)

    resp = await client.post("/section-types", json={"name": name}, headers=_auth(token))

    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("name", ["\u200b", "\u2060\ufeff", "\u200b\u200b\u200b"])
async def test_post_invisible_only_name_is_422_and_no_row(client, db_session, user_factory, name):
    """Yalniz gorunmez karakterden olusan ad `strip`/`btrim`i gecer ama `name_key` bos kalir."""
    token = await _login(client, db_session, user_factory, WRITE_ROLE)

    resp = await client.post("/section-types", json={"name": name}, headers=_auth(token))

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "Bölüm tipi adı boş olamaz"
    count = (await db_session.execute(select(SectionType))).scalars().all()
    assert count == []


async def test_post_name_length_limit_100(client, db_session, user_factory):
    token = await _login(client, db_session, user_factory, WRITE_ROLE)

    ok = await client.post("/section-types", json={"name": "a" * 100}, headers=_auth(token))
    too_long = await client.post("/section-types", json={"name": "b" * 101}, headers=_auth(token))

    assert ok.status_code == 201, ok.text
    assert too_long.status_code == 422, too_long.text


async def test_post_name_whose_key_overflows_column_is_422_with_clear_message(
    client, db_session, user_factory
):
    """NFKC uzatir (`㍿` -> `株式会社`, `ﷺ` 18 karakter): anahtar 400'u asarsa ACIK Turkce hata."""
    token = await _login(client, db_session, user_factory, WRITE_ROLE)

    resp = await client.post("/section-types", json={"name": "ﷺ" * 100}, headers=_auth(token))

    assert resp.status_code == 422, resp.text
    assert "normalize edildikten sonra çok uzun" in resp.json()["detail"]


async def test_post_gate_view_role_is_403_and_full_role_is_201(
    client, db_session, user_factory, tipler
):
    """`sites:view` (kapsam `all`: kapsam yazma kapisi DEGIL, yalniz seviye kapisi olculur) 403;
    `sites:full` ve ustu 201."""
    await _set_sites_level(db_session, WRITE_ROLE, AccessLevel.view)
    view_token = await _login(client, db_session, user_factory, WRITE_ROLE)
    full_token = await _login(client, db_session, user_factory, "system_admin")

    denied = await client.post("/section-types", json={"name": "Yeni A"}, headers=_auth(view_token))
    allowed = await client.post(
        "/section-types", json={"name": "Yeni B"}, headers=_auth(full_token)
    )
    still_reads = await client.get("/section-types", headers=_auth(view_token))

    assert denied.status_code == 403, denied.text
    assert allowed.status_code == 201, allowed.text
    assert still_reads.status_code == 200, still_reads.text  # GET icin view yeter
    names = [r["name"] for r in still_reads.json()]
    assert "Yeni A" not in names
    assert "Yeni B" in names


async def test_post_gate_is_same_as_section_create_gate(client, db_session, user_factory, tipler):
    """Bolum olusturabilen rol = tip ekleyebilen rol: sites=view'a inen rol ikisini de kaybeder."""
    await _set_sites_level(db_session, WRITE_ROLE, AccessLevel.view)
    token = await _login(client, db_session, user_factory, WRITE_ROLE)

    type_resp = await client.post("/section-types", json={"name": "X"}, headers=_auth(token))
    section_resp = await client.post(
        f"/sites/{uuid.uuid4()}/sections", json={"name": "B"}, headers=_auth(token)
    )

    assert type_resp.status_code == section_resp.status_code == 403


# --- Bolum <-> tip ---


async def _site(session, project_factory, slug: str) -> Site:
    project = await project_factory(f"{slug}-{uuid.uuid4().hex[:6]}")
    site = Site(project_id=project.id, code=f"SNT-{uuid.uuid4().hex[:6]}", name="Şantiye")
    session.add(site)
    await session.flush()
    return site


_PUBLISHED = {
    "name": "Kat 1-5",
    "manager_name": "Ali Veli",
    "start_date": "2026-10-01",
    "end_date": "2027-03-31",
}


async def test_section_create_and_patch_with_type_id_returns_id_name(
    client, db_session, user_factory, project_factory, tipler
):
    site = await _site(db_session, project_factory, "BLF-TIP")
    token = await _login(client, db_session, user_factory, WRITE_ROLE)

    created = await client.post(
        f"/sites/{site.id}/sections",
        json={**_PUBLISHED, "section_type_id": str(tipler["mep"].id)},
        headers=_auth(token),
    )

    assert created.status_code == 201, created.text
    assert created.json()["section_type"] == {
        "id": str(tipler["mep"].id),
        "name": "Mekanik / Elektrik",
    }
    assert "section_type_id" not in created.json()
    patched = await client.patch(
        f"/sections/{created.json()['id']}",
        json={"section_type_id": str(tipler["landscape"].id)},
        headers=_auth(token),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["section_type"] == {"id": str(tipler["landscape"].id), "name": "Peyzaj"}
    detail = await client.get(f"/sections/{created.json()['id']}", headers=_auth(token))
    assert detail.json()["section_type"]["name"] == "Peyzaj"


async def test_section_with_unknown_type_id_is_422_on_post_and_patch(
    client, db_session, user_factory, project_factory, tipler
):
    site = await _site(db_session, project_factory, "BLF-YOK")
    token = await _login(client, db_session, user_factory, WRITE_ROLE)
    ghost = str(uuid.uuid4())

    post = await client.post(
        f"/sites/{site.id}/sections",
        json={**_PUBLISHED, "section_type_id": ghost},
        headers=_auth(token),
    )
    ok = await client.post(
        f"/sites/{site.id}/sections",
        json={**_PUBLISHED, "name": "Var", "section_type_id": str(tipler["mep"].id)},
        headers=_auth(token),
    )
    patch = await client.patch(
        f"/sections/{ok.json()['id']}", json={"section_type_id": ghost}, headers=_auth(token)
    )

    assert post.status_code == 422, post.text
    assert post.json() == {"detail": "Bölüm tipi bulunamadı"}
    assert patch.status_code == 422, patch.text
    assert patch.json() == {"detail": "Bölüm tipi bulunamadı"}
    stored = await db_session.get(Section, uuid.UUID(ok.json()["id"]))
    await db_session.refresh(stored)
    assert stored.section_type_id == tipler["mep"].id  # reddedilen PATCH degistirmedi


async def test_published_section_without_type_is_422_but_draft_passes(
    client, db_session, user_factory, project_factory, tipler
):
    site = await _site(db_session, project_factory, "BLF-ZORUNLU")
    token = await _login(client, db_session, user_factory, WRITE_ROLE)

    published = await client.post(
        f"/sites/{site.id}/sections", json=_PUBLISHED, headers=_auth(token)
    )
    draft = await client.post(
        f"/sites/{site.id}/sections",
        json={"name": "Taslak", "is_draft": True},
        headers=_auth(token),
    )

    assert published.status_code == 422, published.text
    assert published.json() == {"detail": "Bölüm tipi seçiniz."}
    assert draft.status_code == 201, draft.text
    assert draft.json()["section_type"] is None


async def _stored_type_id(session, section_id: str):
    row = await session.get(Section, uuid.UUID(section_id))
    await session.refresh(row)
    return row.section_type_id


async def test_patch_null_type_on_published_is_422_and_unchanged(
    client, db_session, user_factory, project_factory, tipler
):
    site = await _site(db_session, project_factory, "BLF-NULL-YAYIN")
    token = await _login(client, db_session, user_factory, WRITE_ROLE)
    created = await client.post(
        f"/sites/{site.id}/sections",
        json={**_PUBLISHED, "section_type_id": str(tipler["mep"].id)},
        headers=_auth(token),
    )
    section_id = created.json()["id"]

    resp = await client.patch(
        f"/sections/{section_id}", json={"section_type_id": None}, headers=_auth(token)
    )

    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": "Bölüm tipi seçiniz."}
    assert await _stored_type_id(db_session, section_id) == tipler["mep"].id


async def test_patch_null_type_on_draft_is_200(
    client, db_session, user_factory, project_factory, tipler
):
    site = await _site(db_session, project_factory, "BLF-NULL-TASLAK")
    token = await _login(client, db_session, user_factory, WRITE_ROLE)
    created = await client.post(
        f"/sites/{site.id}/sections",
        json={"name": "Taslak", "is_draft": True, "section_type_id": str(tipler["mep"].id)},
        headers=_auth(token),
    )

    resp = await client.patch(
        f"/sections/{created.json()['id']}", json={"section_type_id": None}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["section_type"] is None


async def test_patch_other_type_on_published_is_200(
    client, db_session, user_factory, project_factory, tipler
):
    site = await _site(db_session, project_factory, "BLF-DEGIS-YAYIN")
    token = await _login(client, db_session, user_factory, WRITE_ROLE)
    created = await client.post(
        f"/sites/{site.id}/sections",
        json={**_PUBLISHED, "section_type_id": str(tipler["mep"].id)},
        headers=_auth(token),
    )

    resp = await client.patch(
        f"/sections/{created.json()['id']}",
        json={"section_type_id": str(tipler["landscape"].id)},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["section_type"]["name"] == "Peyzaj"


async def test_patch_draft_true_with_null_type_on_published_is_200(
    client, db_session, user_factory, project_factory, tipler
):
    site = await _site(db_session, project_factory, "BLF-TASLAGA-CEK")
    token = await _login(client, db_session, user_factory, WRITE_ROLE)
    created = await client.post(
        f"/sites/{site.id}/sections",
        json={**_PUBLISHED, "section_type_id": str(tipler["mep"].id)},
        headers=_auth(token),
    )

    resp = await client.patch(
        f"/sections/{created.json()['id']}",
        json={"is_draft": True, "section_type_id": None},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["section_type"] is None


async def test_used_type_cannot_be_deleted_fk_restrict(db_session, project_factory, tipler):
    site = await _site(db_session, project_factory, "BLF-RESTRICT")
    db_session.add(Section(site_id=site.id, name="B", section_type_id=tipler["finishing"].id))
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                delete(SectionType).where(SectionType.id == tipler["finishing"].id)
            )


def test_section_type_fk_is_restrict_in_model():
    fk = next(iter(Section.__table__.c.section_type_id.foreign_keys))
    assert fk.ondelete == "RESTRICT"
    assert fk.column.table.name == "section_types"


async def test_name_key_is_derived_in_app_not_typed(db_session):
    row = SectionType(name="  İnce  İŞLER ")
    db_session.add(row)
    await db_session.flush()
    assert row.name_key == "ince işler"
    row.name = "PEYZAJ"
    assert row.name_key == "peyzaj"


# --- N+1 ---


@contextmanager
def _count() -> Iterator[list[str]]:
    statements: list[str] = []

    def hook(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", hook)
    try:
        yield statements
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", hook)


async def test_section_detail_loads_type_in_same_select_without_extra_query(
    client, db_session, user_factory, project_factory, tipler
):
    """`lazy="joined"` sozlesmesi: bolum detayi tipi bolum SELECT'inin `LEFT OUTER JOIN
    section_types`i ile getirir; `section_types` icin AYRI SELECT atilmaz. Esas gerekce N+1
    degil, async ortamda senkron presenter'in tembel yuklemeye dusmemesidir
    (`MissingGreenlet`). N+1 yuzeyi yalniz detayda ve tek satirdir; bolum LISTESI yaniti
    `section_type` tasimaz, listede tipe dokunulmaz (bu yuzden liste sorgu sayisi tipten
    bagimsizdir ve ayri iddia gerektirmez)."""
    site = await _site(db_session, project_factory, "BLF-N1")
    token = await _login(client, db_session, user_factory, WRITE_ROLE)
    section = Section(site_id=site.id, name="S0", section_type_id=tipler["mep"].id)
    db_session.add(section)
    await db_session.flush()
    section_id = section.id
    db_session.expunge_all()  # kimlik haritasi bos: tip gercekten sorguyla okunur

    with _count() as stmts:
        resp = await client.get(f"/sections/{section_id}", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    assert resp.json()["section_type"]["name"] == "Mekanik / Elektrik"
    section_selects = [
        q for q in stmts if "FROM sections" in q and "LEFT OUTER JOIN section_types" in q
    ]
    assert section_selects, stmts
    separate = [q for q in stmts if "FROM section_types" in q and "FROM sections" not in q]
    assert separate == [], separate

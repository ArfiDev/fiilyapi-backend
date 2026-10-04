"""P6 · T5 — `PATCH /sections/{id}` yayina gecis dogrulamasi + kod cakismasi 409.

## Neden ayri bir dosya

T5 iki FINAL REVIEW bulgusunu kapatir; ikisi de `update_section` ile
`update_site` arasindaki DESEN ASIMETRISIDIR. Bu dosya bolumun santiyeyle AYNI
iki kurala uydugunu sabitler:

1. **PATCH GEVSEK, YAYIN SIKI** (`test_sites_update.py` §`is_draft` bloguyla
   birebir): T3'un zorunluluklari yalniz POST'ta baglayici kalirsa etkisizdir —
   `is_draft: true` ile eksik bolum acip `PATCH {"is_draft": false}` gondermek
   hepsini atlatir. Gecis basarisizsa satir TASLAK KALIR ve denetim metni
   `section_published` olur.
2. **Kod cakismasi ON KONTROLU** (karar 2026-07-30): `code` degisiminde alanina
   ozel Turkce mesajla 409 doner, genel "Veri bütünlüğü hatası" degil.
   `exclude_section_id` sarttir — kendi kodunu yeniden gondermek cakisma
   DEGILDIR. Kismi indeks `uq_sections_site_code` YARIS DURUMU emniyet agi
   olarak KALIR.
"""

import uuid
from datetime import date

from sqlalchemy import func, select

from app.modules.audit.messages import section_published, section_updated
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.sites.guards import (
    DUPLICATE_SECTION_CODE,
    SECTION_TYPE_REQUIRED,
)
from app.modules.sites.models import Section, Site
from tests._section_types import SEED_TYPE_IDS, seed_section_types

WRITE_ROLE = "patron"  # sites=full

# Yayin icin gereken DORT alanin tamami (spec §5 `*` isaretleri; BLF-B1: bedel kalkti).
PUBLISH_READY = {
    "section_type_id": SEED_TYPE_IDS["mep"],
    "manager_name": "Ali Veli",
    "start_date": date(2026, 1, 1),
    "end_date": date(2026, 6, 1),
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, session, user_factory) -> str:
    address = f"{WRITE_ROLE}-{uuid.uuid4().hex[:6]}@t.co"
    user = await user_factory(email=address, password="parola1234", role_key=WRITE_ROLE)
    user.all_projects = True
    await session.flush()
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


async def _site(session, project_factory, slug: str) -> Site:
    project = await project_factory(f"{slug}-{uuid.uuid4().hex[:6]}")
    site = Site(project_id=project.id, code=f"SNT-{uuid.uuid4().hex[:6]}", name="Şantiye")
    session.add(site)
    # BLF-B1: `create_all` migration tohumu getirmez — 7 tip burada eklenir.
    await seed_section_types(session)
    await session.flush()
    return site


async def _section(session, site: Site, **fields) -> Section:
    fields.setdefault("code", f"BLM-{uuid.uuid4().hex[:6]}")
    section = Section(site_id=site.id, name="Kaba İnşaat", **fields)
    session.add(section)
    await session.flush()
    return section


async def _reload(session, section: Section) -> Section:
    """Yaniti degil DB'yi okur: "taslak kaldi" iddiasinin tek gecerli kaniti.

    `refresh` kullanilir; `expire` + `select` async oturumda `MissingGreenlet`
    atar (`test_sites_update.py:61` ile ayni gerekce).
    """
    await session.refresh(section)
    return section


# --- Bulgu 1: taslak -> yayin gecisi ---


async def test_publish_with_missing_fields_returns_422_and_stays_draft(
    client, db_session, user_factory, project_factory
):
    """T3'un zorunluluklari PATCH'ten de ATLATILAMAZ; satir TASLAK KALIR."""
    site = await _site(db_session, project_factory, "P6T5-PUB422")
    section = await _section(db_session, site, is_draft=True)
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}", json={"is_draft": False}, headers=_auth(token)
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == SECTION_TYPE_REQUIRED
    assert (await _reload(db_session, section)).is_draft is True


async def test_publish_with_complete_row_returns_200_and_publish_audit_text(
    client, db_session, user_factory, project_factory
):
    """Gecis DUZ guncellemeden ayirt edilir: denetim metni `section_published`."""
    site = await _site(db_session, project_factory, "P6T5-PUBOK")
    section = await _section(db_session, site, is_draft=True, **PUBLISH_READY)
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}", json={"is_draft": False}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    assert (await _reload(db_session, section)).is_draft is False
    rows = list(
        (await db_session.execute(select(AuditLog).where(AuditLog.action == AuditAction.update)))
        .scalars()
        .all()
    )
    assert [row.detail for row in rows] == [section_published(site.name, section.name)]
    assert section_published("Ş", "B") == "Bölüm taslaktan yayına alındı: Ş · B"


async def test_publish_merges_existing_row_with_patch(
    client, db_session, user_factory, project_factory
):
    """Eksik alani AYNI istekte gonderen kullanici haksiz yere reddedilmez."""
    site = await _site(db_session, project_factory, "P6T5-MERGE")
    section = await _section(db_session, site, is_draft=True, section_type_id=SEED_TYPE_IDS["mep"])
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={
            "is_draft": False,
            "manager_name": "Ali Veli",
            "start_date": "2026-01-01",
            "end_date": "2026-06-01",
        },
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    row = await _reload(db_session, section)
    assert row.is_draft is False


async def test_publish_missing_only_type_returns_its_own_message(
    client, db_session, user_factory, project_factory
):
    """Birlesik kayit gercekten OKUNUR: diger alanlar satirda dururken yalniz eksik
    olan alanin mesaji doner. (BLF-B1: eski `SECTION_BUDGET_REQUIRED` dali KALKTI.)"""
    site = await _site(db_session, project_factory, "P6T5-TYPE")
    fields = {**PUBLISH_READY, "section_type_id": None}
    section = await _section(db_session, site, is_draft=True, **fields)
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}", json={"is_draft": False}, headers=_auth(token)
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == SECTION_TYPE_REQUIRED


async def test_normal_patch_on_published_section_stays_relaxed(
    client, db_session, user_factory, project_factory
):
    """REGRESYON BEKCISI: canlidaki eksik alanli eski bolumler duzenlenebilir
    kalmalidir — yalnizca adi degistiren kullanici zorunluluk duvarina carpmaz."""
    site = await _site(db_session, project_factory, "P6T5-RELAX")
    section = await _section(db_session, site, is_draft=False)
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}", json={"name": "Yeni Ad"}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    row = await _reload(db_session, section)
    assert row.name == "Yeni Ad"
    assert row.section_type is None
    rows = list(
        (await db_session.execute(select(AuditLog).where(AuditLog.action == AuditAction.update)))
        .scalars()
        .all()
    )
    assert [r.detail for r in rows] == [section_updated(site.name, "Yeni Ad")]


async def test_draft_false_to_false_is_not_a_publish(
    client, db_session, user_factory, project_factory
):
    """`false -> false` bir GECIS DEGILDIR ve zorunluluklari tetiklemez."""
    site = await _site(db_session, project_factory, "P6T5-FF")
    section = await _section(db_session, site, is_draft=False)
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}", json={"is_draft": False}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    assert (await _reload(db_session, section)).is_draft is False


async def test_draft_true_to_true_is_not_a_publish(
    client, db_session, user_factory, project_factory
):
    """Yarim taslak taslak olarak KAYDEDILEBILIR kalir."""
    site = await _site(db_session, project_factory, "P6T5-TT")
    section = await _section(db_session, site, is_draft=True)
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={"is_draft": True, "name": "Yarım Taslak"},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    assert (await _reload(db_session, section)).is_draft is True


async def test_rejected_publish_writes_no_audit_row(
    client, db_session, user_factory, project_factory
):
    """Denetim GERCEKLESEN olayi kaydeder, denemeyi degil."""
    site = await _site(db_session, project_factory, "P6T5-NOAUDIT")
    section = await _section(db_session, site, is_draft=True)
    token = await _login(client, db_session, user_factory)
    before = int(
        (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    )

    resp = await client.patch(
        f"/sections/{section.id}", json={"is_draft": False}, headers=_auth(token)
    )

    assert resp.status_code == 422, resp.text
    after = int((await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one())
    assert after == before


async def test_patch_consistency_rule_runs_even_for_draft(
    client, db_session, user_factory, project_factory
):
    """Tutarlilik HER ZAMAN: ters tarih araligi taslakta da 422'dir."""
    site = await _site(db_session, project_factory, "P6T5-CONS")
    section = await _section(db_session, site, is_draft=True, start_date=date(2026, 5, 1))
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}", json={"end_date": "2026-01-01"}, headers=_auth(token)
    )

    assert resp.status_code == 422, resp.text


# --- Bulgu 2: PATCH kod cakismasi ---


async def test_patch_duplicate_code_returns_409_with_field_message(
    client, db_session, user_factory, project_factory
):
    """Genel "Veri bütünlüğü hatası" kullaniciya HANGI alanin sorunlu oldugunu
    soylemez; on-kontrol alanina ozel mesaji verir."""
    site = await _site(db_session, project_factory, "P6T5-DUP")
    await _section(db_session, site, code="BLM-001")
    target = await _section(db_session, site, code="BLM-002")
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sections/{target.id}", json={"code": "BLM-001"}, headers=_auth(token)
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == DUPLICATE_SECTION_CODE
    assert (await _reload(db_session, target)).code == "BLM-002"


async def test_patch_same_code_on_itself_is_not_a_conflict(
    client, db_session, user_factory, project_factory
):
    """`exclude_section_id` olmasaydi formun TUM alanlarini gonderen her PATCH
    409 verirdi."""
    site = await _site(db_session, project_factory, "P6T5-SELF")
    section = await _section(db_session, site, code="BLM-003")
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={"code": "BLM-003", "name": "Yeni Ad"},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    row = await _reload(db_session, section)
    assert row.code == "BLM-003"
    assert row.name == "Yeni Ad"


async def test_patch_code_conflict_is_scoped_to_the_site(
    client, db_session, user_factory, project_factory
):
    """Benzersizlik `(site_id, code)` ciftindedir: baska santiyedeki ayni kod
    cakisma DEGILDIR."""
    site = await _site(db_session, project_factory, "P6T5-SCOPE")
    other = await _site(db_session, project_factory, "P6T5-SCOPE2")
    await _section(db_session, other, code="BLM-004")
    section = await _section(db_session, site, code="BLM-005")
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}", json={"code": "BLM-004"}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    assert (await _reload(db_session, section)).code == "BLM-004"


async def test_patch_code_null_skips_the_conflict_check(
    client, db_session, user_factory, project_factory
):
    """Kismi indeks yalniz `code IS NOT NULL` icin kosar; acikca `null`'lamak
    cakisma kontrolunu TETIKLEMEZ."""
    site = await _site(db_session, project_factory, "P6T5-NULL")
    await _section(db_session, site, code=None)
    section = await _section(db_session, site, code="BLM-006")
    token = await _login(client, db_session, user_factory)

    resp = await client.patch(f"/sections/{section.id}", json={"code": None}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    assert (await _reload(db_session, section)).code is None

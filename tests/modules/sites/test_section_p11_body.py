"""P11 · T2 — bolum govdesinin IKI additive alani: bagimlilik + kilometre taslari.

## Neden ayri bir dosya

T1 kolonu ve tabloyu acti (DB seviyesinde `SET NULL`/`CASCADE` orada olculdu);
burasi UC uzerinden dogrulanan davranistir. Iki yeni kural sinifi dogar ve
ikisi de mevcut POST/PATCH testlerinin konusu degildir:

* **bagimlilik** — ayni santiye · self/dongu reddi (zincir yuruyerek),
* **kilometre tasi listesi** — KIMLIK KORUYAN birlestirme (P9 `ShareholderInput`
  emsali), ayri bir CRUD ucu ACILMADAN.

## Sabitlenen kararlar (spec §3, §5 — kullanici onayi 2026-08-11)

1. **Tarih kisiti ZORLANMAZ.** Oncul bitmeden baslayan bolum 422 ALMAZ; bag
   YALNIZ BILGIDIR (`Form - Bolum Ekle` 117 "Gantt'ta bağlantı çizgisi").
   Testi asagida acikca vardir — ileride "mantikli gorunen" bir tarih kurali
   eklenirse KIRILIR, ki amac budur.
2. **Ayni SANTIYE sarti.** Baska santiyedeki bolum ile VAR OLMAYAN id AYNI 422
   metnini alir: ikisi ayirt edilebilseydi, elinde UUID olan kullanici baska bir
   santiyede o kaydin var oldugunu ogrenirdi (IDOR ile tutarli davranis).
3. **Dongu reddi zinciri YURUYEREK bulunur** — self (1'li), 2'li ve 3'lu zincir
   ayri ayri olculur; ziyaret edilen id kumesi sonsuz donguyu engeller.
4. **Kimlik korunumu**: id'li satirin `id`si guncellemeden SONRA AYNIDIR;
   gonderilmeyen mevcut satir DUSER; id'siz satir YENIDIR. Bilinmeyen id ve
   BASKA bolume ait id 422'dir (sessizce yeni satira DONMEZ).
5. **Alan gonderilmezse dokunulmaz** (PATCH semantigi) — bos liste gondermek ile
   alani hic gondermemek AYNI SEY DEGILDIR.
6. **Denetim**: yeni `AuditAction` ACILMAZ; milestone/bagimlilik degisikligi
   mevcut TEK `section_updated` satirini yazar.
"""

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.modules.audit.messages import section_updated
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.sites.models import Section, SectionMilestone, Site
from tests._section_types import SEED_TYPE_IDS, seed_section_types
from tests._silme_yardimci import sil_aile

SECTION_MISSING = "Bölüm bulunamadı"

# Yalniz sema/uygulama tarafinda kurulan metinler burada BIREBIR beklenir.
DEPENDS_NOT_IN_SITE = "Öncül bölüm aynı şantiyede bulunamadı"
DEPENDS_SELF = "Bölüm kendisine bağımlı olamaz"
DEPENDS_CYCLE = "Bölüm bağımlılıkları döngü oluşturamaz"
MILESTONE_UNKNOWN = (
    "Gönderilen kilometre taşı bu bölümde bulunamadı. "
    "Sayfayı yenileyip güncel listeyle tekrar deneyin."
)
MILESTONE_DUPLICATE_IN_PAYLOAD = "Aynı kilometre taşı listede birden çok kez var"

ADMIN_ROLE = "system_admin"  # sites=admin -> view/full/delete kapilarinin hepsi

PUBLISHED_PAYLOAD = {
    "name": "Kat 11–14 Kaba İnşaat",
    "section_type_id": str(SEED_TYPE_IDS["structural"]),
    "manager_name": "Sercan Öztürk",
    "start_date": "2026-10-01",
    "end_date": "2027-03-31",
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _published(**overrides) -> dict:
    return {**PUBLISHED_PAYLOAD, **overrides}


async def _login(client, user_factory) -> str:
    address = f"p11-{uuid.uuid4().hex[:8]}@t.co"
    await user_factory(email=address, password="parola1234", role_key=ADMIN_ROLE)
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


async def _site(session, project_factory, slug: str) -> Site:
    project = await project_factory(f"{slug}-{uuid.uuid4().hex[:6]}")
    site = Site(project_id=project.id, code=f"SNT-{uuid.uuid4().hex[:6]}", name="A-Blok Şantiyesi")
    session.add(site)
    # BLF-B1: `create_all` migration tohumu getirmez — 7 tip burada eklenir.
    await seed_section_types(session)
    await session.flush()
    return site


async def _section(session, site: Site, name: str, **kwargs) -> Section:
    section = Section(site_id=site.id, name=name, **kwargs)
    session.add(section)
    await session.flush()
    return section


async def _milestones(session, section_id: uuid.UUID) -> list[SectionMilestone]:
    result = await session.execute(
        select(SectionMilestone)
        .where(SectionMilestone.section_id == section_id)
        .order_by(SectionMilestone.sort_order)
    )
    return list(result.scalars().all())


# --- POST: iki alanin mutlu yolu ---


async def test_create_writes_dependency_and_milestones(
    client, db_session, user_factory, project_factory
):
    site = await _site(db_session, project_factory, "P11-POST")
    predecessor = await _section(db_session, site, "Temel")
    token = await _login(client, user_factory)

    resp = await client.post(
        f"/sites/{site.id}/sections",
        json=_published(
            depends_on_section_id=str(predecessor.id),
            milestones=[
                {"title": "Kat 14 döşeme tamamlanması", "milestone_date": "2027-01-15"},
                {"title": "Kalıp söküm", "milestone_date": "2027-02-01"},
            ],
        ),
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["depends_on_section_id"] == str(predecessor.id)
    assert [m["title"] for m in body["milestones"]] == [
        "Kat 14 döşeme tamamlanması",
        "Kalıp söküm",
    ]
    assert [m["sort_order"] for m in body["milestones"]] == [0, 1]
    rows = await _milestones(db_session, uuid.UUID(body["id"]))
    assert [(r.title, r.milestone_date) for r in rows] == [
        ("Kat 14 döşeme tamamlanması", date(2027, 1, 15)),
        ("Kalıp söküm", date(2027, 2, 1)),
    ]


async def test_create_without_new_fields_stays_empty(
    client, db_session, user_factory, project_factory
):
    """Iki alan da ADDITIVE'dir: eski govdeler aynen calisir."""
    site = await _site(db_session, project_factory, "P11-POST-BOS")
    token = await _login(client, user_factory)

    resp = await client.post(f"/sites/{site.id}/sections", json=_published(), headers=_auth(token))

    assert resp.status_code == 201, resp.text
    assert resp.json()["depends_on_section_id"] is None
    assert resp.json()["milestones"] == []


# --- Bagimlilik: ayni santiye sarti (karar 2) ---


async def test_create_rejects_predecessor_from_other_site(
    client, db_session, user_factory, project_factory
):
    site = await _site(db_session, project_factory, "P11-SITE-A")
    other = await _site(db_session, project_factory, "P11-SITE-B")
    foreign = await _section(db_session, other, "Yabancı Bölüm")
    token = await _login(client, user_factory)

    resp = await client.post(
        f"/sites/{site.id}/sections",
        json=_published(depends_on_section_id=str(foreign.id)),
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == DEPENDS_NOT_IN_SITE


async def test_unknown_predecessor_is_indistinguishable_from_foreign_one(
    client, db_session, user_factory, project_factory
):
    """Var olmayan id ile baska santiyedeki id AYNI cevabi verir (karar 2)."""
    site = await _site(db_session, project_factory, "P11-YOK")
    token = await _login(client, user_factory)

    resp = await client.post(
        f"/sites/{site.id}/sections",
        json=_published(depends_on_section_id=str(uuid.uuid4())),
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == DEPENDS_NOT_IN_SITE


async def test_patch_rejects_predecessor_from_other_site(
    client, db_session, user_factory, project_factory
):
    site = await _site(db_session, project_factory, "P11-PATCH-SITE")
    other = await _site(db_session, project_factory, "P11-PATCH-OTHER")
    section = await _section(db_session, site, "Kaba İnşaat")
    foreign = await _section(db_session, other, "Yabancı Bölüm")
    token = await _login(client, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={"depends_on_section_id": str(foreign.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == DEPENDS_NOT_IN_SITE
    await db_session.refresh(section)
    assert section.depends_on_section_id is None


# --- Bagimlilik: self / 2'li / 3'lu dongu (karar 3) ---


async def test_patch_rejects_self_dependency(client, db_session, user_factory, project_factory):
    site = await _site(db_session, project_factory, "P11-SELF")
    section = await _section(db_session, site, "Kaba İnşaat")
    token = await _login(client, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={"depends_on_section_id": str(section.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == DEPENDS_SELF


async def test_patch_rejects_two_step_cycle(client, db_session, user_factory, project_factory):
    """A <- B varken B <- A istegi donguyu kapatirdi."""
    site = await _site(db_session, project_factory, "P11-CYCLE2")
    first = await _section(db_session, site, "Temel")
    second = await _section(db_session, site, "Kaba İnşaat", depends_on_section_id=first.id)
    await db_session.flush()
    token = await _login(client, user_factory)

    resp = await client.patch(
        f"/sections/{first.id}",
        json={"depends_on_section_id": str(second.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == DEPENDS_CYCLE
    await db_session.refresh(first)
    assert first.depends_on_section_id is None


async def test_patch_rejects_three_step_cycle(client, db_session, user_factory, project_factory):
    """A <- B <- C varken A -> C donguyu UZAK ucundan kapatirdi."""
    site = await _site(db_session, project_factory, "P11-CYCLE3")
    first = await _section(db_session, site, "Temel")
    second = await _section(db_session, site, "Kaba İnşaat", depends_on_section_id=first.id)
    third = await _section(db_session, site, "İnce İşler", depends_on_section_id=second.id)
    await db_session.flush()
    token = await _login(client, user_factory)

    resp = await client.patch(
        f"/sections/{first.id}",
        json={"depends_on_section_id": str(third.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == DEPENDS_CYCLE


async def test_patch_accepts_non_cyclic_chain(client, db_session, user_factory, project_factory):
    """Ayni zincirin TERS yonu gecerlidir — dongu tespiti asiri genis olmamali."""
    site = await _site(db_session, project_factory, "P11-CHAIN")
    first = await _section(db_session, site, "Temel")
    second = await _section(db_session, site, "Kaba İnşaat", depends_on_section_id=first.id)
    third = await _section(db_session, site, "İnce İşler")
    await db_session.flush()
    token = await _login(client, user_factory)

    resp = await client.patch(
        f"/sections/{third.id}",
        json={"depends_on_section_id": str(second.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["depends_on_section_id"] == str(second.id)


async def test_patch_can_clear_dependency(client, db_session, user_factory, project_factory):
    """Acikca `null` gondermek bagi KOPARIR (gondermemekle karistirilmaz)."""
    site = await _site(db_session, project_factory, "P11-CLEAR")
    predecessor = await _section(db_session, site, "Temel")
    section = await _section(db_session, site, "Kaba İnşaat", depends_on_section_id=predecessor.id)
    await db_session.flush()
    token = await _login(client, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={"depends_on_section_id": None},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["depends_on_section_id"] is None
    await db_session.refresh(section)
    assert section.depends_on_section_id is None


async def test_patch_without_dependency_field_keeps_it(
    client, db_session, user_factory, project_factory
):
    site = await _site(db_session, project_factory, "P11-KEEP")
    predecessor = await _section(db_session, site, "Temel")
    section = await _section(db_session, site, "Kaba İnşaat", depends_on_section_id=predecessor.id)
    await db_session.flush()
    token = await _login(client, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}", json={"name": "Kaba İnşaat 2"}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["depends_on_section_id"] == str(predecessor.id)


# --- Bagimlilik: TARIH KISITI ZORLANMAZ (karar 1) ---


async def test_dependency_does_not_enforce_dates(client, db_session, user_factory, project_factory):
    """Oncul BITMEDEN baslayan bolum 422 ALMAZ — bag yalniz bilgidir (spec §3)."""
    site = await _site(db_session, project_factory, "P11-TARIH")
    predecessor = await _section(
        db_session, site, "Temel", start_date=date(2027, 1, 1), end_date=date(2027, 12, 31)
    )
    token = await _login(client, user_factory)

    resp = await client.post(
        f"/sites/{site.id}/sections",
        json=_published(
            start_date="2026-01-01",
            end_date="2026-06-30",
            depends_on_section_id=str(predecessor.id),
        ),
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["depends_on_section_id"] == str(predecessor.id)


# --- Milestone birlestirme (karar 4/5) ---


async def test_patch_merge_preserves_row_identity(
    client, db_session, user_factory, project_factory
):
    """id'li satir YERINDE guncellenir (id DEGISMEZ), id'siz yenidir, eksik olan DUSER."""
    site = await _site(db_session, project_factory, "P11-MERGE")
    section = await _section(db_session, site, "Kaba İnşaat")
    kept = SectionMilestone(
        section_id=section.id, title="Kat 14 döşeme", milestone_date=date(2027, 1, 15), sort_order=0
    )
    dropped = SectionMilestone(
        section_id=section.id, title="Silinecek", milestone_date=date(2027, 3, 1), sort_order=1
    )
    db_session.add_all([kept, dropped])
    await db_session.flush()
    kept_id, dropped_id = kept.id, dropped.id
    token = await _login(client, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={
            "milestones": [
                {
                    "id": str(kept_id),
                    "title": "Kat 14 döşeme tamamlandı",
                    "milestone_date": "2027-01-20",
                },
                {"title": "Yeni taş", "milestone_date": "2027-04-01"},
            ]
        },
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [m["id"] for m in body["milestones"]][0] == str(kept_id)
    assert [m["title"] for m in body["milestones"]] == ["Kat 14 döşeme tamamlandı", "Yeni taş"]
    assert [m["sort_order"] for m in body["milestones"]] == [0, 1]
    rows = await _milestones(db_session, section.id)
    assert [r.id for r in rows][0] == kept_id
    assert dropped_id not in [r.id for r in rows]
    assert rows[0].milestone_date == date(2027, 1, 20)


async def test_patch_without_milestones_field_keeps_rows(
    client, db_session, user_factory, project_factory
):
    """Alan gonderilmezse mevcut satirlara DOKUNULMAZ (karar 5)."""
    site = await _site(db_session, project_factory, "P11-DOKUNMA")
    section = await _section(db_session, site, "Kaba İnşaat")
    db_session.add(
        SectionMilestone(
            section_id=section.id, title="Kalır", milestone_date=date(2027, 1, 15), sort_order=0
        )
    )
    await db_session.flush()
    token = await _login(client, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}", json={"name": "Kaba İnşaat 2"}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    assert [m["title"] for m in resp.json()["milestones"]] == ["Kalır"]
    assert len(await _milestones(db_session, section.id)) == 1


async def test_patch_empty_list_clears_rows(client, db_session, user_factory, project_factory):
    """Bos liste GONDERMEK, alani hic gondermemekten farklidir: hepsi duser."""
    site = await _site(db_session, project_factory, "P11-BOSALT")
    section = await _section(db_session, site, "Kaba İnşaat")
    db_session.add(
        SectionMilestone(
            section_id=section.id, title="Gidecek", milestone_date=date(2027, 1, 15), sort_order=0
        )
    )
    await db_session.flush()
    token = await _login(client, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}", json={"milestones": []}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["milestones"] == []
    assert await _milestones(db_session, section.id) == []


async def test_patch_rejects_unknown_milestone_id(
    client, db_session, user_factory, project_factory
):
    site = await _site(db_session, project_factory, "P11-BILINMEYEN")
    section = await _section(db_session, site, "Kaba İnşaat")
    token = await _login(client, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={
            "milestones": [
                {"id": str(uuid.uuid4()), "title": "Hayalet", "milestone_date": "2027-01-15"}
            ]
        },
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == MILESTONE_UNKNOWN
    assert await _milestones(db_session, section.id) == []


async def test_patch_rejects_milestone_id_of_another_section(
    client, db_session, user_factory, project_factory
):
    """Baska bolumun satiri CALINAMAZ — bilinmeyen id ile AYNI cevap."""
    site = await _site(db_session, project_factory, "P11-CALMA")
    section = await _section(db_session, site, "Kaba İnşaat")
    other = await _section(db_session, site, "İnce İşler")
    foreign = SectionMilestone(
        section_id=other.id, title="Komşunun taşı", milestone_date=date(2027, 1, 15), sort_order=0
    )
    db_session.add(foreign)
    await db_session.flush()
    token = await _login(client, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={
            "milestones": [
                {"id": str(foreign.id), "title": "Çalındı", "milestone_date": "2027-02-01"}
            ]
        },
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == MILESTONE_UNKNOWN
    await db_session.refresh(foreign)
    assert foreign.section_id == other.id
    assert foreign.title == "Komşunun taşı"


async def test_patch_rejects_duplicate_milestone_id(
    client, db_session, user_factory, project_factory
):
    """Ayni id iki kez -> birlestirme SESSIZCE tek satira cokmemeli (P9 T5 dersi)."""
    site = await _site(db_session, project_factory, "P11-CIFT")
    section = await _section(db_session, site, "Kaba İnşaat")
    row = SectionMilestone(
        section_id=section.id, title="Tek", milestone_date=date(2027, 1, 15), sort_order=0
    )
    db_session.add(row)
    await db_session.flush()
    token = await _login(client, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={
            "milestones": [
                {"id": str(row.id), "title": "Bir", "milestone_date": "2027-01-15"},
                {"id": str(row.id), "title": "İki", "milestone_date": "2027-02-15"},
            ]
        },
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == MILESTONE_DUPLICATE_IN_PAYLOAD
    await db_session.refresh(row)
    assert row.title == "Tek"


async def test_create_rejects_milestone_id(client, db_session, user_factory, project_factory):
    """POST'ta id'li satir olamaz: guncellenecek bir satir HENUZ YOKTUR."""
    site = await _site(db_session, project_factory, "P11-POST-ID")
    token = await _login(client, user_factory)

    resp = await client.post(
        f"/sites/{site.id}/sections",
        json=_published(
            milestones=[{"id": str(uuid.uuid4()), "title": "X", "milestone_date": "2027-01-15"}]
        ),
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == MILESTONE_UNKNOWN


# --- Okuma yanitlari (additive) ---


@pytest.mark.parametrize("path", ["detail", "list", "site"])
async def test_read_surfaces_expose_new_fields(
    client, db_session, user_factory, project_factory, path
):
    """Bolum basan UC yuzey de yeni alanlari tasir; milestone sirasi DETERMINISTIK."""
    site = await _site(db_session, project_factory, "P11-OKUMA")
    predecessor = await _section(db_session, site, "Temel")
    section = await _section(db_session, site, "Kaba İnşaat", depends_on_section_id=predecessor.id)
    db_session.add_all(
        [
            SectionMilestone(
                section_id=section.id, title="İkinci", milestone_date=date(2027, 2, 1), sort_order=1
            ),
            SectionMilestone(
                section_id=section.id,
                title="Birinci",
                milestone_date=date(2027, 1, 1),
                sort_order=0,
            ),
        ]
    )
    await db_session.flush()
    token = await _login(client, user_factory)

    urls = {
        "detail": f"/sections/{section.id}",
        "list": f"/sites/{site.id}/sections",
        "site": f"/sites/{site.id}",
    }
    collections = {"list": "items", "site": "sections"}
    resp = await client.get(urls[path], headers=_auth(token))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    row = (
        body
        if path == "detail"
        else next(item for item in body[collections[path]] if item["id"] == str(section.id))
    )
    assert row["depends_on_section_id"] == str(predecessor.id)
    assert [m["title"] for m in row["milestones"]] == ["Birinci", "İkinci"]


# --- Silme davranisi UC uzerinden (spec §5) ---


async def test_delete_section_cascades_milestones(
    client, db_session, user_factory, project_factory
):
    site = await _site(db_session, project_factory, "P11-CASCADE")
    section = await _section(db_session, site, "Kaba İnşaat")
    db_session.add(
        SectionMilestone(
            section_id=section.id, title="Gidecek", milestone_date=date(2027, 1, 15), sort_order=0
        )
    )
    await db_session.flush()
    section_id = section.id
    token = await _login(client, user_factory)

    resp = await sil_aile(client, _auth(token), "section", section_id)

    assert resp.status_code == 204, resp.text
    assert await _milestones(db_session, section_id) == []


async def test_delete_predecessor_keeps_dependent_and_nulls_link(
    client, db_session, user_factory, project_factory
):
    """Oncul silinince BAGIMLI BOLUM KALIR, yalniz bag kopar (SET NULL)."""
    site = await _site(db_session, project_factory, "P11-SETNULL")
    predecessor = await _section(db_session, site, "Temel")
    dependent = await _section(
        db_session, site, "Kaba İnşaat", depends_on_section_id=predecessor.id
    )
    await db_session.flush()
    token = await _login(client, user_factory)

    resp = await sil_aile(client, _auth(token), "section", predecessor.id)
    assert resp.status_code == 204, resp.text

    # `SET NULL` DB'de olur; testte istek ile fikstur AYNI oturumu paylastigi icin
    # kimlik haritasindaki nesne eskimis kalir (canlida her istek kendi
    # oturumunu acar ve bu durum olusmaz). Bagi DB gerceginden okuyabilmek icin
    # nesne acikca DB'den tazelenir (`expire` DEGIL: eskimis bir oznitelige
    # senkron erisim asenkron oturumda `MissingGreenlet` atar).
    await db_session.refresh(dependent)
    assert dependent.depends_on_section_id is None

    detail = await client.get(f"/sections/{dependent.id}", headers=_auth(token))
    assert detail.status_code == 200, detail.text
    assert detail.json()["depends_on_section_id"] is None


# --- Denetim (karar 6) ---


async def test_milestone_patch_writes_single_existing_audit_row(
    client, db_session, user_factory, project_factory
):
    site = await _site(db_session, project_factory, "P11-DENETIM")
    section = await _section(db_session, site, "Kaba İnşaat")
    token = await _login(client, user_factory)

    resp = await client.patch(
        f"/sections/{section.id}",
        json={"milestones": [{"title": "Yeni taş", "milestone_date": "2027-04-01"}]},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    rows = list(
        (await db_session.execute(select(AuditLog).where(AuditLog.action == AuditAction.update)))
        .scalars()
        .all()
    )
    assert [r.detail for r in rows] == [section_updated(site.name, section.name)]

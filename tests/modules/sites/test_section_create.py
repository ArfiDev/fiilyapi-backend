"""P6 · T3 — genisleyen `POST /sites/{site_id}/sections` (`Form - Bolum Ekle`).

## Neden ayri bir dosya

T2 bolum yuzeyinin OKUMA ve GUNCELLEME tarafini sabitledi; T3 YAZMA tarafini
acar ve orada iki yeni davranis dogar: **taslak-farkindalikli zorunluluk** ve
**otomatik kod uretimi**. Ikisi de `Section` satirinin ILK hâlini belirler,
dolayisiyla PATCH testlerinin yaninda degil kendi dosyasinda durur.

## Sabitlenen kararlar

1. **Zorunluluk kurallari mockup'tan gelir, GOZ KARARI DEGIL** — `Form - Bolum
   Ekle.dc.html` icinde `<span class="req">*</span>` tasiyan alanlar:
   * 67 Bölüm Adı → `name` (Pydantic zaten zorunlu)
   * 69 Bölüm Sırası → `sort_order` (varsayilani 0 oldugu icin ASLA bos olamaz)
   * 70 Bölüm Tipi → `section_type_id`
   * 83 Bölüm Sorumlusu → `manager_user_id` **veya** `manager_name`
   * 107/108 Başlangıç + Planlanan Bitiş → `start_date` / `end_date`
   * 110 Bölüm Bedeli → BLF-B1: KALKTI (yalniz turev `budget`, formda salt-okunur)
   * 66 Şantiye → YOL PARAMETRESIDIR, govdede aranmaz.
   Mockup'ta `*` TASIMAYAN alan zorunlu YAPILMAZ: 68 Bölüm Kodu ("Boş
   bırakılırsa otomatik"), 71 Durum, 74 Açıklama, 84 Yardımcı Sorumlu,
   85 Planlanan İşçi Sayısı.
2. **Taslak (Form 242 "Taslak Kaydet") zorunlulugu KALDIRIR, tutarliligi
   KALDIRMAZ** — `guards.py` docstring'indeki tek cumlelik kural bolume aynen
   gecer: ters tarih araligi taslakta da 422'dir.
3. **`code` bossa `BLM-NN` uretilir** (spec §5, Form 68 ipucu) ve sayac
   SANTIYE ICINDE artar — `_next_site_code`in sirket geneli kapsamindan bilincli
   olarak AYRILIR: bolum kodu evrakta degil, santiye ici siralamada okunur.
4. **Elle verilen cakisan kod 409'dur** — santiye kodu cakismasiyla AYNI kod ve
   AYNI desen (`DuplicateError`), yeni bir desen ICAT EDILMEZ.
5. **Denetim TEK satirdir**: taslak icin AYRI `AuditAction` ACILMAZ (T3 karari),
   taslak da yayin da mevcut `section_created` metnini yazar.
"""

import uuid

import pytest
from sqlalchemy import func, select

from app.core.access import AccessLevel, Scope
from app.modules.audit.messages import section_created
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.roles.models import Module, Role, RolePermission
from app.modules.sites.models import Section, SectionStatus, Site
from app.modules.users.models import UserProjectAccess
from tests._legacy_permission_yardimcisi import sync_page_cells
from tests._section_types import SEED_TYPE_IDS, seed_section_types

SITE_MISSING = "Şantiye bulunamadı"
USER_MISSING = "Seçilen kullanıcı bulunamadı"
DUPLICATE_SECTION_CODE = "Bu bölüm kodu bu şantiyede zaten kullanılıyor"

SECTION_TYPE_REQUIRED = "Bölüm tipi seçiniz."
SECTION_MANAGER_REQUIRED = "Bölüm sorumlusu seçiniz."
SECTION_DATES_REQUIRED = "Başlangıç ve planlanan bitiş tarihi zorunludur."
SECTION_TYPE_MISSING = "Bölüm tipi bulunamadı"
END_BEFORE_START = "Planlanan bitiş tarihi başlangıçtan önce olamaz."

WRITE_ROLE = "patron"  # sites=full
VIEW_ROLE = "site_chief"  # sites=view

# Mockup'ta `*` tasiyan ve govdeden gelen alanlarin TAM kumesi (bkz. modul
# docstring'i 1. madde). `name` ayri tutulur: onu Pydantic zorunlu kilar.
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


async def _login(client, session, user_factory, role_key: str, *, grant_all: bool) -> str:
    address = f"{role_key}-{uuid.uuid4().hex[:6]}@t.co"
    user = await user_factory(email=address, password="parola1234", role_key=role_key)
    if grant_all:
        session.add(UserProjectAccess(user_id=user.id, project_id=None, all_projects=True))
        await session.flush()
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


async def _set_permission(
    session, role_key: str, module_key: str, level: AccessLevel, scope: Scope = Scope.all
) -> None:
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


async def _site(session, project_factory, slug: str) -> Site:
    project = await project_factory(f"{slug}-{uuid.uuid4().hex[:6]}")
    site = Site(project_id=project.id, code=f"SNT-{uuid.uuid4().hex[:6]}", name="A-Blok Şantiyesi")
    session.add(site)
    # BLF-B1: testler `create_all` kullanir, migration tohumu YOK — 7 tip burada eklenir.
    await seed_section_types(session)
    await session.flush()
    return site


async def _sections(session, site_id: uuid.UUID) -> list[Section]:
    result = await session.execute(
        select(Section).where(Section.site_id == site_id).order_by(Section.code)
    )
    return list(result.scalars().all())


# --- Mutlu yol: tum yeni alanlar ---


async def test_create_writes_every_new_column(client, db_session, user_factory, project_factory):
    """T1'in actigi TUM kolonlar POST govdesinden yazilabilir (spec §5)."""
    manager = await user_factory(
        email=f"sef-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        full_name="Sercan Öztürk",
    )
    deputy = await user_factory(
        email=f"yrd-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        full_name="Kadir Yıldız",
    )
    site = await _site(db_session, project_factory, "P6T3-FULL")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    resp = await client.post(
        f"/sites/{site.id}/sections",
        json=_published(
            code="BLM-06",
            status="on_hold",
            description="Kat 11–14 arası betonarme.",
            manager_user_id=str(manager.id),
            deputy_manager_user_id=str(deputy.id),
            planned_worker_count=42,
            sort_order=6,
        ),
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    (section,) = await _sections(db_session, site.id)
    assert section.code == "BLM-06"
    assert section.name == "Kat 11–14 Kaba İnşaat"
    assert section.status is SectionStatus.on_hold
    assert section.section_type_id == SEED_TYPE_IDS["structural"]
    assert section.description == "Kat 11–14 arası betonarme."
    assert section.manager_user_id == manager.id
    assert section.manager_name == "Sercan Öztürk"
    assert section.deputy_manager_user_id == deputy.id
    assert section.deputy_manager_name == "Kadir Yıldız"
    assert section.planned_worker_count == 42
    assert section.budget_amount is None  # BLF-B1: kolon artik yazilmaz
    assert section.sort_order == 6
    assert section.is_draft is False


async def test_create_defaults_stay_untouched(client, db_session, user_factory, project_factory):
    """Mockup'ta `*` TASIMAYAN alanlar bos birakilabilir (71/74/84/85)."""
    site = await _site(db_session, project_factory, "P6T3-DEFAULT")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    resp = await client.post(f"/sites/{site.id}/sections", json=_published(), headers=_auth(token))

    assert resp.status_code == 201, resp.text
    (section,) = await _sections(db_session, site.id)
    assert section.status is SectionStatus.planned
    assert section.description is None
    assert section.deputy_manager_user_id is None
    assert section.deputy_manager_name is None
    assert section.planned_worker_count is None
    assert section.is_draft is False
    assert section.sort_order == 0


# --- Taslak mantigi (kalici karar 4, Form 242) ---


async def test_draft_create_relaxes_required_fields(
    client, db_session, user_factory, project_factory
):
    """ "Taslak Kaydet" (Form 242): mockup'in `*` alanlari BOS gecilebilir."""
    site = await _site(db_session, project_factory, "P6T3-DRAFT")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    resp = await client.post(
        f"/sites/{site.id}/sections",
        json={"name": "Yarım Kalmış Bölüm", "is_draft": True},
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    (section,) = await _sections(db_session, site.id)
    assert section.is_draft is True
    assert section.section_type_id is None
    assert section.budget_amount is None
    assert section.start_date is None


@pytest.mark.parametrize(
    ("omitted", "message"),
    [
        ("section_type_id", SECTION_TYPE_REQUIRED),
        ("manager_name", SECTION_MANAGER_REQUIRED),
        ("start_date", SECTION_DATES_REQUIRED),
        ("end_date", SECTION_DATES_REQUIRED),
    ],
)
async def test_published_create_requires_every_starred_field(
    client, db_session, user_factory, project_factory, omitted, message
):
    """ "Bölümü Oluştur" (Form 243): mockup'in `*` alanlarindan biri eksikse 422
    ve HICBIR satir yazilmaz."""
    site = await _site(db_session, project_factory, f"P6T3-REQ-{omitted}")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)
    payload = _published()
    del payload[omitted]

    resp = await client.post(f"/sites/{site.id}/sections", json=payload, headers=_auth(token))

    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": message}
    assert await _sections(db_session, site.id) == []


async def test_manager_fk_satisfies_manager_requirement(
    client, db_session, user_factory, project_factory
):
    """Sef ya FK ya serbest metin olarak verilir (`SITE_MANAGER_REQUIRED` deseni):
    ikisinden biri yeter."""
    manager = await user_factory(
        email=f"sef-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        full_name="Murat Arslan",
    )
    site = await _site(db_session, project_factory, "P6T3-FKMGR")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)
    payload = _published(manager_user_id=str(manager.id))
    del payload["manager_name"]

    resp = await client.post(f"/sites/{site.id}/sections", json=payload, headers=_auth(token))

    assert resp.status_code == 201, resp.text
    (section,) = await _sections(db_session, site.id)
    assert section.manager_name == "Murat Arslan"


async def test_end_before_start_is_rejected_even_for_draft(
    client, db_session, user_factory, project_factory
):
    """TUTARLILIK kurallari taslakta da kosar (guards docstring'i): yarim taslak
    EKSIK veri saklayabilir, GECERSIZ veri saklayamaz."""
    site = await _site(db_session, project_factory, "P6T3-REVERSE")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    resp = await client.post(
        f"/sites/{site.id}/sections",
        json={
            "name": "Ters Aralık",
            "is_draft": True,
            "start_date": "2027-03-31",
            "end_date": "2026-10-01",
        },
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": END_BEFORE_START}
    assert await _sections(db_session, site.id) == []


# --- Yardimci sorumlu (kalici karar 5) ---


async def test_deputy_fk_overwrites_name_snapshot(
    client, db_session, user_factory, project_factory
):
    """PATCH ile AYNI kural (T2): ad FK'nin TUREVIDIR, govdedeki serbest metin
    yok sayilir."""
    deputy = await user_factory(
        email=f"yrd-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        full_name="Ayşe Yılmaz",
    )
    site = await _site(db_session, project_factory, "P6T3-SNAP")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    resp = await client.post(
        f"/sites/{site.id}/sections",
        json=_published(deputy_manager_user_id=str(deputy.id), deputy_manager_name="YANLIŞ AD"),
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    (section,) = await _sections(db_session, site.id)
    assert section.deputy_manager_name == "Ayşe Yılmaz"


async def test_deputy_accepts_on_leave_user(client, db_session, user_factory, project_factory):
    """Kalici karar 5: IZINLI personel atanabilir — izin GECICI bir durumdur."""
    deputy = await user_factory(
        email=f"izin-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        status="on_leave",
        full_name="İzindeki Sorumlu",
    )
    site = await _site(db_session, project_factory, "P6T3-LEAVE")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    resp = await client.post(
        f"/sites/{site.id}/sections",
        json=_published(deputy_manager_user_id=str(deputy.id)),
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    (section,) = await _sections(db_session, site.id)
    assert section.deputy_manager_name == "İzindeki Sorumlu"


async def test_deputy_passive_user_returns_422_and_writes_nothing(
    client, db_session, user_factory, project_factory
):
    """Pasif kullanici 422 (404 DEGIL: istenen kaynak BOLUMDUR) ve kullanici
    cozumu YAZMADAN ONCE kostugu icin satir hic dogmaz."""
    deputy = await user_factory(
        email=f"pasif-{uuid.uuid4().hex[:6]}@t.co",
        password="parola1234",
        role_key="site_chief",
        status="passive",
    )
    site = await _site(db_session, project_factory, "P6T3-PASSIVE")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    resp = await client.post(
        f"/sites/{site.id}/sections",
        json=_published(deputy_manager_user_id=str(deputy.id)),
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": USER_MISSING}
    assert await _sections(db_session, site.id) == []


# --- Pydantic alan kurallari ---


async def test_negative_numbers_return_422(client, db_session, user_factory, project_factory):
    """`ge=0` PATCH ile ayni yerde durur: DB CHECK'inden ONCE, alan bazli hata."""
    site = await _site(db_session, project_factory, "P6T3-NEG")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    workers = await client.post(
        f"/sites/{site.id}/sections",
        json=_published(planned_worker_count=-1),
        headers=_auth(token),
    )
    order = await client.post(
        f"/sites/{site.id}/sections", json=_published(sort_order=-1), headers=_auth(token)
    )

    assert workers.status_code == 422, workers.text
    assert order.status_code == 422, order.text
    assert await _sections(db_session, site.id) == []


async def test_unknown_section_type_returns_422(client, db_session, user_factory, project_factory):
    """Var olmayan tip kimligi sessizce NULL'a DUSMEZ (BLF-B1: 422, tip bulunamadi)."""
    site = await _site(db_session, project_factory, "P6T3-ENUM")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    resp = await client.post(
        f"/sites/{site.id}/sections",
        json=_published(section_type_id=str(uuid.uuid4())),
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": SECTION_TYPE_MISSING}
    assert await _sections(db_session, site.id) == []


# --- Kod uretimi (spec §5, Form 68) ---


async def test_blank_code_generates_sequential_blm_codes(
    client, db_session, user_factory, project_factory
):
    """Form 68 ipucu "Boş bırakılırsa otomatik": `BLM-NN`, sayimla DEGIL max+1."""
    site = await _site(db_session, project_factory, "P6T3-CODE")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    first = await client.post(
        f"/sites/{site.id}/sections", json=_published(name="Temel"), headers=_auth(token)
    )
    second = await client.post(
        f"/sites/{site.id}/sections", json=_published(name="Kaba"), headers=_auth(token)
    )

    assert first.status_code == second.status_code == 201, second.text
    assert first.json()["code"] == "BLM-01"
    assert second.json()["code"] == "BLM-02"


async def test_code_sequence_is_scoped_to_the_site(
    client, db_session, user_factory, project_factory
):
    """Sayac SANTIYE ICINDE artar: her santiye kendi `BLM-01`inden baslar —
    santiye kodunun sirket geneli kapsamindan bilincli olarak AYRILIR."""
    first_site = await _site(db_session, project_factory, "P6T3-SCOPE-A")
    second_site = await _site(db_session, project_factory, "P6T3-SCOPE-B")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    await client.post(f"/sites/{first_site.id}/sections", json=_published(), headers=_auth(token))
    other = await client.post(
        f"/sites/{second_site.id}/sections", json=_published(), headers=_auth(token)
    )

    assert other.status_code == 201, other.text
    assert other.json()["code"] == "BLM-01"


async def test_generated_code_continues_after_manual_code(
    client, db_session, user_factory, project_factory
):
    """Elle verilen kod sayaci ILERLETIR (max+1): `BLM-06`dan sonra `BLM-07`.
    Ayristirilamayan eski kodlar (`GENEL`) sessizce ATLANIR."""
    site = await _site(db_session, project_factory, "P6T3-MAX")
    db_session.add(Section(site_id=site.id, code="GENEL", name="Eski"))
    await db_session.flush()
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    await client.post(
        f"/sites/{site.id}/sections", json=_published(code="BLM-06"), headers=_auth(token)
    )
    generated = await client.post(
        f"/sites/{site.id}/sections", json=_published(name="Sonraki"), headers=_auth(token)
    )

    assert generated.status_code == 201, generated.text
    assert generated.json()["code"] == "BLM-07"


async def test_duplicate_manual_code_returns_409(client, db_session, user_factory, project_factory):
    """Cakisma kodu SANTIYE kodununkiyle AYNIDIR (409 + alanina ozel Turkce
    mesaj): yeni bir desen ICAT EDILMEZ."""
    site = await _site(db_session, project_factory, "P6T3-DUP")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    first = await client.post(
        f"/sites/{site.id}/sections", json=_published(code="BLM-03"), headers=_auth(token)
    )
    clash = await client.post(
        f"/sites/{site.id}/sections",
        json=_published(code="BLM-03", name="Ayni kod"),
        headers=_auth(token),
    )

    assert first.status_code == 201, first.text
    assert clash.status_code == 409, clash.text
    assert clash.json() == {"detail": DUPLICATE_SECTION_CODE}
    assert len(await _sections(db_session, site.id)) == 1


async def test_same_code_allowed_in_another_site(client, db_session, user_factory, project_factory):
    """`uq_sections_site_code` SANTIYE ICINDE tekildir: baska santiyede ayni kod
    gecerlidir."""
    first_site = await _site(db_session, project_factory, "P6T3-DUP-A")
    second_site = await _site(db_session, project_factory, "P6T3-DUP-B")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    await client.post(
        f"/sites/{first_site.id}/sections", json=_published(code="BLM-01"), headers=_auth(token)
    )
    other = await client.post(
        f"/sites/{second_site.id}/sections", json=_published(code="BLM-01"), headers=_auth(token)
    )

    assert other.status_code == 201, other.text


# --- Yetki + IDOR ---


async def test_create_without_full_permission_returns_403(
    client, db_session, user_factory, project_factory
):
    site = await _site(db_session, project_factory, "P6T3-403")
    await _set_permission(db_session, VIEW_ROLE, "sites", AccessLevel.view)
    token = await _login(client, db_session, user_factory, VIEW_ROLE, grant_all=True)

    resp = await client.post(f"/sites/{site.id}/sections", json=_published(), headers=_auth(token))

    assert resp.status_code == 403, resp.text
    assert await _sections(db_session, site.id) == []


async def test_create_on_invisible_site_is_indistinguishable_from_unknown(
    client, db_session, user_factory, project_factory
):
    """YAZMA ucu de IDOR yuzeyidir: gorunmeyen GERCEK santiye ile var olmayan
    UUID ayni durum kodunu VE ayni govdeyi doner."""
    site = await _site(db_session, project_factory, "P6T3-IDOR")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=False)

    invisible = await client.post(
        f"/sites/{site.id}/sections", json=_published(), headers=_auth(token)
    )
    unknown = await client.post(
        f"/sites/{uuid.uuid4()}/sections", json=_published(), headers=_auth(token)
    )

    assert invisible.status_code == unknown.status_code == 404
    assert invisible.json() == unknown.json() == {"detail": SITE_MISSING}
    assert await _sections(db_session, site.id) == []


# --- Denetim ---


async def test_create_writes_single_section_created_audit_row(
    client, db_session, user_factory, project_factory
):
    """Taslak icin AYRI `AuditAction` ACILMAZ (T3 karari): taslak da yayin da
    mevcut create aksiyonuna TEK satir yazar."""
    site = await _site(db_session, project_factory, "P6T3-AUDIT")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)

    published = await client.post(
        f"/sites/{site.id}/sections", json=_published(name="Yayın"), headers=_auth(token)
    )
    draft = await client.post(
        f"/sites/{site.id}/sections",
        json={"name": "Taslak", "is_draft": True},
        headers=_auth(token),
    )

    assert published.status_code == draft.status_code == 201, draft.text
    rows = list(
        (await db_session.execute(select(AuditLog).where(AuditLog.action == AuditAction.create)))
        .scalars()
        .all()
    )
    # 🔴 SIRA İDDİA EDİLMEZ (2026-09-23, CI kırmızısı): sorguda `ORDER BY` yok ve
    # `AuditLog.id` uuid4'tür — ekleme sırasına göre SIRALANAMAZ. Yerelde satırlar
    # tesadüfen ekleme sırasında geliyordu, CI'da (farklı plan/heap) ters geldi ve
    # test kırmızı verdi. Testin iddiası zaten sıra değil: "taslak da yayın da
    # AYNI aksiyona TEK satır yazar". Bu yüzden karşılaştırma SIRASIZDIR.
    assert sorted(row.detail for row in rows) == sorted(
        [
            section_created(site.name, "Yayın"),
            section_created(site.name, "Taslak"),
        ]
    )


async def test_rejected_create_writes_no_audit_row(
    client, db_session, user_factory, project_factory
):
    """Denetim GERCEKLESEN olayi kaydeder, denemeyi degil."""
    site = await _site(db_session, project_factory, "P6T3-NOAUDIT")
    token = await _login(client, db_session, user_factory, WRITE_ROLE, grant_all=True)
    before = int(
        (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    )

    resp = await client.post(
        f"/sites/{site.id}/sections", json={"name": "Eksik"}, headers=_auth(token)
    )

    assert resp.status_code == 422, resp.text
    after = int((await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one())
    assert after == before


# --- OpenAPI sozlesmesi ---


def test_openapi_create_body_carries_every_new_field():
    """Frontend sozlesmeyi semadan uretir: alan duserse hata backend testlerinde
    degil frontend derlemesinde ortaya cikardi."""
    from app.main import app

    schema = app.openapi()
    node = schema["paths"]["/sites/{site_id}/sections"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    if "$ref" in node:
        node = schema["components"]["schemas"][node["$ref"].rsplit("/", 1)[1]]
    body = node["properties"]

    for field in (
        "section_type_id",
        "description",
        "deputy_manager_user_id",
        "deputy_manager_name",
        "planned_worker_count",
        "is_draft",
    ):
        assert field in body, field
    # BLF-B1: elle bedel ve eski enum alani govdeden KALKTI.
    assert "budget_amount" not in body
    assert "section_type" not in body
    # BOQ-bolum bagi ACILMAZ (kalici karar 1) — spec §6.
    assert "boq_item_ids" not in body
    assert "subcontractor_ids" not in body
    assert "machine_ids" not in body
    # `depends_on_section_id` P6'da ACILMAMISTI (spec §6 "bu dilimde ara cozum
    # yazilmaz"); P11 onu KENDI diliminde acti (P11 spec §3) — bu yuzden yasakli
    # listeden CIKIP beklenen alanlara gecti; mockup'ta ZATEN cizili bir alandi
    # (`Form - Bolum Ekle` 115-117). Ustteki uc yasak alan yerinde KALIR.
    assert "depends_on_section_id" in body
    assert "milestones" in body

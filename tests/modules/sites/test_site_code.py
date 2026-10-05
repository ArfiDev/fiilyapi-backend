"""Santiye kodu uretimi — `SNT-{YYYY}-{NNN}` (spec §3.2, plan T3).

Onceki ad-turevi uretici (`derive_code`/`_unique_code`) mockup satir 67 ile
CELISIYORDU (yer tutucu `SNT-2026-003`, ipucu "Boş bırakılırsa otomatik").
Karar 2026-07-30: tek uretici `_next_site_code`, `PRJ-` emsalinin birebiri —
**sayimla degil maksimum+1**, **kapsam SIRKET GENELI (global)**.

Kisit degismez: `uq_sites_project_code (project_id, code)`. Yani uretim global
tekil, kisit proje ici tekil. Canlidaki ad-turevi kodlara (`A-BLOK`) HIC
dokunulmaz; `LIKE 'SNT-{yil}-%'` suzgeci onlari zaten gormez.
"""

import pytest
from sqlalchemy import select

from app.core.errors import DuplicateError
from app.core.timezone import today
from app.modules.projects.schemas import ProjectCreate, ProjectSiteInput
from app.modules.projects.service import create_project
from app.modules.sites import service
from app.modules.sites.models import Site
from app.modules.sites.schemas import SiteCreate


def _prefix() -> str:
    return f"SNT-{today().year}-"


async def _patron(session, user_factory, email: str):
    user = await user_factory(email=email, password="parola1234", role_key="patron")
    user.all_projects = True
    await session.flush()
    return user


# --- Uretici ---


async def test_next_site_code_on_empty_table(db_session):
    assert await service._next_site_code(db_session) == f"{_prefix()}001"


async def test_next_site_code_is_max_plus_one(db_session, project_factory):
    project = await project_factory("K-1")
    db_session.add(Site(project_id=project.id, code=f"{_prefix()}003", name="Var"))
    await db_session.flush()

    assert await service._next_site_code(db_session) == f"{_prefix()}004"


async def test_next_site_code_after_deletion_does_not_reuse(db_session, project_factory):
    """Maksimum+1, SAYIM DEGIL: aradan bir kod silinse de sayac GERI SARMAZ.

    001/002/003 varken 002 silinir. Sayim tabanli bir uretici `003`u — yani
    yasayan bir kodu — yeniden uretirdi; maksimum+1 `004` verir. (`PRJ-`
    emsalinin birebiri; en BUYUK kodun silinmesi ayri bir durumdur ve max+1'in
    dogasi geregi geri sarar — `_next_project_code` de boyledir.)
    """
    project = await project_factory("K-2")
    sites = [
        Site(project_id=project.id, code=f"{_prefix()}{seq:03d}", name=f"S{seq}")
        for seq in (1, 2, 3)
    ]
    db_session.add_all(sites)
    await db_session.flush()
    await db_session.delete(sites[1])
    await db_session.flush()

    assert await service._next_site_code(db_session) == f"{_prefix()}004"


async def test_legacy_derived_codes_do_not_affect_counter(db_session, project_factory):
    """Canlidaki `A-BLOK`/`MERKEZ` gibi eski kodlar sayaci ETKILEMEZ."""
    project = await project_factory("K-3")
    db_session.add_all(
        [
            Site(project_id=project.id, code="A-BLOK", name="Eski 1"),
            Site(project_id=project.id, code="MERKEZ", name="Eski 2"),
        ]
    )
    await db_session.flush()

    assert await service._next_site_code(db_session) == f"{_prefix()}001"


async def test_next_site_code_is_global_across_projects(seeded_db, user_factory, project_factory):
    """§3.2 GLOBAL karar kilidi: B projesi `001`'i TEKRAR URETMEZ."""
    first_project = await project_factory("K-4")
    second_project = await project_factory("K-5")
    user = await _patron(seeded_db, user_factory, "k4@t.co")

    # `is_draft=True` T6'da EKLENDI: taslak-disi POST artik sef/il/insaat alani/tarih
    # zorunlulugunu kosar (spec §5.1/7-10). Bu test KOD URETICISINI sinar, form
    # zorunlulugunu degil — gerekli alanlari doldurmak testin konusunu bulandirirdi.
    first = await service.create_site(
        seeded_db, user, first_project.id, SiteCreate(name="A", is_draft=True)
    )
    second = await service.create_site(
        seeded_db, user, second_project.id, SiteCreate(name="B", is_draft=True)
    )

    assert first.code == f"{_prefix()}001"
    assert second.code == f"{_prefix()}002"


async def test_next_site_code_ignores_other_years(db_session, project_factory):
    project = await project_factory("K-6")
    db_session.add(Site(project_id=project.id, code="SNT-2025-050", name="Gecen yil"))
    await db_session.flush()

    assert await service._next_site_code(db_session) == f"{_prefix()}001"


async def test_explicit_code_is_not_overwritten(seeded_db, user_factory, project_factory):
    """Kullanici acikca kod verdiyse sessizce degistirilmez (mevcut davranis korunur)."""
    project = await project_factory("K-7")
    user = await _patron(seeded_db, user_factory, "k7@t.co")

    site = await service.create_site(
        seeded_db, user, project.id, SiteCreate(name="Ozel", code="OZEL-1", is_draft=True)
    )

    assert site.code == "OZEL-1"


def test_derive_code_removed():
    """Iki farkli kod deseni yan yana YASAMAZ (§3.2 karari)."""
    assert not hasattr(service, "derive_code")
    assert not hasattr(service, "_unique_code")


async def test_inline_site_creation_uses_new_generator(db_session):
    """P1.1a proje formundaki satir ici santiyeler de ayni ureticiyi kullanir."""
    project = await create_project(
        db_session,
        ProjectCreate(
            name="Şantiyeli",
            project_type="taahhut",
            is_draft=True,
            sites=[
                ProjectSiteInput(name="A-Blok Şantiyesi"),
                ProjectSiteInput(name="B-Blok Şantiyesi"),
            ],
        ),
    )

    assert sorted(s.code for s in project.sites) == [f"{_prefix()}001", f"{_prefix()}002"]


async def test_existing_site_codes_untouched(seeded_db, user_factory, project_factory):
    """Hicbir `UPDATE` yazilmadiginin kaniti: uretici kosunca eski kodlar yerinde durur."""
    project = await project_factory("K-8")
    seeded_db.add(Site(project_id=project.id, code="A-BLOK", name="Eski"))
    await seeded_db.flush()
    user = await _patron(seeded_db, user_factory, "k8@t.co")

    await service.create_site(seeded_db, user, project.id, SiteCreate(name="Yeni", is_draft=True))

    codes = (
        (await seeded_db.execute(select(Site.code).where(Site.project_id == project.id)))
        .scalars()
        .all()
    )
    assert "A-BLOK" in codes


async def test_explicit_duplicate_code_still_conflicts(seeded_db, user_factory, project_factory):
    """Cakismada otomatik yeniden deneme YOK (§8.3) — istek 409 ile reddedilir.

    T6'da istisna TIPI degisti: `IntegrityError` -> `DuplicateError`. Servis artik
    cakismaya acik bir SELECT ile ONCEDEN bakar (spec §7.2, `boq` emsali) ki
    kullanici genel "Veri bütünlüğü hatası" yerine alanina ozel Turkce mesaji
    gorsun. Ikisi de 409'a duser; `uq_sites_project_code` -> IntegrityError yolu
    YARIS DURUMU emniyet agi olarak yerinde KALIR. Erken yakalama ayrica
    atomikligin sartidir: bolumler session'a girmeden istek reddedilir.
    """
    project = await project_factory("K-9")
    seeded_db.add(Site(project_id=project.id, code="A-BLOK", name="Var olan"))
    await seeded_db.flush()
    user = await _patron(seeded_db, user_factory, "k9@t.co")

    with pytest.raises(DuplicateError):
        await service.create_site(
            seeded_db, user, project.id, SiteCreate(name="Kopya", code="A-BLOK", is_draft=True)
        )

"""Task 6 — santiye/bolum servis katmani (spec §3, §4.2, §4.3, §5.2)."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.discipline_scope import UNRESTRICTED
from app.core.errors import NotFoundError
from app.core.timezone import today
from app.modules.audit import messages
from app.modules.sites import service
from app.modules.sites.models import Section, SectionStatus, Site, SiteStatus
from app.modules.sites.schemas import SectionCreate, SectionUpdate, SiteCreate, SiteUpdate


async def _grant_all(session, user) -> None:
    user.all_projects = True
    await session.flush()


async def _patron(session, user_factory, email: str):
    user = await user_factory(email=email, password="parola1234", role_key="patron")
    await _grant_all(session, user)
    return user


async def _site(session, project, code: str = "A-BLOK", **kwargs) -> Site:
    site = Site(project_id=project.id, code=code, name=kwargs.pop("name", "A-Blok Şantiyesi"))
    for field, value in kwargs.items():
        setattr(site, field, value)
    session.add(site)
    await session.flush()
    return site


# --- remaining_days (spec §4.2) ---


async def test_remaining_days_is_null_without_end_date(seeded_db, user_factory, project_factory):
    project = await project_factory("S-1")
    await _site(seeded_db, project, end_date=None)
    user = await _patron(seeded_db, user_factory, "s1@t.co")

    card = (
        await service.list_sites_overview(seeded_db, user, project.id, scope=UNRESTRICTED)
    ).items[0]

    assert card.remaining_days is None


async def test_remaining_days_is_null_when_completed(seeded_db, user_factory, project_factory):
    """Tamamlanan kartta mockup "Teslim · Mayıs 2026" gosterir, kalan gun degil."""
    project = await project_factory("S-2")
    await _site(
        seeded_db,
        project,
        status=SiteStatus.completed,
        end_date=today() + timedelta(days=30),
    )
    user = await _patron(seeded_db, user_factory, "s2@t.co")

    card = (
        await service.list_sites_overview(seeded_db, user, project.id, scope=UNRESTRICTED)
    ).items[0]

    assert card.remaining_days is None


async def test_remaining_days_counts_forward(seeded_db, user_factory, project_factory):
    project = await project_factory("S-3")
    await _site(seeded_db, project, end_date=today() + timedelta(days=157))
    user = await _patron(seeded_db, user_factory, "s3@t.co")

    card = (
        await service.list_sites_overview(seeded_db, user, project.id, scope=UNRESTRICTED)
    ).items[0]

    assert card.remaining_days == 157


async def test_remaining_days_is_negative_when_overdue(seeded_db, user_factory, project_factory):
    """Spec §4.2: gecikme KIRPILMAZ, negatif doner — backend gercegi bastirmaz."""
    project = await project_factory("S-4")
    await _site(seeded_db, project, end_date=today() - timedelta(days=12))
    user = await _patron(seeded_db, user_factory, "s4@t.co")

    card = (
        await service.list_sites_overview(seeded_db, user, project.id, scope=UNRESTRICTED)
    ).items[0]

    assert card.remaining_days == -12


# --- city devralma (spec §4.3) ---


async def test_city_is_inherited_from_project_when_blank(seeded_db, user_factory, project_factory):
    project = await project_factory("S-5", city="Ankara")
    await _site(seeded_db, project, city=None)
    user = await _patron(seeded_db, user_factory, "s5@t.co")

    card = (
        await service.list_sites_overview(seeded_db, user, project.id, scope=UNRESTRICTED)
    ).items[0]

    assert card.city == "Ankara"
    assert card.city_inherited is True


async def test_own_city_wins_and_is_not_flagged(seeded_db, user_factory, project_factory):
    project = await project_factory("S-6", city="Ankara")
    await _site(seeded_db, project, city="Bursa")
    user = await _patron(seeded_db, user_factory, "s6@t.co")

    card = (
        await service.list_sites_overview(seeded_db, user, project.id, scope=UNRESTRICTED)
    ).items[0]

    assert card.city == "Bursa"
    assert card.city_inherited is False


async def test_city_stays_null_when_project_has_none(seeded_db, user_factory, project_factory):
    project = await project_factory("S-7", city=None)
    await _site(seeded_db, project, city=None)
    user = await _patron(seeded_db, user_factory, "s7@t.co")

    card = (
        await service.list_sites_overview(seeded_db, user, project.id, scope=UNRESTRICTED)
    ).items[0]

    assert card.city is None
    assert card.city_inherited is False


# --- sayaclar ve bolum katmani ---


async def test_site_counts_by_status(seeded_db, user_factory, project_factory):
    project = await project_factory("S-8")
    await _site(seeded_db, project, "A", status=SiteStatus.active)
    await _site(seeded_db, project, "B", status=SiteStatus.on_hold)
    await _site(seeded_db, project, "C", status=SiteStatus.completed)
    await _site(seeded_db, project, "D", status=SiteStatus.completed)
    user = await _patron(seeded_db, user_factory, "s8@t.co")

    result = await service.list_sites_overview(seeded_db, user, project.id, scope=UNRESTRICTED)

    assert result.counts.all == 4
    assert result.counts.active == 1
    assert result.counts.on_hold == 1
    assert result.counts.completed == 2


async def test_section_status_counts(seeded_db, user_factory, project_factory):
    project = await project_factory("S-9")
    site = await _site(seeded_db, project)
    for name, status in (
        ("B1", SectionStatus.active),
        ("B2", SectionStatus.active),
        ("B3", SectionStatus.active),
        ("B4", SectionStatus.planned),
        ("B5", SectionStatus.completed),
    ):
        seeded_db.add(Section(site_id=site.id, name=name, status=status))
    await seeded_db.flush()
    user = await _patron(seeded_db, user_factory, "s9@t.co")

    detail = await service.get_site_detail(seeded_db, user, site.id, scope=UNRESTRICTED)

    assert detail.section_count == 5
    assert detail.section_status_counts.planned == 1
    assert detail.section_status_counts.active == 3
    assert detail.section_status_counts.completed == 1


async def test_site_without_sections_is_valid(seeded_db, user_factory, project_factory):
    """Karar 4 (spec §2.4): otomatik "Genel" bolumu ACILMAZ."""
    project = await project_factory("S-10")
    site = await _site(seeded_db, project)
    user = await _patron(seeded_db, user_factory, "s10@t.co")

    detail = await service.get_site_detail(seeded_db, user, site.id, scope=UNRESTRICTED)

    assert detail.sections == []
    assert detail.section_count == 0
    assert detail.section_status_counts.planned == 0


async def test_detail_carries_project_summary(seeded_db, user_factory, project_factory):
    project = await project_factory(
        "S-11", name="Güneşkent", city="Ankara", employer_name="GK A.Ş."
    )
    site = await _site(seeded_db, project)
    user = await _patron(seeded_db, user_factory, "s11@t.co")

    detail = await service.get_site_detail(seeded_db, user, site.id, scope=UNRESTRICTED)

    assert detail.project.name == "Güneşkent"
    assert detail.project.employer_name == "GK A.Ş."


# --- yer tutucular (spec §3) ---


async def test_list_placeholders_use_correct_pending_modules(
    seeded_db, user_factory, project_factory
):
    project = await project_factory("S-12")
    await _site(seeded_db, project)
    user = await _patron(seeded_db, user_factory, "s12@t.co")

    result = await service.list_sites_overview(seeded_db, user, project.id, scope=UNRESTRICTED)
    card = result.items[0]

    # T4 (puantaj §4): `worker_count` artik YER TUTUCU DEGIL — puantaj kaydi
    # olmayan santiyede bile `available` true'dur ve sayi UYDURULMAZ, 0'dir.
    # Baglama davranisinin tamami `tests/timesheet/test_worker_count_binding.py`de.
    assert card.worker_count.available is True
    assert card.worker_count.count == 0
    assert card.worker_count.pending_module == "timesheet"
    # ⚠️ ILR-1: SANTIYE karti yuzdesi BAGLANDI, sahibi GUNLUK. Bu santiyenin
    # BOQ'u bos → payda YOK → zarf bos ama anahtari `site_diary`dir.
    assert card.progress_pct.available is False
    assert card.progress_pct.pending_module == "site_diary"
    assert result.totals.total_progress_payment.pending_module == "progress_payments"
    assert result.totals.subcontractor_count.pending_module == "subcontracts"
    # T4: alt KPI seridinde YALNIZ `active_worker_count` baglandi.
    assert result.totals.active_worker_count.available is True
    assert result.totals.active_worker_count.count == 0
    assert result.totals.active_worker_count.pending_module == "timesheet"
    assert result.totals.average_margin.pending_module == "project_costs"
    assert all(
        not placeholder.available
        for placeholder in (
            result.totals.total_progress_payment,
            result.totals.subcontractor_count,
            result.totals.average_margin,
        )
    )


async def test_detail_and_section_placeholders(seeded_db, user_factory, project_factory):
    project = await project_factory("S-13")
    site = await _site(seeded_db, project)
    seeded_db.add(Section(site_id=site.id, name="Kat 6-10"))
    await seeded_db.flush()
    user = await _patron(seeded_db, user_factory, "s13@t.co")

    detail = await service.get_site_detail(seeded_db, user, site.id, scope=UNRESTRICTED)
    section = detail.sections[0]

    assert detail.total_progress_payment.pending_module == "progress_payments"
    assert detail.contract_amount.pending_module == "contracts"
    assert detail.contract_amount.available is False
    # ⚠️ ILR-1: bolum yuzdesi BAGLANDI; tahsis yok → payda yok → bos, anahtar
    # `site_diary`. (Izin yoklugu OLSAYDI `pending_module is None` olurdu.)
    assert section.progress_pct.pending_module == "site_diary"
    assert section.progress_pct.available is False
    # 🔴 BLM-SAY: BOQ ikilisi BAGLANDI. Tahsisi olmayan bolumde ikisi de
    # OLCULMUS sifirdir (yer tutucu DEGIL) — `worker_count` emsalinin ayni.
    # `CountPlaceholder` dolu hâlde `pending_module` TASIR, `MetricPlaceholder`
    # TASIMAZ (P10 T3): iki zarf sinifinin kurallari FARKLIDIR.
    assert section.boq_item_count.available is True
    assert section.boq_item_count.count == 0
    assert section.boq_item_count.pending_module == "boq"
    assert section.budget.available is True
    assert section.budget.value == Decimal("0.00")
    assert section.budget.pending_module is None
    # T4: bolum sayaci da BAGLANDI — kaydi olmayan bolumde `available` true, sayi 0.
    assert section.worker_count.available is True
    assert section.worker_count.count == 0
    assert section.worker_count.pending_module == "timesheet"


# --- gorunurluk (spec §5.2) ---


async def test_list_for_invisible_project_raises_not_found(
    seeded_db, user_factory, project_factory
):
    hidden = await project_factory("S-14")
    await _site(seeded_db, hidden)
    user = await user_factory(email="s14@t.co", password="parola1234", role_key="patron")

    with pytest.raises(NotFoundError):
        await service.list_sites_overview(seeded_db, user, hidden.id, scope=UNRESTRICTED)


async def test_detail_of_invisible_project_site_raises_not_found(
    seeded_db, user_factory, project_factory
):
    hidden = await project_factory("S-15")
    site = await _site(seeded_db, hidden)
    user = await user_factory(email="s15@t.co", password="parola1234", role_key="patron")

    with pytest.raises(NotFoundError):
        await service.get_site_detail(seeded_db, user, site.id, scope=UNRESTRICTED)


async def test_section_list_of_invisible_site_raises_not_found(
    seeded_db, user_factory, project_factory
):
    hidden = await project_factory("S-16")
    site = await _site(seeded_db, hidden)
    user = await user_factory(email="s16@t.co", password="parola1234", role_key="patron")

    with pytest.raises(NotFoundError):
        await service.list_sections_for_site(seeded_db, user, site.id, scope=UNRESTRICTED)


async def test_update_section_of_invisible_site_raises_not_found(
    seeded_db, user_factory, project_factory
):
    """EN KRITIK DOLAYLI ERISIM (spec §5.2): bolum -> santiye -> proje cozulmeli."""
    hidden = await project_factory("S-17")
    site = await _site(seeded_db, hidden)
    section = Section(site_id=site.id, name="Gizli Bölüm")
    seeded_db.add(section)
    await seeded_db.flush()
    user = await user_factory(email="s17@t.co", password="parola1234", role_key="patron")

    with pytest.raises(NotFoundError):
        await service.update_section(seeded_db, user, section.id, SectionUpdate(name="Sızdı"))


async def test_missing_ids_raise_not_found(seeded_db, user_factory):
    user = await _patron(seeded_db, user_factory, "s18@t.co")

    with pytest.raises(NotFoundError):
        await service.get_site_detail(seeded_db, user, uuid.uuid4(), scope=UNRESTRICTED)
    with pytest.raises(NotFoundError):
        await service.update_section(seeded_db, user, uuid.uuid4(), SectionUpdate(name="X"))
    with pytest.raises(NotFoundError):
        await service.list_sites_overview(seeded_db, user, uuid.uuid4(), scope=UNRESTRICTED)


# --- yazma ---


async def test_create_site_generates_code_when_omitted(seeded_db, user_factory, project_factory):
    """Kod verilmezse SNT-{YYYY}-{NNN} uretilir (spec §3.2; ad-turevi uretici kaldirildi)."""
    from app.core.timezone import today

    project = await project_factory("S-19")
    user = await _patron(seeded_db, user_factory, "s19@t.co")

    # `is_draft=True` T6'da eklendi: taslak-disi POST zorunluluk kurallarini kosar
    # (spec §5.1/7-10). Bu test KOD URETICISINI sinar, form zorunlulugunu degil.
    site = await service.create_site(
        seeded_db, user, project.id, SiteCreate(name="A-Blok Şantiyesi", is_draft=True)
    )

    assert site.code == f"SNT-{today().year}-001"
    assert site.status is SiteStatus.active


async def test_create_site_keeps_explicit_code(seeded_db, user_factory, project_factory):
    project = await project_factory("S-20")
    user = await _patron(seeded_db, user_factory, "s20@t.co")

    site = await service.create_site(
        seeded_db,
        user,
        project.id,
        SiteCreate(name="A-Blok Şantiyesi", code="A-BLOK", is_draft=True),
    )

    assert site.code == "A-BLOK"


async def test_create_site_in_invisible_project_raises_not_found(
    seeded_db, user_factory, project_factory
):
    hidden = await project_factory("S-21")
    user = await user_factory(email="s21@t.co", password="parola1234", role_key="patron")

    with pytest.raises(NotFoundError):
        await service.create_site(seeded_db, user, hidden.id, SiteCreate(name="Sızıntı"))


async def test_update_site_changes_fields(seeded_db, user_factory, project_factory):
    project = await project_factory("S-22")
    site = await _site(seeded_db, project, name="Eski Ad")
    user = await _patron(seeded_db, user_factory, "s22@t.co")

    updated, _ = await service.update_site(
        seeded_db, user, site.id, SiteUpdate(name="Yeni Ad", status=SiteStatus.on_hold)
    )

    assert updated.name == "Yeni Ad"
    assert updated.status is SiteStatus.on_hold
    assert updated.code == "A-BLOK"


async def test_create_section_defaults_to_planned(seeded_db, user_factory, project_factory):
    project = await project_factory("S-23")
    site = await _site(seeded_db, project)
    user = await _patron(seeded_db, user_factory, "s23@t.co")

    # `is_draft=True`: bu test yalnizca DURUM VARSAYILANINI olcer. P6 T3'ten beri
    # taslak-disi bir bolum `Form - Bolum Ekle`in `*` alanlarini ister; onlari
    # buraya tasimak testin konusunu degistirirdi (bkz. `test_section_create.py`).
    section = await service.create_section(
        seeded_db, user, site.id, SectionCreate(name="Kat 6-10", is_draft=True)
    )

    assert section.status is SectionStatus.planned
    assert section.site_id == site.id


async def test_update_section_changes_fields(seeded_db, user_factory, project_factory):
    project = await project_factory("S-24")
    site = await _site(seeded_db, project)
    section = Section(site_id=site.id, name="Eski")
    seeded_db.add(section)
    await seeded_db.flush()
    user = await _patron(seeded_db, user_factory, "s24@t.co")

    # P6 T5: servis denetim metnini DE doner (`update_site` deseni) — yayina
    # gecis ayrimi yalniz burada gorunur.
    updated, detail = await service.update_section(
        seeded_db, user, section.id, SectionUpdate(name="Yeni", status=SectionStatus.active)
    )

    assert updated.name == "Yeni"
    assert updated.status is SectionStatus.active
    assert detail == messages.section_updated(site.name, "Yeni")


async def test_section_list_response_counts(seeded_db, user_factory, project_factory):
    project = await project_factory("S-25")
    site = await _site(seeded_db, project)
    seeded_db.add(Section(site_id=site.id, name="B1", status=SectionStatus.active, sort_order=1))
    seeded_db.add(Section(site_id=site.id, name="B2", status=SectionStatus.planned, sort_order=2))
    await seeded_db.flush()
    user = await _patron(seeded_db, user_factory, "s25@t.co")

    result = await service.list_sections_for_site(seeded_db, user, site.id, scope=UNRESTRICTED)

    assert [s.name for s in result.items] == ["B1", "B2"]
    assert result.counts.active == 1
    assert result.counts.planned == 1
    assert result.counts.completed == 0

"""Okuma uclari (spec §4-§6): liste + detay derleyicileri."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

# BOLUM BOQ SAYACLARININ (`boq_item_count` · `budget`) TEK kaynagi (BLM-SAY):
# `timesheet` ile ayni gerekce — `sites` kendi tahsis sorgusunu yazmaz.
from app.core.discipline_scope import DisciplineScope
from app.core.permissions import can_read
from app.modules.boq import counts as boq_counts
from app.modules.boq import progress as boq_progress
from app.modules.projects.models import Project
from app.modules.projects.schemas import MetricPlaceholder, metric
from app.modules.sites import repository
from app.modules.sites.models import Section, Site
from app.modules.sites.schemas import (
    SectionDetailResponse,
    SectionListResponse,
    SiteDetailResponse,
    SiteListResponse,
)
from app.modules.sites.service.presenters import (
    _section_counts,
    _site_counts,
    _totals,
    to_card,
    to_detail,
    to_section,
    to_section_detail,
)
from app.modules.sites.service.visibility import _visible_project, _visible_section, _visible_site

# Isci sayaclarinin TEK kaynagi puantaj modulüdur (T4, spec §4): bu modul kendi
# `SELECT`ini yazmaz, aksi halde santiye karti ile proje karti ayni ayda farkli
# sayi gosterir. Donem karari (icinde bulunulan ay) orada gerekcelenmistir.
from app.modules.timesheet import counts as timesheet_counts
from app.modules.users.models import User


async def list_sites_overview(
    session: AsyncSession, actor: User, project_id: uuid.UUID, scope: DisciplineScope
) -> SiteListResponse:
    """Isci sayaclari IKI TOPLU sorgudan gelir (santiye kirilimi + proje toplami).

    Kart basina sorgu KOSULMAZ (N+1 yok) ve alt KPI seridi kart sayaclarinin
    TOPLAMI DEGILDIR: iki santiyede birden calisan kisi projede BIR kez sayilir.
    """
    project = await _visible_project(session, actor, project_id)
    sites = await repository.list_sites_for_project(session, project_id)
    site_ids = [site.id for site in sites]
    worker_counts = await timesheet_counts.by_site(session, site_ids)
    project_counts = await timesheet_counts.by_project(session, [project.id])
    card_progress = await site_progress_map(session, actor, site_ids, scope)
    return SiteListResponse(
        counts=_site_counts(sites),
        items=[
            to_card(site, project, worker_counts.get(site.id, 0), card_progress.get(site.id))
            for site in sites
        ],
        totals=_totals(project_counts.get(project.id, 0)),
    )


# --------------------------------------------------------------------------- #
# ILR-1 — bolum FIZIKSEL ilerlemesi (izne duyarli)
# --------------------------------------------------------------------------- #

_SITE_DIARY = "site_diary"


async def section_progress_map(
    session: AsyncSession, actor: User, section_ids: list[uuid.UUID], scope: DisciplineScope
) -> dict[uuid.UUID, MetricPlaceholder]:
    """Bolum -> fiziksel ilerleme zarfi. **TEK sorgu** (N+1 yok).

    🔴 K4: `sites`i okuyup `site_diary`yi okuyamayan roller VAR (olculdu:
    `accounting`, `hr_manager`, `procurement`). Izin yoksa BOS sozluk doner ve
    `to_section` varsayilani `restricted()`e duser — yani gunlukten turemis
    hicbir sayi o role ULASMAZ ve zarf sahte bir gerekce de SOYLEMEZ.
    """
    if not section_ids or not await can_read(session, actor, _SITE_DIARY):
        return {}
    yuzdeler = await boq_progress.physical_for_sections(session, section_ids, scope)
    return {sid: metric(pct, _SITE_DIARY) for sid, pct in yuzdeler.items()}


async def site_progress_map(
    session: AsyncSession, actor: User, site_ids: list[uuid.UUID], scope: DisciplineScope
) -> dict[uuid.UUID, MetricPlaceholder]:
    """Santiye -> fiziksel ilerleme zarfi. `section_progress_map`in kardesi;
    ayni izin kapisina (K4) bakar, yalniz kapsami SANTIYE'dir."""
    if not site_ids or not await can_read(session, actor, _SITE_DIARY):
        return {}
    yuzdeler = await boq_progress.physical_for_sites(session, site_ids, scope)
    return {sid: metric(pct, _SITE_DIARY) for sid, pct in yuzdeler.items()}


async def build_site_detail(
    session: AsyncSession,
    site: Site,
    actor: User,
    project: Project,
    scope: DisciplineScope,
) -> SiteDetailResponse:
    """Santiye detay zarfi + isci sayaclari. YAZMA uclarinin yaniti da buradan
    gecer: okuma ve yazma ayni zarfi tasimazsa ekran kaydettikten sonra sayaci
    kaybeder."""
    site_counts = await timesheet_counts.by_site(session, [site.id])
    section_ids = [s.id for s in site.sections]
    section_counts = await timesheet_counts.by_section(session, section_ids)
    section_boq = await boq_counts.by_section(session, section_ids, scope)
    # Milestone koleksiyonu SENKRON donusturucuye girmeden ONCE yuklenir
    # (gerekcesi `repository.ensure_milestones_loaded` docstring'inde).
    await repository.ensure_milestones_loaded(session, site.sections)
    section_progress = await section_progress_map(session, actor, section_ids, scope)
    site_progress = await site_progress_map(session, actor, [site.id], scope)
    return to_detail(
        site,
        project,
        site_counts.get(site.id, 0),
        section_counts,
        section_boq,
        section_progress,
        site_progress.get(site.id),
    )


async def build_section_detail(
    session: AsyncSession, section: Section, actor: User, scope: DisciplineScope
) -> SectionDetailResponse:
    section_counts = await timesheet_counts.by_section(session, [section.id])
    section_boq = await boq_counts.by_section(session, [section.id], scope)
    await repository.ensure_milestones_loaded(session, [section])
    section_progress = await section_progress_map(session, actor, [section.id], scope)
    return to_section_detail(
        section,
        section_counts.get(section.id, 0),
        section_boq.get(section.id, boq_counts.EMPTY),
        section_progress.get(section.id),
    )


async def get_site_detail(
    session: AsyncSession,
    actor: User,
    site_ref: uuid.UUID | str,
    scope: DisciplineScope,
    *,
    project_ref: uuid.UUID | str | None = None,
) -> SiteDetailResponse:
    """URL-2: `site_ref` UUID ya da slug. Kapsam ve fail-closed kurali
    `_visible_site` docstring'indedir — burada kopyalanmaz."""
    site, project = await _visible_site(session, actor, site_ref, project_ref=project_ref)
    return await build_site_detail(session, site, actor, project, scope)


async def list_sections_for_site(
    session: AsyncSession, actor: User, site_id: uuid.UUID, scope: DisciplineScope
) -> SectionListResponse:
    site, _ = await _visible_site(session, actor, site_id)
    sections = await repository.list_sections(session, site.id)
    section_ids = [s.id for s in sections]
    section_counts = await timesheet_counts.by_section(session, section_ids)
    section_boq = await boq_counts.by_section(session, section_ids, scope)
    await repository.ensure_milestones_loaded(session, sections)
    section_progress = await section_progress_map(session, actor, section_ids, scope)
    return SectionListResponse(
        counts=_section_counts(sections),
        items=[
            to_section(
                s,
                section_counts.get(s.id, 0),
                section_boq.get(s.id, boq_counts.EMPTY),
                section_progress.get(s.id),
            )
            for s in sections
        ],
    )


async def get_section_detail(
    session: AsyncSession,
    actor: User,
    section_ref: uuid.UUID | str,
    scope: DisciplineScope,
    *,
    site_ref: uuid.UUID | str | None = None,
    project_ref: uuid.UUID | str | None = None,
) -> SectionDetailResponse:
    """P6 §5 — `GET /sections/{section_id}`.

    Gorunurluk suzgeci `_visible_section`tir (bolum -> santiye -> proje):
    OKUMA ucu de YENI BIR IDOR YUZEYIDIR. Kendi erisim mantigini yazmaz,
    silme/guncelleme uclariyla AYNI fonksiyonu cagirir — iki ayri suzgec zamanla
    ayrisir ve ayrisan taraf sessiz bir yetki sizintisi olur.

    Gorunmeyen bolum 404 `Bölüm bulunamadı` doner ve govdesi var olmayan bir
    UUID'ninkiyle BIREBIR AYNIDIR.
    """
    section, _ = await _visible_section(
        session, actor, section_ref, site_ref=site_ref, project_ref=project_ref
    )
    return await build_section_detail(session, section, actor, scope)

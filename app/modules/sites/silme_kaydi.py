"""Şantiye ve bölüm türlerinin silme motoru kaydı (SIL-B1); `silme/kayitlar.py` ithal eder."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.silme.turler import KokBilgisi, SilmeTuru, tur_kaydet
from app.modules.audit import messages
from app.modules.projects.models import Project
from app.modules.sites import guards
from app.modules.sites.models import Section, Site


async def _site_oku(session: AsyncSession, site_id: uuid.UUID) -> KokBilgisi | None:
    site = await session.get(Site, site_id)
    if site is None:
        return None
    project = await session.get(Project, site.project_id)
    proje_adi = project.name if project is not None else ""
    return KokBilgisi(ad=site.name, denetim_metni=messages.site_deleted(proje_adi, site.name))


async def _bolum_oku(session: AsyncSession, section_id: uuid.UUID) -> KokBilgisi | None:
    section = await session.get(Section, section_id)
    if section is None:
        return None
    site = await session.get(Site, section.site_id)
    site_adi = site.name if site is not None else ""
    return KokBilgisi(
        ad=section.name, denetim_metni=messages.section_deleted(site_adi, section.name)
    )


tur_kaydet(
    SilmeTuru(
        anahtar="site",
        tablo="sites",
        etiket="Şantiye",
        bulunamadi=guards.SITE_MISSING,
        kok_oku=_site_oku,
    )
)
tur_kaydet(
    SilmeTuru(
        anahtar="section",
        tablo="sections",
        etiket="Bölüm",
        bulunamadi=guards.SECTION_MISSING,
        kok_oku=_bolum_oku,
    )
)

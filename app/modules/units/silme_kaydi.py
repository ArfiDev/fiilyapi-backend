"""Blok ve ünite türlerinin silme motoru kaydı (SIL-B1); `silme/kayitlar.py` ithal eder."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.silme.turler import KokBilgisi, SilmeTuru, tur_kaydet
from app.modules.audit import messages
from app.modules.projects.models import Project
from app.modules.units import guards
from app.modules.units.models import Block, Unit


async def _proje_adi(session: AsyncSession, project_id: uuid.UUID) -> str:
    project = await session.get(Project, project_id)
    return project.name if project is not None else ""


async def _blok_oku(session: AsyncSession, block_id: uuid.UUID) -> KokBilgisi | None:
    block = await session.get(Block, block_id)
    if block is None:
        return None
    proje_adi = await _proje_adi(session, block.project_id)
    return KokBilgisi(ad=block.name, denetim_metni=messages.block_deleted(proje_adi, block.name))


async def _unite_oku(session: AsyncSession, unit_id: uuid.UUID) -> KokBilgisi | None:
    unit = await session.get(Unit, unit_id)
    if unit is None:
        return None
    block = await session.get(Block, unit.block_id)
    proje_adi = await _proje_adi(session, unit.project_id)
    blok_adi = block.name if block is not None else ""
    return KokBilgisi(
        ad=unit.unit_no,
        denetim_metni=messages.unit_deleted(proje_adi, blok_adi, unit.unit_no),
    )


tur_kaydet(
    SilmeTuru(
        anahtar="block",
        tablo="blocks",
        etiket="Blok",
        bulunamadi=guards.BLOCK_MISSING,
        kok_oku=_blok_oku,
    )
)
tur_kaydet(
    SilmeTuru(
        anahtar="unit",
        tablo="units",
        etiket="Ünite",
        bulunamadi=guards.UNIT_MISSING,
        kok_oku=_unite_oku,
    )
)

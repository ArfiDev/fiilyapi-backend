import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.projects.models import Employer, Project

# units.models yalniz app.core.db'yi import eder — cembersel import YOK.
from app.modules.units.models import Unit
from app.modules.users.models import UserProjectAccess


async def list_employers(session: AsyncSession, q: str | None, active_only: bool) -> list[Employer]:
    """Ada gore ILIKE suzgeci + aktiflik; siralama DB'de (ORDER BY name), istemcide degil."""
    stmt = select(Employer)
    if active_only:
        stmt = stmt.where(Employer.is_active.is_(True))
    if q:
        stmt = stmt.where(Employer.name.ilike(f"%{q}%"))
    stmt = stmt.order_by(Employer.name)
    return list((await session.execute(stmt)).scalars().all())


async def get_employer(session: AsyncSession, employer_id: uuid.UUID) -> Employer | None:
    return await session.get(Employer, employer_id)


async def get_employer_by_tax_number(session: AsyncSession, tax_number: str) -> Employer | None:
    return (
        await session.execute(select(Employer).where(Employer.tax_number == tax_number))
    ).scalar_one_or_none()


async def add_employer(session: AsyncSession, employer: Employer) -> Employer:
    session.add(employer)
    await session.flush()
    await session.refresh(employer)
    return employer


async def list_projects(session: AsyncSession) -> list[Project]:
    result = await session.execute(select(Project).order_by(Project.code))
    return list(result.scalars().all())


async def get_project(session: AsyncSession, project_id: uuid.UUID) -> Project | None:
    return await session.get(Project, project_id)


async def list_codes_with_prefix(session: AsyncSession, prefix: str) -> list[str]:
    """Verilen önekle başlayan tüm proje kodları (otomatik kod üretimi için, spec §3.5)."""
    stmt = select(Project.code).where(Project.code.like(f"{prefix}%"))
    return list((await session.execute(stmt)).scalars().all())


async def project_code_exists(session: AsyncSession, code: str) -> bool:
    """`projects.code` (benzersiz) zaten kullanılıyor mu."""
    return (await session.scalar(select(Project.id).where(Project.code == code))) is not None


async def list_projects_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[Project]:
    """Kullanicinin user_project_access satirlarina gore gorunur projeler.

    all_projects=True satiri varsa tumu doner; yoksa yalnizca verilen project_id'ler.
    Hic satir yoksa bos liste. Siralama code artan.
    """
    access_rows = (
        (
            await session.execute(
                select(UserProjectAccess).where(UserProjectAccess.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )

    if not access_rows:
        return []

    if any(row.all_projects for row in access_rows):
        return await list_projects(session)

    project_ids = [row.project_id for row in access_rows if row.project_id is not None]
    if not project_ids:
        return []

    result = await session.execute(
        select(Project).where(Project.id.in_(project_ids)).order_by(Project.code)
    )
    return list(result.scalars().all())


async def shareholder_ids_with_units(
    session: AsyncSession, shareholder_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """Verilen hissedarlardan HANGILERINE unite atanmis oldugunu doner (P9 spec §4.1).

    Tek sorgu (DISTINCT) — hissedar basina sorgu ACILMAZ. `units.shareholder_id`
    icin `relationship` kurulmadigi (P9 spec §3) icin bag ACIK sorguyla okunur.
    """
    if not shareholder_ids:
        return set()
    result = await session.execute(
        select(Unit.shareholder_id).where(Unit.shareholder_id.in_(shareholder_ids)).distinct()
    )
    return {row for row in result.scalars().all() if row is not None}

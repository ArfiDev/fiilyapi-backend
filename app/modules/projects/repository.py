import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.projects.models import Employer, Project

# units.models yalniz app.core.db'yi import eder — cembersel import YOK.
from app.modules.units.models import Unit
from app.modules.users.models import ProjectMember, User


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


async def list_member_projects(
    session: AsyncSession, user_id: uuid.UUID
) -> list[tuple[Project, uuid.UUID]]:
    """Kullanıcının EKİBİNDE olduğu projeler + o projedeki rol kimliği (IZN-B3). `code` artan.

    Üyelik `project_members` satırıdır; "Tüm projeler" ve Sistem Yöneticisi kararı çağırandadır
    (`projects.service.visible_projects`). Hiç satır yoksa boş liste.
    """
    rows = await session.execute(
        select(Project, ProjectMember.role_id)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user_id)
        .order_by(Project.code)
    )
    return [(project, role_id) for project, role_id in rows.all()]


async def list_projects_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[Project]:
    """Kullanıcının projeleri (IZN-B3): `users.all_projects` → hepsi, aksi hâlde ekip satırları.

    Sistem Yöneticisi ayrıcalığı YOK (panelin eski kuralı: yalnız işaret/üyelik); rol düzeyi
    süzgeci de yok. Kesin görünürlük `projects.service.visible_projects`tir. `code` artan.
    """
    all_projects = await session.scalar(select(User.all_projects).where(User.id == user_id))
    if all_projects:
        return await list_projects(session)
    return [project for project, _role_id in await list_member_projects(session, user_id)]


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

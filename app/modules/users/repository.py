import uuid

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager, joinedload

from app.modules.roles.models import Role
from app.modules.users.models import ProjectMember, User

#: Türkçe-güvenli arama katlaması (SQL `translate` + Python `str.translate` AYNI tablo): `İ I ı i`
#: hepsi `i`, `Ş ş`→`s`, `Ğ ğ`→`g`, `Ü ü`→`u`, `Ö ö`→`o`, `Ç ç`→`c`. PostgreSQL `lower`/`ILIKE`
#: C ya da en_US harmanlamasında `İ`/`ı`yı katlamaz; bu yüzden iki yan da önce KATLANIR.
_TR_FROM = "İIıŞşĞğÜüÖöÇç"
_TR_TO = "iiissgguuoocc"
_TR_FOLD = {ord(src): dst for src, dst in zip(_TR_FROM, _TR_TO, strict=True)}


def fold_search_text(text: str) -> str:
    """Arama metnini katlar: Türkçe harfler ASCII karşılığına, ASCII harfler küçüğe."""
    folded = text.translate(_TR_FOLD)
    return "".join(c.lower() if c.isascii() else c for c in folded)


def _folded(column: ColumnElement[str]) -> ColumnElement[str]:
    return func.lower(func.translate(column, _TR_FROM, _TR_TO))


def _search_filter(q: str | None) -> ColumnElement[bool] | None:
    """`q` boşsa süzgeç yok; doluysa ad / e-posta / ANA rol adında KATLANMIŞ içerir araması."""
    needle = fold_search_text((q or "").strip())
    if not needle:
        return None
    return or_(
        *(
            _folded(column).contains(needle, autoescape=True)
            for column in (User.full_name, User.email, Role.name)
        )
    )


async def list_users(
    session: AsyncSession, limit: int = 50, offset: int = 0, q: str | None = None
) -> list[User]:
    stmt = select(User).join(Role, Role.id == User.role_id).options(contains_eager(User.role))
    condition = _search_filter(q)
    if condition is not None:
        stmt = stmt.where(condition)
    result = await session.execute(
        stmt.order_by(User.full_name, User.id).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


async def count_users(session: AsyncSession, q: str | None = None) -> int:
    stmt = select(func.count()).select_from(User).join(Role, Role.id == User.role_id)
    condition = _search_filter(q)
    if condition is not None:
        stmt = stmt.where(condition)
    return (await session.execute(stmt)).scalar_one()


async def project_counts(session: AsyncSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Kullanıcı başına proje ekibi satır sayısı — TEK `COUNT … GROUP BY` (N+1 yok)."""
    if not user_ids:
        return {}
    rows = await session.execute(
        select(ProjectMember.user_id, func.count())
        .where(ProjectMember.user_id.in_(user_ids))
        .group_by(ProjectMember.user_id)
    )
    return {user_id: count for user_id, count in rows.all()}


async def role_labels(
    session: AsyncSession, role_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, str]]:
    """Rol kimliği → (görünen ad, anahtar) — TEK sorgu; `User.role` (lazy="raise") yüklü
    olmasa da çalışır."""
    if not role_ids:
        return {}
    rows = await session.execute(select(Role.id, Role.name, Role.key).where(Role.id.in_(role_ids)))
    return {role_id: (name, key) for role_id, name, key in rows.all()}


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await session.execute(
        select(User).options(joinedload(User.role)).where(User.id == user_id)
    )
    return result.scalar_one_or_none()


async def get_user_locked(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """`SELECT … FOR UPDATE` — erişimin (ana rol + ekip + disiplin) TAM-DEĞİŞTİRME giriş kapısı.

    `joinedload(User.role)` BİLEREK YOK: `FOR UPDATE` bir OUTER JOIN ile
    birleştirilemez (PostgreSQL `FOR UPDATE cannot be applied to the nullable
    side of an outer join` der). Kilidin işi rolü okumak değil, aynı kullanıcının
    erişim satırlarını yazan iki isteği serileştirmektir.
    """
    result = await session.execute(select(User).where(User.id == user_id).with_for_update())
    return result.scalar_one_or_none()


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def add_user(session: AsyncSession, user: User) -> User:
    session.add(user)
    await session.flush()
    return user

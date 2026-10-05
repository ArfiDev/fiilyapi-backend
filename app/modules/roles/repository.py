import uuid
from dataclasses import dataclass

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import SYSTEM_ADMIN_ROLE_KEY, AccessLevel, Scope
from app.core.page_gate import display_level, load_cells
from app.core.sayfalar import HiddenCategory, PageLevel
from app.modules.roles.models import (
    Module,
    Role,
    RoleHiddenField,
    RolePagePermission,
    RolePermission,
)


async def get_permission(
    session: AsyncSession, role_id: uuid.UUID, module_key: str
) -> RolePermission | None:
    stmt = (
        select(RolePermission)
        .join(Module, Module.id == RolePermission.module_id)
        .where(RolePermission.role_id == role_id, Module.key == module_key)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_roles(session: AsyncSession) -> list[Role]:
    result = await session.execute(select(Role).order_by(Role.is_system.desc(), Role.name))
    return list(result.scalars().all())


async def get_role(session: AsyncSession, role_id: uuid.UUID) -> Role | None:
    return await session.get(Role, role_id)


async def get_module(session: AsyncSession, module_key: str) -> Module | None:
    stmt = select(Module).where(Module.key == module_key)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_modules(session: AsyncSession) -> list[Module]:
    result = await session.execute(select(Module).order_by(Module.sort_order))
    return list(result.scalars().all())


async def get_role_matrix(
    session: AsyncSession, role_id: uuid.UUID
) -> list[tuple[Module, RolePermission]]:
    """Rolün matrisi — HER modül için bir hücre, izin satırı olmasa bile.

    🔴 Bu fonksiyon eskiden INNER JOIN'di ve `modules`ı değil `role_permissions`ı
    sürüyordu. Satırı olmayan modül matristen TAMAMEN DÜŞÜYORDU; belirti Ayarlar
    ekranı değil `/auth/me` idi — `permissions` haritasında anahtar HİÇ bulunmuyordu.

    Delik yapısaldır: `create_custom_role` yalnız o anda var olan modüller için
    hücre açar, uzantı migration'ları ise izin satırlarını `WHERE r.key = :role_key`
    süzgeciyle ve sabit `ROLE_ORDER` üzerinde yazar (tek istisna `ai`). Yani
    migration'dan ÖNCE açılmış her özel rol, sonradan inen her modülün dışında kalır.

    Artık sürücü `modules`tır (LEFT OUTER JOIN) ve eksik hücre yerine VARSAYILAN
    KAPALI bir hücre üretilir. Üretilen nesne `session.add` EDİLMEZ: okuma yolu
    yazmaz, yoksa `uq_role_module` yarışında çift satır doğardı. Kalıcı hücreyi
    yalnız `service.update_role_permission` açar.
    """
    stmt = (
        select(Module, RolePermission)
        .outerjoin(
            RolePermission,
            (RolePermission.module_id == Module.id) & (RolePermission.role_id == role_id),
        )
        .order_by(Module.sort_order)
    )
    result = await session.execute(stmt)
    return [
        (module, permission or varsayilan_hucre(role_id, module))
        for module, permission in result.all()
    ]


def varsayilan_hucre(role_id: uuid.UUID, module: Module) -> RolePermission:
    """İzin satırı olmayan (rol, modül) çifti için GEÇİCİ, kalıcılaşmayan hücre.

    Varsayılan `none`dur — `core/permissions.py` zaten satır yokken reddediyordu,
    bu hücre o davranışı AYNEN temsil eder; matrise görünürlük ekler, yetki eklemez.
    """
    return RolePermission(
        role_id=role_id,
        module_id=module.id,
        access_level=AccessLevel.none,
        scope=Scope.all,
    )


async def list_role_page_cells(
    session: AsyncSession, role_id: uuid.UUID
) -> list[RolePagePermission]:
    """Rolün sayfa hücreleri — TEK sorgu (`/auth/me` yolu; rol başına ≤100 satır)."""
    result = await session.execute(
        select(RolePagePermission)
        .where(RolePagePermission.role_id == role_id)
        .order_by(RolePagePermission.page_key)
    )
    return list(result.scalars().all())


async def list_role_hidden_categories(
    session: AsyncSession, role_id: uuid.UUID
) -> list[HiddenCategory]:
    """Rolün gizlediği hassas alan kategorileri — TEK sorgu."""
    result = await session.execute(
        select(RoleHiddenField.category).where(RoleHiddenField.role_id == role_id)
    )
    return sorted(result.scalars().all(), key=lambda c: c.value)


async def has_legacy_cells(session: AsyncSession, role_id: uuid.UUID) -> bool:
    count = (
        await session.execute(
            select(func.count())
            .select_from(RolePermission)
            .where(RolePermission.role_id == role_id)
        )
    ).scalar_one()
    return count > 0


async def role_user_counts(
    session: AsyncSession, role_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Roller için FARKLI kullanıcı sayısı — TEK sorgu (IZN-B3).

    Bir kullanıcı bir role ANA rolü olarak YA DA bir proje ekibi satırındaki rolü olarak bağlıysa
    o rolde sayılır; iki yoldan da bağlı (ya da birkaç projede aynı rolde) kişi TEK kez sayılır
    (`UNION` tekilleştirir). `0` ⇔ rol silinebilir; `project_members.role_id` RESTRICT olduğu için
    bu sayı FK'nin de aynasıdır.
    """
    if not role_ids:
        return {}
    from app.modules.users.models import ProjectMember, User  # roles ↔ users döngüsünü önler

    pairs = (
        select(User.role_id.label("role_id"), User.id.label("user_id"))
        .where(User.role_id.in_(role_ids))
        .union(
            select(ProjectMember.role_id, ProjectMember.user_id).where(
                ProjectMember.role_id.in_(role_ids)
            )
        )
        .subquery()
    )
    result = await session.execute(select(pairs.c.role_id, func.count()).group_by(pairs.c.role_id))
    return {role_id: count for role_id, count in result.all()}


@dataclass(frozen=True)
class MaskBasis:
    """Bir rolün alan maskesi için tek bakışlık özeti (`core.permissions.role_default_scope`)."""

    has_legacy_rows: bool
    hides_all_amounts: bool


async def role_mask_basis(session: AsyncSession, role_id: uuid.UUID) -> MaskBasis:
    """Eski `role_permissions` satırı var mı + `tum_tutarlar` gizli mi — TEK sorgu."""
    legacy = exists().where(RolePermission.role_id == role_id)
    hidden = exists().where(
        RoleHiddenField.role_id == role_id,
        RoleHiddenField.category == HiddenCategory.tum_tutarlar,
    )
    row = (await session.execute(select(legacy, hidden))).one()
    return MaskBasis(has_legacy_rows=bool(row[0]), hides_all_amounts=bool(row[1]))


async def derived_role_matrix(
    session: AsyncSession,
    role_id: uuid.UUID,
    role_key: str,
    cells: dict[str, tuple[PageLevel, bool]] | None = None,
) -> list[tuple[Module, AccessLevel, Scope]]:
    """Rolün modül matrisi, SAYFA HÜCRELERİNDEN türetilmiş (salt okur; IZN-B2).

    `/auth/me.permissions` ve `GET /roles/{id}/permissions` bunu okur (frontend B6/F5'e kadar).
    Düzey: `page_gate.display_level` (Sistem Yöneticisi: her modül `admin`). Kapsam: eski satırı
    olan modülde DONMUŞ eski satır; satırı olmayanda `role_default_scope` kuralı (satırı hiç
    olmayan rolde `tum_tutarlar` → `limited`). `cells` çağıranda yüklüyse (`/auth/me`) verilir:
    ikinci hücre sorgusu koşmaz.
    """
    modules = await list_modules(session)
    if cells is None:
        cells = {} if role_key == SYSTEM_ADMIN_ROLE_KEY else await load_cells(session, role_id)
    legacy_scopes = {
        module.key: perm.scope
        for module, perm in (await get_role_matrix(session, role_id))
        if perm in session  # kalıcı satır (varsayılan hücre session'a eklenmez)
    }
    default_scope = Scope.all
    if role_key != SYSTEM_ADMIN_ROLE_KEY:
        basis = await role_mask_basis(session, role_id)
        if not basis.has_legacy_rows and basis.hides_all_amounts:
            default_scope = Scope.limited
    return [
        (
            module,
            AccessLevel.admin
            if role_key == SYSTEM_ADMIN_ROLE_KEY
            else display_level(cells, module.key),
            legacy_scopes.get(module.key, default_scope),
        )
        for module in modules
    ]

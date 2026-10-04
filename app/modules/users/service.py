import uuid

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, NotFoundError, PermissionLockedError
from app.core.page_gate import is_admin_role, load_cells, page_ok
from app.core.sayfalar import PageLevel
from app.core.security import hash_password
from app.modules.projects.models import Project
from app.modules.roles.models import SYSTEM_ADMIN_KEY, Role
from app.modules.users import repository
from app.modules.users.models import User, UserProjectAccess, UserStatus
from app.modules.users.schemas import ProjectAccessInput, UserCreate, UserUpdate


async def _is_last_active_system_admin(session: AsyncSession, user: User) -> bool:
    if user.role.key != SYSTEM_ADMIN_KEY or user.status is not UserStatus.active:
        return False
    count = (
        await session.execute(
            select(func.count())
            .select_from(User)
            .join(Role, Role.id == User.role_id)
            .where(Role.key == SYSTEM_ADMIN_KEY, User.status == UserStatus.active)
        )
    ).scalar_one()
    return count <= 1


_PAGE_RANK = {PageLevel.none: 0, PageLevel.view: 1, PageLevel.edit: 2}


def _grant_covers(actor: tuple[PageLevel, bool], target: tuple[PageLevel, bool]) -> bool:
    """Aktörün sayfa hücresi, hedef rolün hücresini (düzey + onay) KARŞILIYOR mu?"""
    actor_level, actor_approve = actor
    target_level, target_approve = target
    return _PAGE_RANK[actor_level] >= _PAGE_RANK[target_level] and (
        actor_approve or not target_approve
    )


async def _require_assignable_role(session: AsyncSession, actor: User, role_id: uuid.UUID) -> Role:
    """Aktörün bu rolü atamaya yetkisi var mı?

    "Rol Yönetimi" sayfasında Düzenler (eski `user_management=admin`; Sistem Yöneticisi) her rolü
    atar. Onun altındaki bir aktör (1) sistem rolünü (Sistem Yöneticisi) atayamaz ve (2) KENDİ
    sayfa hücrelerini (düzey + onay) herhangi bir sayfada aşan bir rolü atayamaz — yoksa güçlü
    bir rolü kendine ya da açtığı kullanıcıya vererek sahip olmadığı yetkiyi kendine basar
    (spec §5.0). IZN-B2: karşılaştırma eski modül matrisi yerine SAYFA HÜCRELERİ üzerindendir;
    yeni roller artık atanabilir (B1 atama kilidi kalktı).
    """
    role = (await session.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
    if role is None:
        raise NotFoundError("Rol bulunamadı")

    # Sistem Yöneticisi rolünü YALNIZ Sistem Yöneticisi atayabilir: "Rol Yönetimi Düzenler"
    # sahibi (özel rol) de dahil kimse kendine/başkasına bu rolü veremez.
    if role.key == SYSTEM_ADMIN_KEY and not await is_admin_role(session, actor):
        raise PermissionLockedError(
            "Sistem Yöneticisi rolü yalnızca Sistem Yöneticisi tarafından atanabilir"
        )

    if await page_ok(session, actor, "ayarlar.rol_yonetimi", "edit"):
        return role

    if role.is_system:
        raise PermissionLockedError(
            "Sistem rolleri yalnızca Sistem Yöneticisi tarafından atanabilir"
        )

    actor_cells = await load_cells(session, actor.role_id)
    none_cell = (PageLevel.none, False)
    for page_key, target in (await load_cells(session, role_id)).items():
        if not _grant_covers(actor_cells.get(page_key, none_cell), target):
            raise PermissionLockedError("Sahip olmadığınız yetkileri içeren bir rol atayamazsınız")
    return role


async def create_user(session: AsyncSession, actor: User, data: UserCreate) -> User:
    if await repository.get_user_by_email(session, data.email) is not None:
        raise DomainError("Bu e-posta zaten kayıtlı")
    await _require_assignable_role(session, actor, data.role_id)

    password_hash = await run_in_threadpool(hash_password, data.password)
    user = User(
        email=data.email,
        password_hash=password_hash,
        full_name=data.full_name,
        title=data.title,
        role_id=data.role_id,
        status=data.status,
    )
    return await repository.add_user(session, user)


async def update_user(
    session: AsyncSession, actor: User, user_id: uuid.UUID, data: UserUpdate
) -> User:
    user = await repository.get_user(session, user_id)
    if user is None:
        raise NotFoundError("Kullanıcı bulunamadı")

    demotes_role = data.role_id is not None and data.role_id != user.role_id
    deactivates = data.status is not None and data.status is not UserStatus.active
    if (demotes_role or deactivates) and await _is_last_active_system_admin(session, user):
        raise DomainError("Son aktif Sistem Yöneticisi düşürülemez")

    if data.role_id is not None:
        await _require_assignable_role(session, actor, data.role_id)
        user.role_id = data.role_id
    if data.full_name is not None:
        user.full_name = data.full_name
    if data.title is not None:
        user.title = data.title
    if data.status is not None:
        user.status = data.status
    await session.flush()
    return user


async def set_user_password(session: AsyncSession, user_id: uuid.UUID, new_password: str) -> None:
    user = await repository.get_user(session, user_id)
    if user is None:
        raise NotFoundError("Kullanıcı bulunamadı")
    user.password_hash = await run_in_threadpool(hash_password, new_password)
    # Parola değişince mevcut token'ları geçersiz kıl — eski oturumlar düşmeli.
    user.token_version += 1
    await session.flush()


async def delete_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    user = await repository.get_user(session, user_id)
    if user is None:
        raise NotFoundError("Kullanıcı bulunamadı")
    if await _is_last_active_system_admin(session, user):
        raise DomainError("Son aktif Sistem Yöneticisi silinemez")
    await session.delete(user)
    await session.flush()


async def set_project_access(
    session: AsyncSession, user_id: uuid.UUID, data: ProjectAccessInput
) -> list[UserProjectAccess]:
    # 🔴 TAM-DEĞİŞTİRME (`DELETE` + `INSERT`) KİLİT ALTINDA KOŞAR. Tabloda
    # `(user_id, project_id)` UNIQUE kısıtı YOKTUR (migration `e274019416f6`
    # yalnız PK + NON-UNIQUE indeks açar), yani kilitsiz iki eşzamanlı istek
    # birbirinin `DELETE`iyle `INSERT`i arasına girip ya iki kümenin karışımını
    # ya da AYNI projenin çift satırını bırakır. Erişim kararı
    # (`projects.visible_projects`) bu satırlardan okunur — sapma SESSİZDİR.
    # Kısıt yerine kilit seçildi: canlı satırlara dokunulmaz, migration gerekmez.
    user = await repository.get_user_locked(session, user_id)
    if user is None:
        raise NotFoundError("Kullanıcı bulunamadı")
    # Kayıt #51 (kalan bacak): tabloda (user_id, project_id) üzerinde UNIQUE
    # kısıt YOK ve `ProjectAccessInput.project_ids` tekilleştirme yapmıyor —
    # tek istekte aynı proje ID'si birden fazla gönderilirse eşzamanlılık
    # gerekmeden AYNI projeye iki satır yazılır. Sırayı koruyarak burada
    # tekilleştir; giriş sırası mockup/UI için anlamlı olabileceğinden
    # `set()` yerine sıra-koruyan tekilleştirme kullanılır.
    project_ids = list(dict.fromkeys(data.project_ids))
    if not data.all_projects and project_ids:
        found = (
            (await session.execute(select(Project.id).where(Project.id.in_(project_ids))))
            .scalars()
            .all()
        )
        missing = set(project_ids) - set(found)
        if missing:
            raise NotFoundError("Proje bulunamadı")
    return await repository.replace_project_access(session, user_id, data.all_projects, project_ids)

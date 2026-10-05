import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionLockedError,
    RoleValidationError,
)
from app.core.sayfalar import (
    SAYFA_ANAHTARLARI,
    SAYFA_BY_KEY,
    HiddenCategory,
    PageLevel,
    sistem_yoneticisi_sayfalari,
)
from app.core.slug import slugify
from app.modules.audit import messages
from app.modules.pages.schemas import PageGrant
from app.modules.roles.models import (
    SYSTEM_ADMIN_KEY,
    Role,
    RoleHiddenField,
    RolePagePermission,
    RolePermission,
)
from app.modules.roles.repository import (
    list_role_hidden_categories,
    list_role_page_cells,
    role_user_counts,
)
from app.modules.roles.schemas import (
    RoleCopy,
    RoleCreate,
    RolePagesResponse,
    RoleResponse,
)


async def role_responses(session: AsyncSession, roles: list[Role]) -> list[RoleResponse]:
    """Rol yanıtları; `user_count` TEK sorguyla (N+1 yok)."""
    user_counts = await role_user_counts(session, [role.id for role in roles])
    return [
        RoleResponse(
            id=role.id,
            key=role.key,
            name=role.name,
            emoji=role.emoji,
            description=role.description,
            is_system=role.is_system,
            # IZN-B2: B1 atama kilidi kalktı; her rol atanabilir (alan frontend uyumu için KALIR).
            is_assignable=True,
            user_count=user_counts.get(role.id, 0),
            is_locked=role.key == SYSTEM_ADMIN_KEY,
        )
        for role in roles
    ]


async def rename_role(
    session: AsyncSession,
    role_id: uuid.UUID,
    name: str,
    emoji: str,
    description: str,
) -> Role:
    """Rolün görünen bilgilerini günceller. key asla değişmez — kod ona dayanır."""
    role = (await session.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
    if role is None:
        raise NotFoundError("Rol bulunamadı")
    role.name = name
    role.emoji = emoji
    role.description = description
    await session.flush()
    return role


async def create_custom_role(session: AsyncSession, data: RoleCreate) -> Role:
    """Yeni özel rol oluşturur; her sayfa için "Görmez" hücre açar (hücre sayısı = rol × 100).

    IZN-B2: eski `role_permissions` satırı AÇILMAZ (donmuş tablo): kapılar sayfa hücrelerinden
    karar verir. IZN-B4: alan maskesi bu rolün `hidden_fields` kategorilerinden okunur
    (`RolePagesResponse.hidden_fields_effective` her rolde `true`).
    """
    existing = (
        await session.execute(select(Role).where(Role.key == data.key))
    ).scalar_one_or_none()
    if existing is not None:
        raise DomainError("Bu rol anahtarı zaten kullanılıyor")

    role = Role(
        key=data.key,
        name=data.name,
        emoji=data.emoji,
        description=data.description,
        is_system=False,
    )
    session.add(role)
    await session.flush()

    for page_key in SAYFA_ANAHTARLARI:
        session.add(RolePagePermission(role_id=role.id, page_key=page_key, level=PageLevel.none))
    await session.flush()
    return role


async def delete_role(session: AsyncSession, role_id: uuid.UUID) -> None:
    """Rolü siler (IZN-B2, KARARLAR 8e9a684): kapıyı router tutar (yalnız Sistem Yöneticisi).

    Silinemeyen TEK rol Sistem Yöneticisi'dir (anahtara bakar; `is_system` Patron'da da true
    olduğu için artık kilit değildir). Kullanıcısı olan rol 409 ile reddedilir (`user_count`).
    """
    role = (await session.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
    if role is None:
        raise NotFoundError("Rol bulunamadı")
    if role.key == SYSTEM_ADMIN_KEY:
        raise PermissionLockedError("Sistem Yöneticisi rolü silinemez")

    in_use = (await role_user_counts(session, [role_id])).get(role_id, 0)
    if in_use > 0:
        raise ConflictError("Bu role atanmış kullanıcılar var; önce onları başka role taşıyın")

    # Sayfa hücreleri, gizli alanlar ve eski hücreler CASCADE ile silinir.
    await session.delete(role)
    await session.flush()


# ---------------------------------------------------------------------------
# Sayfa izinleri (IZN-B2): okuma, toplu yazma, gizli alanlar, kopyalama
# ---------------------------------------------------------------------------


async def _get_role_or_404(session: AsyncSession, role_id: uuid.UUID) -> Role:
    role = await session.get(Role, role_id)
    if role is None:
        raise NotFoundError("Rol bulunamadı")
    return role


def _grants_from_rows(rows: list[RolePagePermission]) -> dict[str, PageGrant]:
    """Rolün 100 sayfası: satırı olmayan (ya da katalogdan kalkmış) sayfa Görmez sayılır."""
    by_key = {row.page_key: row for row in rows if row.page_key in SAYFA_BY_KEY}
    return {
        key: PageGrant(level=by_key[key].level, approve=by_key[key].can_approve)
        if key in by_key
        else PageGrant(level=PageLevel.none, approve=False)
        for key in SAYFA_ANAHTARLARI
    }


async def role_page_grants(session: AsyncSession, role: Role) -> dict[str, PageGrant]:
    """Rolün sayfa hücreleri. Sistem Yöneticisi hücre taşımaz → çözücü haritası (her yer Edit)."""
    if role.key == SYSTEM_ADMIN_KEY:
        return {
            key: PageGrant(level=level, approve=approve)
            for key, (level, approve) in sistem_yoneticisi_sayfalari().items()
        }
    return _grants_from_rows(await list_role_page_cells(session, role.id))


async def get_role_pages(session: AsyncSession, role_id: uuid.UUID) -> RolePagesResponse:
    role = await _get_role_or_404(session, role_id)
    locked = role.key == SYSTEM_ADMIN_KEY
    return RolePagesResponse(
        role_id=role.id,
        is_locked=locked,
        pages=await role_page_grants(session, role),
        hidden_fields=[] if locked else await list_role_hidden_categories(session, role.id),
        hidden_fields_effective=True,  # IZN-B4: yeni maske HER rolde geçerli (hibrit kural kalktı)
    )


def _grant_violation(page_key: str, grant: PageGrant) -> str | None:
    """Bir hücrenin kural ihlali (Türkçe) ya da `None`. Kural katalogdadır, DB'de değil."""
    sayfa = SAYFA_BY_KEY[page_key]
    if grant.approve and not sayfa.onay_var:
        return f"{sayfa.ad}: bu sayfada onay eylemi yok"
    if grant.approve and grant.level is PageLevel.none:
        return f"{sayfa.ad}: Görmez düzeyindeki sayfada onay verilemez"
    return None


@dataclass(frozen=True)
class RolePagesChange:
    """`update_role_pages` sonucu: denetim satırları bundan kurulur."""

    role: Role
    page_changes: list[str]
    hidden_changed: bool
    hidden_fields: list[HiddenCategory]


async def update_role_pages(
    session: AsyncSession,
    role_id: uuid.UUID,
    pages: dict[str, PageGrant],
    hidden: list[HiddenCategory],
) -> RolePagesChange:
    """TAM matris + gizli alan kümesi, TEK transaction (hepsi ya da hiçbiri).

    Sıra: 404 (rol yok) → 403 (Sistem Yöneticisi kilitli) → 422 (eksik sayfa, sonra hücre
    ihlalleri " · " ile birleşik). Aynı değeri yeniden yazmak değişiklik sayılmaz.
    """
    role = await _get_role_or_404(session, role_id)
    if role.key == SYSTEM_ADMIN_KEY:
        raise PermissionLockedError("Sistem Yöneticisi rolünün izinleri değiştirilemez")

    missing = [key for key in SAYFA_ANAHTARLARI if key not in pages]
    if missing:
        adlar = ", ".join(SAYFA_BY_KEY[key].ad for key in missing[:5])
        fazla = f" ve {len(missing) - 5} sayfa daha" if len(missing) > 5 else ""
        raise RoleValidationError(
            f"Tüm sayfalar gönderilmeli: {len(missing)} sayfa eksik ({adlar}{fazla})"
        )
    violations = [v for key, grant in pages.items() if (v := _grant_violation(key, grant))]
    if violations:
        raise RoleValidationError(" · ".join(violations))

    page_changes = await _apply_page_cells(session, role_id, pages)
    ordered, hidden_changed = await _apply_hidden_fields(session, role_id, hidden)
    return RolePagesChange(role, page_changes, hidden_changed, ordered)


async def _apply_page_cells(
    session: AsyncSession, role_id: uuid.UUID, pages: dict[str, PageGrant]
) -> list[str]:
    """Hücreleri yazar (DOĞRULAMASIZ; çağıran doğrular). Döner: DEĞİŞENlerin denetim etiketleri."""
    existing = {row.page_key: row for row in await list_role_page_cells(session, role_id)}
    changes: list[str] = []
    for key, grant in pages.items():
        row = existing.get(key)
        if row is None:
            session.add(
                RolePagePermission(
                    role_id=role_id, page_key=key, level=grant.level, can_approve=grant.approve
                )
            )
            unchanged = grant.level is PageLevel.none and not grant.approve
        else:
            unchanged = row.level is grant.level and row.can_approve == grant.approve
            if not unchanged:
                row.level = grant.level
                row.can_approve = grant.approve
        if not unchanged:
            label = messages.page_cell_label(
                SAYFA_BY_KEY[key].ad, messages.PAGE_LEVEL_LABELS[grant.level], grant.approve
            )
            changes.append(label)
    await session.flush()
    return changes


async def _apply_hidden_fields(
    session: AsyncSession, role_id: uuid.UUID, hidden: list[HiddenCategory]
) -> tuple[list[HiddenCategory], bool]:
    """Gizli kategori kümesinin TAM DEĞİŞTİRMESİ. Döner: (sıralı küme, değişti mi)."""
    wanted = set(hidden)
    current = set(await list_role_hidden_categories(session, role_id))
    for category in current - wanted:
        row = await session.get(RoleHiddenField, (role_id, category))
        if row is not None:
            await session.delete(row)
    for category in wanted - current:
        session.add(RoleHiddenField(role_id=role_id, category=category))
    await session.flush()
    return sorted(wanted, key=lambda c: c.value), wanted != current


_KEY_MAX = 50
_KEY_FALLBACK = "rol"


async def _unique_role_key(session: AsyncSession, name: str) -> str:
    """Addan rol anahtarı: ASCII slug (`_`), `^[a-z][a-z0-9_]*$`, çakışırsa `_2`, `_3`…"""
    base = (slugify(name) or _KEY_FALLBACK).replace("-", "_")
    if not base[0].isalpha():
        base = f"{_KEY_FALLBACK}_{base}"
    base = base[: _KEY_MAX - 6].rstrip("_")
    rows = await session.execute(select(Role.key).where(Role.key.like(f"{base}%")))
    taken = set(rows.scalars())
    key, suffix = base, 2
    while key in taken:
        key = f"{base}_{suffix}"
        suffix += 1
    return key


async def copy_role(session: AsyncSession, source_id: uuid.UUID, data: RoleCopy) -> Role:
    """Kaynak rolün sayfa hücreleri + gizli alanlarıyla yeni (silinebilir) rol açar.

    Kilit ve silme KOPYALANMAZ. Sistem Yöneticisi kopyası: her sayfa Düzenler + (onay eylemi
    varsa) Onaylar, gizli alan YOK (çözücü haritası). Yeni rol `is_system=false`.
    """
    source = await _get_role_or_404(session, source_id)
    grants = await role_page_grants(session, source)
    locked = source.key == SYSTEM_ADMIN_KEY
    hidden = [] if locked else await list_role_hidden_categories(session, source.id)
    role = await create_custom_role(
        session,
        RoleCreate(
            key=await _unique_role_key(session, data.name),
            name=data.name,
            emoji=data.emoji,
            description=data.description,
        ),
    )
    # Eski `role_permissions` satırları da kopyalanır (CEO onarım kararı): kopya, kaynağın alan
    # maskesi kapsamını (`finance`/`limited`) DONMUŞ eski satırdan korur ve `hidden_fields_
    # effective` kaynakla aynı olur. Sistem Yöneticisi kaynağında `admin` düzeyi `full`a iner:
    # silme (admin) kopyalanmaz.
    legacy_rows = (
        (await session.execute(select(RolePermission).where(RolePermission.role_id == source.id)))
        .scalars()
        .all()
    )
    for row in legacy_rows:
        level = (
            AccessLevel.full
            if row.access_level is AccessLevel.admin and locked
            else row.access_level
        )
        session.add(
            RolePermission(
                role_id=role.id, module_id=row.module_id, access_level=level, scope=row.scope
            )
        )
    # `create_custom_role` her sayfaya "Görmez" satırı açtı; üstüne kaynağın hücreleri yazılır.
    # Doğrulama atlanır: kaynak zaten kayıtlı bir roldür (bayat hücre kopyayı da reddetmesin).
    await _apply_page_cells(session, role.id, grants)
    await _apply_hidden_fields(session, role.id, hidden)
    return role

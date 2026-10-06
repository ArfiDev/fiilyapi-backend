"""TEST YARDIMCISI — eski modül hücresini yazıp sayfa hücrelerini YENİDEN TÜRETİR (IZN-B2).

Üretimde YOKTUR: `PUT /roles/{id}/permissions/{module}` 410 oldu ve `page_sync` (B1 write-through)
üretim kodundan çıktı (CEO kararı). Testler bugünkü rol/düzey kurulumlarını (58 kullanım) yine
"eski düzey → sayfa hücresi" diliyle yapar: `update_role_permission` eski satırı yazar ve
`sync_page_cells` aynı dönüşümle (`core/sayfalar.sayfa_matrisi` / `gizli_alanlar`) 100 sayfa
hücresini ve `tum_tutarlar` bayrağını yeniden üretir. Bu, parite testinin "ekrandan değiştirilmiş
hücre" senaryolarının da aracıdır.

Üretim servisinin geri kalanı (`create_custom_role` vb.) `app/modules/roles/service.py`dedir.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import DROPPED_SCOPES, AccessLevel, Scope
from app.core.errors import NotFoundError, PermissionLockedError
from app.core.sayfalar import HiddenCategory, gizli_alanlar, sayfa_matrisi
from app.modules.roles.models import (
    IZN_ROLE_KEYS,
    SYSTEM_ADMIN_KEY,
    Role,
    RoleHiddenField,
    RolePagePermission,
    RolePermission,
)
from app.modules.roles.repository import (
    get_module,
    get_permission,
    get_role_matrix,
    has_legacy_cells,
)


async def sync_page_cells(session: AsyncSession, role_id: uuid.UUID) -> None:
    matrix = await get_role_matrix(session, role_id)
    cells = {module.key: (perm.access_level, perm.scope) for module, perm in matrix}

    existing = {
        row.page_key: row
        for row in (
            await session.execute(
                select(RolePagePermission).where(RolePagePermission.role_id == role_id)
            )
        )
        .scalars()
        .all()
    }
    for page_key, (level, approve) in sayfa_matrisi(cells).items():
        row = existing.get(page_key)
        if row is None:
            session.add(
                RolePagePermission(
                    role_id=role_id, page_key=page_key, level=level, can_approve=approve
                )
            )
        elif row.level is not level or row.can_approve != approve:
            row.level = level
            row.can_approve = approve

    wanted = HiddenCategory.tum_tutarlar in gizli_alanlar(cells)
    hidden = await session.get(RoleHiddenField, (role_id, HiddenCategory.tum_tutarlar))
    if wanted and hidden is None:
        session.add(RoleHiddenField(role_id=role_id, category=HiddenCategory.tum_tutarlar))
    elif not wanted and hidden is not None:
        await session.delete(hidden)
    await session.flush()


async def update_role_permission(
    session: AsyncSession,
    role_id: uuid.UUID,
    module_key: str,
    level: AccessLevel,
    scope: Scope,
) -> RolePermission:
    """Matrisin bir hücresini günceller.

    system_admin rolünün hiçbir hücresi değiştirilemez — aktör kim olursa olsun.
    """
    role = (await session.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
    if role is None:
        raise NotFoundError("Rol bulunamadı")

    if role.key == SYSTEM_ADMIN_KEY:
        raise PermissionLockedError("Sistem Yöneticisi rolünün izinleri değiştirilemez")

    # IZN-B1: yeni roller eski modül matrisinden YÖNETİLMEZ. Tek bir eski hücre yazmak onu
    # `_has_legacy_cells` atama kilidinden "kurtarırdı" (ve downgrade'in "migration'ın eklediği
    # rol = role_permissions satırı yok" tanımını bozardı). Eski hücreleri OLAN (elle açılmış
    # çakışan anahtarlı) rol etkilenmez.
    if role.key in IZN_ROLE_KEYS and not await has_legacy_cells(session, role.id):
        raise PermissionLockedError("Bu rol yeni Sayfa İzinleri ekranından yönetilecek")

    permission = await get_permission(session, role_id, module_key)
    if permission is None:
        # 🔴 Satırın YOKLUĞU "böyle bir hücre olamaz" demek DEĞİLDİR: uzantı
        # migration'ları izin satırlarını sabit `ROLE_ORDER` üzerinde yazar, o
        # yüzden migration'dan önce açılmış her özel rol sonradan inen modülün
        # hücresine sahip olmaz. Eskiden burada 404 atılıyordu ve yönetici o
        # modülü ekrandan KALICI OLARAK açamıyordu. Hücre artık burada doğar;
        # matris tarafı `repository.get_role_matrix` ile zaten görünür.
        # 404 YALNIZ modül gerçekten yoksa kalır — uydurma anahtar satır açmaz.
        module = await get_module(session, module_key)
        if module is None:
            raise NotFoundError("İzin satırı bulunamadı")
        permission = RolePermission(
            role_id=role_id,
            module_id=module.id,
            access_level=AccessLevel.none,
            scope=Scope.all,
        )
        session.add(permission)

    # IZN-B6a: eski kapsam maskesi (`field_scope`/`scoped_route`/`kablolu_moduller`) söküldü; yeni
    # maske kapsamdan bağımsızdır. Düşen kapsamlar (own/project/stock) ve `limited`/`finance`
    # (artık hiçbir modülde bir karşılığı yok) yazılamaz; yalnız `all` atanır.
    if scope in DROPPED_SCOPES:
        raise PermissionLockedError(
            "Bu kapsam kaldırıldı; erişimi daraltmak için proje erişimini kullanın."
        )
    if scope is not Scope.all:
        raise PermissionLockedError(
            "Bu kapsam uygulanmıyor (alan maskesi tanımlı değil); "
            "erişimi daraltmak için proje erişimini kullanın."
        )

    permission.access_level = level
    permission.scope = scope
    await session.flush()
    # IZN-B1 WRITE-THROUGH: eski hücre değişince o rolün sayfa hücreleri ve `tum_tutarlar`
    # bayrağı AYNI transaction'da yeniden türetilir (B2'ye dek iki model ayrışmasın).
    await sync_page_cells(session, role_id)
    return permission

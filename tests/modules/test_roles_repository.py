import uuid

import pytest
from sqlalchemy import func, select

from app.core.access import AccessLevel, Scope
from app.core.errors import DomainError, NotFoundError, PermissionLockedError
from app.modules.roles import repository, service
from app.modules.roles.models import (
    Module,
    ModuleGroup,
    Role,
    RolePagePermission,
    RolePermission,
)
from app.modules.roles.schemas import RoleCreate
from tests._legacy_permission_yardimcisi import update_role_permission


async def test_create_custom_role_eski_hucre_ACMAZ_100_sayfa_hucresi_acar(seeded_db):
    """IZN-B2: özel rol eski `role_permissions` satırı taşımaz (donmuş tablo); 100 sayfa hücresi."""
    role = await service.create_custom_role(
        seeded_db, RoleCreate(key="saha_amiri", name="Saha Amiri", emoji="🚧", description="")
    )
    assert role.is_system is False
    count = (
        await seeded_db.execute(
            select(func.count())
            .select_from(RolePermission)
            .where(RolePermission.role_id == role.id)
        )
    ).scalar_one()
    assert count == 0
    sayfa = (
        await seeded_db.execute(
            select(func.count())
            .select_from(RolePagePermission)
            .where(RolePagePermission.role_id == role.id)
        )
    ).scalar_one()
    assert sayfa == 100


async def test_create_custom_role_duplicate_key_raises(seeded_db):
    with pytest.raises(DomainError):
        await service.create_custom_role(
            seeded_db, RoleCreate(key="patron", name="X", emoji="", description="")
        )


async def test_delete_system_role_locked(seeded_db):
    sysadmin = (
        await seeded_db.execute(select(Role).where(Role.key == "system_admin"))
    ).scalar_one()
    with pytest.raises(PermissionLockedError):
        await service.delete_role(seeded_db, sysadmin.id)


async def test_delete_unknown_role_raises(seeded_db):
    with pytest.raises(NotFoundError):
        await service.delete_role(seeded_db, uuid.uuid4())


async def test_delete_role_in_use_rejected(seeded_db, user_factory):
    role = await service.create_custom_role(
        seeded_db, RoleCreate(key="gecici_rol", name="Geçici", emoji="", description="")
    )
    await user_factory(email="ru@t.co", password="parola1234", role_key="gecici_rol")
    with pytest.raises(DomainError):
        await service.delete_role(seeded_db, role.id)


async def test_get_role_matrix_returns_all_modules(seeded_db):
    patron = (await seeded_db.execute(select(Role).where(Role.key == "patron"))).scalar_one()
    matrix = await repository.get_role_matrix(seeded_db, patron.id)
    assert len(matrix) == 23  # PLN-B1


# ---------------------------------------------------------------------------
# 🔴 SONRADAN İNEN MODÜL — özel rol kalıcı olarak dışarıda kalıyordu
#
# `create_custom_role` yalnız O ANDA var olan modüller için hücre açar. Dokuz
# uzantı migration'ı (invoicing · projects · sites · boq · boq-fix · contracts ·
# sales · documents · equipment) izin satırlarını `WHERE r.key = :role_key`
# süzgeciyle ve yalnız sabit `ROLE_ORDER` üzerinde yazar; `seed_reference_data`
# da 8 sistem rolüyle sınırlıdır ve ÜSTELİK uygulama kodunda çağıranı yoktur
# (ölçüldü: `alembic/versions/e5f7a9c1b3d4...py` docstring §SAPMA 1). Yalnız
# `ai` migration'ı süzgeci bilerek kaldırır.
#
# Sonuç: migration'dan ÖNCE açılmış bir özel rol o modülü
#   * `/auth/me` yanıtında HİÇ göremez (`get_role_matrix` INNER JOIN'di), ve
#   * İzin Matrisi ekranından AÇAMAZ (`update_role_permission` 404 atıyordu).
# Kalıcı ve sessiz. Kapı fail-closed olduğu için sızıntı DEĞİL, kilitlenmedir.
# ---------------------------------------------------------------------------


async def _sonradan_inen_modul(session) -> Module:
    """Bir migration'ın yaptığını yapar: yalnız `modules` satırı, izin satırı YOK."""
    module = Module(key="yeni_modul", name="Yeni Modül", group=ModuleGroup.SISTEM, sort_order=99)
    session.add(module)
    await session.flush()
    return module


async def test_matris_sonradan_inen_modulu_de_dondurur(seeded_db):
    role = await service.create_custom_role(
        seeded_db, RoleCreate(key="ozel_rol", name="Özel", emoji="", description="")
    )
    await _sonradan_inen_modul(seeded_db)

    matrix = await repository.get_role_matrix(seeded_db, role.id)

    assert len(matrix) == 24  # 23 seed + sonradan inen
    hucre = {module.key: perm for module, perm in matrix}["yeni_modul"]
    assert hucre.access_level is AccessLevel.none  # varsayılan KAPALI
    assert hucre.scope is Scope.all


async def test_sistem_rolu_icin_de_ayni_varsayilan_hucre(seeded_db):
    """Süzgeçli migration'lar sistem rollerini de atlayabilir (ROLE_ORDER dışı yok,
    ama modül satırı migration'sız düşerse aynı delik oluşur)."""
    patron = (await seeded_db.execute(select(Role).where(Role.key == "patron"))).scalar_one()
    await _sonradan_inen_modul(seeded_db)

    matrix = await repository.get_role_matrix(seeded_db, patron.id)

    assert len(matrix) == 24  # 23 seed + sonradan inen


async def test_varsayilan_hucre_veritabanina_YAZILMAZ(seeded_db):
    """Okuma yolu yazmaz: sentetik hücre kalıcılaşırsa `uq_role_module` yarışında
    çift satır ve sessiz izin üretir."""
    role = await service.create_custom_role(
        seeded_db, RoleCreate(key="ozel_rol2", name="Özel2", emoji="", description="")
    )
    await _sonradan_inen_modul(seeded_db)

    await repository.get_role_matrix(seeded_db, role.id)
    await seeded_db.flush()

    count = (
        await seeded_db.execute(
            select(func.count())
            .select_from(RolePermission)
            .where(RolePermission.role_id == role.id)
        )
    ).scalar_one()
    assert count == 0  # sentetik hücre sayılmaz (IZN-B2: özel rol eski satır taşımaz)


async def test_izin_guncelleme_eksik_hucreyi_OLUSTURUR(seeded_db):
    """Yönetici ekrandan açabilmeli: satır yoksa 404 değil, satır DOĞAR."""
    role = await service.create_custom_role(
        seeded_db, RoleCreate(key="ozel_rol3", name="Özel3", emoji="", description="")
    )
    await _sonradan_inen_modul(seeded_db)

    perm = await update_role_permission(
        seeded_db, role.id, "yeni_modul", AccessLevel.view, Scope.all
    )

    assert perm.access_level is AccessLevel.view
    okunan = await repository.get_permission(seeded_db, role.id, "yeni_modul")
    assert okunan is not None
    assert okunan.access_level is AccessLevel.view


async def test_olmayan_modul_hala_404(seeded_db):
    """Doğan satır YALNIZ gerçek bir modül içindir; uydurma anahtar 404 kalır."""
    role = await service.create_custom_role(
        seeded_db, RoleCreate(key="ozel_rol4", name="Özel4", emoji="", description="")
    )
    with pytest.raises(NotFoundError):
        await update_role_permission(
            seeded_db, role.id, "boyle_bir_modul_yok", AccessLevel.view, Scope.all
        )

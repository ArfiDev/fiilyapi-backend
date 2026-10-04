import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import DROPPED_SCOPES, AccessLevel, Scope, satisfies
from app.core.errors import DomainError, NotFoundError, PermissionLockedError
from app.core.field_scope import gizlenen_kova
from app.core.sayfalar import SAYFA_ANAHTARLARI, PageLevel
from app.modules.roles.models import (
    IZN_ROLE_KEYS,
    SYSTEM_ADMIN_KEY,
    Module,
    Role,
    RolePagePermission,
    RolePermission,
)
from app.modules.roles.page_sync import sync_page_cells
from app.modules.roles.repository import (
    get_module,
    get_permission,
    has_legacy_cells,
    legacy_cell_role_ids,
    role_assignable,
)
from app.modules.roles.schemas import RoleCreate, RoleResponse
from app.modules.roles.scope_wiring import kablolu_moduller


async def role_responses(session: AsyncSession, roles: list[Role]) -> list[RoleResponse]:
    """Rol yanıtları; `is_assignable` TEK sorguyla (N+1 yok) ve atama kilidiyle AYNI kuraldan."""
    with_cells = await legacy_cell_role_ids(session, [role.id for role in roles])
    return [
        RoleResponse(
            id=role.id,
            key=role.key,
            name=role.name,
            emoji=role.emoji,
            description=role.description,
            is_system=role.is_system,
            is_assignable=role_assignable(role.key, role.id in with_cells),
        )
        for role in roles
    ]


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

    # 🔴 KAPSAM UYGULANIYOR (2026-09-19): `core/field_scope` alan maskesini yazar,
    # `core/permissions.actor_scope` + `core/scoped_route` onu uca bağlar. Bu blok
    # kapsam DEKORATİFKEN yazılmıştı ve o zaman doğruydu: hiçbir uç
    # `permission.scope`u okumadığı için ekranda verilen "Sınırlı / Mali" sözü
    # YALANDI ve yeni bir yalanın kalıcı yazılmasına izin verilmiyordu. Söz
    # tutulduktan sonra aynı fren TERSİNE ÇEVRİLDİ: yönetici kendi kurduğu kısıtı
    # hiçbir hücreye ATAYAMIYORDU (yalnız mevcut değeri geri gönderebiliyordu) ve
    # reddetme metni "henüz uygulanmıyor" diyerek artık YANLIŞ bilgi veriyordu.
    #
    # Atanabilir kapsamlar ELLE LİSTELENMEZ; iki gerçek kaynaktan TÜRETİLİR:
    #   * `access.DROPPED_SCOPES` — matristen DÜŞEN kapsamlar (own/project/stock),
    #   * `field_scope.gizlenen_kova()` — maskesi GERÇEKTEN yazılmış kapsamlar.
    # Üçüncü bir liste tutmak bugün onarılan kusurun aynısını üretirdi: iki liste
    # bir gün ayrışır ve ekran yine uygulanmayan bir kısıt vaat ederdi. Bekçisi
    # `tests/modules/test_role_service.py::test_ATANABILIR_kapsam_listesi_*`.
    #
    # 🔴 DÜŞEN kapsamlar MEVCUT OLSA BİLE geri yazılamaz. "Mevcudu geri göndermek
    #    serbesttir" muafiyeti artık HİÇBİR YERDE yok — karar mevcut değere değil
    #    kapsamın UYGULANIP UYGULANMADIĞINA bakar. Muafiyet kalsaydı migration
    #    canlıyı `all`a çektikten sonra bile düşen kapsam ekrandan yeniden doğardı.
    if scope in DROPPED_SCOPES:
        raise PermissionLockedError(
            "Bu kapsam kaldırıldı; erişimi daraltmak için proje erişimini kullanın."
        )
    # 🔴 Burada FAIL-CLOSED'uz, `field_scope`un OKUMA yolundaki fail-OPEN'ının
    # tersine. Oradaki gevşeklik "canlıda kalmış bir kalıntı satır ekranı
    # BOŞALTMASIN" içindir; burada ise yönetici YENİ bir söz veriyor. Maskesi
    # olmayan bir kapsam yazılabilseydi `Scope`a eklenen her üye — hiç
    # uygulanmadan — matristen atanabilir hâle gelirdi.
    if scope is not Scope.all and gizlenen_kova(scope) is None:
        raise PermissionLockedError(
            "Bu kapsam uygulanmıyor (alan maskesi tanımlı değil); "
            "erişimi daraltmak için proje erişimini kullanın."
        )
    # 🔴 MODÜL EKSENLİ FAIL-CLOSED (kalan iş #4, 2026-09-23): üstteki kapı yalnız
    # KAPSAMIN maskesi var mı diye sorar, hangi MODÜLE yazıldığına hiç bakmaz.
    # `limited`/`finance` yalnız `kablolu_moduller()`in döndürdüğü routerlarda
    # (`route_class=kapsam_rotasi(...)` + `dependencies=[kapsam_kapisi(...)]`)
    # bir karşılığa sahiptir; geri kalan 16 modülde (payroll · treasury ·
    # accounting · invoicing · personnel · … ) köprü YOKTUR — maske aktörün
    # `_KAPSAM` ContextVar'ı hiç yazılmadığı için sessizce `Scope.all` görür ve
    # HİÇBİR ŞEY GİZLEMEZ. Bu kapı olmadan yönetici `personnel = view/limited`
    # yazabiliyordu (200 dönüyordu), ekran "Sınırlı" gösteriyordu ve kullanıcı
    # maaşı tam değeriyle görmeye devam ediyordu — ayrı yetki verildiği YALANDI.
    #
    # Bu kontrol yalnız kapsam GERÇEKTEN bir kovayı gizliyorsa (`gizlenen_kova`
    # `None` DEĞİLSE) anlamlıdır; `Scope.all` hiçbir şeyi maskelemediği için her
    # modülde serbest kalmaya devam eder (üstteki `all_a_cekilebilir` testiyle
    # aynı ilke). Pozitif kontrol: `tests/modules/test_role_service.py::
    # test_MODUL_EKSENLI_kapsam_kablolu_OLMAYAN_modulde_REDDEDILIR`.
    if gizlenen_kova(scope) is not None and module_key not in kablolu_moduller():
        raise PermissionLockedError(
            "Bu modülde kapsam kısıtı uygulanmıyor (alan maskesi bu modülün "
            "uçlarına bağlı değil); erişimi daraltmak için proje erişimini kullanın."
        )
    # 🔴 MASKELEYEN KAPSAM + YAZAN SEVİYE = ÜRÜNDE KARŞILIĞI OLMAYAN HÜCRE.
    # Maske girdiyi `None` yapar; salt-okuma yüzeyi bunu "—" diye basar ama YAZMA
    # yüzeyi basamaz: `frontend/src/lib/masked.ts::maskesiz` maskeli bir değer
    # forma/hesaba düştüğünde RENDER anında atar (bilerek: `?? 0` yazmak
    # kullanıcının GÖREMEDİĞİ bir sayıyı kaydeder, satırı atlamak kaydı sessizce
    # siler). O dosya "`full` izin matrisinde yalnız `Scope.all` ile gelir" diye
    # YAZILI bir varsayım taşıyordu ve bu varsayımı hiçbir şey uygulamıyordu —
    # eski fren yalnız KAPSAM değişimine bakıyor, SEVİYEYİ hiç denetlemiyordu.
    # Eşik `draft`: kayıt OLUŞTURABİLEN ilk seviye odur; `none`/`view` serbest
    # kalır (ekranın "Sınırlı"/"Mali" preset'lerinin ikisi de `view`dır).
    if gizlenen_kova(scope) is not None and satisfies(level, AccessLevel.draft):
        raise PermissionLockedError(
            "Kapsam kısıtlı bir hücre yazma seviyesi taşıyamaz: maskelenen alan "
            "form ve hesaplara düşerdi. Bu seviye için kapsamı 'all' seçin."
        )

    permission.access_level = level
    permission.scope = scope
    await session.flush()
    # IZN-B1 WRITE-THROUGH: eski hücre değişince o rolün sayfa hücreleri ve `tum_tutarlar`
    # bayrağı AYNI transaction'da yeniden türetilir (B2'ye dek iki model ayrışmasın).
    await sync_page_cells(session, role_id)
    return permission


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
    """Yeni özel rol oluşturur; tüm modüller için none/all izin satırı seedler.

    IZN-B1: sayfa matrisinde de her sayfa için "Görmez" hücre açılır — "hücre sayısı =
    rol × 100" değişmezi (migration testi) migration SONRASI açılan rollerde de geçerli kalır
    ve `/auth/me.pages` boş harita yerine açık "Görmez" döner. Eski kapının davranışı değişmez.
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

    modules = (await session.execute(select(Module))).scalars().all()
    for module in modules:
        session.add(
            RolePermission(
                role_id=role.id,
                module_id=module.id,
                access_level=AccessLevel.none,
                scope=Scope.all,
            )
        )
    for page_key in SAYFA_ANAHTARLARI:
        session.add(RolePagePermission(role_id=role.id, page_key=page_key, level=PageLevel.none))
    await session.flush()
    return role


async def delete_role(session: AsyncSession, role_id: uuid.UUID) -> None:
    """Özel rolü siler. Sistem rolleri kilitlidir."""
    role = (await session.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
    if role is None:
        raise NotFoundError("Rol bulunamadı")
    if role.is_system:
        raise PermissionLockedError("Sistem rolleri silinemez")

    from app.modules.users.models import User  # fonksiyon ici import (dongu riskini onler)

    in_use = (
        await session.execute(select(func.count()).select_from(User).where(User.role_id == role_id))
    ).scalar_one()
    if in_use > 0:
        raise DomainError("Bu role atanmış kullanıcılar var; önce onları başka role taşıyın")

    await session.delete(role)  # role_permissions CASCADE ile silinir
    await session.flush()

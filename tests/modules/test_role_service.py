import uuid

import pytest
from sqlalchemy import select

from app.core.access import AccessLevel, Scope
from app.core.errors import NotFoundError, PermissionLockedError
from app.core.sayfalar import PageLevel, sayfa_matrisi
from app.modules.roles.models import (
    SYSTEM_ADMIN_KEY,
    Module,
    Role,
    RolePagePermission,
    RolePermission,
)
from app.modules.roles.repository import get_permission
from app.modules.roles.service import rename_role
from tests._legacy_permission_yardimcisi import update_role_permission
from tests._modul_duzeyi_yardimcisi import _baslangic_haritasi, modul_duzeyi_yaz


async def _role(session, key: str) -> Role:
    return (await session.execute(select(Role).where(Role.key == key))).scalar_one()


async def _hucre_seviyeleri(session, role_id) -> dict[str, PageLevel]:
    rows = await session.execute(
        select(RolePagePermission.page_key, RolePagePermission.level).where(
            RolePagePermission.role_id == role_id
        )
    )
    return {page_key: level for page_key, level in rows.all()}


def _beklenen_hucreler(role_key: str, modul: str, level: AccessLevel) -> dict[str, PageLevel]:
    harita = {**_baslangic_haritasi(role_key), modul: level}
    hucreler = {m: (lvl, Scope.all) for m, lvl in harita.items()}
    return {key: lvl for key, (lvl, _) in sayfa_matrisi(hucreler).items()}


async def test_permission_can_be_raised_for_normal_role(seeded_db):
    role = await _role(seeded_db, "site_chief")
    await modul_duzeyi_yaz(seeded_db, role.id, "progress_payments", AccessLevel.approve)
    beklenen = _beklenen_hucreler("site_chief", "progress_payments", AccessLevel.approve)
    assert await _hucre_seviyeleri(seeded_db, role.id) == beklenen


async def test_permission_can_be_lowered_for_normal_role(seeded_db):
    role = await _role(seeded_db, "patron")
    await modul_duzeyi_yaz(seeded_db, role.id, "payroll", AccessLevel.view)
    beklenen = _beklenen_hucreler("patron", "payroll", AccessLevel.view)
    assert await _hucre_seviyeleri(seeded_db, role.id) == beklenen


async def test_system_admin_permissions_are_locked(seeded_db):
    """Kilitlenme koruması: system_admin izin satırları hiç kimse tarafından değiştirilemez."""
    role = await _role(seeded_db, "system_admin")
    with pytest.raises(PermissionLockedError):
        await modul_duzeyi_yaz(seeded_db, role.id, "settings", AccessLevel.none)


async def test_system_admin_can_still_be_renamed(seeded_db):
    """Ad/emoji/açıklama düzenlenebilir; kilitli olan yalnızca izinlerdir."""
    role = await _role(seeded_db, "system_admin")
    renamed = await rename_role(
        seeded_db, role.id, name="Süper Yönetici", emoji="⚡", description=""
    )
    assert renamed.name == "Süper Yönetici"
    assert renamed.key == "system_admin"


async def test_renaming_never_changes_key(seeded_db):
    """Yetki kontrolü key'e dayanır; ad değişince yetkiler kaymamalı."""
    role = await _role(seeded_db, "field_engineer")
    renamed = await rename_role(seeded_db, role.id, name="Teknik Ofis", emoji="📐", description="")
    assert renamed.key == "field_engineer"


async def test_rename_unknown_role_raises_not_found(seeded_db):
    with pytest.raises(NotFoundError):
        await rename_role(seeded_db, uuid.uuid4(), "X", "", "")


async def test_update_permission_missing_row_raises_not_found(seeded_db):
    patron = await _role(seeded_db, "patron")
    with pytest.raises(NotFoundError):
        await modul_duzeyi_yaz(seeded_db, patron.id, "olmayan_modul", AccessLevel.view)


async def test_update_permission_unknown_role_raises_not_found(seeded_db):
    with pytest.raises(NotFoundError):
        await modul_duzeyi_yaz(seeded_db, uuid.uuid4(), "dashboard", AccessLevel.view)


async def test_update_permission_system_admin_still_locked(seeded_db):
    sysadmin = await _role(seeded_db, "system_admin")
    with pytest.raises(PermissionLockedError):
        await modul_duzeyi_yaz(seeded_db, sysadmin.id, "dashboard", AccessLevel.view)


async def _non_all_scope_cell(session) -> tuple[Role, str, Scope]:
    """Seed'de `all` DIŞI kapsam taşıyan ilk hücreyi (rol, modül, kapsam) döner."""
    row = (
        await session.execute(
            select(Role, Module.key, RolePermission.scope)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Module, Module.id == RolePermission.module_id)
            .where(RolePermission.scope != Scope.all, Role.key != SYSTEM_ADMIN_KEY)
            .order_by(Role.key, Module.key)
            .limit(1)
        )
    ).first()
    assert row is not None, "Seed'de all dışı kapsam kalmamış — bu testin dayanağı yok"
    return row[0], row[1], row[2]


async def test_DUSEN_kapsam_ALL_hucreye_YAZILAMAZ(seeded_db):
    """Düşen bir kapsam (`own`) `all` taşıyan bir hücreye de yazılamaz.

    🔴 Docstring 2026-09-19 akşamı DÜZELTİLDİ: eski hâli "`Scope` karar
    mekanizmasına HİÇ bağlı değil, `permissions.py`de `scope` kelimesi geçmez"
    diyordu. O cümle artık YANLIŞ — eski `actor_scope` (IZN-B6a'da söküldü) tam olarak
    `permission.scope`u okurdu. Testin ÖLÇTÜĞÜ şey değişmedi ama GEREKÇESİ değişti:
    `own` reddedilir çünkü uygulanmıyor değil, matristen DÜŞÜRÜLDÜ
    (`DROPPED_SCOPES`); uygulanan `limited`/`finance` ise artık serbestçe atanır.
    """
    role = await _role(seeded_db, "site_chief")
    before = await get_permission(seeded_db, role.id, "personnel")
    eski_seviye, eski_kapsam = before.access_level, before.scope

    with pytest.raises(PermissionLockedError):
        await update_role_permission(seeded_db, role.id, "personnel", AccessLevel.view, Scope.own)

    after = await get_permission(seeded_db, role.id, "personnel")
    assert (after.access_level, after.scope) == (eski_seviye, eski_kapsam)


async def test_kapsam_all_a_cekilebilir(seeded_db):
    """`all` HER ZAMAN serbesttir: kısıtı geri almak hiçbir kapıya takılmaz.

    (Eski docstring "yalanı geri almak" diyordu — kapsam uygulanmadığı dönemin
    dili. Kısıt artık gerçek; kaldırılması yine de serbest kalmalı.)
    """
    role, module_key, _ = await _non_all_scope_cell(seeded_db)

    updated = await update_role_permission(
        seeded_db, role.id, module_key, AccessLevel.view, Scope.all
    )

    assert updated.scope is Scope.all


# --------------------------------------------------------------------------- #
# DÜŞEN KAPSAMLAR — kullanıcı kararı 2026-09-19 (bkz. test_izin_kapsami_bekcisi)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dusen", [Scope.own, Scope.project, Scope.stock])
async def test_DUSEN_kapsam_MEVCUT_OLSA_BILE_geri_yazilamaz(seeded_db, dusen: Scope):
    """🔴 KAÇAĞIN TAM YERİ — ve bu test bir SAHTE-YEŞİLDEN doğdu.

    İlk hâlinde satırın kapsamı `all` iken `own` göndermeyi deniyordum; o yolu
    ESKİ fren de (`scope is not all and scope != mevcut`) zaten reddediyordu,
    yani test kodu değiştirmeden YEŞİL geçti ve hiçbir şey ölçmedi.

    Gerçek açık, satırın kapsamı ZATEN düşen bir kapsam olduğunda ortaya çıkar:
    eski fren *"mevcut kapsamı geri göndermek serbesttir"* dediği için
    `own → own` isteği GEÇİYORDU. Migration canlı satırları `all`a çekse bile bu
    yol açık kalsaydı, uygulanmayan kapsam ekrandan YENİDEN doğardı.
    """
    role = await _role(seeded_db, "site_chief")
    permission = await get_permission(seeded_db, role.id, "progress_payments")
    permission.scope = dusen  # canlıda migration ÖNCESİ hâl
    await seeded_db.flush()

    with pytest.raises(PermissionLockedError):
        await update_role_permission(
            seeded_db, role.id, "progress_payments", AccessLevel.view, dusen
        )


async def test_ALL_kapsami_HER_ZAMAN_serbesttir(seeded_db):
    """🔴 POZİTİF KONTROL — fren "her kapsamı reddet" hâline gelirse yönetici
    hiçbir izni değiştiremez olurdu."""
    role = await _role(seeded_db, "site_chief")
    updated = await update_role_permission(
        seeded_db, role.id, "progress_payments", AccessLevel.view, Scope.all
    )
    assert updated.scope is Scope.all


# --------------------------------------------------------------------------- #
# UYGULANAN KAPSAMLAR — `limited` / `finance` (2026-09-19, alan maskesi kurulduktan SONRA)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "yazan",
    [
        AccessLevel.draft,
        AccessLevel.request,
        AccessLevel.approve,
        AccessLevel.full,
        AccessLevel.admin,
    ],
)
@pytest.mark.parametrize("maskeleyen", [Scope.limited, Scope.finance])
async def test_MASKELEYEN_kapsam_YAZAN_seviyeyle_BIRLESEMEZ(
    seeded_db, maskeleyen: Scope, yazan: AccessLevel
):
    """🔴 Maskeli veri YAZMA yüzeyine DÜŞEMEZ — bekçisi buydu ve yoktu.

    `frontend/src/lib/masked.ts` şunu YAZILI olarak varsayar: *"o ekranlar `full`
    yetki ister ve `full` izin matrisinde yalnız `Scope.all` ile gelir"*. Bu
    varsayımı hiçbir şey uygulamıyordu: kapı yalnız KAPSAM değişimine bakıyordu,
    SEVİYE serbestti. `full` + `finance` hücresi oluşturulduğunda maskelenmiş
    (`None`) metraj yazma formuna düşer ve `maskesiz()` RENDER anında atar —
    React ağacı çöker, sayfa beyaz kalır.

    `none`/`view` serbest kalır (pozitif kontrolü üstteki testtedir): salt-okuma
    yüzeyi maskeli değeri zaten "—" diye basar.

    🔴 Modül `contracts` (KABLOLU) — `personnel` DEĞİL: `personnel` üzerinde
    çalıştırılsaydı kalan iş #4'ün yeni MODÜL kapısı bu testten ÖNCE devreye
    girer ve test aslında hangi kapıyı ölçtüğünü kaybederdi (yine
    `PermissionLockedError` alırdı ama SEVİYE+KAPSAM bileşimi hiç sınanmazdı).
    """
    role = await _role(seeded_db, "site_chief")
    before = await get_permission(seeded_db, role.id, "contracts")
    eski = (before.access_level, before.scope)

    with pytest.raises(PermissionLockedError):
        await update_role_permission(seeded_db, role.id, "contracts", yazan, maskeleyen)

    after = await get_permission(seeded_db, role.id, "contracts")
    assert (after.access_level, after.scope) == eski, "Reddedilen istek satırı DEĞİŞTİRMEMELİ"


async def test_RED_METINLERI_HENUZ_UYGULANMIYOR_DEMEZ(seeded_db):
    """🔴 Metin de bir sözleşmedir: "Kapsam kısıtı HENÜZ UYGULANMIYOR" artık YALAN.

    Kapsam 2026-09-19'da uygulandı; o cümleyi okuyan yönetici (ve bir sonraki
    geliştirici) mekanizmanın hiç olmadığına inanırdı. Ayrıca bu test DÜŞEN
    kapsamın KENDİ metnini çakar: iki ret dalı (düşen / maskesi yok) aynı
    gerekçeye çökerse yönetici "kaldırıldı" yerine "maske tanımlı değil" okur ve
    yanlış yere bakar.
    """
    role = await _role(seeded_db, "site_chief")

    with pytest.raises(PermissionLockedError) as dusen:
        await update_role_permission(seeded_db, role.id, "personnel", AccessLevel.view, Scope.own)
    with pytest.raises(PermissionLockedError) as seviye:
        # `contracts` (KABLOLU) — bu dal SEVİYE+KAPSAM ret metnini ölçer; `personnel`
        # (kablosuz) olsaydı kalan iş #4'ün MODÜL kapısı araya girer ve metin
        # gerekçesi burada iddia edilenden FARKLI bir dala (modül eksenine) ait olurdu.
        await update_role_permission(
            seeded_db, role.id, "contracts", AccessLevel.full, Scope.finance
        )

    metinler = [str(dusen.value), str(seviye.value)]
    assert not any("henüz uygulanmıyor" in metin for metin in metinler), metinler
    assert "kaldırıldı" in str(dusen.value), "Düşen kapsamın kendi gerekçesi kalmalı"


# --------------------------------------------------------------------------- #
# MODÜL EKSENİ — kalan iş #4 (2026-09-23): ATANABİLİR ile KABLOLU eşitlendi
# --------------------------------------------------------------------------- #


async def test_IZN_B4_limited_artik_HICBIR_modulde_atanamaz(seeded_db):
    """IZN-B4a: altı eski modül de yeni maskeye geçti, eski köprüyü taşıyan modül kalmadı
    (eski köprü söküldü, IZN-B6a). Eski `limited`/`finance` hücre yazımı her modülde reddedilir.
    Bu dosyadaki eski kapsam testleri B6'da (eski matris sökümü) birlikte silinir."""
    role = await _role(seeded_db, "site_chief")
    with pytest.raises(PermissionLockedError):
        await update_role_permission(seeded_db, role.id, "sites", AccessLevel.view, Scope.limited)

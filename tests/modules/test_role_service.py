import uuid

import pytest
from sqlalchemy import select

from app.core.access import DROPPED_SCOPES, AccessLevel, Scope
from app.core.errors import NotFoundError, PermissionLockedError
from app.core.field_scope import gizlenen_kova
from app.modules.roles.models import SYSTEM_ADMIN_KEY, Module, Role, RolePermission
from app.modules.roles.repository import get_permission
from app.modules.roles.scope_wiring import kablolu_moduller
from app.modules.roles.service import rename_role
from tests._legacy_permission_yardimcisi import update_role_permission


async def _role(session, key: str) -> Role:
    return (await session.execute(select(Role).where(Role.key == key))).scalar_one()


async def test_permission_can_be_raised_for_normal_role(seeded_db):
    role = await _role(seeded_db, "site_chief")
    updated = await update_role_permission(
        seeded_db, role.id, "progress_payments", AccessLevel.approve, Scope.all
    )
    assert updated.access_level is AccessLevel.approve


async def test_permission_can_be_lowered_for_normal_role(seeded_db):
    role = await _role(seeded_db, "patron")
    updated = await update_role_permission(
        seeded_db, role.id, "payroll", AccessLevel.view, Scope.all
    )
    assert updated.access_level is AccessLevel.view


async def test_system_admin_permissions_are_locked(seeded_db):
    """Kilitlenme koruması: system_admin izin satırları hiç kimse tarafından değiştirilemez."""
    role = await _role(seeded_db, "system_admin")
    with pytest.raises(PermissionLockedError):
        await update_role_permission(seeded_db, role.id, "settings", AccessLevel.none, Scope.all)


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
        await update_role_permission(
            seeded_db, patron.id, "olmayan_modul", AccessLevel.view, Scope.all
        )


async def test_update_permission_unknown_role_raises_not_found(seeded_db):
    with pytest.raises(NotFoundError):
        await update_role_permission(
            seeded_db, uuid.uuid4(), "dashboard", AccessLevel.view, Scope.all
        )


async def test_update_permission_system_admin_still_locked(seeded_db):
    sysadmin = await _role(seeded_db, "system_admin")
    with pytest.raises(PermissionLockedError):
        await update_role_permission(
            seeded_db, sysadmin.id, "dashboard", AccessLevel.view, Scope.all
        )


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
    diyordu. O cümle artık YANLIŞ — `core/permissions.actor_scope` tam olarak
    `permission.scope`u okur. Testin ÖLÇTÜĞÜ şey değişmedi ama GEREKÇESİ değişti:
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


async def test_seed_kapsami_korunurken_seviye_degistirilebilir(seeded_db):
    """Seed satırları AYNEN kalır: `all` dışı kapsam taşıyan bir hücrenin seviyesi

    düzenlenebilmeli. 🔴 Bu test eskiden kapının "mevcut kapsamı geri göndermek
    serbesttir" MUAFİYETİNİ ölçüyordu; o muafiyet kaldırıldı (kapı artık mevcut
    değere hiç bakmaz, kapsamın UYGULANIP uygulanmadığına bakar). Ölçtüğü davranış
    aynı kaldığı için test de kaldı — ama gerekçesi artık `limited`/`finance`in
    uygulanmış olmasıdır. Gönderilen seviye `none`: yazan seviye + maskeleyen
    kapsam bileşimi ayrıca reddedilir (bkz. `test_MASKELEYEN_kapsam_*`).
    """
    role, module_key, mevcut_kapsam = await _non_all_scope_cell(seeded_db)

    updated = await update_role_permission(
        seeded_db, role.id, module_key, AccessLevel.none, mevcut_kapsam
    )

    assert (updated.access_level, updated.scope) == (AccessLevel.none, mevcut_kapsam)


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


@pytest.mark.parametrize("seviye", [AccessLevel.none, AccessLevel.view])
@pytest.mark.parametrize("uygulanan", [Scope.limited, Scope.finance])
async def test_UYGULANAN_kapsam_ATANABILIR(seeded_db, uygulanan: Scope, seviye: AccessLevel):
    """🔴 POZİTİF KONTROL — kapsam artık UYGULANIYOR, o hâlde ATANABİLMELİ.

    Eski fren `scope is not all and scope != permission.scope` diyordu; yani
    kapsamı `all` olan bir hücreye `limited`/`finance` HİÇ yazılamıyordu ve
    mekanizma yalnız seed'deki hücrelerde yaşayabiliyordu. Bu test o tek yönlü
    kapıyı çakar: daraltma da genişletme kadar mümkün olmalı.

    🔴 Modül `contracts` (KABLOLU) — `personnel` DEĞİL. Kalan iş #4 (2026-09-23)
    ile `update_role_permission` MODÜL eksenli üçüncü bir kapı kazandı:
    `personnel` köprüsü YOK, `contracts`ınki VAR. Bu test kapsamın atanabildiğini
    ölçmek içindir; kablolu olmayan bir modülle çalıştırılırsa artık YENİ kapıya
    çarpar ve ölçtüğü şey değişir (bkz. `test_MODUL_EKSENLI_*`).
    """
    role = await _role(seeded_db, "site_chief")
    before = await get_permission(seeded_db, role.id, "contracts")
    assert before.scope is Scope.all, "Testin dayanağı: bu hücre seed'de `all`"

    updated = await update_role_permission(seeded_db, role.id, "contracts", seviye, uygulanan)

    assert updated.scope is uygulanan
    okunan = await get_permission(seeded_db, role.id, "contracts")
    assert (okunan.access_level, okunan.scope) == (seviye, uygulanan), "Yazma KALICI olmalı"


async def test_UYGULANAN_kapsamlar_BIRBIRINE_cevrilebilir(seeded_db):
    """Seed'li bir `limited` hücresi `finance`a (ya da tersi) çevrilebilmeli.

    Eski fren yalnızca MEVCUT kapsamın geri gönderilmesine izin verdiği için
    iki uygulanan kapsam arasındaki geçiş de kapalıydı.
    """
    role, module_key, mevcut = await _non_all_scope_cell(seeded_db)
    hedef = Scope.finance if mevcut is Scope.limited else Scope.limited

    updated = await update_role_permission(seeded_db, role.id, module_key, AccessLevel.view, hedef)

    assert updated.scope is hedef


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


def test_ATANABILIR_kapsam_listesi_IKI_KAYNAKTAN_TURETILIR() -> None:
    """🔴 SAYI değil BEKÇİ: "uygulanan kapsamlar" üçüncü bir elle yazılmış liste OLAMAZ.

    İki gerçek kaynak var ve birbirini tamamlamalı: `core.access.DROPPED_SCOPES`
    (matristen DÜŞEN) ve `core.field_scope` maskesi (UYGULANAN). `Scope`a yeni
    bir üye eklenip ikisinden birine yazılmazsa bu test kırılır ve ekleyeni
    seçim yapmaya zorlar: ya maskeyi yaz ya düşenlere koy. Aksi hâlde
    `update_role_permission` ya uygulanmayan bir kapsamı atattırır (bugün
    onarılan kusurun aynısı) ya da uygulanan bir kapsamı sessizce reddeder.
    """
    maskeleyen = {kapsam for kapsam in Scope if gizlenen_kova(kapsam) is not None}

    assert set(Scope) - DROPPED_SCOPES - {Scope.all} == maskeleyen, (
        "`Scope` üyeleri ile maske/düşen listeleri ayrıştı: her üye ya "
        "`DROPPED_SCOPES` içinde ya `field_scope` maskesinde olmalı (`all` hariç)."
    )


async def test_MASKESI_KALDIRILAN_kapsam_ANINDA_ATANAMAZ(seeded_db, monkeypatch):
    """🔴 Kapı `field_scope`u GERÇEKTEN okuyor mu — yoksa elle yazılmış bir liste mi?

    Üstteki `test_ATANABILIR_kapsam_listesi_*` iki kaynağın tümleyen kalmasını
    çakar ama kapının o kaynağa BAĞLI olduğunu ölçmez: gövdede
    `scope in {limited, finance}` yazsaydı o test de bu testin pozitif kontrolü de
    yeşil kalırdı. Burada maske ÇALIŞMA ANINDA kaldırılır; kapı türetiyorsa
    kapsam aynı anda atanamaz olur, elle listeliyorsa 200 dönmeye devam eder.

    Özel (`_`) sözlüğe dokunmak bilinçlidir: ölçülen şey tam olarak o bağlantıdır.
    """
    from app.core import field_scope

    monkeypatch.delitem(field_scope._GIZLENEN, Scope.limited)
    role = await _role(seeded_db, "site_chief")

    with pytest.raises(PermissionLockedError):
        await update_role_permission(
            seeded_db, role.id, "personnel", AccessLevel.view, Scope.limited
        )


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


async def test_MODUL_EKSENLI_kapsam_kablolu_OLMAYAN_modulde_REDDEDILIR(seeded_db):
    """🔴 POZİTİF KONTROL — envanter kaydı #4'ün taşıyıcı iddiasının ÖLÇÜMÜ.

    Onarım ÖNCESİ: `payroll` hücresine `view/limited` KABUL ediliyordu (200) —
    `payroll` routerı düz `APIRoute`, `PayrollLineResponse` para alanlarını tam
    değeriyle dönüyordu; yönetici ayrı yetki verdiğini sanıyor, ikisi de her
    şeyi gösteriyordu. `payroll` `kablolu_moduller()`de YOKTUR (ölçüldü,
    `tests/modules/test_scope_wiring.py`). Bu test o kabulü ÇAKAR: aynı istek
    artık `PermissionLockedError` vermeli.
    """
    assert "payroll" not in kablolu_moduller(), "Testin dayanağı: payroll KABLOSUZ olmalı"
    role = await _role(seeded_db, "hr_manager")
    before = await get_permission(seeded_db, role.id, "payroll")
    eski = (before.access_level, before.scope)

    with pytest.raises(PermissionLockedError):
        await update_role_permission(seeded_db, role.id, "payroll", AccessLevel.view, Scope.limited)

    after = await get_permission(seeded_db, role.id, "payroll")
    assert (after.access_level, after.scope) == eski, "Reddedilen istek satırı DEĞİŞTİRMEMELİ"


async def test_MODUL_EKSENLI_kapsam_KABLOLU_modulde_SERBESTTIR(seeded_db):
    """🔴 POZİTİF KONTROL — yeni kapı SIRADAN kablolu modülleri KİLİTLEMEMELİ.

    "Her modülü reddet" hâline gelen bozuk bir kapı da bu testin öncekiyle
    birlikte yakalayacağı hatadır: kablolu bir modülde aynı istek SERBEST
    kalmalı.
    """
    assert "sites" in kablolu_moduller(), "Testin dayanağı: sites KABLOLU olmalı"
    role = await _role(seeded_db, "field_engineer")

    updated = await update_role_permission(
        seeded_db, role.id, "sites", AccessLevel.view, Scope.limited
    )

    assert updated.scope is Scope.limited

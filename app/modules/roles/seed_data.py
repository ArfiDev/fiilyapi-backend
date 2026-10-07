"""Rol/modül/izin matrisinin başlangıç değerleri — spec §5.1 ve §5.2.

Bu yalnızca ilk kurulum değeridir. Kullanıcı İzin Matrisi ekranından her hücreyi
değiştirebilir; tek istisna system_admin rolüdür (kilitlenme koruması, spec §5.0).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.sayfalar import (
    MODULSUZ_VARSAYILAN,  # noqa: F401 — bekçiler seed_data üzerinden de okur
    SAYFA_ANAHTARLARI,
    HiddenCategory,
    PageLevel,
    sayfa_matrisi,
)
from app.modules.roles.models import (
    SYSTEM_ADMIN_KEY,
    Role,
    RoleHiddenField,
    RolePagePermission,
)

ROLES: list[dict] = [
    {
        "key": "system_admin",
        "name": "Sistem Yöneticisi",
        "emoji": "🛡️",
        "is_system": True,
        "description": "Tüm modüller · Tüm projeler · Ayarlar · Kullanıcı yönetimi · Silme yetkisi",
    },
    {
        "key": "patron",
        "name": "Patron",
        "emoji": "👔",
        "is_system": False,
        "description": "Tüm modüller · Tüm projeler (ayarlar hariç)",
    },
    {
        "key": "site_chief",
        "name": "Şantiye Şefi",
        "emoji": "👷",
        "is_system": False,
        "description": "Günlük kayıt, puantaj, stok görüntüle",
    },
    {
        "key": "field_engineer",
        "name": "Saha Mühendisi",
        "emoji": "📐",
        "is_system": False,
        "description": "Günlük kayıt, hakediş taslağı, puantaj görüntüle",
    },
    {
        "key": "hr_manager",
        "name": "İK Müdürü",
        "emoji": "👥",
        "is_system": False,
        "description": "Personel, puantaj, bordro",
    },
    {
        "key": "accounting",
        "name": "Muhasebe",
        "emoji": "📒",
        "is_system": False,
        "description": "Yevmiye, bordro, hakediş onay, e-fatura",
    },
    {
        "key": "project_manager",
        "name": "Proje Müdürü",
        "emoji": "🏗",
        "is_system": False,
        "description": "Proje görünümü, raporlar, hakediş onay",
    },
    {
        "key": "procurement",
        "name": "Satınalma",
        "emoji": "🛒",
        "is_system": False,
        "description": "Stok, satınalma, teklif, tedarikçi",
    },
]

# Kısayollar — matrisi okunur tutmak için.
_A = AccessLevel.admin  # ✓ Süper (silme dahil)
_F = AccessLevel.full  # ✓ Tam (silme hariç)
_N = AccessLevel.none  # —
_V = AccessLevel.view  # Görüntüle
#: 🔴 Kapsam 2026-09-19'da `project` → `all` oldu: proje kapsamı
#: proje ekibinden (`project_members`) sürülür ve `progress_payments/repository.py` onu
#: ZATEN uygular. İki mekanizmanın aynı kısıtı iki yerden söylemesi, bir gün
#: ayrışmaları demekti.
_DRF = AccessLevel.draft  # Taslak
_REQ = AccessLevel.request  # Talep
_APR = AccessLevel.approve  # Onay

# Sütun sırası — MATRIX'teki her satır bu sırayla okunur.
ROLE_ORDER = [
    "system_admin",
    "patron",
    "site_chief",
    "field_engineer",
    "hr_manager",
    "accounting",
    "project_manager",
    "procurement",
]

# Spec §5.2 matrisi.
MATRIX: dict[str, list[AccessLevel]] = {
    #                    sysadmin patron  şef    saha   İK     muhasebe  PM     satınalma
    "dashboard": [_A, _F, _V, _V, _V, _V, _F, _N],
    # 🔴 2026-09-19 (kullanıcı kararı): bu satır ESKİDEN `_OWN`/`_FIN`/`_PRJ`/
    #    `_STK` taşıyordu ve DÖRDÜ DE UYGULANMIYORDU. Ölçüldü: `GET /approvals`
    #    bilerek kapısızdır ve dönen küme zaten "bu adım SANA düştü" olgusuyla
    #    sınırlıdır; `own`u satır süzgeci yapmak `_pending_filter`in "Bekçi 5"ini
    #    (kendi evrakını onaylayamazsın) TERS ÇEVİRİRDİ. Bekçisi
    #    `tests/modules/test_izin_kapsami_bekcisi.py`.
    "approvals": [_A, _F, _V, _V, _V, _V, _V, _V],
    # dashboard satirinin aynisi: proje kartlari ayni gorunurluk yuzeyi,
    # asil suzgec proje ekibi `project_members` (IZN-B3).
    "projects": [_A, _F, _V, _V, _V, _V, _F, _N],
    # spec §5.1 + kullanici karari 2026-07-28. Taban profil projects satiridir;
    # TEK FARK Satinalma: projects=_N iken sites=_LIM. "Projeyi goremeyen ama
    # santiyesini goren rol" tutarsiz gorunur ama BILINCLI istisnadir ve kullanici
    # tarafindan onaylanmistir — tutarlilik adina geri alinmamalidir.
    # Bolum AYRI izin modulu degildir: bolum santiyenin ic kirilimidir, sites
    # izni ikisini de kapsar (spec §4).
    "sites": [_A, _F, _V, _V, _V, _V, _F, _V],
    "site_diary": [_A, _F, _F, _F, _N, _N, _V, _N],
    "timesheet": [_A, _F, _F, _V, _F, _V, _N, _N],
    "personnel": [_A, _F, _V, _V, _F, _F, _V, _N],
    "payroll": [_A, _F, _N, _N, _F, _F, _N, _N],
    "inventory": [_A, _F, _V, _V, _N, _N, _V, _F],
    "procurement": [_A, _F, _REQ, _REQ, _N, _N, _APR, _F],
    "progress_payments": [_A, _F, _DRF, _DRF, _N, _APR, _APR, _N],
    "accounting": [_A, _F, _N, _N, _N, _F, _V, _N],
    "invoicing": [_A, _F, _N, _N, _N, _F, _V, _N],
    "treasury": [_A, _F, _N, _N, _N, _F, _V, _N],
    "settings": [_A, _N, _N, _N, _N, _N, _N, _N],
    "user_management": [_A, _N, _N, _N, _N, _N, _N, _N],
    # spec §4 kullanici karari: site_chief=_LIM (gorur), field_engineer=_N
    # (gormez) — "sites" satirinda ikisi birebir ayni oldugu icin bu ayrim
    # ancak ayri modulle mumkun. accounting/project_manager seviyeleri
    # "sites" satirindan turetildi. procurement=_LIM (kullanici karari,
    # 2026-07-30): satinalma malzemeyi poz uzerinden aliyor, teklif/siparis
    # akisi poz listesine bakmayi gerektiriyor — artik "sites" satiriyla
    # birebir ayni (fix/boq-procurement-permission).
    "boq": [_A, _F, _V, _N, _N, _V, _F, _V],
    # spec §5 (P5): site_chief/field_engineer/hr_manager/procurement = none —
    # taşeron sözleşmesi görmemeli. accounting = view/finance (mali görünürlük
    # deseni, oluşturmaz). project_manager = full (taşeron sözleşmesini
    # pratikte proje müdürü yapar).
    "contracts": [_A, _F, _N, _N, _N, _V, _F, _N],
    # P8 spec §8 S1: `contracts` satırıyla BİREBİR aynı seviyeler — gerekçe de aynı.
    # site_chief/field_engineer/hr_manager/procurement = none: satış bedeli, alıcı
    # TCKN'si ve tahsilat planı saha/İK/satınalma rollerini ilgilendirmez (en az
    # ayrıcalık). accounting = view/finance: tahsilatı mali gözle izler ama satış
    # kaydı AÇMAZ (invoicing/treasury/contracts'taki mali görünürlük deseni).
    # project_manager = full: ayrı bir "satış müdürü" rolü YOK; satışı pratikte
    # proje müdürü yönetir. Silme yalnız admin'de (full silmeyi kapsamaz).
    "sales": [_A, _F, _N, _N, _N, _V, _F, _N],
    # Belge çekirdeği spec §6: `contracts`/`sales`ten BİLİNÇLİ olarak AYRIŞIR —
    # arşiv gizli veri değil ORTAK hafızadır, hiçbir rol `_N` değildir.
    # site_chief/field_engineer = _F: sahanın belgesini (ruhsat, tutanak, fotoğraf)
    # zaten onlar üretir; yükleyemezlerse arşiv boş kalır. accounting = _F: fatura
    # ve sözleşme ekini muhasebe yükler (mali görünürlük deseni burada YETMEZ).
    # hr_manager/project_manager/procurement = _V: okurlar, arşive yazmazlar.
    # Silme yalnız system_admin'dedir (`_A`; `full` silmeyi kapsamaz).
    "documents": [_A, _F, _F, _F, _V, _F, _V, _V],
    # MK-1 spec §6: site_chief = _F (makineyi sahada o kullanır; çalışma ve
    # arıza kaydını o girer). field_engineer = _V (izler, kayıt açmaz).
    # hr_manager = _N (makine İK'yı ilgilendirmez — operatör ataması personel
    # modülünden değil buradan yapılır ama İK'nın karar yüzeyi değildir).
    # accounting = _F: makine hem VARLIK hem MALİYET yüzeyidir (alış bedeli,
    # amortisman süresi, kira bedeli) — `documents`taki gibi mali görünürlük
    # deseni burada YETMEZ. project_manager = _F, procurement = _V (kiralama
    # firması `suppliers`tan gelir, satınalma kartı okur ama açmaz).
    # Silme yalnız system_admin'dedir (`_A`; `full` silmeyi kapsamaz) — zaten
    # DELETE ucu YOKTUR, kullanımdan kaldırma `is_active=false` iledir.
    "equipment": [_A, _F, _F, _V, _N, _F, _F, _V],
    # AI-0b · kullanici karari 2026-08-29: "AI'i herkes KENDI KAPSAMINDA
    # kullanabilsin." Yani hicbir rol `_N` degildir.
    #
    # 🔴 system_admin = `_A` SECIM DEGIL ZORUNLULUKTUR: `test_seed_matrix.py::
    # test_system_admin_has_admin_level_everywhere` her modulde `admin` bekler.
    #
    # 🔴 patron `_F` DEGIL `_V`: `ai` modulunde `full` HICBIR SEY IFADE ETMEZ.
    # Yazma kapisi seviyede degil ROL ANAHTARINDADIR (`SYSTEM_ADMIN_KEY`,
    # spec §6.1). `_F` yazsaydik Izin Matrisi ekrani var OLMAYAN bir yetkiyi
    # ("Tam") gosterirdi. Anlamli seviye kumesi `{none, view}`; ekrandaki tek
    # gercek ayrim "AI'i kullanabilir / kullanamaz"dir.
    #
    # ⚠️ system_admin sutunu ekrandan DEGISTIRILEMEZ (`PermissionMatrix.tsx`
    # `readOnly = role.key === SYSTEM_ADMIN_KEY`), yani buraya `_V` yazilsaydi
    # sonsuza kadar `_V` kalirdi.
    "ai": [_A, _V, _V, _V, _V, _V, _V, _V],
    # PLN-B1 (§3.9 B1-8, CEO onayi 2026-09-25). Seviye esleme `progress_payments`
    # emsali: goruntule=view · butce/oran/ayar/dagitim yazma=draft · dondurma, taslak
    # silme (B3: rapor onayi + kilit acma)=approve · sirket katalogu/disiplin=full.
    # 🔴 sef = `_APR` BILINCLI: sef baseline dondurabilir ve rapor onaylayabilir.
    # Saha muhendisi `_DRF`: butce/dagitim yazar, donduramaz. Kapsam maskesi
    # BAGLANMAZ (adam-saat para degil) → limited/finance bu modulde atanamaz.
    "earned_value": [_A, _F, _APR, _DRF, _N, _V, _F, _N],
}


async def seed_reference_data(session: AsyncSession) -> None:
    """Rolleri ve 13 rolün sayfa hücrelerini yükler (IZN-B6b: `modules`/`role_permissions` yok).

    Idempotent: hangi başlangıç durumundan çalıştırılırsa çalıştırılsın sonuçta 8 rol + sayfa
    hücreleri bulunur; mevcut satırların üzerine yazılmaz.
    """
    existing_role_rows = (await session.execute(select(Role))).scalars().all()
    roles_by_key: dict[str, Role] = {role.key: role for role in existing_role_rows}
    for row in ROLES:
        if row["key"] in roles_by_key:
            continue
        role = Role(**row)
        session.add(role)
        roles_by_key[row["key"]] = role

    await session.flush()
    # IZN-B2: kapılar SAYFA HÜCRELERİNDEN karar verir → 7 eski rolün hücreleri de burada kurulur
    # (üretimde `izn_b1`/`izn_b2` migration'ları CANLI satırlardan türetir; bu, `create_all`
    # şemasıyla çalışan testler ve boş-DB kurulumu içindir). Idempotent.
    await _seed_page_cells(
        session, roles_by_key, tuple(r for r in ROLE_ORDER if r != SYSTEM_ADMIN_KEY)
    )


# ---------------------------------------------------------------------------
# IZN-B1 — sayfa bazlı izin modelinin başlangıç değerleri (IZN-PLAN §1.3, §1.4, §7).
#
# 🔴 Bu bölüm `ROLES`/`ROLE_ORDER`/`MATRIX`e DOKUNMAZ: o üçü dondurulmuş migration'larla
# BİREBİR eşit tutulur (`tests/modules/test_seed_migration_matches_seed_data.py`,
# `TKL-B7-TASARIM.md` §4.6.6). Yeni roller ayrı listelerde durur. Eşitlik bekçisi:
# `tests/modules/test_izn_b1_seed_migration_esitligi.py` (migration'a ELLE kopyalanan
# `IZN_*` / `MATRIX` / `MODULSUZ_VARSAYILAN` değerleriyle çakar; migration `app` import etmez).
# ---------------------------------------------------------------------------

IZN_ROLES: list[dict] = [
    {
        "key": "planning_engineer",
        "name": "Planlama Mühendisi",
        "emoji": "📐",
        "is_system": False,
        "description": "Planlama, adam-saat bütçesi, günlük kayıt ve ilerleme raporları",
    },
    {
        "key": "technical_office",
        "name": "Teknik Ofis",
        "emoji": "🗂️",
        "is_system": False,
        "description": "Sözleşme, iş kalemi ve teknik belge hazırlama",
    },
    {
        "key": "warehouse_keeper",
        "name": "Depo Sorumlusu",
        "emoji": "📦",
        "is_system": False,
        "description": "Stok giriş/çıkış, depo ve malzeme takibi",
    },
    {
        "key": "viewer",
        "name": "Görüntüleyici",
        "emoji": "👁️",
        "is_system": False,
        "description": "Tüm ekranları salt okunur görür; tutarlar gizli",
    },
    {
        "key": "finance_manager",
        "name": "Finans Müdürü",
        "emoji": "💰",
        "is_system": False,
        "description": "Muhasebe, bordro, fatura ve hazine; mali onaylar",
    },
    {
        "key": "cost_engineer",
        "name": "Maliyet Mühendisi",
        "emoji": "🧮",
        "is_system": False,
        "description": "Maliyet ve sözleşme takibi, proje görünümü",
    },
]

IZN_ROLE_ORDER = [
    "planning_engineer",
    "technical_office",
    "warehouse_keeper",
    "viewer",
    "finance_manager",
    "cost_engineer",
]

# Yeni rollerin başlangıç hücreleri, ESKİ düzey/kapsam sözlüğüyle yazılır (yukarıdaki `MATRIX`le
# aynı kısayollar) ve `core/sayfalar.sayfa_matrisi` ile sayfa hücresine çevrilir — eski ve yeni
# roller TEK dönüşüm yolundan geçer. IZN-PLAN §7 (CEO, ekrandan düzeltilir):
#   Planlama Müh. = Saha Müh. sütunu + Planlama Düzenler (earned_value `draft`, boq `none`, Onaylar
#   YOK; şirket katalogları `IZN_SAYFA_ISTISNALARI`nda) · Teknik Ofis = PM'in sözleşme/iş kalemi
#   satırları (`full`) [+ projects/sites `view`: PLANDA YOK, proje sayfalarına girebilsin diye
#   bilinçli sapma] · Depo = Satınalma'nın stok satırları · Görüntüleyici = her yer Görür, Ayarlar
#   hariç (`IZN_SAYFA_ISTISNALARI`) · Finans Müdürü = Muhasebe sütunu birebir (`_FIN` → `_V`; mali
#   Onaylar Muhasebe'nin `full` hücrelerinden zaten gelir) · Maliyet Müh. = PM Görür + Sözleşmeler
#   Düzenler (`contracts` `full`: sayfa eşiği; Teklif "Dönüştür" Onaylar'ı `projects:admin` ister).
# `_LIM` KULLANILMAZ: bu roller eski kapıdan zaten geçemez; "tutarları gizle" `IZN_HIDDEN_FIELDS`te.
IZN_MATRIX: dict[str, list[AccessLevel]] = {
    #                      plan   teknik depo   görünt finans maliyet
    "dashboard": [_V, _N, _N, _V, _V, _V],
    "approvals": [_V, _V, _V, _V, _V, _V],
    "projects": [_V, _V, _N, _V, _V, _V],
    "sites": [_V, _V, _N, _V, _V, _V],
    "site_diary": [_F, _V, _N, _V, _N, _V],
    "timesheet": [_V, _N, _N, _V, _V, _N],
    "personnel": [_V, _N, _N, _V, _F, _V],
    "payroll": [_N, _N, _N, _V, _F, _N],
    "inventory": [_V, _N, _F, _V, _N, _V],
    "procurement": [_REQ, _N, _V, _V, _N, _V],
    "progress_payments": [_DRF, _N, _N, _V, _APR, _V],
    "accounting": [_N, _N, _N, _V, _F, _V],
    "invoicing": [_N, _N, _N, _V, _F, _V],
    "treasury": [_N, _N, _N, _V, _F, _V],
    "settings": [_N, _N, _N, _N, _N, _N],
    "user_management": [_N, _N, _N, _N, _N, _N],
    "boq": [_N, _F, _N, _V, _V, _V],
    "contracts": [_N, _F, _N, _V, _V, _F],
    "sales": [_N, _N, _N, _V, _V, _V],
    "documents": [_F, _F, _V, _V, _F, _V],
    "equipment": [_V, _N, _V, _V, _F, _V],
    "ai": [_V, _V, _V, _V, _V, _V],
    "earned_value": [_DRF, _V, _N, _V, _V, _V],
}

#: Yeni rollerin gizli alan bayrakları (rol başına kutucuk). Satır var = gizli.
IZN_HIDDEN_FIELDS: dict[str, tuple[HiddenCategory, ...]] = {
    # IZN-B4c (CEO): tutarı gizli rol ücret/kişisel alanları da görmez (`maas_kisisel`).
    "planning_engineer": (HiddenCategory.tum_tutarlar, HiddenCategory.maas_kisisel),
    "technical_office": (),
    "warehouse_keeper": (HiddenCategory.tum_tutarlar, HiddenCategory.maas_kisisel),
    "viewer": (HiddenCategory.tum_tutarlar, HiddenCategory.maas_kisisel),
    "finance_manager": (),
    "cost_engineer": (HiddenCategory.maas_kisisel,),
}

#: Yeni rollerin SAYFA düzeyindeki istisnaları (modül hücresiyle ifade edilemeyen kararlar;
#: IZN-PLAN §7): Planlama Müh. = Saha Müh. + "Planlama Düzenler" (şirket katalogları `full`
#: eşiğinde, Saha Müh.'nin `draft`ı onlara yetmez; Onaylar YOK) · Görüntüleyici: "Ayarlar hariç"
#: = TÜM `ayarlar.*` Görmez (kişisel Bildirimler/Görünüm dahil).
IZN_SAYFA_ISTISNALARI: dict[str, dict[str, tuple[PageLevel, bool]]] = {
    "planning_engineer": {
        "planlama.birim_oran_katalogu": (PageLevel.edit, False),
        "planlama.disiplin_yonetimi": (PageLevel.edit, False),
    },
    "viewer": {
        key: (PageLevel.none, False) for key in SAYFA_ANAHTARLARI if key.startswith("ayarlar.")
    },
}


def _rol_hucreleri(
    matrix: dict[str, list[AccessLevel]], order: list[str], role_key: str
) -> dict[str, AccessLevel]:
    index = order.index(role_key)
    return {module_key: cells[index] for module_key, cells in matrix.items()}


#: Sistem Yöneticisi HARİÇ 13 rolün başlangıç sayfa matrisi (rol → sayfa → (düzey, onay)).
#: Eski 7 rol, `MATRIX`ten (yedek hücre: migration'da canlı satırı OLMAYAN çift buna düşer);
#: 6 yeni rol `IZN_MATRIX`ten türetilir.
PAGE_MATRIX: dict[str, dict[str, tuple[PageLevel, bool]]] = {
    **{
        role_key: sayfa_matrisi(_rol_hucreleri(MATRIX, ROLE_ORDER, role_key))
        for role_key in ROLE_ORDER
        if role_key != SYSTEM_ADMIN_KEY
    },
    **{
        role_key: {
            **sayfa_matrisi(_rol_hucreleri(IZN_MATRIX, IZN_ROLE_ORDER, role_key)),
            **IZN_SAYFA_ISTISNALARI.get(role_key, {}),
        }
        for role_key in IZN_ROLE_ORDER
    },
}

#: ESKİ 7 rolün gizli alan kümesi (IZN-B4c madde 20, CEO onaylı). B1 migration'ı eski `limited`
#: kapsamını küresel `tum_tutarlar`a çevirmişti; yeni maske küresel olduğu için bu İK Müdürü'nün
#: ücret/bordroyu, Satınalma'nın kendi fiyatlarını görmesini engelliyordu. Burada AÇIK kategori
#: kümesi tutulur; tabloda olmayan eski rol (patron/accounting/project_manager) = boş küme.
#: Canlıya `izn_b4c_eski_rol_gizli_alanlari` migration'ı taşır (bekçisi
#: `tests/modules/test_izn_b4c_madde20_rol_kumeleri.py`).
_SAHA_VE_IK_GIZLI: tuple[HiddenCategory, ...] = (
    HiddenCategory.sozlesme_fiyat,
    HiddenCategory.maliyet_kar,
    HiddenCategory.banka_kasa,
    HiddenCategory.satis_alici,
)
ESKI_ROL_GIZLI_ALANLAR: dict[str, tuple[HiddenCategory, ...]] = {
    "hr_manager": _SAHA_VE_IK_GIZLI,
    "site_chief": _SAHA_VE_IK_GIZLI,
    "field_engineer": _SAHA_VE_IK_GIZLI,
    "procurement": (
        HiddenCategory.sozlesme_fiyat,
        HiddenCategory.satis_alici,
        HiddenCategory.banka_kasa,
    ),
}

#: Rol başına gizli alan kümesi (Sistem Yöneticisi hariç): eski roller `ESKI_ROL_GIZLI_ALANLAR`dan
#: (yoksa boş), yeni roller `IZN_HIDDEN_FIELDS`ten.
HIDDEN_FIELDS: dict[str, frozenset[HiddenCategory]] = {
    **{
        role_key: frozenset(ESKI_ROL_GIZLI_ALANLAR.get(role_key, ()))
        for role_key in ROLE_ORDER
        if role_key != SYSTEM_ADMIN_KEY
    },
    **{role_key: frozenset(cats) for role_key, cats in IZN_HIDDEN_FIELDS.items()},
}


async def seed_izn_reference_data(session: AsyncSession) -> None:
    """6 yeni rolü, `PAGE_MATRIX` hücrelerini ve gizli alan bayraklarını yükler.

    🔴 Üretimde bu fonksiyon ÇAĞRILMAZ: orada hücreleri `izn_b1` migration'ı CANLI
    `role_permissions` satırlarından türetir. Bu, testlerin (`create_all` şeması) aynı
    başlangıç durumunu kurması içindir. `seed_reference_data` (8 rol / 184 hücre) önce koşmuş
    olmalıdır; Sistem Yöneticisi hücre taşımaz. Idempotent: var olan çift yeniden yazılmaz.
    """
    roles_by_key: dict[str, Role] = {
        role.key: role for role in (await session.execute(select(Role))).scalars().all()
    }
    for row in IZN_ROLES:
        if row["key"] not in roles_by_key:
            role = Role(**row)
            session.add(role)
            roles_by_key[row["key"]] = role
    await session.flush()

    await _seed_page_cells(session, roles_by_key, tuple(PAGE_MATRIX))
    await session.flush()


async def _seed_page_cells(
    session: AsyncSession, roles_by_key: dict[str, Role], role_keys: tuple[str, ...]
) -> None:
    """`PAGE_MATRIX` hücrelerini ve `HIDDEN_FIELDS` bayraklarını yükler (idempotent)."""
    existing_pages = set(
        (
            await session.execute(select(RolePagePermission.role_id, RolePagePermission.page_key))
        ).all()
    )
    for role_key in role_keys:
        cells = PAGE_MATRIX[role_key]
        role = roles_by_key.get(role_key)
        if role is None:
            continue
        for page_key, (level, can_approve) in cells.items():
            if (role.id, page_key) in existing_pages:
                continue
            session.add(
                RolePagePermission(
                    role_id=role.id, page_key=page_key, level=level, can_approve=can_approve
                )
            )

    existing_hidden = set(
        (await session.execute(select(RoleHiddenField.role_id, RoleHiddenField.category))).all()
    )
    for role_key in role_keys:
        categories = HIDDEN_FIELDS[role_key]
        role = roles_by_key.get(role_key)
        if role is None:
            continue
        for category in sorted(categories, key=lambda c: c.value):
            if (role.id, category) not in existing_hidden:
                session.add(RoleHiddenField(role_id=role.id, category=category))
    await session.flush()

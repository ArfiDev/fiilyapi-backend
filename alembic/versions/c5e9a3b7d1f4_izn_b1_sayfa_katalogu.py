"""IZN-B1 — sayfa bazlı izin modeli: tablolar, 6 yeni rol, hücreler CANLI satırlardan

Karar kaynağı: KARARLAR §1.7 (IZN maddeleri) · IZN-PLAN §1.1–§1.4. Bu dilim EKLEYİCİDİR: hiçbir
uç kapısı, `role_permissions` satırı ya da kullanıcı etkilenmez; yalnız yeni veri kurulur ve
`GET /pages` + `/auth/me` üzerinden yayınlanır.

## Ne yapar
* `page_level` (none/view/edit) ve `hidden_category` (6 kategori) enumları.
* `role_page_permissions` (PK role_id+page_key; FK roles CASCADE; CHECK: onay ⇒ düzey ≠ none) ve
  `role_hidden_fields` (PK role_id+category; FK roles CASCADE).
* 6 yeni rol (`planning_engineer`, `technical_office`, `warehouse_keeper`, `viewer`,
  `finance_manager`, `cost_engineer`; `is_system=false`). Yeni rollere `role_permissions` satırı
  YAZILMAZ: eski kapı onları 403 ile dışarıda tutar (fail-closed); servis atamayı kilitler.
* Sayfa hücreleri (rol × 100 sayfa; Sistem Yöneticisi hücre TAŞIMAZ):
  - 🔴 **CANLI `role_permissions` satırlarından türetilir, kod sabitinden DEĞİL** (CEO kararı):
    canlıda hücreler ekrandan değiştirilmiş olabilir. Eski → yeni eşleme (IZN-PLAN §1.3 + CEO
    "genişleme yok"): her sayfanın bugünkü GERÇEK eşikleri (görme / yazma / onay eylemi; ESİK
    TABLOSU `PAGES`) vardır; hücre = eski seviye o eşiği gerçekten karşılıyorsa (Görür görme
    eşiğini, Düzenler yazma eşiğini, Onaylar onay eylemi eşiğini). Sayfa → eski modül eşlemesi
    `PAGES` (7 sayfanın eski modülü yok: `MODULSUZ_VARSAYILAN`).
  - Satırı OLMAYAN (rol, modül) çifti: seed hücresine (`MATRIX`; migration'a ELLE kopyalı —
    `app` IMPORT EDİLMEZ), özel rolde Görmez.
  - Ekrandan açılmış ÖZEL roller de taşınır (aynı yol).
  - 6 yeni rolün hücreleri `IZN_MATRIX`ten (aynı dönüşüm), gizli alanları `IZN_HIDDEN_FIELDS`ten.
* `role_hidden_fields`: eski roller için `limited` kapsamlı (erişimi olan) herhangi bir hücre ⇒
  `tum_tutarlar`; `finance` kapsamının karşılığı YOK (düşer).
* Sapma raporu: seed'den SAPAN canlı hücre sayısı, seed rollerinde seed'e düşen eksik çift sayısı,
  özel rollerde Görmez'e düşen eksik çift sayısı (ayrı), özel rol anahtarları ve eşikli eşlemenin
  eski sade eşlemeye göre KISTIĞI hücre sayısı/satırları WARNING olarak basılır; akışı durdurmaz.

## Ön koşul bekçisi
Aynı anahtarlı bir rol ekrandan elle açılmışsa migration `RAISE` etmez (korkuluk upgrade'i
patlatamaz): o rolü yeniden eklemez, WARNING basar ve hücrelerini var olan her rol gibi canlı
satırlarından türetir.

## Kilit
`roles` ve `role_permissions` SHARE ROW EXCLUSIVE, NOWAIT + savepoint ile hep-ya-hic alınır
(okuma serbest, yazma beklemez; e4a8c2d6f1b3 kalıbı); `SET LOCAL lock_timeout = '10s'` son
savunma. Downgrade'de `roles` ACCESS EXCLUSIVE (yeni tabloların FK tetikleyicilerini düşürmek
referans verilen tabloda AE ister) — kilit YÜKSELTME deadlock'u olmasın diye baştan, güçlü modda.

## Downgrade
Yeni tabloları ve enumları düşürür; 6 yeni rolü siler (yalnız `role_permissions` satırı OLMAYAN
yani bu migration'ın eklediği roller; elle açılmış çakışan rol KORUNUR). Eklenen roller bir
kullanıcıya atanmışsa fail-closed `RAISE` eder (açık mesaj): kullanıcı sessizce rolsüz/silinmiş
kalmaz, migration geri alınır.

Revision ID: c5e9a3b7d1f4
Revises: b7c3e9a1d5f2
Create Date: 2026-10-04

"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c5e9a3b7d1f4"
down_revision: str | Sequence[str] | None = "b7c3e9a1d5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `alembic.ini`: kök logger WARNING/stderr, `alembic` INFO; ikisi de deploy günlüğünde görünür.
logger = logging.getLogger("alembic.runtime.migration")

PAGE_TABLE = "role_page_permissions"
HIDDEN_TABLE = "role_hidden_fields"
LEVEL_ENUM = "page_level"
CATEGORY_ENUM = "hidden_category"
LEVELS = ("none", "view", "edit")
CATEGORIES = (
    "sozlesme_fiyat",
    "maliyet_kar",
    "maas_kisisel",
    "banka_kasa",
    "satis_alici",
    "tum_tutarlar",
)

SYSTEM_ADMIN_KEY = "system_admin"
TUM_TUTARLAR = "tum_tutarlar"
LIMITED_SCOPE = "limited"

LOCK_RETRY_INTERVAL_S = 0.2
LOCK_CEILING_S = 10.0
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
UPGRADE_LOCKS = (("roles", "SHARE ROW EXCLUSIVE"), ("role_permissions", "SHARE ROW EXCLUSIVE"))
DOWNGRADE_LOCKS = (
    ("roles", "ACCESS EXCLUSIVE"),
    (PAGE_TABLE, "ACCESS EXCLUSIVE"),
    (HIDDEN_TABLE, "ACCESS EXCLUSIVE"),
)

#: Rapor satırı tavanı (günlük taşmasın; sayılar her zaman eksiksiz basılır). 13 rol × 100 sayfa
#: içinde kısılan hücre sayısı bunun altında kalır, yani liste fiilen eksiksizdir.
MAX_DEVIATION_LINES = 500

# ---------------------------------------------------------------------------
# ELLE KOPYALAR (migration `app`i import etmez; bekçisi
# `tests/modules/test_izn_b1_seed_migration_esitligi.py`)
# ---------------------------------------------------------------------------

_A = ("admin", "all")
_F = ("full", "all")
_N = ("none", "all")
_V = ("view", "all")
_LIM = ("view", "limited")
_FIN = ("view", "finance")
_DRF = ("draft", "all")
_REQ = ("request", "all")
_APR = ("approve", "all")

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

#: Eski seed matrisi (a477fdf00fdf + uzantı migration'ları + kapsam düzeltmesinin bileşkesi).
MATRIX: dict[str, list[tuple[str, str]]] = {
    "dashboard": [_A, _F, _LIM, _LIM, _LIM, _FIN, _F, _N],
    "approvals": [_A, _F, _V, _V, _V, _V, _V, _V],
    "projects": [_A, _F, _LIM, _LIM, _LIM, _FIN, _F, _N],
    "sites": [_A, _F, _LIM, _LIM, _LIM, _FIN, _F, _LIM],
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
    "boq": [_A, _F, _LIM, _N, _N, _FIN, _F, _LIM],
    "contracts": [_A, _F, _N, _N, _N, _FIN, _F, _N],
    "sales": [_A, _F, _N, _N, _N, _FIN, _F, _N],
    "documents": [_A, _F, _F, _F, _V, _F, _V, _V],
    "equipment": [_A, _F, _F, _V, _N, _F, _F, _V],
    "ai": [_A, _V, _V, _V, _V, _V, _V, _V],
    "earned_value": [_A, _F, _APR, _DRF, _N, _V, _F, _N],
}

IZN_ROLES = [
    {
        "key": "planning_engineer",
        "name": "Planlama Mühendisi",
        "emoji": "📐",
        "description": "Planlama, adam-saat bütçesi, günlük kayıt ve ilerleme raporları",
    },
    {
        "key": "technical_office",
        "name": "Teknik Ofis",
        "emoji": "🗂️",
        "description": "Sözleşme, iş kalemi ve teknik belge hazırlama",
    },
    {
        "key": "warehouse_keeper",
        "name": "Depo Sorumlusu",
        "emoji": "📦",
        "description": "Stok giriş/çıkış, depo ve malzeme takibi",
    },
    {
        "key": "viewer",
        "name": "Görüntüleyici",
        "emoji": "👁️",
        "description": "Tüm ekranları salt okunur görür; tutarlar gizli",
    },
    {
        "key": "finance_manager",
        "name": "Finans Müdürü",
        "emoji": "💰",
        "description": "Muhasebe, bordro, fatura ve hazine; mali onaylar",
    },
    {
        "key": "cost_engineer",
        "name": "Maliyet Mühendisi",
        "emoji": "🧮",
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

IZN_MATRIX: dict[str, list[tuple[str, str]]] = {
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

IZN_HIDDEN_FIELDS: dict[str, tuple[str, ...]] = {
    "planning_engineer": ("tum_tutarlar",),
    "technical_office": (),
    "warehouse_keeper": ("tum_tutarlar",),
    "viewer": ("tum_tutarlar", "maas_kisisel"),
    "finance_manager": (),
    "cost_engineer": ("maas_kisisel",),
}

#: Yeni rollerin SAYFA düzeyindeki istisnaları (`seed_data.IZN_SAYFA_ISTISNALARI` kopyası).
IZN_SAYFA_ISTISNALARI: dict[str, dict[str, tuple[str, bool]]] = {
    "planning_engineer": {
        "planlama.birim_oran_katalogu": ("edit", False),
        "planlama.disiplin_yonetimi": ("edit", False),
    },
    "viewer": {
        "ayarlar.sirket_bilgileri": ("none", False),
        "ayarlar.bildirimler": ("none", False),
        "ayarlar.gorunum": ("none", False),
        "ayarlar.planlama": ("none", False),
        "ayarlar.kullanicilar": ("none", False),
        "ayarlar.rol_yonetimi": ("none", False),
        "ayarlar.sayfa_izinleri": ("none", False),
        "ayarlar.onay_rolleri": ("none", False),
        "ayarlar.bordro_oranlari": ("none", False),
        "ayarlar.entegrasyonlar": ("none", False),
        "ayarlar.yedekleme": ("none", False),
        "ayarlar.denetim_gunlugu": ("none", False),
        "ayarlar.gelistirme": ("none", False),
    },
}

#: Eski modül anahtarı OLMAYAN yedi sayfanın başlangıç düzeyi (bugün kapısız = herkese açık).
MODULSUZ_VARSAYILAN: dict[str, str] = {
    "genel.raporlar": "view",
    "mali.sirket_varliklari": "view",
    "ayarlar.bildirimler": "view",
    "ayarlar.gorunum": "view",
    "ayarlar.entegrasyonlar": "view",
    "ayarlar.yedekleme": "view",
    "ayarlar.gelistirme": "none",
}

#: (sayfa anahtarı, eski modül | None, onay eylemi var mı, eşik "görme|yazma|onay"):
#: `app/core/sayfalar.py`ın (`_ESIKLER`) kopyası. Eşik: "-" yok; koşullar "&" ile VE; koşul "f"
#: (sayfanın eski modülünde ≥ düzey) ya da "modul:f". Düzey harfleri n v d r a f x (x = admin).
PAGES: tuple[tuple[str, str | None, bool, str], ...] = (
    ("genel.gosterge_paneli", "dashboard", False, "v|-|-"),
    ("genel.onay_kutusu", "approvals", True, "n|-|f"),
    ("genel.fiil_ai", "ai", False, "v|-|-"),
    ("genel.raporlar", None, False, "-|-|-"),
    ("genel.projeler", "projects", False, "v|x|-"),
    ("genel.proje_takvimi", "projects", False, "v|-|-"),
    ("saha.puantaj", "timesheet", False, "v|f|-"),
    ("saha.makine_ekipman", "equipment", False, "v|f|-"),
    ("saha.makine_calisma", "equipment", False, "v|f|-"),
    ("saha.makine_yakit", "equipment", False, "v|f|-"),
    ("saha.makine_kira", "equipment", True, "v|f|f"),
    ("saha.gunluk_kayit", "site_diary", True, "v|f|x"),
    ("ik.personel", "personnel", False, "v|f|-"),
    ("ik.izin_yonetimi", "personnel", True, "v|f|f"),
    ("ik.belge_sertifika", "personnel", False, "v|f|-"),
    ("planlama.panel", "earned_value", False, "v|-|-"),
    ("planlama.adam_saat_butcesi", "earned_value", True, "v|d|a"),
    ("planlama.gunluk_rapor", "earned_value", True, "v|-|a"),
    ("planlama.haftalik_qurr", "earned_value", False, "v|-|-"),
    ("planlama.birim_oran_katalogu", "earned_value", False, "v|f|-"),
    ("planlama.disiplin_yonetimi", "earned_value", False, "v|f|-"),
    ("teklif.teklif_hazirlama", "contracts", True, "v|f|projects:x&f"),
    ("teklif.sablonlar", "contracts", False, "v|f|-"),
    ("teklif.sozlesmeler", "contracts", False, "v|f|-"),
    ("teklif.taseron_firmalar", "contracts", False, "v|f|-"),
    ("teklif.isveren_sozlesme", "contracts", False, "v|f|-"),
    ("teklif.poz_dagilimi", "contracts", False, "v|f|-"),
    ("teklif.taseron_sozlesme", "contracts", False, "v|f|-"),
    ("teklif.is_kalemi_katalogu", "contracts", False, "v|f|-"),
    ("stok.stok_depo", "inventory", False, "v|f|-"),
    ("stok.satinalma_talepleri", "procurement", True, "v|r|r"),
    ("stok.siparisler", "procurement", False, "v|f|-"),
    ("stok.tedarikciler", "procurement", False, "v|f|-"),
    ("stok.teklif_karsilastirma", "procurement", True, "v|f|f"),
    ("mali.satis", "sales", True, "v|f|f"),
    ("mali.satis_blok", "projects", False, "d|f|-"),
    ("mali.satis_unite", "projects", False, "d|f|-"),
    ("mali.satis_toplu_uretim", "projects", False, "f|f|-"),
    ("mali.satis_excel", "projects", False, "d|f|-"),
    ("mali.satis_paylasim", "projects", False, "d|f|-"),
    ("mali.yevmiye", "accounting", True, "v|f|f"),
    ("mali.hesap_plani", "accounting", False, "v|f|-"),
    ("mali.mizan", "accounting", False, "v|-|-"),
    ("mali.kdv_beyani", "accounting", False, "v|-|-"),
    ("mali.banka_mutabakati", "accounting", False, "v|-|-"),
    ("mali.donem_kapanisi", "accounting", True, "v|f|x"),
    ("mali.fatura", "invoicing", True, "v|f|f"),
    ("mali.hazine", "treasury", False, "v|f|-"),
    ("mali.cek_odeme", "treasury", True, "v|f|f"),
    ("mali.hakedis_isveren", "progress_payments", True, "v|d|x"),
    ("mali.hakedis_taseron", "progress_payments", True, "v|d|x"),
    ("mali.gelir_tablosu", "accounting", False, "v|-|-"),
    ("mali.bilanco", "accounting", False, "v|-|-"),
    ("mali.nakit_akisi", "accounting", False, "v|-|-"),
    ("mali.bordro", "payroll", True, "v|f|f"),
    ("mali.bordro_gecmis", "payroll", False, "v|-|-"),
    ("mali.sgk_bildirimi", "payroll", True, "v|f|f"),
    ("mali.sirket_varliklari", None, False, "-|-|-"),
    ("mali.belge_arsivi", "documents", False, "v|f|-"),
    ("proje.santiyeler", "projects", False, "v&sites:v|v&sites:f|-"),
    ("proje.ozet", "projects", False, "v|-|-"),
    ("proje.paylasim_tablosu", "projects", False, "v|-|-"),
    ("proje.is_kalemleri", "contracts", False, "v|f|-"),
    ("proje.isveren_hakedis", "progress_payments", True, "v|d|x"),
    ("proje.taseron_hakedis", "progress_payments", True, "v|d|x"),
    ("proje.belgeler", "documents", False, "v|f|-"),
    ("santiye.bolumler", "sites", False, "v|f|-"),
    ("santiye.is_kalemleri", "boq", False, "v|f|-"),
    ("santiye.puantaj", "timesheet", False, "v|f|-"),
    ("santiye.stok", "inventory", False, "v|f|-"),
    ("santiye.hakedisler", "progress_payments", True, "v|d|x"),
    ("santiye.gunluk_kayit", "site_diary", True, "v|f|x"),
    ("santiye.belgeler", "documents", False, "v|f|-"),
    ("santiye.bolum_dagilimi", "boq", False, "v|f|-"),
    ("santiye.gunluk_ozet", "site_diary", False, "v|-|-"),
    ("santiye.gunluk_planlama", "site_diary", False, "v|f|-"),
    ("santiye.adam_saat_butcesi", "earned_value", True, "v|d|a"),
    ("santiye.planlama_paneli", "earned_value", False, "v|-|-"),
    ("santiye.gunluk_ilerleme_raporu", "earned_value", True, "v|-|a"),
    ("santiye.haftalik_qurr", "earned_value", False, "v|-|-"),
    ("bolum.detay", "sites", False, "v|f|-"),
    ("bolum.is_kalemleri", "boq", False, "v|-|-"),
    ("bolum.puantaj", "timesheet", False, "v|-|-"),
    ("bolum.malzeme", "inventory", False, "v|-|-"),
    ("bolum.hakedis", "progress_payments", False, "v|-|-"),
    ("bolum.gunluk_kayit", "site_diary", False, "v|-|-"),
    ("bolum.gunluk_kayit_detay", "site_diary", True, "v|f|x"),
    ("ayarlar.sirket_bilgileri", "settings", False, "n|f|-"),
    ("ayarlar.bildirimler", None, False, "-|-|-"),
    ("ayarlar.gorunum", None, False, "-|-|-"),
    ("ayarlar.planlama", "earned_value", False, "v|d|-"),
    ("ayarlar.kullanicilar", "user_management", False, "v|f|-"),
    ("ayarlar.rol_yonetimi", "user_management", False, "v|x|-"),
    ("ayarlar.sayfa_izinleri", "user_management", False, "v|x|-"),
    ("ayarlar.onay_rolleri", "approvals", False, "n|x|-"),
    ("ayarlar.bordro_oranlari", "payroll", False, "v|x|-"),
    ("ayarlar.entegrasyonlar", None, False, "-|-|-"),
    ("ayarlar.yedekleme", None, False, "-|-|-"),
    ("ayarlar.denetim_gunlugu", "settings", False, "v|-|-"),
    ("ayarlar.gelistirme", None, False, "-|-|-"),
)

Cells = dict[str, tuple[str, str]]


class LockCeilingExceededError(RuntimeError):
    """Kilitler tavan süresinde alınamadı; migration fail-closed düşer."""


class RoleInUseError(RuntimeError):
    """Downgrade: eklenen roller kullanıcıya atanmış; geri alma fail-closed durur."""


def _sqlstate(exc: sa.exc.DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def _acquire_all_or_nothing(bind: sa.Connection, locks: tuple[tuple[str, str], ...]) -> None:
    """Tüm kilitleri NOWAIT + savepoint ile hep-ya-hiç alır; hiçbir kilit beklenerek tutulmaz."""
    deadline = time.monotonic() + LOCK_CEILING_S
    while True:
        try:
            with bind.begin_nested():
                for table, mode in locks:
                    bind.execute(sa.text(f"LOCK TABLE {table} IN {mode} MODE NOWAIT"))
            return
        except sa.exc.DBAPIError as exc:
            if _sqlstate(exc) != LOCK_NOT_AVAILABLE_SQLSTATE:
                raise
            if time.monotonic() >= deadline:
                raise LockCeilingExceededError(
                    f"{LOCK_CEILING_S}s icinde kilitler alinamadi: "
                    f"{[t for t, _ in locks]} (migration geri alindi)"
                ) from exc
        bind.execute(sa.text("SELECT pg_sleep(:s)"), {"s": LOCK_RETRY_INTERVAL_S})


LEVEL_LETTER = {
    "n": "none",
    "v": "view",
    "d": "draft",
    "r": "request",
    "a": "approve",
    "f": "full",
    "x": "admin",
}
LEVEL_RANK = {
    "none": 0,
    "view": 1,
    "draft": 2,
    "request": 3,
    "approve": 4,
    "full": 5,
    "admin": 6,
}


def _threshold_met(part: str, default_module: str, cells: Cells) -> bool | None:
    """Eşik parçası sağlanıyor mu? "-" (eşik yok) -> None."""
    if part == "-":
        return None
    for condition in part.split("&"):
        module, _, letter = condition.rpartition(":")
        level = cells.get(module or default_module, _N)[0]
        if LEVEL_RANK[level] < LEVEL_RANK[LEVEL_LETTER[letter]]:
            return False
    return True


def _page_cell(page_key: str, module_key: str | None, spec: str, cells: Cells) -> tuple[str, bool]:
    """Eşikli türetme: eski seviye o sayfadaki eylemin eşiğini GERÇEKTEN karşılıyorsa.

    Görmez/Görür görme eşiğiyle, Düzenler yazma eşiğiyle, Onaylar onay eşiğiyle belirlenir;
    onay yalnız Görmez DEĞİLKEN (DB CHECK).
    """
    if module_key is None:
        return MODULSUZ_VARSAYILAN[page_key], False
    view_part, write_part, approve_part = spec.split("|")
    if not _threshold_met(view_part, module_key, cells):
        return "none", False
    level = "edit" if _threshold_met(write_part, module_key, cells) else "view"
    return level, bool(_threshold_met(approve_part, module_key, cells))


def _legacy_page_cell(level: str, approve_action: bool) -> tuple[str, bool]:
    """ESKİ sade eşleme (yalnız KISILAN GENİŞLEME raporu için): approve/full/admin → Onaylar."""
    if level == "none":
        return "none", False
    if level == "view":
        return "view", False
    if level in ("draft", "request"):
        return "edit", False
    return "edit", approve_action


def _page_cells(cells: Cells) -> list[tuple[str, str, bool]]:
    rows: list[tuple[str, str, bool]] = []
    for page_key, module_key, _approve_action, spec in PAGES:
        level, approve = _page_cell(page_key, module_key, spec, cells)
        rows.append((page_key, level, approve))
    return rows


def _narrowed(cells: Cells, role_key: str) -> list[str]:
    """Eski sade eşlemeye göre KISILAN hücreler (genişleme yok ilkesi)."""
    lines: list[str] = []
    for page_key, module_key, approve_action, spec in PAGES:
        if module_key is None:
            continue
        old_level, old_approve = _legacy_page_cell(cells.get(module_key, _N)[0], approve_action)
        new_level, new_approve = _page_cell(page_key, module_key, spec, cells)
        if LEVEL_RANK_PAGE[new_level] < LEVEL_RANK_PAGE[old_level] or (
            old_approve and not new_approve
        ):
            lines.append(
                f"{role_key}:{page_key} "
                f"eski={old_level}/{old_approve} yeni={new_level}/{new_approve}"
            )
    return lines


LEVEL_RANK_PAGE = {"none": 0, "view": 1, "edit": 2}


def _hidden_categories(cells: Cells) -> list[str]:
    limited = any(level != "none" and scope == LIMITED_SCOPE for level, scope in cells.values())
    return [TUM_TUTARLAR] if limited else []


def _seed_cell(role_key: str, module_key: str) -> tuple[str, str]:
    """Seed hücresi; ROLE_ORDER'da olmayan (özel) rolde Görmez."""
    if role_key in ROLE_ORDER:
        return MATRIX[module_key][ROLE_ORDER.index(role_key)]
    return _N


def _live_cells(bind: sa.Connection) -> dict[tuple[str, str], tuple[str, str]]:
    rows = bind.execute(
        sa.text(
            "SELECT r.key, m.key, rp.access_level::text, rp.scope::text "
            "FROM role_permissions rp "
            "JOIN roles r ON r.id = rp.role_id "
            "JOIN modules m ON m.id = rp.module_id"
        )
    ).all()
    return {(role_key, module_key): (level, scope) for role_key, module_key, level, scope in rows}


def _warn_summary(
    deviations: list[str],
    seed_missing: int,
    custom_missing: int,
    custom_roles: list[str],
    colliding: list[str],
    narrowed: list[str],
) -> None:
    logger.warning(
        "IZN-B1: seed'den SAPAN canli hucre sayisi=%d; satiri olmadigi icin seed'e dusen "
        "seed-rol (rol,modul) cifti=%d; ozel rollerde satiri olmadigi icin Gormez'e dusen "
        "(rol,modul) cifti=%d; ozel rol anahtarlari=%s; mevcut cakisan yeni-rol anahtarlari=%s; "
        "KISILAN genisleme (eski sade esleme -> esikli esleme) hucre sayisi=%d",
        len(deviations),
        seed_missing,
        custom_missing,
        sorted(custom_roles),
        sorted(colliding),
        len(narrowed),
    )
    for label, lines in (("sapan hucre", deviations), ("kisilan genisleme", narrowed)):
        for line in lines[:MAX_DEVIATION_LINES]:
            logger.warning("IZN-B1: %s %s", label, line)
        if len(lines) > MAX_DEVIATION_LINES:
            logger.warning(
                "IZN-B1: ... ve %d %s daha (liste kisaltildi)",
                len(lines) - MAX_DEVIATION_LINES,
                label,
            )


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    _acquire_all_or_nothing(bind, UPGRADE_LOCKS)

    level_enum = postgresql.ENUM(*LEVELS, name=LEVEL_ENUM, create_type=False)
    category_enum = postgresql.ENUM(*CATEGORIES, name=CATEGORY_ENUM, create_type=False)
    level_enum.create(bind, checkfirst=False)
    category_enum.create(bind, checkfirst=False)

    op.create_table(
        PAGE_TABLE,
        sa.Column(
            "role_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_key", sa.String(64), nullable=False),
        sa.Column("level", level_enum, nullable=False),
        sa.Column("can_approve", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("role_id", "page_key"),
        sa.CheckConstraint(
            "NOT can_approve OR level <> 'none'",
            name="ck_role_page_permissions_approve_needs_level",
        ),
    )
    op.create_table(
        HIDDEN_TABLE,
        sa.Column(
            "role_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", category_enum, nullable=False),
        sa.PrimaryKeyConstraint("role_id", "category"),
    )

    role_ids: dict[str, uuid.UUID] = {
        key: role_id for role_id, key in bind.execute(sa.text("SELECT id, key FROM roles")).all()
    }
    live = _live_cells(bind)

    added: set[str] = set()
    colliding: list[str] = []
    for row in IZN_ROLES:
        if row["key"] in role_ids:
            colliding.append(row["key"])
            continue
        role_id = uuid.uuid4()
        bind.execute(
            sa.text(
                "INSERT INTO roles (id, key, name, emoji, description, is_system) "
                "VALUES (:id, :key, :name, :emoji, :description, false)"
            ),
            {"id": role_id, **row},
        )
        role_ids[row["key"]] = role_id
        added.add(row["key"])

    page_rows: list[dict] = []
    hidden_rows: list[dict] = []
    deviations: list[str] = []
    narrowed: list[str] = []
    seed_missing = 0
    custom_missing = 0
    custom_roles: list[str] = []
    module_keys = list(MATRIX)

    for role_key, role_id in role_ids.items():
        if role_key == SYSTEM_ADMIN_KEY:
            continue
        if role_key in added:
            index = IZN_ROLE_ORDER.index(role_key)
            cells: Cells = {m: IZN_MATRIX[m][index] for m in module_keys}
            hidden = list(IZN_HIDDEN_FIELDS[role_key])
        else:
            if role_key not in ROLE_ORDER:
                custom_roles.append(role_key)
            cells = {}
            for module_key in module_keys:
                seed = _seed_cell(role_key, module_key)
                live_cell = live.get((role_key, module_key))
                if live_cell is None:
                    if role_key in ROLE_ORDER:
                        seed_missing += 1
                    else:
                        custom_missing += 1
                    cells[module_key] = seed
                    continue
                cells[module_key] = live_cell
                if role_key in ROLE_ORDER and live_cell != seed:
                    deviations.append(
                        f"{role_key}:{module_key} canli={live_cell[0]}/{live_cell[1]} "
                        f"seed={seed[0]}/{seed[1]}"
                    )
            hidden = _hidden_categories(cells)
            narrowed.extend(_narrowed(cells, role_key))
        overrides = IZN_SAYFA_ISTISNALARI.get(role_key, {}) if role_key in added else {}
        for page_key, level, approve in _page_cells(cells):
            level, approve = overrides.get(page_key, (level, approve))
            page_rows.append(
                {"role_id": role_id, "page_key": page_key, "level": level, "can_approve": approve}
            )
        for category in hidden:
            hidden_rows.append({"role_id": role_id, "category": category})

    if page_rows:
        bind.execute(
            sa.text(
                f"INSERT INTO {PAGE_TABLE} (role_id, page_key, level, can_approve) "
                f"VALUES (:role_id, :page_key, CAST(:level AS {LEVEL_ENUM}), :can_approve)"
            ),
            page_rows,
        )
    if hidden_rows:
        bind.execute(
            sa.text(
                f"INSERT INTO {HIDDEN_TABLE} (role_id, category) "
                f"VALUES (:role_id, CAST(:category AS {CATEGORY_ENUM}))"
            ),
            hidden_rows,
        )

    _warn_summary(deviations, seed_missing, custom_missing, custom_roles, colliding, narrowed)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    _acquire_all_or_nothing(bind, DOWNGRADE_LOCKS)

    # Bu migration'ın eklediği roller = anahtarı yeni-rol listesinde VE hiç `role_permissions`
    # satırı olmayan roller (elle açılmış çakışan rolün 23 satırı vardır → korunur).
    ours = bind.execute(
        sa.text(
            "SELECT r.id, r.key FROM roles r WHERE r.key IN :keys "
            "AND NOT EXISTS (SELECT 1 FROM role_permissions rp WHERE rp.role_id = r.id)"
        ).bindparams(sa.bindparam("keys", expanding=True)),
        {"keys": IZN_ROLE_ORDER},
    ).all()
    our_ids = [role_id for role_id, _key in ours]

    if our_ids:
        in_use = bind.execute(
            sa.text(
                "SELECT r.key, count(u.id) FROM roles r JOIN users u ON u.role_id = r.id "
                "WHERE r.id IN :ids GROUP BY r.key ORDER BY r.key"
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": our_ids},
        ).all()
        if in_use:
            detail = ", ".join(f"{key}={count} kullanici" for key, count in in_use)
            raise RoleInUseError(
                "izn_b1 downgrade DURDU (fail-closed): eklenen yeni roller kullaniciya atanmis "
                f"({detail}). Once bu kullanicilari baska role tasiyin, sonra geri alin. "
                "Migration geri alindi; hicbir sey silinmedi."
            )
        bind.execute(
            sa.text("DELETE FROM roles WHERE id IN :ids").bindparams(
                sa.bindparam("ids", expanding=True)
            ),
            {"ids": our_ids},
        )

    kept = sorted(set(IZN_ROLE_ORDER) - {key for _id, key in ours})
    if kept:
        existing = bind.execute(
            sa.text("SELECT key FROM roles WHERE key IN :keys").bindparams(
                sa.bindparam("keys", expanding=True)
            ),
            {"keys": kept},
        ).all()
        if existing:
            logger.warning(
                "IZN-B1 downgrade: elle acilmis cakisan roller KORUNDU (silinmedi): %s",
                sorted(key for (key,) in existing),
            )

    op.drop_table(HIDDEN_TABLE)
    op.drop_table(PAGE_TABLE)
    postgresql.ENUM(name=CATEGORY_ENUM).drop(bind, checkfirst=False)
    postgresql.ENUM(name=LEVEL_ENUM).drop(bind, checkfirst=False)

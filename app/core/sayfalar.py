"""Sayfa anahtarı kataloğu — sayfa bazlı izin modelinin TEK KAYNAĞI (IZN-B1, IZN-PLAN §1.2).

100 sayfa: menü sayfaları + proje içindeki her sekme + Ayarlar sayfaları. Katalog
`IZN-ENVANTER.md` tablosundan BİR KEZ üretildi; sonrasında tek kaynak bu koddur
(envanter tarihsel belgedir). `envanter_no` o tablodaki satır numarasıdır ve
`tests/core/test_sayfa_katalogu_bekcisi.py` kataloğu envanterin kendi §4 tablosuna
karşı çakar.

Neden DB tablosu değil KOD SABİTİ: `modules` gibi bir tabloda her sayfa değişikliği
migration ve eşitlik bekçisi isterdi (`TKL-B7-TASARIM.md` §4.6.6). `page_key` DB'de
serbest metindir; servis yazarken anahtarı bu katalogla doğrular, bekçi testi DB seed'inin
anahtarlarını bu katalogla birebir eşitler.

`eski_modul`: sayfanın hücresini HANGİ eski modül hücresinden türeteceğini söyler (23 modül,
`roles/seed_data.MODULES`). Yedi sayfanın eski anahtarı yoktur (Raporlar, Şirket Varlıkları,
Geliştirme, Bildirimler, Görünüm, Entegrasyonlar, Yedekleme) → `None`; onların başlangıç
düzeyi `roles/seed_data.MODULSUZ_VARSAYILAN`dadır. Bu alan B6'da (eski tabloların sökümü)
anlamını yitirir; API'ye ÇIKMAZ.

`kaynak`: aynı veriyi besleyen sayfaları toplar (örn. `hakedis_isveren` = #50 + #65 + #72);
sonraki dilimler (B2/B5) uç kapısını sayfa değil kaynak üzerinden kurar.

`ikizler` (yalnız kök sayfada): aynı veriyi proje bağlamında gösteren proje-içi sayfaların
anahtarları. KARARLAR (K5): proje verili kök sayfa menüde, ana rolde YA DA herhangi bir proje
rolünde Görür ise görünür; frontend bunu bu listeyle hesaplar.
"""

# ruff: noqa: E501  (katalog bir veri tablosudur: satır başına bir sayfa)
import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from app.core.access import AccessLevel, Scope, satisfies


class PageLevel(str, enum.Enum):
    """Sayfa erişim düzeyi: Görmez / Görür / Düzenler. Silme düzey DEĞİLDİR (yalnız Sistem Yöneticisi);
    "Onaylar" ayrı bir bayraktır (`can_approve`)."""

    none = "none"
    view = "view"
    edit = "edit"


class HiddenCategory(str, enum.Enum):
    """Rol başına gizlenen hassas alan kategorisi. Satır var = o kategori bu rolde gizli."""

    sozlesme_fiyat = "sozlesme_fiyat"
    maliyet_kar = "maliyet_kar"
    maas_kisisel = "maas_kisisel"
    banka_kasa = "banka_kasa"
    satis_alici = "satis_alici"
    tum_tutarlar = "tum_tutarlar"


class PageGroup(str, enum.Enum):
    genel = "genel"
    saha = "saha"
    ik = "ik"
    planlama = "planlama"
    teklif = "teklif"
    stok = "stok"
    mali = "mali"
    proje_ici = "proje_ici"
    ayarlar = "ayarlar"


class PageKind(str, enum.Enum):
    sirket = "sirket"
    proje = "proje"


#: Menüdeki sıra ve görünen ad (onaylı Sayfa İzinleri mockup'ı: 9 menü grubu).
GRUP_ADLARI: Final[dict[PageGroup, str]] = {
    PageGroup.genel: "Genel",
    PageGroup.saha: "Saha",
    PageGroup.ik: "İK",
    PageGroup.planlama: "Planlama",
    PageGroup.teklif: "Teklif ve Sözleşmeler",
    PageGroup.stok: "Stok & Satınalma",
    PageGroup.mali: "Mali",
    PageGroup.proje_ici: "Proje içi sekmeler",
    PageGroup.ayarlar: "Ayarlar",
}

#: Eski modül anahtarlarının tamamı (23). `roles/seed_data.MODULES` ile birebir; bekçi çakar.
ESKI_MODULLER: Final[frozenset[str]] = frozenset(
    {
        "dashboard",
        "approvals",
        "projects",
        "sites",
        "site_diary",
        "timesheet",
        "personnel",
        "payroll",
        "inventory",
        "procurement",
        "progress_payments",
        "accounting",
        "invoicing",
        "treasury",
        "settings",
        "user_management",
        "boq",
        "contracts",
        "sales",
        "documents",
        "equipment",
        "ai",
        "earned_value",
    }
)


#: Bir eşik = (modül, en az düzey) çiftlerinin VE'si (hepsi sağlanmalı). Bugünkü GERÇEK kapı:
#: uç kapısı ile ön yüz kapısının sıkısı (kullanıcı ikisini de geçemiyorsa eylemi yapamaz).
Esik = tuple[tuple[str, AccessLevel], ...]

_SEVIYE_HARFI: Final[dict[str, AccessLevel]] = {
    "n": AccessLevel.none,
    "v": AccessLevel.view,
    "d": AccessLevel.draft,
    "r": AccessLevel.request,
    "a": AccessLevel.approve,
    "f": AccessLevel.full,
    "x": AccessLevel.admin,
}

# ESİK TABLOSU (envanter no -> "görme|yazma|onay"). Her parça: "-" (yok) ya da "&" ile birleşen
# koşullar; koşul ya "f" (sayfanın eski modülünde ≥ düzey) ya da "modul:f" (başka modül).
# Düzey harfleri: n none · v view · d draft · r request · a approve · f full · x admin.
# Kaynak: IZN-ENVANTER "İzin modülü (FE okuması · BE kapısı)" + "Düzenler kapsamı" + "Onay eylemi"
# sütunları; ön yüz kapıları `frontend/src` içinde grep ile doğrulandı. Düzenler eşiği = sayfadaki
# yazma eyleminin gerçek (BE) eşiği; yazma eylemi hiç yoksa "-" (sayfa Görür'ün üstüne çıkmaz).
# Onaylar eşiği = sayfadaki onay eylemini GERÇEKTEN açan düzey. Gerekçeler ve CEO kararları
# (genişleme yok): yalnız-admin eylemler (proje oluştur, dönüştür, yeniden aç, dönem aç, onayı geri al,
# bordro vergi dilimi, onay eşiği) `x`; yazması `full` olan uçlar `f`. "n" = bugün kapısız (herkes görür).
# fmt: off
_ESIKLER: Final[dict[int, str]] = {
    1: "v|-|-", 2: "n|-|f", 3: "v|-|-", 5: "v|x|-", 6: "v|-|-",
    7: "v|f|-", 8: "v|f|-", 9: "v|f|-", 10: "v|f|-", 11: "v|f|f", 12: "v|f|x",
    13: "v|f|-", 14: "v|f|f", 15: "v|f|-",
    16: "v|-|-", 17: "v|d|a", 18: "v|-|a", 19: "v|-|-", 20: "v|f|-", 21: "v|f|-",
    22: "v|f|projects:x&f", 23: "v|f|-", 24: "v|f|-", 25: "v|f|-", 26: "v|f|-", 27: "v|f|-",
    28: "v|f|-", 29: "v|f|-",
    30: "v|f|-", 31: "v|r|r", 32: "v|f|-", 33: "v|f|-", 34: "v|f|f",
    35: "v|f|f", 36: "d|f|-", 37: "d|f|-", 38: "f|f|-", 39: "d|f|-", 40: "d|f|-",
    41: "v|f|f", 42: "v|f|-", 43: "v|-|-", 44: "v|-|-", 45: "v|-|-", 46: "v|f|x",
    47: "v|f|f", 48: "v|f|-", 49: "v|f|f", 50: "v|d|x", 51: "v|d|x",
    52: "v|-|-", 53: "v|-|-", 54: "v|-|-", 55: "v|f|f", 56: "v|-|-", 57: "v|f|f", 59: "v|f|-",
    61: "v&sites:v|v&sites:f|-", 62: "v|-|-", 63: "v|-|-", 64: "v|f|-", 65: "v|d|x", 66: "v|d|x",
    67: "v|f|-", 68: "v|f|-", 69: "v|f|-", 70: "v|f|-", 71: "v|f|-", 72: "v|d|x", 73: "v|f|x",
    74: "v|f|-", 75: "v|f|-", 76: "v|-|-", 77: "v|f|-", 78: "v|d|a", 79: "v|-|-", 80: "v|-|a",
    81: "v|-|-", 82: "v|f|-", 83: "v|-|-", 84: "v|-|-", 85: "v|-|-", 86: "v|-|-", 87: "v|-|-",
    88: "v|f|x",
    89: "n|f|-", 92: "v|d|-", 93: "v|f|-", 94: "v|x|-", 95: "v|x|-", 96: "n|x|-", 97: "v|x|-",
    100: "v|-|-",
}
# fmt: on
#: Eşiği olmayan (modülsüz) yedi sayfa: Raporlar, Şirket Varlıkları, Geliştirme, Bildirimler,
#: Görünüm, Entegrasyonlar, Yedekleme. Başlangıç düzeyleri `MODULSUZ_VARSAYILAN`.
ESIK_SPEC_MODULSUZ: Final[str] = "-|-|-"

#: Eski modül anahtarı OLMAYAN yedi sayfanın başlangıç düzeyi. İlke: bugün kapısız olan sayfa
#: herkese açıktır → Görür (menü davranışı değişmez); Geliştirme yalnız `system_admin` rolüne
#: açık olan geçici sayfadır (FE `canSeeGelistirme`) → Görmez (Sistem Yöneticisi çözücüden geçer).
MODULSUZ_VARSAYILAN: Final[dict[str, PageLevel]] = {
    "genel.raporlar": PageLevel.view,
    "mali.sirket_varliklari": PageLevel.view,
    "ayarlar.bildirimler": PageLevel.view,
    "ayarlar.gorunum": PageLevel.view,
    "ayarlar.entegrasyonlar": PageLevel.view,
    "ayarlar.yedekleme": PageLevel.view,
    "ayarlar.gelistirme": PageLevel.none,
}


def esik_coz(metin: str, varsayilan_modul: str) -> Esik | None:
    """ "-" -> None; "f" -> ((varsayilan_modul, full),); "projects:x&f" -> iki koşul (VE)."""
    if metin == "-":
        return None
    kosullar = []
    for parca in metin.split("&"):
        modul, _, harf = parca.rpartition(":")
        kosullar.append((modul or varsayilan_modul, _SEVIYE_HARFI[harf]))
    return tuple(kosullar)


@dataclass(frozen=True)
class Sayfa:
    envanter_no: int
    key: str
    ad: str
    grup: PageGroup
    alt_grup: str | None
    rota: str
    tur: PageKind
    onay_var: bool
    kaynak: str
    eski_modul: str | None
    ikizler: tuple[str, ...] = ()
    gorme: Esik | None = None
    yazma: Esik | None = None
    onay: Esik | None = None


_G = PageGroup
_P = "Proje"
_S = "Şantiye"
_B = "Bölüm"

# fmt: off
# (no, anahtar, ad, grup, alt_grup, rota, kaynak, eski_modul, onay_var, ikiz_nolari)
# Sıra: menü gruplarının mockup'taki sırası; grup içinde envanter numarası sırası.
_HAM: Final[tuple[tuple, ...]] = (
    # --- Genel (6) ---
    (1, "genel.gosterge_paneli", "Gösterge Paneli", _G.genel, None, "/", "dashboard", "dashboard", False, ()),
    (2, "genel.onay_kutusu", "Onay Kutusu", _G.genel, None, "/onay-kutusu", "onay_kutusu", "approvals", True, ()),
    (3, "genel.fiil_ai", "FİİL AI", _G.genel, None, "/asistan", "ai", "ai", False, ()),
    (4, "genel.raporlar", "Raporlar", _G.genel, None, "/raporlar", "raporlar", None, False, ()),
    (5, "genel.projeler", "Projeler", _G.genel, None, "/projeler", "proje", "projects", False, ()),
    (6, "genel.proje_takvimi", "Proje Takvimi", _G.genel, None, "/projeler/takvim", "proje", "projects", False, ()),
    # --- Saha (6) ---
    (7, "saha.puantaj", "Puantaj", _G.saha, None, "/puantaj", "puantaj", "timesheet", False, (70, 84)),
    (8, "saha.makine_ekipman", "Makine & Ekipman › Ekipman Listesi", _G.saha, None, "/makine", "makine", "equipment", False, ()),
    (9, "saha.makine_calisma", "Makine & Ekipman › Çalışma Kaydı", _G.saha, None, "/makine/calisma", "makine", "equipment", False, ()),
    (10, "saha.makine_yakit", "Makine & Ekipman › Yakıt Takibi", _G.saha, None, "/makine/yakit", "makine", "equipment", False, ()),
    (11, "saha.makine_kira", "Makine & Ekipman › Kira Hakedişi", _G.saha, None, "/makine/kira", "makine_kira", "equipment", True, ()),
    (12, "saha.gunluk_kayit", "Günlük Kayıt", _G.saha, None, "/gunluk-kayit", "gunluk_kayit", "site_diary", True, (73, 87, 88)),
    # --- İK (3) ---
    (13, "ik.personel", "Personel › Personel Listesi", _G.ik, None, "/personel", "personel", "personnel", False, ()),
    (14, "ik.izin_yonetimi", "Personel › İzin Yönetimi", _G.ik, None, "/personel/izinler", "personel_izin", "personnel", True, ()),
    (15, "ik.belge_sertifika", "Personel › Belge & Sertifika", _G.ik, None, "/personel/belgeler", "personel_belge", "personnel", False, ()),
    # --- Planlama (6) ---
    (16, "planlama.panel", "Planlama Paneli", _G.planlama, None, "/planlama/panel", "planlama_panel", "earned_value", False, (79,)),
    (17, "planlama.adam_saat_butcesi", "Adam-Saat Bütçesi", _G.planlama, None, "/planlama/adam-saat-butcesi", "adam_saat_butcesi", "earned_value", True, (78,)),
    (18, "planlama.gunluk_rapor", "Günlük İlerleme Raporu", _G.planlama, None, "/planlama/gunluk-rapor", "gunluk_ilerleme_raporu", "earned_value", True, (80,)),
    (19, "planlama.haftalik_qurr", "Haftalık QURR", _G.planlama, None, "/planlama/haftalik-qurr", "haftalik_qurr", "earned_value", False, (81,)),
    (20, "planlama.birim_oran_katalogu", "Birim Oran Kataloğu", _G.planlama, None, "/planlama/birim-oran-katalogu", "birim_oran_katalogu", "earned_value", False, ()),
    (21, "planlama.disiplin_yonetimi", "Disiplin Yönetimi", _G.planlama, None, "/planlama/disiplin-yonetimi", "disiplin", "earned_value", False, ()),
    # --- Teklif ve Sözleşmeler (8) ---
    (22, "teklif.teklif_hazirlama", "Teklif Hazırlama", _G.teklif, None, "/teklif-hazirlama", "teklif", "contracts", True, ()),
    (23, "teklif.sablonlar", "Teklif Şablonları", _G.teklif, None, "/teklif-hazirlama/sablonlar", "teklif_sablon", "contracts", False, ()),
    (24, "teklif.sozlesmeler", "Sözleşmeler (İşveren · Taşeron)", _G.teklif, None, "/sozlesmeler", "sozlesme", "contracts", False, ()),
    (25, "teklif.taseron_firmalar", "Taşeron Firmalar", _G.teklif, None, "/sozlesmeler/taseronlar", "taseron_firma", "contracts", False, ()),
    (26, "teklif.isveren_sozlesme", "İşveren Sözleşme Detayı", _G.teklif, None, "/sozlesmeler/isveren/[projectId]", "sozlesme_isveren", "contracts", False, ()),
    (27, "teklif.poz_dagilimi", "İşveren Sözleşmesi › Poz Dağılımı", _G.teklif, None, "/sozlesmeler/isveren/[projectId]/poz-dagilimi", "sozlesme_poz_dagilimi", "contracts", False, ()),
    (28, "teklif.taseron_sozlesme", "Taşeron Sözleşme Detayı", _G.teklif, None, "/sozlesmeler/taseron/[contractId]", "sozlesme_taseron", "contracts", False, ()),
    (29, "teklif.is_kalemi_katalogu", "İş Kalemi Kataloğu", _G.teklif, None, "/planlama/is-kalemi-katalogu", "is_kalemi_katalogu", "contracts", False, ()),
    # --- Stok & Satınalma (5) ---
    (30, "stok.stok_depo", "Stok & Depo", _G.stok, None, "/stok", "stok", "inventory", False, (71, 85)),
    (31, "stok.satinalma_talepleri", "Satınalma & Teklif › Satın Alma Talepleri", _G.stok, None, "/satinalma", "satinalma_talep", "procurement", True, ()),
    (32, "stok.siparisler", "Satınalma & Teklif › Siparişler", _G.stok, None, "/satinalma/siparisler", "satinalma_siparis", "procurement", False, ()),
    (33, "stok.tedarikciler", "Satınalma & Teklif › Tedarikçiler", _G.stok, None, "/satinalma/tedarikciler", "tedarikci", "procurement", False, ()),
    (34, "stok.teklif_karsilastirma", "Satınalma & Teklif › Teklif Karşılaştırma", _G.stok, None, "/satinalma/talepler/[id]/teklifler", "satinalma_teklif", "procurement", True, ()),
    # --- Mali (25) ---
    (35, "mali.satis", "Satış Yönetimi", _G.mali, None, "/satis", "satis", "sales", True, ()),
    (36, "mali.satis_blok", "Satış › Blok Ekle", _G.mali, None, "/satis/blok-ekle", "unite", "projects", False, ()),
    (37, "mali.satis_unite", "Satış › Ünite Ekle", _G.mali, None, "/satis/unite-ekle", "unite", "projects", False, ()),
    (38, "mali.satis_toplu_uretim", "Satış › Toplu Üretim", _G.mali, None, "/satis/toplu-uretim", "unite", "projects", False, ()),
    (39, "mali.satis_excel", "Satış › Excel İçe Aktar", _G.mali, None, "/satis/excel-ice-aktar", "unite", "projects", False, ()),
    (40, "mali.satis_paylasim", "Satış › Paylaşım Girişi", _G.mali, None, "/satis/paylasim-girisi", "arsa_paylasim", "projects", False, ()),
    (41, "mali.yevmiye", "Muhasebe › Yevmiye", _G.mali, None, "/muhasebe", "yevmiye", "accounting", True, ()),
    (42, "mali.hesap_plani", "Muhasebe › Hesap Planı", _G.mali, None, "/muhasebe/hesap-plani", "hesap_plani", "accounting", False, ()),
    (43, "mali.mizan", "Muhasebe › Mizan", _G.mali, None, "/muhasebe/mizan", "mizan", "accounting", False, ()),
    (44, "mali.kdv_beyani", "Muhasebe › KDV Beyanı", _G.mali, None, "/muhasebe/kdv-beyani", "kdv", "accounting", False, ()),
    (45, "mali.banka_mutabakati", "Muhasebe › Banka Mutabakatı", _G.mali, None, "/muhasebe/banka-mutabakati", "banka_mutabakat", "accounting", False, ()),
    (46, "mali.donem_kapanisi", "Muhasebe › Dönem Kapanışı", _G.mali, None, "/muhasebe/donem-kapanisi", "donem_kapanisi", "accounting", True, ()),
    (47, "mali.fatura", "Fatura Yönetimi (Giden · Gelen)", _G.mali, None, "/faturalar", "fatura", "invoicing", True, ()),
    (48, "mali.hazine", "Hazine", _G.mali, None, "/hazine", "hazine", "treasury", False, ()),
    (49, "mali.cek_odeme", "Çek & Ödeme", _G.mali, None, "/hazine/cek-senet", "cek_odeme", "treasury", True, ()),
    (50, "mali.hakedis_isveren", "Hakedişler › İşveren", _G.mali, None, "/hakedisler", "hakedis_isveren", "progress_payments", True, (65, 72, 86)),
    (51, "mali.hakedis_taseron", "Hakedişler › Taşeron", _G.mali, None, "/hakedisler/taseron", "hakedis_taseron", "progress_payments", True, (66, 72, 86)),
    (52, "mali.gelir_tablosu", "Mali Tablolar › Gelir Tablosu", _G.mali, None, "/mali-tablolar", "mali_tablolar", "accounting", False, ()),
    (53, "mali.bilanco", "Mali Tablolar › Bilanço", _G.mali, None, "/mali-tablolar/bilanco", "mali_tablolar", "accounting", False, ()),
    (54, "mali.nakit_akisi", "Mali Tablolar › Nakit Akışı", _G.mali, None, "/mali-tablolar/nakit-akisi", "mali_tablolar", "accounting", False, ()),
    (55, "mali.bordro", "Bordro › Aylık Bordro", _G.mali, None, "/bordro", "bordro", "payroll", True, ()),
    (56, "mali.bordro_gecmis", "Bordro › Bordro Geçmişi", _G.mali, None, "/bordro/gecmis", "bordro_gecmis", "payroll", False, ()),
    (57, "mali.sgk_bildirimi", "Bordro › SGK Bildirimi", _G.mali, None, "/bordro/sgk", "sgk", "payroll", True, ()),
    (58, "mali.sirket_varliklari", "Şirket Varlıkları", _G.mali, None, "/sirket-varliklari", "sirket_varliklari", None, False, ()),
    (59, "mali.belge_arsivi", "Belge Arşivi", _G.mali, None, "/belgeler", "belge", "documents", False, (67, 74)),
    # --- Proje içi sekmeler (28) ---
    (61, "proje.santiyeler", "Proje › Şantiyeler", _G.proje_ici, _P, "/projeler/[projectId]", "proje", "projects", False, ()),
    (62, "proje.ozet", "Proje › Proje Özeti", _G.proje_ici, _P, "/projeler/[projectId]/ozet", "proje", "projects", False, ()),
    (63, "proje.paylasim_tablosu", "Proje › Paylaşım Tablosu", _G.proje_ici, _P, "/projeler/[projectId]/paylasim", "arsa_paylasim", "projects", False, ()),
    (64, "proje.is_kalemleri", "Proje › İş Kalemleri", _G.proje_ici, _P, "/sozlesmeler/isveren/[projectId]?tab=items", "sozlesme_isveren", "contracts", False, ()),
    (65, "proje.isveren_hakedis", "Proje › İşveren Hakediş", _G.proje_ici, _P, "/hakedisler?project_id=", "hakedis_isveren", "progress_payments", True, ()),
    (66, "proje.taseron_hakedis", "Proje › Taşeron Hakediş", _G.proje_ici, _P, "/hakedisler/taseron?project_id=", "hakedis_taseron", "progress_payments", True, ()),
    (67, "proje.belgeler", "Proje › Belgeler", _G.proje_ici, _P, "/belgeler?proje=", "belge", "documents", False, ()),
    (68, "santiye.bolumler", "Şantiye › Bölümler", _G.proje_ici, _S, "/projeler/[p]/santiyeler/[s]", "bolum", "sites", False, ()),
    (69, "santiye.is_kalemleri", "Şantiye › İş Kalemleri", _G.proje_ici, _S, "/projeler/[p]/santiyeler/[s]/is-kalemleri", "is_kalemi", "boq", False, ()),
    (70, "santiye.puantaj", "Şantiye › Puantaj", _G.proje_ici, _S, "/projeler/[p]/santiyeler/[s]/puantaj", "puantaj", "timesheet", False, ()),
    (71, "santiye.stok", "Şantiye › Stok", _G.proje_ici, _S, "/projeler/[p]/santiyeler/[s]/stok", "stok", "inventory", False, ()),
    (72, "santiye.hakedisler", "Şantiye › Hakedişler", _G.proje_ici, _S, "/projeler/[p]/santiyeler/[s]/hakedisler", "hakedis_isveren", "progress_payments", True, ()),
    (73, "santiye.gunluk_kayit", "Şantiye › Günlük Kayıt", _G.proje_ici, _S, "/projeler/[p]/santiyeler/[s]/gunluk-kayit", "gunluk_kayit", "site_diary", True, ()),
    (74, "santiye.belgeler", "Şantiye › Belgeler", _G.proje_ici, _S, "/projeler/[p]/santiyeler/[s]/belgeler", "belge", "documents", False, ()),
    (75, "santiye.bolum_dagilimi", "Şantiye › İş Kalemleri › Bölüm Dağılımı", _G.proje_ici, _S, "/projeler/[p]/santiyeler/[s]/is-kalemleri/bolum-dagilimi", "is_kalemi_dagilimi", "boq", False, ()),
    (76, "santiye.gunluk_ozet", "Şantiye › Günlük Kayıt › Aylık Özet", _G.proje_ici, _S, "/projeler/[p]/santiyeler/[s]/gunluk-kayit/ozet", "gunluk_kayit", "site_diary", False, ()),
    (77, "santiye.gunluk_planlama", "Şantiye › Günlük Kayıt › Planlama", _G.proje_ici, _S, "/projeler/[p]/santiyeler/[s]/gunluk-kayit/planlama", "gunluk_planlama", "site_diary", False, ()),
    (78, "santiye.adam_saat_butcesi", "Şantiye › Adam-Saat Bütçesi", _G.proje_ici, _S, "/projeler/[p]/santiyeler/[s]/adam-saat-butcesi", "adam_saat_butcesi", "earned_value", True, ()),
    (79, "santiye.planlama_paneli", "Şantiye › Planlama Paneli", _G.proje_ici, _S, "/projeler/[p]/santiyeler/[s]/planlama-paneli", "planlama_panel", "earned_value", False, ()),
    (80, "santiye.gunluk_ilerleme_raporu", "Şantiye › Günlük İlerleme Raporu", _G.proje_ici, _S, "/projeler/[p]/santiyeler/[s]/gunluk-ilerleme-raporu", "gunluk_ilerleme_raporu", "earned_value", True, ()),
    (81, "santiye.haftalik_qurr", "Şantiye › Haftalık QURR", _G.proje_ici, _S, "/projeler/[p]/santiyeler/[s]/haftalik-qurr", "haftalik_qurr", "earned_value", False, ()),
    (82, "bolum.detay", "Bölüm Detayı", _G.proje_ici, _B, "/projeler/[p]/santiyeler/[s]/bolumler/[sectionId]", "bolum", "sites", False, ()),
    (83, "bolum.is_kalemleri", "Bölüm › İş Kalemleri", _G.proje_ici, _B, "/projeler/[p]/…/bolumler/[sectionId]?sekme=is-kalemleri", "is_kalemi", "boq", False, ()),
    (84, "bolum.puantaj", "Bölüm › İşçiler & Puantaj", _G.proje_ici, _B, "/projeler/[p]/…/bolumler/[sectionId]?sekme=puantaj", "puantaj", "timesheet", False, ()),
    (85, "bolum.malzeme", "Bölüm › Malzeme", _G.proje_ici, _B, "/projeler/[p]/…/bolumler/[sectionId]?sekme=malzeme", "stok", "inventory", False, ()),
    (86, "bolum.hakedis", "Bölüm › Hakediş", _G.proje_ici, _B, "/projeler/[p]/…/bolumler/[sectionId]?sekme=hakedis", "hakedis_taseron", "progress_payments", False, ()),
    (87, "bolum.gunluk_kayit", "Bölüm › Günlük Kayıt", _G.proje_ici, _B, "/projeler/[p]/…/bolumler/[sectionId]?sekme=gunluk-kayit", "gunluk_kayit", "site_diary", False, ()),
    (88, "bolum.gunluk_kayit_detay", "Bölüm › Günlük Kayıt Detayı", _G.proje_ici, _B, "/projeler/[p]/santiyeler/[s]/bolumler/[sectionId]/gunluk-kayit/[entryId]", "gunluk_kayit", "site_diary", True, ()),
    # --- Ayarlar (13) ---
    (89, "ayarlar.sirket_bilgileri", "Şirket Bilgileri", _G.ayarlar, None, "/ayarlar/sirket-bilgileri", "sirket_bilgileri", "settings", False, ()),
    (90, "ayarlar.bildirimler", "Bildirimler", _G.ayarlar, None, "/ayarlar/bildirimler", "bildirimler", None, False, ()),
    (91, "ayarlar.gorunum", "Görünüm", _G.ayarlar, None, "/ayarlar/gorunum", "gorunum", None, False, ()),
    (92, "ayarlar.planlama", "Planlama Ayarları", _G.ayarlar, None, "/ayarlar/planlama", "planlama_ayarlari", "earned_value", False, ()),
    (93, "ayarlar.kullanicilar", "Kullanıcılar", _G.ayarlar, None, "/ayarlar/kullanicilar", "kullanici", "user_management", False, ()),
    (94, "ayarlar.rol_yonetimi", "Rol Yönetimi", _G.ayarlar, None, "/ayarlar/roller", "rol", "user_management", False, ()),
    (95, "ayarlar.sayfa_izinleri", "Sayfa İzinleri", _G.ayarlar, None, "/ayarlar/izin-matrisi", "sayfa_izinleri", "user_management", False, ()),
    (96, "ayarlar.onay_rolleri", "Onay Rolleri ve Eşik", _G.ayarlar, None, "/ayarlar/onay-rolleri", "onay_rolleri", "approvals", False, ()),
    (97, "ayarlar.bordro_oranlari", "Bordro Oranları", _G.ayarlar, None, "/ayarlar/bordro-oranlari", "bordro_oranlari", "payroll", False, ()),
    (98, "ayarlar.entegrasyonlar", "Entegrasyonlar", _G.ayarlar, None, "/ayarlar/entegrasyonlar", "entegrasyon", None, False, ()),
    (99, "ayarlar.yedekleme", "Yedekleme", _G.ayarlar, None, "/ayarlar/yedekleme", "yedekleme", None, False, ()),
    (100, "ayarlar.denetim_gunlugu", "Denetim Günlüğü", _G.ayarlar, None, "/ayarlar/denetim-gunlugu", "denetim_gunlugu", "settings", False, ()),
    (60, "ayarlar.gelistirme", "Geliştirme (geçici)", _G.ayarlar, None, "/gelistirme", "gelistirme", None, False, ()),
)
# fmt: on


def esik_spec(no: int, eski_modul: str | None) -> str:
    """Sayfanın "görme|yazma|onay" eşik metni (migration kopyasıyla eşitlik bekçisi bunu okur)."""
    return ESIK_SPEC_MODULSUZ if eski_modul is None else _ESIKLER[no]


def _esikler(no: int, eski_modul: str | None) -> dict[str, Esik | None]:
    if eski_modul is None:
        return {"gorme": None, "yazma": None, "onay": None}
    gorme, yazma, onay = _ESIKLER[no].split("|")
    return {
        "gorme": esik_coz(gorme, eski_modul),
        "yazma": esik_coz(yazma, eski_modul),
        "onay": esik_coz(onay, eski_modul),
    }


def _kur() -> tuple[Sayfa, ...]:
    anahtar_by_no = {satir[0]: satir[1] for satir in _HAM}
    sayfalar = []
    for no, key, ad, grup, alt_grup, rota, kaynak, eski_modul, onay_var, ikiz_nolari in _HAM:
        sayfalar.append(
            Sayfa(
                envanter_no=no,
                key=key,
                ad=ad,
                grup=grup,
                alt_grup=alt_grup,
                rota=rota,
                tur=PageKind.proje if grup is PageGroup.proje_ici else PageKind.sirket,
                onay_var=onay_var,
                kaynak=kaynak,
                eski_modul=eski_modul,
                ikizler=tuple(anahtar_by_no[n] for n in ikiz_nolari),
                **_esikler(no, eski_modul),
            )
        )
    return tuple(sayfalar)


#: 100 sayfa, menü sırasıyla. Kaynak sırası: grup sırası, grup içinde envanter numarası.
SAYFALAR: Final[tuple[Sayfa, ...]] = _kur()

SAYFA_BY_KEY: Final[dict[str, Sayfa]] = {s.key: s for s in SAYFALAR}
SAYFA_ANAHTARLARI: Final[tuple[str, ...]] = tuple(s.key for s in SAYFALAR)
ONAY_VAR_ANAHTARLARI: Final[frozenset[str]] = frozenset(s.key for s in SAYFALAR if s.onay_var)

#: OpenAPI'de `page_key` bir enum olur → frontend'e TS birleşik tip (union) olarak iner ve
#: `nav-config.ts`teki yazım hatası tip denetiminde yakalanır. Üyeler katalogdan DİNAMİK
#: üretilir; ikinci bir elle liste TUTULMAZ.
PageKey = enum.Enum(
    "PageKey",
    {key.replace(".", "_"): key for key in SAYFA_ANAHTARLARI},
    type=str,
)


def sistem_yoneticisi_sayfalari() -> dict[str, tuple[PageLevel, bool]]:
    """Sistem Yöneticisi her yerde "Düzenler" + onay eylemi olan sayfada "Onaylar" taşır.

    Hücre olarak DB'de TUTULMAZ (IZN-PLAN §1.1: çözücü `role.key == SYSTEM_ADMIN_KEY` için
    her yere evet der); `/auth/me` frontend'in tek tip okuması için bu haritayı üretir.
    """
    return {s.key: (PageLevel.edit, s.onay_var) for s in SAYFALAR}


# ---------------------------------------------------------------------------
# Eski modül hücresi -> sayfa hücresi TÜRETMESİ (IZN-PLAN §1.3 + CEO "genişleme yok" kararı).
# TEK KAYNAK burasıdır: migration `izn_b1` aynı kuralı ELLE kopyalar (`app` import etmez) ve
# `tests/modules/test_izn_b1_seed_migration_esitligi.py` ikisini çakar.
# ---------------------------------------------------------------------------

#: Bir rolün modül hücreleri: modül anahtarı -> (düzey, kapsam). Eksik modül = (none, all).
ModulHucreleri = Mapping[str, tuple[AccessLevel, Scope]]


def esik_karsilaniyor(esik: Esik, hucreler: ModulHucreleri) -> bool:
    return all(
        satisfies(hucreler.get(modul, (AccessLevel.none, Scope.all))[0], seviye)
        for modul, seviye in esik
    )


def sayfa_hucresi(sayfa: Sayfa, hucreler: ModulHucreleri) -> tuple[PageLevel, bool]:
    """Sayfanın hücresi: eski seviye o sayfadaki eylemin eşiğini GERÇEKTEN karşılıyorsa.

    Görmez/Görür görme eşiğiyle, Düzenler yazma eşiğiyle, Onaylar onay eşiğiyle belirlenir;
    hiçbir sayfa rolün bugün fiilen yapamadığı bir eylemi (Düzenler/Onaylar) kazanmaz.
    Onay, görünmeyen sayfada anlamsızdır (DB CHECK) → onay yalnız Görmez DEĞİLKEN.
    """
    if sayfa.gorme is None:
        return MODULSUZ_VARSAYILAN[sayfa.key], False
    if not esik_karsilaniyor(sayfa.gorme, hucreler):
        return PageLevel.none, False
    level = (
        PageLevel.edit
        if sayfa.yazma is not None and esik_karsilaniyor(sayfa.yazma, hucreler)
        else PageLevel.view
    )
    approve = sayfa.onay is not None and esik_karsilaniyor(sayfa.onay, hucreler)
    return level, approve


def sayfa_matrisi(hucreler: ModulHucreleri) -> dict[str, tuple[PageLevel, bool]]:
    """Bir rolün modül hücrelerinden 100 sayfalık hücre kümesi."""
    return {sayfa.key: sayfa_hucresi(sayfa, hucreler) for sayfa in SAYFALAR}


#: `limited` kapsamı rol bayrağı `tum_tutarlar`a çevrilir (§1.3); `finance` karşılıksızdır.
def gizli_alanlar(hucreler: ModulHucreleri) -> frozenset[HiddenCategory]:
    """Rolün herhangi bir (erişimi olan) hücresinde `limited` kapsam varsa `tum_tutarlar`."""
    limited = any(
        level is not AccessLevel.none and scope is Scope.limited
        for level, scope in hucreler.values()
    )
    return frozenset({HiddenCategory.tum_tutarlar}) if limited else frozenset()

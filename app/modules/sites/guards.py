"""Santiye yazma yolunun ORTAK korkuluklari ve Turkce hata metinleri (spec §5.1, §7.2).

`units/guards.py` deseninin birebiri: alan HATA SINIFLARI `app/core/errors.py`'de,
METINLER modul icinde sabit olarak durur. Kurallar burada TEK kopya durur; POST
(T6) ve PATCH'in yayina gecis dali (T7) KOPYALAMAZ, CAGIRIR — iki kopya zamanla
ayrisir ve ayrisan taraf sessiz bir veri hatasi olur.

## Tek cumlelik kural

**Tutarlilik kurallari HER ZAMAN kosar, zorunluluk kurallari YALNIZ
taslak-disinda.**

Yarim kalmis bir taslak asla GECERSIZ veri saklamaz (bitisi baslangictan onceki
bir santiye hicbir modda anlamli degildir), yalniz EKSIK veri saklar (sefi henuz
secilmemis olabilir). Ayrimi tersine cevirmek iki yonde de zarar verir:

* zorunlulugu taslakta da kosarsak "Taslak Kaydet" dugmesi islevsiz kalir;
* tutarliligi taslakta gevsetirsek DB'ye anlamsiz tarih araliklari sizar ve
  yayina gecis aninda kullaniciya ilk kez, baglamsiz bir hata olarak doner.

## Kosulmadigi yer

`PATCH /sites/{id}` bu dogrulamayi TAM olarak kosmaz (§0.3/3, §5.3): kosarsa
canlidaki sefsiz/il bilgisi olmayan eski santiyeler duzenlenemez hâle gelir ve
kullanici yalnizca adi degistirmek isterken "Şantiye şefi seçiniz." duvarina
carpar. PATCH'te yalniz tutarlilik kurallari, `is_draft: true -> false`
gecisinde ise BIRLESIK kayit uzerinde tum kurallar kosar.
"""

from typing import Protocol

from app.core.errors import SiteValidationError

# --- Spec §7.2 tablosundan BIREBIR alinmistir — yeniden yazilmaz. ---

# 404 govdeleri AYIRT EDICI OLMAMALIDIR: gorunmeyen bir projedeki GERCEK santiye
# ile var olmayan santiye AYNI mesaji doner, aksi hâlde elinde UUID olan kullanici
# kaydin hâlâ var oldugunu ve baska bir projeye ait oldugunu ayirt edebilirdi.
PROJECT_MISSING = "Proje bulunamadı"
SITE_MISSING = "Şantiye bulunamadı"
SECTION_MISSING = "Bölüm bulunamadı"

# Sef/ISG icin secilen kullanici yok veya pasif. 404 DEGIL 422: istenen kaynak
# santiyedir, kullanici degil — kullanici burada bir ALAN DEGERIDIR.
USER_NOT_FOUND = "Seçilen kullanıcı bulunamadı"

# 409 — kod cakismalari. Yeni istisna sinifi ACILMAZ: mevcut `DuplicateError`.
DUPLICATE_SITE_CODE = "Bu şantiye kodu bu projede zaten kullanılıyor"
DUPLICATE_SECTION_CODE = "Bu bölüm kodu bu şantiyede zaten kullanılıyor"

# 422 — zorunluluk kurallari (YALNIZ taslak-disinda; mockup satir 69/79/85/94/95).
SITE_MANAGER_REQUIRED = "Şantiye şefi seçiniz."
CITY_REQUIRED = "İl / ilçe zorunludur."
CONSTRUCTION_AREA_REQUIRED = "İnşaat alanı zorunludur."
DATES_REQUIRED = "Başlangıç ve planlanan bitiş tarihi zorunludur."

# 422 — bolum zorunluluk kurallari (YALNIZ taslak-disinda). KAYNAK:
# `Form - Bolum Ekle.dc.html` icinde `<span class="req">*</span>` TASIYAN alanlar
# — goz karari alan eklenmez, `*` tasimayan alan zorunlu YAPILMAZ:
#
#   * 67  Bölüm Adı            -> `name`        (Pydantic `min_length=1` ile zorunlu)
#   * 69  Bölüm Sırası         -> `sort_order`  (varsayilani 0; ASLA bos olamaz)
#   * 70  Bölüm Tipi           -> SECTION_TYPE_REQUIRED
#   * 83  Bölüm Sorumlusu      -> SECTION_MANAGER_REQUIRED
#   * 107 Başlangıç Tarihi     -> SECTION_DATES_REQUIRED
#   * 108 Planlanan Bitiş      -> SECTION_DATES_REQUIRED
#   * 110 Bölüm Bedeli (₺)     -> BLF-B1: KALKTI (bedel yalniz turev, formda salt-okunur)
#   * 66  Şantiye              -> YOL PARAMETRESI, govdede aranmaz.
#
# `*` TASIMAYAN ve bu yuzden zorunlu OLMAYANLAR: 68 Bölüm Kodu (ipucu: "Boş
# bırakılırsa otomatik"), 71 Durum, 74 Açıklama, 84 Yardımcı Sorumlu,
# 85 Planlanan İşçi Sayısı. 109 Süre (Gün) `readonly` turevdir, hic saklanmaz.
SECTION_TYPE_REQUIRED = "Bölüm tipi seçiniz."
SECTION_MANAGER_REQUIRED = "Bölüm sorumlusu seçiniz."
SECTION_DATES_REQUIRED = "Başlangıç ve planlanan bitiş tarihi zorunludur."
# BLF-B1: bolum tipi artik sirket geneli tablodan secilir; var olmayan id 422.
SECTION_TYPE_MISSING = "Bölüm tipi bulunamadı"
# 409 — yeni bolum tipi adi mevcut bir tiple (normalize esitligiyle) cakisiyor.
SECTION_TYPE_TAKEN_AS = "Bu bölüm tipi zaten var: {name}"

# 422 — P11 bolum bagimliligi (spec §3, S3). UCU DE 422'DIR, 404 DEGIL: istenen
# kaynak GUNCELLENEN bolumdur, oncul burada bir ALAN DEGERIDIR.
#
# `DEPENDS_NOT_IN_SITE` tek metindir ve UC durumu birden karsilar: var olmayan id,
# baska santiyedeki bolum, gorunmeyen projedeki bolum. Ayirt edilebilir metinler
# yazmak, elinde UUID olan kullaniciya "bu kayit baska bir yerde VAR" bilgisini
# sizdirirdi (`SITE_MISSING`/`SECTION_MISSING` ayirt edilemezlik kuralinin ayni
# uygulamasi). Guncellenen bolumun santiyesi zaten aktore GORUNURDUR, dolayisiyla
# bu metin hicbir gorunurluk disi bilgi tasimaz.
DEPENDS_NOT_IN_SITE = "Öncül bölüm aynı şantiyede bulunamadı"
DEPENDS_SELF = "Bölüm kendisine bağımlı olamaz"
# Zincir YURUYEREK bulunur (spec §5): 2'li, 3'lu ve daha uzun halkalar ayni
# metni alir — kullanici icin fark yoktur, tek eylem bagi degistirmektir.
DEPENDS_CYCLE = "Bölüm bağımlılıkları döngü oluşturamaz"

# 422 — P11 kilometre tasi birlestirmesi (P9 `SHAREHOLDER_*` metinlerinin
# birebiri). Bilinmeyen id ile BASKA bolume ait id AYNI metni alir: satir bu
# bolumde YOKTUR, nerede oldugu bu ucun konusu degildir.
MILESTONE_UNKNOWN = (
    "Gönderilen kilometre taşı bu bölümde bulunamadı. "
    "Sayfayı yenileyip güncel listeyle tekrar deneyin."
)
# P9 T5 dersi: ayni id iki kez gonderilirse birlestirme SESSIZCE tek satira
# cokerdi (200 doner, ikinci girdi kazanir, ilki gerekcesiz kaybolur).
MILESTONE_DUPLICATE_IN_PAYLOAD = "Aynı kilometre taşı listede birden çok kez var"

# 422 — tutarlilik kurallari (HER ZAMAN).
END_BEFORE_START = "Planlanan bitiş tarihi başlangıçtan önce olamaz."
SAFETY_OFFICER_CONFLICT = "İSG uzmanı ya sistem kullanıcısı ya dış kaynak (OSGB) olabilir."

# `{n}` 1-TABANLIDIR: kullanici 2. satiri goruyor, 1 indeksini degil.
SECTION_END_BEFORE_START = "{n}. bölüm: bitiş tarihi başlangıçtan önce olamaz."
SECTION_NAME_REQUIRED = "{n}. bölüm: bölüm adı zorunludur."

# GPS BICIM HATASI SABITI YOKTUR (§3.5 revize karari): sunucu GPS metnini
# dogrulamaz, dolayisiyla boyle bir hata uretmez.


class _SiteLike(Protocol):
    """`validate_site`'in okudugu alanlar.

    Ozellikle `SiteCreate`'e BAGLANMAZ: T7'de PATCH'in yayina gecis dali ayni
    kurallari BIRLESIK bir kayit (mevcut satir + patch) uzerinde kosturacak.
    Somut tipe baglamak orada ikinci bir kopya kural yazmayi zorunlu kilardi.
    """

    name: str
    site_manager_user_id: object
    site_manager_name: object
    safety_officer_user_id: object
    safety_officer_is_outsourced: bool
    city: object
    construction_area_m2: object
    start_date: object
    end_date: object


def _validate_sections(data: _SiteLike) -> None:
    """Bolum satirlari — TUTARLILIK, dolayisiyla taslakta da kosar.

    ILK hatada durur: cok satirli hata listesi URETILMEZ (§8.2). Form 2-5
    satirliktir; Excel ice aktarmanin satir bazli raporu (`UnitImportRowReport`,
    yuzlerce satir icin tasarlandi) burada kullaniciya yardim etmez, yalnizca
    govdeyi sisirir.
    """
    for index, section in enumerate(getattr(data, "sections", None) or [], start=1):
        if not (section.name or "").strip():
            raise SiteValidationError(SECTION_NAME_REQUIRED.format(n=index))
        if (
            section.start_date is not None
            and section.end_date is not None
            and section.end_date < section.start_date
        ):
            raise SiteValidationError(SECTION_END_BEFORE_START.format(n=index))


def validate_site(data: _SiteLike, *, is_draft: bool) -> None:
    """Spec §5.1 tablosunun 11 satirini uygular.

    1/4 numarali satirlar (ad bos olamaz, tutarlar >= 0) Pydantic'te durur ve
    burada TEKRARLANMAZ — iki kopya kural zamanla ayrisir.
    5 numarali satir (GPS) icin kural YOKTUR.
    """
    # --- Tutarlilik: HER ZAMAN ---
    if (
        data.start_date is not None
        and data.end_date is not None
        and data.end_date < data.start_date
    ):
        raise SiteValidationError(END_BEFORE_START)

    if data.safety_officer_is_outsourced and data.safety_officer_user_id is not None:
        raise SiteValidationError(SAFETY_OFFICER_CONFLICT)

    _validate_sections(data)

    # --- Zorunluluk: YALNIZ taslak-disinda ---
    if is_draft:
        return

    # Sef ya sistem kullanicisi ya da serbest metin olarak verilir (P1.1a mirasi
    # `site_manager_name` hâlâ gecerli bir giristir); ikisinden biri yeter.
    if data.site_manager_user_id is None and not (data.site_manager_name or "").strip():
        raise SiteValidationError(SITE_MANAGER_REQUIRED)

    if not (data.city or "").strip():
        raise SiteValidationError(CITY_REQUIRED)

    # ISG uzmani BILINCLI OLARAK burada yok: hicbir kosulda zorunlu degildir (§13/6).

    if data.construction_area_m2 is None:
        raise SiteValidationError(CONSTRUCTION_AREA_REQUIRED)

    if data.start_date is None or data.end_date is None:
        raise SiteValidationError(DATES_REQUIRED)


class _SectionLike(Protocol):
    """`validate_section`'in okudugu alanlar (`_SiteLike` deseninin birebiri).

    `SectionCreate`'e BAGLANMAZ: PATCH'in ileride bir yayina gecis dali acmasi
    hâlinde ayni kurallar BIRLESIK bir kayit (mevcut satir + patch) uzerinde
    kosturulabilsin. Somut tipe baglamak orada ikinci bir kopya kural yazmayi
    zorunlu kilardi.
    """

    section_type_id: object
    manager_user_id: object
    manager_name: object
    start_date: object
    end_date: object


def validate_section(data: _SectionLike, *, is_draft: bool) -> None:
    """`Form - Bolum Ekle` kurallari. Modul docstring'indeki TEK CUMLELIK KURAL
    bolume aynen gecer: **tutarlilik HER ZAMAN, zorunluluk YALNIZ taslak-disinda.**

    Zorunlu alanlarin kaynagi mockup'taki `*` isaretidir (bkz. yukaridaki metin
    sabitleri). `name` ve `sort_order` burada TEKRARLANMAZ: ikisini de Pydantic
    zorunlu kilar (`min_length=1` / varsayilan 0) ve iki kopya kural zamanla
    ayrisir.
    """
    # --- Tutarlilik: HER ZAMAN ---
    if (
        data.start_date is not None
        and data.end_date is not None
        and data.end_date < data.start_date
    ):
        raise SiteValidationError(END_BEFORE_START)

    # --- Zorunluluk: YALNIZ taslak-disinda ---
    if is_draft:
        return

    if data.section_type_id is None:
        raise SiteValidationError(SECTION_TYPE_REQUIRED)

    # Sorumlu ya sistem kullanicisi ya serbest metin olarak verilir — santiye
    # sefindeki (`SITE_MANAGER_REQUIRED`) ayrimin birebiri: ikisinden biri yeter.
    if data.manager_user_id is None and not (data.manager_name or "").strip():
        raise SiteValidationError(SECTION_MANAGER_REQUIRED)

    if data.start_date is None or data.end_date is None:
        raise SiteValidationError(SECTION_DATES_REQUIRED)

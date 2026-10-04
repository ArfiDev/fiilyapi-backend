"""Taşeron sözleşmesi yazma yolunun ORTAK korkulukları ve Türkçe hata metinleri
(spec §4, §7).

`sites/guards.py` deseninin birebiri: alan HATA SINIFLARI `app/core/errors.py`'de,
METİNLER modül içinde sabit olarak durur. Kurallar burada TEK kopya durur; POST
ve PATCH'in taslak→yayın dalı KOPYALAMAZ, ÇAĞIRIR — iki kopya kural zamanla
ayrışır ve ayrışan taraf sessiz bir veri hatası olur.

## Tek cümlelik kural

**Tutarlılık kuralları HER ZAMAN koşar, zorunluluk kuralları YALNIZ
taslak-dışında.**

Yarım kalmış bir taslak asla GEÇERSİZ veri saklamaz (bitişi başlangıçtan önceki
bir sözleşme hiçbir modda anlamlı değildir), yalnız EKSİK veri saklar (taşeron
firma henüz seçilmemiş olabilir). Ayrımı tersine çevirmek iki yönde de zarar
verir:

* zorunluluğu taslakta da koşarsak "Taslak Kaydet" düğmesi işlevsiz kalır;
* tutarlılığı taslakta gevşetirsek DB'ye anlamsız tarih aralıkları sızar ve
  yayına geçiş anında kullanıcıya ilk kez, bağlamsız bir hata olarak döner.

## Koşulmadığı yer

`PATCH /subcontractor-contracts/{id}` genel dalında bu doğrulama TAM olarak
koşmaz (spec §4, `sites/guards.py` §0.3/3 dersinin aynısı): koşarsa canlıdaki
eksik kayıtlı taslaklar düzenlenemez hâle gelir. PATCH'in genel dalında yalnız
tutarlılık kuralları koşar; `is_draft: true -> false` geçişinde ise BİRLEŞİK
kayıt üzerinde tüm kurallar koşar. Bu koşullama servis katmanının (C8/C9) işidir
— `validate_subcontract` yalnız `is_draft` bayrağına göre kararı verir.
"""

from datetime import date
from typing import Protocol

from app.core.errors import SiteValidationError

# --- Spec §4 tablosundan BİREBİR alınmıştır — yeniden yazılmaz. ---

# 404 gövdeleri AYIRT EDİCİ OLMAMALIDIR: görünmeyen bir projedeki GERÇEK kayıt ile
# var olmayan kayıt AYNI mesajı döner, aksi hâlde elinde UUID olan kullanıcı
# kaydın hâlâ var olduğunu ve başka bir projeye ait olduğunu ayırt edebilirdi
# (`sites/guards.py` desenin aynısı).
CONTRACT_MISSING = "Sözleşme bulunamadı"
SUBCONTRACTOR_MISSING = "Taşeron bulunamadı"
ITEM_MISSING = "Kalem bulunamadı"
GROUP_MISSING = "Grup bulunamadı"

# 422 — zorunluluk kuralları (YALNIZ taslak-dışında; spec §4 tablosu, mockup
# FORM 55/75/82/90/91/93-94/138).
PROJECT_REQUIRED = "Proje seçiniz."
SUBCONTRACTOR_REQUIRED = "Taşeron firma seçiniz."
CATEGORY_REQUIRED = "İş kategorisi zorunludur."
CONTRACT_NO_REQUIRED = "Sözleşme no zorunludur."
SIGNATURE_DATE_REQUIRED = "İmza tarihi zorunludur."
DATES_REQUIRED = "İşe başlama ve bitiş tarihi zorunludur."
ITEM_PRICES_REQUIRED = "Tüm pozlarda taşeron birim fiyatı zorunludur."

# ŞANTİYE (`site_id`) BİLİNÇLİ OLARAK burada YOK — K4 onaylı sapma: `FORM` 59'daki
# `*` işaretine rağmen şantiye hiçbir koşulda zorunlu değildir (kullanıcı kararı).
# Bunu sonradan "eksik" diye eklemeyin.

# 422 — tutarlılık kuralları (HER ZAMAN; spec §4 tablosu).
END_BEFORE_START = "Bitiş tarihi işe başlama tarihinden önce olamaz."

# `SITE_PROJECT_MISMATCH` ve `DISTRIBUTION_EXCEEDS` şantiye-proje eşleşmesi ve
# poz dağılımı toplamı DB erişimi gerektirir; `validate_subcontract` içinde
# KULLANILMAZ, ilgili serviste (C8/C10) kontrol edilir. Metin sabiti yine de tek
# kopya burada durur.
SITE_PROJECT_MISMATCH = "Seçilen şantiye bu projeye ait değil"
DISTRIBUTION_EXCEEDS = "Şantiye kotaları toplamı sözleşme miktarını aşamaz."

# 422 — spec §3.3 kısmi benzersiz indeksi
# (`uq_boq_items_contract_item_site`): her (kalem, şantiye) çifti için TEK kota
# hücresi vardır. Gövde ekranın tamamı olduğu için aynı hücrenin iki kez
# gönderilmesi kullanıcı hatasıdır; `IntegrityError` → 409 "Veri bütünlüğü
# hatası" gövdesine DÜŞÜLMEZ, önce burada anlaşılır 422 ile karşılanır.
DUPLICATE_ALLOCATION = "Aynı kalem ve şantiye için tek kota gönderilebilir."

# 409 — BOQ satırı oluşturulurken `uq_boq_items_site_code` çakışması. Şantiye
# aynı poz numarasını kendi başına girmiş olabilir (spec §3.3: `contract_item_id
# IS NULL` satırlar meşrudur); bu durumda sözleşmeden kopyalanacak satır
# yazılamaz. Spec bu durumu adlandırmıyor — task C8 kararı, C13 gözden geçirir.
BOQ_CODE_TAKEN_IN_SITE = "Bu poz numarası hedef şantiyede zaten kullanılıyor"


def boq_code_taken_in_site(site_name: str, code: str) -> str:
    """409 — kalem `code` tazelemesinin çarptığı ŞANTİYE ve KOD (TB4/S8).

    Sabit metnin tek başına yetmediği tek yer burasıdır: kalem PATCH'i BOQ'yu
    göstermeyen bir ekrandan gelir ve kalem birden çok şantiyeye dağıtılmış
    olabilir — kullanıcı hangi şantiyedeki hangi numaranın engellediğini
    gövdeden okuyamazsa çakışmayı arayarak bulmak zorunda kalır. Üslup
    `subcontractor_progress_payments.guards.quantity_exceeds_quota` ile aynı:
    sabit metin + `" · "` ile ayrılmış bağlam. Dağıtım yazma yolu bağlamı
    ekranda zaten gösterdiği için SABİT metni kullanmaya devam eder.
    """
    return f"{BOQ_CODE_TAKEN_IN_SITE}: {site_name} · {code}"


# 409 — silme korkulukları (spec §7). Yeni istisna sınıfı AÇILMAZ: mevcut
# `RelatedRecordsExistError`. Metinlerde ADET VERİLMEZ, eyleme dönüktür
# (`sites/guards.py`'deki `BLOCK_HAS_UNITS` dersi).
SUBCONTRACTOR_HAS_CONTRACTS = "Bu taşeronun sözleşmesi var, önce sözleşmeleri silin"
GROUP_HAS_ITEMS = "Bu grupta poz var, önce pozları silin"


def subcontractor_has_diary_rows(entry_count: int, row_count: int, first_date: date) -> str:
    """PLN-B2.11 — taşeronun günlük FİRMA işçi satırı varken taşeron silinemez (409).

    "Adet verilmez" kuralının BİLİNÇLİ istisnası (CEO kararı, PLN-B2.10/11 ikizi):
    firma satırı o günün firma adam-saatinin kanıtıdır; kullanıcı hangi
    günlükleri düzelteceğini bilmeden engeli aşamaz.
    """
    return (
        f"Taşeronun {entry_count} günlükte {row_count} işçi satırı var "
        f"(ilk: {first_date:%d.%m.%Y}); silinemez"
    )


# 422 — `load-from-employer` (spec §6.5): işveren sözleşmesi ya da kalemi yoksa.
NO_EMPLOYER_ITEMS = "Bu projenin işveren sözleşmesinde poz yok"

# --- İşveren sözleşmesi poz grup/kalem yazma yolu (task C6, spec §6.2/§3.3) ---

# Grup dolaylı kimlikle (POST .../contract/items gövdesindeki group_id) başka
# projenin grubuna işaret edebilir — `BoqGroupSiteMismatchError` deseninin
# aynısı, yeni istisna sınıfı AÇILMAZ, `SiteValidationError` (422) kullanılır.
GROUP_PROJECT_MISMATCH = "Poz grubu bu sözleşmeye ait değil"

# 409 — `(project_id, code)` çakışması (`BoqItem` deseninin aynısı).
DUPLICATE_ITEM_CODE = "Bu poz numarası bu sözleşmede zaten kullanılıyor"

# 422 — spec §3.3: kalan hesabı `remaining = quantity - Σ(bağlı boq_items.quantity)`,
# hiçbir yerde negatif Kalan gösterilmez. Task brief'in kararı: `DISTRIBUTION_EXCEEDS`
# metni burada UYGUN DEĞİL (o metin dağıtım toplamının sözleşme miktarını AŞMASI
# içindir, spec §6.3) — bu ise TERSİ yön: sözleşme miktarı zaten dağıtılmış toplamın
# ALTINA indirilemez. Task C6'nın kendi metni, C13'te merkezileştirilmez.
ITEM_QUANTITY_BELOW_DISTRIBUTED = "Miktar, dağıtılmış toplamın altına indirilemez."


class _ItemLike(Protocol):
    """`validate_subcontract`'ın kalemlerde okuduğu tek alan.

    Pydantic'te zaten duran kurallar (`quantity > 0`, yüzde aralığı, ad boş
    olamaz, `unit_price >= 0`) burada TEKRARLANMAZ — iki kopya kural zamanla
    ayrışır. Yalnız yayında zorunlu olan `unit_price` varlığı burada denetlenir.
    """

    unit_price: object


class _SubcontractLike(Protocol):
    """`validate_subcontract`'ın okuduğu alanlar.

    Somut bir şemaya (`SubcontractorContractCreate` vb.) BAĞLANMAZ: PATCH'in
    taslak→yayın dalı aynı kuralları BİRLEŞİK bir kayıt (mevcut satır + patch)
    üzerinde koşturacak. Somut tipe bağlamak orada ikinci bir kopya kural
    yazmayı zorunlu kılardı (`sites/guards.py` desenin aynısı).
    """

    project_id: object
    subcontractor_id: object
    work_category: object
    contract_no: object
    signature_date: object
    start_date: object
    end_date: object
    items: object


def validate_subcontract(data: _SubcontractLike, *, is_draft: bool) -> None:
    """Spec §4 tablosunun zorunluluk + tutarlılık kurallarını uygular.

    İLK hatada durur: çok satırlı hata listesi ÜRETİLMEZ — form tek seferde tek
    alan gösterir, kullanıcı düzeltir, tekrar dener (`sites/guards.py` deseni).
    """
    # --- Tutarlılık: HER ZAMAN ---
    if (
        data.start_date is not None
        and data.end_date is not None
        and data.end_date < data.start_date
    ):
        raise SiteValidationError(END_BEFORE_START)

    # --- Zorunluluk: YALNIZ taslak-dışında ---
    if is_draft:
        return

    if data.project_id is None:
        raise SiteValidationError(PROJECT_REQUIRED)

    if data.subcontractor_id is None:
        raise SiteValidationError(SUBCONTRACTOR_REQUIRED)

    if not (data.work_category or "").strip():
        raise SiteValidationError(CATEGORY_REQUIRED)

    if not (data.contract_no or "").strip():
        raise SiteValidationError(CONTRACT_NO_REQUIRED)

    if data.signature_date is None:
        raise SiteValidationError(SIGNATURE_DATE_REQUIRED)

    if data.start_date is None or data.end_date is None:
        raise SiteValidationError(DATES_REQUIRED)

    # ŞANTİYE BİLİNÇLİ OLARAK burada YOK — K4 onaylı sapma (FORM 59'daki *).

    if data.items and any(k.unit_price is None for k in data.items):
        raise SiteValidationError(ITEM_PRICES_REQUIRED)

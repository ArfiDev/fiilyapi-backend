"""Personel korkulukları ve Türkçe hata metinleri (puantaj spec §2, §3).

`customers/guards.py` deseninin aynısı: hata SINIFLARI `app/core/errors.py`'de,
METİNLER modül içinde tek kopya sabit olarak durur ve POST ile PATCH aynı
fonksiyonu ÇAĞIRIR, kuralı kopyalamaz.

## Tek cümlelik kural

**Kaynak `subcontractor` DEĞİLSE taşeron bağı BOŞ olmalıdır.**

Aynı kural DB'de de vardır (`ck_personnel_subcontractor_only_for_subcontractor_source`),
ama CHECK ihlali `IntegrityError` -> 409 "Veri bütünlüğü hatası" verirdi; kullanıcı
hangi alanı düzelteceğini öğrenemezdi. Bu yüzden servis DB'ye DÜŞMEDEN 422 atar,
DB CHECK'i yarış/doğrudan-SQL emniyet ağı olarak KALIR.

**Ters yön ZORLANMAZ** (spec §2): kaynağı `subcontractor` olan bir kayıt taşeron
seçilmeden de oluşturulabilir — taslak esnekliği. Sonraki okuyucu bunu "eksik"
sanıp zorunluluk EKLEMESİN.
"""

import uuid

from app.core.errors import PersonnelValidationError
from app.modules.site_diary.models import WorkerSource

# 404 gövdesi (`customers/guards.py` deseni).
PERSONNEL_MISSING = "Personel bulunamadı"

# --- İK-1 T3: belge alt-kaynağı korkulukları ------------------------------

# 404 — personelin belge kaydı yok/görünmez (`PERSONNEL_MISSING` deseni).
PERSONNEL_DOCUMENT_MISSING = "Personel belgesi bulunamadı"

# 404 — gövdedeki `type_id` katalogda hiç yok (spec §4b: gövde içi varlık ref 404).
DOCUMENT_TYPE_MISSING = "Belge tipi bulunamadı"

# 422 — tip katalogda VAR ama pasif (`is_active=false`). 404 DEĞİL: kayıt vardır,
# engelleyen şey düzeltilebilir bir DURUMDUR (başka aktif tip seçilebilir).
DOCUMENT_TYPE_INACTIVE = "Seçilen belge tipi pasif, kullanılamaz"

# 422 — `type_id` XOR `free_label`: katalogdan bir tip SEÇİLİR YA DA serbest
# etiket girilir, ikisi birden ya da hiçbiri OLAMAZ. Aynı kural pydantic'te (giriş
# doğrulaması) ve DB CHECK'inde (`ck_personnel_document_type_xor_label`) de vardır;
# bu servis korkuluğu ikisinin arasındaki üçüncü kattır ve kullanıcıya Türkçe
# 422 verir (çıplak DB CHECK 409 "veri bütünlüğü" verirdi).
TYPE_XOR_LABEL = "Belge için ya katalog tipi ya da serbest etiket girilmeli (yalnız biri)"

# 422 — kaynak/taşeron uyuşmazlığı.
SUBCONTRACTOR_NOT_ALLOWED = (
    "Taşeron firması yalnız kaynağı taşeron olan personelde doldurulabilir; "
    "kaynağı değiştirin ya da firma bağını temizleyin."
)

# 422 — TCKN checksum (İK-1 spec §5 K1). NULL/boş tc_no doğrulama ATLANIR (taslak
# serbest); DOLU ise 11 hane + rakam + ilk hane sıfır DEĞİL + T.C. standart algoritma.
INVALID_TCKN = "Geçersiz TC kimlik numarası (11 haneli olmalı ve doğrulamayı geçmeli)."

# 409 — aynı TCKN iki DOLU kayıt (`customers` benzersizlik deseni). DB
# `uq_personnel_tc_no` YARIŞ DURUMU emniyet ağı olarak KALIR.
DUPLICATE_TCKN = "Bu TC kimlik numarası zaten kayıtlı"

# 404 — gövde içi varlık ref'i yok (spec §4b kanonu: gövde içi varlık ref 404).
PROJECT_NOT_FOUND = "Atanan proje bulunamadı"
SECTION_NOT_FOUND = "Atanan bölüm bulunamadı"

# 422 — kural ihlali (spec §4b kanonu; `documents.SITE_NOT_IN_PROJECT` deseni).
SECTION_NOT_IN_PROJECT = "Seçilen bölüm bu projeye ait değil"
SECTION_REQUIRES_PROJECT = "Bölüm atamak için önce proje seçilmelidir"

# 422 — yayın (is_draft=false) için PE ✱ zorunlu alan kümesi (mockup 51-118).
# Cinsiyet/Medeni/E-posta/Bölüm/Ödeme/IBAN/SGK ✱ DEĞİL — bilinçle listede yok.
PUBLISH_REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("full_name", "Ad Soyad"),
    ("tc_no", "TC Kimlik No"),
    ("birth_date", "Doğum Tarihi"),
    ("phone", "Cep Telefonu"),
    ("address", "Adres"),
    ("emergency_contact_name", "Acil Durum Kişisi"),
    ("emergency_contact_phone", "Acil Durum Telefonu"),
    ("trade", "Meslek / Görev"),
    ("hire_date", "İşe Giriş Tarihi"),
    ("assigned_project_id", "Atandığı Proje"),
    ("wage_type", "Ücret Tipi"),
    ("wage_amount", "Ücret Tutarı"),
)
PUBLISH_MISSING = "Personeli yayınlamak için şu alanlar zorunludur: {}"

# --- İK-2 T2: izin talebi korkulukları (spec §3, §5 K2/K3) -----------------

# 404 — talep yok/görünmez (`PERSONNEL_MISSING` deseni; var olmayanla ayırt edilemez).
LEAVE_REQUEST_MISSING = "İzin talebi bulunamadı"

# 404 — gövdedeki `leave_type_id` katalogda hiç yok (spec §4b: gövde içi varlık ref).
LEAVE_TYPE_MISSING = "İzin tipi bulunamadı"

# 422 — tip katalogda VAR ama pasif. 404 DEĞİL (`DOCUMENT_TYPE_INACTIVE` gerekçesi):
# kayıt vardır, engelleyen şey düzeltilebilir bir DURUMDUR (başka tip seçilebilir).
LEAVE_TYPE_INACTIVE = "Seçilen izin tipi pasif, kullanılamaz"

# 422 — bitiş başlangıçtan önce. DB CHECK (`ck_leave_requests_date_order`) VARDIR
# ama ihlali 409 "veri bütünlüğü" verirdi; servis DB'ye düşmeden Türkçe 422 atar.
LEAVE_DATE_ORDER = "İzin bitiş tarihi başlangıç tarihinden önce olamaz"

# 422 — PATCH gövdesinde NOT NULL kolonlara AÇIK `null`. Şema alanları
# `... | None = None` olmak ZORUNDA (kısmi gönderim), bu yüzden "gönderilmedi" ile
# "null gönderildi" ancak `model_fields_set` ile ayrılır. Ayrılmazsa null,
# `setattr` ile NOT NULL kolona yazılır ve `calculate_leave_days` DB'ye
# VARILMADAN `TypeError` atar (yakalayıcısı yok → 500) ya da `leave_type_id`de
# gövdesi belirsiz 409'a düşer. `note`/`document_id` bu listede DEĞİL: onlar
# nullable kolonlardır ve null ile TEMİZLENMELERİ meşrudur.
LEAVE_NULLABLE_OLMAYAN_ALANLAR: tuple[str, ...] = ("leave_type_id", "start_date", "end_date")
LEAVE_FIELD_NOT_NULL = "İzin tipi, başlangıç ve bitiş tarihi boş bırakılamaz"

# 409 — karara bağlanmış talep DÜZENLENEMEZ/SİLİNEMEZ (spec §3: "yalnız pending").
# `ConflictError`: engel kaydın MEVCUT DURUMUDUR, gövdedeki bir alan değil; 422
# verilseydi ekran "hangi alanı düzelteyim" diye arardı. Onaylı izin bakiyeyi
# etkilemiştir — geriye dönük düzenlemesi bakiyeyi sessizce kaydırırdı.
LEAVE_NOT_PENDING = "Yalnız bekleyen (onaylanmamış) izin talebi düzenlenebilir ya da silinebilir"

# --- İK-2 T3: onay/red + bakiye korkulukları (spec §2, §5 K3/K4/K5) --------

# 409 — karara BAĞLANMIŞ talep yeniden karara bağlanamaz. `LEAVE_NOT_PENDING`ten
# AYRI bir metindir çünkü o cümle "düzenlenebilir ya da silinebilir" der; onay
# ucundan dönseydi kullanıcı yanlış eylemi aradığını sanırdı. Onay TEK adımdır
# (spec §5 K4) — ikinci bir aşama ya da "yeniden değerlendirme" YOKTUR.
LEAVE_DECISION_NOT_PENDING = "Yalnız bekleyen izin talebi onaylanabilir ya da reddedilebilir"

# 409 — spec §5 K3: aynı personelin ÇAKIŞAN ONAYLI izni. Kural YALNIZ `approve`ta
# işler (POST/PATCH'te değil, spec §3): İK çakışan bir talebi KAYDEDEBİLMELİ ve
# çakışmayı onay anında değerlendirebilmelidir. RED bu kapıdan ETKİLENMEZ.
LEAVE_OVERLAPPING_APPROVED = "Bu personelin seçilen tarih aralığında onaylanmış başka bir izni var"

# 409 — spec §5 K5: talebin günü kalan yıllık haktan büyük (İZ 98-99 onay engeli).
# YALNIZ `deducts_from_annual` tiplerde denetlenir (hastalık/mazeret düşmez).
LEAVE_ENTITLEMENT_EXCEEDED = "Talep edilen gün sayısı personelin kalan yıllık izin hakkını aşıyor"

# 🔴 409 — NULL-EŞİK KANONU (fail-closed): kalan hak HESAPLANAMIYOR (kıdem 1 yılı
# doldurmadı ya da `hire_date` girilmemiş). Bilinmeyen KÜÇÜK değil BÜYÜK sayılır:
# "hesaplanamadı"yı 0 kullanılmış güne çevirmek TAM HAKKI açardı. Red serbesttir.
LEAVE_ENTITLEMENT_UNKNOWN = (
    "Personelin yıllık izin hakkı hesaplanamıyor (kıdem 1 yılı doldurmamış ya da "
    "işe giriş tarihi girilmemiş); talep onaylanamaz"
)

# 422 — red gerekçesi ZORUNLU (TH emsali) ve YALNIZ BOŞLUKTAN oluşamaz.
LEAVE_REJECT_REASON_REQUIRED = "İzin reddi için gerekçe zorunludur"

# --- İK-2.1: self-servis izin talebi korkulukları -------------------------

# 404 — aktörün `user_id`si HİÇBİR personel kaydına bağlı değil (K3). `user_id`
# yalnız OPSİYONEL bir köprüdür (`models.py:81`): saha personelinin çoğunun
# login'i YOKTUR, dolayısıyla bu hâl bir HATA DEĞİL normal bir durumdur ve
# 500'e ASLA düşmez. Kod 404'tür (repo emsali: istenen kayıt yoksa 404) —
# "yetkin yok" (403) YANLIŞ olurdu: kullanıcının yetkisi vardır, ORTADA KAYIT
# YOKTUR ve çözümü İK'nın kartına login'ini bağlamasıdır.
SELF_PERSONNEL_MISSING = (
    "Kullanıcınıza bağlı bir personel kaydı bulunamadı; İK ile iletişime geçin."
)

# 409 — İKİ (ya da daha çok) personel kaydı AYNI `user_id`ye bağlı (K4).
# 🔴 ÖLÇÜLDÜ: `personnel.user_id` üzerinde UNIQUE kısıt YOKTUR — yalnız tekil
# OLMAYAN `ix_personnel_user_id` indeksi vardır (`c6d7e8f9a0b1_puantaj_cekirdegi.py:90`).
# Belirsizlikte FAIL-CLOSED davranılır: sunucu hangi kaydın kastedildiğini
# TAHMİN ETMEZ, hiçbir şey YAZMAZ. (Kısıt eklemek migration ister; bu dilimde
# AÇILMADI.)
SELF_PERSONNEL_AMBIGUOUS = (
    "Kullanıcınıza birden fazla personel kaydı bağlı; İK kayıtları düzeltmeden "
    "self-servis izin talebi açılamaz."
)

# --- OK-1A T5: kendi izin talebini ONAYLAMA yasağı ------------------------

# 403 — kimse KENDİ izin talebini onaylayamaz (kullanıcı kararı 2026-08-21).
# TEK İSTİSNA `admin` seviyesidir ve o da denetim günlüğüne
# `messages.APPROVAL_ON_BEHALF_MARK` ("vekâleten") işaretiyle geçer.
#
# 🔴 403'tür, 409 DEĞİL: kayıt DOĞRU durumdadır (`pending`) ve BAŞKA bir yetkili
# aynı anda onaylayabilir — engelleyen şey AKTÖRÜN KİM OLDUĞUDUR. 422 de değil:
# gövdede düzeltilecek bir alan yoktur (gövde zaten alan kabul etmez).
#
# 🔴 RED bu kapıdan ETKİLENMEZ: kullanıcı kararı yalnız "onaylayamaz" der ve
# kendi talebini reddetmek bir yetki YÜKSELTMESİ değildir (kişi zaten talebi
# `pending`ken silebilir/geri çekebilir — `LEAVE_DELETE_NOT_ALLOWED`).
LEAVE_APPROVE_OWN_REQUEST = (
    "Kendi izin talebinizi onaylayamazsınız; kararı başka bir yetkili vermelidir."
)


# --- İK-2.2: talebi GERİ ÇEKME korkuluğu ----------------------------------

# 409 — geri çekme YALNIZ `pending` talepte anlamlıdır. `LEAVE_NOT_PENDING`
# ("düzenlenebilir ya da silinebilir") ve `LEAVE_DECISION_NOT_PENDING`
# ("onaylanabilir ya da reddedilebilir") metinlerinden AYRIDIR ve bu bilinçlidir
# (guards.py:117 emsali): kullanıcıya yaptığı eylemin adıyla konuşmayan bir
# korkuluk, onu YANLIŞ eylemi aramaya yollar. Karara bağlanmış bir talebi geri
# çekmek onayı ya da reddi SESSİZCE İPTAL ederdi.
LEAVE_WITHDRAW_NOT_PENDING = "Yalnız bekleyen izin talebi geri çekilebilir"


# --- KARARLAR.md §1.10 (kullanıcı kararı 2026-09-23): açık `null` -> 422 ------
#
# NOT NULL kolona PATCH gövdesinde AÇIKÇA `null` gönderilirse alan adıyla
# (pydantic `loc` üzerinden) 422 verilir — DB'ye düşüp opak 409 "Veri bütünlüğü
# hatası" olmaz. Emsal `contracts/schemas.py::EmployerContractItemUpdate.
# _acik_null_reddedilir`: `field_validator(..., mode="before")` YALNIZ gövdede
# GEÇEN alan için çalışır (pydantic'in "before" doğrulayıcı davranışı);
# "alan hiç gönderilmedi" hâli ETKİLENMEZ, yalnız AÇIK `null` reddedilir.
#
# Metin BİLEREK sabittir (alan adını TEKRARLAMAZ): field adı zaten pydantic
# hata gövdesinin `loc`unda vardır, mesajın kendisi jenerik kalır (kanon metni
# birebir).
PERSONNEL_FIELD_NOT_NULL = "Alan boşaltılamaz; değiştirmemek için gövdeden çıkarın."

# Hedef kolonu DB'de `nullable=False` olan `PersonnelUpdate` alanları.
# 🔴 ELLE YAZILMIŞ bir liste bir sonraki NOT NULL alan eklendiğinde SESSİZCE
# kör kalır (depo kanonu: "sayı değil BEKÇİ yaz") — bu yüzden bu tuple
# `tests/personnel/test_personnel_ik_api.py`de `Personnel.__table__.columns`
# ÜZERİNDEN TÜRETİLİP doğrulanır; elle kopya DEĞİL, DB şemasının izdüşümüdür.
PERSONNEL_NULLABLE_OLMAYAN_ALANLAR: tuple[str, ...] = (
    "full_name",
    "source",
    "is_active",
    "is_draft",
)


def validate_personnel_source(source: WorkerSource, subcontractor_id: uuid.UUID | None) -> None:
    """Kural BİRLEŞİK kayıt üzerinde koşar.

    PATCH'te kaynak değişip taşeron alanı değişmeyebilir (ya da tersi); bu yüzden
    çağıran taraf DB'deki değerlerle gövdedeki değerleri birleştirip buraya öyle
    verir. Yalnız gövdeye bakmak, `subcontractor -> company` geçişinde eski taşeron
    bağının kayıtta kalmasına izin verirdi.
    """
    if source is not WorkerSource.subcontractor and subcontractor_id is not None:
        raise PersonnelValidationError(SUBCONTRACTOR_NOT_ALLOWED)


def validate_tckn(tc_no: str) -> None:
    """T.C. kimlik numarası checksum doğrulaması (spec §5 K1) — geçersizse 422.

    Kural: 11 hane, hepsi rakam, ilk hane 0 DEĞİL; 10. hane =
    ((1.+3.+5.+7.+9. hane)*7 - (2.+4.+6.+8. hane)) mod 10; 11. hane = ilk 10
    hanenin toplamı mod 10. NULL/boş kontrolü ÇAĞIRANIN işidir — bu fonksiyon
    yalnız DOLU değerle çağrılır (taslak serbest).
    """
    if len(tc_no) != 11 or not tc_no.isdigit():
        raise PersonnelValidationError(INVALID_TCKN)
    d = [int(c) for c in tc_no]
    if d[0] == 0:
        raise PersonnelValidationError(INVALID_TCKN)
    tek = d[0] + d[2] + d[4] + d[6] + d[8]
    cift = d[1] + d[3] + d[5] + d[7]
    if (tek * 7 - cift) % 10 != d[9]:
        raise PersonnelValidationError(INVALID_TCKN)
    if sum(d[:10]) % 10 != d[10]:
        raise PersonnelValidationError(INVALID_TCKN)


def missing_publish_fields(merged: object) -> list[str]:
    """Birleşik kayıtta EKSİK ✱ alanların Türkçe etiketleri (boş liste = tam).

    `merged` mevcut kayıt + PATCH gövdesinin birleşimidir (P6 `_merged` deseni);
    yayın (is_draft=false) yalnız bu birleşim tam ise geçer. Boş string de EKSİK
    sayılır (kullanıcı alanı temizlemiş olabilir).
    """
    eksik: list[str] = []
    for attr, label in PUBLISH_REQUIRED_FIELDS:
        value = getattr(merged, attr, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            eksik.append(label)
    return eksik

"""Şantiye günlüğü korkulukları ve Türkçe hata metinleri (spec §2, §3).

Metinler TEK yerde durur; router'a ya da servise gömülü string YAZILMAZ
(`subcontractor_progress_payments/guards.py` deseninin aynısı).

`sites` modülünün "bulunamadı" cümleleri KOPYALANMAZ, İMPORT edilir: günlük ucu
görünmeyen bir şantiye için `sites` ucundan FARKLI bir cümle dönerse, elinde bir
UUID olan kullanıcı iki uç arasındaki farktan kaydın var olduğunu çıkarabilir.
"""

from decimal import Decimal

from app.modules.sites.guards import SITE_MISSING

__all__ = [
    "DUPLICATE_WORKER_SUBCONTRACTOR",
    "LINE_SECTION_MISMATCH",
    "LINE_SECTION_NOT_ALLOCATED",
    "TEMP_ORDER",
    "WORKER_SUBCONTRACTOR_SOURCE",
    "WORKER_SUBCONTRACTOR_UNKNOWN",
    "temp_order_ok",
    "DUPLICATE_LINE",
    "INVALID_STATUS_TRANSITION",
    "DUPLICATE_WORKER_COUNT",
    "ENTRY_DATE_TAKEN",
    "ENTRY_MISSING",
    "ENTRY_NOT_DELETABLE",
    "ENTRY_NOT_EDITABLE",
    "LINE_ITEM_MISMATCH",
    "MAX_YEAR",
    "MIN_YEAR",
    "SECTION_MISMATCH",
    "SITE_MISSING",
    "SUGGESTION_NO_BRIDGE",
    "SUGGESTION_NO_QUANTITY",
    "TRADE_REQUIRED",
    "WORKER_COUNTS_NULL",
    "YEAR_REQUIRED_FOR_MONTH",
]

#: `year` sınırı — EMSALLERDEN alındı, icat EDİLMEDİ: `treasury/router.py:289`
#: (`cash_flow.MIN_YEAR`/`MAX_YEAR`), `equipment/router.py:223,249,378`,
#: `payroll/router.py:537`, `personnel/router.py:746` hepsi 2000-2200 kullanır.
#: Sınırsız bırakılınca değer `period_conditions` → `_month_bounds`
#: (repository.py:141-146) üzerinden doğrudan `datetime.date()`e gidiyordu ve
#: `date(0,1,1)` / `date(10000,1,1)` `ValueError` fırlatıyordu; bu istisnanın
#: `core/exception_handlers.py`de handler'ı YOK, yani kullanıcı girdisi 500
#: üretiyordu. `month` zaten `Query(ge=1, le=12)` ile sınırlıydı.
#:
#: TEK kopya burada durur (`guards.py`): hem `router.py` (liste + özet) hem
#: `router_suggestion.py` (öneri) buradan İTHAL eder — `router.py` bunu
#: `router_suggestion` modülünü de import ettiği için (`router.include_router`),
#: sabitler `router.py`de tanımlansaydı `router_suggestion.py`nin ondan import
#: etmesi DAİRESEL İTHAL olurdu (kayıt #38'in "tek kopya" gerekçesi burada da
#: geçerli: iki tanım TANIMLAMA, ortak yere TAŞI).
MIN_YEAR = 2000
MAX_YEAR = 2200

# 404 — kayıt yok VEYA kapsam dışında. İki hâl AYIRT EDİLEMEZ (spec §3 IDOR
# kuralı): görünmeyen projedeki gerçek kayıt için 403 dönmek, kaydın varlığını
# sızdırmanın en kısa yoludur.
ENTRY_MISSING = "Günlük kayıt bulunamadı"

# 409 (`DuplicateError`) — UQ (site_id, entry_date) ihlali. Servis `IntegrityError`a
# DÜŞMEDEN ÖNCE açık bir SELECT ile bunu fırlatır; kullanıcı "Veri bütünlüğü
# hatası" gibi anlamsız bir cümle değil NE YAPACAĞINI görür. Genel `IntegrityError`
# handler'ı (409) yarış durumu emniyet ağı olarak KALIR.
ENTRY_DATE_TAKEN = "Bu şantiyede bu güne ait günlük kayıt zaten var"

# 409 (`ConflictError`) — gönderilmiş kayda YAZMA yasağı. Durumu geri almanın tek
# yolu `reopen` ucudur (T4); PATCH sessizce başarısız olmaz, açıkça çakışır.
ENTRY_NOT_EDITABLE = "Gönderilmiş günlük kayıt düzenlenemez"

# 409 (`ConflictError`) — silme kuralının BİRİNCİ katmanı: gönderilmiş kayıt
# ADMİN DAHİL kimseye silinmez. `can_delete` (ikinci katman) admin'e koşulsuz
# izin verdiği için bu kontrol ondan ÖNCE koşmak zorundadır.
ENTRY_NOT_DELETABLE = "Gönderilmiş günlük kayıt silinemez"

# 422 — bölüm bilgi alanıdır ama SAHİPSİZ olamaz: günlüğün şantiyesine ait
# olmalıdır. Var OLMAYAN bölüm de AYNI 422'yi alır — bölüm bir alan DEĞERİDİR,
# ayrı bir kaynak değil (`subcontractor_progress_payments.guards.SECTION_MISMATCH`
# gerekçesinin aynısı).
SECTION_MISMATCH = "Seçilen bölüm bu şantiyeye ait değil"

# --- T3: satır + işçi kırılımı yazma ---

# 422 — `PUT …/lines` gövdesindeki poz günlüğün ŞANTİYESİNİN BOQ'suna ait değil.
# Var OLMAYAN poz da AYNI cümleyi alır: `boq_item_id` bir alan DEĞERİDİR, ayrı bir
# kaynak değil — iki hâl ayrıştırılsaydı elinde UUID olan kullanıcı komşu
# şantiyenin pozunun varlığını 404/422 farkından çıkarabilirdi.
LINE_ITEM_MISMATCH = "Seçilen poz bu şantiyenin BOQ'suna ait değil"

# 409 (`DuplicateError`) — kısmi UQ `uq_site_diary_lines_boq_item` ihlali GÖVDE
# İÇİNDE yakalanır. `IntegrityError` emniyet ağı olarak kalır ama kullanıcının
# normalde göreceği cümle budur ("Veri bütünlüğü hatası" değil).
DUPLICATE_LINE = "Aynı poz (aynı bölümle) gövdede birden fazla kez gönderildi"

# 409 (`DuplicateError`) — UQ (entry_id, trade, source) ihlali. Aynı meslek FARKLI
# kaynakla meşrudur (GK418-430 rozetleri); çakışan yalnız ÜÇLÜNÜN tamamıdır.
DUPLICATE_WORKER_COUNT = "Aynı meslek ve kaynak için birden fazla işçi satırı gönderildi"

# 422 — `trade` serbest metindir (katalog YOK, spec §2); katalogsuz bir alanın tek
# korkuluğu boşluğun reddidir. Kırpılmadan kabul edilseydi " Kalıpçı" ile "Kalıpçı"
# UQ'da AYRI iki satır olur, ekranda aynı meslek iki kez görünürdü.
TRADE_REQUIRED = "Meslek adı boş olamaz"

# 422 — `worker_counts: null`. `null` bir NİYET DEĞİLDİR: sessizce yok sayılsaydı
# "hepsini sil" demek isteyen kullanıcı sildiğini sanırdı. Temizlemenin tek yolu
# BOŞ LİSTEDİR; alanın hiç gönderilmemesi ise "dokunma" demektir.
WORKER_COUNTS_NULL = "İşçi kırılımı listesi null olamaz; temizlemek için boş liste gönderin"

# --- T4: durum akışı ---

# 409 (`ConflictError`) — geçiş tablosunda (`transitions.TRANSITIONS`) OLMAYAN
# hücre. Sessiz/idempotent geçiş YOKTUR: ikinci kez "Gönder"e basan kullanıcıya
# "gönderdim" demek ilk gönderimin damgasını sessizce üzerine yazmak olurdu,
# taslak bir kaydı "geri al"mak da hiç yapılmamış bir işi yapılmış göstermek.
# TEK cümle iki yönü de kapsar (`subcontractor_progress_payments.guards`
# deseninin aynısı): hangi geçişin neden reddedildiğini ayrıştırmak, kaydın
# durumunu bilmeyen bir istemciye durum bilgisi sızdırmaktır.
INVALID_STATUS_TRANSITION = "Bu durum geçişi yapılamaz"

# --- T5: hakediş "günlükten doldur" önerisi (spec §4, §7 S2) ---
#
# Bu İKİ metin bir HATA DEĞİL, 200 yanıtın `reason` alanıdır: öneri BOŞ döndüğünde
# kullanıcı NEDEN boş olduğunu görmelidir. Sessiz boş liste, "günlüğe hiç miktar
# girilmemiş" ile "girilmiş ama köprü kurulmamış" hâllerini ayırt edilemez kılar
# ve kullanıcıyı var olmayan bir veri hatasını aramaya gönderir.

# Dönemde gönderilmiş günlük miktarı YOK (ya da hepsi sıfır). Taslak günlükler
# SAYILMAZ (spec §3) — kullanıcı "girdim ama görünmüyor" derse cevabı budur:
# kayıt henüz GÖNDERİLMEMİŞTİR.
SUGGESTION_NO_QUANTITY = "Seçilen dönemde gönderilmiş günlük miktarı yok"

# Miktar VAR ama poz sözleşme kalemine köprülenmemiş: `boq_items.contract_item_id`
# NULL (işveren) ya da hiçbir taşeron kalemi o kaleme bağlı değil. Öneri hangi
# hakediş satırına yazılacağını BİLEMEZ; köprü BOQ ekranında kurulur.
SUGGESTION_NO_BRIDGE = (
    "Günlükte miktar var ama pozlar sözleşme kalemine bağlanmamış; öneri üretilemedi"
)

# 422 — `month` tek başına anlamsızdır ("her yılın temmuzu" bir dönem değildir).
# Sessizce yok saymak, kullanıcının filtrelediğini sandığı bir listeyi filtresiz
# göstermek olurdu.
YEAR_REQUIRED_FOR_MONTH = "Ay filtresi için yıl da belirtilmelidir"

# --- PLN-B2.1: çekirdek iyileştirmeleri (hava · bölüm kırılımı · taşeron satırı) ---

# 422 — min > max. Gövde içinde şema, PATCH'te BİRLEŞİK değer (gövde + mevcut)
# üzerinden servis söyler; DB CHECK `ck_site_diary_entries_temp_min_le_max` son ağdır.
TEMP_ORDER = "Minimum sıcaklık maksimum sıcaklıktan büyük olamaz"


def temp_order_ok(temp_min: Decimal | None, temp_max: Decimal | None) -> bool:
    """İkisi de doluysa min ≤ max; biri boşsa kural yok (taslak yarım doldurulabilir)."""
    return temp_min is None or temp_max is None or temp_min <= temp_max


# 422 — satırın bölümü günlüğün şantiyesine ait değil (var olmayan bölüm AYNI cümle).
LINE_SECTION_MISMATCH = "Satırdaki bölüm bu şantiyeye ait değil"

# 422 — kalemin o bölüme TAHSİSİ yok. Yaprak = kalem × bölüm (spec §2); tahsis
# edilmemiş kısım "Bölümsüz" satıra (section_id boş) yazılır.
LINE_SECTION_NOT_ALLOCATED = (
    "Kalemin bu bölüme tahsisi yok; miktarı Bölümsüz satıra yazın ya da önce BOQ'da tahsis edin"
)

# 422 — firma bağı yalnız taşeron kaynaklı satırda anlamlıdır (DB CHECK ikizi).
WORKER_SUBCONTRACTOR_SOURCE = "Taşeron firması yalnız kaynağı taşeron olan satıra bağlanabilir"

# 422 — var olmayan firma (kimlik bir alan DEĞERİDİR; ayrı kaynak değil).
WORKER_SUBCONTRACTOR_UNKNOWN = "Seçilen taşeron firması bulunamadı"

# 409 — kısmi UQ `uq_site_diary_worker_counts_entry_subcontractor` gövde içinde.
DUPLICATE_WORKER_SUBCONTRACTOR = "Aynı taşeron firması için birden fazla işçi satırı gönderildi"

# PLN-B2.x-A — ESKİ ISTEMCI BEKCISI: bolum/firma alanlarini hic tasimayan (B2 oncesi) bir
# istemci DEGISTIRME semantigiyle kaydedince yeni alanli satirlari sessizce silerdi.
STALE_CLIENT = "Sayfa güncellendi, yenileyip tekrar deneyin"

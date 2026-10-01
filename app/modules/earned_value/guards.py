"""Planlama (EV) kullaniciya gorunen hata metinleri — TEK yer (Turkce)."""

from __future__ import annotations

DISCIPLINE_MISSING = "Disiplin bulunamadı"
DISCIPLINE_CODE_TAKEN = "Bu disiplin kodu zaten kayıtlı"
DISCIPLINE_IN_USE = "Disiplin kullanımda (BOQ grubu eşlemesi, katalog ya da baseline); silinemez"
DISCIPLINE_ASSIGNED_TO_USERS = "Disiplin kullanıcılara atanmış; önce atamalar kaldırılmalı"
CATALOG_ITEM_MISSING = "Katalog iş tipi bulunamadı"
#: EV-BORC-5: ad alanına özel — normalize eşleşmede VAR OLAN kaydın yazımı gösterilir.
CATALOG_ITEM_TAKEN_AS = (
    "Ad: bu disiplinde aynı ad ve birimle bir iş tipi zaten var — «{name}» ({uom}). "
    "Büyük/küçük harf, İ/I ve boşluk farkı ayrı iş tipi sayılmaz"
)
CATALOG_NO_ACTUAL = "Bu iş tipi için tamamlanmış şantiye gerçekleşeni yok"

HOLIDAY_RANGE_INVALID = "Tatil bitişi başlangıçtan önce olamaz"
HOLIDAY_RANGE_OVERLAP = "Tatil aralıkları çakışıyor"
DAILY_BANDS_ORDER = "Günlük PF bantları sıralı olmalı: kırmızı ≤ yeşil ≤ şüpheli yüksek"
WEEKLY_BANDS_ORDER = "Haftalık PF bantları sıralı olmalı: kırmızı ≤ yeşil"
ALL_DAYS_OFF = "En az bir çalışma günü olmalı"
COMPOSITE_ITEM_FOREIGN = "Paçal metrikteki iş tipi bu şantiyenin BOQ'unda yok"

REVISION_MISSING = "Revizyon bulunamadı"
NO_DRAFT = "Açık taslak revizyon yok"
DRAFT_EXISTS = "Zaten açık bir taslak revizyon var"
NOT_DRAFT = "Yalnız taslak revizyon düzenlenebilir"
BOQ_GROUP_FOREIGN = "BOQ grubu bu şantiyeye ait değil"
BOQ_ITEM_FOREIGN = "BOQ kalemi bu şantiyeye ait değil"
SECTION_FOREIGN = "Bölüm bu şantiyeye ait değil"
LEAF_MISSING = "Bu kalem × bölüm için planlı miktar yok (BOQ tahsisi)"
RATE_NEGATIVE = "Birim oran negatif olamaz"
WINDOW_RANGE_INVALID = "Pencere bitişi başlangıçtan önce olamaz"
FREEZE_BLOCKED = "Baseline dondurulamaz: engeller giderilmeli"
SITE_COMPLETED_READ_ONLY = "Tamamlanmış şantiyenin planlama ayarları salt okunurdur"
SITE_COMPLETED_BUDGET_READ_ONLY = "Tamamlanmış şantiyenin bütçesi salt okunurdur"
SITE_COMPLETED_DAY_READ_ONLY = "Tamamlanmış şantiyede planlama kaydı salt okunurdur"

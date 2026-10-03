"""Cekirdek katalog hata metinleri (TKL-B1; EV `guards.py` yeniden ihrac eder)."""

from __future__ import annotations

#: KATALOG-UQ-2 duzeltme turu: NFKC normalize METNİ UZATABİLİR (bazı kod noktalarında 18
#: kata kadar) — ad/birim kolon sınırı içinde kalsa da türetilen anahtar taşabilir.
#: `{field}` "Ad"/"Birim", `{max_len}` kolon sınırı (bkz. `labels.NAME_KEY_MAX_LEN`/
#: `UOM_KEY_MAX_LEN`).
CATALOG_KEY_TOO_LONG = (
    "{field} normalize edildikten sonra çok uzun (sınır: {max_len} karakter); "
    "özel karakterler (üst simge, ligatür, tam genişlik vb.) normalizasyonda uzayabilir"
)

DISCIPLINE_MISSING = "Disiplin bulunamadı"
DISCIPLINE_CODE_TAKEN = "Bu disiplin kodu zaten kayıtlı"
CATALOG_ITEM_MISSING = "Katalog iş tipi bulunamadı"
#: EV-BORC-5: ad alanına özel — normalize eşleşmede VAR OLAN kaydın yazımı gösterilir.
CATALOG_ITEM_TAKEN_AS = (
    "Ad: bu disiplinde aynı ad ve birimle bir iş tipi zaten var — «{name}» ({uom}). "
    "Büyük/küçük harf, İ/I ve boşluk farkı ayrı iş tipi sayılmaz"
)

#: KAT-B1: Bakanlik/kaynak poz kodu tekil (kismi UQ); tekil uc 409 metni.
SOURCE_CODE_TAKEN_AS = (
    "Kaynak poz no: bu kod zaten kayıtlı — {poz_no} · {name} ({uom}); "
    "kaynak poz no şirket genelinde tekildir"
)
#: KAT-B1 (2a): fiyat tarihi yalniz referans fiyatla anlamlidir.
REF_PRICE_DATE_NEEDS_PRICE = (
    "Fiyat tarihi: referans fiyat olmadan fiyat tarihi verilemez; "
    "önce referans fiyatı girin ya da tarihi boş bırakın"
)
#: KAT-B1 toplu ekleme (satir bazli yapisal hata metinleri).
BULK_REJECTED = "Toplu ekleme reddedildi: {count} satırda hata var; hiçbir kalem yazılmadı"
BULK_SOURCE_REPEATED = "Kaynak poz no: istek içinde tekrar ediyor — ilk geçtiği satır {first}"
BULK_ITEM_REPEATED = (
    "Ad: istek içinde aynı disiplinde aynı ad ve birimle başka bir satır var — satır {first}"
)
BULK_SOURCE_EXISTS = (
    "Kaynak poz no: bu kod katalogda zaten kayıtlı — {poz_no} · {name} ({uom}); "
    "fiyatı güncellemek için on_source_conflict=update_price kullanın"
)

#: KAT-B1.1: istek sirasinda katalog degisti (siniflandirma kilit altinda farkli cikti).
BULK_CATALOG_CHANGED = (
    "Toplu ekleme sırasında katalog başka bir işlemle değişti; hiçbir kalem yazılmadı, "
    "isteği yeniden deneyin"
)
#: KAT-B1.1 (D5): eslesen kalemde fiyat gonderilmeden tarih verildi, kalemin fiyati yok.
BULK_DATE_ON_PRICELESS_ITEM = (
    "Fiyat tarihi: eşleşen kalemin referans fiyatı yok; tarih için `ref_price` de gönderin"
)
SOURCE_CODE_CONTROL_CHARS = "Kaynak poz no: kontrol/biçim karakteri (görünmez karakter) içeremez"
REF_PRICE_DATE_OUT_OF_RANGE = "Fiyat tarihi: {low:%d.%m.%Y} ile {high:%d.%m.%Y} arasında olmalı"

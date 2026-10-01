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

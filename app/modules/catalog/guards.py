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

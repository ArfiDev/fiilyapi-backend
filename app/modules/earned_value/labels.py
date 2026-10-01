"""Ad/birim NORMALIZASYONU — YENIDEN IHRAC (BLF-B1.1).

Kural, sabitler ve tam gerekce `app/core/labels.py`ye TASINDI: `sites` cekirdek
moduldur ve planlamayi (EV) import EDEMEZ (PLANLAMA-SPEC §2.7), ama bolum tipi
tekilligi ayni `normalize_label` kuralini ister. Bu modul davranis DEGISTIRMEDEN
ayni adlari disari verir; mevcut EV import yollari (`earned_value.labels`) kirilmaz.
"""

from app.core.labels import (
    NAME_KEY_MAX_LEN,
    UOM_KEY_MAX_LEN,
    normalize_label,
)

__all__ = ["NAME_KEY_MAX_LEN", "UOM_KEY_MAX_LEN", "normalize_label"]

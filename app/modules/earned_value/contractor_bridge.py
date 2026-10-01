"""Cekirdek <-> motor `ContractorType` kopru yardimcilari (TKL-B1).

`EvDiscipline`/`EvCatalogItem` cekirdek `app.modules.catalog`ta; kolonlari CEKIRDEK
`ContractorType` dondurur (yazma yonunu kolonun `@validates`i normalize eder). Motor
`isinstance(.., ContractorType)` (`engine/types.py:107`) ve EV raporlari `is ContractorType.OWN`
(`panel_headcount.py`, `report_panel.py`) kullandigindan ORM'dan okunan deger motora girmeden
ONCE `to_engine_ct` ile cevrilir — ZORUNLU nokta `budget_repository.load_disciplines`.
Iki enum da `str` alt sinifi oldugundan deger ile arama (`Enum(value)`) duz metni de, oteki
enumun uyesini de kabul eder; `value.value` duz metinde `AttributeError` (500) verirdi.
"""

from __future__ import annotations

from typing import overload

from app.modules.catalog.models import ContractorType as CoreContractorType
from app.modules.earned_value.engine import ContractorType as EngineContractorType


@overload
def to_engine_ct(value: None) -> None: ...
@overload
def to_engine_ct(
    value: CoreContractorType | EngineContractorType | str,
) -> EngineContractorType: ...
def to_engine_ct(
    value: CoreContractorType | EngineContractorType | str | None,
) -> EngineContractorType | None:
    return None if value is None else EngineContractorType(value)

"""TKL-B1 — cekirdek <-> motor `ContractorType` siniri: okuma yonu `to_engine_ct` (motor
`isinstance`/`is` kullanir), yazma yonu kolonun `@validates`i; iki enum ve duz metin
kabul edilir."""

import pytest

from app.modules.catalog.models import ContractorType as CoreContractorType
from app.modules.catalog.models import EvCatalogItem, EvDiscipline
from app.modules.earned_value.contractor_bridge import to_engine_ct
from app.modules.earned_value.engine import ContractorType as EngineContractorType


@pytest.mark.parametrize("value", [CoreContractorType.OWN, EngineContractorType.OWN, "own"])
def test_to_engine_ct_hedef_motor_enumu(value: object) -> None:
    sonuc = to_engine_ct(value)  # type: ignore[arg-type]
    assert sonuc is EngineContractorType.OWN


@pytest.mark.parametrize(
    "value", [CoreContractorType.SUBCON, EngineContractorType.SUBCON, "subcon"]
)
@pytest.mark.parametrize("model", [EvDiscipline, EvCatalogItem])
def test_kolon_hep_cekirdek_enum_tutar(model: type, value: object) -> None:
    """Yazma yonu kolonun `@validates`i: motor enumu / duz metin atansa da kolonda cekirdek
    enum durur (tip oturum gecmisine bagli olmasin)."""
    nesne = model(default_contractor_type=value)
    assert nesne.default_contractor_type is CoreContractorType.SUBCON


def test_none_none_kalir() -> None:
    assert to_engine_ct(None) is None

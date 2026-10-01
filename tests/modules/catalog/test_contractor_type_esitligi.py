"""TKL-B1 bekcisi: cekirdek `ContractorType` ikizi == motor `ContractorType` (uye, deger, sira).

Cekirdek motoru import edemez (`test_engine_isolation`), bu yuzden enum IKI yerde yazili;
sapma (bir tarafa uye eklemek) DB'ye yazilan/okunan degerleri sessizce ayirirdi. Iki kolon
PG tipi `ev_contractor_type`i EV'deki 3 tabloyla (toplam 5) PAYLASIR: tip adi ve deger listesi AYNI
olmali (yoksa create_all/alembic tipi ikinci kez yaratmaya calisir).
"""

from sqlalchemy import Enum

from app.modules.catalog.models import ContractorType as CoreContractorType
from app.modules.catalog.models import EvCatalogItem, EvDiscipline
from app.modules.earned_value.engine.types import ContractorType as EngineContractorType
from app.modules.earned_value.models import _contractor_enum


def _pairs(enum_cls) -> list[tuple[str, str]]:
    return [(m.name, m.value) for m in enum_cls]


def test_cekirdek_ikiz_motor_enumuyla_ayni_uye_deger_ve_sirada() -> None:
    assert _pairs(CoreContractorType) == _pairs(EngineContractorType), (
        "Cekirdek ContractorType ikizi motor enumundan SAPTI: "
        f"cekirdek={_pairs(CoreContractorType)} motor={_pairs(EngineContractorType)}"
    )


def test_iki_kolon_ev_contractor_type_tipini_ev_ile_ayni_degerlerle_kullanir() -> None:
    ev_tip = _contractor_enum()
    for model in (EvDiscipline, EvCatalogItem):
        tip = model.__table__.c.default_contractor_type.type
        assert isinstance(tip, Enum)
        assert tip.name == "ev_contractor_type" == ev_tip.name, f"{model.__name__}: PG tip adi"
        assert list(tip.enums) == list(ev_tip.enums), f"{model.__name__}: PG enum degerleri"

"""SIL-B1 — `DeleteKind` (OpenAPI enum) ile motorun tür kayıt defteri ve HTTP yüzeyi eşit olmalı."""

from app.core.silme.turler import kayitli_turler
from app.modules.silme import kayitlar  # noqa: F401  (tür kaydı)
from app.modules.silme.schemas import DeleteKind


def test_enum_uyeleri_ile_kayit_defteri_birebir_ayni() -> None:
    assert {k.value for k in DeleteKind} == set(kayitli_turler())


def test_her_turun_kok_tablosu_ve_404_mesaji_dolu() -> None:
    for anahtar, tur in kayitli_turler().items():
        assert tur.anahtar == anahtar
        assert tur.tablo and tur.etiket
        assert tur.bulunamadi.endswith("bulunamadı")

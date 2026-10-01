"""TKL-B1 bekcisi: PLANLAMA-SPEC §2.7 madde 2 — EV semasi siniri.

Kural A: EV-sahipli OLMAYAN hicbir tablonun FK'si EV-sahipli tabloyu hedeflemez
(cekirdek istege bagli modulun tablosuna baglanamaz).
Kural B: `ev_` oneki EV-sahiplik demektir; TEK istisna cekirdege tasinan
`CORE_EV_PREFIXED` (TKL-B1, TKL-PLAN §1) — bunlar cekirdek sahipli OLMAK ZORUNDA.
"EV-sahipli" = mapper sinifi `app.modules.earned_value` paketinde tanimli tablo.
"""

from app.core.db import Base
from app.modules.catalog import models as _catalog  # noqa: F401  (kayit icin)
from app.modules.earned_value import models as _ev  # noqa: F401  (kayit icin)

CORE_EV_PREFIXED = frozenset({"ev_disciplines", "ev_catalog_items"})
MIN_TARANAN_TABLO = 110
MIN_EV_TABLO = 18
EV_PAKETI = "app.modules.earned_value"


def _ev_sahipli() -> set[str]:
    return {
        m.local_table.name
        for m in Base.registry.mappers
        if m.class_.__module__ == EV_PAKETI or m.class_.__module__.startswith(EV_PAKETI + ".")
    }


def test_tarayici_kor_degil() -> None:
    tablolar = Base.metadata.tables
    assert len(tablolar) >= MIN_TARANAN_TABLO, f"yalniz {len(tablolar)} tablo tarandi"
    assert len(_ev_sahipli()) >= MIN_EV_TABLO, f"yalniz {len(_ev_sahipli())} EV tablosu bulundu"


def test_kural_a_cekirdek_tablo_ev_tablosuna_fk_vermez() -> None:
    ev = _ev_sahipli()
    ihlaller = [
        f"{tablo.name}.{fk.parent.name} -> {fk.column.table.name}"
        for tablo in Base.metadata.tables.values()
        if tablo.name not in ev
        for fk in tablo.foreign_keys
        if fk.column.table.name in ev
    ]
    assert not ihlaller, (
        "EV-sahipli olmayan tablo EV tablosuna FK veriyor (§2.7 madde 2 ihlali): "
        + "; ".join(sorted(ihlaller))
    )


def test_kural_b_ev_oneki_yalniz_ev_sahipli_istisna_cekirdek_sahipli() -> None:
    ev = _ev_sahipli()
    onekliler = {ad for ad in Base.metadata.tables if ad.startswith("ev_")}
    sahipsiz = sorted(onekliler - ev - CORE_EV_PREFIXED)
    assert not sahipsiz, f"`ev_` oneki tasiyip EV paketinde olmayan tablo(lar): {sahipsiz}"
    ev_icinde = sorted(CORE_EV_PREFIXED & ev)
    assert not ev_icinde, f"Cekirdek sahipli olmasi gereken tablo EV paketinde: {ev_icinde}"


def test_istisna_listesi_bayatlamamis() -> None:
    ev = _ev_sahipli()
    eksik = sorted(ad for ad in CORE_EV_PREFIXED if ad not in Base.metadata.tables)
    assert not eksik, f"Istisna listesinde metadata'da olmayan tablo (bayat): {eksik}"
    assert not (CORE_EV_PREFIXED & ev), "Istisna listesindeki tablo EV-sahipli (bayat)"

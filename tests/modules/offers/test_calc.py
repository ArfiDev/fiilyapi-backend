"""TKL-B4.1 — `offers/calc.py`: saf hesap (T31). Beklenenler ELLE hesaplanmis SABIT degerlerdir.

Formul: B.F. = ROUND(c x (1+g) x (1+k)); tutar = ROUND(B.F. x q); maliyet = ROUND(c x q);
GG = ROUND(c x (1+g) x q) - maliyet; kar = tutar - maliyet - GG.
"""

from __future__ import annotations

import ast
import dataclasses
import random
from decimal import Decimal as D
from pathlib import Path

import pytest

from app.modules.offers import calc
from app.modules.offers.calc import (
    ItemInput,
    ManualPriceWithoutCostError,
    OfferCalcError,
    calc_item,
    calc_revision,
    suggest_cost,
)

REV = {"overhead_pct": D("12"), "profit_pct": D("15")}


def _item(c: str | None, q: str = "1", **kw) -> ItemInput:
    return ItemInput(
        quantity=D(q),
        unit_mhr=kw.pop("unit_mhr", D("1")),
        cost_unit_price=None if c is None else D(c),
        **{k: (None if v is None else D(v)) for k, v in kw.items()},
    )


def _values(item: ItemInput, **rev) -> tuple:
    r = calc_item(item, **{**REV, **rev})
    assert r.customer and r.internal
    return (
        r.customer.unit_price,
        r.customer.amount,
        r.internal.cost,
        r.internal.overhead,
        r.internal.profit,
    )


# ------------------------------------------------------------------ kalem


def test_tablo_c100_g12_k15_birim_fiyat_128_80() -> None:
    assert _values(_item("100")) == (D("128.80"), D("128.80"), D("100.00"), D("12.00"), D("16.80"))


def test_miktar_10_tutar_maliyet_gg_kar() -> None:
    assert _values(_item("100", "10")) == (
        D("128.80"),
        D("1288.00"),
        D("1000.00"),
        D("120.00"),
        D("168.00"),
    )


def test_maliyet_sifir_fiyatli_ve_hepsi_sifir() -> None:
    r = calc_item(_item("0", "5"), **REV)
    assert r.priced is True
    assert r.customer is not None and r.internal is not None
    assert (r.customer.unit_price, r.customer.amount) == (D("0.00"), D("0.00"))
    assert (r.internal.cost, r.internal.overhead, r.internal.profit) == (D(0), D(0), D(0))


@pytest.mark.parametrize(
    ("profit_pct", "beklenen_bf"),
    [
        # 10 x 1.0005 = 10.005 -> HALF_UP 10.01 (banker's: 10.00)
        ("0.05", D("10.01")),
        # 10 x 1.0025 = 10.025 -> HALF_UP 10.03 (banker's: 10.02)
        ("0.25", D("10.03")),
    ],
)
def test_birim_fiyat_yarim_kurus_ROUND_HALF_UP(profit_pct: str, beklenen_bf: D) -> None:
    bf, *_ = _values(_item("10", overhead_pct="0", profit_pct=profit_pct))
    assert bf == beklenen_bf


def test_tutar_ve_maliyet_yarim_kurus_yukari() -> None:
    # B.F. 1.00 x 0.005 = 0.005 -> 0.01 ; maliyet 1.00 x 0.005 -> 0.01
    bf, amount, cost, gg, kar = _values(_item("1", "0.005", overhead_pct="0", profit_pct="0"))
    assert (bf, amount, cost, gg, kar) == (D("1.00"), D("0.01"), D("0.01"), D("0.00"), D("0.00"))


def test_kalem_gg_ve_kar_revizyon_yuzdesini_EZER() -> None:
    # 200 x 1.05 x 1.10 = 231.00 (revizyon 12/15 gecersiz)
    bf, amount, cost, gg, kar = _values(_item("200", overhead_pct="5", profit_pct="10"))
    assert (bf, amount, cost, gg, kar) == (
        D("231.00"),
        D("231.00"),
        D("200.00"),
        D("10.00"),
        D("21.00"),
    )


def test_kalem_sadece_gg_ezer_kar_revizyondan_gelir() -> None:
    # g=0 (kalem), k=15 (revizyon): 100 x 1 x 1.15
    assert _values(_item("100", overhead_pct="0"))[0] == D("115.00")


def test_kalem_sifir_yuzde_ezmesi_None_ile_karismaz() -> None:
    """`0` bir DEGERDIR (ezer); `None` revizyona duser."""
    assert _values(_item("100", overhead_pct="0", profit_pct="0"))[0] == D("100.00")
    assert _values(_item("100", overhead_pct=None, profit_pct=None))[0] == D("128.80")


def test_elle_birim_fiyat_turev_kar_yuzdesi() -> None:
    r = calc_item(_item("100", "2", offer_unit_price="140"), **REV)
    assert r.customer and r.internal
    # 140 / (100 x 1.12) - 1 = 0.25
    assert r.customer.unit_price == D("140")
    assert r.customer.amount == D("280.00")
    assert (r.internal.cost, r.internal.overhead, r.internal.profit) == (
        D("200.00"),
        D("24.00"),
        D("56.00"),
    )
    assert r.internal.profit_pct == D("25.00")


def test_elle_birim_fiyat_maliyet_sifirsa_turev_kar_yuzdesi_None() -> None:
    r = calc_item(_item("0", "3", offer_unit_price="10"), **REV)
    assert r.internal and r.customer
    assert r.internal.profit_pct is None
    assert r.customer.amount == D("30.00")
    assert (r.internal.cost, r.internal.overhead, r.internal.profit) == (D(0), D(0), D("30.00"))


def test_elle_birim_fiyat_ve_maliyet_YOKSA_hata_SO4() -> None:
    with pytest.raises(ManualPriceWithoutCostError):
        calc_item(_item(None, offer_unit_price="100"), **REV)
    assert issubclass(ManualPriceWithoutCostError, OfferCalcError)
    assert issubclass(OfferCalcError, ValueError)


def test_maliyet_yok_kalem_fiyatsiz_ama_adam_saati_var() -> None:
    r = calc_item(_item(None, "4", unit_mhr=D("2.5")), **REV)
    assert r.priced is False
    assert r.customer is None
    assert r.internal.man_hours == D("10.0")  # adam-saat IC yapidadir (T37)
    assert (r.internal.cost, r.internal.overhead, r.internal.profit) == (None, None, None)


def test_uygulanan_kar_yuzdesi_elle_fiyat_yokken_kalem_veya_revizyon_kari() -> None:
    r = calc_item(_item("100", profit_pct="20"), **REV)
    assert r.internal and r.internal.profit_pct == D("20")


# --------------------------------------------------------------- revizyon


def test_revizyon_toplamlari_ve_genel_kar_yuzdesi() -> None:
    items = [_item("100"), _item("100", offer_unit_price="150"), _item(None, "2")]
    r = calc_revision(items, vat_pct=D("20"), **REV)
    # net 128.80 + 150.00 ; maliyet 200 ; GG 24 ; kar 16.80 + 38.00
    assert r.customer.net == D("278.80")
    assert r.customer.vat == D("55.76")
    assert r.customer.gross == D("334.56")
    assert (r.internal.cost, r.internal.overhead, r.internal.profit) == (
        D("200.00"),
        D("24.00"),
        D("54.80"),
    )
    # 54.80 / 224 = 24.4642.. -> 24.46
    assert r.internal.profit_pct == D("24.46")
    assert r.unpriced_count == 1
    assert [i.priced for i in r.items] == [True, True, False]


def test_fiyatsiz_kalem_toplama_GIRMEZ() -> None:
    only = calc_revision([_item("100")], vat_pct=D("20"), **REV)
    with_unpriced = calc_revision([_item("100"), _item(None, "50")], vat_pct=D("20"), **REV)
    assert only.customer == with_unpriced.customer
    assert only.internal.cost == with_unpriced.internal.cost
    assert with_unpriced.unpriced_count == 1


def test_kdv_yarim_kurus_yukari() -> None:
    # net 0.25, KDV %10 -> 0.025 -> 0.03 (banker's: 0.02)
    r = calc_revision([_item("0.25", overhead_pct="0", profit_pct="0")], vat_pct=D("10"), **REV)
    assert r.customer.net == D("0.25")
    assert (r.customer.vat, r.customer.gross) == (D("0.03"), D("0.28"))


def test_toplam_adam_saat_fiyatsiz_dahil() -> None:
    items = [
        _item("1", "2.5", unit_mhr=D("1.8")),
        _item("1", "10", unit_mhr=D("0.25")),
        _item(None, "1", unit_mhr=D("3.0")),
    ]
    r = calc_revision(items, vat_pct=D("20"), **REV)
    assert r.internal.man_hours == D("10.00")  # 4.5 + 2.5 + 3.0


def test_bos_revizyon_sifir_ve_kar_yuzdesi_None() -> None:
    r = calc_revision([], vat_pct=D("20"), **REV)
    assert (r.customer.net, r.customer.vat, r.customer.gross) == (D(0), D(0), D(0))
    assert r.internal.profit_pct is None and r.unpriced_count == 0


def test_tum_maliyetler_sifirsa_genel_kar_yuzdesi_None() -> None:
    r = calc_revision([_item("0")], vat_pct=D("20"), **REV)
    assert r.internal.profit_pct is None


def test_DEGISMEZ_maliyet_GG_kar_tutari_toplar_500_rastgele_kalem() -> None:
    rng = random.Random(20261002)
    items: list[ItemInput] = []
    for _ in range(500):
        c = None if rng.random() < 0.1 else D(rng.randint(0, 2_000_000)) / 100
        manual = None
        if c is not None and rng.random() < 0.3:
            manual = D(rng.randint(0, 3_000_000)) / 100
        items.append(
            ItemInput(
                quantity=D(rng.randint(1, 5_000_000)) / 1000,
                unit_mhr=D(rng.randint(1, 100_000)) / 10_000,
                cost_unit_price=c,
                overhead_pct=None if rng.random() < 0.5 else D(rng.randint(0, 10_000)) / 100,
                profit_pct=None if rng.random() < 0.5 else D(rng.randint(0, 99_999)) / 100,
                offer_unit_price=manual,
            )
        )
    rev = calc_revision(items, overhead_pct=D("12.37"), profit_pct=D("15.55"), vat_pct=D("18.5"))

    fiyatli = 0
    for r in rev.items:
        if not r.priced:
            continue
        fiyatli += 1
        assert r.customer and r.internal
        assert r.internal.cost + r.internal.overhead + r.internal.profit == r.customer.amount
        assert r.customer.amount == r.customer.amount.quantize(D("0.01"))
    assert fiyatli > 400
    assert rev.customer.net == sum((r.customer.amount for r in rev.items if r.customer), D(0))
    assert rev.internal.cost + rev.internal.overhead + rev.internal.profit == rev.customer.net
    assert rev.customer.gross == rev.customer.net + rev.customer.vat
    assert rev.unpriced_count == 500 - fiyatli


# ------------------------------------------------------------ suggest_cost


@pytest.mark.parametrize(
    ("son", "ref", "beklenen"),
    [
        (D("5"), D("10"), D("5")),  # son fiyat kazanir
        (None, D("10"), D("10")),  # son yok -> referans
        (None, None, None),  # ikisi de yok -> bos
        (D("0"), D("10"), D("0")),  # sifir da gecerli bir son fiyattir
    ],
)
def test_suggest_cost_son_ref_bos(son, ref, beklenen) -> None:
    assert suggest_cost(son, ref) == beklenen


# ------------------------------------------- B4.1 denetim bulgulari (E2, E4, E5)


def test_E2a_birim_fiyat_ONCE_yuvarlanir_tutar_miktarla_buyur() -> None:
    """c=1,00 g=12 k=15 q=1000: B.F. = ROUND(1,288) = 1,29 → tutar 1290,00. "B.F. yuvarlamadan
    x q" mutanti 1288,00 verirdi (yuvarlama miktarla BUYUR)."""
    bf, tutar, maliyet, gg, kar = _values(_item("1.00", "1000"))
    assert bf == D("1.29")
    assert tutar == D("1290.00")
    assert (maliyet, gg, kar) == (D("1000.00"), D("120.00"), D("170.00"))


def test_E2b_GG_bagimsiz_yuvarlanmaz_iki_yuvarlamanin_FARKIDIR() -> None:
    """c=1,01 q=2,5 g=7 k=15 (ELLE): maliyet = ROUND(2,525) = 2,53; c(1+g)q = 2,70175 → 2,70;
    GG = 2,70 - 2,53 = 0,17. BAGIMSIZ ROUND(c x g x q) = ROUND(0,17675) = 0,18 olurdu.
    B.F. = ROUND(1,01 x 1,07 x 1,15 = 1,242805) = 1,24; tutar = ROUND(3,10) = 3,10;
    kar = 3,10 - 2,53 - 0,17 = 0,40 (bagimsiz GG ile 0,39)."""
    bf, tutar, maliyet, gg, kar = _values(_item("1.01", "2.5", overhead_pct="7"))
    assert (bf, tutar, maliyet, gg, kar) == (
        D("1.24"),
        D("3.10"),
        D("2.53"),
        D("0.17"),
        D("0.40"),
    )


def test_E5_elle_birim_fiyat_kurus_ustu_hassasiyet_savunmasi_ROUND() -> None:
    """Sema 2 haneyi zorlar; calc buna GUVENMEZ: elle B.F. 10,005 → 10,01 (HALF_UP)."""
    r = calc_item(_item("5", "1", offer_unit_price="10.005"), **REV)
    assert r.customer and r.customer.unit_price == D("10.01")
    assert r.customer.amount == D("10.01")


def test_E4_tavandaki_girdiler_InvalidOperation_vermez_ve_tam_dogrudur() -> None:
    """Sema tavani: maliyet <= 1e12, miktar <= 1e9, kar <= 999,99 %, GG <= 100 %. Tek kalem
    tutari 25 hane; 20 000 kalemin toplami 30 hane — varsayilan 28 haneli baglamda
    `quantize` `InvalidOperation` verirdi. Beklenen ELLE: B.F. = 1e12 x 2 x 10,9999 =
    21 999 800 000 000,00; tutar = x 1e9."""
    tavan = ItemInput(
        quantity=D("1000000000"),
        unit_mhr=D("1"),
        cost_unit_price=D("1000000000000.00"),
        overhead_pct=D("100"),
        profit_pct=D("999.99"),
    )
    bf = D("21999800000000.00")
    tutar = D("21999800000000000000000.00")
    r = calc_item(tavan, **REV)
    assert r.customer and r.customer.unit_price == bf and r.customer.amount == tutar
    assert r.internal.cost == D("1000000000000000000000.00")
    assert r.internal.overhead == D("1000000000000000000000.00")
    assert r.internal.cost + r.internal.overhead + r.internal.profit == tutar

    rev = calc_revision([tavan] * 20_000, vat_pct=D("100"), **REV)
    assert rev.customer.net == tutar * 20_000
    assert rev.customer.vat == rev.customer.net  # KDV %100
    assert rev.customer.gross == rev.customer.net * 2
    assert rev.internal.cost + rev.internal.overhead + rev.internal.profit == rev.customer.net


# --------------------------------------------------------------- yapisal


def _alanlar(*siniflar: type) -> set[str]:
    return {f.name for s in siniflar for f in dataclasses.fields(s)}


#: Isveren ciktisinda ASLA gorunmeyecek kavramlarin ad parcalari (T37).
_IC_KAVRAMLAR = ("cost", "overhead", "profit", "man_hours", "mhr", "hours", "margin")


def test_musteri_ve_ic_alt_yapilari_AYRIK_alanlar_tasir() -> None:
    musteri = _alanlar(calc.CustomerLine, calc.CustomerTotals)
    ic = _alanlar(calc.InternalLine, calc.InternalTotals)
    assert musteri == {"unit_price", "amount", "net", "vat", "gross"}
    assert not musteri & ic
    assert {"cost", "overhead", "profit", "profit_pct", "man_hours"} <= ic


def test_E3_ust_duzey_sonuclar_ic_alan_TASIMAZ_musteri_yapisinda_ic_kavram_adi_yok() -> None:
    """Isveren ciktisi (B5) `customer` alt yapilarindan kurulur; adam-saat/maliyet/GG/kar HICBIR
    musteri alaninin adinda gecmez ve `ItemResult`/`RevisionResult` ust duzeyine SIZMAZ."""
    ic = _alanlar(calc.InternalLine, calc.InternalTotals)
    musteri = _alanlar(calc.CustomerLine, calc.CustomerTotals)
    assert not [a for a in musteri if any(k in a for k in _IC_KAVRAMLAR)], musteri
    kalem_ust = _alanlar(calc.ItemResult)
    revizyon_ust = _alanlar(calc.RevisionResult)
    # B5.1 (SO-21): `quantified` ve `unquantified_count` YAPISAL alanlardir (para/ic deger degil).
    assert kalem_ust == {"priced", "customer", "internal", "quantified"}
    assert revizyon_ust == {
        "items",
        "customer",
        "internal",
        "unpriced_count",
        "unquantified_count",
    }
    assert not (kalem_ust | revizyon_ust) & ic, "ic alan ust duzeye SIZDI"
    assert not [a for a in kalem_ust | revizyon_ust if any(k in a for k in _IC_KAVRAMLAR)]


def test_E3_fiyatsiz_kalemde_adam_saat_ic_yapida_musteri_yapisi_bos() -> None:
    r = calc_item(_item(None, "4", unit_mhr=D("2.5")), **REV)
    assert r.customer is None and r.internal.man_hours == D("10.0")
    fiyatli = calc_item(_item("10", "4", unit_mhr=D("2.5")), **REV)
    assert fiyatli.internal.man_hours == D("10.0")
    assert fiyatli.customer is not None
    assert not any(
        hasattr(fiyatli.customer, k) for k in ("man_hours", "cost", "overhead", "profit")
    )


def test_calc_modulu_yalniz_stdlib_import_eder() -> None:
    kaynak = Path(calc.__file__).read_text(encoding="utf-8")
    kokler: set[str] = set()
    for node in ast.walk(ast.parse(kaynak)):
        if isinstance(node, ast.Import):
            kokler |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            kokler.add(node.module.split(".")[0])
    assert kokler <= {"__future__", "collections", "dataclasses", "decimal"}, kokler

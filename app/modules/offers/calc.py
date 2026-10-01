"""Teklif hesabi — SAF Decimal modulu (TKL-B4.1, TKL-PLAN §2.3a "Hesap", T31).

Bu modul DB, ORM ya da web katmani BILMEZ (yalniz stdlib `decimal`/`dataclasses`): hesap TEK
yerdedir, ekran yalniz gosterir. Para birimi YALNIZ TL (T36). Yuzdeler YUZDE BIRIMIYLE girer
(`12` = %12). Yuvarlama her yerde `ROUND_HALF_UP`, 0,01.

## Formul (kalem)
`g` = kalem GG % ?? revizyon GG %, `k` = kalem kar % ?? revizyon kar %, `c` = maliyet B.F.

* Elle teklif B.F. (`P`) varsa B.F. = `P`; `c` BOS ise HATA (SO-4, `ManualPriceWithoutCostError`:
  kar % geri hesabi ve `maliyet + GG + kar = tutar` degismezi maliyet ister).
  Turev kar % = `P / (c x (1+g)) - 1` (`c > 0` iken; `c = 0` ise `None`).
* Yoksa `c` varsa B.F. = `ROUND(c x (1+g) x (1+k))`; `c` yoksa kalem FIYATSIZdir
  (toplamlara girmez, `unpriced_count`).
* `tutar = ROUND(B.F. x miktar)` · `maliyet = ROUND(c x miktar)` ·
  `GG = ROUND(c x (1+g) x miktar) - maliyet` · `kar = tutar - maliyet - GG`.

🔴 DEGISMEZ: her fiyatli kalemde `maliyet + GG + kar == tutar` BIREBIR. Bu, kar'in tutardan
KALAN olarak turetilmesiyle saglanir: ucunu de bagimsiz yuvarlamak (ornegin GG'yi
`ROUND(c x g x miktar)` ile) kurus kayar. Toplamlarda `net = SUM(tutar)`.

## Toplam (revizyon)
net = SUM(tutar) · KDV = `ROUND(net x kdv)` · brut = net + KDV · toplam adam-saat =
SUM(miktar x unit_mhr) (fiyatsiz kalemler DAHIL) · genel kar % = SUM(kar) / (SUM(maliyet) +
SUM(GG)) x 100 (payda 0 ise `None`).

## Iki gorunum (B5): isveren ciktisi ve ic cikti
Sonuc yapilari musteriye GORUNUR ve IC alanlari AYRI alt yapilarda tasir ki B5 suzmesi
tek satir olsun: `CustomerLine`/`CustomerTotals` (B.F., tutar, net/KDV/brut) isveren ciktisina
girer; `InternalLine`/`InternalTotals` (maliyet, GG, kar, kar %, adam-saat) ASLA girmez.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "CustomerLine",
    "CustomerTotals",
    "InternalLine",
    "InternalTotals",
    "ItemInput",
    "ItemResult",
    "ManualPriceWithoutCostError",
    "OfferCalcError",
    "RevisionResult",
    "calc_item",
    "calc_revision",
    "suggest_cost",
]

_CENT = Decimal("0.01")
_ONE = Decimal(1)
_HUNDRED = Decimal(100)
_PCT = Decimal("0.01")  # yuzde -> oran carpani


class OfferCalcError(ValueError):
    """Hesap girdisi tutarsiz — servis 422'ye cevirir."""


class ManualPriceWithoutCostError(OfferCalcError):
    """SO-4: maliyet B.F. bos iken elle teklif B.F. verilemez."""


@dataclass(frozen=True, slots=True)
class ItemInput:
    """Bir kalemin hesap girdisi. `None` = girilmemis (revizyon degeri / hesaplanir)."""

    quantity: Decimal
    unit_mhr: Decimal
    cost_unit_price: Decimal | None = None
    overhead_pct: Decimal | None = None
    profit_pct: Decimal | None = None
    offer_unit_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CustomerLine:
    """Isverene GORUNUR kalem degerleri."""

    unit_price: Decimal
    amount: Decimal


@dataclass(frozen=True, slots=True)
class InternalLine:
    """IC kalem degerleri — isveren ciktisinda YOKTUR."""

    cost: Decimal
    overhead: Decimal
    profit: Decimal
    #: Elle B.F. varsa turetilmis kar % (`c = 0` ise `None`); yoksa uygulanan kar %.
    profit_pct: Decimal | None
    man_hours: Decimal


@dataclass(frozen=True, slots=True)
class ItemResult:
    """`priced` yanlissa `customer`/`internal` `None`; adam-saat YINE de `man_hours`tadir."""

    priced: bool
    man_hours: Decimal
    customer: CustomerLine | None
    internal: InternalLine | None


@dataclass(frozen=True, slots=True)
class CustomerTotals:
    net: Decimal
    vat: Decimal
    gross: Decimal


@dataclass(frozen=True, slots=True)
class InternalTotals:
    cost: Decimal
    overhead: Decimal
    profit: Decimal
    #: `None` = payda (maliyet + GG) sifir.
    profit_pct: Decimal | None
    man_hours: Decimal


@dataclass(frozen=True, slots=True)
class RevisionResult:
    items: tuple[ItemResult, ...]
    customer: CustomerTotals
    internal: InternalTotals
    unpriced_count: int


def _round(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def calc_item(item: ItemInput, *, overhead_pct: Decimal, profit_pct: Decimal) -> ItemResult:
    """Tek kalem. `overhead_pct`/`profit_pct` revizyon yuzdeleridir (kalem degeri ezer)."""
    man_hours = item.quantity * item.unit_mhr
    cost_unit = item.cost_unit_price
    if cost_unit is None:
        if item.offer_unit_price is not None:
            raise ManualPriceWithoutCostError(
                "Maliyet birim fiyatı boşken elle teklif birim fiyatı girilemez"
            )
        return ItemResult(priced=False, man_hours=man_hours, customer=None, internal=None)

    g = (item.overhead_pct if item.overhead_pct is not None else overhead_pct) * _PCT
    k_pct = item.profit_pct if item.profit_pct is not None else profit_pct
    loaded_unit = cost_unit * (_ONE + g)  # c x (1+g)

    if item.offer_unit_price is not None:
        unit_price = item.offer_unit_price
        derived_pct = (
            _round((unit_price / loaded_unit - _ONE) * _HUNDRED) if loaded_unit > 0 else None
        )
    else:
        unit_price = _round(loaded_unit * (_ONE + k_pct * _PCT))
        derived_pct = k_pct

    amount = _round(unit_price * item.quantity)
    cost = _round(cost_unit * item.quantity)
    overhead = _round(loaded_unit * item.quantity) - cost
    profit = amount - cost - overhead
    return ItemResult(
        priced=True,
        man_hours=man_hours,
        customer=CustomerLine(unit_price=unit_price, amount=amount),
        internal=InternalLine(
            cost=cost,
            overhead=overhead,
            profit=profit,
            profit_pct=derived_pct,
            man_hours=man_hours,
        ),
    )


def calc_revision(
    items: Sequence[ItemInput],
    *,
    overhead_pct: Decimal,
    profit_pct: Decimal,
    vat_pct: Decimal,
) -> RevisionResult:
    """Revizyon toplamlari + kalem sonuclari (girdi sirasiyla)."""
    results = tuple(
        calc_item(item, overhead_pct=overhead_pct, profit_pct=profit_pct) for item in items
    )
    lines = [(r.customer, r.internal) for r in results if r.customer and r.internal]
    net = sum((c.amount for c, _ in lines), Decimal(0))
    cost = sum((i.cost for _, i in lines), Decimal(0))
    overhead = sum((i.overhead for _, i in lines), Decimal(0))
    profit = sum((i.profit for _, i in lines), Decimal(0))
    vat = _round(net * vat_pct * _PCT)
    base = cost + overhead
    return RevisionResult(
        items=results,
        customer=CustomerTotals(net=net, vat=vat, gross=net + vat),
        internal=InternalTotals(
            cost=cost,
            overhead=overhead,
            profit=profit,
            profit_pct=_round(profit / base * _HUNDRED) if base > 0 else None,
            man_hours=sum((r.man_hours for r in results), Decimal(0)),
        ),
        unpriced_count=len(results) - len(lines),
    )


def suggest_cost(last_price: Decimal | None, ref_price: Decimal | None) -> Decimal | None:
    """Maliyet onerisi (T32/SO-6): son fiyat → referans fiyat → bos (sifir da gecerlidir)."""
    if last_price is not None:
        return last_price
    return ref_price

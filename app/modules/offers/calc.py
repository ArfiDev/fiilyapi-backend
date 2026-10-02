"""Teklif hesabi — SAF Decimal modulu (TKL-B4.1, TKL-PLAN §2.3a "Hesap", T31).

Bu modul DB, ORM ya da web katmani BILMEZ (yalniz stdlib `decimal`/`dataclasses`): hesap TEK
yerdedir, ekran yalniz gosterir. Para birimi YALNIZ TL (T36). Yuzdeler YUZDE BIRIMIYLE girer
(`12` = %12). Yuvarlama her yerde `ROUND_HALF_UP`, 0,01.

## Formul (kalem)
`g` = kalem GG % ?? revizyon GG %, `k` = kalem kar % ?? revizyon kar %, `c` = maliyet B.F.

* Elle teklif B.F. (`P`) varsa B.F. = `P`; `c` BOS ise HATA (SO-4, `ManualPriceWithoutCostError`:
  kar % geri hesabi ve `maliyet + GG + kar = tutar` degismezi maliyet ister).
  Turev kar % = `P / (c x (1+g)) - 1` (`c > 0` iken; `c = 0` ise `None`).
* Elle B.F. de `ROUND` edilir (savunma: sema 2 haneyi zorlar, ama hesap buna guvenmez).
* Yoksa `c` varsa B.F. = `ROUND(c x (1+g) x (1+k))`; `c` yoksa kalem FIYATSIZdir
  (toplamlara girmez, `unpriced_count`).
* `tutar = ROUND(B.F. x miktar)` · `maliyet = ROUND(c x miktar)` ·
  `GG = ROUND(c x (1+g) x miktar) - maliyet` · `kar = tutar - maliyet - GG`.

🔴 DEGISMEZ: her fiyatli kalemde `maliyet + GG + kar == tutar` BIREBIR. Bu, kar'in tutardan
KALAN olarak turetilmesiyle saglanir: ucunu de bagimsiz yuvarlamak (ornegin GG'yi
`ROUND(c x g x miktar)` ile) kurus kayar. Toplamlarda `net = SUM(tutar)`.

## Miktarsiz kalem (SO-21)
`quantity is None` = "miktar girilmedi" (sablondan teklif). Miktarsiz kalem TOPLAMLARA GIRMEZ:
`CustomerLine.amount` ve `InternalLine.cost/overhead/profit` `None`; `InternalLine.man_hours`
da `None` ("bilinmiyor", 0 DEGIL) ve toplam adam-saate KATKISI yoktur. Fiyatli miktarsiz
kalemin B.F.si yine hesaplanir (`unit_price` dolu, ekran birim fiyati gosterebilir). AYRI sayac
`unquantified_count`:
miktari bos kalem sayisi. `unpriced_count` (maliyeti bos kalem) ile BAGIMSIZDIR: hem maliyeti
hem miktari bos kalem IKISINDE de sayilir. Fiyatli+miktarli kalemlerin hesabi DEGISMEZ.

## Toplam (revizyon)
net = SUM(tutar) · KDV = `ROUND(net x kdv)` · brut = net + KDV · toplam adam-saat =
SUM(miktar x unit_mhr) YALNIZ MIKTARLI kalemlerden (fiyatsiz kalemler DAHIL; miktarsiz kalemin
adam-saati bilinmedigi icin toplama girmez — toplam bu yuzden kismi olabilir, `unquantified_count`
kac kalemin eksik oldugunu soyler) · genel kar % = SUM(kar) / (SUM(maliyet) +
SUM(GG)) x 100 (payda 0 ise `None`).

## Iki gorunum (B5): isveren ciktisi ve ic cikti
Sonuc yapilari musteriye GORUNUR ve IC alanlari AYRI alt yapilarda tasir ki B5 suzmesi
tek satir olsun: `CustomerLine`/`CustomerTotals` (B.F., tutar, net/KDV/brut) isveren ciktisina
girer; `InternalLine`/`InternalTotals` (maliyet, GG, kar, kar %, adam-saat) ASLA girmez.
`ItemResult`/`RevisionResult` UST DUZEYI yalniz yapisal alanlar tasir (`priced`, `unpriced_count`,
alt yapilar): adam-saat FIYATSIZ kalemde de `internal` icindedir (T37: isveren ciktisinda adam-saat
YOKTUR).

## Hassasiyet (E4)
Hesap `Decimal` baglaminin varsayilan 28 hane sinirina GUVENMEZ: `_CALC_CONTEXT` (60 hane,
HALF_UP) icinde kosar; tavanli girdilerde (sema: B.F./maliyet <= 1e12, miktar <= 1e9) hicbir
ara deger `InvalidOperation`a dusmez ve yuvarlama sessizce kayamaz.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext

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
_CALC_CONTEXT = Context(prec=60, rounding=ROUND_HALF_UP)


class OfferCalcError(ValueError):
    """Hesap girdisi tutarsiz — servis 422'ye cevirir."""


class ManualPriceWithoutCostError(OfferCalcError):
    """SO-4: maliyet B.F. bos iken elle teklif B.F. verilemez."""


@dataclass(frozen=True, slots=True)
class ItemInput:
    """Bir kalemin hesap girdisi. `None` = girilmemis (revizyon degeri / hesaplanir)."""

    #: `None` = miktar girilmedi (SO-21): kalem toplamlara girmez.
    quantity: Decimal | None
    unit_mhr: Decimal
    cost_unit_price: Decimal | None = None
    overhead_pct: Decimal | None = None
    profit_pct: Decimal | None = None
    offer_unit_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CustomerLine:
    """Isverene GORUNUR kalem degerleri."""

    unit_price: Decimal
    #: `None` = miktarsiz kalem (SO-21): tutar uretilmez, toplamlara girmez.
    amount: Decimal | None


@dataclass(frozen=True, slots=True)
class InternalLine:
    """IC kalem degerleri — isveren ciktisinda YOKTUR. Fiyatsiz kalemde para alanlari `None`,
    `man_hours` YINE DE doludur (miktar varsa). `man_hours` `None` = miktar girilmedi: adam-saat
    BILINMIYOR (0 degil)."""

    man_hours: Decimal | None
    cost: Decimal | None = None
    overhead: Decimal | None = None
    profit: Decimal | None = None
    #: Elle B.F. varsa turetilmis kar % (`c = 0` ise `None`); yoksa uygulanan kar %.
    profit_pct: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ItemResult:
    """`priced` yanlissa `customer` `None`; `internal` HER ZAMAN vardir (adam-saat orada)."""

    priced: bool
    customer: CustomerLine | None
    internal: InternalLine
    #: Miktari dolu mu (SO-21). `priced and quantified` = toplamlara giren kalem.
    quantified: bool = True


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
    #: Maliyeti bos kalem sayisi (miktari bos olsa da sayilir).
    unpriced_count: int
    #: Miktari bos kalem sayisi (SO-21; maliyeti bos olsa da sayilir). `unpriced_count`la
    #: BAGIMSIZ: bir kalem ikisinde de sayilabilir.
    unquantified_count: int = 0


def _round(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def calc_item(item: ItemInput, *, overhead_pct: Decimal, profit_pct: Decimal) -> ItemResult:
    """Tek kalem. `overhead_pct`/`profit_pct` revizyon yuzdeleridir (kalem degeri ezer)."""
    with localcontext(_CALC_CONTEXT):
        return _calc_item(item, overhead_pct=overhead_pct, profit_pct=profit_pct)


def _calc_item(item: ItemInput, *, overhead_pct: Decimal, profit_pct: Decimal) -> ItemResult:
    quantified = item.quantity is not None
    man_hours = item.quantity * item.unit_mhr if item.quantity is not None else None
    cost_unit = item.cost_unit_price
    if cost_unit is None:
        if item.offer_unit_price is not None:
            raise ManualPriceWithoutCostError(
                "Maliyet birim fiyatı boşken elle teklif birim fiyatı girilemez"
            )
        return ItemResult(
            priced=False,
            customer=None,
            internal=InternalLine(man_hours=man_hours),
            quantified=quantified,
        )

    g = (item.overhead_pct if item.overhead_pct is not None else overhead_pct) * _PCT
    k_pct = item.profit_pct if item.profit_pct is not None else profit_pct
    loaded_unit = cost_unit * (_ONE + g)  # c x (1+g)

    if item.offer_unit_price is not None:
        unit_price = _round(item.offer_unit_price)
        derived_pct = (
            _round((unit_price / loaded_unit - _ONE) * _HUNDRED) if loaded_unit > 0 else None
        )
    else:
        unit_price = _round(loaded_unit * (_ONE + k_pct * _PCT))
        derived_pct = k_pct

    if item.quantity is None:  # SO-21: B.F. var, tutar/maliyet/GG/kar YOK
        return ItemResult(
            priced=True,
            customer=CustomerLine(unit_price=unit_price, amount=None),
            internal=InternalLine(man_hours=man_hours, profit_pct=derived_pct),
            quantified=False,
        )
    amount = _round(unit_price * item.quantity)
    cost = _round(cost_unit * item.quantity)
    overhead = _round(loaded_unit * item.quantity) - cost
    profit = amount - cost - overhead
    return ItemResult(
        priced=True,
        customer=CustomerLine(unit_price=unit_price, amount=amount),
        internal=InternalLine(
            man_hours=man_hours,
            cost=cost,
            overhead=overhead,
            profit=profit,
            profit_pct=derived_pct,
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
    with localcontext(_CALC_CONTEXT):
        return _calc_revision(
            items, overhead_pct=overhead_pct, profit_pct=profit_pct, vat_pct=vat_pct
        )


def _calc_revision(
    items: Sequence[ItemInput],
    *,
    overhead_pct: Decimal,
    profit_pct: Decimal,
    vat_pct: Decimal,
) -> RevisionResult:
    results = tuple(
        calc_item(item, overhead_pct=overhead_pct, profit_pct=profit_pct) for item in items
    )
    lines = [
        (r.customer, r.internal)
        for r in results
        if r.customer is not None and r.customer.amount is not None
    ]
    net = sum((c.amount for c, _ in lines if c.amount is not None), Decimal(0))
    cost = sum((i.cost or Decimal(0) for _, i in lines), Decimal(0))
    overhead = sum((i.overhead or Decimal(0) for _, i in lines), Decimal(0))
    profit = sum((i.profit or Decimal(0) for _, i in lines), Decimal(0))
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
            man_hours=sum(
                (r.internal.man_hours for r in results if r.internal.man_hours is not None),
                Decimal(0),
            ),
        ),
        unpriced_count=sum(1 for r in results if not r.priced),
        unquantified_count=sum(1 for r in results if not r.quantified),
    )


def suggest_cost(last_price: Decimal | None, ref_price: Decimal | None) -> Decimal | None:
    """Maliyet onerisi (T32/SO-6): son fiyat → referans fiyat → bos (sifir da gecerlidir)."""
    if last_price is not None:
        return last_price
    return ref_price

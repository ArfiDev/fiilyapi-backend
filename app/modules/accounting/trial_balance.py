"""Mizanın sorgu çekirdeği (MU-2 T4) — `projedesign/Muhasebe - Mizan.dc.html`.

Mockup 8 kolon çizer: `Hesap Kodu` · `Hesap Adı` · Açılış(Borç/Alacak) ·
Dönem Hareketi(Borç/Alacak) · Kapanış(Borç/Alacak).

## 🔴 Üç grup AYNI ŞEY DEĞİLDİR

| Grup | Nicelik | Kaç taraf dolu | Mockup |
|---|---|---|---|
| Açılış | **NET** (`Σdebit − Σcredit`) | en fazla BİRİ | satır 83-84, 123-124 |
| Dönem  | **BRÜT** (`Σdebit` ve `Σcredit` AYRI) | **İKİSİ BİRDEN** | satır 85-86 |
| Kapanış| **NET** | en fazla BİRİ | satır 87-88 |

Aritmetik (mockup satırları kendi içinde tutarlıdır):

* Kasa: `180.000 + (2.640.000 − 2.535.200) = 284.800` (satır 87)
* Satıcılar: `−840.000 + (6.120.000 − 7.464.000) = −2.184.000` → **Alacak**
  `2.184.000` (satır 128)

🔴 Dönem hareketini yanlışlıkla NET yazan bir uygulama, yalnız TEK TARAFLI
hareketi olan hesaplarda doğru sonuç verir — ayrışma ancak aynı hesapta hem borç
hem alacak varken görünür. Bekçisi
`test_donem_hareketi_BRUTTUR_iki_taraf_da_dolar_kapanis_TEK_taraflidir`.

## Dönem modeli: BİRİKİMLİ ARALIK

Mockup satır 45: `Ocak–Temmuz 2026`. Aralık YILIN OCAK ayından seçilen aya kadardır:

    açılış  : entry_date <  {year}-01-01                       → NET
    dönem   : {year}-01-01 ≤ entry_date ≤ {year}-{month} sonu  → BRÜT (iki toplam)
    kapanış : açılış_net + (dönem_borç − dönem_alacak)         → NET

Üçünde de yalnız `balance.POSTING_STATUSES` sayılır: `draft` HİÇ girmez,
`reversed` GİRER (çift ters kayıt kanonu — `balance.py` modül docstring'i).

## 🔴 `SIGN`/`balance_column()` MİZANDA KULLANILMAZ

Bu, "neden `balance.balance_column` çağırmadın" sorusunun kalıcı cevabıdır:
mizan kolonları **HAM borç/alacak**tır, tür-işaretli BAKİYE değil. `320`
Satıcılar (pasif) mizanda **Alacak** kolonuna düşer çünkü ham `net`i
negatiftir; `SIGN[liability] = −1` uygulansaydı sayı pozitife döner ve **Borç**
kolonuna yazılırdı — mockup satır 127-128 ile doğrudan çelişirdi. Aynı gerekçe
`ledger.py`de de geçerlidir ("İşaret HAM `net`tir").

Bakiyenin TEK KAYNAĞI ilkesi yine de korunur: fiş süzgeci `posting_filter()`ten
**AYNEN** okunur, `status == posted` hiçbir yerde elle yazılmaz.

## 🔴 Saf takvim — `today()` BİLİNMEZ

`year`/`month` uçta ZORUNLUDUR, dolayısıyla bu modül sunucunun "bugün"üne HİÇ
ihtiyaç duymaz: TB5'in yerel-takvim kusuru (üretimde kanıtlı, `date.today()`
UTC gününü okuyup TR gecesinde bir gün geri kalıyordu) burada YAPISAL OLARAK
imkânsızdır. Ay sonu `calendar.monthrange` ile saf aritmetikle bulunur —
`ledger.default_period()`ün aksine burada `timezone.today()` bile çağrılmaz.

## N+1

Kaç hesap olursa olsun **TEK** sorgu: üç pencere koşullu `SUM(CASE WHEN …)`
ifadeleriyle TEK `GROUP BY`da toplanır. 200 satırlık tekdüzen hesap planında
hesap başına sorgu koşan bir uygulama patlardı. Ölçüm tahmine değil
`before_cursor_execute` sayacına dayanır (`test_mu1_balance.py` emsali).

Para her yerde `Decimal`dir; kayan nokta hiçbir aşamada devreye girmez.
"""

import calendar
from datetime import date
from decimal import Decimal

from sqlalchemy import ColumnElement, Select, Subquery, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.accounting.balance import ZERO, posting_filter
from app.modules.accounting.bordro_hesaplari import bordro_hesabi_mi
from app.modules.accounting.models import ChartAccount, JournalEntry, JournalLine
from app.modules.accounting.reports_schemas import (
    TrialBalanceResponse,
    TrialBalanceRow,
    TrialBalanceTotals,
)

__all__ = ["build_trial_balance", "month_end", "year_start"]

_TUTAR_ALANLARI = (
    "opening_debit",
    "opening_credit",
    "period_debit",
    "period_credit",
    "closing_debit",
    "closing_credit",
)


def year_start(year: int) -> date:
    """Açılışın sınırı — yılın 1 Ocak'ı. Aralık SOLDAN AÇIKTIR (`<`)."""
    return date(year, 1, 1)


def month_end(year: int, month: int) -> date:
    """Ayın SON günü — `calendar.monthrange` ile saf aritmetik.

    🔴 28 sabitlenmiş bir uygulama 2028'de 29 Şubat'ı dışarıda bırakır ve o
    günün cirosu mizandan sessizce kaybolurdu (bekçisi
    `test_subat_ve_ARTIK_YIL_son_gunu_dogru_bulunur`).

    Fonksiyon SAFTIR: `date.today()`/`utcnow()` çağırmaz, dolayısıyla aynı girdi
    her koşuda aynı çıktıyı verir (K6 AST bekçisi de tetiklenmez).
    """
    return date(year, month, calendar.monthrange(year, month)[1])


def _kosullu_toplam(kosul: ColumnElement[bool], deger: ColumnElement) -> ColumnElement[Decimal]:
    """`SUM(CASE WHEN kosul THEN deger ELSE 0 END)` — üç pencerenin TEK yazımı.

    `else_` AÇIKTIR: bırakılsaydı pencereye hiç satır düşmeyen bir hesapta
    `SUM()` NULL döner ve dıştaki `COALESCE`a bağımlılık artardı. İki katman da
    yerinde durur (`balance.balance_column` ile aynı gerekçe).
    """
    return func.coalesce(func.sum(case((kosul, deger), else_=literal(ZERO))), literal(ZERO))


def _pencereler(year: int, month: int) -> Subquery:
    """Hesap başına ÜÇ pencere — **TEK** `GROUP BY`.

    Üç ayrı sorgu koşulsaydı biri süzgeci alır öteki almaz ve kapanış, açılış
    ile dönem hareketinin toplamı OLMAKTAN çıkardı. `join` INNER'dır: satırı
    hiç olmayan hesap burada kaybolur, dışarıdaki `outerjoin` onu `COALESCE`
    ile sıfıra düşürür (`balance.net_by_account` deseni).
    """
    baslangic = year_start(year)
    bitis = month_end(year, month)
    donem_penceresi = (JournalEntry.entry_date >= baslangic) & (JournalEntry.entry_date <= bitis)

    return (
        select(
            JournalLine.account_id.label("account_id"),
            # AÇILIŞ: NET (tek ifade — iki taraf ayrı okunsaydı biri süzgeci alır
            # öteki almazdı).
            _kosullu_toplam(
                JournalEntry.entry_date < baslangic, JournalLine.debit - JournalLine.credit
            ).label("opening_net"),
            # 🔴 DÖNEM: BRÜT — `debit` ve `credit` AYRI toplanır, netleştirilmez.
            _kosullu_toplam(donem_penceresi, JournalLine.debit).label("period_debit"),
            _kosullu_toplam(donem_penceresi, JournalLine.credit).label("period_credit"),
        )
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(posting_filter())
        .group_by(JournalLine.account_id)
        .subquery()
    )


def select_trial_balance_rows(year: int, month: int, include_empty: bool) -> Select:
    """Mizan satırlarının **TEK** `Select`i — sıralama `code` ARTAN.

    `outerjoin`dir ki `include_empty=True` hiç hareketi olmayan hesapları da
    getirebilsin; süzgeç `include_empty=False`ta DIŞ sorguda uygulanır (alt
    sorguya `HAVING` olarak konsaydı hareketsiz hesaplar zaten alt sorguda
    olmadığı için hiçbir şeyi elemezdi).
    """
    pencere = _pencereler(year, month)
    acilis = func.coalesce(pencere.c.opening_net, literal(ZERO))
    donem_borc = func.coalesce(pencere.c.period_debit, literal(ZERO))
    donem_alacak = func.coalesce(pencere.c.period_credit, literal(ZERO))

    stmt = (
        select(
            ChartAccount.id.label("account_id"),
            ChartAccount.code.label("account_code"),
            ChartAccount.name.label("account_name"),
            acilis.label("opening_net"),
            donem_borc.label("period_debit"),
            donem_alacak.label("period_credit"),
        )
        .outerjoin(pencere, pencere.c.account_id == ChartAccount.id)
        .order_by(ChartAccount.code.asc())
    )
    if not include_empty:
        # Üç pencerenin HİÇBİRİNDE hareketi olmayan hesap listelenmez: mockup'ın
        # 8 satırının hepsi hareketlidir (satır 80-159) ve tekdüzen hesap planının
        # kullanılmayan yüzlerce satırı mizanı okunamaz hâle getirirdi.
        # 🔴 Üç koşul da gereklidir: yalnız kapanışa bakılsaydı, borcu alacağına
        # EŞİT olan hareketli bir hesap (net 0) sessizce kaybolurdu.
        stmt = stmt.where(or_(acilis != 0, donem_borc != 0, donem_alacak != 0))
    return stmt


def _taraflar(net: Decimal) -> tuple[Decimal, Decimal]:
    """NET bir niceliği (borç, alacak) çiftine ayırır — en fazla BİRİ dolu.

    Sıfır İKİ TARAFI DA boş bırakır (mockup satır 133-134: `391`in açılışı iki
    kolonda da `—`); sıfırı borç tarafına yazan bir uygulama tfoot toplamını
    sahte biçimde şişirmezdi ama `is_balanced`in anlamını bozardı.
    """
    if net > 0:
        return net, ZERO
    if net < 0:
        return ZERO, -net
    return ZERO, ZERO


async def build_trial_balance(
    session: AsyncSession, *, year: int, month: int, include_empty: bool
) -> TrialBalanceResponse:
    """Mizanın tamamı — **TEK** sorgu, sayfalama YOK.

    Kapanış ve taraf ayrımı SQL'de değil burada yapılır: `CASE` ile SQL'e
    gömülseydi altı kolonun her biri için ayrı bir ifade yazılır ve "kapanış =
    açılış + (borç − alacak)" tanımı ALTI yerde tekrarlanırdı. Satır sayısı
    sınırlıdır (hesap planı ~200), maliyet ihmal edilebilir.

    `totals` TÜM kümenin toplamıdır — `rows` üzerinden toplanır ki tfoot ile
    satırların AYRIŞMASI yapısal olarak imkânsız olsun (K15: mockup'ta ayrışmış
    olması bir SUNUM göstermeliğidir, kural değil).
    """
    kayitlar = (
        (await session.execute(select_trial_balance_rows(year, month, include_empty)))
        .mappings()
        .all()
    )

    rows: list[TrialBalanceRow] = []
    for kayit in kayitlar:
        acilis_borc, acilis_alacak = _taraflar(kayit["opening_net"])
        donem_borc = kayit["period_debit"]
        donem_alacak = kayit["period_credit"]
        kapanis_borc, kapanis_alacak = _taraflar(kayit["opening_net"] + donem_borc - donem_alacak)
        rows.append(
            TrialBalanceRow(
                account_id=kayit["account_id"],
                account_code=kayit["account_code"],
                account_name=kayit["account_name"],
                opening_debit=acilis_borc,
                opening_credit=acilis_alacak,
                period_debit=donem_borc,
                period_credit=donem_alacak,
                closing_debit=kapanis_borc,
                closing_credit=kapanis_alacak,
            )
        )

    totals = TrialBalanceTotals(
        opening_debit=sum((r.opening_debit for r in rows), ZERO),
        opening_credit=sum((r.opening_credit for r in rows), ZERO),
        period_debit=sum((r.period_debit for r in rows), ZERO),
        period_credit=sum((r.period_credit for r in rows), ZERO),
        closing_debit=sum((r.closing_debit for r in rows), ZERO),
        closing_credit=sum((r.closing_credit for r in rows), ZERO),
    )
    # IZN-B5a (21a): bordro beslenen bir hesapta HAREKET/BAKIYE varsa genel toplam o tutari icerir.
    totals._bordro_dahil = any(
        bordro_hesabi_mi(r.account_code) and any(getattr(r, alan) for alan in _TUTAR_ALANLARI)
        for r in rows
    )
    return TrialBalanceResponse(
        year=year,
        month=month,
        # Mockup satır 54-57 kontrol banner'ı. 🔴 SÜS DEĞİLDİR: dengesizlik
        # KURULABİLİR — TB6 T2'den sonra BAŞLIKTAN değil SATIRLARDAN:
        # `ck_journal_entries_posting_balanced` BAŞLIK toplamlarını bağlar,
        # mizan ise `journal_lines`ı toplar; başlığı dengeli, satırları dengesiz
        # bir fiş kurulabilir (bekçisi `test_DENGESIZ_defterde_is_balanced_FALSE`).
        is_balanced=totals.closing_debit == totals.closing_credit,
        rows=rows,
        totals=totals,
    )

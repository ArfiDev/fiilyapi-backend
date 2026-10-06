"""Nakit Akış Tablosunun sorgu çekirdeği (MT-1 T5) — `Mali Tablo - Nakit Akışı`.

🔴 **`/treasury/cash-flow` İLE AYNI ŞEY DEĞİLDİR ve bu BİLİNÇLİDİR.**

| | `/treasury/cash-flow` | `/cash-flow-statement` (BURASI) |
|---|---|---|
| Taban | `payments` + `invoices` | **yevmiye** (`journal_lines`) |
| Şekil | ayın **GÜNLÜK** giriş/çıkış serisi | **A/B/C** işletme/yatırım/finansman |
| Pencere | TEK ay, `year`/`month` opsiyonel | Ocak→`month`, ikisi de ZORUNLU |
| Ekran | F-HZ hazine paneli (E9:90-106) | Mali Tablolar → Nakit Akışı (NA) |

İkisi FARKLI SAYI basar ve bu bir kusur değildir. Ayrım her iki modül
docstring'ine de yazılıdır (`treasury/cash_flow.py` karşılığı) — aksi hâlde
"iki nakit akışı farklı sayı basıyor" kusuru doğar ve hangisinin doğru olduğu
anlaşılamaz.

## 🔑 KK-2 — kaynak YEVMİYEDİR, `treasury.payments` DEĞİL

Kullanıcı kararı: Bilanço ile Nakit Akışı **TEK tabandan** gelmelidir. İki taban
olsaydı `Kasa ve Bankalar` (BL:51) ile `DÖNEM SONU NAKİT` (NA:100) sessizce
ayrışır ve hiçbir kolon farkı bunu ele vermezdi. Bu modül `treasury`yi ve
`invoicing`i HİÇ ithal etmez (döngüsüzlük testte ölçülür — `vat_return.py`
emsali).

## Akışın türetimi — dağıtım (allocation) YOKTUR

Nakit, grup `10` (Hazır Değerler) hesaplarıdır. Bir fiş dengeli olduğu için
`Σ(borç − alacak) = 0`, dolayısıyla:

    nakit değişimi = Σ_{nakit bacaklar}(borç − alacak)
                   = −Σ_{nakit OLMAYAN bacaklar}(borç − alacak)
                   = Σ_{nakit OLMAYAN bacaklar}(alacak − borç)

Yani **her nakit olmayan bacak, sınıflandırıldığı kaleme `alacak − borç` kadar
katkı yapar** ve katkıların toplamı TAM OLARAK nakit değişimine eşittir. Çok
bacaklı fişlerde "hangi karşı hesap" sorusuna oransal dağıtımla cevap aramaya
gerek YOKTUR; her bacak kendi kalemine gider.

Sonuçları:
* Yön kendiliğinden doğrudur: tahsilat (`120` alacaklanır) **+**, ödeme (`320`
  borçlanır) **−** — mockup NA:71-75 işaretleriyle birebir.
* 🔴 **Kasa→banka transferi akış ÜRETMEZ**: iki bacak da grup `10`dadır, nakit
  olmayan bacak YOKTUR → katkı sıfırdır (net nakit değişimi de sıfırdır).
* 🔴 **Nakde dokunmayan fiş HİÇ sayılmaz**: `EXISTS` süzgeci, satırlarından en
  az biri grup `10`da olan fişleri seçer. Olmasaydı vadeli bir satış fişi
  (`120`/`600`) "Müşterilerden Tahsilat" olarak basılır ve tablo tahakkuk
  tablosuna dönerdi.

Sınıflandırma karşı hesabın 2 haneli grubundan yapılır ve harita
`statement_map.py`dedir (TEK KOPYA, Bilanço ile paylaşılır).

## Dönem modeli BİRİKİMLİ ARALIK — mizanla AYNI pencere semantiği

Mockup NA:37 `Ocak–Temmuz 2026`:

    açılış nakdi : entry_date <  {year}-01-01                → NET
    akış         : {year}-01-01 <= entry_date <= month_end
    kapanış nakdi: entry_date <= month_end                   → NET

`trial_balance.year_start` ve `month_end` **İTHAL EDİLİR** — ikinci bir ay sonu
aritmetiği yazılsaydı biri artık yılı kaçırırdı (`calendar.monthrange`).
🔴 Açılış sınırı SOLDAN AÇIKTIR (`<`): `<=` yapılsaydı 1 Ocak hem açılışa hem
döneme sayılır ve kapanış çift gösterirdi (MU-2 T6'nın kaçırdığı kusur).

## Dört alan

`net_change` (A+B+C) ile `closing_cash` **AYRI ŞEYLERDİR**; mockup ikisini tek
satırda birleştirip çelişkiye düşmüştür (`DÖNEM SONU NAKİT (A+B+C)` etiketi ama
değeri BL:51 kapanış nakdi). Uç dördünü de döndürür; kimlik
`closing_cash == opening_cash + net_change` testte kilitlidir.

## N+1

**İKİ** sorgu: (1) nakit pencereleri (açılış + aylık hareket, TEK `GROUP BY`),
(2) sınıflandırma. `monthly_cash` için ay başına sorgu koşan bir uygulama 12
kat maliyetlenirdi; kümülatif toplama Python'da yapılır.

Para her yerde `Decimal`dir; uç **YUVARLAMAZ** (MT-K2).
"""

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import ColumnElement, Select, case, exists, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.modules.accounting import statement_map
from app.modules.accounting.balance import ZERO, posting_filter
from app.modules.accounting.bordro_hesaplari import bordro_hesabi_mi
from app.modules.accounting.models import ChartAccount, JournalEntry, JournalLine
from app.modules.accounting.reports_schemas import (
    CashFlowStatementLine,
    CashFlowStatementResponse,
    CashFlowStatementSection,
    MonthlyCashPoint,
)
from app.modules.accounting.trial_balance import month_end, year_start

__all__ = [
    "build_cash_flow_statement",
    "select_cash_flow_lines",
    "select_cash_windows",
]

#: Açılış nakdinin kovası. Ay numaraları `1..12` olduğu için `0` çakışmaz ve
#: pencere ayrımı TEK sorguda taşınır — ayrı bir açılış sorgusu, süzgeci
#: kopyalamak zorunda kalır ve biri gün gelip ötekinden ayrışırdı.
_OPENING_BUCKET = 0


def _grup(kolon: ColumnElement[str]) -> ColumnElement[str]:
    """Hesap kodunun 2 haneli TDHP grubu, SQL tarafında.

    `statement_map.group_of()`ün SQL karşılığıdır; Python tarafında yeniden
    hesaplanmaz, yalnız NAKİT süzgeci için gerekir (nakit olmayan bacakların
    sınıflandırması saf modülde yapılır).
    """
    return func.substr(kolon, 1, 2)


def select_cash_windows(year: int, month: int) -> Select:
    """Nakit kovaları — açılış (`0`) + `1..month` aylık hareket, **TEK** sorgu.

    Yalnız grup `10` hesapları sayılır (nakdin TANIMI, KK-2). Kova `CASE` ile
    üretilir: yıl başından önceki her şey `0`a, dönem içi hareketler kendi
    ayına düşer.

    🔴 `period_month` kolonu kullanılır, `EXTRACT` değil: kolon zaten vardır ve
    `ck_journal_entries_period_matches_date` onu `entry_date` ile KİLİTLER —
    ikisi ayrışamaz.
    """
    yil_basi = year_start(year)
    bitis = month_end(year, month)
    kova = case(
        (JournalEntry.entry_date < yil_basi, literal(_OPENING_BUCKET)),
        else_=JournalEntry.period_month,
    )
    return (
        select(
            kova.label("bucket"),
            func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), literal(ZERO)).label(
                "net"
            ),
        )
        .select_from(JournalLine)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .join(ChartAccount, ChartAccount.id == JournalLine.account_id)
        .where(
            posting_filter(),
            JournalEntry.entry_date <= bitis,
            _grup(ChartAccount.code) == statement_map.CASH_GROUP,
        )
        .group_by(kova)
    )


def select_cash_flow_lines(year: int, month: int) -> Select:
    """Nakde dokunan fişlerin NAKİT OLMAYAN bacakları — hesap başına `alacak − borç`.

    `EXISTS` süzgeci fişin en az bir grup `10` bacağı olmasını arar. Bir `JOIN`
    ile yazılsaydı iki nakit bacağı olan fiş karşı bacaklarını İKİ KEZ sayardı.

    Dış süzgeç grup `10`u ELER: nakdin kendisi bir "karşı hesap" olamaz ve
    kasa→banka transferi böylece kendiliğinden sıfır katkı verir.
    """
    yil_basi = year_start(year)
    bitis = month_end(year, month)

    nakit_satir = aliased(JournalLine)
    nakit_hesap = aliased(ChartAccount)
    fis_nakde_dokunuyor = exists(
        select(literal(1))
        .select_from(nakit_satir)
        .join(nakit_hesap, nakit_hesap.id == nakit_satir.account_id)
        .where(
            nakit_satir.entry_id == JournalEntry.id,
            _grup(nakit_hesap.code) == statement_map.CASH_GROUP,
        )
    )

    return (
        select(
            ChartAccount.code.label("code"),
            func.coalesce(func.sum(JournalLine.credit - JournalLine.debit), literal(ZERO)).label(
                "flow"
            ),
        )
        .select_from(JournalLine)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .join(ChartAccount, ChartAccount.id == JournalLine.account_id)
        .where(
            posting_filter(),
            JournalEntry.entry_date >= yil_basi,
            JournalEntry.entry_date <= bitis,
            _grup(ChartAccount.code) != statement_map.CASH_GROUP,
            fis_nakde_dokunuyor,
        )
        .group_by(ChartAccount.code)
        .order_by(ChartAccount.code.asc())
    )


def _nakit_serisi(
    kovalar: Sequence, year: int, month: int
) -> tuple[Decimal, list[MonthlyCashPoint], Decimal]:
    """Açılış · aylık KAPANIŞ BAKİYELERİ · kapanış — kümülatif toplamla.

    🔴 Seri AKIŞ DEĞİL BAKİYEDİR (grafiğin adı `Aylık Nakit Pozisyonu`, NA:109):
    her nokta açılış nakdini de içerir ve hareketsiz bir ayda ÖNCEKİ değeri
    tekrarlar. Aylık akış basan bir uygulama aynı veriyle bambaşka bir eğri
    çizer ve son noktası `closing_cash`e denk GELMEZDİ.
    """
    aylik_hareket = {int(kayit["bucket"]): kayit["net"] for kayit in kovalar}
    acilis = aylik_hareket.get(_OPENING_BUCKET, ZERO)

    seri: list[MonthlyCashPoint] = []
    yuruyen = acilis
    for ay in range(1, month + 1):
        yuruyen += aylik_hareket.get(ay, ZERO)
        seri.append(MonthlyCashPoint(year=year, month=ay, closing_cash=yuruyen))
    return acilis, seri, yuruyen


def _bolumler(
    tutarlar: dict[tuple[str, str], Decimal], kodlar: dict[tuple[str, str], list[str]]
) -> tuple[list[CashFlowStatementSection], Decimal]:
    """A/B/C ağacı + ara toplamlar.

    Ara toplamlar KALEMLERDEN toplanır, mockup'tan KOPYALANMAZ (K15: mockup'ın
    A bölümü satırları `5.842.000` toplarken ara toplam `6.842.000` basıyor —
    NA:71-78, 1.000.000 fark; bu bir SUNUM göstermeliğidir).
    """
    bolumler: list[CashFlowStatementSection] = []
    net_degisim = ZERO
    for bolum in statement_map.CASH_FLOW_SECTIONS:
        satirlar: list[CashFlowStatementLine] = []
        ara_toplam = ZERO
        for kalem in bolum.lines:
            anahtar = (bolum.key, kalem.key)
            tutar = tutarlar.get(anahtar, ZERO)
            satirlar.append(
                CashFlowStatementLine(
                    key=kalem.key,
                    label=kalem.label,
                    amount=tutar,
                    account_codes=sorted(kodlar.get(anahtar, [])),
                )
            )
            ara_toplam += tutar
        bolumler.append(
            CashFlowStatementSection(
                key=bolum.key,
                code=bolum.code,
                title=bolum.title,
                subtotal_label=bolum.subtotal_label,
                subtotal=ara_toplam,
                lines=satirlar,
            )
        )
        net_degisim += ara_toplam
    return bolumler, net_degisim


async def build_cash_flow_statement(
    session: AsyncSession, *, year: int, month: int
) -> CashFlowStatementResponse:
    """Nakit Akış Tablosunun tamamı — **İKİ** sorgu, sayfalama YOK.

    🔴 Kimlik: `closing_cash == opening_cash + net_change`. İki taraf AYRI
    sorgulardan gelir (biri nakit bacaklarını, öteki karşı bacakları toplar) ve
    dengeli bir defterde ZORUNLU olarak eşittir — eşitsizlik dengesiz bir
    `reversed` fişin işaretidir (bilançonun `is_balanced`i ile aynı sınıf
    kusur).
    """
    kovalar = (await session.execute(select_cash_windows(year, month))).mappings().all()
    acilis, seri, kapanis = _nakit_serisi(kovalar, year, month)

    tutarlar: dict[tuple[str, str], Decimal] = {}
    kodlar: dict[tuple[str, str], list[str]] = {}
    for kayit in (await session.execute(select_cash_flow_lines(year, month))).mappings().all():
        hedef = statement_map.cash_flow_line_for(kayit["code"])
        # 🔴 İKİNCİ KATMAN: sorgunun `WHERE`ı grup `10`u zaten eledi, bu dal
        # bugün ERİŞİLMEZDİR. Yine de duruyor çünkü sorgu bir gün gevşetilirse
        # (ör. tek sorguya birleştirme) nakit bacakları sessizce "karşı hesap"
        # sayılırdı. 🔴 T6'da ÖLÇÜLDÜ: iki katman birbirini MASKELER — SQL
        # süzgeci kaldırılınca HTTP ucundan hiçbir fark görünmüyordu (28/28
        # yeşil). Bu yüzden SQL katmanının KENDİ bekçisi vardır ve çekirdek
        # `Select`e iner (`test_SQL_katmani_grup_10u_KENDISI_eler`).
        if hedef is None:
            continue
        tutarlar[hedef] = tutarlar.get(hedef, ZERO) + kayit["flow"]
        kodlar.setdefault(hedef, []).append(kayit["code"])

    bolumler, net_degisim = _bolumler(tutarlar, kodlar)

    # IZN-B5a: bordro iceren bir donem akisi kumulatif nakit noktalarina da yansir (fail-closed).
    bordro_var = any(
        bordro_hesabi_mi(kod) for b in bolumler for k in b.lines for kod in k.account_codes
    )
    for nokta in seri:
        nokta._bordro_dahil = bordro_var
    return CashFlowStatementResponse(
        year=year,
        month=month,
        sections=bolumler,
        net_change=net_degisim,
        opening_cash=acilis,
        closing_cash=kapanis,
        monthly_cash=seri,
    )

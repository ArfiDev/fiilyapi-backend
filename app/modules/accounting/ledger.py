"""`GET /journal` — yevmiye defteri + 🔴 **KOŞAN BAKİYE** (MU-1 spec §6d, §6e).

E8:101-106 tablosu **SATIR bazlıdır**, fiş bazlı değil: altı satırın her biri bir
`journal_lines` kaydıdır ve boş tarafı hep `—`dir.

## Koşan bakiyenin TEK tanımı

Süzülmüş satır kümesinin **kanonik sıralamada** `Σ(debit − credit)` kümülatif
toplamı; başlangıç değeri **`carried_balance`** (pencere ÖNCESİ tüm satırların
aynı işaretli toplamı).

    carried_balance + SUM(jl.debit - jl.credit) OVER (
        ORDER BY je.entry_date, je.created_at, jl.sort_order, jl.id
        ROWS UNBOUNDED PRECEDING)

E8'in `Bakiye` sütunu mockup'ta **göstermeliktir** (tarih DESC iken artıp düşer,
hiçbir aritmetiği tutmaz) — kural bu yüzden YAPIDAN okunur, ekrandan değil.

## 🔴 Dört tuzak

**1. Devir ŞARTTIR.** Olmasaydı sayfa 2'de ya da ay değişince bakiye sıfırdan
başlar, anlamsız bir seri çıkardı. Devir AYNI süzgeçlerle (hesap süzgeci dahil),
pencerenin ÖNCESİNDEKİ satırlardan hesaplanır ve **AYRI bir sorgudur**: pencere
fonksiyonuna gömülseydi `LIMIT`ten etkilenirdi.

**2. Birikim ESKİDEN YENİYE, gösterim YENİDEN ESKİYE.** E8 tarih DESC'tir ama
koşan bakiye yalnız KRONOLOJİK birikimde anlamlıdır: pencere fonksiyonu ASC
koşar, yanıt DESC döner. Tek yönde yazılsaydı ya ekran ters sıralanır ya da
bakiye tersten birikirdi.

**3. Pencere fonksiyonu ALT SORGUDA, `LIMIT` DIŞTA (R9).** Ters olsaydı her
sayfa yalnız o sayfanın satırlarını toplar ve 2. sayfanın bakiyesi YALAN olurdu.

**4. 🔴 Sıralamanın son parçası `jl.id`dir (R8).** `func.now()` **işlem başına
SABİTTİR**: aynı işlemde yazılan iki fişin `created_at`i EŞİTTİR. `jl.id`
olmadan sıra Postgres'in keyfine kalır, `LIMIT/OFFSET` ile sayfalanan küme satır
**TEKRARLAR ya da ATLAR** ve koşan bakiye sayfadan sayfaya oynar. `created_at`
yalnız **ORDER BY**da kullanılır — takvim bileşeni ÇIKARILMAZ, K6 AST bekçisi
tetiklenmez.

## İşaret HAM `net`tir

Borç `+`, alacak `−`; türe göre **ÇEVRİLMEZ** (K3'ün `SIGN`ı burada
KULLANILMAZ). Hesap süzgeci OPSİYONELDİR (E8:96 `Tüm Hesaplar`) ve karışık
türlerin bulunduğu bir kümede tür-bazlı işaret tanımsız olurdu — `320` Pasif ile
`100` Aktif aynı sütunda toplanamazdı.

Hangi fişlerin deftere girdiği `balance.POSTING_STATUSES`ten okunur — 🔴 TEK
KOPYA: `draft` GİRMEZ, `reversed` GİRER (aksi hâlde çift ters kayıt, R6).

Bu modül BAŞKA BİR MODÜLÜ IMPORT ETMEZ.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import ColumnElement, Select, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import today
from app.modules.accounting.balance import POSTING_STATUSES, ZERO
from app.modules.accounting.models import (
    ChartAccount,
    JournalEntry,
    JournalEntryStatus,
    JournalLine,
)
from app.modules.accounting.schemas import LedgerResponse, LedgerRow

__all__ = ["build_ledger", "month_bounds"]


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """Ayın ilk ve son günü (sınırlar DAHİL).

    Son gün "bir sonraki ayın 1'inden bir gün önce" olarak bulunur: `month + 1`
    aritmetiği Aralık'ta yıl taşırdı ve ay uzunluklarını (28/29/30/31) elle
    bilmek gerekirdi (`invoicing/summary.current_month_bounds` deseni).
    """
    ilk = date(year, month, 1)
    sonraki_ay = (ilk + timedelta(days=32)).replace(day=1)
    return ilk, sonraki_ay - timedelta(days=1)


def default_period() -> tuple[int, int]:
    """🔴 K6 SINIR ÇAĞRISI — varsayılan dönem `timezone.today()`nin ayıdır.

    `date.today()` sunucunun yerel saatini (Railway'de UTC) okurdu; TR gecesi
    00:00-03:00 arasında bir gün geride kalır ve ayın İLK gecesinde defter bir
    ÖNCEKİ aya bakardı.
    """
    bugun = today()
    return bugun.year, bugun.month


def _net() -> ColumnElement[Decimal]:
    """Satır başına ham nicelik — `debit − credit`. TEK yazım."""
    return JournalLine.debit - JournalLine.credit


#: 🔴 Sütunlar TEK TEK ve AÇIK ADLA seçilir; `select(JournalLine, JournalEntry,
#: ChartAccount)` yazılsaydı üç tablonun `id`/`created_at`/`description`
#: sütunları ÇAKIŞIR ve SQLAlchemy onları `id_1`/`id_2` diye numaralandırırdı —
#: alt sorgudan okuma o numaralara bağlanır ve bir kolon eklendiğinde SESSİZCE
#: kayardı.
def _base(account_id: uuid.UUID | None, status: JournalEntryStatus | None) -> Select:
    """Defterin ortak `FROM` + süzgeç gövdesi.

    Devir sorgusu ile pencere sorgusu BURADAN türer: ayrı yazılsalardı biri
    hesap ya da durum süzgecini alır öteki almaz ve devir, gösterilen seriyle
    ALAKASIZ bir sayı olurdu.

    Durum süzgeci `POSTING_STATUSES` ile KESİŞTİRİLMEZ, onun İÇİNDEN seçilir:
    kullanıcı `draft` isteseydi bile defter onu göstermezdi — deftere giriş
    kuralı bir tercih değil, K2'nin mali iz kanonudur.
    """
    stmt = (
        select(
            JournalEntry.id.label("entry_id"),
            JournalEntry.entry_date.label("entry_date"),
            JournalEntry.status.label("entry_status"),
            JournalEntry.description.label("description"),
            JournalEntry.detail_note.label("detail_note"),
            JournalEntry.created_at.label("entry_created_at"),
            JournalEntry.source_type.label("source_type"),
            JournalLine.id.label("line_id"),
            JournalLine.sort_order.label("sort_order"),
            JournalLine.account_id.label("account_id"),
            JournalLine.debit.label("debit"),
            JournalLine.credit.label("credit"),
            ChartAccount.code.label("account_code"),
            ChartAccount.name.label("account_name"),
        )
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .join(ChartAccount, ChartAccount.id == JournalLine.account_id)
        .where(JournalEntry.status.in_(POSTING_STATUSES))
    )
    if account_id is not None:
        stmt = stmt.where(JournalLine.account_id == account_id)
    if status is not None:
        stmt = stmt.where(JournalEntry.status == status)
    return stmt


def _filter_only(account_id: uuid.UUID | None, status: JournalEntryStatus | None):
    """Süzgeç koşullarının listesi — sayım ve devir sorguları için."""
    kosullar = [JournalEntry.status.in_(POSTING_STATUSES)]
    if account_id is not None:
        kosullar.append(JournalLine.account_id == account_id)
    if status is not None:
        kosullar.append(JournalEntry.status == status)
    return kosullar


async def _carried_balance(
    session: AsyncSession,
    *,
    ilk_gun: date,
    account_id: uuid.UUID | None,
    status: JournalEntryStatus | None,
) -> Decimal:
    """🔴 Pencere ÖNCESİ toplam — **AYRI** sorgu (R9).

    "Önce" ölçütü `entry_date < ilk_gun`dur, dönem kolonları değil: kanonik
    sıralamanın BİRİNCİ parçası `entry_date`tir ve
    `ck_journal_entries_period_matches_date` ikisinin ayrışmasını zaten
    engeller — ama sıralama ile devir ölçütünün AYNI kolondan okunması, ileride
    dönem kolonlarına dokunulsa bile seriyi tutarlı tutar.

    🔴 `COALESCE` ŞARTTIR: satırı olmayan bir geçmişte `SUM()` **NULL** döner,
    `0` değil — devir alanı BOŞ basardı (R12). Ve yeni açılan HER hesap bu
    hâldedir, yani kusur ekranın tamamını boşaltırdı.
    """
    stmt = (
        select(func.coalesce(func.sum(_net()), literal(ZERO)))
        .select_from(JournalLine)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(*_filter_only(account_id, status), JournalEntry.entry_date < ilk_gun)
    )
    return (await session.execute(stmt)).scalar_one()


async def build_ledger(
    session: AsyncSession,
    *,
    year: int,
    month: int,
    account_id: uuid.UUID | None,
    status: JournalEntryStatus | None,
    limit: int | None,
    offset: int,
) -> LedgerResponse:
    """E8'in tablosu — üç sorgu (devir · sayım · sayfa), satır sayısından bağımsız.

    Akış:
      1. devir (ayrı sorgu, pencere ÖNCESİ);
      2. pencere fonksiyonu **ALT SORGUDA**, kanonik dört parçalı ASC sıralamayla
         ve `ROWS UNBOUNDED PRECEDING` ile — burada `LIMIT` YOKTUR;
      3. dış sorgu DESC sıralar ve `LIMIT/OFFSET` uygular.

    2 ile 3 birleştirilseydi (`LIMIT` içeride) her sayfa sıfırdan birikir ve
    2. sayfanın bakiyesi yalan olurdu.

    🔴 **`limit=None` → SAYFALAMA YOK** (Excel ucu, `audit.repository` emsali):
    ayın eşleşen TÜM satırları döner. Bu, yukarıdaki 3. maddenin doğrudan
    sonucudur — `LIMIT` zaten yalnız DIŞ sorgudadır, dolayısıyla kaldırılması
    devri ve koşan bakiyeyi etkilemez. `GET /journal` bunu ASLA göndermez
    (orada `limit` hâlâ `int`, tavan 200, aşım **422**).
    """
    ilk_gun, son_gun = month_bounds(year, month)
    carried = await _carried_balance(session, ilk_gun=ilk_gun, account_id=account_id, status=status)

    pencere = (JournalEntry.entry_date >= ilk_gun, JournalEntry.entry_date <= son_gun)

    sayim = (
        select(func.count())
        .select_from(JournalLine)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(*_filter_only(account_id, status), *pencere)
    )
    total = (await session.execute(sayim)).scalar_one()

    # 🔴 Kanonik sıralama — DÖRT parça, sonuncusu `jl.id` (R8).
    kanonik = (
        JournalEntry.entry_date.asc(),
        JournalEntry.created_at.asc(),
        JournalLine.sort_order.asc(),
        JournalLine.id.asc(),
    )
    kosan = (literal(carried) + func.sum(_net()).over(order_by=kanonik, rows=(None, 0))).label(
        "running_balance"
    )

    alt = _base(account_id, status).add_columns(kosan).where(*pencere).subquery("defter")

    # Gösterim DESC (E8 tarih DESC) — ama dört parçanın HEPSİ ters çevrilir:
    # yalnız tarih çevrilseydi aynı günün satırları içinde sıra yine keyfî
    # kalırdı ve sayfalama R8'in tam olarak kapattığı deliğe geri düşerdi.
    dis = (
        select(alt)
        .order_by(
            alt.c.entry_date.desc(),
            alt.c.entry_created_at.desc(),
            alt.c.sort_order.desc(),
            alt.c.line_id.desc(),
        )
        .offset(offset)
    )
    if limit is not None:
        # 🔴 `limit=None` = SINIR YOK (`audit.repository.list_audit_entries`
        # emsali). Burada GÜVENLİDİR ve tam da 3. tuzağın yapısından ötürü:
        # pencere fonksiyonu ALT sorgudadır ve ayın TAMAMI üzerinde koşar,
        # `LIMIT` yalnız DIŞ sorguya uygulanır. Sınır kaldırılınca alt sorgu
        # değişmez, dış sorgu tüm satırları döner — `carried_balance` ve
        # `running_balance` AYNEN doğru kalır.
        dis = dis.limit(limit)
    satirlar = (await session.execute(dis)).mappings().all()

    return LedgerResponse(
        items=[
            LedgerRow(
                entry_id=satir["entry_id"],
                entry_date=satir["entry_date"],
                entry_status=satir["entry_status"],
                account_id=satir["account_id"],
                account_code=satir["account_code"],
                account_name=satir["account_name"],
                description=satir["description"],
                detail_note=satir["detail_note"],
                debit=satir["debit"],
                credit=satir["credit"],
                running_balance=satir["running_balance"],
                source_type=satir["source_type"],
            )
            for satir in satirlar
        ],
        total=total,
        # 🔴 `limit=None` (Excel ucu) zarfa `total` olarak yazılır: sınır
        # uygulanmadığında uygulanabilecek en küçük doğru sınır kümenin kendi
        # büyüklüğüdür (`items` TAMDIR, `limit == total`). Alan `int | None`a
        # genişletilmedi çünkü `LedgerResponse` bu dilimin dosya sınırı
        # DIŞINDADIR (`schemas.py`) ve nullable bir `limit` `GET /journal`ın
        # yayımlanmış sözleşmesini değiştirirdi. Dosya ucu zarfın sayfalama
        # alanlarını okumaz; yalnız `items` kullanılır.
        limit=total if limit is None else limit,
        offset=offset,
        carried_balance=carried,
    )

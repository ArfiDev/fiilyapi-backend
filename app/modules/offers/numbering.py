"""Teklif numarasi ureticisi: `TKL-{yil}-{sira:04d}` (`TKL-2026-0014`) — TKL-B4.1, SO-7.

Emsal `accounting/numbering.py` (YEV): ORTAK mekanizma, gerekceler orada ayrintili —
buraya KOPYALANMAZ (iki kopya bir gun ayrisir). Ozet ve BU modulun farklari:

* **Sayac tablosu (`offer_counters`) + UPSERT** `ON CONFLICT (year) DO UPDATE ... RETURNING`.
  `max + 1` DEGIL: `max + 1` numarayi hayatta kalan satirlardan yeniden hesaplar ve en buyuk
  numarali TASLAK silinince numarasi YENIDEN KULLANILIR. Teklif taslagi silinebilir
  (TKL-PLAN §2.3a) ve numara olusturmada verilir; sayac monotondur, numara asla geri verilmez.
* `DO NOTHING` DEGIL: `DO NOTHING` catismada deger DONDURMEZ; `DO UPDATE` satiri KILITLER ve
  DONDURUR. Catismada ikinci istek, birincinin commit/rollback'ini satir kilidinde BEKLER.
* **Tek ifade.** Emsal iki ifadedir (`next_no` = SIRADAKI; kilitle, sonra tuket). Burada kolon
  `last_no` = DAGITILAN SON numaradir: INSERT 1 yazar, catismada `last_no + 1` yazar ve
  `RETURNING last_no` dogrudan dagitilan numaradir — emsaldeki "donen deger eksi bir" okuma
  tuzagi yoktur. Satir tek ifadede kilitlenip artirildigi icin iki ifade arasi pencere de yoktur.
* Cekirdek (ORM degil) ifade: `OfferCounter` nesnesi yuklenmez, bayat kimlik haritasi olmaz.
* Rollback sayaci GERI ALIR (cagiranin oturumunda kosar; otonom transaction yok). Bosluk
  yalniz commit edilmis bir teklifin SILINMESINDEN dogar ve bilerek kabul edilmistir.
* **9999'dan sonra numara BUDANMAZ**: `TKL-2026-10000`. `SEQUENCE_WIDTH` en az genisliktir.

## Yil (SO-7)
Yil = teklifin OLUSTURULDUGU an (Istanbul saati, `DISPLAY_TIMEZONE`), teklif tarihi DEGIL:
numara olusturmada verilir, tarih sonradan degisebilir. Cagiran yili `current_offer_year()` ile
alir; `date.today()`/`utcnow()` repoda yasaktir (AST bekcisi). `next_offer_no` yili ZORUNLU ve
ADLI parametre olarak ister — gizli bir varsayilan yoktur.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import timezone
from app.modules.offers.models import OfferCounter

__all__ = [
    "FIRST_SEQUENCE",
    "OFFER_NO_PREFIX",
    "SEQUENCE_WIDTH",
    "current_offer_year",
    "format_offer_no",
    "next_offer_no",
]

#: Seri koku — ayar ekraninda cizilmez, MODUL SABITIDIR.
OFFER_NO_PREFIX = "TKL"

#: EN AZ genislik — TAVAN DEGIL (9999'dan sonra bes haneye uzar).
SEQUENCE_WIDTH = 4

#: Yilin ilk teklifinin sirasi; sayac her 1 Ocak'ta buraya doner.
FIRST_SEQUENCE = 1

_COUNTERS = OfferCounter.__table__


def current_offer_year() -> int:
    """Olusturma anindaki Istanbul takvim yili (SO-7) — gun siniri TEK kaynaktan
    (`app.core.timezone.today`; yerel takvim bekcisi `tests/test_local_calendar_guard.py`)."""
    return timezone.today().year


def format_offer_no(year: int, sequence: int) -> str:
    """`(2026, 14) -> "TKL-2026-0014"` · `(2026, 10000) -> "TKL-2026-10000"`."""
    return f"{OFFER_NO_PREFIX}-{year}-{sequence:0{SEQUENCE_WIDTH}d}"


async def next_offer_no(session: AsyncSession, *, year: int) -> str:
    """Sayaci ATOMIK ilerletir ve dagitilan numarayi dondurur (tek UPSERT ifadesi)."""
    sequence = await session.scalar(
        pg_insert(_COUNTERS)
        .values(year=year, last_no=FIRST_SEQUENCE)
        .on_conflict_do_update(
            index_elements=["year"],
            set_={"last_no": _COUNTERS.c.last_no + 1},
        )
        .returning(_COUNTERS.c.last_no)
    )
    if sequence is None:  # pragma: no cover - RETURNING her zaman satir doner
        raise RuntimeError("Teklif numarası sayacı değer döndürmedi")
    return format_offer_no(year, sequence)

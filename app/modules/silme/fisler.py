"""Silinecek muhasebe fişlerinin dökümü (SIL-B2): önizleme + denetim satırı AYNI okumayı kullanır.

Fiş, ağaca `accounting/silme_kaydi.py` kancasıyla (kaynak belge → fiş) ve `reversal_of_id` FK'siyle
girer; burada yalnız AĞAÇTAKİ fişler okunur. Kapalı dönem bilgisi (`accounting_periods.status`)
silmeyi ENGELLEMEZ, yalnız önizlemede ve denetimde görünür (K2: kapalı dönem durdurmaz).
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Base
from app.core.silme.cozucu import SilmeAgaci, ornekler, pk_in
from app.core.silme.etiketler import tablo_bilgisi
from app.modules.accounting.models import (
    AccountingPeriod,
    AccountingPeriodStatus,
    JournalEntry,
)
from app.modules.accounting.silme_kaydi import FIS_KAYNAKLARI

TABLO = "journal_entries"


@dataclass(frozen=True)
class FisSatiri:
    entry_no: str
    entry_date: date
    status: str
    is_reversal: bool
    source_type: str | None
    total: Decimal
    period_closed: bool


async def fis_dokumu(session: AsyncSession, agac: SilmeAgaci) -> list[FisSatiri]:
    """Ağaçtaki TÜM fişler (kök fiş dahil), fiş no artan. Fişsiz ağaçta boş liste."""
    idler = agac.kayitlar.get(TABLO)
    if not idler:
        return []
    fis = JournalEntry.__table__
    sorgu = (
        select(
            JournalEntry.entry_no,
            JournalEntry.entry_date,
            JournalEntry.status,
            JournalEntry.reversal_of_id,
            JournalEntry.source_type,
            JournalEntry.total_debit,
            AccountingPeriod.status.label("donem_durumu"),
        )
        .select_from(
            JournalEntry.__table__.outerjoin(
                AccountingPeriod.__table__,
                and_(
                    AccountingPeriod.year == JournalEntry.period_year,
                    AccountingPeriod.month == JournalEntry.period_month,
                ),
            )
        )
        .where(pk_in(fis, sorted(idler, key=str)))
        .order_by(JournalEntry.entry_no)
    )
    return [
        FisSatiri(
            entry_no=satir.entry_no,
            entry_date=satir.entry_date,
            status=satir.status.value,
            is_reversal=satir.reversal_of_id is not None,
            source_type=satir.source_type.value if satir.source_type is not None else None,
            total=satir.total_debit,
            period_closed=satir.donem_durumu is AccountingPeriodStatus.closed,
        )
        for satir in (await session.execute(sorgu)).all()
    ]


@dataclass(frozen=True)
class FissizKalanBelge:
    """Fişi silinen ama KENDİSİ ağaçta olmayan kaynak belge (yalnız kök fiş silmede doğar)."""

    table: str
    label: str
    ref: str

    @property
    def message(self) -> str:
        return f"Kaynak belge fişsiz kalacak: {self.label} {self.ref}".rstrip()


async def fissiz_kalan_belgeler(session: AsyncSession, agac: SilmeAgaci) -> list[FissizKalanBelge]:
    """Ağaçtaki fişlerin kaynak belgelerinden AĞAÇTA OLMAYANLAR: belge kalır, fişi gider.

    Bilinçli karar: mizan fişlerden hesaplanır; belge ile fiş arasındaki bu tutarsızlık
    kök fiş silinirse bilerek kabul edilir ve önizlemede AÇIKÇA gösterilir.
    """
    idler = agac.kayitlar.get(TABLO)
    if not idler:
        return []
    fis = JournalEntry.__table__
    sorgu = select(JournalEntry.source_type, JournalEntry.source_id).where(
        pk_in(fis, sorted(idler, key=str)), JournalEntry.source_id.is_not(None)
    )
    kaynaklar: dict[str, set[uuid.UUID]] = {}
    for kaynak_turu, kaynak_id in (await session.execute(sorgu)).all():
        tablo = FIS_KAYNAKLARI[kaynak_turu.value]
        if (kaynak_id,) not in agac.kayitlar.get(tablo, set()):
            kaynaklar.setdefault(tablo, set()).add(kaynak_id)
    sonuc: list[FissizKalanBelge] = []
    for tablo in sorted(kaynaklar):
        etiket = tablo_bilgisi(tablo).etiket
        for kaynak_id in sorted(kaynaklar[tablo], key=str):
            ref = await ornekler(session, Base.metadata, tablo, {(kaynak_id,)})
            sonuc.append(FissizKalanBelge(tablo, etiket, ref[0] if ref else ""))
    return sonuc

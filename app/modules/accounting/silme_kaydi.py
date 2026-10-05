"""Muhasebe fişinin silme motoru kancası (SIL-B1): `journal_entries.source_type/source_id`.

Fişi doğuran belge bir FK ile bağlı DEĞİLDİR (çok biçimli referans), bu yüzden motor onu
metadata'dan bulamaz. Her kaynak türü için bir kanca kaydedilir: belge ağaçta ise fişi de
ağaçtadır (`linked`). Fişin satırları (`journal_lines`) ve stornosu (`reversal_of_id`) FK ile
zaten bağlıdır ve motor onları kendiliğinden bulur.

Fişler `is_financial` olarak görünür ve kaynak belgeyle BİRLİKTE silinir (SIL-B2, K2): fişin
stornosu (`reversal_of_id`) ve satırları ağaçtadır, kapalı dönem silmeyi durdurmaz.

Bu dosya ayrıca `journal_entry` türünü kaydeder: kök fiş, stornosu ve satırlarıyla silinir.
"""

import uuid

from sqlalchemy import ColumnElement, String, Table, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.silme.graf import FkDisiBag, kanca_kaydet
from app.core.silme.turler import KokBilgisi, SilmeTuru, tur_kaydet
from app.modules.accounting import guards
from app.modules.accounting.models import JournalEntry
from app.modules.audit import messages

#: `JournalSourceType` üyesi → fişin kaynağı olan tablo (`models.JournalSourceType` docstring'i).
FIS_KAYNAKLARI: dict[str, str] = {
    "invoice": "invoices",
    "payment": "payments",
    "payroll_period": "payroll_periods",
    "progress_payment": "progress_payments",
    "subcontractor_progress_payment": "subcontractor_progress_payments",
    "equipment_rental_invoice": "equipment_rental_invoices",
    "financial_instrument": "financial_instruments",
}


def _kosul(kaynak_turu: str):  # type: ignore[no-untyped-def]
    def kosul(alt: Table, ust: Table) -> ColumnElement[bool]:
        return and_(alt.c.source_type.cast(String) == kaynak_turu, alt.c.source_id == ust.c.id)

    return kosul


for _tur, _tablo in FIS_KAYNAKLARI.items():
    kanca_kaydet(
        FkDisiBag(
            ad=f"journal_entries.source:{_tur}",
            ust_tablo=_tablo,
            alt_tablo="journal_entries",
            kosul=_kosul(_tur),
        )
    )


def _storno_orijinali(alt: Table, ust: Table) -> ColumnElement[bool]:
    """Storno fişi → stornosu OLDUĞU orijinal fiş (`reversal_of_id`, FK ters yönü)."""
    return alt.c.id == ust.c.reversal_of_id


# Orijinal → storno yönü FK ile (RESTRICT) zaten ağaçtadır. Ters yön: kök bir STORNO ise orijinali
# de gider; stornosu silinen orijinal `reversed` damgasıyla kalsaydı mizan ile durum ayrışırdı.
kanca_kaydet(
    FkDisiBag(
        ad="journal_entries.reversal_original",
        ust_tablo="journal_entries",
        alt_tablo="journal_entries",
        kosul=_storno_orijinali,
        sirayi_etkilemez=True,
    )
)


async def _fis_oku(session: AsyncSession, entry_id: uuid.UUID) -> KokBilgisi | None:
    entry = await session.get(JournalEntry, entry_id)
    if entry is None:
        return None
    return KokBilgisi(
        ad=f"{entry.entry_no} · {entry.entry_date}",
        denetim_metni=messages.journal_entry_deleted(entry.entry_date, entry.description),
    )


tur_kaydet(
    SilmeTuru(
        anahtar="journal_entry",
        tablo="journal_entries",
        etiket="Yevmiye fişi",
        bulunamadi=guards.JOURNAL_ENTRY_MISSING,
        kok_oku=_fis_oku,
    )
)

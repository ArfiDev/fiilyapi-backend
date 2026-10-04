"""Muhasebe fişinin silme motoru kancası (SIL-B1): `journal_entries.source_type/source_id`.

Fişi doğuran belge bir FK ile bağlı DEĞİLDİR (çok biçimli referans), bu yüzden motor onu
metadata'dan bulamaz. Her kaynak türü için bir kanca kaydedilir: belge ağaçta ise fişi de
ağaçtadır (`linked`). Fişin satırları (`journal_lines`) ve stornosu (`reversal_of_id`) FK ile
zaten bağlıdır ve motor onları kendiliğinden bulur.

Fişler `is_financial` olarak görünür; SIL-B2'ye kadar mali bağı olan kayıt SİLİNMEZ
(`DeleteFinancialPendingError`). Kancanın işi önizlemeyi TAM göstermektir.
"""

from sqlalchemy import ColumnElement, String, Table, and_

from app.core.silme.graf import FkDisiBag, kanca_kaydet

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

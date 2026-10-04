"""Onay zincirinin silme motoru kancası (SIL-B1): `approval_chains.document_type/document_id`.

`document_id` çok biçimli bir referanstır (FK YOK, bkz. `models.py` docstring'i). Evrak silinirse
zinciri ve adımları (`approval_steps`, FK CASCADE) yetim kalmasın diye birlikte gider.
"""

from sqlalchemy import ColumnElement, String, Table, and_

from app.core.silme.graf import FkDisiBag, kanca_kaydet

#: `ApprovalDocumentType` üyesi → evrak tablosu.
ZINCIR_EVRAKLARI: dict[str, str] = {
    "subcontractor_progress_payment": "subcontractor_progress_payments",
    "purchase_request": "purchase_requests",
    "progress_payment": "progress_payments",
}


def _kosul(evrak_turu: str):  # type: ignore[no-untyped-def]
    def kosul(alt: Table, ust: Table) -> ColumnElement[bool]:
        return and_(alt.c.document_type.cast(String) == evrak_turu, alt.c.document_id == ust.c.id)

    return kosul


for _tur, _tablo in ZINCIR_EVRAKLARI.items():
    kanca_kaydet(
        FkDisiBag(
            ad=f"approval_chains.document:{_tur}",
            ust_tablo=_tablo,
            alt_tablo="approval_chains",
            kosul=_kosul(_tur),
        )
    )

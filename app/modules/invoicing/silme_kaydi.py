"""Faturanın silme motoru kaydı (SIL-B2); `silme/kayitlar.py` ithal eder.

Fatura silinince kalemleri (CASCADE), ödemeleri (RESTRICT → birlikte), onun fişleri ve stornoları
ağaçtadır. "Yalnız taslak fatura silinir" kuralı yalnız bu motor yolunda atlanır.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.silme.turler import KokBilgisi, SilmeTuru, tur_kaydet
from app.modules.audit import messages
from app.modules.invoicing import guards
from app.modules.invoicing.models import Invoice


async def _fatura_oku(session: AsyncSession, invoice_id: uuid.UUID) -> KokBilgisi | None:
    invoice = await session.get(Invoice, invoice_id)
    if invoice is None:
        return None
    return KokBilgisi(
        ad=invoice.invoice_no, denetim_metni=messages.invoice_deleted(invoice.invoice_no)
    )


tur_kaydet(
    SilmeTuru(
        anahtar="invoice",
        tablo="invoices",
        etiket="Fatura",
        bulunamadi=guards.INVOICE_MISSING,
        kok_oku=_fatura_oku,
    )
)

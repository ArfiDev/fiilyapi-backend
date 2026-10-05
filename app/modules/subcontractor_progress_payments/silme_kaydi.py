"""Taşeron hakedişinin silme motoru kaydı (SIL-B2); `silme/kayitlar.py` ithal eder.

İşveren hakedişi kaydıyla aynı kural: satırlar, onay zinciri, hakediş faturası ve fişi ağaçtadır;
onaylı/ödenmiş hakediş de silinir (iş kuralı 409'u yalnız bu yolda atlanır).
"""

import uuid

from sqlalchemy import ColumnElement, String, Table, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.silme.etiketler import ONAYLI_DURUMLAR
from app.core.silme.graf import FkDisiBag, kanca_kaydet
from app.core.silme.turler import KokBilgisi, SilmeTuru, tur_kaydet
from app.modules.audit import messages
from app.modules.contracts.models import SubcontractorContract
from app.modules.projects.models import Project
from app.modules.subcontractor_progress_payments import guards, service
from app.modules.subcontractor_progress_payments.models import SubcontractorProgressPayment


async def _hakedis_oku(session: AsyncSession, payment_id: uuid.UUID) -> KokBilgisi | None:
    payment = await session.get(SubcontractorProgressPayment, payment_id)
    if payment is None:
        return None
    # Satır toplamı için ŞART: oturumdaki nesnenin satırları yüklenmemiş olabilir (async'te lazy
    # yükleme patlar).
    await session.refresh(payment, attribute_names=["lines"])
    project = await session.get(Project, payment.project_id)
    contract = await session.get(SubcontractorContract, payment.contract_id)
    ozet = service.deletion_summary(
        payment,
        project.name if project is not None else "",
        contract.subcontractor_name if contract is not None else None,
    )
    return KokBilgisi(
        ad=f"{ozet.project_name} · #{ozet.sequence_no}",
        denetim_metni=messages.subcontractor_progress_payment_deleted(
            ozet.project_name,
            ozet.subcontractor_name,
            ozet.sequence_no,
            ozet.status_label,
            ozet.amount,
        ),
    )


tur_kaydet(
    SilmeTuru(
        anahtar="subcontractor_progress_payment",
        tablo="subcontractor_progress_payments",
        etiket="Taşeron hakedişi",
        bulunamadi=guards.PAYMENT_MISSING,
        kok_oku=_hakedis_oku,
    )
)


def _satir_onayli_baslik(alt: Table, ust: Table) -> ColumnElement[bool]:
    """Satır → ÜSTBİLGİ, yalnız başlık onaylı/ödenmişse (işveren hakedişiyle aynı kural)."""
    return and_(
        alt.c.id == ust.c.payment_id,
        alt.c.status.cast(String).in_(ONAYLI_DURUMLAR),
    )


kanca_kaydet(
    FkDisiBag(
        ad="subcontractor_progress_payments.lines_header",
        ust_tablo="subcontractor_progress_payment_lines",
        alt_tablo="subcontractor_progress_payments",
        kosul=_satir_onayli_baslik,
        sirayi_etkilemez=True,
    )
)

"""İşveren hakedişinin silme motoru kaydı (SIL-B2); `silme/kayitlar.py` ithal eder.

Hakedişin satırları (CASCADE), onay zinciri (kanca), hakediş faturası (RESTRICT) ve fişi (kanca)
ağaçtadır. Onaylı/ödenmiş hakediş de silinir: "yalnız taslak" kuralı SADECE bu motor yolunda
atlanır, durum geçişi ve diğer yazma yolları aynen kalır.
"""

import uuid

from sqlalchemy import ColumnElement, String, Table, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.silme.etiketler import ONAYLI_DURUMLAR
from app.core.silme.graf import FkDisiBag, kanca_kaydet
from app.core.silme.turler import KokBilgisi, SilmeTuru, tur_kaydet
from app.modules.audit import messages
from app.modules.progress_payments import guards, service
from app.modules.progress_payments.models import ProgressPayment
from app.modules.projects.models import Project


async def _hakedis_oku(session: AsyncSession, payment_id: uuid.UUID) -> KokBilgisi | None:
    payment = await session.get(ProgressPayment, payment_id)
    if payment is None:
        return None
    # Satır toplamı için ŞART: oturumdaki nesnenin satırları yüklenmemiş olabilir (async'te lazy
    # yükleme patlar).
    await session.refresh(payment, attribute_names=["lines"])
    project = await session.get(Project, payment.project_id)
    ozet = service.deletion_summary(payment, project.name if project is not None else "")
    return KokBilgisi(
        ad=f"{ozet.project_name} · #{ozet.sequence_no}",
        denetim_metni=messages.progress_payment_deleted(
            ozet.project_name, ozet.sequence_no, ozet.status_label, ozet.amount
        ),
    )


tur_kaydet(
    SilmeTuru(
        anahtar="progress_payment",
        tablo="progress_payments",
        etiket="İşveren hakedişi",
        bulunamadi=guards.PAYMENT_MISSING,
        kok_oku=_hakedis_oku,
    )
)


def _satir_baslik(alt: Table, ust: Table) -> ColumnElement[bool]:
    """Satır → ÜSTBİLGİ, DURUMDAN BAĞIMSIZ (silme yolu başlığı bununla `FOR UPDATE` kilitler)."""
    return alt.c.id == ust.c.payment_id


def _satir_onayli_baslik(alt: Table, ust: Table) -> ColumnElement[bool]:
    """Satır → ÜSTBİLGİ, yalnız başlık onaylı/ödenmişse (muhasebeleşmiş)."""
    return and_(
        alt.c.id == ust.c.payment_id,
        alt.c.status.cast(String).in_(ONAYLI_DURUMLAR),
    )


# Şantiye/bölüm silinince bir hakedişin yalnız BAZI satırları gidebilir. Hakediş onaylı/ödenmişse
# fişi ve faturası eski toplamı taşır ve bayat kalırdı: başlık, fişi/faturası/ödemeleriyle birlikte
# silinir (GECE KARARI; önizlemede görünür). Taslak/onay bekleyen hakedişte toplam satırlardan
# türer, başlık kalır. Başlık durumu YARIŞTA değişebilir (onay): silme yolu başlığı durumdan
# bağımsız kilitler ve kararı kilit altında yeniden verir (`kilit_kosulu`).
kanca_kaydet(
    FkDisiBag(
        ad="progress_payments.lines_header",
        ust_tablo="progress_payment_lines",
        alt_tablo="progress_payments",
        kosul=_satir_onayli_baslik,
        sirayi_etkilemez=True,
        kilit_kosulu=_satir_baslik,
    )
)

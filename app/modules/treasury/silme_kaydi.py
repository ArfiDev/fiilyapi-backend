"""Ödeme ve çek/senedin silme motoru kaydı (SIL-B2); `silme/kayitlar.py` ithal eder.

* `payment`: ödemenin fişi (kanca) ve stornosu ağaçtadır. Ödeme PORTFÖY DIŞI (tahsil/ödendi/iade)
  bir çek/senede bağlıysa o çek ve fişi de ağaca girer (çekin fişi ödemenin `101` bacağını
  boşalttığı için ayrı kalamaz); portföydeki çek KALIR (ödemesiz portföy çeki tutarlı bir durumdur).
* `financial_instrument`: çekin fişi ve çeke bağlı TÜM ödemeler ağaçtadır.

## Silme sonrası (`sonrasi` kancası)

Ağaçtaki ödemelerin faturası ağaçta DEĞİLSE faturanın `collected` damgası kalan ödemelerden
yeniden türetilir (`payments_service._rederive_status`: `delete_payment` ile AYNI kod). Ödemenin
faturasına bağlı kaynak hakediş / kira hakedişi `paid` ise dayanağı (nakde geçmiş ödeme) gittiği
için `approved`a geri alınır: `paid` damgası ödemeye dayanır, boş bir "Ödendi" rozeti kalmaz.
"""

import uuid

from sqlalchemy import ColumnElement, String, Table, and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Base
from app.core.silme.cozucu import SilmeAgaci, kolon_in
from app.core.silme.graf import FkDisiBag, kanca_kaydet
from app.core.silme.sonrasi import Sonra, SonrasiKancasi, sonrasi_kaydet
from app.core.silme.turler import KokBilgisi, SilmeTuru, tur_kaydet
from app.modules.audit import messages
from app.modules.invoicing.models import Invoice
from app.modules.treasury import payments_service, repository
from app.modules.treasury.instruments import guards as instrument_guards
from app.modules.treasury.models import BankAccount, FinancialInstrument, Payment


async def _odeme_oku(session: AsyncSession, payment_id: uuid.UUID) -> KokBilgisi | None:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        return None
    invoice = await session.get(Invoice, payment.invoice_id)
    hesap = await session.get(BankAccount, payment.bank_account_id)
    fatura_no = invoice.invoice_no if invoice is not None else ""
    banka = hesap.bank_name if hesap is not None else ""
    gorunen = hesap.display_name if hesap is not None else None
    return KokBilgisi(
        ad=f"{fatura_no} · {payment.paid_on}",
        denetim_metni=messages.payment_deleted(fatura_no, banka, gorunen),
    )


async def _cek_oku(session: AsyncSession, instrument_id: uuid.UUID) -> KokBilgisi | None:
    instrument = await session.get(FinancialInstrument, instrument_id)
    if instrument is None:
        return None
    return KokBilgisi(
        ad=f"{instrument.serial_no} · {instrument.drawer_name}",
        denetim_metni=messages.financial_instrument_deleted(
            instrument.serial_no, instrument.drawer_name
        ),
    )


tur_kaydet(
    SilmeTuru(
        anahtar="payment",
        tablo="payments",
        etiket="Ödeme / tahsilat",
        bulunamadi=payments_service.PAYMENT_MISSING,
        kok_oku=_odeme_oku,
    )
)
tur_kaydet(
    SilmeTuru(
        anahtar="financial_instrument",
        tablo="financial_instruments",
        etiket="Çek / senet",
        bulunamadi=instrument_guards.INSTRUMENT_MISSING,
        kok_oku=_cek_oku,
    )
)


# --- Çek ↔ ödeme kancaları ---


def _odeme_portfoy_disi_cek(alt: Table, ust: Table) -> ColumnElement[bool]:
    """Ödeme → bağlı çek/senet, YALNIZ evrak portföy dışındaysa (tahsil/ödendi/iade/iptal)."""
    return and_(
        alt.c.id == ust.c.financial_instrument_id,
        alt.c.status.cast(String) != "portfolio",
    )


def _cek_odemeleri(alt: Table, ust: Table) -> ColumnElement[bool]:
    """Çek/senet → ona bağlı TÜM ödemeler (durumdan bağımsız)."""
    return alt.c.financial_instrument_id == ust.c.id


kanca_kaydet(
    FkDisiBag(
        ad="financial_instruments.payment_linked_non_portfolio",
        ust_tablo="payments",
        alt_tablo="financial_instruments",
        kosul=_odeme_portfoy_disi_cek,
    )
)
kanca_kaydet(
    FkDisiBag(
        ad="payments.instrument_payments",
        ust_tablo="financial_instruments",
        alt_tablo="payments",
        kosul=_cek_odemeleri,
        sirayi_etkilemez=True,
    )
)

# --- Silme sonrası: fatura durumu + `paid` hakediş ---

#: Fatura kolonu → kaynak tablosu (`realized.SOURCE_DIRECTION` anahtarlarıyla aynı üç kaynak).
_FATURA_KAYNAKLARI: dict[str, str] = {
    "progress_payment_id": "progress_payments",
    "subcontractor_progress_payment_id": "subcontractor_progress_payments",
    "equipment_rental_invoice_id": "equipment_rental_invoices",
}


async def _odeme_sonrasi_hazirla(session: AsyncSession, agac: SilmeAgaci) -> Sonra | None:
    odeme_idler = [pk[0] for pk in agac.kayitlar.get("payments", set())]
    fatura_idler = set(
        (
            await session.execute(
                select(Payment.invoice_id).where(kolon_in(Payment.id, odeme_idler))
            )
        ).scalars()
    )
    if not fatura_idler:
        return None
    kalan_faturalar = fatura_idler - {pk[0] for pk in agac.kayitlar.get("invoices", set())}
    kaynaklar: dict[str, set[uuid.UUID]] = {}
    for kolon, tablo in _FATURA_KAYNAKLARI.items():
        sorgu = select(getattr(Invoice, kolon)).where(
            kolon_in(Invoice.id, list(fatura_idler)), getattr(Invoice, kolon).is_not(None)
        )
        bulunan = set((await session.execute(sorgu)).scalars())
        bulunan -= {pk[0] for pk in agac.kayitlar.get(tablo, set())}
        if bulunan:
            kaynaklar[tablo] = bulunan

    async def sonra(oturum: AsyncSession) -> None:
        for fatura_id in sorted(kalan_faturalar, key=str):
            fatura = await oturum.get(
                Invoice, fatura_id, with_for_update=True, populate_existing=True
            )
            if fatura is None:
                continue
            payments_service._rederive_status(  # noqa: SLF001
                fatura, await repository.paid_total_for_invoice(oturum, fatura_id)
            )
        await oturum.flush()
        for tablo, idler in kaynaklar.items():
            t = Base.metadata.tables[tablo]
            await oturum.execute(
                update(t)
                .where(kolon_in(t.c.id, list(idler)), t.c.status.cast(String) == "paid")
                .values(status="approved", paid_at=None)
            )

    return sonra


sonrasi_kaydet(SonrasiKancasi(ad="payments.sonrasi", tablo="payments", once=_odeme_sonrasi_hazirla))

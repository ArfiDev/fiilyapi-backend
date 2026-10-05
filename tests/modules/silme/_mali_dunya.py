"""SIL-B2 test dünyası: mali aile fabrikaları (fatura, ödeme, çek, fiş + storno) + MİZAN BEKÇİSİ.

Fişler servis üzerinden DEĞİL doğrudan satır olarak yazılır: motor fişi `source_type/source_id`
kancasıyla bulur; testin konusu fişleme kuralları değil, silme sonrası defter tutarlılığıdır.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select, text

from app.modules.accounting.models import (
    ChartAccount,
    ChartAccountType,
    JournalEntry,
    JournalEntryStatus,
    JournalLine,
    JournalSourceType,
)
from app.modules.accounting.silme_kaydi import FIS_KAYNAKLARI
from app.modules.invoicing.models import (
    Invoice,
    InvoiceDirection,
    InvoiceDocumentType,
    InvoiceLine,
    InvoiceStatus,
)
from app.modules.treasury.models import (
    BankAccount,
    BankAccountType,
    FinancialInstrument,
    FinancialInstrumentDirection,
    FinancialInstrumentKind,
    FinancialInstrumentStatus,
    Payment,
    PaymentMethodKind,
)

_sayac = {"fis": 0, "fatura": 0}


@dataclass
class Hesaplar:
    alacak: ChartAccount  # 120 (aktif)
    gelir: ChartAccount  # 600 (gelir)


async def hesaplar(session) -> Hesaplar:
    async def _al(kod: str, ad: str, tur: ChartAccountType) -> ChartAccount:
        var = (
            await session.execute(select(ChartAccount).where(ChartAccount.code == kod))
        ).scalar_one_or_none()
        if var is not None:
            return var
        yeni = ChartAccount(code=kod, name=ad, account_type=tur)
        session.add(yeni)
        await session.flush()
        return yeni

    return Hesaplar(
        await _al("120", "Alıcılar", ChartAccountType.asset),
        await _al("600", "Satış Gelirleri", ChartAccountType.revenue),
    )


async def fis(
    session,
    user,
    hs: Hesaplar,
    *,
    kaynak: tuple[JournalSourceType, uuid.UUID] | None = None,
    tarih: date = date(2026, 3, 10),
    tutar: Decimal = Decimal("1000.00"),
    durum: JournalEntryStatus = JournalEntryStatus.posted,
    storno_of: JournalEntry | None = None,
) -> JournalEntry:
    """Dengeli bir fiş (borç 120 / alacak 600; storno'da ters)."""
    _sayac["fis"] += 1
    kayit = JournalEntry(
        entry_no=f"YEV-{tarih.year}-{9000 + _sayac['fis']:04d}",
        entry_date=tarih,
        period_year=tarih.year,
        period_month=tarih.month,
        description="Storno" if storno_of is not None else "Test fişi",
        status=durum,
        total_debit=tutar,
        total_credit=tutar,
        reversal_of_id=storno_of.id if storno_of is not None else None,
        source_type=kaynak[0] if kaynak else None,
        source_id=kaynak[1] if kaynak else None,
        created_by_id=user.id,
    )
    session.add(kayit)
    await session.flush()
    borc, alacak = (hs.gelir, hs.alacak) if storno_of is not None else (hs.alacak, hs.gelir)
    session.add_all(
        [
            JournalLine(entry_id=kayit.id, sort_order=0, account_id=borc.id, debit=tutar),
            JournalLine(entry_id=kayit.id, sort_order=1, account_id=alacak.id, credit=tutar),
        ]
    )
    await session.flush()
    return kayit


async def stornolu_fis(
    session,
    user,
    hs: Hesaplar,
    kaynak: tuple[JournalSourceType, uuid.UUID],
    *,
    tarih: date = date(2026, 3, 10),
    tutar: Decimal = Decimal("1000.00"),
) -> tuple[JournalEntry, JournalEntry]:
    """Orijinal (`reversed`, kaynaklı) + storno (kaynaksız, `reversal_of_id` dolu)."""
    orijinal = await fis(
        session, user, hs, kaynak=kaynak, tarih=tarih, tutar=tutar,
        durum=JournalEntryStatus.reversed,
    )  # fmt: skip
    storno = await fis(session, user, hs, tarih=tarih, tutar=tutar, storno_of=orijinal)
    return orijinal, storno


async def fatura(
    session,
    user,
    *,
    proje=None,
    site=None,
    yon: InvoiceDirection = InvoiceDirection.outgoing,
    durum: InvoiceStatus = InvoiceStatus.sent,
    toplam: Decimal = Decimal("1000.00"),
    hakedis=None,
    tasaron_hakedisi=None,
) -> Invoice:
    _sayac["fatura"] += 1
    kayit = Invoice(
        direction=yon,
        invoice_no=f"F-{_sayac['fatura']:04d}",
        document_type=InvoiceDocumentType.earchive,
        status=durum,
        issue_date=date(2026, 3, 5),
        party_name="Test Taraf",
        project_id=proje.id if proje is not None else None,
        site_id=site.id if site is not None else None,
        progress_payment_id=hakedis.id if hakedis is not None else None,
        subcontractor_progress_payment_id=(
            tasaron_hakedisi.id if tasaron_hakedisi is not None else None
        ),
        subtotal=toplam,
        tax_base=toplam,
        vat_amount=Decimal("0.00"),
        total=toplam,
        created_by_id=user.id,
    )
    session.add(kayit)
    await session.flush()
    session.add(
        InvoiceLine(
            invoice_id=kayit.id,
            sort_order=0,
            description="Kalem",
            quantity=Decimal("1"),
            unit_price=toplam,
            vat_rate=Decimal("0"),
            line_total=toplam,
        )
    )
    await session.flush()
    return kayit


async def banka(session) -> BankAccount:
    var = (await session.execute(select(BankAccount))).scalars().first()
    if var is not None:
        return var
    kayit = BankAccount(
        bank_name="Test Bankası", account_type=BankAccountType.checking, display_name="Vadesiz"
    )
    session.add(kayit)
    await session.flush()
    return kayit


async def cek(
    session,
    user,
    *,
    durum: FinancialInstrumentStatus = FinancialInstrumentStatus.portfolio,
    no: str = "CEK-1",
) -> FinancialInstrument:
    kayit = FinancialInstrument(
        instrument_kind=FinancialInstrumentKind.cheque,
        direction=FinancialInstrumentDirection.received,
        serial_no=no,
        drawer_name="Keşideci A.Ş.",
        issue_date=date(2026, 3, 1),
        due_date=date(2026, 4, 1),
        amount=Decimal("500.00"),
        status=durum,
    )
    session.add(kayit)
    await session.flush()
    return kayit


async def odeme(
    session,
    user,
    fatura_: Invoice,
    *,
    tutar: Decimal = Decimal("500.00"),
    cek_: FinancialInstrument | None = None,
) -> Payment:
    hesap = await banka(session)
    kayit = Payment(
        invoice_id=fatura_.id,
        bank_account_id=hesap.id,
        method=PaymentMethodKind.cheque if cek_ is not None else PaymentMethodKind.transfer,
        financial_instrument_id=cek_.id if cek_ is not None else None,
        amount=tutar,
        paid_on=date(2026, 3, 20),
        created_by_id=user.id,
    )
    session.add(kayit)
    await session.flush()
    return kayit


# --- MİZAN BEKÇİSİ ---


async def mizan(session) -> dict[str, tuple[Decimal, Decimal]]:
    """Hesap kodu → (Σ borç, Σ alacak): defterin (fiş satırları) kendisinden yeniden hesaplanır."""
    satirlar = await session.execute(
        text(
            "SELECT a.code, COALESCE(SUM(l.debit),0), COALESCE(SUM(l.credit),0) "
            "FROM chart_of_accounts a JOIN journal_lines l ON l.account_id = a.id "
            "GROUP BY a.code ORDER BY a.code"
        )
    )
    return {kod: (borc, alacak) for kod, borc, alacak in satirlar.all()}


def mizan_cikar(
    once: dict[str, tuple[Decimal, Decimal]], silinen: list[tuple[str, Decimal, Decimal]]
) -> dict[str, tuple[Decimal, Decimal]]:
    """`once` mizanından silinen fiş satırlarını (kod, borç, alacak) düşer; sıfırlananı atar."""
    sonuc = dict(once)
    for kod, borc, alacak in silinen:
        b, a = sonuc.get(kod, (Decimal(0), Decimal(0)))
        sonuc[kod] = (b - borc, a - alacak)
    return {k: v for k, v in sonuc.items() if v != (Decimal(0), Decimal(0))}


async def fis_satirlari(session, fis_idleri: list[uuid.UUID]) -> list[tuple[str, Decimal, Decimal]]:
    sorgu = text(
        "SELECT a.code, l.debit, l.credit FROM journal_lines l "
        "JOIN chart_of_accounts a ON a.id = l.account_id WHERE l.entry_id = ANY(:idler)"
    ).bindparams(idler=fis_idleri)
    return [(k, b, a) for k, b, a in (await session.execute(sorgu)).all()]


async def tutarsizliklar(session) -> dict[str, int]:
    """Defter bekçisi: hepsi 0 olmalı.

    * `yetim_fis`: `source_id` karşılıksız (kaynak belge tablosunda yok);
    * `yetim_storno`: `reversal_of_id` orijinali yok (FK engeller, yine de sayılır);
    * `stornosuz_reversed`: `reversed` fişin stornosu yok;
    * `satirsiz_fis`: satırı olmayan fiş; `dengesiz`: Σ borç ≠ Σ alacak (satır düzeyinde).
    """
    sayilar: dict[str, int] = {}
    yetim = 0
    for tablo in set(FIS_KAYNAKLARI.values()):
        kaynak_turleri = [t for t, v in FIS_KAYNAKLARI.items() if v == tablo]
        sorgu = text(
            f"SELECT count(*) FROM journal_entries e WHERE e.source_type::text = ANY(:turler) "
            f'AND NOT EXISTS (SELECT 1 FROM "{tablo}" k WHERE k.id = e.source_id)'
        ).bindparams(turler=kaynak_turleri)
        yetim += int((await session.execute(sorgu)).scalar_one())
    sayilar["yetim_fis"] = yetim
    sayilar["yetim_storno"] = int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM journal_entries s WHERE s.reversal_of_id IS NOT NULL "
                    "AND NOT EXISTS (SELECT 1 FROM journal_entries o WHERE o.id = s.reversal_of_id)"
                )
            )
        ).scalar_one()
    )
    sayilar["stornosuz_reversed"] = int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM journal_entries o WHERE o.status::text = 'reversed' "
                    "AND NOT EXISTS (SELECT 1 FROM journal_entries s WHERE s.reversal_of_id = o.id)"
                )
            )
        ).scalar_one()
    )
    sayilar["satirsiz_fis"] = int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM journal_entries e WHERE NOT EXISTS "
                    "(SELECT 1 FROM journal_lines l WHERE l.entry_id = e.id)"
                )
            )
        ).scalar_one()
    )
    sayilar["dengesiz"] = int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM (SELECT entry_id FROM journal_lines GROUP BY entry_id "
                    "HAVING SUM(debit) <> SUM(credit)) x"
                )
            )
        ).scalar_one()
    )
    return sayilar


# --- Sayım / denetim yardımcıları ---


async def tablo_sayimlari(session) -> dict[str, int]:
    """TÜM tabloların satır sayısı (`audit_log` hariç: giriş de denetim satırı yazar)."""
    from app.core.db import Base  # noqa: PLC0415

    sayilar: dict[str, int] = {}
    for tablo in Base.metadata.tables:
        if tablo == "audit_log":
            continue
        sayilar[tablo] = int(
            (await session.execute(text(f'SELECT count(*) FROM "{tablo}"'))).scalar_one()
        )
    return sayilar


def sayim_farki(once: dict[str, int], sonra: dict[str, int]) -> dict[str, int]:
    """`tablo → silinen satır sayısı` (yalnız değişenler)."""
    return {t: once[t] - sonra[t] for t in once if once[t] != sonra[t]}


async def donemi_kapat(session, user, yil: int, ay: int) -> None:
    from datetime import UTC, datetime  # noqa: PLC0415

    from app.modules.accounting.models import (  # noqa: PLC0415
        AccountingPeriod,
        AccountingPeriodStatus,
    )

    session.add(
        AccountingPeriod(
            year=yil,
            month=ay,
            status=AccountingPeriodStatus.closed,
            closed_at=datetime.now(UTC),
            closed_by_id=user.id,
        )
    )
    await session.flush()


async def son_silme_denetimi(session) -> str:
    from app.modules.audit.models import AuditAction, AuditLog  # noqa: PLC0415

    sorgu = (
        select(AuditLog.detail)
        .where(AuditLog.action == AuditAction.delete)
        .order_by(AuditLog.occurred_at.desc())
        .limit(1)
    )
    return (await session.execute(sorgu)).scalar_one()

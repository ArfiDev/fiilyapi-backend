"""Kapanmış bordro dönemine düşen puantaj uyarısı (SIL-B2, kullanıcı kararı).

Bordrosu kapanmış (`pending_approval|approved|paid`) bir aya düşen puantaj satırı silinirse
YALNIZ puantaj satırı gider; bordro dönemi, satırları ve fişi yerinde kalır, mizan bozulmaz.
Bu modül silmeyi engellemez, yalnız önizlemede AÇIKÇA bildirir.
"""

from dataclasses import dataclass

from sqlalchemy import String, extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.silme.cozucu import SilmeAgaci, pk_in
from app.core.silme.etiketler import KAPALI_BORDRO_DURUMLARI
from app.modules.payroll.models import PayrollPeriod
from app.modules.timesheet.models import TimesheetEntry


@dataclass(frozen=True)
class KapaliBordroDonemi:
    year: int
    month: int
    status: str


@dataclass(frozen=True)
class KapaliBordroUyarisi:
    entry_count: int
    periods: list[KapaliBordroDonemi]

    @property
    def message(self) -> str | None:
        if not self.entry_count:
            return None
        return (
            f"Bordrosu kapanmış ayda {self.entry_count} puantaj satırı siliniyor; bordro değişmez"
        )


async def kapali_bordro_uyarisi(session: AsyncSession, agac: SilmeAgaci) -> KapaliBordroUyarisi:
    idler = agac.kayitlar.get("timesheet_entries")
    if not idler:
        return KapaliBordroUyarisi(0, [])
    t = TimesheetEntry.__table__
    p = PayrollPeriod.__table__
    sorgu = (
        select(p.c.year, p.c.month, p.c.status.cast(String), func.count(t.c.id))
        .select_from(
            t.join(
                p,
                (p.c.year == extract("year", t.c.work_date))
                & (p.c.month == extract("month", t.c.work_date)),
            )
        )
        .where(
            pk_in(t, sorted(idler, key=str)), p.c.status.cast(String).in_(KAPALI_BORDRO_DURUMLARI)
        )
        .group_by(p.c.year, p.c.month, p.c.status.cast(String))
        .order_by(p.c.year, p.c.month)
    )
    satirlar = (await session.execute(sorgu)).all()
    return KapaliBordroUyarisi(
        entry_count=sum(int(s[3]) for s in satirlar),
        periods=[KapaliBordroDonemi(int(y), int(a), d) for y, a, d, _ in satirlar],
    )

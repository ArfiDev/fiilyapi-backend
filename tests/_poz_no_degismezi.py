"""TKL-B2 poz no DEGISMEZI — tek SQL, tek yardimci (testler arasi kopya YOK).

DEGISMEZ: her kalemin `poz_no` oneki = kendi disiplininin GUNCEL kodu + `-`, kalan EN AZ 4
rakam; poz no'lar sirket genelinde tekil. Migration dagitim notundaki "dagitim sonrasi
degismez denetimi" de ayni `VIOLATIONS_SQL`i kosar.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

VIOLATIONS_SQL = text(
    "SELECT count(*) FROM ev_catalog_items i JOIN ev_disciplines d ON d.id = i.discipline_id "
    "WHERE left(i.poz_no, length(d.code) + 1) <> d.code || '-' "
    "OR substr(i.poz_no, length(d.code) + 2) !~ '^[0-9]{4,}$'"
)
DUPLICATES_SQL = text(
    "SELECT count(*) FROM (SELECT poz_no FROM ev_catalog_items GROUP BY poz_no "
    "HAVING count(*) > 1) t"
)


async def assert_poz_invariant(session: AsyncSession) -> None:
    assert (await session.execute(VIOLATIONS_SQL)).scalar_one() == 0, "onek degismezi bozuk"
    assert (await session.execute(DUPLICATES_SQL)).scalar_one() == 0, "poz no tekrari"

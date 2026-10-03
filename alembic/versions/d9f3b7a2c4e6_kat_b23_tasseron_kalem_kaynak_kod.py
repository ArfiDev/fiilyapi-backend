"""KAT-B2.3 — taseron sozlesme kalemine Bakanlik kaynak kodu (SNAPSHOT)

Karar kaynagi: KAT-PLAN §6 (K4, K9, K14). `subcontractor_contract_items.source_code` kaynak
isveren kaleminin (`source_contract_item_id` bagi) kodunun KOPYASIDIR; bag SET NULL'a dusse ya da
isveren kaleminin kodu degisse de taseron kalemi olustugu anin kodunu korur. Bagsiz kalem NULL
(istemciden alinmaz). Taseron `code`una DOKUNULMAZ.

## Ne ekler
* `subcontractor_contract_items.source_code` VARCHAR(32) NULL (kisit/indeks YOK).

## Backfill (K14: eski satirlarin TEK doldurma yolu budur)
`source_contract_item_id` bagli ve kaynak kalemin `source_code`u NOT NULL ise kopyalanir; bagsiz /
SET NULL'lanmis / kodsuz kaynakli kalem NULL kalir. Kaynak (`employer_contract_items.source_code`)
B2.1 revizyonunda (c7a1e5b9d3f2) dolar → down_revision zinciri sirayi garanti eder.

## Kilit (c7a1e5b9d3f2 ile AYNI hep-ya-hic kalibi; KARARLAR §2 + coklu kilit inceltmesi)
Kilitler TEK TEK, bir SAVEPOINT icinde NOWAIT alinir: `subcontractor_contract_items` ACCESS
EXCLUSIVE (ALTER), `employer_contract_items` SHARE ROW EXCLUSIVE (backfill kaynagi: okumayi
bloklamaz, yazmayi engeller → backfill sirasinda kaynak degismez). Biri alinamazsa (55P03)
savepoint GERI ALINIR, `LOCK_RETRY_INTERVAL_S` beklenir, yeniden denenir; `LOCK_CEILING_S` dolarsa
migration acik hata ile DUSER (fail-closed). `SET LOCAL lock_timeout = '10s'` son savunma.
`app` IMPORT EDILMEZ. Tek migration, `transaction_per_migration=True` → hepsi ya da hicbiri.

## Downgrade
Ayni lock_timeout + hep-ya-hic kilit (yalniz taseron kalem tablosu AE), sonra kolon duser; kopya
kodlar KAYBOLUR (eski semada karsiligi yok).

Revision ID: d9f3b7a2c4e6
Revises: c7a1e5b9d3f2
Create Date: 2026-10-03

"""

from __future__ import annotations

import time
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d9f3b7a2c4e6"
down_revision: str | Sequence[str] | None = "c7a1e5b9d3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SUBCONTRACT_ITEMS = "subcontractor_contract_items"
EMPLOYER_ITEMS = "employer_contract_items"

LOCK_RETRY_INTERVAL_S = 0.2  # NOWAIT basarisizligindan sonra yeniden deneme araligi
LOCK_CEILING_S = 10.0  # toplam deneme tavani; dolunca migration duser (fail-closed)
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"

UPGRADE_LOCKS = (
    (SUBCONTRACT_ITEMS, "ACCESS EXCLUSIVE"),
    (EMPLOYER_ITEMS, "SHARE ROW EXCLUSIVE"),
)
DOWNGRADE_LOCKS = ((SUBCONTRACT_ITEMS, "ACCESS EXCLUSIVE"),)


class LockCeilingExceededError(RuntimeError):
    """Kilitler tavan suresinde alinamadi; migration fail-closed duser."""


def _sqlstate(exc: sa.exc.DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def _acquire_all_or_nothing(bind: sa.Connection, locks: Sequence[tuple[str, str]]) -> None:
    """Tum kilitleri NOWAIT + savepoint ile hep-ya-hic alir; hicbir kilit beklenerek tutulmaz."""
    deadline = time.monotonic() + LOCK_CEILING_S
    while True:
        try:
            with bind.begin_nested():
                for table, mode in locks:
                    bind.execute(sa.text(f"LOCK TABLE {table} IN {mode} MODE NOWAIT"))
            return
        except sa.exc.DBAPIError as exc:
            if _sqlstate(exc) != LOCK_NOT_AVAILABLE_SQLSTATE:
                raise
            if time.monotonic() >= deadline:
                raise LockCeilingExceededError(
                    f"{LOCK_CEILING_S}s icinde kilitler alinamadi: "
                    f"{[t for t, _ in locks]} (migration geri alindi)"
                ) from exc
        # pg_sleep: surucu dongusunu bloklamadan bekler
        bind.execute(sa.text("SELECT pg_sleep(:s)"), {"s": LOCK_RETRY_INTERVAL_S})


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    _acquire_all_or_nothing(bind, UPGRADE_LOCKS)

    op.add_column(SUBCONTRACT_ITEMS, sa.Column("source_code", sa.String(length=32), nullable=True))

    bind.execute(
        sa.text(
            "UPDATE subcontractor_contract_items AS s SET source_code = e.source_code "
            "FROM employer_contract_items AS e WHERE e.id = s.source_contract_item_id "
            "AND e.source_code IS NOT NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    _acquire_all_or_nothing(bind, DOWNGRADE_LOCKS)
    op.drop_column(SUBCONTRACT_ITEMS, "source_code")

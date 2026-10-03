"""KAT-B2.4 — hakedis satirlarina (isveren + taseron) Bakanlik kaynak kodu (SNAPSHOT)

Karar kaynagi: KAT-PLAN §6.7 (K5, K8, K13). `progress_payment_lines.source_code` baglı isveren
sozlesme kaleminin (`contract_item_id`), `subcontractor_progress_payment_lines.source_code` bagli
taseron sozlesme kaleminin kodunun KOPYASIDIR (snapshot besliyi altiliya cikar); kalem silinse
(FK SET NULL) ya da kalemin kodu degisse de satir olustugu anin kodunu korur. Bagsiz satir NULL.
Hakedis `code`una ve para alanlarina DOKUNULMAZ.

## Ne ekler
* `progress_payment_lines.source_code` ve `subcontractor_progress_payment_lines.source_code`
  VARCHAR(32) NULL (kisit/indeks YOK).

## Backfill (K13: onayli/odenmis dahil TUM satirlar; para etkisi yok, posting `code` okumaz)
SIRA: (1) isveren hakedis satiri ← `employer_contract_items` (`contract_item_id` bagi),
(2) taseron hakedis satiri ← `subcontractor_contract_items` (`contract_item_id` bagi; kaynak kolon
B2.3 revizyonunda (d9f3b7a2c4e6) dolar → down_revision zinciri sirayi garanti eder). Kaynak
kodu NULL ya da bagi kopmus (yetim) satir NULL kalir.

## Kilit (d9f3b7a2c4e6 ile AYNI hep-ya-hic kalibi; KARARLAR §2 + coklu kilit inceltmesi)
Kilitler TEK TEK, bir SAVEPOINT icinde NOWAIT alinir: ALTER edilen iki satir tablosu ACCESS
EXCLUSIVE, backfill kaynagi iki kalem tablosu SHARE ROW EXCLUSIVE (okumayi bloklamaz, yazmayi
engeller → backfill sirasinda kaynak degismez). Biri alinamazsa (55P03) savepoint GERI ALINIR,
`LOCK_RETRY_INTERVAL_S` beklenir, yeniden denenir; `LOCK_CEILING_S` dolarsa migration acik hata ile
DUSER (fail-closed). `SET LOCAL lock_timeout = '10s'` son savunma. `app` IMPORT EDILMEZ. Tek
migration, `transaction_per_migration=True` → hepsi ya da hicbiri.

## Downgrade
Ayni lock_timeout + hep-ya-hic kilit (yalniz iki satir tablosu AE), sonra iki kolon duser; kopya
kodlar KAYBOLUR (eski semada karsiligi yok).

Revision ID: e4a8c2d6f1b3
Revises: d9f3b7a2c4e6
Create Date: 2026-10-03

"""

from __future__ import annotations

import time
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e4a8c2d6f1b3"
down_revision: str | Sequence[str] | None = "d9f3b7a2c4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMPLOYER_LINES = "progress_payment_lines"
SUBCONTRACT_LINES = "subcontractor_progress_payment_lines"
EMPLOYER_ITEMS = "employer_contract_items"
SUBCONTRACT_ITEMS = "subcontractor_contract_items"

LOCK_RETRY_INTERVAL_S = 0.2  # NOWAIT basarisizligindan sonra yeniden deneme araligi
LOCK_CEILING_S = 10.0  # toplam deneme tavani; dolunca migration duser (fail-closed)
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"

UPGRADE_LOCKS = (
    (EMPLOYER_LINES, "ACCESS EXCLUSIVE"),
    (SUBCONTRACT_LINES, "ACCESS EXCLUSIVE"),
    (EMPLOYER_ITEMS, "SHARE ROW EXCLUSIVE"),
    (SUBCONTRACT_ITEMS, "SHARE ROW EXCLUSIVE"),
)
DOWNGRADE_LOCKS = (
    (EMPLOYER_LINES, "ACCESS EXCLUSIVE"),
    (SUBCONTRACT_LINES, "ACCESS EXCLUSIVE"),
)


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

    op.add_column(EMPLOYER_LINES, sa.Column("source_code", sa.String(length=32), nullable=True))
    op.add_column(SUBCONTRACT_LINES, sa.Column("source_code", sa.String(length=32), nullable=True))

    # (1) isveren hakedis satiri ← isveren sozlesme kalemi
    bind.execute(
        sa.text(
            "UPDATE progress_payment_lines AS l SET source_code = i.source_code "
            "FROM employer_contract_items AS i WHERE i.id = l.contract_item_id "
            "AND i.source_code IS NOT NULL"
        )
    )
    # (2) taseron hakedis satiri ← taseron sozlesme kalemi (kopya kolonu B2.3 doldurdu)
    bind.execute(
        sa.text(
            "UPDATE subcontractor_progress_payment_lines AS l SET source_code = i.source_code "
            "FROM subcontractor_contract_items AS i WHERE i.id = l.contract_item_id "
            "AND i.source_code IS NOT NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    _acquire_all_or_nothing(bind, DOWNGRADE_LOCKS)
    op.drop_column(SUBCONTRACT_LINES, "source_code")
    op.drop_column(EMPLOYER_LINES, "source_code")

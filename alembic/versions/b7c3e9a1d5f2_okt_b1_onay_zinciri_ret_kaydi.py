"""OKT-B1 — onay zinciri RET KAYDI (ret zinciri artik SILMEZ)

Karar kaynagi: KARARLAR 62ae58a (Onay Kutusu gecmisi). Eski K2 ("ret zinciri siler, adimlar
CASCADE ile gider") kullanici karariyla degisti: reddedilen zincir kayit olarak KALIR; kim /
ne zaman / gerekce saklanir. Eski (zaten silinmis) retler geri GELMEZ — backfill YOKTUR.

## Ne yapar
* `approval_chains`a uc kolon: `rejected_at` (timestamptz NULL), `rejected_by_user_id` (FK users,
  ON DELETE SET NULL), `rejection_reason` (text NULL).
  `rejected_at IS NOT NULL` = zincir reddedildi.
* `ck_approval_chains_rejection_pair`: `(rejected_at IS NULL) = (rejection_reason IS NULL)` — ret
  damgasi gerekcesiz yazilamaz. Mevcut satirlarin hepsi (NULL, NULL) oldugu icin
  kisit guvenle gecer.
* `uq_approval_chains_document` (UNIQUE document_type, document_id) KALDIRILIR, yerine KISMI unique
  indeks `uq_approval_chains_open_document ... WHERE rejected_at IS NULL` gelir: bir evragin ayni
  anda en fazla BIR ACIK zinciri olur; reddedilmis zincirler birikebilir ve yeniden gonderim yeni
  zincir acar.

## Kilit
Tablo kucuktur (evrak basina bir satir). `approval_chains` (ve downgrade'de cascade'in dokunacagi
`approval_steps`) ACCESS EXCLUSIVE, NOWAIT + savepoint ile hep-ya-hic alinir (e4a8c2d6f1b3 kalibi);
`SET LOCAL lock_timeout = '10s'` son savunma. `app` IMPORT EDILMEZ.

## Downgrade
Reddedilmis zincirler (ve CASCADE ile adimlari) SILINIR: eski semada karsiligi yoktur ve kismi
indeksin disindaki satirlar eski UNIQUE'i ihlal edebilir.
Sonra kisit/indeks/kolonlar eski hâle doner.

Revision ID: b7c3e9a1d5f2
Revises: e4a8c2d6f1b3
Create Date: 2026-10-04

"""

from __future__ import annotations

import time
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7c3e9a1d5f2"
down_revision: str | Sequence[str] | None = "e4a8c2d6f1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHAINS = "approval_chains"
STEPS = "approval_steps"

LOCK_RETRY_INTERVAL_S = 0.2
LOCK_CEILING_S = 10.0
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"

LOCKS = ((CHAINS, "ACCESS EXCLUSIVE"), (STEPS, "ACCESS EXCLUSIVE"))


class LockCeilingExceededError(RuntimeError):
    """Kilitler tavan suresinde alinamadi; migration fail-closed duser."""


def _sqlstate(exc: sa.exc.DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def _acquire_all_or_nothing(bind: sa.Connection) -> None:
    """Tum kilitleri NOWAIT + savepoint ile hep-ya-hic alir; hicbir kilit beklenerek tutulmaz."""
    deadline = time.monotonic() + LOCK_CEILING_S
    while True:
        try:
            with bind.begin_nested():
                for table, mode in LOCKS:
                    bind.execute(sa.text(f"LOCK TABLE {table} IN {mode} MODE NOWAIT"))
            return
        except sa.exc.DBAPIError as exc:
            if _sqlstate(exc) != LOCK_NOT_AVAILABLE_SQLSTATE:
                raise
            if time.monotonic() >= deadline:
                raise LockCeilingExceededError(
                    f"{LOCK_CEILING_S}s icinde kilitler alinamadi: "
                    f"{[t for t, _ in LOCKS]} (migration geri alindi)"
                ) from exc
        bind.execute(sa.text("SELECT pg_sleep(:s)"), {"s": LOCK_RETRY_INTERVAL_S})


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    _acquire_all_or_nothing(bind)

    op.add_column(CHAINS, sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        CHAINS,
        sa.Column("rejected_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(CHAINS, sa.Column("rejection_reason", sa.Text(), nullable=True))
    op.create_foreign_key(
        "approval_chains_rejected_by_user_id_fkey",
        CHAINS,
        "users",
        ["rejected_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_approval_chains_rejection_pair",
        CHAINS,
        "(rejected_at IS NULL) = (rejection_reason IS NULL)",
    )
    op.drop_constraint("uq_approval_chains_document", CHAINS, type_="unique")
    op.create_index(
        "uq_approval_chains_open_document",
        CHAINS,
        ["document_type", "document_id"],
        unique=True,
        postgresql_where=sa.text("rejected_at IS NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    _acquire_all_or_nothing(bind)

    # Reddedilmis zincirler (adimlari CASCADE ile) eski semada yer bulamaz.
    bind.execute(sa.text(f"DELETE FROM {CHAINS} WHERE rejected_at IS NOT NULL"))
    op.drop_index("uq_approval_chains_open_document", table_name=CHAINS)
    op.create_unique_constraint(
        "uq_approval_chains_document", CHAINS, ["document_type", "document_id"]
    )
    op.drop_constraint("ck_approval_chains_rejection_pair", CHAINS, type_="check")
    op.drop_constraint("approval_chains_rejected_by_user_id_fkey", CHAINS, type_="foreignkey")
    op.drop_column(CHAINS, "rejection_reason")
    op.drop_column(CHAINS, "rejected_by_user_id")
    op.drop_column(CHAINS, "rejected_at")

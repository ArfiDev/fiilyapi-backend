"""DSC-B0 — `user_disciplines`: kullanici → disiplin atamasi (EV uzanti tablosu)

Disiplin kapsami (DISIPLIN-KAPSAMI-SPEC.md) temeli. Bir kullaniciya bir ya da birden cok
`ev_disciplines` atanabilir; atamasi OLMAYAN kullanici kisitsizdir. Bu dilim yalniz
tabloyu ekler — hicbir uc henuz suzmez.

* PK `(user_id, discipline_id)`; `user_id` → `users.id` ON DELETE CASCADE (kullanici
  silinince atamalari gider), `discipline_id` → `ev_disciplines.id` ON DELETE RESTRICT
  (atanmis disiplin silinemez; servis sayaci 409 verir).
* `ix_user_disciplines_discipline_id`: disiplin tarafi (silme sayimi + RESTRICT denetimi).
  PK kullanici-onde oldugundan bu yon icin ayri indeks gerekir.

Yeni tablo → canli veriye dokunmaz; downgrade tabloyu (ve atamalari) DUSURUR.

Revision ID: d5c0b0a1e7f3
Revises: 3102e435238c
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5c0b0a1e7f3"
down_revision: str | Sequence[str] | None = "3102e435238c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_disciplines",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("discipline_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["discipline_id"], ["ev_disciplines.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("user_id", "discipline_id"),
    )
    op.create_index(
        "ix_user_disciplines_discipline_id", "user_disciplines", ["discipline_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_user_disciplines_discipline_id", table_name="user_disciplines")
    op.drop_table("user_disciplines")

"""TKL-B4.1 — teklif cekirdegi: ayar + sayac + teklif/revizyon/grup/kalem tablolari

Karar kaynagi: TKL-PLAN §2.3a (TKL-B4 uygulama tasarimi), T31-T37.

## Ne ekler (YALNIZ YENI TABLO — mevcut tabloya ALTER YOK)
* `offer_settings` — tekil satir (`only_row` UNIQUE + CHECK); migration tek satiri tohumlar:
  GG 12 / kar 15 / KDV 20 / gecerlilik 30 gun / varsayilan odeme metni.
* `offer_counters` — yil bazli monoton numara sayaci (`last_no`).
* `offers`, `offer_revisions`, `offer_groups`, `offer_items`.
* Iki YENI PG enum tipi: `offer_revision_status`, `offer_price_escalation`.
* `price_index_type` PG tipi projelerle PAYLASILIR: `postgresql.ENUM(create_type=False)`,
  yeniden yaratilmaz ve downgrade'de DUSURULMEZ (`project_contracts` kullanir).
* Para birimi kolonu YOK (T36: yalniz TL).

## Kilit analizi (ISIN OLCUMU — pg_locks, PG 18, 2026-10-02)
FK'li `CREATE TABLE` yeni tabloda kendi kilidini alir; FK HEDEFI tablolarda ise
`SHARE ROW EXCLUSIVE` alir (FK tetikleyicileri hedefe eklenir; hedefin yazmalarini bloklar,
okumalarini ETMEZ): `employers`, `users`, `ev_catalog_items` (+ yeni tablolarin birbirine
FK'leri icin yalniz yeni tablolar). ACCESS EXCLUSIVE alan hedef tablo YOKTUR; yani calisan
konteynerin okumalari bloklanmaz, yalniz bu migration suresince (milisaniyeler) o uc tabloya
YAZMA bekler. Bu yuzden acik `LOCK TABLE` KOSMAYIZ (yukseltme riski yok, kilit tum FK
ifadelerinde ZATEN alinir) — yalniz `SET LOCAL lock_timeout = '10s'`: kilit 10 sn icinde
alinamazsa migration DUSER (fail-closed). Alinan kilit islem sonuna kadar tutulur
(`transaction_per_migration=True`; kilit suresi = migration suresi, tablolar bos).
`app` IMPORT EDILMEZ.

## Downgrade
Tablolar (cocuktan ebeveyne) ve YALNIZ iki yeni enum tipi dusurulur. Teklif verisi KAYBOLUR
(eski semada karsiligi yok); `price_index_type` KALIR.

## Dagitim notu
Yeni tablolar eski kodla acilan konteyneri ETKILEMEZ (kimse okumaz/yazmaz).

Revision ID: d8f1a3c5e7b9
Revises: c5d7e9a2b4f6
Create Date: 2026-10-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d8f1a3c5e7b9"
down_revision: str | Sequence[str] | None = "c5d7e9a2b4f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_PAYMENT_TERMS = "Ödeme aylık hakedişle, 30 gün vadeli"

STATUS_VALUES = ("draft", "sent", "won", "lost", "withdrawn")
ESCALATION_VALUES = ("tuik", "fixed")
NEW_ENUMS = (
    ("offer_revision_status", STATUS_VALUES),
    ("offer_price_escalation", ESCALATION_VALUES),
)


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False)


def _user_fk(name: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL", name=f"fk_offer_revisions_{name}_users"),
        nullable=True,
    )


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))

    for name, values in NEW_ENUMS:
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=False)
    status_t = postgresql.ENUM(*STATUS_VALUES, name="offer_revision_status", create_type=False)
    escalation_t = postgresql.ENUM(
        *ESCALATION_VALUES, name="offer_price_escalation", create_type=False
    )
    index_t = postgresql.ENUM(name="price_index_type", create_type=False)

    op.create_table(
        "offer_settings",
        _uuid_pk(),
        sa.Column("only_row", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "default_overhead_pct",
            sa.Numeric(5, 2),
            server_default=sa.text("12.00"),
            nullable=False,
        ),
        sa.Column(
            "default_profit_pct", sa.Numeric(6, 2), server_default=sa.text("15.00"), nullable=False
        ),
        sa.Column(
            "default_vat_pct", sa.Numeric(5, 2), server_default=sa.text("20.00"), nullable=False
        ),
        sa.Column(
            "default_validity_days", sa.Integer(), server_default=sa.text("30"), nullable=False
        ),
        sa.Column(
            "default_payment_terms",
            sa.Text(),
            server_default=sa.text(f"'{DEFAULT_PAYMENT_TERMS}'"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("only_row"),
        sa.CheckConstraint("only_row IS TRUE", name="ck_offer_settings_single_row"),
        sa.CheckConstraint(
            "default_overhead_pct >= 0 AND default_overhead_pct <= 100",
            name="ck_offer_settings_overhead_range",
        ),
        sa.CheckConstraint(
            "default_profit_pct >= 0 AND default_profit_pct <= 999.99",
            name="ck_offer_settings_profit_range",
        ),
        sa.CheckConstraint(
            "default_vat_pct >= 0 AND default_vat_pct <= 100", name="ck_offer_settings_vat_range"
        ),
        sa.CheckConstraint(
            "default_validity_days >= 1 AND default_validity_days <= 365",
            name="ck_offer_settings_validity_range",
        ),
    )
    # Tekil satir tohumu (kolon varsayilanlari 12 / 15 / 20 / 30 / odeme metni ile ayni).
    bind.execute(
        sa.text(
            "INSERT INTO offer_settings (id, only_row, default_overhead_pct, default_profit_pct, "
            "default_vat_pct, default_validity_days, default_payment_terms) "
            "VALUES (gen_random_uuid(), true, 12.00, 15.00, 20.00, 30, :terms)"
        ).bindparams(terms=DEFAULT_PAYMENT_TERMS)
    )

    op.create_table(
        "offer_counters",
        sa.Column("year", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_no", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("year"),
        sa.CheckConstraint("last_no >= 1", name="ck_offer_counters_last_no_positive"),
    )

    op.create_table(
        "offers",
        _uuid_pk(),
        sa.Column("offer_no", sa.String(32), nullable=False),
        sa.Column(
            "employer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "employers.id", ondelete="RESTRICT", name="fk_offers_employer_id_employers"
            ),
            nullable=False,
        ),
        sa.Column("employer_name", sa.String(200), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("scope_summary", sa.Text(), nullable=True),
        sa.Column(
            "prepared_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id", ondelete="SET NULL", name="fk_offers_prepared_by_user_id_users"
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("offer_no", name="uq_offers_offer_no"),
        sa.CheckConstraint("btrim(title) <> ''", name="ck_offers_title_not_blank"),
    )
    op.create_index("ix_offers_employer_id", "offers", ["employer_id"])

    op.create_table(
        "offer_revisions",
        _uuid_pk(),
        sa.Column(
            "offer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "offers.id", ondelete="CASCADE", name="fk_offer_revisions_offer_id_offers"
            ),
            nullable=False,
        ),
        sa.Column("rev_no", sa.Integer(), nullable=False),
        sa.Column("status", status_t, server_default=sa.text("'draft'"), nullable=False),
        sa.Column("offer_date", sa.Date(), nullable=False),
        sa.Column("validity_days", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("overhead_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("profit_pct", sa.Numeric(6, 2), nullable=False),
        sa.Column("vat_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("payment_terms", sa.Text(), nullable=True),
        sa.Column("delivery_days", sa.Integer(), nullable=True),
        sa.Column(
            "price_escalation", escalation_t, server_default=sa.text("'fixed'"), nullable=False
        ),
        sa.Column("price_index_type", index_t, nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk("sent_by_user_id"),
        sa.Column("won_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk("won_by_user_id"),
        sa.Column("lost_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk("lost_by_user_id"),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk("withdrawn_by_user_id"),
        sa.Column("lost_reason", sa.Text(), nullable=True),
        sa.Column("winning_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _user_fk("created_by_user_id"),
        sa.UniqueConstraint("offer_id", "rev_no", name="uq_offer_revisions_offer_rev"),
        sa.CheckConstraint("rev_no >= 0", name="ck_offer_revisions_rev_no_nonneg"),
        sa.CheckConstraint(
            "validity_days >= 1 AND validity_days <= 365", name="ck_offer_revisions_validity_range"
        ),
        sa.CheckConstraint(
            "overhead_pct >= 0 AND overhead_pct <= 100", name="ck_offer_revisions_overhead_range"
        ),
        sa.CheckConstraint(
            "profit_pct >= 0 AND profit_pct <= 999.99", name="ck_offer_revisions_profit_range"
        ),
        sa.CheckConstraint("vat_pct >= 0 AND vat_pct <= 100", name="ck_offer_revisions_vat_range"),
        sa.CheckConstraint(
            "delivery_days IS NULL OR delivery_days >= 0", name="ck_offer_revisions_delivery_nonneg"
        ),
        sa.CheckConstraint(
            "(price_escalation = 'tuik') = (price_index_type IS NOT NULL)",
            name="ck_offer_revisions_escalation_index",
        ),
        sa.CheckConstraint(
            "status NOT IN ('sent', 'won', 'lost') OR sent_at IS NOT NULL",
            name="ck_offer_revisions_stamp_sent",
        ),
        sa.CheckConstraint(
            "status <> 'draft' OR sent_at IS NULL", name="ck_offer_revisions_stamp_draft"
        ),
        sa.CheckConstraint(
            "(status = 'won') = (won_at IS NOT NULL)", name="ck_offer_revisions_stamp_won"
        ),
        sa.CheckConstraint(
            "(status = 'lost') = (lost_at IS NOT NULL)", name="ck_offer_revisions_stamp_lost"
        ),
        sa.CheckConstraint(
            "(status = 'withdrawn') = (withdrawn_at IS NOT NULL)",
            name="ck_offer_revisions_stamp_withdrawn",
        ),
        sa.CheckConstraint(
            "status = 'lost' OR (lost_reason IS NULL AND winning_amount IS NULL)",
            name="ck_offer_revisions_lost_fields_only_lost",
        ),
        sa.CheckConstraint(
            "winning_amount IS NULL OR winning_amount >= 0",
            name="ck_offer_revisions_winning_amount_nonneg",
        ),
    )

    op.create_table(
        "offer_groups",
        _uuid_pk(),
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "offer_revisions.id",
                ondelete="CASCADE",
                name="fk_offer_groups_revision_id_offer_revisions",
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.UniqueConstraint("id", "revision_id", name="uq_offer_groups_id_revision"),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_offer_groups_name_not_blank"),
    )
    op.create_index("ix_offer_groups_revision_id", "offer_groups", ["revision_id"])

    op.create_table(
        "offer_items",
        _uuid_pk(),
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "offer_revisions.id",
                ondelete="CASCADE",
                name="fk_offer_items_revision_id_offer_revisions",
            ),
            nullable=False,
        ),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "catalog_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "ev_catalog_items.id",
                ondelete="RESTRICT",
                name="fk_offer_items_catalog_item_id_ev_catalog_items",
            ),
            nullable=False,
        ),
        sa.Column("poz_no", sa.String(32), nullable=False),
        sa.Column("description", sa.String(200), nullable=False),
        sa.Column("unit", sa.String(50), nullable=False),
        sa.Column("quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column("unit_mhr", sa.Numeric(12, 4), nullable=False),
        sa.Column("cost_unit_price", sa.Numeric(18, 2), nullable=True),
        sa.Column("overhead_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("profit_pct", sa.Numeric(6, 2), nullable=True),
        sa.Column("offer_unit_price", sa.Numeric(18, 2), nullable=True),
        sa.ForeignKeyConstraint(
            ["group_id", "revision_id"],
            ["offer_groups.id", "offer_groups.revision_id"],
            ondelete="CASCADE",
            name="fk_offer_items_group_revision",
        ),
        sa.CheckConstraint("quantity > 0", name="ck_offer_items_quantity_positive"),
        sa.CheckConstraint("unit_mhr > 0", name="ck_offer_items_unit_mhr_positive"),
        sa.CheckConstraint(
            "cost_unit_price IS NULL OR cost_unit_price >= 0", name="ck_offer_items_cost_nonneg"
        ),
        sa.CheckConstraint(
            "offer_unit_price IS NULL OR offer_unit_price >= 0", name="ck_offer_items_offer_nonneg"
        ),
        sa.CheckConstraint(
            "overhead_pct IS NULL OR (overhead_pct >= 0 AND overhead_pct <= 100)",
            name="ck_offer_items_overhead_range",
        ),
        sa.CheckConstraint(
            "profit_pct IS NULL OR (profit_pct >= 0 AND profit_pct <= 999.99)",
            name="ck_offer_items_profit_range",
        ),
    )
    op.create_index("ix_offer_items_revision_id", "offer_items", ["revision_id"])
    op.create_index("ix_offer_items_group_id", "offer_items", ["group_id"])
    op.create_index("ix_offer_items_catalog_item_id", "offer_items", ["catalog_item_id"])


def downgrade() -> None:
    # Cocuktan ebeveyne; `price_index_type` BILEREK dusurulmez (projeler kullanir).
    op.drop_table("offer_items")
    op.drop_table("offer_groups")
    op.drop_table("offer_revisions")
    op.drop_table("offers")
    op.drop_table("offer_counters")
    op.drop_table("offer_settings")
    bind = op.get_bind()
    for name, values in reversed(NEW_ENUMS):
        postgresql.ENUM(*values, name=name).drop(bind, checkfirst=False)

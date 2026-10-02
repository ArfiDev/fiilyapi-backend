"""TKL-B5.1 — teklif sablonlari + miktarsiz kalem (SO-21)

Karar kaynagi: TKL-PLAN §2.3 (sablon tablolari), SABAH ONAYI SO-21.

## Ne yapar
* `offer_items.quantity` NULL olabilir ("miktar girilmedi"): `NOT NULL` kalkar, eski
  `ck_offer_items_quantity_positive` KALDIRILIR, yerine `ck_offer_items_quantity_null_or_positive`
  (`quantity IS NULL OR quantity > 0`) gelir. TEK `ALTER TABLE` ifadesi: tablo kilidi baştan
  tek seferde alinir (yukseltme/siralama riski yok).
* `offers.template_id` (FK `offer_templates` ON DELETE SET NULL, NULL) + indeks: sablon
  "kullanim sayisi" bu bagdan TUREV sayilir. Yine TEK `ALTER TABLE` ifadesi.
* Yeni tablolar: `offer_templates` (kismi UQ `WHERE is_default` → TEK varsayilan),
  `offer_template_groups`, `offer_template_items` (bilesik FK: grup AYNI sablonda). Fiyat ve
  miktar SAKLANMAZ (T12).

## Kilit analizi
`SET LOCAL lock_timeout = '10s'` HER DDL'den once: kilit 10 sn icinde alinamazsa migration
DUSER (fail-closed). `ALTER TABLE offer_items` ve `ALTER TABLE offers` ACCESS EXCLUSIVE alir
(kisa: kisit dogrulamasi + kolon ekleme meta verisi; B4 tablolari yeni ve kucuk); yeni
tablolarin FK hedefleri (`users`, `ev_catalog_items`) icin `SHARE ROW EXCLUSIVE` (yalniz
yazmalari bekletir) — B4 migration'inda OLCULDU. `app` IMPORT EDILMEZ.

## Dagitim notu (iki surum)
Migration ESKI kodla uyumludur: eski kod `quantity` hep dolu yazar ve `template_id`yi
bilmez (NULL kalir). Yeni kod eski semayla ACILMAZ (miktarsiz kalem yazar) → migration ONCE.

## Downgrade
Miktarsiz kalem VARSA downgrade DURUR (sessiz veri uydurma yok: kullanici once miktar girer ya
da kalemi siler). Sonra sablon tablolari (cocuktan ebeveyne), `offers.template_id` ve yeni
CHECK dusurulur; eski CHECK + NOT NULL geri gelir. Sablonlar KAYBOLUR.

Revision ID: e4b7c9d1a3f5
Revises: d8f1a3c5e7b9
Create Date: 2026-10-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e4b7c9d1a3f5"
down_revision: str | Sequence[str] | None = "d8f1a3c5e7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False)


def _user_fk(name: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL", name=f"fk_offer_templates_{name}_users"),
        nullable=True,
    )


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))

    op.create_table(
        "offer_templates",
        _uuid_pk(),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("overhead_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("profit_pct", sa.Numeric(6, 2), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        _user_fk("created_by_user_id"),
        _user_fk("updated_by_user_id"),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_offer_templates_name_not_blank"),
        sa.CheckConstraint(
            "overhead_pct IS NULL OR (overhead_pct >= 0 AND overhead_pct <= 100)",
            name="ck_offer_templates_overhead_range",
        ),
        sa.CheckConstraint(
            "profit_pct IS NULL OR (profit_pct >= 0 AND profit_pct <= 999.99)",
            name="ck_offer_templates_profit_range",
        ),
    )
    # TEK varsayilan: kismi tekil indeks (iki satir ayni anda `is_default` olamaz).
    op.create_index(
        "uq_offer_templates_single_default",
        "offer_templates",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.create_table(
        "offer_template_groups",
        _uuid_pk(),
        sa.Column(
            "template_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "offer_templates.id",
                ondelete="CASCADE",
                name="fk_offer_template_groups_template_id_offer_templates",
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.UniqueConstraint("id", "template_id", name="uq_offer_template_groups_id_template"),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_offer_template_groups_name_not_blank"),
    )
    op.create_index(
        "ix_offer_template_groups_template_id", "offer_template_groups", ["template_id"]
    )

    op.create_table(
        "offer_template_items",
        _uuid_pk(),
        sa.Column(
            "template_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "offer_templates.id",
                ondelete="CASCADE",
                name="fk_offer_template_items_template_id_offer_templates",
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
                name="fk_offer_template_items_catalog_item_id_ev_catalog_items",
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["group_id", "template_id"],
            ["offer_template_groups.id", "offer_template_groups.template_id"],
            ondelete="CASCADE",
            name="fk_offer_template_items_group_template",
        ),
    )
    op.create_index("ix_offer_template_items_template_id", "offer_template_items", ["template_id"])
    op.create_index("ix_offer_template_items_group_id", "offer_template_items", ["group_id"])
    op.create_index(
        "ix_offer_template_items_catalog_item_id", "offer_template_items", ["catalog_item_id"]
    )

    # SO-21: miktarsiz kalem. TEK ALTER ifadesi (tablo kilidi bastan, tek seferde).
    bind.execute(
        sa.text(
            "ALTER TABLE offer_items "
            "ALTER COLUMN quantity DROP NOT NULL, "
            "DROP CONSTRAINT ck_offer_items_quantity_positive, "
            "ADD CONSTRAINT ck_offer_items_quantity_null_or_positive "
            "CHECK (quantity IS NULL OR quantity > 0)"
        )
    )
    # Sablondan teklif bagi. TEK ALTER ifadesi.
    bind.execute(
        sa.text(
            "ALTER TABLE offers "
            "ADD COLUMN template_id uuid, "
            "ADD CONSTRAINT fk_offers_template_id_offer_templates "
            "FOREIGN KEY (template_id) REFERENCES offer_templates (id) ON DELETE SET NULL"
        )
    )
    op.create_index("ix_offers_template_id", "offers", ["template_id"])


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    unquantified = bind.execute(
        sa.text("SELECT count(*) FROM offer_items WHERE quantity IS NULL")
    ).scalar_one()
    if unquantified:
        raise RuntimeError(
            f"Downgrade durdu: {unquantified} miktarsız teklif kalemi var. "
            "Önce miktar girin ya da kalemleri silin (sessiz miktar uydurulmaz)."
        )
    op.drop_index("ix_offers_template_id", table_name="offers")
    bind.execute(
        sa.text(
            "ALTER TABLE offers "
            "DROP CONSTRAINT fk_offers_template_id_offer_templates, "
            "DROP COLUMN template_id"
        )
    )
    bind.execute(
        sa.text(
            "ALTER TABLE offer_items "
            "DROP CONSTRAINT ck_offer_items_quantity_null_or_positive, "
            "ALTER COLUMN quantity SET NOT NULL, "
            "ADD CONSTRAINT ck_offer_items_quantity_positive CHECK (quantity > 0)"
        )
    )
    op.drop_table("offer_template_items")
    op.drop_table("offer_template_groups")
    op.drop_table("offer_templates")

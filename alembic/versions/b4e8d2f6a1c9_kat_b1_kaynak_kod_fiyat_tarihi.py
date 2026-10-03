"""KAT-B1 — katalog kalemine Bakanlik kaynak kodu + fiyat tarihi

Karar kaynagi: Bakanlik 2026 birim fiyat listesinin Is Kalemi Katalogu'na aktarimi.
Bizim `poz_no` (DISIPLIN-NNNN, sunucu uretir) AYNEN kalir; Bakanlik numarasi AYRI alandadir.

## Ne ekler
* `ev_catalog_items.source_code` VARCHAR(32) NULL — Bakanlik (kaynak) poz kodu, ornek
  `15.100.1001`. KISMI UNIQUE indeks `uq_ev_catalog_items_source_code WHERE source_code IS NOT
  NULL`: bos kodlu kalemler (mevcut hepsi) serbest, dolu kod sirket genelinde tekil.
* `ev_catalog_items.ref_price_date` DATE NULL — `ref_price`in GECERLILIK tarihi (fiyat listesi
  tarihi, ornek 01.01.2026). `price_updated_at` (sunucu damgasi) ile AYNI sey DEGILDIR.
* CHECK `ck_ev_catalog_items_ref_price_date_requires_price`: `ref_price_date IS NULL OR
  ref_price IS NOT NULL` (fiyat tarihi yalniz fiyatla anlamli; servis kuralinin DB yedegi).
Mevcut satirlar NULL kalir (CHECK'i ihlal eden satir olamaz).

## Upgrade (tek migration, `transaction_per_migration=True` → hepsi ya da hicbiri)
`SET LOCAL lock_timeout = '10s'` ve TEK ifadede `LOCK TABLE ev_catalog_items IN ACCESS
EXCLUSIVE MODE` EN BASTA (TKL-B2 kilit kanonu: kilit YUKSELTMESI yok; yalniz ALTER edilen
tablo). Kilit 10 sn icinde alinamazsa migration DUSER → konteyner acilmaz (fail-closed).
Indeks `CONCURRENTLY` DEGIL: tablo kucuk, ayni transaction icinde. `app` IMPORT EDILMEZ.

## Downgrade
Indeks + iki kolon duser; kurulmus kaynak kodlari ve fiyat tarihleri KAYBOLUR (eski semada
karsiligi yok).

Revision ID: b4e8d2f6a1c9
Revises: a9d3b5f7c1e8
Create Date: 2026-10-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b4e8d2f6a1c9"
down_revision: str | Sequence[str] | None = "a9d3b5f7c1e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "ev_catalog_items"
UQ_SOURCE_CODE = "uq_ev_catalog_items_source_code"
CK_DATE_NEEDS_PRICE = "ck_ev_catalog_items_ref_price_date_requires_price"


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    bind.execute(sa.text("LOCK TABLE ev_catalog_items IN ACCESS EXCLUSIVE MODE"))

    op.add_column(TABLE, sa.Column("source_code", sa.String(length=32), nullable=True))
    op.add_column(TABLE, sa.Column("ref_price_date", sa.Date(), nullable=True))
    op.create_index(
        UQ_SOURCE_CODE,
        TABLE,
        ["source_code"],
        unique=True,
        postgresql_where=sa.text("source_code IS NOT NULL"),
    )
    op.create_check_constraint(
        CK_DATE_NEEDS_PRICE, TABLE, "ref_price_date IS NULL OR ref_price IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_constraint(CK_DATE_NEEDS_PRICE, TABLE, type_="check")
    op.drop_index(
        UQ_SOURCE_CODE, table_name=TABLE, postgresql_where=sa.text("source_code IS NOT NULL")
    )
    op.drop_column(TABLE, "ref_price_date")
    op.drop_column(TABLE, "source_code")

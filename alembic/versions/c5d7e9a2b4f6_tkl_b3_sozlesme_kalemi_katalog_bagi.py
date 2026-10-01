"""TKL-B3.1 — isveren sozlesmesi kalemine katalog iz bagi + fiyat degisim damgasi

Karar kaynagi: TKL-PLAN §2.4 / §3 (TKL-B3), "+ Poz Ekle" katalog baglama + son fiyat zemini.

## Ne ekler
* `employer_contract_items.catalog_item_id` UUID NULL, FK → `ev_catalog_items.id`
  ON DELETE SET NULL (katalog kalemi zaten silinmiyor; SET NULL savunma amacli) ve KISMI
  indeks `ix_employer_contract_items_catalog_item_id WHERE catalog_item_id IS NOT NULL`
  (son fiyat saglayicisi `DISTINCT ON (catalog_item_id)` okur; bagsiz cogunluk indekse girmez).
  Mevcut satirlar NULL kalir (gecmise donuk baglama YOK).
* `employer_contract_items.price_changed_at` TIMESTAMPTZ NOT NULL, server_default `now()`:
  `unit_price` DEGER olarak her degistiginde ilerler (uygulama yazar). Eski konteyner
  penceresinde INSERT server_default ile gecer (kirilmaz).

## Mevcut satirlar
Mevcut satirlarda `price_changed_at = updated_at` (EN IYI YAKLASIK): gercek fiyat degisim
zamani eski semada tutulmuyordu; `updated_at` fiyat disi bir alan degisimini de icerdiginden
damga fiyatin gercek degisiminden SONRA olabilir, ama onunla hic ilgisiz bir zaman da degildir.

## Upgrade (tek migration, `transaction_per_migration=True` → hepsi ya da hicbiri)
1. `SET LOCAL lock_timeout = '10s'`; sonra IKI AYRI `LOCK TABLE`, sabit sirayla (ebeveyn
   once): `ev_catalog_items` IN SHARE ROW EXCLUSIVE MODE, ardindan `employer_contract_items`
   IN ACCESS EXCLUSIVE MODE. Gerekce: AE baslangic kurali ALTER edilen tablo icindir
   (TKL-B2 kilit kanonu: kilit YUKSELTMESI YOK). Katalog tablosu yalniz FK HEDEFIDIR;
   FK eklemek referans tabloya zaten SHARE ROW EXCLUSIVE ister, daha gucluyu istemez —
   AE alinsaydi, kalem tablosu kilidi beklenirken katalog OKUMALARI da 10 sn'ye kadar
   bloklanirdi. Katalog tablosuna sonradan daha guclu kilit isteyen ifade YOKTUR (yukseltme
   yok). Kilit 10 sn icinde alinamazsa migration DUSER → konteyner acilmaz (fail-closed).
2. Iki kolon eklenir (`price_changed_at` DEFAULT now() ile NOT NULL: sabit varsayilan,
   tablo yeniden yazilmaz), mevcut satirlar `updated_at` ile doldurulur.
3. FK + kismi indeks.
`app` IMPORT EDILMEZ.

## Downgrade
Indeks, FK ve iki kolon duser. Kurulmus katalog baglari ve fiyat damgalari KAYBOLUR
(bilincli: eski semada karsiligi yok).

## Dagitim notu
Migration ESKI konteyner trafik alirken kosar: pencerede eski kodla acilan kalem
`catalog_item_id = NULL` ve `price_changed_at = now()` ile dogar (kirilmaz); eski kodla fiyat
degisimi damgayi ilerletmez (pencere kisa, kabul edilmis risk). Kilit islem sonuna kadar
tutulur (satir sayisi kucuk; kalem tablosunda okuma da bekler, katalog okumalari beklemez).

Revision ID: c5d7e9a2b4f6
Revises: a2c4e6f81b3d
Create Date: 2026-10-01

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c5d7e9a2b4f6"
down_revision: str | Sequence[str] | None = "a2c4e6f81b3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "employer_contract_items"
FK_CATALOG = "fk_employer_contract_items_catalog_item_id_ev_catalog_items"
IX_CATALOG = "ix_employer_contract_items_catalog_item_id"


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    # Ebeveyn (yalniz FK hedefi) once, SRE; ALTER edilen kalem tablosu sonra, AE.
    bind.execute(sa.text("LOCK TABLE ev_catalog_items IN SHARE ROW EXCLUSIVE MODE"))
    bind.execute(sa.text("LOCK TABLE employer_contract_items IN ACCESS EXCLUSIVE MODE"))

    op.add_column(TABLE, sa.Column("catalog_item_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        TABLE,
        sa.Column(
            "price_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    # En iyi yaklasik: eski semada fiyat degisim zamani tutulmuyordu.
    bind.execute(sa.text(f"UPDATE {TABLE} SET price_changed_at = updated_at"))

    op.create_foreign_key(
        FK_CATALOG, TABLE, "ev_catalog_items", ["catalog_item_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index(
        IX_CATALOG,
        TABLE,
        ["catalog_item_id"],
        unique=False,
        postgresql_where=sa.text("catalog_item_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        IX_CATALOG, table_name=TABLE, postgresql_where=sa.text("catalog_item_id IS NOT NULL")
    )
    op.drop_constraint(FK_CATALOG, TABLE, type_="foreignkey")
    op.drop_column(TABLE, "price_changed_at")
    op.drop_column(TABLE, "catalog_item_id")

"""TKL-B6.1b — teklif→proje donusturme zemini: `offers` kolonlari + `ev_contract_item_rates`

Karar kaynagi: TKL-PLAN §4.3-§4.5, `olcum-B6.md` §6, §8.

## Ne yapar
* `offers`: `project_id` (FK `projects` ON DELETE RESTRICT, NULL) + `uq_offers_project_id`
  (bir proje TEK tekliften dogar) + `converted_at` (timestamptz NULL) + `converted_by_user_id`
  (FK `users` ON DELETE SET NULL) + CHECK `ck_offers_conversion_pair`:
  `(project_id IS NULL) = (converted_at IS NULL)` — donusturme izi ya TAM ya HIC.
* `ev_contract_item_rates` (EV uzanti tablosu): `contract_item_id` PK + FK
  `employer_contract_items` ON DELETE CASCADE; `unit_mhr` Numeric(12,4) > 0; `source`
  (`ev_rate_source`; deger `'offer'` burada TUR olarak anilir,
  VERI YAZILMAZ); created_at/updated_at.
  `catalog_item_id` kolonu YOK: sozlesme kaleminin katalog bagi cekirdekte ve sabittir (turev kopya
  ayrisirdi).

## ON DELETE secimi (SABAH ONAYI adayi)
`offers.project_id` RESTRICT: SET NULL secilseydi proje silindiginde `project_id` NULL olur ama
`converted_at` dolu kalir → CHECK ihlali (silme zaten patlar, anlamsiz hata). RESTRICT ile
donusturulmus projenin silinmesi acik FK hatasiyla (IntegrityError → 409 isleyicisi)
engellenir; teklif arsivinin
"hangi projeye donustu" izi kaybolmaz. Projeyi yine de silmek isteyen once ilisikligi kaldirmalidir
(B6.2 icin: silme yolu onceden 409 donmeli).

## 🔴 `'offer'` degeri bu migration'da KULLANILMAZ
`f2a6c8e0b4d7` (bir ONCEKI revizyon, AYRI islem; `transaction_per_migration=True`) degeri ekler.
Bu migration `ev_contract_item_rates.source` kolonunu `ev_rate_source` TURUNDE kurar ama hicbir
satir yazmaz, CHECK/DEFAULT'ta `'offer'` literali gecmez → PG 16'da `unsafe use of new value` YOK.

## Kilit analizi
`SET LOCAL lock_timeout = '10s'` HER DDL'den once (10 sn'de kilit alinamazsa migration DUSER).
* `ALTER TABLE offers` TEK ifade: ACCESS EXCLUSIVE baştan, tek seferde (kolon ekleme meta veri,
  tablo yeniden yazilmaz; FK/UQ/CHECK dogrulamasi tum satirlar icin NULL → kisa). Yukseltme riski
  yok; FK hedefleri `projects` ve `users` ayni ifadenin icinde `SHARE ROW EXCLUSIVE` alir
  (yalniz yazmalari kisa bekletir; B5 migration'inda OLCULDU).
* `CREATE TABLE ev_contract_item_rates`: yeni tablo; FK hedefi `employer_contract_items` icin
  `SHARE ROW EXCLUSIVE` (yalniz yazmalari bekletir) — referans tabloya baska kilit alinmaz.

## Dagitim notu
Eski kodla uyumlu: eski kod yeni kolonlari bilmez (NULL kalir; CHECK ciftlerde NULL=NULL saglanir).
Yeni kod eski semayla ACILMAZ → migration ONCE.

## Downgrade
Donusturulmus teklif (`project_id` dolu) VARSA durur (iz kaybi: sessiz silinmez).
`ev_contract_item_rates`
bos olmali DEGIL (tohum tablosu tureviktir; sozlesme kaleminden yeniden uretilebilir) → dusurulur.
Sonra `offers` kolonlari tek `ALTER TABLE` ile dusurulur.

Revision ID: a9d3b5f7c1e8
Revises: f2a6c8e0b4d7
Create Date: 2026-10-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a9d3b5f7c1e8"
down_revision: str | Sequence[str] | None = "f2a6c8e0b4d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))

    # TEK ALTER ifadesi: `offers` kilidi bastan, tek seferde.
    bind.execute(
        sa.text(
            "ALTER TABLE offers "
            "ADD COLUMN project_id uuid, "
            "ADD COLUMN converted_at timestamptz, "
            "ADD COLUMN converted_by_user_id uuid, "
            "ADD CONSTRAINT fk_offers_project_id_projects "
            "FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT, "
            "ADD CONSTRAINT fk_offers_converted_by_user_id_users "
            "FOREIGN KEY (converted_by_user_id) REFERENCES users (id) ON DELETE SET NULL, "
            "ADD CONSTRAINT uq_offers_project_id UNIQUE (project_id), "
            "ADD CONSTRAINT ck_offers_conversion_pair "
            "CHECK ((project_id IS NULL) = (converted_at IS NULL))"
        )
    )

    op.create_table(
        "ev_contract_item_rates",
        sa.Column(
            "contract_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "employer_contract_items.id",
                ondelete="CASCADE",
                name="fk_ev_cir_contract_item_id_employer_contract_items",
            ),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("unit_mhr", sa.Numeric(12, 4), nullable=False),
        sa.Column(
            "source",
            postgresql.ENUM(name="ev_rate_source", create_type=False),
            nullable=False,
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
        sa.CheckConstraint("unit_mhr > 0", name="ck_ev_contract_item_rates_unit_mhr_positive"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    donusen = bind.execute(
        sa.text("SELECT count(*) FROM offers WHERE project_id IS NOT NULL")
    ).scalar_one()
    if donusen:
        raise RuntimeError(
            f"Downgrade durdu: {donusen} teklif projeye donusturulmus (offers.project_id dolu). "
            "Donusturme izi sessizce silinmez; once iliskiyi elle karara baglayin."
        )
    op.drop_table("ev_contract_item_rates")
    bind.execute(
        sa.text(
            "ALTER TABLE offers "
            "DROP CONSTRAINT ck_offers_conversion_pair, "
            "DROP CONSTRAINT uq_offers_project_id, "
            "DROP CONSTRAINT fk_offers_converted_by_user_id_users, "
            "DROP CONSTRAINT fk_offers_project_id_projects, "
            "DROP COLUMN converted_by_user_id, "
            "DROP COLUMN converted_at, "
            "DROP COLUMN project_id"
        )
    )

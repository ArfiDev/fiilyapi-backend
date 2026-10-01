"""TKL-B2 — is kalemi katalogu: otomatik poz no + disiplin sayaci + referans fiyat kolonlari

Karar kaynagi: TKL-PLAN §2.1a + kullanici kararlari T21-T24 (2026-10-01).

## Ne ekler
* `ev_disciplines.poz_counter` INT NOT NULL DEFAULT 0 (+ `ck_ev_disciplines_poz_counter_nonneg`):
  disiplinde VERILEN SON sira (monoton sayac; numara asla yeniden kullanilmaz).
* `ev_catalog_items.poz_no` VARCHAR(32) NOT NULL + `uq_ev_catalog_items_poz_no` (sirket
  genelinde tekil). Bicim: `disiplin kodu + "-" + EN AZ 4 hane` (`MIM-0001`).
* `ev_catalog_items.ref_price` NUMERIC(18,2) NULL (+ `ck_ev_catalog_items_ref_price_nonneg`:
  `ref_price >= 0`) ve `price_updated_at` TIMESTAMPTZ NULL.

## Upgrade (tek migration, `transaction_per_migration=True` → hepsi ya da hicbiri)
1. `SET LOCAL lock_timeout = '10s'` + TEK ifadede `LOCK TABLE ev_disciplines,
   ev_catalog_items IN ACCESS EXCLUSIVE MODE` (sabit sira, kilit YUKSELTMESI YOK): numaralama
   suresince eszamanli OKUMA da YAZMA da bekler (ACCESS EXCLUSIVE okumayi da bekletir; pencere
   28 satirlik islem kadar kisadir). Neden SHARE ROW EXCLUSIVE degil: ardindan gelen
   `ADD COLUMN` ACCESS EXCLUSIVE ister; once okuyup sonra yazan eski-konteyner islemiyle
   kilit YUKSELTME DEADLOCK'u olurdu (kanit: TKL-B2.3 curutme betigi). Kilit 10 sn icinde
   alinamazsa migration DUSER → konteyner acilmaz (fail-closed), yeniden dagitim; uzun bir
   AE kuyrugunun canli istekleri sonsuza dek bekletmesi onlenir.
2. Kolonlar NULL'li eklenir (`poz_counter` DEFAULT 0).
3. Mevcut kalemler (canlida 28) disiplin icinde `created_at ASC, name_key ASC, id ASC`
   sirasiyla 1..n numaralanir. Siralama PYTHON'da yapilir (DB harmanlamasina bagli
   `ORDER BY name_key` KULLANILMAZ — CI/uretim harmanlama farki sahte fark yaratirdi; Python
   kod noktasi sirasi her ortamda ayni). Biçim fonksiyonu burada DONMUS KOPYADIR
   (`_format_poz_no`); migration `app`i IMPORT ETMEZ (KATALOG-UQ kanonu).
4. Sayaclar yazilir: disiplinde n kalem varsa `poz_counter = n`, kalemsizse 0.
5. FAIL-CLOSED: numarasiz satir kalirsa `RuntimeError` — islem geri alinir, hicbir sey
   degismez; Dockerfile `alembic upgrade head && uvicorn` oldugu icin konteyner ACILMAZ.
6. `poz_no` NOT NULL + UQ, CHECK'ler, `poz_counter` NOT NULL.

## Downgrade
Iki CHECK, UQ ve dort kolon (`poz_no`, `ref_price`, `price_updated_at`, `poz_counter`)
duser. Verilmis numaralar ve fiyatlar KAYBOLUR (bilincli: eski semada karsiligi yok).

## Dagitim / kilit notu
Adim 1'in kilidi islem sonuna kadar tutulur (28 satirda saniyenin kesri; okuma da bekler).
Railway'de migration
ESKI konteyner trafik alirken kosar: pencerede ESKI kodla katalog OLUSTURMA istegi `poz_no`
NOT NULL ihlaliyle DUSER, bozuk veri YAZILMAZ — kabul edilmis risk (KARARLAR, TKL-B2).
Eski kodla disiplin degistiren/kodu degistiren PATCH numarayi yenilemez; pencere kisa, islem
nadir (yonetici isi). Eski kodla pencere icinde kalem tasima / disiplin kodu degistirme
DEGISMEZI (onek = guncel kod) bozabilir → DAGITIM SONRASI degismez denetimi kosulur
(testlerdeki `VIOLATIONS_SQL`: onek <> guncel kod + '-' olan kalem sayisi 0 olmali).

Revision ID: a2c4e6f81b3d
Revises: 7e3b1c9a4f20
Create Date: 2026-10-01

"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "a2c4e6f81b3d"
down_revision: str | Sequence[str] | None = "7e3b1c9a4f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UQ_POZ_NO = "uq_ev_catalog_items_poz_no"
CK_REF_PRICE = "ck_ev_catalog_items_ref_price_nonneg"
CK_POZ_COUNTER = "ck_ev_disciplines_poz_counter_nonneg"

#: Poz no sayi kisminin EN AZ hane sayisi (DONMUS: `app.modules.catalog.service.POZ_MIN_DIGITS`).
POZ_MIN_DIGITS = 4


def _format_poz_no(code: str, seq: int) -> str:
    """DONMUS KOPYA (`app.modules.catalog.service.format_poz_no`)."""
    return f"{code}-{seq:0{POZ_MIN_DIGITS}d}"


def _number_items(
    disciplines: Sequence[tuple[Any, str]], items: Sequence[tuple[Any, Any, Any, str]]
) -> tuple[dict[Any, str], dict[Any, int]]:
    """Saf numaralama. `disciplines`: (id, code); `items`: (id, discipline_id, created_at,
    name_key). Donus: (kalem id → poz no, disiplin id → sayac).

    Sira disiplin icinde `created_at, name_key, id` (Python karsilastirmasi; id UUID metni).
    """
    codes = {disc_id: code for disc_id, code in disciplines}
    by_discipline: dict[Any, list[tuple[Any, Any, str, str]]] = {d: [] for d in codes}
    for item_id, disc_id, created_at, name_key in items:
        by_discipline[disc_id].append((created_at, name_key, str(item_id), item_id))
    numbers: dict[Any, str] = {}
    counters: dict[Any, int] = {}
    for disc_id, group in by_discipline.items():
        group.sort(key=lambda row: (row[0], row[1], row[2]))
        for seq, (_created, _name_key, _id_text, item_id) in enumerate(group, start=1):
            numbers[item_id] = _format_poz_no(codes[disc_id], seq)
        counters[disc_id] = len(group)
    return numbers, counters


def _assert_every_item_numbered(bind: sa.engine.Connection) -> None:
    unnumbered = bind.execute(
        sa.text("SELECT count(*) FROM ev_catalog_items WHERE poz_no IS NULL")
    ).scalar_one()
    if unnumbered:
        raise RuntimeError(
            f"TKL-B2 upgrade DURDU: {unnumbered} katalog kaleminde poz_no uretilemedi; "
            "hicbir sey degistirilmedi. "
            "(catalog items left without poz_no; migration aborted, nothing changed)"
        )


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    bind.execute(sa.text("LOCK TABLE ev_disciplines, ev_catalog_items IN ACCESS EXCLUSIVE MODE"))

    op.add_column("ev_catalog_items", sa.Column("poz_no", sa.String(length=32), nullable=True))
    op.add_column(
        "ev_catalog_items", sa.Column("ref_price", sa.Numeric(precision=18, scale=2), nullable=True)
    )
    op.add_column(
        "ev_catalog_items",
        sa.Column("price_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ev_disciplines",
        sa.Column("poz_counter", sa.Integer(), server_default=sa.text("0"), nullable=True),
    )

    disciplines = [tuple(r) for r in bind.execute(sa.text("SELECT id, code FROM ev_disciplines"))]
    items = [
        tuple(r)
        for r in bind.execute(
            sa.text("SELECT id, discipline_id, created_at, name_key FROM ev_catalog_items")
        )
    ]
    numbers, counters = _number_items(disciplines, items)
    if numbers:
        bind.execute(
            sa.text("UPDATE ev_catalog_items SET poz_no = :poz_no WHERE id = :id"),
            [{"id": item_id, "poz_no": poz_no} for item_id, poz_no in numbers.items()],
        )
    if counters:
        bind.execute(
            sa.text("UPDATE ev_disciplines SET poz_counter = :n WHERE id = :id"),
            [{"id": disc_id, "n": n} for disc_id, n in counters.items()],
        )

    _assert_every_item_numbered(bind)

    op.alter_column("ev_catalog_items", "poz_no", nullable=False)
    op.create_unique_constraint(UQ_POZ_NO, "ev_catalog_items", ["poz_no"])
    op.create_check_constraint(CK_REF_PRICE, "ev_catalog_items", "ref_price >= 0")
    op.alter_column("ev_disciplines", "poz_counter", nullable=False)
    op.create_check_constraint(CK_POZ_COUNTER, "ev_disciplines", "poz_counter >= 0")


def downgrade() -> None:
    op.drop_constraint(CK_POZ_COUNTER, "ev_disciplines", type_="check")
    op.drop_column("ev_disciplines", "poz_counter")
    op.drop_constraint(CK_REF_PRICE, "ev_catalog_items", type_="check")
    op.drop_constraint(UQ_POZ_NO, "ev_catalog_items", type_="unique")
    op.drop_column("ev_catalog_items", "price_updated_at")
    op.drop_column("ev_catalog_items", "ref_price")
    op.drop_column("ev_catalog_items", "poz_no")

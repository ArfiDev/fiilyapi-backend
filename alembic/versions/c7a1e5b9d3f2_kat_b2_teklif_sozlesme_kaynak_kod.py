"""KAT-B2.1 — teklif ve isveren sozlesme kalemlerine Bakanlik kaynak kodu (SNAPSHOT)

Karar kaynagi: KAT-PLAN §6 (K1-K3). Katalogdaki `source_code` (KAT-B1) sonradan degisebilir ya da
silinebilir; teklif/sozlesme kalemi OLUSTUGU ANIN kodunu KOPYA olarak tasir (`poz_no` snapshot
kanonu gibi). Sozlesme `code`una DOKUNULMAZ (K1).

## Ne ekler
* `offer_items.source_code` VARCHAR(32) NULL
* `employer_contract_items.source_code` VARCHAR(32) NULL
Kisit/indeks YOK (kopya; tekillik katalogdadir).

## Backfill (mevcut satirlar; "ekleme anindaki kod" bilinemez → BUGUNKU katalog kodu)
* `offer_items` ← `ev_catalog_items.source_code` (`catalog_item_id` NOT NULL → hepsi eslesir).
* `employer_contract_items` ← katalog, YALNIZ `catalog_item_id IS NOT NULL`; baglisiz NULL
  kalir (K2).
Yeni kayitlar icin K3 (donusturmede kaynak teklif snapshot'i) uygulama kodundadir.

## Kilit (KARARLAR §2 kanonu + TKL-B3.3 inceltmesi + KAT-B2.1b: HEP-YA-HIC)
`LOCK TABLE a, b` tablolari TEK SEFERDE DEGIL, TEK TEK ve sirayla alir; iki ayri kilit ifadesi de
sirayla alinir. Bekleyen bir kilit elde tutulan oburlerini BIRAKMAZ → eski konteyner trafigiyle
gercek deadlock olculdu (A: katalog YAZ → kalem OKU; B: teklif kalemi OKU → sozlesme kalemi YAZ;
her iki yon uygulamada var, hicbir sabit sira ikisini birden kurtarmaz).
Cozum: `SET LOCAL lock_timeout = '10s'` (ALTER/backfill icin son savunma), sonra ALTER/backfill'den
ONCE, yukseltmesiz, HEP-YA-HIC: bir SAVEPOINT icinde uc kilit de NOWAIT alinir
(katalog SHARE ROW EXCLUSIVE — okumayi bloklamaz, yazmayi engeller; iki kalem tablosu ACCESS
EXCLUSIVE). Biri alinamazsa (SQLSTATE 55P03) savepoint GERI ALINIR (o ana dek alinan kilitler
birakilir → hicbir kilit BEKLENEREK tutulmaz, deadlock dongusu kurulamaz), `LOCK_RETRY_INTERVAL_S`
beklenir, yeniden denenir. `LOCK_CEILING_S` dolarsa acik hata ile migration DUSER (fail-closed,
surum eski kalir). Bedel: surekli okuma trafiginde AE kuyruga girmedigi icin ac kalabilir → tavan
sonunda yine fail-closed (eski `lock_timeout` sonucuyla ayni).
`app` IMPORT EDILMEZ. Tek migration, `transaction_per_migration=True` → hepsi ya da hicbiri.

## Downgrade
Ayni lock_timeout + hep-ya-hic kilit (iki kalem tablosu), sonra iki kolon duser; kopya kodlar
KAYBOLUR (eski semada karsiligi yok).

Revision ID: c7a1e5b9d3f2
Revises: b4e8d2f6a1c9
Create Date: 2026-10-03

"""

from __future__ import annotations

import time
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7a1e5b9d3f2"
down_revision: str | Sequence[str] | None = "b4e8d2f6a1c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OFFER_ITEMS = "offer_items"
CONTRACT_ITEMS = "employer_contract_items"
CATALOG = "ev_catalog_items"

LOCK_RETRY_INTERVAL_S = 0.2  # NOWAIT basarisizligindan sonra yeniden deneme araligi
LOCK_CEILING_S = 10.0  # toplam deneme tavani; dolunca migration duser (fail-closed)
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"

# (tablo, kip) — upgrade: katalog yalniz SRE (okumayi bloklamaz), iki kalem tablosu AE.
UPGRADE_LOCKS = (
    (CATALOG, "SHARE ROW EXCLUSIVE"),
    (CONTRACT_ITEMS, "ACCESS EXCLUSIVE"),
    (OFFER_ITEMS, "ACCESS EXCLUSIVE"),
)
DOWNGRADE_LOCKS = (
    (CONTRACT_ITEMS, "ACCESS EXCLUSIVE"),
    (OFFER_ITEMS, "ACCESS EXCLUSIVE"),
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

    op.add_column(OFFER_ITEMS, sa.Column("source_code", sa.String(length=32), nullable=True))
    op.add_column(CONTRACT_ITEMS, sa.Column("source_code", sa.String(length=32), nullable=True))

    bind.execute(
        sa.text(
            "UPDATE offer_items AS i SET source_code = c.source_code "
            "FROM ev_catalog_items AS c WHERE c.id = i.catalog_item_id "
            "AND c.source_code IS NOT NULL"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE employer_contract_items AS i SET source_code = c.source_code "
            "FROM ev_catalog_items AS c WHERE c.id = i.catalog_item_id "
            "AND i.catalog_item_id IS NOT NULL AND c.source_code IS NOT NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    _acquire_all_or_nothing(bind, DOWNGRADE_LOCKS)
    op.drop_column(CONTRACT_ITEMS, "source_code")
    op.drop_column(OFFER_ITEMS, "source_code")

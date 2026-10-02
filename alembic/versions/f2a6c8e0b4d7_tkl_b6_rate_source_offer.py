"""TKL-B6.1a — `ev_rate_source` enum'una `'offer'` degeri (adam-saat kaynagi: teklif)

Karar kaynagi: TKL-PLAN §4.3-§4.5 (oran kaynak etiketi, S-R6), `olcum-B6.md` §4.

## Ne yapar
YALNIZ `ALTER TYPE ev_rate_source ADD VALUE IF NOT EXISTS 'offer'`. Yeni tablo/kolon YOKTUR.

## 🔴 NEDEN AYRI BIR MIGRATION (OLCULDU)
`alembic/env.py` `transaction_per_migration=True`dir. Postgres, DAHA ONCE (baska bir islemde)
yaratilmis bir enum'a `ADD VALUE` ile eklenen degerin AYNI ISLEMDE KULLANILMASINI yasaklar:
`unsafe use of new value "offer" of enum type ev_rate_source` (PG 12-16; PG 17+ gevsetildi).
`ev_rate_source` `46a82e4b271c`de yaratilmistir. Bu yuzden `'offer'` degerini KULLANAN hicbir
sey (server_default, CHECK, tohum, INSERT) BU migration'a KONMAZ; bir sonraki revizyon
(`a9d3b5f7c1e8`) yalniz yeni tablonun kolon TURU olarak tipi anar (deger yazmaz). Yerel test
sunucusu da PG 16.14'tur (CI ile ayni ana surum) — kusur yerelde de gorulur.
Emsal: `b7c8d9e0f1a2` (MU-3D).

`IF NOT EXISTS`: yarim kalmis bir turdan sonra tekrar kosulmayi guvenli kilar. Deger SONA
eklenir (`catalog, history, manual, offer`).

## Kilit analizi
`ADD VALUE` tablo kilidi ALMAZ (yalniz tip katalogu); `SET LOCAL lock_timeout = '10s'` yine de
yazilir (kanon). Yazma/okuma bekletmez, eski kodla uyumludur (eski kod 'offer' bilmez/yazmaz).
Yeni kod eski semayla ACILMAZ degil: 'offer' yazmaya kalkana dek fark yoktur → migration ONCE.

## DOWNGRADE — tip BASTAN KURULUR (ELLE kosulur, acilis yolunda DEGIL)
Postgres enum'dan deger SILEMEZ. Tipi kullanan TUM kolonlar `pg_attribute`tan OLCULUR
(bugun `ev_leaf_settings.rate_source`, `ev_baseline_leaves.rate_source`; B6.1b'nin
`ev_contract_item_rates.source` kolonu bir ONCEKI adimda dusurulmustur — ama kesif dinamiktir,
ileride eklenen kolon da kapsanir). Kullanan tablolar `ACCESS EXCLUSIVE` ile KILITLENIR
(ada gore sirali → kilit sirasi sabit, ucuncu islemle kilitlenme riski yok) ve SONRA veri kapisi
kosar: herhangi bir satir `'offer'` tasiyorsa FAIL-CLOSED `RuntimeError` (donusturulecegi bir
deger YOKTUR; sessiz 'manual'a cevirmek kaynagi yalanlardi). Sonra yeni tip olusturulur,
kolonlar `USING col::text::tip` ile cevrilir (tablo yeniden yazilir; tablolar kucuk), eski tip
dusurulur ve yeni tip eski ada alinir. Kolonlarin sunucu varsayilani YOKTUR (olculdu).

Elle yazilmistir (autogenerate DEGIL) — repo deseni. `app` IMPORT EDILMEZ.

Revision ID: f2a6c8e0b4d7
Revises: e4b7c9d1a3f5
Create Date: 2026-10-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2a6c8e0b4d7"
down_revision: str | Sequence[str] | None = "e4b7c9d1a3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUM_NAME = "ev_rate_source"
NEW_MEMBER = "offer"
#: Downgrade'in yeniden kuracagi tip — bu migration ONCESI uye kumesi, SIRASIYLA.
PREVIOUS_MEMBERS: tuple[str, ...] = ("catalog", "history", "manual")

_USING_COLUMNS_SQL = """
SELECT c.relname, a.attname
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE a.atttypid = to_regtype(:tip)
  AND a.attnum > 0 AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p')
  AND n.nspname = current_schema()
ORDER BY c.relname, a.attname
"""


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    # 🔴 Yeni deger BU migration'da KULLANILMAZ — kullanilsaydi `unsafe use of new value`.
    op.execute(f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{NEW_MEMBER}'")


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))

    kolonlar = [(r[0], r[1]) for r in bind.execute(sa.text(_USING_COLUMNS_SQL), {"tip": ENUM_NAME})]
    # Once KILIT (veri kapisi ile yeniden yazim arasinda yeni 'offer' satiri girmesin).
    for tablo in sorted({t for t, _ in kolonlar}):
        bind.execute(sa.text(f"LOCK TABLE {tablo} IN ACCESS EXCLUSIVE MODE"))

    # 🔴 VERI KAPISI — deger kullanilirken geri donus IMKANSIZDIR.
    for tablo, kolon in kolonlar:
        kalan = bind.execute(
            sa.text(f"SELECT count(*) FROM {tablo} WHERE {kolon}::text = :uye"),
            {"uye": NEW_MEMBER},
        ).scalar_one()
        if kalan:
            raise RuntimeError(
                f"downgrade durduruldu: `{tablo}.{kolon}` icinde {kalan} satir "
                f"'{NEW_MEMBER}' degerini tasiyor. Postgres enum'dan deger SILEMEZ; tip "
                "bastan kurulacagi icin bu satirlarin donusturulecegi bir deger YOKTUR. "
                "Once kaynagi elle karara baglayin (sessiz 'manual' donusumu kaynagi yalanlar)."
            )

    eski = ", ".join(f"'{uye}'" for uye in PREVIOUS_MEMBERS)
    op.execute(f"ALTER TYPE {ENUM_NAME} RENAME TO {ENUM_NAME}_old")
    op.execute(f"CREATE TYPE {ENUM_NAME} AS ENUM ({eski})")
    for tablo, kolon in kolonlar:
        op.execute(
            f"ALTER TABLE {tablo} ALTER COLUMN {kolon} "
            f"TYPE {ENUM_NAME} USING {kolon}::text::{ENUM_NAME}"
        )
    op.execute(f"DROP TYPE {ENUM_NAME}_old")

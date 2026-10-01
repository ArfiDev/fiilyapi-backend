"""BLF-B1 — `sections.section_type` PG enum → sirket geneli `section_types` tablosuna FK

BOLUM-FORMU-SPEC.md F-b: bolum tipi listesi artik GENISLETILEBILIR (kullanici "+ Yeni tip
ekle" ile sirket geneline tip ekler). Sabit 7 degerli PG enum `section_type` bunu
tasiyamaz; yerine `section_types` tablosu acilir, mevcut 7 tip TOHUMLANIR ve
`sections.section_type` → `sections.section_type_id` (FK) olarak VERI KAYBISIZ tasinir.

## Upgrade (tek migration, `transaction_per_migration=True` → hepsi ya da hicbiri)
1. `section_types` (id, name, name_key, sort_order, created_at) + `uq_section_types_name_key`
   + `ck_section_types_name_nonblank` + `ck_section_types_name_key_nonblank`; 7 tohum SABIT
   UUID'lerle (asagida `SEEDS`).
2. `sections.section_type_id` UUID NULL + `ix_sections_section_type_id` +
   `fk_sections_section_type_id` → `section_types.id` ON DELETE RESTRICT (kullanilan tip
   silinemez).
3. Eski enum degeri → tohum id eslemesi (`SEEDS` ile birebir, tek UPDATE).
4. FAIL-CLOSED dogrulama: `section_type IS NOT NULL AND section_type_id IS NULL` satiri
   varsa (eslenmemis enum degeri = veri kaybi) ACIK HATAYLA durur; islem geri alinir,
   HICBIR SEY degismez. Dockerfile `alembic upgrade head && uvicorn` oldugu icin bu durumda
   konteyner acilmaz — sessiz veri kaybi yerine bilincli kirmizi.
5. `sections.section_type` kolonu ve `DROP TYPE section_type` (ENUM tipi kolonla birlikte
   SILINMEZ — `d4e5f6a7b8c9` dersi; unutulursa downgrade'in `CREATE TYPE`i patlar).

Adim 2'deki `ADD COLUMN` `sections` uzerinde ACCESS EXCLUSIVE kilit alir ve islem sonuna
kadar tutar: adim 3 ile 4 arasina eszamanli yazma GIREMEZ.

## Downgrade
1. ONCE kontrol: tohum OLMAYAN bir tipe bagli bolum varsa ACIK HATAYLA durur (o tipin eski
   enumda karsiligi yok; sessizce NULL'a cevirmek veri kaybidir). Kullanicinin tohum-disi
   tipi bolumden ayirmasi (ya da tohum tipe tasimasi) gerekir.
2. `section_type` enum tipini (orijinal 7 deger, orijinal sira) ve kolonu geri kurar.
3. Tohum id → enum degeri geri eslemesi. Adim 1 tohum-disi bagi disladigi icin her bagli
   satir burada eslenir (ikinci, ayni kosulu tekrarlayan bir bekci BILINCLI konmadi — adim
   1'in mutasyon testi onu tek basina kanitlar).
4. FK / indeks / `section_type_id` / `section_types` tablosu duser. Hicbir bolume bagli
   OLMAYAN tohum-disi tipler tabloyla birlikte gider (bilincli; bolum verisi kaybolmaz).

## Normalize anahtar (KATALOG-UQ dersi)
`name_key` = `app.core.labels.normalize_label(name)` (v2 kurali). Migration
`app`i IMPORT ETMEZ (modeller/kural ileride degisir, migration donmus kalmali) — anahtarlar
2026-10-01'de bir kerelik hesaplanip asagiya SABIT yazildi. Esitlik bekcisi:
`tests/modules/sites/test_blf_b1_migration.py::test_seed_name_keys_equal_normalize_label`.

Revision ID: 7e3b1c9a4f20
Revises: d5c0b0a1e7f3
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7e3b1c9a4f20"
down_revision: str | Sequence[str] | None = "d5c0b0a1e7f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "section_types"
ENUM_NAME = "section_type"
FK_NAME = "fk_sections_section_type_id"
INDEX_NAME = "ix_sections_section_type_id"
UQ_NAME = "uq_section_types_name_key"
CK_NAME = "ck_section_types_name_nonblank"
CK_KEY_NAME = "ck_section_types_name_key_nonblank"

#: (id, eski enum degeri, ad, name_key, sort_order) — sira `d4e5f6a7b8c9.SECTION_TYPE_LABELS`
#: ile BIREBIR (downgrade enum'u bu sirayla geri kurar). UUID'ler SABITTIR: downgrade geri
#: eslemeyi bu id'lerle yapar, tekrar kosan upgrade ayni id'leri uretir.
SEEDS: tuple[tuple[str, str, str, str, int], ...] = (
    (
        "cf92b5d6-3f36-496a-88cb-b7251f3d83ad",
        "foundation_infra",
        "Temel & Altyapı",
        "temel & altyapı",
        1,
    ),
    ("2cc298a7-84bd-4efe-87c9-8ecd7e3fa184", "structural", "Kaba İnşaat", "kaba inşaat", 2),
    ("89acade8-e402-4990-9ad2-fdd944cb082c", "finishing", "İnce İşler", "ince işler", 3),
    ("e75c299d-8815-436c-9be1-f47a1bc53efe", "facade_roof", "Cephe & Çatı", "cephe & çatı", 4),
    (
        "bf7ae014-0e5e-4244-9ce1-87909f9f6ab1",
        "mep",
        "Mekanik / Elektrik",
        "mekanik / elektrik",
        5,
    ),
    ("9ada18dd-ddb5-4af6-947a-cc8fe082afd1", "landscape", "Peyzaj", "peyzaj", 6),
    (
        "c40ad7f6-9a23-4d3d-9385-9aacb3284dff",
        "handover",
        "Teslimat & Kabul",
        "teslimat & kabul",
        7,
    ),
)

ENUM_LABELS: tuple[str, ...] = tuple(seed[1] for seed in SEEDS)
SEED_IDS: tuple[str, ...] = tuple(seed[0] for seed in SEEDS)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _seed_id_list() -> str:
    return ", ".join(f"{_sql_literal(seed_id)}::uuid" for seed_id in SEED_IDS)


def _create_section_types() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("name_key", sa.String(length=400), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name_key", name=UQ_NAME),
        sa.CheckConstraint("length(btrim(name)) > 0", name=CK_NAME),
        sa.CheckConstraint("name_key <> ''", name=CK_KEY_NAME),
    )
    rows = ",\n".join(
        f"({_sql_literal(seed_id)}::uuid, {_sql_literal(name)}, {_sql_literal(key)}, {order})"
        for seed_id, _enum, name, key, order in SEEDS
    )
    op.execute(f"INSERT INTO {TABLE} (id, name, name_key, sort_order) VALUES\n{rows}")


def _map_enum_to_seed_ids() -> None:
    whens = " ".join(
        f"WHEN {_sql_literal(enum_value)} THEN {_sql_literal(seed_id)}::uuid"
        for seed_id, enum_value, _name, _key, _order in SEEDS
    )
    op.execute(
        f"UPDATE sections SET section_type_id = CASE section_type::text {whens} END "
        "WHERE section_type IS NOT NULL"
    )


def _assert_every_enum_row_mapped() -> None:
    unmapped = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT section_type::text AS value, count(*) AS n FROM sections "
                "WHERE section_type IS NOT NULL AND section_type_id IS NULL "
                "GROUP BY section_type ORDER BY section_type::text"
            )
        )
        .all()
    )
    if unmapped:
        detail = ", ".join(f"{row.value}={row.n}" for row in unmapped)
        raise RuntimeError(
            "BLF-B1 upgrade DURDU: eslenmemis bolum tipi (enum) degerleri var — "
            f"{detail}. Tasima veri kaybi yaratirdi; hicbir sey degistirilmedi. "
            "(unmapped section_type enum values; migration aborted, nothing changed)"
        )


def upgrade() -> None:
    _create_section_types()

    op.add_column("sections", sa.Column("section_type_id", sa.UUID(), nullable=True))
    op.create_index(INDEX_NAME, "sections", ["section_type_id"], unique=False)
    op.create_foreign_key(
        FK_NAME, "sections", TABLE, ["section_type_id"], ["id"], ondelete="RESTRICT"
    )

    _map_enum_to_seed_ids()
    _assert_every_enum_row_mapped()

    op.drop_column("sections", "section_type")
    op.execute(f"DROP TYPE {ENUM_NAME}")


def _assert_no_section_on_non_seed_type() -> None:
    blocking = (
        op.get_bind()
        .execute(
            sa.text(
                f"SELECT t.name AS name, count(*) AS n FROM sections s "
                f"JOIN {TABLE} t ON t.id = s.section_type_id "
                f"WHERE t.id NOT IN ({_seed_id_list()}) "
                "GROUP BY t.name ORDER BY t.name"
            )
        )
        .all()
    )
    if blocking:
        detail = ", ".join(f"'{row.name}'={row.n}" for row in blocking)
        raise RuntimeError(
            "BLF-B1 downgrade DURDU: tohum disi (sonradan eklenmis) bolum tipine bagli "
            f"bolumler var — {detail}. Eski enumda karsiligi yok; NULL'a cevirmek veri "
            "kaybidir. Once bu bolumleri tohum tiplerden birine tasiyin. Hicbir sey "
            "degistirilmedi. (sections reference non-seed section types; downgrade aborted)"
        )


def downgrade() -> None:
    _assert_no_section_on_non_seed_type()

    labels = ", ".join(_sql_literal(label) for label in ENUM_LABELS)
    op.execute(f"CREATE TYPE {ENUM_NAME} AS ENUM ({labels})")
    op.execute(f"ALTER TABLE sections ADD COLUMN section_type {ENUM_NAME}")

    whens = " ".join(
        f"WHEN {_sql_literal(seed_id)}::uuid THEN {_sql_literal(enum_value)}::{ENUM_NAME}"
        for seed_id, enum_value, _name, _key, _order in SEEDS
    )
    op.execute(
        f"UPDATE sections SET section_type = CASE section_type_id {whens} END "
        "WHERE section_type_id IS NOT NULL"
    )

    op.drop_constraint(FK_NAME, "sections", type_="foreignkey")
    op.drop_index(INDEX_NAME, table_name="sections")
    op.drop_column("sections", "section_type_id")
    op.drop_table(TABLE)

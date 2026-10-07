"""IZN-B6c — eski izin tabloları ve enum tipleri DÜŞER

Düşenler: `role_permissions`, `modules`, `user_project_access`, `user_disciplines` ve
`scope`, `access_level`, `module_group` enum tipleri.

Önkoşul (iki sürüm kanonu): IZN-B6b kodu bu tabloları okumayı bıraktı ve canlıda çalışıyor.
Yerlerini `role_page_permissions` (sayfa izinleri), `project_members` + `users.all_projects`
(proje ekibi) ve `project_member_disciplines` aldı. Bu migration YALNIZ DROP eder.

## Upgrade
1. `SET LOCAL lock_timeout`: kilit alınamazsa 10 sn sonra düşer (deploy sonsuza dek beklemez).
2. 4 tablonun satır sayısı WARNING olarak basılır (akış DURMAZ; veri kaybı bilinçlidir).
3. DROP sırası (FK'ye göre): `role_permissions` → `user_disciplines` → `user_project_access`
   → `modules`.
4. Enum tipleri düşmeden ÖNCE `pg_attribute`'tan ölçülür: başka bir kolon hâlâ kullanıyorsa
   `RuntimeError` ile DURUR (işlem geri alınır, hiçbir şey düşmez). Kullanan yoksa DROP TYPE.

## Downgrade
Tablolar ve enum'lar BOŞ geri açılır; DDL, ilk yaratan revizyonlardan (`9b06c643996e`,
`e274019416f6`, `d5c0b0a1e7f3`) birebir alınmıştır (head öncesindeki son hâl; sonradan DDL
değiştiren revizyon yoktur: `b2c3d4e5f8a1` yalnız veri güncelledi). Veri GERİ GELMEZ.

Migration `app` IMPORT ETMEZ.

Revision ID: e6b1d9f3a7c2
Revises: d4b8f1a6c3e9
Create Date: 2026-10-07

"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e6b1d9f3a7c2"
down_revision: str | Sequence[str] | None = "d4b8f1a6c3e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

LOCK_TIMEOUT = "10s"
#: DROP sırası (FK'ye göre).
DROP_TABLES = ("role_permissions", "user_disciplines", "user_project_access", "modules")
COUNT_TABLES = ("role_permissions", "modules", "user_project_access", "user_disciplines")
ENUM_TYPES = ("scope", "access_level", "module_group")

#: İlk yaratan revizyondaki etiketler ve SIRALARI (`9b06c643996e`).
MODULE_GROUP_LABELS = ("GENEL", "SAHA", "STOK_SATINALMA", "MALI", "SISTEM")
ACCESS_LEVEL_LABELS = ("none", "view", "draft", "request", "approve", "full", "admin")
SCOPE_LABELS = ("all", "own", "project", "finance", "stock", "limited")


def _count_rows(bind: sa.Connection) -> dict[str, int]:
    return {
        table: bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608
        for table in COUNT_TABLES
    }


def _assert_types_unused(bind: sa.Connection) -> None:
    """Enum tiplerini (ve dizi tiplerini) kullanan KALAN kolon varsa RuntimeError."""
    rows = bind.execute(
        sa.text(
            "SELECT c.relname, a.attname, t.typname "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_type t ON t.oid = a.atttypid OR t.typarray = a.atttypid "
            "WHERE t.typname = ANY(:types) AND t.typtype = 'e' "
            "AND a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
            "ORDER BY c.relname, a.attname"
        ),
        {"types": list(ENUM_TYPES)},
    ).all()
    if rows:
        kullananlar = ", ".join(f"{r.relname}.{r.attname} ({r.typname})" for r in rows)
        raise RuntimeError(
            f"IZN-B6c: enum tipi başka kolonda kullanılıyor, DROP TYPE yapılmadı: {kullananlar}"
        )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    for table, count in _count_rows(bind).items():
        logger.warning("IZN-B6c: %s DROP edilecek, %d satir", table, count)
    for table in DROP_TABLES:
        op.drop_table(table)
    _assert_types_unused(bind)
    for name in ENUM_TYPES:
        op.execute(f"DROP TYPE {name}")


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    bind = op.get_bind()
    module_group = postgresql.ENUM(*MODULE_GROUP_LABELS, name="module_group", create_type=False)
    access_level = postgresql.ENUM(*ACCESS_LEVEL_LABELS, name="access_level", create_type=False)
    scope = postgresql.ENUM(*SCOPE_LABELS, name="scope", create_type=False)
    for enum in (scope, access_level, module_group):
        enum.create(bind, checkfirst=False)

    op.create_table(
        "modules",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("key", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("group", module_group, nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_modules_key"), "modules", ["key"], unique=True)

    op.create_table(
        "role_permissions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("role_id", sa.UUID(), nullable=False),
        sa.Column("module_id", sa.UUID(), nullable=False),
        sa.Column("access_level", access_level, nullable=False),
        sa.Column("scope", scope, nullable=False),
        sa.ForeignKeyConstraint(["module_id"], ["modules.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("role_id", "module_id", name="uq_role_module"),
    )
    op.create_index(
        op.f("ix_role_permissions_role_id"), "role_permissions", ["role_id"], unique=False
    )

    op.create_table(
        "user_project_access",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("all_projects", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_user_project_access_user_id"), "user_project_access", ["user_id"], unique=False
    )

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

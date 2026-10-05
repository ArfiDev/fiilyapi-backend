"""IZN-B3b — onay zinciri proje rolünden: `user_approval_roles` tablosu SÖKÜLÜR

Karar kaynağı: KARARLAR §1.7 K1 · IZN-PLAN §6 IZN-B3b. Zincir adımı rol adıyla kalır
(`approval_steps.approval_role`); adımı artık BELGENİN PROJESİNDE o role atanmış kişi
(`project_members.role.key`) ya da "Tüm projeler" işaretli ve ANA rolü o rol olan kişi onaylar.
Ayrı "onay rolü" ataması kalktı.

## Ne yapar
* Önce eski atamalar SAYILIR ve WARNING basılır (deploy günlüğünde görünür): toplam atama, kaç
  atama kullanıcının ANA rolüyle uyuşmuyor, kaç atama sahibi "Tüm projeler" DEĞİL (bu atamalar
  artık hiçbir şey onaylatmaz: adım sahipliği proje rolünden / ana roldan gelir).
* Sonra `user_approval_roles` DÜŞER. `approval_role` enum tipi KALIR (`approval_steps` kullanır).
* Canlıda proje ve zincir yoktur; açık zincir etkisi yoktur.

## Kilit
`users` (FK hedefi) SHARE ROW EXCLUSIVE ve `user_approval_roles` ACCESS EXCLUSIVE tüm kilitler
baştan, NOWAIT + savepoint ile hep-ya-hiç (kilit YÜKSELTME deadlock'u olmasın);
`SET LOCAL lock_timeout = '10s'` son savunma. Migration `app` IMPORT ETMEZ.

## Downgrade
Tabloyu (ve indeksini) yeniden açar; her kullanıcıya ANA rol anahtarı `approval_role` enum
değerlerinden biriyse o satırı yazar (YAKLAŞIK geri dönüş: proje başına rol ataması ve eski
çoklu atamalar geri gelmez). Satır alamayan kullanıcı sayısı WARNING ile bildirilir.

Revision ID: e8b4c2d6f9a1
Revises: d3a7f1c9b5e2
Create Date: 2026-10-05

"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e8b4c2d6f9a1"
down_revision: str | Sequence[str] | None = "d3a7f1c9b5e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

TABLE = "user_approval_roles"

LOCK_RETRY_INTERVAL_S = 0.2
LOCK_CEILING_S = 10.0
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
UPGRADE_LOCKS = (
    ("users", "SHARE ROW EXCLUSIVE"),
    (TABLE, "ACCESS EXCLUSIVE"),
)
DOWNGRADE_LOCKS = (
    ("users", "SHARE ROW EXCLUSIVE"),
    ("roles", "SHARE ROW EXCLUSIVE"),
)


class LockCeilingExceededError(RuntimeError):
    """Kilitler tavan süresinde alınamadı: migration geri alınır, hiçbir şey yazılmaz."""


def _sqlstate(exc: sa.exc.DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def _acquire_all_or_nothing(bind: sa.Connection, locks: tuple[tuple[str, str], ...]) -> None:
    """Tüm kilitleri NOWAIT + savepoint ile hep-ya-hiç alır; hiçbir kilit beklenerek tutulmaz."""
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
        bind.execute(sa.text("SELECT pg_sleep(:s)"), {"s": LOCK_RETRY_INTERVAL_S})


def _scalar(bind: sa.Connection, sql: str) -> int:
    return int(bind.execute(sa.text(sql)).scalar_one())


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    _acquire_all_or_nothing(bind, UPGRADE_LOCKS)

    _warn_upgrade(bind)
    op.drop_index("ix_user_approval_roles_user_id", table_name=TABLE)
    op.drop_table(TABLE)


def _warn_upgrade(bind: sa.Connection) -> None:
    """Sayımlar WARNING olarak basılır (deploy günlüğünde görünür); akışı durdurmaz."""
    total = _scalar(bind, f"SELECT count(*) FROM {TABLE}")
    mismatch = _scalar(
        bind,
        f"SELECT count(*) FROM {TABLE} a JOIN users u ON u.id = a.user_id "
        "JOIN roles r ON r.id = u.role_id WHERE r.key <> a.approval_role::text",
    )
    not_all_projects = _scalar(
        bind,
        f"SELECT count(*) FROM {TABLE} a JOIN users u ON u.id = a.user_id WHERE NOT u.all_projects",
    )
    logger.warning(
        "IZN-B3b: user_approval_roles %d atama dusuyor; %d atama kullanicinin ANA rolu ile "
        "uyusmuyor, %d atamanin sahibi 'Tum projeler' DEGIL (artik adim sahipligi yalniz proje "
        "rolunden / 'Tum projeler' + ana rolden gelir; bu atamalar hicbir adimi onaylatmaz)",
        total,
        mismatch,
        not_all_projects,
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    _acquire_all_or_nothing(bind, DOWNGRADE_LOCKS)

    op.create_table(
        TABLE,
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "approval_role",
            postgresql.ENUM(name="approval_role", create_type=False),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "approval_role", name="uq_user_approval_roles_user_role"),
    )
    op.create_index("ix_user_approval_roles_user_id", TABLE, ["user_id"])

    # YAKLAŞIK geri dönüş: ana rol anahtarı enum değerlerinden biriyse o satır yazılır.
    bind.execute(
        sa.text(
            f"INSERT INTO {TABLE} (id, user_id, approval_role) "
            "SELECT gen_random_uuid(), u.id, r.key::approval_role FROM users u "
            "JOIN roles r ON r.id = u.role_id "
            "WHERE r.key IN (SELECT e.enumlabel FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'approval_role')"
        )
    )
    restored = _scalar(bind, f"SELECT count(*) FROM {TABLE}")
    users_total = _scalar(bind, "SELECT count(*) FROM users")
    logger.warning(
        "IZN-B3b downgrade: %d kullanicinin ana rolu onay rolu -> %d atama yazildi; %d kullanici "
        "satir ALMADI. Proje basina rol atamalari ve eski cok-rollu atamalar GERI GELMEZ.",
        restored,
        restored,
        users_total - restored,
    )

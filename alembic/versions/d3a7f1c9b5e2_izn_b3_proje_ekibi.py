"""IZN-B3 — proje ekibi: users.all_projects, project_members, project_member_disciplines

Karar kaynağı: KARARLAR §1.7 (IZN: kişi yalnız ekibinde olduğu projeyi görür, rol PROJE BAŞINA,
"Tüm projeler" işaretli kişi ana rolle çalışır, disiplin proje başına) · IZN-PLAN §1.1, §1.4.

## Ne yapar
* `users.all_projects` (bool, NOT NULL, varsayılan false).
* `project_members` (user × project × rol; UQ user+project; user/project CASCADE, rol RESTRICT).
* `project_member_disciplines` (üye × disiplin; üye CASCADE, disiplin RESTRICT).
* GEÇİŞ (bugünkü verilerden; eski tablolar KALIR ama artık okunmaz/yazılmaz, B6'da düşer):
  - `user_project_access`te `all_projects=true` satırı olan kullanıcı → `users.all_projects=true`.
  - `user_project_access`in proje satırları → `project_members`, rol = kullanıcının ANA rolü
    (eski tabloda `(user_id, project_id)` UNIQUE yoktu: yinelenen satırlar TEKİLLEŞİR).
    `all_projects` kullanıcıda proje satırı yazılmaz (hedef modelde böyle kişi ekip satırı
    taşımaz); sayısı WARNING olarak basılır.
  - Global `user_disciplines` → kullanıcının HER `project_members` satırına AYNI disiplinler.
    GEREKÇELİ SEÇİM (planda belirsiz): disiplin kısıtı proje başına olduğu için hedefsiz kalan iki
    durum VARDIR ve ikisi de WARNING olarak sayılır, satırlar silinmez (B6'ya kadar durur):
    (a) `all_projects` kullanıcı (planda "disiplin kısıtı olmaz"), (b) ekip satırı olmayan kullanıcı
    (görecek projesi zaten yok). (a) kısıtı KALDIRIR: bilinçli, plan kuralı; sayı raporda görünür.

## Kilit
Tüm kilitler baştan, NOWAIT + savepoint ile hep-ya-hiç (c5e9a3b7d1f4 kalıbı; kilit YÜKSELTME
deadlock'u olmasın): `users` ALTER için ACCESS EXCLUSIVE, FK hedefleri (`projects`, `roles`,
`ev_disciplines`) ve kaynak tablolar SHARE ROW EXCLUSIVE. `SET LOCAL lock_timeout = '10s'` son
savunma. Migration `app` IMPORT ETMEZ.

## Downgrade
Geri döner: yeni tablolardaki veri eski tablolara YAZILIR (`user_project_access`: `all_projects`
kullanıcı için tek NULL-proje satırı, ekip üyeleri için proje satırları; `user_disciplines`: ekip
satırı olan kullanıcının TÜM projelerindeki disiplinlerin BİRLEŞİMİ), sonra yeni tablolar ve
kolon düşer. Kayıp (proje başına rol / proje başına ayrı disiplin) WARNING ile bildirilir.

Revision ID: d3a7f1c9b5e2
Revises: 671436c0b42a
Create Date: 2026-10-04

"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d3a7f1c9b5e2"
down_revision: str | Sequence[str] | None = "671436c0b42a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

MEMBERS_TABLE = "project_members"
MEMBER_DISC_TABLE = "project_member_disciplines"

LOCK_RETRY_INTERVAL_S = 0.2
LOCK_CEILING_S = 10.0
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
UPGRADE_LOCKS = (
    ("users", "ACCESS EXCLUSIVE"),
    ("projects", "SHARE ROW EXCLUSIVE"),
    ("roles", "SHARE ROW EXCLUSIVE"),
    ("ev_disciplines", "SHARE ROW EXCLUSIVE"),
    ("user_project_access", "SHARE ROW EXCLUSIVE"),
    ("user_disciplines", "SHARE ROW EXCLUSIVE"),
)
DOWNGRADE_LOCKS = (
    ("users", "ACCESS EXCLUSIVE"),
    ("projects", "ACCESS EXCLUSIVE"),
    ("roles", "ACCESS EXCLUSIVE"),
    ("ev_disciplines", "ACCESS EXCLUSIVE"),
    (MEMBERS_TABLE, "ACCESS EXCLUSIVE"),
    (MEMBER_DISC_TABLE, "ACCESS EXCLUSIVE"),
    ("user_project_access", "ACCESS EXCLUSIVE"),
    ("user_disciplines", "ACCESS EXCLUSIVE"),
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

    op.add_column(
        "users",
        sa.Column("all_projects", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        MEMBERS_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "project_id", name="uq_project_members_user_project"),
    )
    op.create_index("ix_project_members_project_id", MEMBERS_TABLE, ["project_id"])
    op.create_index("ix_project_members_role_id", MEMBERS_TABLE, ["role_id"])
    op.create_table(
        MEMBER_DISC_TABLE,
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("discipline_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], [f"{MEMBERS_TABLE}.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["discipline_id"], ["ev_disciplines.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("member_id", "discipline_id"),
    )
    op.create_index(
        "ix_project_member_disciplines_discipline_id", MEMBER_DISC_TABLE, ["discipline_id"]
    )

    bind.execute(
        sa.text(
            "UPDATE users SET all_projects = true WHERE EXISTS ("
            "SELECT 1 FROM user_project_access a WHERE a.user_id = users.id AND a.all_projects)"
        )
    )
    # Eski tabloda UNIQUE yoktu: (user, proje) çiftleri DISTINCT ile tekilleşir.
    bind.execute(
        sa.text(
            f"INSERT INTO {MEMBERS_TABLE} (id, user_id, project_id, role_id) "
            "SELECT gen_random_uuid(), u.id, a.project_id, u.role_id "
            "FROM (SELECT DISTINCT user_id, project_id FROM user_project_access "
            "      WHERE project_id IS NOT NULL) a "
            "JOIN users u ON u.id = a.user_id "
            "WHERE NOT u.all_projects"
        )
    )
    bind.execute(
        sa.text(
            f"INSERT INTO {MEMBER_DISC_TABLE} (member_id, discipline_id) "
            "SELECT pm.id, ud.discipline_id FROM project_members pm "
            "JOIN user_disciplines ud ON ud.user_id = pm.user_id"
        )
    )
    _warn_upgrade(bind)


def _warn_upgrade(bind: sa.Connection) -> None:
    """Sayımlar WARNING olarak basılır (deploy günlüğünde görünür); akışı durdurmaz."""
    all_projects_users = _scalar(bind, "SELECT count(*) FROM users WHERE all_projects")
    members = _scalar(bind, f"SELECT count(*) FROM {MEMBERS_TABLE}")
    member_disciplines = _scalar(bind, f"SELECT count(*) FROM {MEMBER_DISC_TABLE}")
    ignored_rows = _scalar(
        bind,
        "SELECT count(*) FROM (SELECT DISTINCT a.user_id, a.project_id FROM user_project_access a "
        "JOIN users u ON u.id = a.user_id WHERE a.project_id IS NOT NULL AND u.all_projects) t",
    )
    legacy_rows = _scalar(bind, "SELECT count(*) FROM user_project_access")
    legacy_duplicates = _scalar(
        bind,
        "SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) AS n FROM user_project_access "
        "WHERE project_id IS NOT NULL GROUP BY user_id, project_id) t",
    )
    disc_dropped_all = _scalar(
        bind,
        "SELECT count(DISTINCT ud.user_id) FROM user_disciplines ud "
        "JOIN users u ON u.id = ud.user_id WHERE u.all_projects",
    )
    disc_no_target = _scalar(
        bind,
        "SELECT count(DISTINCT ud.user_id) FROM user_disciplines ud "
        "JOIN users u ON u.id = ud.user_id WHERE NOT u.all_projects AND NOT EXISTS ("
        f"SELECT 1 FROM {MEMBERS_TABLE} pm WHERE pm.user_id = u.id)",
    )
    logger.warning(
        "IZN-B3: user_project_access %d satir -> %d tum-projeler kullanicisi, %d ekip satiri "
        "(%d yinelenen satir tekillesti, %d proje satiri tum-projeler kullanicisinda yazilmadi); "
        "%d proje-uye disiplin satiri",
        legacy_rows,
        all_projects_users,
        members,
        legacy_duplicates,
        ignored_rows,
        member_disciplines,
    )
    if disc_dropped_all:
        logger.warning(
            "IZN-B3: %d tum-projeler kullanicisinin global disiplini HEDEFSIZ kaldi "
            "(hedef modelde disiplin kisiti olmaz; user_disciplines satirlari B6'ya kadar durur)",
            disc_dropped_all,
        )
    if disc_no_target:
        logger.warning(
            "IZN-B3: %d kullanicinin global disiplini HEDEFSIZ kaldi (ekip satiri yok; "
            "user_disciplines satirlari B6'ya kadar durur)",
            disc_no_target,
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    _acquire_all_or_nothing(bind, DOWNGRADE_LOCKS)

    role_loss = _scalar(
        bind,
        f"SELECT count(*) FROM {MEMBERS_TABLE} pm JOIN users u ON u.id = pm.user_id "
        "WHERE pm.role_id <> u.role_id",
    )
    per_project_loss = _scalar(
        bind,
        "WITH sets AS (SELECT pm.user_id, pm.id, coalesce(string_agg(d.discipline_id::text, ',' "
        "ORDER BY d.discipline_id::text), '') AS s FROM project_members pm "
        "LEFT JOIN project_member_disciplines d ON d.member_id = pm.id GROUP BY pm.user_id, pm.id) "
        "SELECT count(*) FROM (SELECT user_id FROM sets GROUP BY user_id "
        "HAVING count(DISTINCT s) > 1) t",
    )

    # Eski tablolar yeni gerçeğe göre YENİDEN KURULUR (yeni tablolar tek yazma yeriydi).
    bind.execute(sa.text("DELETE FROM user_project_access"))
    bind.execute(
        sa.text(
            "INSERT INTO user_project_access (id, user_id, project_id, all_projects) "
            "SELECT gen_random_uuid(), id, NULL, true FROM users WHERE all_projects"
        )
    )
    bind.execute(
        sa.text(
            "INSERT INTO user_project_access (id, user_id, project_id, all_projects) "
            f"SELECT gen_random_uuid(), user_id, project_id, false FROM {MEMBERS_TABLE}"
        )
    )
    # Disiplin: ekip satırı olan kullanıcının eski global kümesi, projelerindeki disiplinlerin
    # BİRLEŞİMİyle değişir. Ekip satırı olmayan kullanıcının eski satırlarına DOKUNULMAZ.
    bind.execute(
        sa.text(
            "DELETE FROM user_disciplines WHERE user_id IN (SELECT user_id FROM project_members)"
        )
    )
    bind.execute(
        sa.text(
            "INSERT INTO user_disciplines (user_id, discipline_id) "
            "SELECT DISTINCT pm.user_id, d.discipline_id FROM project_members pm "
            "JOIN project_member_disciplines d ON d.member_id = pm.id"
        )
    )
    if role_loss or per_project_loss:
        logger.warning(
            "IZN-B3 downgrade: %d ekip satirinin proje rolu (ana rolden farkli) ve %d kullanicinin "
            "proje basina AYRI disiplin kumesi eski modelde TASINAMAZ (kullanici basina birlesim "
            "yazildi)",
            role_loss,
            per_project_loss,
        )

    op.drop_index("ix_project_member_disciplines_discipline_id", table_name=MEMBER_DISC_TABLE)
    op.drop_table(MEMBER_DISC_TABLE)
    op.drop_index("ix_project_members_role_id", table_name=MEMBERS_TABLE)
    op.drop_index("ix_project_members_project_id", table_name=MEMBERS_TABLE)
    op.drop_table(MEMBERS_TABLE)
    op.drop_column("users", "all_projects")

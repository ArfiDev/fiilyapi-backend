"""IZN-B2 — Onaylar eşiği düzeltmesi (hakediş + satın alma talebi) ve Patron `is_system=false`

Karar kaynağı: CEO (2026-10-04, IZN-B2 DEVAM) · IZN-PLAN §2.2, §8b "Yan etki 1".

## Neden
B1'in tek "Onaylar" biti, approve düzeyindeki eylemleri (hakediş Onayla/Reddet/Ödendi, satın alma
talebi Onayla/Reddet) admin/request eşiğine taşıdığı için Muhasebe/PM'de DARALMA oluşturuyordu.
B2 kapı köprüsü her `(modül, düzey)` kapısını eşit eşikli sayfa bayrağına bağlar; bu iki kapının
bayrağı yoktu. Katalog eşikleri düzeltildi (`core/sayfalar.py`):

* `mali.hakedis_isveren`, `mali.hakedis_taseron`, `proje.isveren_hakedis`,
  `proje.taseron_hakedis`, `santiye.hakedisler`: Onaylar eşiği `progress_payments`
  **admin → approve**.
  "Onayı Geri Al" (admin) Onaylar'a BAĞLANMAZ: yalnız Sistem Yöneticisi.
* `stok.satinalma_talepleri`: Onaylar eşiği `procurement` **request → approve**.

## Ne yapar
* Bu 6 sayfanın `can_approve` değerini CANLI `role_permissions` satırlarından yeniden türetir
  (yalnız `can_approve`; `level` aynı kalır çünkü görme/yazma eşikleri değişmedi). Eski satırı
  olmayan 6 yeni rol için `IZN_MATRIX` kopyası (iki modül) kullanılır. Eski satırı da kopyası da
  olmayan rol DEĞİŞTİRİLMEZ. Migration `app` IMPORT ETMEZ (donmuş kopya; eşitlik bekçisi
  `tests/modules/test_izn_b2_migration.py`).
* `patron.is_system = false` (plan §8b): rol silme kilidi artık yalnız `system_admin`; Patron
  kullanıcısızsa silinebilir ve "sistem rolünü yalnız Sistem Yöneticisi atar" kuralı Patron'a
  işlemez.
* Sapma raporu: değişen hücre sayısı WARNING olarak basılır; akışı durdurmaz.

## Kilit
`role_page_permissions` ve `role_permissions` SHARE ROW EXCLUSIVE, NOWAIT + savepoint ile
hep-ya-hiç; `SET LOCAL lock_timeout = '10s'` son savunma (B1 kalıbı). Tüm statement'lar
ekleyici/UPDATE; şema değişmez.

## Downgrade
B1 değerlerine döner: aynı 6 sayfanın `can_approve`ı B1 eşikleriyle (hakediş: admin, talep:
request) yeniden türetilir; `patron.is_system = true`.

Revision ID: 671436c0b42a
Revises: c5e9a3b7d1f4
Create Date: 2026-10-04

"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "671436c0b42a"
down_revision: str | Sequence[str] | None = "c5e9a3b7d1f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

PAGE_TABLE = "role_page_permissions"
SYSTEM_ADMIN_KEY = "system_admin"
PATRON_KEY = "patron"

LOCK_RETRY_INTERVAL_S = 0.2
LOCK_CEILING_S = 10.0
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
LOCKS = (
    ("roles", "SHARE ROW EXCLUSIVE"),
    ("role_permissions", "SHARE ROW EXCLUSIVE"),
    (PAGE_TABLE, "SHARE ROW EXCLUSIVE"),
)

LEVEL_RANK = {"none": 0, "view": 1, "draft": 2, "request": 3, "approve": 4, "full": 5, "admin": 6}

#: sayfa anahtarı -> (eski modül, ESKİ [B1] onay eşiği düzeyi, YENİ [B2] onay eşiği düzeyi).
#: `core/sayfalar._ESIKLER` ve `ESIK_SPEC_B1_FARKLARI` ile eşitlik bekçide.
AFFECTED_PAGES: dict[str, tuple[str, str, str]] = {
    "stok.satinalma_talepleri": ("procurement", "request", "approve"),
    "mali.hakedis_isveren": ("progress_payments", "admin", "approve"),
    "mali.hakedis_taseron": ("progress_payments", "admin", "approve"),
    "proje.isveren_hakedis": ("progress_payments", "admin", "approve"),
    "proje.taseron_hakedis": ("progress_payments", "admin", "approve"),
    "santiye.hakedisler": ("progress_payments", "admin", "approve"),
}

#: 6 yeni rol: eski satırı YOK; etkilenen iki modülün düzeyi `seed_data.IZN_MATRIX`ten ELLE kopya.
IZN_ROLE_ORDER = [
    "planning_engineer",
    "technical_office",
    "warehouse_keeper",
    "viewer",
    "finance_manager",
    "cost_engineer",
]
IZN_MODULE_LEVELS: dict[str, list[str]] = {
    "progress_payments": ["draft", "none", "none", "view", "approve", "view"],
    "procurement": ["request", "none", "view", "view", "none", "view"],
}


class LockCeilingExceededError(RuntimeError):
    """Kilitler tavan süresinde alınamadı (migration geri alınır)."""


def _sqlstate(exc: sa.exc.DBAPIError) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def _acquire_all_or_nothing(bind: sa.Connection) -> None:
    deadline = time.monotonic() + LOCK_CEILING_S
    while True:
        try:
            with bind.begin_nested():
                for table, mode in LOCKS:
                    bind.execute(sa.text(f"LOCK TABLE {table} IN {mode} MODE NOWAIT"))
            return
        except sa.exc.DBAPIError as exc:
            if _sqlstate(exc) != LOCK_NOT_AVAILABLE_SQLSTATE:
                raise
            if time.monotonic() >= deadline:
                raise LockCeilingExceededError(
                    f"{LOCK_CEILING_S}s icinde kilitler alinamadi (migration geri alindi)"
                ) from exc
        bind.execute(sa.text("SELECT pg_sleep(:s)"), {"s": LOCK_RETRY_INTERVAL_S})


def _module_levels(bind: sa.Connection) -> dict[str, dict[str, str]]:
    """rol anahtarı -> {modül: eski düzey}; yalnız etkilenen iki modül, CANLI satırlardan."""
    modules = sorted({module for module, _old, _new in AFFECTED_PAGES.values()})
    rows = bind.execute(
        sa.text(
            "SELECT r.key, m.key, rp.access_level::text "
            "FROM role_permissions rp "
            "JOIN roles r ON r.id = rp.role_id "
            "JOIN modules m ON m.id = rp.module_id "
            "WHERE m.key = ANY(:modules)"
        ),
        {"modules": modules},
    ).all()
    levels: dict[str, dict[str, str]] = {}
    for role_key, module_key, level in rows:
        levels.setdefault(role_key, {})[module_key] = level
    return levels


def _role_levels(
    bind: sa.Connection, role_key: str, live: dict[str, dict[str, str]]
) -> dict[str, str] | None:
    """Rolün etkilenen modüllerdeki düzeyi: canlı satır → IZN kopyası → None (dokunma)."""
    if role_key in live:
        return live[role_key]
    if role_key in IZN_ROLE_ORDER:
        index = IZN_ROLE_ORDER.index(role_key)
        return {module: levels[index] for module, levels in IZN_MODULE_LEVELS.items()}
    return None


def _apply(bind: sa.Connection, threshold_index: int) -> int:
    """Etkilenen sayfaların `can_approve`ı: `threshold_index` 1=eski (B1), 2=yeni (B2) eşik."""
    live = _module_levels(bind)
    roles = bind.execute(sa.text("SELECT id, key FROM roles")).all()
    changed = 0
    for role_id, role_key in roles:
        if role_key == SYSTEM_ADMIN_KEY:
            continue
        levels = _role_levels(bind, role_key, live)
        if levels is None:
            continue
        for page_key, spec in AFFECTED_PAGES.items():
            module_key, threshold = spec[0], spec[threshold_index]
            rank = LEVEL_RANK[levels.get(module_key, "none")]
            approve = rank >= LEVEL_RANK[threshold]
            # Onay görünmeyen sayfada OLMAZ (DB CHECK); `level` bu migration'da DEĞİŞMEZ.
            result = bind.execute(
                sa.text(
                    f"UPDATE {PAGE_TABLE} SET can_approve = :approve "
                    "WHERE role_id = :role_id AND page_key = :page_key "
                    "AND can_approve <> :approve AND (level::text <> 'none' OR NOT :approve)"
                ),
                {"approve": approve, "role_id": role_id, "page_key": page_key},
            )
            changed += result.rowcount or 0
    return changed


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    _acquire_all_or_nothing(bind)
    changed = _apply(bind, threshold_index=2)
    bind.execute(
        sa.text("UPDATE roles SET is_system = false WHERE key = :key"), {"key": PATRON_KEY}
    )
    logger.warning(
        "IZN-B2: Onaylar esigi duzeltmesiyle degisen sayfa hucresi sayisi=%d; "
        "patron.is_system=false",
        changed,
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    _acquire_all_or_nothing(bind)
    changed = _apply(bind, threshold_index=1)
    bind.execute(sa.text("UPDATE roles SET is_system = true WHERE key = :key"), {"key": PATRON_KEY})
    logger.warning("IZN-B2 (downgrade): B1 esigine donen sayfa hucresi sayisi=%d", changed)

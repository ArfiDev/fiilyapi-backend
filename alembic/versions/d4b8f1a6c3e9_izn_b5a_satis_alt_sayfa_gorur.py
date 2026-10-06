"""IZN-B5a madde 3 — satış alt sayfalarının Görür hücreleri seed ile eşitlenir

Karar kaynağı: CEO (IZN-B5a, seçenek a: seed = migrate edilmiş DB). `mali.satis_blok`,
`mali.satis_unite`, `mali.satis_excel`, `mali.satis_paylasim` sayfalarının görme eşiği
`projects:draft` → `projects:view` oldu (`app/core/sayfalar.py::_ESIKLER` 36/37/39/40). Seed bu dört
sayfayı `projects:view` geçen rollerde `view` üretir; mevcut DB'deki roller `none` kalmıştı.

## Kural (upgrade)
Sistem Yöneticisi DIŞINDAKİ her rolde, ESKİ `projects:view` sayfa bayraklarından en az biri
(`genel.projeler`, `genel.proje_takvimi`, `proje.ozet`, `proje.paylasim_tablosu` düzeyi `none`
değil) varsa dört sayfanın hücresi `none` (ya da satır yok) iken `view` olur. `view`/`edit`
hücreye DOKUNULMAZ. Veri erişimi değişmez: bu rol aynı GET uçlarını zaten geçiyordu; fark yalnız
menüde görünmektir. Açılan hücre sayısı INFO basılır.

## Downgrade
Yalnız upgrade kuralının AÇABİLECEĞİ hücreler geri `none` olur: aynı koşulu sağlayan rolde hücre
TAM `view` ve onaysızsa. Rol adı geçen bir "Sayfa izinleri değişti: <rol adı> (" denetim kaydı
varsa (biri ekrandan kaydetmiş) rol DOKUNULMAZ ve WARNING basılır. Denetim kaydı YALNIZ rol
ADIYLA eşleşir: yeniden adlandırılmış rolde eski ad bulunamaz (bilinçli kabul, IZN-B4c emsali).

Migration `app` IMPORT ETMEZ (anahtarlar elle kopya; bekçisi
`tests/modules/test_izn_b5a_migration.py`).

Revision ID: d4b8f1a6c3e9
Revises: c7e2a9d4b1f3
Create Date: 2026-10-06

"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4b8f1a6c3e9"
down_revision: str | Sequence[str] | None = "c7e2a9d4b1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

SYSTEM_ADMIN_KEY = "system_admin"
#: ELLE KOPYA: `_ESIKLER` 36/37/39/40.
SATIS_SAYFALARI: tuple[str, ...] = (
    "mali.satis_blok",
    "mali.satis_unite",
    "mali.satis_excel",
    "mali.satis_paylasim",
)
#: ELLE KOPYA: `page_gate.gate_flags("projects", view)` − SATIS_SAYFALARI (B5a öncesi kapı).
ESKI_PROJE_GORME: tuple[str, ...] = (
    "genel.projeler",
    "genel.proje_takvimi",
    "proje.ozet",
    "proje.paylasim_tablosu",
)
AUDIT_PREFIX = "Sayfa izinleri değişti: {name} ("


def _uygun_roller(bind: sa.Connection) -> list[tuple[object, str]]:
    rows = bind.execute(
        sa.text(
            "SELECT r.id, r.name FROM roles r WHERE r.key <> :admin AND EXISTS ("
            "SELECT 1 FROM role_page_permissions p WHERE p.role_id = r.id "
            "AND p.page_key = ANY(:eski) AND p.level <> 'none') ORDER BY r.key"
        ),
        {"admin": SYSTEM_ADMIN_KEY, "eski": list(ESKI_PROJE_GORME)},
    )
    return [(row[0], row[1]) for row in rows]


def _has_audit(bind: sa.Connection, name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM audit_log "
                "WHERE action = 'update' AND starts_with(detail, :prefix))"
            ),
            {"prefix": AUDIT_PREFIX.format(name=name)},
        ).scalar_one()
    )


def upgrade() -> None:
    bind = op.get_bind()
    acilan = 0
    for role_id, _name in _uygun_roller(bind):
        for page in SATIS_SAYFALARI:
            result = bind.execute(
                sa.text(
                    "INSERT INTO role_page_permissions (role_id, page_key, level, can_approve) "
                    "VALUES (:rid, :page, 'view', false) "
                    "ON CONFLICT (role_id, page_key) DO UPDATE SET level = 'view' "
                    "WHERE role_page_permissions.level = 'none'"
                ),
                {"rid": role_id, "page": page},
            )
            acilan += result.rowcount
    logger.info("IZN-B5a: %s satış alt sayfası Görür hücresi açıldı", acilan)


def downgrade() -> None:
    bind = op.get_bind()
    for role_id, name in _uygun_roller(bind):
        if _has_audit(bind, name):
            logger.warning("IZN-B5a downgrade: %s sayfa izinleri ekrandan değişmiş, atlandı", name)
            continue
        bind.execute(
            sa.text(
                "UPDATE role_page_permissions SET level = 'none' WHERE role_id = :rid "
                "AND page_key = ANY(:pages) AND level = 'view' AND NOT can_approve"
            ),
            {"rid": role_id, "pages": list(SATIS_SAYFALARI)},
        )

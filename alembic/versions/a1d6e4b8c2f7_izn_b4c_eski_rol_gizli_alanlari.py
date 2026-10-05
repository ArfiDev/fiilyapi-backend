"""IZN-B4c madde 20 — eski rollerin B1'den gelen `tum_tutarlar` bayrağı onaylı kümelerle değişir

Karar kaynağı: CEO onayı (IZN-B4c). `izn_b1` eski `limited` kapsamını (modül başınaydı) rol için
KÜRESEL `tum_tutarlar` bayrağına çevirmişti. Yeni maske küresel olduğu için İK Müdürü ücret/bordro,
Satınalma kendi fiyatlarını göremiyordu. Onaylı kümeler:

* `hr_manager`, `site_chief`, `field_engineer` → {sozlesme_fiyat, maliyet_kar, banka_kasa,
  satis_alici} (`maas_kisisel` AÇIK)
* `procurement` → {sozlesme_fiyat, satis_alici, banka_kasa} (`maliyet_kar` + `maas_kisisel` AÇIK)

## Kural (upgrade)
Yalnız bu 4 rol anahtarında ve `role_hidden_fields` kümesi TAM OLARAK {tum_tutarlar} olan roller
değişir: `tum_tutarlar` satırı silinir, onaylı kategoriler eklenir. Kümesi farklı olan rol (ekrandan
değiştirilmiş) DOKUNULMAZ. Rol adı geçen bir "Gizli alanlar değişti: <rol adı> · ..." denetim
kaydı (rol yönetimi API'si gizli alan değişikliğinde bunu `audit_log`a yazar) varsa, küme hâlâ
{tum_tutarlar} olsa bile rol DOKUNULMAZ (biri ekrandan bilerek kaydetmiş demektir). Denetim kaydı
YALNIZ rol ADIYLA eşleşir (kayıtta rol kimliği yoktur): rol sonradan yeniden adlandırıldıysa eski
ad kaydı bulunamaz; bu durumda küme-eşitliği kuralı tek koruma olarak kalır. Atlanan roller ve
sebebi WARNING olarak basılır.

## Downgrade
Aynı 4 rolde küme TAM OLARAK onaylı kümeye eşitse geri {tum_tutarlar} yapılır; farklıysa dokunulmaz.

Kilit: `roles` + `role_hidden_fields` SHARE ROW EXCLUSIVE tek ifadede; `SET LOCAL lock_timeout`.
Migration `app` IMPORT ETMEZ (sabitler elle kopya; bekçisi
`tests/modules/test_izn_b4c_madde20_rol_kumeleri.py`).

Revision ID: a1d6e4b8c2f7
Revises: e8b4c2d6f9a1
Create Date: 2026-10-05

"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1d6e4b8c2f7"
down_revision: str | Sequence[str] | None = "e8b4c2d6f9a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

HIDDEN_TABLE = "role_hidden_fields"
CATEGORY_ENUM = "hidden_category"
TUM_TUTARLAR = "tum_tutarlar"
LOCK_TIMEOUT = "10s"
LOCKED_TABLES = ("roles", HIDDEN_TABLE)

#: ELLE KOPYA: `seed_data.ESKI_ROL_GIZLI_ALANLAR` (CEO onaylı).
_SAHA_VE_IK = ("sozlesme_fiyat", "maliyet_kar", "banka_kasa", "satis_alici")
APPROVED_SETS: dict[str, frozenset[str]] = {
    "hr_manager": frozenset(_SAHA_VE_IK),
    "site_chief": frozenset(_SAHA_VE_IK),
    "field_engineer": frozenset(_SAHA_VE_IK),
    "procurement": frozenset({"sozlesme_fiyat", "satis_alici", "banka_kasa"}),
}
OLD_SET = frozenset({TUM_TUTARLAR})

#: `app/modules/audit/messages/core.py::role_hidden_fields_updated` başlığı (elle kopya).
AUDIT_PREFIX = "Gizli alanlar değişti: {name} · "


def _roles(bind: sa.Connection) -> dict[str, tuple[object, str]]:
    rows = bind.execute(
        sa.text("SELECT id, key, name FROM roles WHERE key IN :keys").bindparams(
            sa.bindparam("keys", expanding=True)
        ),
        {"keys": list(APPROVED_SETS)},
    ).all()
    return {key: (role_id, name) for role_id, key, name in rows}


def _current(bind: sa.Connection, role_id: object) -> frozenset[str]:
    rows = bind.execute(
        sa.text(f"SELECT category::text FROM {HIDDEN_TABLE} WHERE role_id = :rid"),
        {"rid": role_id},
    ).scalars()
    return frozenset(rows)


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


def _replace(bind: sa.Connection, role_id: object, new: frozenset[str]) -> None:
    bind.execute(sa.text(f"DELETE FROM {HIDDEN_TABLE} WHERE role_id = :rid"), {"rid": role_id})
    for category in sorted(new):
        bind.execute(
            sa.text(
                f"INSERT INTO {HIDDEN_TABLE} (role_id, category) "
                f"VALUES (:rid, CAST(:category AS {CATEGORY_ENUM}))"
            ),
            {"rid": role_id, "category": category},
        )


def _lock(bind: sa.Connection) -> None:
    bind.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    tables = ", ".join(LOCKED_TABLES)
    bind.execute(sa.text(f"LOCK TABLE {tables} IN SHARE ROW EXCLUSIVE MODE"))


def upgrade() -> None:
    bind = op.get_bind()
    _lock(bind)
    roles = _roles(bind)
    changed: list[str] = []
    skipped: list[str] = []
    for key, approved in APPROVED_SETS.items():
        if key not in roles:
            skipped.append(f"{key}: rol yok")
            continue
        role_id, name = roles[key]
        current = _current(bind, role_id)
        if current != OLD_SET:
            skipped.append(f"{key}: kume={sorted(current)} (ekrandan degistirilmis, dokunulmadi)")
        elif _has_audit(bind, name):
            skipped.append(f"{key}: gizli alan denetim kaydi var (dokunulmadi)")
        else:
            _replace(bind, role_id, approved)
            changed.append(key)
    logger.warning(
        "IZN-B4c madde 20: tum_tutarlar -> onayli kume, degisen roller=%s; atlanan=%s",
        changed,
        skipped,
    )


def downgrade() -> None:
    bind = op.get_bind()
    _lock(bind)
    roles = _roles(bind)
    changed: list[str] = []
    skipped: list[str] = []
    for key, approved in APPROVED_SETS.items():
        if key not in roles:
            continue
        role_id, _name = roles[key]
        current = _current(bind, role_id)
        if current != approved:
            skipped.append(f"{key}: kume={sorted(current)} (onayli kume degil, dokunulmadi)")
            continue
        _replace(bind, role_id, OLD_SET)
        changed.append(key)
    logger.warning(
        "IZN-B4c madde 20 downgrade: onayli kume -> tum_tutarlar, degisen roller=%s; atlanan=%s",
        changed,
        skipped,
    )

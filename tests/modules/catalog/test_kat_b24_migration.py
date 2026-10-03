"""KAT-B2.4 — `e4a8c2d6f1b3` migration: iki hakedis satiri tablosuna `source_code` + backfill.

Tek kullanimlik veritabani (HZ-1 deseni): bir onceki revizyona (d9f3b7a2c4e6) cikilir, VERI
tohumlanir (isveren/taseron kalemi kodlu/kodsuz; taslak/onayli/odenmis hakedis; bagli/yetim satir)
→ upgrade (backfill; K13: onayli/odenmis DAHIL) → kalem kodu degisse/kalem silinse satir kopyasi
ETKILENMEZ → downgrade → upgrade gidis-donus.
Kilit: metin bekcisi + DAVRANISSAL deadlock testi (iki oturum, gercek migration kodu; iki aile ×
iki senaryo; pozitif kontrol: bloklayan kilit sirasi ayni senaryoda DeadlockDetected uretir) +
tavan testi.
"""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import asyncpg
import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from tests.modules.treasury.test_hz1_migration import (
    _asyncpg_dsn,
    _create_scratch_database,
    _drop_scratch_database,
    _run_alembic,
    _seed_user,
)

PREVIOUS = "d9f3b7a2c4e6"
REVISION = "e4a8c2d6f1b3"
MIGRATION = next(Path("alembic/versions").glob(f"{REVISION}_*.py"))

KOD_E = "15.100.1001"
KOD_E_SILINEN = "15.100.1004"
KOD_S = "25.200.2001"

LINE_TABLES = ("progress_payment_lines", "subcontractor_progress_payment_lines")


async def _kolon(conn: asyncpg.Connection, tablo: str) -> tuple[str, str, int] | None:
    row = await conn.fetchrow(
        "SELECT is_nullable, data_type, character_maximum_length "
        "FROM information_schema.columns WHERE column_name = 'source_code' "
        "AND table_name = $1",
        tablo,
    )
    return None if row is None else (row[0], row[1], row[2])


async def _payment(
    conn: asyncpg.Connection, tablo: str, ids: dict, key: str, seq: int, status: str, **extra
) -> None:  # noqa: ANN003, E501
    cols = "id, project_id, sequence_no, vat_pct, advance_pct, retainage_pct, created_by, status"
    vals = "$1, $2, $3, 20, 0, 0, $4, $5"
    args = [ids[key], ids["project"], seq, ids["user"], status]
    if tablo == "spp":
        cols += ", contract_id"
        vals += ", $6"
        args.append(ids["contract"])
        table = "subcontractor_progress_payments"
        enum = "subcontractor_payment_status"
    else:
        table = "progress_payments"
        enum = "progress_payment_status"
    vals = vals.replace("$5", f"$5::{enum}")
    await conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({vals})", *args)  # noqa: S608


async def _tohum(conn: asyncpg.Connection) -> dict[str, uuid.UUID]:  # noqa: PLR0915
    keys = (
        "project", "cgroup", "site", "e_dolu", "e_bos", "e_sil", "subc", "contract",
        "s_dolu", "s_bos", "s_baglisiz",
        "pp_taslak", "pp_onayli", "pp_odenmis", "spp_taslak", "spp_onayli", "spp_odenmis",
        "pl_taslak", "pl_onayli", "pl_odenmis", "pl_bos", "pl_yetim",
        "sl_taslak", "sl_onayli", "sl_odenmis", "sl_bos", "sl_yetim",
    )  # fmt: skip
    ids = {k: uuid.uuid4() for k in keys}
    ids["user"] = await _seed_user(conn)
    await conn.execute(
        "INSERT INTO projects (id, code, name, budget, progress_pct) "
        "VALUES ($1, 'P-B24', 'Mig', 0, 0)",
        ids["project"],
    )
    await conn.execute("INSERT INTO project_contracts (project_id) VALUES ($1)", ids["project"])
    await conn.execute(
        "INSERT INTO sites (id, project_id, code, name) VALUES ($1, $2, 'S1', 'Site')",
        ids["site"],
        ids["project"],
    )
    await conn.execute(
        "INSERT INTO employer_contract_groups (id, project_id, name) VALUES ($1, $2, 'G')",
        ids["cgroup"],
        ids["project"],
    )
    for key, code, kod in (
        ("e_dolu", "01", KOD_E),
        ("e_bos", "02", None),
        ("e_sil", "04", KOD_E_SILINEN),
    ):
        await conn.execute(
            "INSERT INTO employer_contract_items (id, project_id, group_id, code, description, "
            "unit, quantity, unit_price, source_code) "
            "VALUES ($1, $2, $3, $4, 'Beton', 'm3', 1, 100, $5)",
            ids[key],
            ids["project"],
            ids["cgroup"],
            code,
            kod,
        )
    await conn.execute("INSERT INTO subcontractors (id, name) VALUES ($1, 'T')", ids["subc"])
    await conn.execute(
        "INSERT INTO subcontractor_contracts (id, project_id, subcontractor_id, created_by) "
        "VALUES ($1, $2, $3, $4)",
        ids["contract"],
        ids["project"],
        ids["subc"],
        ids["user"],
    )
    for key, code, kod in (
        ("s_dolu", "01", KOD_S),
        ("s_bos", "02", None),
        ("s_baglisiz", "03", None),
    ):
        await conn.execute(
            "INSERT INTO subcontractor_contract_items (id, contract_id, code, description, unit, "
            "quantity, source_code) VALUES ($1, $2, $3, 'Beton', 'm3', 1, $4)",
            ids[key],
            ids["contract"],
            code,
            kod,
        )
    for seq, (suffix, status) in enumerate(
        (("taslak", "draft"), ("onayli", "approved"), ("odenmis", "paid")), start=1
    ):
        await _payment(conn, "pp", ids, f"pp_{suffix}", seq, status)
        await _payment(conn, "spp", ids, f"spp_{suffix}", seq, status)
    for key, payment, item, code in (
        ("pl_taslak", "pp_taslak", "e_dolu", "01"),
        ("pl_onayli", "pp_onayli", "e_dolu", "01"),
        ("pl_odenmis", "pp_odenmis", "e_sil", "04"),
        ("pl_bos", "pp_taslak", "e_bos", "02"),
        ("pl_yetim", "pp_taslak", None, "99"),
    ):
        await conn.execute(
            "INSERT INTO progress_payment_lines (id, payment_id, contract_item_id, site_id, code, "
            "description, unit, contract_unit_price, quantity) "
            "VALUES ($1, $2, $3, $4, $5, 'Beton', 'm3', 100, 1)",
            ids[key],
            ids[payment],
            ids[item] if item else None,
            ids["site"],
            code,
        )
    for key, payment, item, code in (
        ("sl_taslak", "spp_taslak", "s_dolu", "01"),
        ("sl_onayli", "spp_onayli", "s_dolu", "01"),
        ("sl_odenmis", "spp_odenmis", "s_dolu", "01"),
        ("sl_bos", "spp_taslak", "s_bos", "02"),
        ("sl_yetim", "spp_taslak", None, "99"),
    ):
        await conn.execute(
            "INSERT INTO subcontractor_progress_payment_lines (id, payment_id, contract_item_id, "
            "code, description, unit, contract_unit_price, quantity) "
            "VALUES ($1, $2, $3, $4, 'Beton', 'm3', 100, 1)",
            ids[key],
            ids[payment],
            ids[item] if item else None,
            code,
        )
    return ids


async def _kopyalar(conn: asyncpg.Connection, ids: dict[str, uuid.UUID]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for key in ("pl_taslak", "pl_onayli", "pl_odenmis", "pl_bos", "pl_yetim"):
        out[key] = await conn.fetchval(
            "SELECT source_code FROM progress_payment_lines WHERE id=$1", ids[key]
        )
    for key in ("sl_taslak", "sl_onayli", "sl_odenmis", "sl_bos", "sl_yetim"):
        out[key] = await conn.fetchval(
            "SELECT source_code FROM subcontractor_progress_payment_lines WHERE id=$1", ids[key]
        )
    return out


BEKLENEN_BACKFILL = {
    # isveren: bagli+kodlu → kopya (taslak/onayli/odenmis HEPSI, K13); kodsuz/yetim → NULL
    "pl_taslak": KOD_E, "pl_onayli": KOD_E, "pl_odenmis": KOD_E_SILINEN,
    "pl_bos": None, "pl_yetim": None,
    # taseron: ayni
    "sl_taslak": KOD_S, "sl_onayli": KOD_S, "sl_odenmis": KOD_S,
    "sl_bos": None, "sl_yetim": None,
}  # fmt: skip


async def test_KATB24_migration_kolon_backfill_snapshot_downgrade_ve_gidis_donus() -> None:
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            ids = await _tohum(conn)
            for tablo in LINE_TABLES:
                assert await _kolon(conn, tablo) is None
        finally:
            await conn.close()

        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for tablo in LINE_TABLES:
                assert await _kolon(conn, tablo) == ("YES", "character varying", 32)
            assert await _kopyalar(conn, ids) == BEKLENEN_BACKFILL
            # para/denetim etkisi yok: backfill yalniz source_code'a yazdi
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM progress_payment_lines WHERE contract_unit_price = 100 "
                    "AND quantity = 1 AND coefficient = 1 AND code IN ('01','02','04','99')"
                )
                == 5
            )
            # SNAPSHOT: kalem kodu degisince satir ESKI kodu korur
            await conn.execute(
                "UPDATE employer_contract_items SET source_code = '99.999.9999' WHERE id = $1",
                ids["e_dolu"],
            )
            await conn.execute(
                "UPDATE subcontractor_contract_items SET source_code = '88.888.8888' WHERE id = $1",
                ids["s_dolu"],
            )
            # ... ve kalem silinince (FK SET NULL) kopya KALIR
            await conn.execute("DELETE FROM employer_contract_items WHERE id = $1", ids["e_sil"])
            await conn.execute(
                "DELETE FROM subcontractor_contract_items WHERE id = $1", ids["s_dolu"]
            )
            assert await _kopyalar(conn, ids) == BEKLENEN_BACKFILL
            assert (
                await conn.fetchval(
                    "SELECT contract_item_id FROM progress_payment_lines WHERE id = $1",
                    ids["pl_odenmis"],
                )
                is None
            )
            # kolon varsayilani yok: eski konteyner penceresi (kolonsuz INSERT) gecer
            await conn.execute(
                "INSERT INTO subcontractor_progress_payment_lines (id, payment_id, code, "
                "description, unit, contract_unit_price, quantity) "
                "VALUES ($1, $2, '77', 'd', 'm', 1, 1)",
                uuid.uuid4(),
                ids["spp_taslak"],
            )
        finally:
            await conn.close()

        _run_alembic("downgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for tablo in LINE_TABLES:
                assert await _kolon(conn, tablo) is None
            assert await conn.fetchval("SELECT count(*) FROM progress_payment_lines") == 5
            assert (
                await conn.fetchval("SELECT count(*) FROM subcontractor_progress_payment_lines")
                == 6
            )
        finally:
            await conn.close()

        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            kopyalar = await _kopyalar(conn, ids)
            # gidis-donus: backfill BUGUNKU kaynak kodunu yazar; baglari kopmus satir NULL kalir
            assert kopyalar["pl_taslak"] == "99.999.9999"
            assert kopyalar["pl_onayli"] == "99.999.9999"
            assert kopyalar["pl_odenmis"] is None and kopyalar["pl_yetim"] is None
            assert kopyalar["sl_taslak"] is None and kopyalar["sl_odenmis"] is None
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


def _src() -> str:
    return MIGRATION.read_text()


def _upgrade_body() -> str:
    return _src().split("def upgrade()")[1].split("def downgrade()")[0]


def _downgrade_body() -> str:
    return _src().split("def downgrade()")[1]


def test_KATB24_migration_kilit_kaniti_en_basta_hep_ya_hic() -> None:
    """Kilit kanonu kaynak metinden: lock_timeout → hep-ya-hic (NOWAIT+savepoint) kilitler → ilk
    DDL → backfill (isveren, sonra taseron). Kilit hicbir DDL'den SONRA gelmez; `app` yok."""
    src = _src()
    up = _upgrade_body()
    assert (
        up.index("SET LOCAL lock_timeout = '10s'")
        < up.index("_acquire_all_or_nothing(bind, UPGRADE_LOCKS)")
        < up.index("op.add_column")
        < up.index("UPDATE progress_payment_lines")
        < up.index("UPDATE subcontractor_progress_payment_lines")
    )
    assert up.count("op.add_column") == 2
    down = _downgrade_body()
    assert (
        down.index("SET LOCAL lock_timeout = '10s'")
        < down.index("_acquire_all_or_nothing(bind, DOWNGRADE_LOCKS)")
        < down.index("op.drop_column")
    )
    helper = src.split("def _acquire_all_or_nothing")[1].split("def upgrade()")[0]
    assert "begin_nested()" in helper and "MODE NOWAIT" in helper
    assert "LockCeilingExceededError" in helper and "LOCK_CEILING_S" in helper
    kod = src.split("revision: str", 1)[1]  # docstring haric
    assert kod.count("LOCK TABLE") == kod.count("MODE NOWAIT") == 1  # bekleyen kilit yok
    # ALTER edilen iki satir tablosu AE; backfill kaynagi iki kalem tablosu SRE (yukseltme yok)
    for tablo in ("EMPLOYER_LINES", "SUBCONTRACT_LINES"):
        assert f'({tablo}, "ACCESS EXCLUSIVE")' in kod
    for tablo in ("EMPLOYER_ITEMS", "SUBCONTRACT_ITEMS"):
        assert f'({tablo}, "SHARE ROW EXCLUSIVE")' in kod
    assert "import app" not in src and "from app" not in src
    # backfill YALNIZ bagli kalem + kodlu kaynak: yetim/kodsuz NULL kalir; durum filtresi YOK
    assert "i.id = l.contract_item_id" in up and up.count("i.source_code IS NOT NULL") == 2
    assert "status" not in up


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("katb24_migration_under_test", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bloklayan_sira(
    *tablolar_ve_kipler: tuple[str, str],
) -> Callable[[sa.Connection, object], None]:
    """Onarimdan ONCEKI davranis (pozitif kontrol): tek tek BEKLEYEN (NOWAIT'siz) kilitler."""

    def bloklayan(bind: sa.Connection, _locks: object) -> None:
        for tablo, kip in tablolar_ve_kipler:
            bind.execute(sa.text(f"LOCK TABLE {tablo} IN {kip} MODE"))

    return bloklayan


async def _migrate_in_process(
    database: str, fn_name: str, *, patch: Callable[[ModuleType], None] | None = None
) -> None:
    """Gercek migration fonksiyonunu (upgrade/downgrade) ayni kodla, surec ici kosar."""
    module = _load_migration()
    if patch is not None:
        patch(module)
    dsn = _asyncpg_dsn(database).replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(dsn)

    def run(sync_conn: sa.Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(module, fn_name)()

    try:
        async with engine.begin() as conn:
            await conn.run_sync(run)
    finally:
        await engine.dispose()


async def _t1_oturumu(database: str) -> tuple[asyncpg.Connection, object]:
    conn = await asyncpg.connect(_asyncpg_dsn(database))
    tr = conn.transaction()
    await tr.start()
    return conn, tr


# aile → (kalem tablosu, satir tablosu, kalem anahtari, ikinci kalem anahtari, hakedis anahtari)
_AILE = {
    "isveren": ("employer_contract_items", "progress_payment_lines",
                "e_dolu", "e_bos", "pp_onayli"),
    "taseron": ("subcontractor_contract_items", "subcontractor_progress_payment_lines",
                "s_dolu", "s_bos", "spp_onayli"),
}  # fmt: skip


async def _t1_ilk_adim(
    aile: str, senaryo: str, conn: asyncpg.Connection, ids: dict[str, uuid.UUID]
) -> None:
    kalem_t, satir_t, kalem, _, _ = _AILE[aile]
    if senaryo == "A":  # sozlesme kalemi YAZ (once) → hakedis satiri YAZ
        await conn.execute(
            f"UPDATE {kalem_t} SET description = 'y' WHERE id = $1",  # noqa: S608
            ids[kalem],
        )
    else:  # B: hakedis satirini OKU (once) → sozlesme kalemi YAZ
        await conn.fetchval(f"SELECT count(*) FROM {satir_t}")  # noqa: S608


async def _t1_calistir(
    aile: str, senaryo: str, conn: asyncpg.Connection, tr: object, ids: dict[str, uuid.UUID]
) -> BaseException | None:
    """Eski konteyner islemi (uygulamadaki yollar: sozlesme kalemi PATCH, hakedis satiri PUT).
    Deadlock kurbani olursa hatayi dondurur."""
    kalem_t, _, kalem, kalem2, hakedis = _AILE[aile]
    try:
        if senaryo == "A":
            if aile == "isveren":
                await conn.execute(
                    "INSERT INTO progress_payment_lines (id, payment_id, contract_item_id, "
                    "site_id, code, description, unit, contract_unit_price, quantity) "
                    "VALUES ($1, $2, $3, $4, '98', 'x', 'm', 1, 1)",
                    uuid.uuid4(), ids[hakedis], ids[kalem2], ids["site"],
                )  # fmt: skip
            else:
                await conn.execute(
                    "INSERT INTO subcontractor_progress_payment_lines (id, payment_id, "
                    "contract_item_id, code, description, unit, contract_unit_price, quantity) "
                    "VALUES ($1, $2, $3, '98', 'x', 'm', 1, 1)",
                    uuid.uuid4(), ids[hakedis], ids[kalem2],
                )  # fmt: skip
        else:
            await conn.execute(
                f"UPDATE {kalem_t} SET description = 'z' WHERE id = $1",  # noqa: S608
                ids[kalem2],
            )
        await tr.commit()  # type: ignore[attr-defined]
        return None
    except Exception as exc:  # noqa: BLE001
        await tr.rollback()  # type: ignore[attr-defined]
        return exc


async def _senaryo(
    aile: str,
    senaryo: str,
    *,
    patch: Callable[[ModuleType], None] | None = None,
    bekle_s: float = 1.5,
) -> tuple[BaseException | None, BaseException | None, str, bool]:
    """(migration_hatasi, t1_hatasi, db, kolon_var_mi). DB'yi cagiran DUSURUR."""
    database = await _create_scratch_database()
    _run_alembic("upgrade", PREVIOUS, database=database)
    seed = await asyncpg.connect(_asyncpg_dsn(database))
    try:
        ids = await _tohum(seed)
    finally:
        await seed.close()
    conn, tr = await _t1_oturumu(database)
    mig_err: BaseException | None = None
    try:
        await _t1_ilk_adim(aile, senaryo, conn, ids)
        mig = asyncio.create_task(_migrate_in_process(database, "upgrade", patch=patch))
        await asyncio.sleep(bekle_s)  # migration kilit denemesinde / beklemede
        t1_err = await _t1_calistir(aile, senaryo, conn, tr, ids)
        try:
            await asyncio.wait_for(mig, timeout=60)
        except BaseException as exc:  # noqa: BLE001
            mig_err = exc
    finally:
        await conn.close()
    check = await asyncpg.connect(_asyncpg_dsn(database))
    try:
        kolon_var = all([await _kolon(check, t) is not None for t in LINE_TABLES])
    finally:
        await check.close()
    return mig_err, t1_err, database, kolon_var


def _deadlock_mu(exc: BaseException | None) -> bool:
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, asyncpg.exceptions.DeadlockDetectedError):
            return True
        if "deadlock detected" in str(cur):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


@pytest.mark.parametrize("aile", ["isveren", "taseron"])
@pytest.mark.parametrize("senaryo", ["A", "B"])
async def test_KATB24_migration_eski_konteyner_trafigiyle_deadlock_yok(
    aile: str, senaryo: str
) -> None:
    mig_err, t1_err, database, kolon_var = await _senaryo(aile, senaryo)
    try:
        assert not _deadlock_mu(mig_err) and not _deadlock_mu(t1_err), (mig_err, t1_err)
        assert t1_err is None, t1_err
        assert mig_err is None, mig_err
        assert kolon_var
    finally:
        await _drop_scratch_database(database)


# A: T1 kalem YAZ → satir YAZ  ⇒ cevrim icin migration satir AE → kalem SRE sirasinda BEKLER.
# B: T1 satir OKU → kalem YAZ  ⇒ kalem SRE → satir AE sirasinda BEKLER.
_BLOKLAYAN_SIRA = {
    ("isveren", "A"): (("progress_payment_lines", "ACCESS EXCLUSIVE"),
                       ("employer_contract_items", "SHARE ROW EXCLUSIVE")),
    ("isveren", "B"): (("employer_contract_items", "SHARE ROW EXCLUSIVE"),
                       ("progress_payment_lines", "ACCESS EXCLUSIVE")),
    ("taseron", "A"): (("subcontractor_progress_payment_lines", "ACCESS EXCLUSIVE"),
                       ("subcontractor_contract_items", "SHARE ROW EXCLUSIVE")),
    ("taseron", "B"): (("subcontractor_contract_items", "SHARE ROW EXCLUSIVE"),
                       ("subcontractor_progress_payment_lines", "ACCESS EXCLUSIVE")),
}  # fmt: skip


@pytest.mark.parametrize("aile", ["isveren", "taseron"])
@pytest.mark.parametrize("senaryo", ["A", "B"])
async def test_KATB24_pozitif_kontrol_bloklayan_kilit_sirasi_deadlock_uretir(
    aile: str, senaryo: str
) -> None:
    def eski_siraya_don(module: ModuleType) -> None:
        module._acquire_all_or_nothing = _bloklayan_sira(*_BLOKLAYAN_SIRA[(aile, senaryo)])

    mig_err, t1_err, database, _ = await _senaryo(aile, senaryo, patch=eski_siraya_don)
    try:
        assert _deadlock_mu(mig_err) or _deadlock_mu(t1_err), (mig_err, t1_err)
    finally:
        await _drop_scratch_database(database)


@pytest.mark.parametrize("tablo", LINE_TABLES)
async def test_KATB24_migration_kilit_tavani_dolunca_fail_closed_surum_eski_kalir(
    tablo: str,
) -> None:
    """C: eski islem kilidi tavandan uzun tutar → LockCeilingExceededError, kolonlar YOK."""
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn, tr = await _t1_oturumu(database)
        try:
            await conn.fetchval(f"SELECT count(*) FROM {tablo}")  # noqa: S608  AccessShare

            def kisa_tavan(module: ModuleType) -> None:
                module.LOCK_CEILING_S = 0.8
                module.LOCK_RETRY_INTERVAL_S = 0.1

            with pytest.raises(Exception) as hata:  # noqa: PT011
                await asyncio.wait_for(
                    _migrate_in_process(database, "upgrade", patch=kisa_tavan), timeout=30
                )
            assert type(hata.value).__name__ == "LockCeilingExceededError", hata.value
        finally:
            await tr.rollback()  # type: ignore[attr-defined]
            await conn.close()
        check = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for t in LINE_TABLES:
                assert await _kolon(check, t) is None
        finally:
            await check.close()
        # tavan hatasindan sonra hicbir kilit/islem kalmadi: yeniden deneme basarili
        await _migrate_in_process(database, "upgrade")
    finally:
        await _drop_scratch_database(database)

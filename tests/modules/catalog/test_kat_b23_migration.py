"""KAT-B2.3 — `d9f3b7a2c4e6` migration: `subcontractor_contract_items.source_code` + backfill.

Tek kullanimlik veritabani (HZ-1 deseni): bir onceki revizyona (c7a1e5b9d3f2) cikilir, VERI
tohumlanir (kaynak isveren kalemi kodlu/kodsuz, bagli/bagsiz/SET NULL'lanmis taseron kalemi) →
upgrade (backfill) → isveren kaleminin kodu degisse/kalem silinse taseron kopyasi ETKILENMEZ →
downgrade → upgrade gidis-donus.
Kilit: metin bekcisi + DAVRANISSAL deadlock testi (iki oturum, gercek migration kodu; pozitif
kontrol: bloklayan kilit sirasi ayni senaryoda DeadlockDetected uretir) + tavan testi.
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

PREVIOUS = "c7a1e5b9d3f2"
REVISION = "d9f3b7a2c4e6"
MIGRATION = next(Path("alembic/versions").glob(f"{REVISION}_*.py"))

KOD_DOLU = "15.100.1001"
KOD_SILINEN = "15.100.1004"


async def _kolon(conn: asyncpg.Connection) -> tuple[str, str, int] | None:
    row = await conn.fetchrow(
        "SELECT is_nullable, data_type, character_maximum_length "
        "FROM information_schema.columns WHERE column_name = 'source_code' "
        "AND table_name = 'subcontractor_contract_items'"
    )
    return None if row is None else (row[0], row[1], row[2])


async def _tohum(conn: asyncpg.Connection) -> dict[str, uuid.UUID]:
    ids = {
        k: uuid.uuid4()
        for k in (
            "project",
            "cgroup",
            "e_dolu",
            "e_bos",
            "e_sil",
            "subc",
            "contract",
            "s_dolu",
            "s_bos",
            "s_baglisiz",
            "s_sil",
        )
    }
    user_id = await _seed_user(conn)
    await conn.execute(
        "INSERT INTO projects (id, code, name, budget, progress_pct) "
        "VALUES ($1, 'P-B23', 'Mig', 0, 0)",
        ids["project"],
    )
    await conn.execute("INSERT INTO project_contracts (project_id) VALUES ($1)", ids["project"])
    await conn.execute(
        "INSERT INTO employer_contract_groups (id, project_id, name) VALUES ($1, $2, 'G')",
        ids["cgroup"],
        ids["project"],
    )
    for key, code, kod in (
        ("e_dolu", "01", KOD_DOLU),
        ("e_bos", "02", None),
        ("e_sil", "04", KOD_SILINEN),
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
        user_id,
    )
    for key, code, kaynak in (
        ("s_dolu", "01", "e_dolu"),
        ("s_bos", "02", "e_bos"),
        ("s_baglisiz", "03", None),
        ("s_sil", "04", "e_sil"),
    ):
        await conn.execute(
            "INSERT INTO subcontractor_contract_items (id, contract_id, source_contract_item_id, "
            "code, description, unit, quantity) VALUES ($1, $2, $3, $4, 'Beton', 'm3', 1)",
            ids[key],
            ids["contract"],
            ids[kaynak] if kaynak else None,
            code,
        )
    return ids


async def _kopyalar(conn: asyncpg.Connection, ids: dict[str, uuid.UUID]) -> dict[str, str | None]:
    return {
        key: await conn.fetchval(
            "SELECT source_code FROM subcontractor_contract_items WHERE id=$1", ids[key]
        )
        for key in ("s_dolu", "s_bos", "s_baglisiz", "s_sil")
    }


async def test_KATB23_migration_kolon_backfill_snapshot_downgrade_ve_gidis_donus() -> None:
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            ids = await _tohum(conn)
            assert await _kolon(conn) is None
        finally:
            await conn.close()

        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _kolon(conn) == ("YES", "character varying", 32)
            # BACKFILL: bagli+kodlu → kopya; bagli+kodsuz → NULL; bagsiz → NULL (K4)
            assert await _kopyalar(conn, ids) == {
                "s_dolu": KOD_DOLU,
                "s_bos": None,
                "s_baglisiz": None,
                "s_sil": KOD_SILINEN,
            }
            # SNAPSHOT: isveren kaleminin kodu degisince kopya ESKI kodu korur
            await conn.execute(
                "UPDATE employer_contract_items SET source_code = '99.999.9999' WHERE id = $1",
                ids["e_dolu"],
            )
            await conn.execute(
                "UPDATE employer_contract_items SET source_code = '77.777.7777' WHERE id = $1",
                ids["e_bos"],
            )
            # ... ve kaynak kalem silinince (FK SET NULL) kopya KALIR
            await conn.execute("DELETE FROM employer_contract_items WHERE id = $1", ids["e_sil"])
            kopyalar = await _kopyalar(conn, ids)
            assert kopyalar == {
                "s_dolu": KOD_DOLU,
                "s_bos": None,
                "s_baglisiz": None,
                "s_sil": KOD_SILINEN,
            }
            assert (
                await conn.fetchval(
                    "SELECT source_contract_item_id FROM subcontractor_contract_items "
                    "WHERE id = $1",
                    ids["s_sil"],
                )
                is None
            )
            # kolon varsayilani yok: eski konteyner penceresi (kolonsuz INSERT) gecer
            await conn.execute(
                "INSERT INTO subcontractor_contract_items (id, contract_id, code, description, "
                "unit, quantity) VALUES ($1, $2, '09', 'd', 'm', 1)",
                uuid.uuid4(),
                ids["contract"],
            )
        finally:
            await conn.close()

        _run_alembic("downgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _kolon(conn) is None
            assert await conn.fetchval("SELECT count(*) FROM subcontractor_contract_items") == 5
        finally:
            await conn.close()

        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _kolon(conn) is not None
            # gidis-donus: backfill bugunku kaynak kodu yazar; baglari kopmus kalem NULL kalir
            kopyalar = await _kopyalar(conn, ids)
            assert kopyalar["s_dolu"] == "99.999.9999"
            assert kopyalar["s_bos"] == "77.777.7777"
            assert kopyalar["s_baglisiz"] is None and kopyalar["s_sil"] is None
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


def test_KATB23_migration_kilit_kaniti_en_basta_hep_ya_hic() -> None:
    """Kilit kanonu kaynak metinden: lock_timeout → hep-ya-hic (NOWAIT+savepoint) kilitler → ilk
    DDL → backfill. Kilit hicbir DDL'den SONRA gelmez; downgrade de ayni desende; `app` yok."""
    src = _src()
    up = _upgrade_body()
    assert (
        up.index("SET LOCAL lock_timeout = '10s'")
        < up.index("_acquire_all_or_nothing(bind, UPGRADE_LOCKS)")
        < up.index("op.add_column")
        < up.index("UPDATE subcontractor_contract_items")
    )
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
    # kaynak tablo backfill sirasinda DEGISMESIN: SRE; kopya tablo ALTER icin AE
    assert '(SUBCONTRACT_ITEMS, "ACCESS EXCLUSIVE")' in kod
    assert '(EMPLOYER_ITEMS, "SHARE ROW EXCLUSIVE")' in kod
    assert "import app" not in src and "from app" not in src
    # backfill YALNIZ bagli kalem + kodlu kaynak (K4): bagsiz/kodsuz NULL kalir
    assert "e.id = s.source_contract_item_id" in up and "e.source_code IS NOT NULL" in up


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("katb23_migration_under_test", MIGRATION)
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


async def _t1_ilk_adim(senaryo: str, conn: asyncpg.Connection, ids: dict[str, uuid.UUID]) -> None:
    if senaryo == "A":  # isveren kalemi YAZ (once) → taseron kalemi YAZ
        await conn.execute(
            "UPDATE employer_contract_items SET description = 'y' WHERE id = $1", ids["e_dolu"]
        )
    else:  # B: taseron kalemi OKU (once) → isveren kalemi YAZ
        await conn.fetchval("SELECT count(*) FROM subcontractor_contract_items")


async def _t1_calistir(
    senaryo: str, conn: asyncpg.Connection, tr: object, ids: dict[str, uuid.UUID]
) -> BaseException | None:
    """Eski konteyner islemi (uygulamadaki yollar: isveren kalemi PATCH, tekil taseron kalemi
    POST). Deadlock kurbani olursa hatayi dondurur."""
    try:
        if senaryo == "A":
            await conn.execute(
                "INSERT INTO subcontractor_contract_items (id, contract_id, "
                "source_contract_item_id, code, description, unit, quantity) "
                "VALUES ($1, $2, $3, '99', 'x', 'm', 1)",
                uuid.uuid4(),
                ids["contract"],
                ids["e_dolu"],
            )
        else:
            await conn.execute(
                "UPDATE employer_contract_items SET description = 'z' WHERE id = $1", ids["e_bos"]
            )
        await tr.commit()  # type: ignore[attr-defined]
        return None
    except Exception as exc:  # noqa: BLE001
        await tr.rollback()  # type: ignore[attr-defined]
        return exc


async def _senaryo(
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
        await _t1_ilk_adim(senaryo, conn, ids)
        mig = asyncio.create_task(_migrate_in_process(database, "upgrade", patch=patch))
        await asyncio.sleep(bekle_s)  # migration kilit denemesinde / beklemede
        t1_err = await _t1_calistir(senaryo, conn, tr, ids)
        try:
            await asyncio.wait_for(mig, timeout=60)
        except BaseException as exc:  # noqa: BLE001
            mig_err = exc
    finally:
        await conn.close()
    check = await asyncpg.connect(_asyncpg_dsn(database))
    try:
        kolon_var = await _kolon(check) is not None
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


@pytest.mark.parametrize("senaryo", ["A", "B"])
async def test_KATB23_migration_eski_konteyner_trafigiyle_deadlock_yok(senaryo: str) -> None:
    mig_err, t1_err, database, kolon_var = await _senaryo(senaryo)
    try:
        assert not _deadlock_mu(mig_err) and not _deadlock_mu(t1_err), (mig_err, t1_err)
        assert t1_err is None, t1_err
        assert mig_err is None, mig_err
        assert kolon_var
    finally:
        await _drop_scratch_database(database)


# A: T1 isveren YAZ → taseron YAZ  ⇒ cevrim icin migration taseron AE → isveren SRE sirasinda
# BEKLER. B: T1 taseron OKU → isveren YAZ ⇒ isveren SRE → taseron AE sirasinda BEKLER.
_BLOKLAYAN_SIRA = {
    "A": (("subcontractor_contract_items", "ACCESS EXCLUSIVE"),
          ("employer_contract_items", "SHARE ROW EXCLUSIVE")),
    "B": (("employer_contract_items", "SHARE ROW EXCLUSIVE"),
          ("subcontractor_contract_items", "ACCESS EXCLUSIVE")),
}  # fmt: skip


@pytest.mark.parametrize("senaryo", ["A", "B"])
async def test_KATB23_pozitif_kontrol_bloklayan_kilit_sirasi_deadlock_uretir(senaryo: str) -> None:
    def eski_siraya_don(module: ModuleType) -> None:
        module._acquire_all_or_nothing = _bloklayan_sira(*_BLOKLAYAN_SIRA[senaryo])

    mig_err, t1_err, database, _ = await _senaryo(senaryo, patch=eski_siraya_don)
    try:
        assert _deadlock_mu(mig_err) or _deadlock_mu(t1_err), (mig_err, t1_err)
    finally:
        await _drop_scratch_database(database)


async def test_KATB23_migration_kilit_tavani_dolunca_fail_closed_surum_eski_kalir() -> None:
    """C: eski islem kilidi tavandan uzun tutar → LockCeilingExceededError, kolon YOK."""
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn, tr = await _t1_oturumu(database)
        try:
            await conn.fetchval("SELECT count(*) FROM subcontractor_contract_items")  # AccessShare

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
            assert await _kolon(check) is None
        finally:
            await check.close()
        # tavan hatasindan sonra hicbir kilit/islem kalmadi: yeniden deneme basarili
        await _migrate_in_process(database, "upgrade")
    finally:
        await _drop_scratch_database(database)

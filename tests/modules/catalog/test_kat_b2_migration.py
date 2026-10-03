"""KAT-B2.1 — `c7a1e5b9d3f2` migration: iki kalem tablosuna `source_code` + backfill.

Tek kullanimlik veritabani (HZ-1 deseni): bir onceki revizyona cikilir, VERI tohumlanir (katalog
kodu dolu/bos, bagli/baglisiz sozlesme kalemi) → upgrade (backfill) → katalog degisimi kopyayi
ETKILEMEZ → downgrade (kolonlar yok) → upgrade gidis-donus.
Kilit: metin bekcisi + DAVRANISSAL deadlock testi (iki oturum, gercek migration kodu; pozitif
kontrol: eski bloklayan kilit sirasi ayni senaryoda DeadlockDetected uretir).
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
)

PREVIOUS = "b4e8d2f6a1c9"
REVISION = "c7a1e5b9d3f2"
MIGRATION = next(Path("alembic/versions").glob(f"{REVISION}_*.py"))


async def _kolonlar(conn: asyncpg.Connection) -> dict[str, tuple[str, str, int]]:
    rows = await conn.fetch(
        "SELECT table_name, is_nullable, data_type, character_maximum_length "
        "FROM information_schema.columns WHERE column_name = 'source_code' "
        "AND table_name IN ('offer_items', 'employer_contract_items')"
    )
    return {
        r["table_name"]: (r["is_nullable"], r["data_type"], r["character_maximum_length"])
        for r in rows
    }


async def _tohum(conn: asyncpg.Connection) -> dict[str, uuid.UUID]:
    ids = {
        k: uuid.uuid4()
        for k in (
            "disc",
            "cat_dolu",
            "cat_bos",
            "employer",
            "offer",
            "rev",
            "group",
            "oi_dolu",
            "oi_bos",
            "project",
            "cgroup",
            "ci_bagli_dolu",
            "ci_bagli_bos",
            "ci_baglisiz",
        )
    }
    await conn.execute(
        "INSERT INTO ev_disciplines (id, code, name, color, default_contractor_type) "
        "VALUES ($1, 'MIG', 'Mig', '#2563EB', 'own')",
        ids["disc"],
    )
    for key, ad, poz, kod in (
        ("cat_dolu", "k1", "MIG-0001", "15.100.1001"),
        ("cat_bos", "k2", "MIG-0002", None),
    ):
        await conn.execute(
            "INSERT INTO ev_catalog_items (id, discipline_id, name, uom, name_key, uom_key, "
            "standard_unit_mhr, default_contractor_type, poz_no, source_code) "
            "VALUES ($1, $2, $3, 'm', $3, 'm', 1, 'own', $4, $5)",
            ids[key],
            ids["disc"],
            ad,
            poz,
            kod,
        )
    await conn.execute("INSERT INTO employers (id, name) VALUES ($1, 'İ')", ids["employer"])
    await conn.execute(
        "INSERT INTO offers (id, offer_no, employer_id, employer_name, title) "
        "VALUES ($1, 'TKL-2026-0001', $2, 'İ', 'T')",
        ids["offer"],
        ids["employer"],
    )
    await conn.execute(
        "INSERT INTO offer_revisions (id, offer_id, rev_no, offer_date, overhead_pct, "
        "profit_pct, vat_pct) VALUES ($1, $2, 0, '2026-10-02', 12, 15, 20)",
        ids["rev"],
        ids["offer"],
    )
    await conn.execute(
        "INSERT INTO offer_groups (id, revision_id, name) VALUES ($1, $2, 'A')",
        ids["group"],
        ids["rev"],
    )
    for key, cat, poz in (("oi_dolu", "cat_dolu", "MIG-0001"), ("oi_bos", "cat_bos", "MIG-0002")):
        await conn.execute(
            "INSERT INTO offer_items (id, revision_id, group_id, catalog_item_id, poz_no, "
            "description, unit, quantity, unit_mhr) VALUES ($1, $2, $3, $4, $5, 'd', 'm', 1, 1)",
            ids[key],
            ids["rev"],
            ids["group"],
            ids[cat],
            poz,
        )
    await conn.execute(
        "INSERT INTO projects (id, code, name, budget, progress_pct) "
        "VALUES ($1, 'P-MIG', 'Mig', 0, 0)",
        ids["project"],
    )
    await conn.execute("INSERT INTO project_contracts (project_id) VALUES ($1)", ids["project"])
    await conn.execute(
        "INSERT INTO employer_contract_groups (id, project_id, name) VALUES ($1, $2, 'G')",
        ids["cgroup"],
        ids["project"],
    )
    for key, code, cat in (
        ("ci_bagli_dolu", "01", "cat_dolu"),
        ("ci_bagli_bos", "02", "cat_bos"),
        ("ci_baglisiz", "03", None),
    ):
        await conn.execute(
            "INSERT INTO employer_contract_items (id, project_id, group_id, code, description, "
            "unit, quantity, unit_price, catalog_item_id) "
            "VALUES ($1, $2, $3, $4, 'Beton', 'm3', 1, 100, $5)",
            ids[key],
            ids["project"],
            ids["cgroup"],
            code,
            ids[cat] if cat else None,
        )
    return ids


async def _kopyalar(conn: asyncpg.Connection, ids: dict[str, uuid.UUID]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for key in ("oi_dolu", "oi_bos"):
        out[key] = await conn.fetchval("SELECT source_code FROM offer_items WHERE id=$1", ids[key])
    for key in ("ci_bagli_dolu", "ci_bagli_bos", "ci_baglisiz"):
        out[key] = await conn.fetchval(
            "SELECT source_code FROM employer_contract_items WHERE id=$1", ids[key]
        )
    return out


async def test_KATB2_migration_kolon_backfill_snapshot_downgrade_ve_gidis_donus() -> None:
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            ids = await _tohum(conn)
            assert await _kolonlar(conn) == {}
        finally:
            await conn.close()

        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            varchar32 = ("YES", "character varying", 32)
            assert await _kolonlar(conn) == {
                "offer_items": varchar32,
                "employer_contract_items": varchar32,
            }
            # BACKFILL: katalog kodu dolu → kopyalandi; bos → NULL; baglisiz sozlesme kalemi NULL
            assert await _kopyalar(conn, ids) == {
                "oi_dolu": "15.100.1001",
                "oi_bos": None,
                "ci_bagli_dolu": "15.100.1001",
                "ci_bagli_bos": None,
                "ci_baglisiz": None,
            }
            # SNAPSHOT: katalogdaki kod degisince / temizlenince kopya ESKI kodu korur
            await conn.execute(
                "UPDATE ev_catalog_items SET source_code = '99.999.9999' WHERE id = $1",
                ids["cat_dolu"],
            )
            await conn.execute(
                "UPDATE ev_catalog_items SET source_code = '77.777.7777' WHERE id = $1",
                ids["cat_bos"],
            )
            kopyalar = await _kopyalar(conn, ids)
            assert kopyalar["oi_dolu"] == kopyalar["ci_bagli_dolu"] == "15.100.1001"
            assert kopyalar["oi_bos"] is None and kopyalar["ci_bagli_bos"] is None
            # kolon varsayilani yok: eski konteyner penceresi (kolonsuz INSERT) gecer
            await conn.execute(
                "INSERT INTO offer_items (id, revision_id, group_id, catalog_item_id, poz_no, "
                "description, unit, quantity, unit_mhr) "
                "VALUES ($1, $2, $3, $4, 'MIG-0001', 'd', 'm', 1, 1)",
                uuid.uuid4(),
                ids["rev"],
                ids["group"],
                ids["cat_dolu"],
            )
        finally:
            await conn.close()

        _run_alembic("downgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _kolonlar(conn) == {}
            assert await conn.fetchval("SELECT count(*) FROM offer_items") == 3
            assert await conn.fetchval("SELECT count(*) FROM employer_contract_items") == 3
        finally:
            await conn.close()

        _run_alembic("upgrade", REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert len(await _kolonlar(conn)) == 2
            # gidis-donus: backfill bugunku katalog kodunu yazar (snapshot'i sifirlayan downgrade)
            assert (await _kopyalar(conn, ids))["oi_dolu"] == "99.999.9999"
            assert (await _kopyalar(conn, ids))["ci_baglisiz"] is None
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


def test_KATB2_migration_kilit_kaniti_en_basta_hep_ya_hic() -> None:
    """Kilit kanonu kaynak metinden: lock_timeout → hep-ya-hic (NOWAIT+savepoint) kilitler → ilk
    DDL. Kilit hicbir DDL'den SONRA gelmez; downgrade de ayni desende; `app` import edilmez."""
    src = _src()
    up = _upgrade_body()
    assert (
        up.index("SET LOCAL lock_timeout = '10s'")
        < up.index("_acquire_all_or_nothing(bind, UPGRADE_LOCKS)")
        < up.index("op.add_column")
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
    # bekleyen (NOWAIT'siz) LOCK TABLE kalmamali
    kod = src.split("revision: str", 1)[1]  # docstring haric
    assert kod.count("LOCK TABLE") == kod.count("MODE NOWAIT") == 1
    assert "import app" not in src and "from app" not in src


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("katb2_migration_under_test", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _eski_bloklayan_kilit_sirasi(bind: sa.Connection, _locks: object) -> None:
    """Onarimdan ONCEKI davranis (pozitif kontrol): tek tek BEKLEYEN kilitler, AE → SRE."""
    bind.execute(
        sa.text("LOCK TABLE employer_contract_items, offer_items IN ACCESS EXCLUSIVE MODE")
    )
    bind.execute(sa.text("LOCK TABLE ev_catalog_items IN SHARE ROW EXCLUSIVE MODE"))


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


async def _t1_calistir(
    senaryo: str, conn: asyncpg.Connection, tr: object, ids: dict[str, uuid.UUID], database: str
) -> BaseException | None:
    """Eski konteyner islemi. T1 once ilk kilidini alir (cagiran), migration basladiktan sonra
    ikinci adimi atar; deadlock kurbani olursa hatayi dondurur."""
    try:
        if senaryo == "A":  # katalog YAZ (once) → kalem tablosu OKU
            await conn.fetchval("SELECT count(*) FROM employer_contract_items")
        else:  # B: teklif kalemi OKU (once) → sozlesme kalemi YAZ
            await conn.execute(
                "INSERT INTO employer_contract_items (id, project_id, group_id, code, "
                "description, unit, quantity, unit_price, catalog_item_id) "
                "VALUES ($1,$2,$3,'99','x','m',1,1,$4)",
                uuid.uuid4(),
                ids["project"],
                ids["cgroup"],
                ids["cat_dolu"],
            )
        await tr.commit()  # type: ignore[attr-defined]
        return None
    except Exception as exc:  # noqa: BLE001
        await tr.rollback()  # type: ignore[attr-defined]
        return exc


async def _t1_ilk_adim(senaryo: str, conn: asyncpg.Connection, ids: dict[str, uuid.UUID]) -> None:
    if senaryo == "A":
        await conn.execute(
            "UPDATE ev_catalog_items SET ref_price = 5 WHERE id = $1", ids["cat_dolu"]
        )
    else:
        await conn.fetchval("SELECT count(*) FROM offer_items")


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
        t1_err = await _t1_calistir(senaryo, conn, tr, ids, database)
        try:
            await asyncio.wait_for(mig, timeout=60)
        except BaseException as exc:  # noqa: BLE001
            mig_err = exc
    finally:
        await conn.close()
    check = await asyncpg.connect(_asyncpg_dsn(database))
    try:
        kolon_var = len(await _kolonlar(check)) == 2
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
async def test_KATB2_migration_eski_konteyner_trafigiyle_deadlock_yok(senaryo: str) -> None:
    mig_err, t1_err, database, kolon_var = await _senaryo(senaryo)
    try:
        assert not _deadlock_mu(mig_err) and not _deadlock_mu(t1_err), (mig_err, t1_err)
        assert t1_err is None, t1_err
        assert mig_err is None, mig_err
        assert kolon_var
    finally:
        await _drop_scratch_database(database)


@pytest.mark.parametrize("senaryo", ["A", "B"])
async def test_KATB2_pozitif_kontrol_eski_kilit_sirasi_deadlock_uretir(senaryo: str) -> None:
    def eski_siraya_don(module: ModuleType) -> None:
        module._acquire_all_or_nothing = _eski_bloklayan_kilit_sirasi

    mig_err, t1_err, database, _ = await _senaryo(senaryo, patch=eski_siraya_don)
    try:
        assert _deadlock_mu(mig_err) or _deadlock_mu(t1_err), (mig_err, t1_err)
    finally:
        await _drop_scratch_database(database)


async def test_KATB2_migration_kilit_tavani_dolunca_fail_closed_surum_eski_kalir() -> None:
    """C: eski islem kilidi tavandan uzun tutar → LockCeilingExceededError, kolon YOK."""
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn, tr = await _t1_oturumu(database)
        try:
            await conn.fetchval("SELECT count(*) FROM offer_items")  # AccessShare tutuyor

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
            assert await _kolonlar(check) == {}
        finally:
            await check.close()
        # tavan hatasindan sonra hicbir kilit/islem kalmadi: yeniden deneme basarili
        await _migrate_in_process(database, "upgrade")
    finally:
        await _drop_scratch_database(database)

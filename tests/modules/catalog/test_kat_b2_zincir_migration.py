"""KAT-B2.4a — ZINCIR testi: katalog → isveren kalemi → tasaron kalemi → hakedis satirlari.

c7a1e5b9d3f2 (isveren kalemi ← katalog) → d9f3b7a2c4e6 (taseron kalemi ← isveren kalemi) →
e4a8c2d6f1b3 (hakedis satirlari ← kalem) ZINCIRI tek `alembic upgrade head` cagrisinda sinanir.
Tekil migration testleri sonraki halkanin girdisini elle tohumlar; bu yuzden halkalar arasi
siralama/bagimlilik (ornegin taseron satirinin kodu, d9f3'un yazdigi taseron kalemi kodundan
gelir) yalniz burada olculur.
"""

from __future__ import annotations

import uuid

import asyncpg

from tests.modules.treasury.test_hz1_migration import (
    _asyncpg_dsn,
    _create_scratch_database,
    _drop_scratch_database,
    _run_alembic,
    _seed_user,
)

BASE = "b4e8d2f6a1c9"  # KAT-B2.1 oncesi: hicbir tabloda source_code yok
KOD = "15.100.1001"


async def _tohum(conn: asyncpg.Connection) -> dict[str, uuid.UUID]:
    keys = (
        "disc", "cat", "project", "cgroup", "site", "e_bagli", "e_baglisiz", "subc", "contract",
        "s_bagli", "s_baglisiz", "pp", "spp", "pl_bagli", "pl_baglisiz", "sl_bagli", "sl_baglisiz",
    )  # fmt: skip
    ids = {k: uuid.uuid4() for k in keys}
    user = await _seed_user(conn)
    await conn.execute(
        "INSERT INTO ev_disciplines (id, code, name, color, default_contractor_type) "
        "VALUES ($1, 'ZIN', 'Zincir', '#2563EB', 'own')",
        ids["disc"],
    )
    await conn.execute(
        "INSERT INTO ev_catalog_items (id, discipline_id, name, uom, name_key, uom_key, "
        "standard_unit_mhr, default_contractor_type, poz_no, source_code) "
        "VALUES ($1, $2, 'k', 'm', 'k', 'm', 1, 'own', 'ZIN-0001', $3)",
        ids["cat"],
        ids["disc"],
        KOD,
    )
    await conn.execute(
        "INSERT INTO projects (id, code, name, budget, progress_pct) VALUES ($1,'P-Z','Z',0,0)",
        ids["project"],
    )
    await conn.execute("INSERT INTO project_contracts (project_id) VALUES ($1)", ids["project"])
    await conn.execute(
        "INSERT INTO sites (id, project_id, code, name) VALUES ($1, $2, 'S', 'S')",
        ids["site"],
        ids["project"],
    )
    await conn.execute(
        "INSERT INTO employer_contract_groups (id, project_id, name) VALUES ($1, $2, 'G')",
        ids["cgroup"],
        ids["project"],
    )
    for key, code, cat in (("e_bagli", "01", ids["cat"]), ("e_baglisiz", "02", None)):
        await conn.execute(
            "INSERT INTO employer_contract_items (id, project_id, group_id, code, description, "
            "unit, quantity, unit_price, catalog_item_id) "
            "VALUES ($1, $2, $3, $4, 'B', 'm3', 1, 100, $5)",
            ids[key],
            ids["project"],
            ids["cgroup"],
            code,
            cat,
        )
    await conn.execute("INSERT INTO subcontractors (id, name) VALUES ($1, 'T')", ids["subc"])
    await conn.execute(
        "INSERT INTO subcontractor_contracts (id, project_id, subcontractor_id, created_by) "
        "VALUES ($1, $2, $3, $4)",
        ids["contract"],
        ids["project"],
        ids["subc"],
        user,
    )
    for key, code, src in (("s_bagli", "01", "e_bagli"), ("s_baglisiz", "02", "e_baglisiz")):
        await conn.execute(
            "INSERT INTO subcontractor_contract_items (id, contract_id, source_contract_item_id, "
            "code, description, unit, quantity, unit_price) "
            "VALUES ($1, $2, $3, $4, 'B', 'm3', 1, 50)",
            ids[key],
            ids["contract"],
            ids[src],
            code,
        )
    await conn.execute(
        "INSERT INTO progress_payments (id, project_id, sequence_no, vat_pct, advance_pct, "
        "retainage_pct, created_by, status) VALUES ($1, $2, 1, 20, 0, 0, $3, 'approved')",
        ids["pp"],
        ids["project"],
        user,
    )
    await conn.execute(
        "INSERT INTO subcontractor_progress_payments (id, contract_id, project_id, sequence_no, "
        "vat_pct, advance_pct, retainage_pct, created_by, status) "
        "VALUES ($1, $2, $3, 1, 20, 0, 0, $4, 'paid')",
        ids["spp"],
        ids["contract"],
        ids["project"],
        user,
    )
    for key, item, code in (("pl_bagli", "e_bagli", "01"), ("pl_baglisiz", "e_baglisiz", "02")):
        await conn.execute(
            "INSERT INTO progress_payment_lines (id, payment_id, contract_item_id, site_id, code, "
            "description, unit, contract_unit_price, quantity) "
            "VALUES ($1, $2, $3, $4, $5, 'B', 'm3', 100, 1)",
            ids[key],
            ids["pp"],
            ids[item],
            ids["site"],
            code,
        )
    for key, item, code in (("sl_bagli", "s_bagli", "01"), ("sl_baglisiz", "s_baglisiz", "02")):
        await conn.execute(
            "INSERT INTO subcontractor_progress_payment_lines (id, payment_id, contract_item_id, "
            "code, description, unit, contract_unit_price, quantity) "
            "VALUES ($1, $2, $3, $4, 'B', 'm3', 50, 1)",
            ids[key],
            ids["spp"],
            ids[item],
            code,
        )
    return ids


async def test_KATB2_zincir_katalog_kodu_tek_upgrade_head_ile_dort_tabloya_akar() -> None:
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", BASE, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            ids = await _tohum(conn)
        finally:
            await conn.close()

        _run_alembic("upgrade", "head", database=database)  # TEK cagri: c7a1 → d9f3 → e4a8

        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:

            async def kod(tablo: str, key: str) -> str | None:
                return await conn.fetchval(
                    f"SELECT source_code FROM {tablo} WHERE id = $1",  # noqa: S608
                    ids[key],
                )

            assert await kod("employer_contract_items", "e_bagli") == KOD
            assert await kod("subcontractor_contract_items", "s_bagli") == KOD
            assert await kod("progress_payment_lines", "pl_bagli") == KOD
            assert await kod("subcontractor_progress_payment_lines", "sl_bagli") == KOD
            # katalogsuz kol: zincirin hicbir halkasinda kod uretilmez
            assert await kod("employer_contract_items", "e_baglisiz") is None
            assert await kod("subcontractor_contract_items", "s_baglisiz") is None
            assert await kod("progress_payment_lines", "pl_baglisiz") is None
            assert await kod("subcontractor_progress_payment_lines", "sl_baglisiz") is None
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

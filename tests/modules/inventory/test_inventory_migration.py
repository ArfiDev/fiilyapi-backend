"""ST T1 — stok cekirdegi: 4 tablo + 3 enum (spec §2).

Neden ayri bir tur donusu testi: bu migration DORT YENI TABLO ve UC YENI ENUM
getiriyor. Enum'lari dusurmeyi unutan bir `downgrade` ikinci upgrade'i
"type already exists" ile patlatirdi (d4e5f6a7b8c9 dersi) ve bu yalniz CANLIDA
gorulurdu. Ayrica FK silme davranislari burada SEMANTIKTIR:
  * hareketi olan bir depo/kart SILINEMEZ (RESTRICT) — bakiye tarihi bozulmaz
  * baslik silinince satirlari DUSER (CASCADE) — yetim satir bakiyeyi sisirirdi
  * santiye silinince merkez-olmayan depo KALIR, santiye bagi kopar (SET NULL)

Test kendi TEK KULLANIMLIK veritabanini acar ve sonunda dusurur; `.env` ve
`TEST_DATABASE_URL` veritabani ELLENMEZ. Alembic alt surecte kosturulur cunku
`alembic/env.py` kendi `asyncio.run()` dongusunu kurar ve calisan bir
pytest-asyncio dongusunun icinden cagrilamaz (P11 deseni).
"""

import os
import subprocess
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.modules.inventory.models import (
    StockCategory,
    StockEntry,
    StockEntryLine,
    StockEntryType,
    StockItem,
    StockQuality,
    Warehouse,
)

BACKEND_DIR = Path(__file__).parents[3]
# `python -m alembic`: yerelde `.venv/bin/python`, CI'da sistem Python'u. Sabit
# `.venv/bin/alembic` yolu CI'da YOKTUR ve testi yalniz orada kirardi.
ALEMBIC_CMD = (sys.executable, "-m", "alembic")

# Revizyonlara ACIKCA cikilir; `head` / `-1` KULLANILMAZ — sonraki dilimler
# revizyon ekledikce bu test sessizce yanlis seyi olcerdi.
PARENT_REVISION = "d0e1f2a3b4c5"  # P11 takvim/gantt
ST_REVISION = "e1f2a3b4c5d6"

TABLES = ("stock_items", "warehouses", "stock_entries", "stock_entry_lines")
ENUMS = ("stock_category", "stock_entry_type", "stock_quality")
INDEXES = (
    "ix_warehouses_site_id",
    "ix_stock_entries_warehouse_id",
    "ix_stock_entries_source_warehouse_id",
    "ix_stock_entries_entry_date",
    "ix_stock_entry_lines_entry_id",
    "ix_stock_entry_lines_item_id",
)

# Kalici kararlar (spec §5). Bu adlar tesadufi degil — ileride "kucuk bir ekleme"
# diye geri sizmalarina karsi korkuluk. Sipariş/tedarikci/belge/bolum-ihtiyac
# SA ve BC dilimlerinin isidir; bakiye ise TUREVDIR, kolon DEGILDIR.
FORBIDDEN_ITEM_COLUMNS = ("balance", "current_stock", "stock_qty", "monthly_need", "section_id")
FORBIDDEN_WAREHOUSE_COLUMNS = ("balance", "current_stock")
# `purchase_order_id` 2026-08-12'de SA T1 (`f3a4b5c6d7e8`) ile ACILDI ve artik
# yasakli degildir — SG 85 "Ilgili Siparis" gercege dondu. Yasakli kalanlar:
# `supplier_id` (tedarikci HALA serbest metindir — kayitli karar) ve `document_id`.
FORBIDDEN_ENTRY_COLUMNS = ("order_id", "supplier_id", "document_id")
FORBIDDEN_LINE_COLUMNS = ("line_total", "amount", "order_id", "document_id", "balance_after")


def _asyncpg_dsn(database: str) -> str:
    base = settings.test_database_url.replace("postgresql+asyncpg://", "postgresql://")
    return base.rsplit("/", 1)[0] + f"/{database}"


def _run_alembic(*args: str, database: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": _asyncpg_dsn(database)}
    result = subprocess.run(
        [*ALEMBIC_CMD, *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic {' '.join(args)} basarisiz:\n{result.stdout}\n{result.stderr}")
    return result


async def _table_exists(conn: asyncpg.Connection, table: str) -> bool:
    return await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}")


async def _enum_exists(conn: asyncpg.Connection, name: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_type WHERE typname = $1 AND typtype = 'e')", name
    )


async def _enum_labels(conn: asyncpg.Connection, name: str) -> list[str]:
    return await conn.fetchval(
        "SELECT enum_range(NULL::" + name + ")::text[]",
    )


async def _index_exists(conn: asyncpg.Connection, name: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = $1)", name
    )


async def _current_revision(conn: asyncpg.Connection) -> str | None:
    return await conn.fetchval("SELECT version_num FROM alembic_version")


async def _create_scratch_database() -> str:
    database = f"inventory_st_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(_asyncpg_dsn("postgres"))
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()
    return database


async def _drop_scratch_database(database: str) -> None:
    admin = await asyncpg.connect(_asyncpg_dsn("postgres"))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    finally:
        await admin.close()


async def _seed_site(conn: asyncpg.Connection, code: str) -> uuid.UUID:
    project_id, site_id = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO projects (id, code, name, status, budget, progress_pct) "
        "VALUES ($1, $2, 'ST Tur Donusu', 'active', 0, 0)",
        project_id,
        code,
    )
    await conn.execute(
        "INSERT INTO sites (id, project_id, code, name) VALUES ($1, $2, $3, 'ST Santiye')",
        site_id,
        project_id,
        code,
    )
    return site_id


async def _seed_warehouse(
    conn: asyncpg.Connection, name: str, site_id: uuid.UUID | None = None
) -> uuid.UUID:
    warehouse_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO warehouses (id, name, site_id) VALUES ($1, $2, $3)",
        warehouse_id,
        name,
        site_id,
    )
    return warehouse_id


async def _seed_item(conn: asyncpg.Connection, code: str) -> uuid.UUID:
    item_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO stock_items (id, code, name, category, unit, is_active) "
        "VALUES ($1, $2, 'Cimento', 'structural', 'Ton', true)",
        item_id,
        code,
    )
    return item_id


async def _seed_entry(conn: asyncpg.Connection, warehouse_id: uuid.UUID) -> uuid.UUID:
    entry_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO stock_entries (id, entry_type, entry_date, warehouse_id) "
        "VALUES ($1, 'purchase', $2, $3)",
        entry_id,
        date(2026, 8, 11),
        warehouse_id,
    )
    return entry_id


async def _seed_line(
    conn: asyncpg.Connection, entry_id: uuid.UUID, item_id: uuid.UUID, quantity: str
) -> uuid.UUID:
    line_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO stock_entry_lines (id, entry_id, item_id, quantity, quality) "
        "VALUES ($1, $2, $3, $4, 'ok')",
        line_id,
        entry_id,
        item_id,
        Decimal(quantity),
    )
    return line_id


# --------------------------------------------------------------------------- #
# Model katmani
# --------------------------------------------------------------------------- #


def test_enum_values_match_spec():
    """Enum degerleri spec §2 BIREBIR — mockup'un kategori/tip/kalite kumesi."""
    assert [e.value for e in StockCategory] == [
        "structural",
        "steel",
        "electrical",
        "mechanical",
        "interior",
    ]
    assert [e.value for e in StockEntryType] == ["purchase", "transfer", "adjustment"]
    assert [e.value for e in StockQuality] == ["ok", "defective", "rejected"]


def test_stock_item_columns_match_spec():
    columns = StockItem.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "code",
        "name",
        "category",
        "unit",
        "min_stock",
        "is_active",
        "created_at",
        "updated_at",
    }
    assert columns["code"].type.length == 30
    assert columns["code"].unique
    assert columns["name"].type.length == 200
    # `unit` SERBEST METINDIR — mockup kumesi acik uclu (Ton/Torba/Metre/Adet/m³).
    assert columns["unit"].type.length == 20
    assert not isinstance(columns["unit"].type, type(columns["category"].type))
    assert columns["min_stock"].nullable, "min_stock yoksa durum None'dir (spec §3)"


def test_warehouse_site_is_nullable_with_scoped_unique():
    """`site_id IS NULL` = merkez depo (SG 84). Zorunlu olsaydi merkez depo
    modellenemezdi. UQ (site_id, name) ayni santiyede ayni ad iki kez acilmasin."""
    columns = Warehouse.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "name",
        "site_id",
        "created_at",
        "updated_at",
    }
    assert columns["name"].type.length == 100
    assert columns["site_id"].nullable
    (foreign_key,) = tuple(columns["site_id"].foreign_keys)
    assert foreign_key.column.table.name == "sites"
    assert foreign_key.ondelete == "SET NULL"
    unique = {
        tuple(c.name for c in constraint.columns)
        for constraint in Warehouse.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("site_id", "name") in unique


def test_stock_entry_columns_and_delete_semantics():
    columns = StockEntry.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "entry_type",
        "entry_date",
        "warehouse_id",
        "source_warehouse_id",
        "supplier_name",
        # SA T1 (`f3a4b5c6d7e8`) ile ADDITIVE olarak eklendi — SG 85 "Ilgili Siparis".
        "purchase_order_id",
        "delivery_note_no",
        "received_by_user_id",
        "note",
        "created_at",
        "updated_at",
    }
    assert not columns["warehouse_id"].nullable
    (warehouse_fk,) = tuple(columns["warehouse_id"].foreign_keys)
    # RESTRICT: hareketi olan depo silinemez, yoksa bakiye tarihi delinir.
    assert warehouse_fk.ondelete == "RESTRICT"
    assert columns["source_warehouse_id"].nullable, "yalniz transfer doldurur"
    (source_fk,) = tuple(columns["source_warehouse_id"].foreign_keys)
    assert source_fk.ondelete == "RESTRICT"
    # Tedarikci SERBEST METIN (spec §7 S3) — FK DEGIL.
    assert columns["supplier_name"].type.length == 200
    assert not columns["supplier_name"].foreign_keys
    assert columns["delivery_note_no"].type.length == 50
    (user_fk,) = tuple(columns["received_by_user_id"].foreign_keys)
    assert user_fk.ondelete == "SET NULL"


def test_stock_entry_line_columns_and_delete_semantics():
    """🔴 STOK-BOLUM (2026-08-29): kume IKI ADDITIVE kolonla genisledi.

    `section_id` ve `boq_item_id` SATIR bazindadir (kullanici karari): tek bir
    sarf fisi ayni gun farkli malzemeleri farkli pozlara cikarabilir. Ikisi de
    NULLABLE'dir — merkez depoya alim, transfer ve sayim duzeltmesi gibi
    hareketlerin bolumu YOKTUR ve zorunlu yapmak mevcut akislari 422'ye
    dusururdu.
    """
    columns = StockEntryLine.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "entry_id",
        "item_id",
        "quantity",
        "unit_price",
        "quality",
        "section_id",
        "boq_item_id",
    }
    (entry_fk,) = tuple(columns["entry_id"].foreign_keys)
    assert entry_fk.ondelete == "CASCADE", "yetim satir bakiyeyi sisirir"
    (item_fk,) = tuple(columns["item_id"].foreign_keys)
    assert item_fk.ondelete == "RESTRICT", "hareketi olan kart silinemez"
    assert not columns["quantity"].nullable
    assert columns["unit_price"].nullable

    # 🔴 SET NULL, CASCADE DEGIL: bolum/poz dusurulse de stok hareketi KALIR.
    # CASCADE bir bolumun silinmesini BAKIYE degisikligine cevirirdi.
    (section_fk,) = tuple(columns["section_id"].foreign_keys)
    (boq_fk,) = tuple(columns["boq_item_id"].foreign_keys)
    assert (section_fk.ondelete, boq_fk.ondelete) == ("SET NULL", "SET NULL"), (
        "atif FK'lari CASCADE'e cevrilmis — bolum silmek bakiyeyi degistirir"
    )
    assert columns["section_id"].nullable and columns["boq_item_id"].nullable


def test_quantity_has_no_sign_constraint():
    """`adjustment` satirlari NEGATIF olabilir (spec §7 S4): sayim farki/iade/sarf
    tek kapisidir. DB'de isaret kisiti KOYULMAZ — tip kurallari uygulama
    katmanindadir (T3), cunku `purchase` >0 kurali tipe BAGLIDIR."""
    checks = [
        constraint
        for constraint in StockEntryLine.__table__.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    ]
    assert not checks, "miktara DB kisiti konmus — negatif duzeltme kilitlenir"


def test_forbidden_columns_are_absent():
    """Kalici kararlar (spec §5): bakiye TUREVDIR (kolon YOK) · siparis FK'si,
    tedarikci FK'si, belge alani, bolum-ihtiyac kolonu ACILMAZ (SA/BC dilimleri)."""
    for name in FORBIDDEN_ITEM_COLUMNS:
        assert name not in StockItem.__table__.columns, f"stock_items.{name} acilmamaliydi"
    for name in FORBIDDEN_WAREHOUSE_COLUMNS:
        assert name not in Warehouse.__table__.columns, f"warehouses.{name} acilmamaliydi"
    for name in FORBIDDEN_ENTRY_COLUMNS:
        assert name not in StockEntry.__table__.columns, f"stock_entries.{name} acilmamaliydi"
    for name in FORBIDDEN_LINE_COLUMNS:
        assert name not in StockEntryLine.__table__.columns, (
            f"stock_entry_lines.{name} acilmamaliydi — satir tutari TUREVDIR"
        )


def test_permission_module_already_seeded():
    """Spec §7 S5: seed'de stok anahtari VARSA o kullanilir. `inventory` zaten
    seed'de ("Stok & Depo", STOK_SATINALMA) — bu yuzden 21. modul ACILMAZ, izin
    migration'i YOKTUR. Anahtar bir gun degisirse bu test ST'nin izin
    varsayimini dusurur ve uclar sessizce yetkisiz kalmaz."""
    from app.core.sayfalar import MODUL_ANAHTARLARI
    from app.modules.roles.seed_data import MATRIX

    keys = set(MODUL_ANAHTARLARI)
    assert "inventory" in keys, "ST uclarinin dayandigi izin modulu seed'den kalkmis"
    assert "inventory" in MATRIX
    assert "stock" not in keys, "ikinci bir stok modulu acilmis — tek anahtar `inventory`"


def test_supplier_and_order_tables_belong_to_procurement():
    """SA tablolari ST modulunde DEGIL, `procurement` modulunde yasar.

    Test 2026-08-12'de SA T1 ile guncellendi: `suppliers`/`purchase_orders`
    artik VARDIR (ST spec §5 onlari yalnizca ST DILIMINDEN disliyordu). Olcum
    "yoklar mi"dan "hangi modulun dosyasinda tanimlilar mi"ya dondu — ST'nin
    kendi modulune sizmalarina karsi korkuluk ayakta kalsin.

    `stock_consumptions` ise HALA ACILMAZ: sarf/cikis tek kapisi `adjustment`
    satirinin negatif miktaridir (ST §7 S4).
    """
    from app.core.db import Base
    from app.modules.procurement import models as procurement_models

    procurement_tables = {
        cls.__tablename__
        for cls in vars(procurement_models).values()
        if isinstance(cls, type) and hasattr(cls, "__tablename__")
    }
    for table in ("suppliers", "purchase_orders", "purchase_requests"):
        assert table in Base.metadata.tables, f"{table} SA T1'de acilmis olmaliydi"
        assert table in procurement_tables, f"{table} `procurement` modulunde tanimli degil"
    assert "stock_consumptions" not in Base.metadata.tables, (
        "sarf/cikis tablosu acilmis — tek kapi `adjustment` negatif miktaridir"
    )


# --------------------------------------------------------------------------- #
# Migration tur donusu
# --------------------------------------------------------------------------- #


def test_alembic_has_single_head():
    """Iki head = canlida deploy kilitlenmesi (`alembic upgrade head` patlar)."""
    result = subprocess.run(
        [*ALEMBIC_CMD, "heads"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    # Yalniz SAYI olculur; head KIMLIGI iddia edilmez — sonraki dilim head'i
    # ileri tasidiginda bu test ilgisiz yere kirilirdi (P11'in dersi).
    # ST'nin kendi revizyonu tur donusu testinde ACIKCA kullanilir.
    assert len(heads) == 1, f"tek head bekleniyordu, cikti:\n{result.stdout}"


async def test_upgrade_downgrade_upgrade_round_trip():
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", ST_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for table in TABLES:
                assert await _table_exists(conn, table), table
            for enum_name in ENUMS:
                assert await _enum_exists(conn, enum_name), enum_name
            for index in INDEXES:
                assert await _index_exists(conn, index), index
            assert await _enum_labels(conn, "stock_category") == [
                "structural",
                "steel",
                "electrical",
                "mechanical",
                "interior",
            ]
            assert await _enum_labels(conn, "stock_entry_type") == [
                "purchase",
                "transfer",
                "adjustment",
            ]
            assert await _enum_labels(conn, "stock_quality") == ["ok", "defective", "rejected"]
            assert await _current_revision(conn) == ST_REVISION
        finally:
            await conn.close()

        _run_alembic("downgrade", PARENT_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for table in TABLES:
                # Kalan bir tablo IKINCI upgrade'i "already exists" ile patlatirdi.
                assert not await _table_exists(conn, table), table
            for enum_name in ENUMS:
                # Enum tablolarla BIRLIKTE dusmez: acikca DROP TYPE gerekir.
                assert not await _enum_exists(conn, enum_name), (
                    f"{enum_name} tipi downgrade'de kalmis — ikinci upgrade patlar"
                )
            assert await _current_revision(conn) == PARENT_REVISION
        finally:
            await conn.close()

        _run_alembic("upgrade", ST_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for table in TABLES:
                assert await _table_exists(conn, table), table
            assert await _current_revision(conn) == ST_REVISION
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_db_level_semantics():
    """DB seviyesinde: kart kodu UQ · depo UQ · RESTRICT · CASCADE · SET NULL ·
    negatif miktar SERBEST."""
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", ST_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            site_id = await _seed_site(conn, "P-ST1")
            warehouse_id = await _seed_warehouse(conn, "D-1", site_id)
            item_id = await _seed_item(conn, "SNK-0421")
            entry_id = await _seed_entry(conn, warehouse_id)
            await _seed_line(conn, entry_id, item_id, "12.500")

            # 1) `code` UQ.
            with pytest.raises(asyncpg.UniqueViolationError):
                await _seed_item(conn, "SNK-0421")

            # 2) Depo adi santiye kapsaminda tekil.
            with pytest.raises(asyncpg.UniqueViolationError):
                await _seed_warehouse(conn, "D-1", site_id)
            # Baska santiyede ayni ad SERBEST.
            other_site_id = await _seed_site(conn, "P-ST2")
            await _seed_warehouse(conn, "D-1", other_site_id)

            # 3) Hareketi olan kart/depo SILINEMEZ (RESTRICT).
            #
            # ⚠️ BEKLENEN TIP POSTGRES SURUMUNE GORE DEGISIR (PR #26 CI kirigi,
            #    2026-08-11): `RESTRICT` ihlalinin SQLSTATE'i PG 18'de `23001`
            #    (-> `RestrictViolationError`), PG 16'da `23503`
            #    (-> `ForeignKeyViolationError`). Ikisi KARDES sinif (ortak ata
            #    `IntegrityConstraintViolationError`), biri otekini KAPSAMAZ.
            #    Yalniz `RestrictViolationError` yazilmisti: yerelde (PG 18.4)
            #    YESIL, CI'da (postgres:16-alpine) KIRMIZI — surum farki disinda
            #    hicbir sey degismeden. Tuple ikisini de kabul eder; ata sinifi
            #    yazmak UniqueViolation'i da yutacagi icin TERCIH EDILMEDI.
            _restrict = (asyncpg.RestrictViolationError, asyncpg.ForeignKeyViolationError)
            with pytest.raises(_restrict):
                await conn.execute("DELETE FROM stock_items WHERE id = $1", item_id)
            with pytest.raises(_restrict):
                await conn.execute("DELETE FROM warehouses WHERE id = $1", warehouse_id)

            # 4) Negatif miktar DB'de serbest (sayim farki/sarf — spec §7 S4).
            await _seed_line(conn, entry_id, item_id, "-3.000")
            assert await conn.fetchval(
                "SELECT sum(quantity) FROM stock_entry_lines WHERE entry_id = $1", entry_id
            ) == Decimal("9.500")

            # 5) Baslik silinince satirlari DUSER (CASCADE).
            await conn.execute("DELETE FROM stock_entries WHERE id = $1", entry_id)
            assert await conn.fetchval("SELECT count(*) FROM stock_entry_lines") == 0

            # 6) Santiye silinince depo KALIR, bagi kopar (SET NULL) — merkez
            #    depoya doner, yok olmaz.
            await conn.execute("DELETE FROM sites WHERE id = $1", site_id)
            row = await conn.fetchrow("SELECT * FROM warehouses WHERE id = $1", warehouse_id)
            assert row is not None, "santiye silinince depo da silindi (CASCADE kacagi)"
            assert row["site_id"] is None
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


# --- 🔴 MERKEZ DEPO ADININ DB KATMANI (kayıt 193) ---------------------------
#
# `uq_warehouses_site_name` Postgres'in varsayılan `NULLS DISTINCT` semantiği
# yüzünden `site_id IS NULL` dalında FİİLEN ÇALIŞMAZ: iki merkez depo AYNI adı
# taşıyabilir. Kısıt bunu `models.py` ve `guards.py` docstring'lerinde "BİLİNEN
# SINIR" diye YAZIYORDU ve tek savunma servis korkuluğuydu
# (`_assert_warehouse_name_free`). Korkuluk TEK KATMANDIR: iki eşzamanlı istek
# ikisi de "ad boş" okur ve ikisi de yazar — kardeş dal (şantiyeli depo) bu
# yarışta DB tarafından korunurken merkez dalı korunmuyordu.
#
# Onarım KISMİ TEKİL İNDEKStir (`WHERE site_id IS NULL`), deponun `customers`
# ve `subcontractor_progress_payments`ta zaten kullandığı desen.


async def test_merkez_depo_AYNI_ADI_IKI_KEZ_ALAMAZ(db_session) -> None:
    """DB katmanı — servis korkuluğu ATLANARAK doğrudan yazılır."""
    db_session.add(Warehouse(name="Merkez Depo (Sincan)", site_id=None))
    await db_session.flush()

    db_session.add(Warehouse(name="Merkez Depo (Sincan)", site_id=None))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_merkez_depo_FARKLI_ad_serbest(db_session) -> None:
    """Pozitif kontrol: indeks ADA bakar, merkez dalını TOPTAN kilitlemez."""
    db_session.add(Warehouse(name="Merkez Depo (Sincan)", site_id=None))
    db_session.add(Warehouse(name="Merkez Depo (Etimesgut)", site_id=None))
    await db_session.flush()


async def test_SANTIYELI_depo_ayni_adi_BASKA_santiyede_alabilir(db_session, gorunen_proje) -> None:
    """Pozitif kontrol: kısmi indeks YALNIZ merkez dalına uygulanır.

    Bu iddia olmasaydı indeks yanlışlıkla `WHERE` süzgeci olmadan açıldığında
    (yani ad GLOBAL tekil olduğunda) hiçbir test kırmızıya dönmezdi — iki
    şantiyenin "Saha Deposu"nu aynı anda açamaması sessiz bir gerileme olurdu.
    """
    from app.modules.sites.models import Site

    s1 = Site(project_id=gorunen_proje.id, code="DEP-S1", name="Şantiye 1")
    s2 = Site(project_id=gorunen_proje.id, code="DEP-S2", name="Şantiye 2")
    db_session.add_all([s1, s2])
    await db_session.flush()

    db_session.add(Warehouse(name="Saha Deposu", site_id=s1.id))
    db_session.add(Warehouse(name="Saha Deposu", site_id=s2.id))
    await db_session.flush()


MERKEZ_INDEKS_REVISION = "a1b2c3d4e5f7"
MERKEZ_INDEKS_ADI = "uq_warehouses_central_name"


def test_merkez_indeksi_MODELDE_kismi_ve_tekil() -> None:
    """Model katmanı — `create_all` ile kurulan test şeması bunu taşır."""
    indeks = {i.name: i for i in Warehouse.__table__.indexes}
    assert MERKEZ_INDEKS_ADI in indeks, f"kısmi indeks modelde yok: {list(indeks)}"
    hedef = indeks[MERKEZ_INDEKS_ADI]
    assert hedef.unique, "indeks TEKİL değil — çift merkez depo geçer"
    suzgec = str(hedef.dialect_options["postgresql"]["where"])
    assert "site_id IS NULL" in suzgec, (
        f"süzgeç merkez dalını hedeflemiyor: {suzgec!r} — süzgeçsiz bir tekil "
        "indeks adı GLOBAL tekil yapar ve iki şantiye aynı adı kullanamaz"
    )


async def test_merkez_indeksi_MIGRATION_ile_de_iner_ve_geri_alinir() -> None:
    """🔴 Model katmanı CANLIYI KURMAZ: test şeması `create_all`den doğar,
    canlı şema `alembic upgrade`den. İkisi ayrışırsa suite bunu HİÇ görmez
    (bu dosyanın `test_upgrade_downgrade_upgrade_round_trip`inin dersi)."""
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", MERKEZ_INDEKS_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            satir = await conn.fetchrow(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'warehouses' AND indexname = $1",
                MERKEZ_INDEKS_ADI,
            )
            assert satir is not None, "migration kısmi indeksi AÇMADI"
            tanim = satir["indexdef"]
            assert "UNIQUE" in tanim, tanim
            assert "site_id IS NULL" in tanim, tanim

            # DB düzeyinde ısırıyor mu — ORM'siz, doğrudan.
            await conn.execute(
                "INSERT INTO warehouses (id, name, site_id) VALUES (gen_random_uuid(), 'M', NULL)"
            )
            with pytest.raises(asyncpg.exceptions.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO warehouses (id, name, site_id) "
                    "VALUES (gen_random_uuid(), 'M', NULL)"
                )
        finally:
            await conn.close()

        _run_alembic("downgrade", "f3a7c9e1d5b2", database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            kalan = await conn.fetchval(
                "SELECT count(*) FROM pg_indexes WHERE tablename = 'warehouses' AND indexname = $1",
                MERKEZ_INDEKS_ADI,
            )
            assert kalan == 0, "downgrade indeksi BIRAKMIŞ"
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

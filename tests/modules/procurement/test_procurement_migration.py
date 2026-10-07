"""SA T1 — satinalma cekirdegi: 5 tablo + 4 enum + ST'ye 1 additive kolon (spec §2).

Neden ayri bir tur donusu testi (ST emsali): bu migration BES YENI TABLO, DORT
YENI ENUM ve `stock_entries`e bir FK KOLONU getiriyor. Enum'lari dusurmeyi
unutan bir `downgrade` ikinci upgrade'i "type already exists" ile patlatirdi
(d4e5f6a7b8c9 dersi) ve bu yalniz CANLIDA gorulurdu.

FK silme davranislari burada SEMANTIKTIR:
  * talep silinince kalemleri/teklifleri DUSER (CASCADE) — yetim kalem/teklif
    karsilastirma ekranini kirletirdi
  * SIPARISI OLAN talep SILINEMEZ (RESTRICT) — siparis kaydinin talebi yok olmaz
  * tedarikcisi olan kayitlar tedarikciyi kilitler (RESTRICT) — `is_active=false`
    kullanilir, DELETE ucu zaten yoktur (spec §4)
  * santiye/bolum silinince talep KALIR, bagi kopar (SET NULL) — santiye talebin
    DARALTMASIDIR, sahibi projedir
  * siparis silinse bile stok girisi KALIR (SET NULL) — hareket gecmisi bir
    satinalma kaydina bagli olarak yok olamaz

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

from app.core.config import settings
from app.modules.inventory.models import StockEntry
from app.modules.procurement.models import (
    PaymentTerms,
    PurchaseOrder,
    PurchaseOrderStatus,
    PurchasePriority,
    PurchaseQuote,
    PurchaseRequest,
    PurchaseRequestLine,
    PurchaseRequestStatus,
    Supplier,
)

BACKEND_DIR = Path(__file__).parents[3]
# `python -m alembic`: yerelde `.venv/bin/python`, CI'da sistem Python'u. Sabit
# `.venv/bin/alembic` yolu CI'da YOKTUR ve testi yalniz orada kirardi.
ALEMBIC_CMD = (sys.executable, "-m", "alembic")

# Revizyonlara ACIKCA cikilir; `head` / `-1` KULLANILMAZ — sonraki dilimler
# revizyon ekledikce bu test sessizce yanlis seyi olcerdi.
PARENT_REVISION = "e1f2a3b4c5d6"  # ST stok cekirdegi
SA_REVISION = "f3a4b5c6d7e8"

TABLES = (
    "suppliers",
    "purchase_requests",
    "purchase_request_lines",
    "purchase_quotes",
    "purchase_orders",
)
ENUMS = ("payment_terms", "purchase_priority", "purchase_request_status", "purchase_order_status")
INDEXES = (
    "ix_purchase_requests_project_id",
    "ix_purchase_requests_site_id",
    "ix_purchase_requests_section_id",
    "ix_purchase_requests_status",
    "ix_purchase_requests_request_date",
    "ix_purchase_request_lines_request_id",
    "ix_purchase_request_lines_stock_item_id",
    "ix_purchase_quotes_request_id",
    "ix_purchase_quotes_supplier_id",
    "ix_purchase_orders_request_id",
    "ix_purchase_orders_supplier_id",
    "ix_purchase_orders_project_id",
    "ix_purchase_orders_status",
    "ix_stock_entries_purchase_order_id",
)

# Kalici kararlar (spec §5). Bu adlar tesadufi degil — ileride "kucuk bir ekleme"
# diye geri sizmalarina karsi korkuluk.
FORBIDDEN_SUPPLIER_COLUMNS = (
    # Degerlendirme GIRISI yok: puan uydurulamaz (spec §5, pending).
    "rating",
    "score",
    "performance",
    # Mockup'ta YOK — acilmaz (spec §5).
    "address",
    "email",
    "iban",
    "contact_person",
    # "Bu Yil Toplam Siparis" TUREVDIR.
    "total_orders",
    "order_count",
)
FORBIDDEN_REQUEST_COLUMNS = (
    # Cok adimli onay MOTORU ACILMAZ (§7 S2) — zincir gorseli frontend turevi.
    "approval_step",
    "approval_chain_id",
    "current_approver_id",
    # Toplam tutar TUREVDIR (kalemlerden).
    "total_amount",
    "estimated_total",
)
FORBIDDEN_LINE_COLUMNS = (
    # Satir tutari ve "Mevcut Stok" TUREVDIR (spec §2).
    "line_total",
    "amount",
    "current_stock",
    "stock_balance",
)
FORBIDDEN_QUOTE_COLUMNS = (
    # "EN IYI FIYAT"/"EN HIZLI" rozetleri TUREVDIR (spec §2).
    "is_best_price",
    "is_fastest",
    "rank",
    # `delivery_time` SERBEST METINDIR — gun sayisina ZORLANMAZ.
    "delivery_days",
)
FORBIDDEN_ORDER_COLUMNS = (
    # Kismi teslim ayrimi YOK — bilinen sinir (§7 S4).
    "delivered_quantity",
    "received_at",
    "partial_delivery",
    "goods_receipt_id",
)


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


async def _column_exists(conn: asyncpg.Connection, table: str, column: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name = $1 AND column_name = $2)",
        table,
        column,
    )


async def _enum_exists(conn: asyncpg.Connection, name: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_type WHERE typname = $1 AND typtype = 'e')", name
    )


async def _enum_labels(conn: asyncpg.Connection, name: str) -> list[str]:
    return await conn.fetchval("SELECT enum_range(NULL::" + name + ")::text[]")


async def _index_exists(conn: asyncpg.Connection, name: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = $1)", name
    )


async def _current_revision(conn: asyncpg.Connection) -> str | None:
    return await conn.fetchval("SELECT version_num FROM alembic_version")


async def _create_scratch_database() -> str:
    database = f"procurement_sa_{uuid.uuid4().hex[:8]}"
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


# --------------------------------------------------------------------------- #
# Ham SQL seed yardimcilari (tur donusu testleri ORM kullanmaz)
# --------------------------------------------------------------------------- #


async def _seed_project(conn: asyncpg.Connection, code: str) -> uuid.UUID:
    project_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO projects (id, code, name, status, budget, progress_pct) "
        "VALUES ($1, $2, 'SA Tur Donusu', 'active', 0, 0)",
        project_id,
        code,
    )
    return project_id


async def _seed_site(conn: asyncpg.Connection, project_id: uuid.UUID, code: str) -> uuid.UUID:
    site_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO sites (id, project_id, code, name) VALUES ($1, $2, $3, 'SA Santiye')",
        site_id,
        project_id,
        code,
    )
    return site_id


async def _seed_user(conn: asyncpg.Connection, email: str) -> uuid.UUID:
    role_id, user_id = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO roles (id, key, name, emoji, description, is_system) "
        "VALUES ($1, $2, 'SA Rol', '', '', false)",
        role_id,
        f"sa_rol_{uuid.uuid4().hex[:8]}",
    )
    await conn.execute(
        "INSERT INTO users (id, email, password_hash, full_name, title, role_id, status, "
        "token_version) VALUES ($1, $2, 'x', 'SA Kullanici', '', $3, 'active', 0)",
        user_id,
        email,
        role_id,
    )
    return user_id


async def _seed_supplier(conn: asyncpg.Connection, name: str) -> uuid.UUID:
    supplier_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO suppliers (id, name, payment_terms, is_active) "
        "VALUES ($1, $2, 'days_30', true)",
        supplier_id,
        name,
    )
    return supplier_id


async def _seed_request(
    conn: asyncpg.Connection,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    request_no: str,
    site_id: uuid.UUID | None = None,
) -> uuid.UUID:
    request_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO purchase_requests (id, request_no, request_date, priority, project_id, "
        "site_id, status, created_by_user_id) "
        "VALUES ($1, $2, $3, 'normal', $4, $5, 'draft', $6)",
        request_id,
        request_no,
        date(2026, 8, 12),
        project_id,
        site_id,
        user_id,
    )
    return request_id


# --------------------------------------------------------------------------- #
# Model katmani
# --------------------------------------------------------------------------- #


def test_enum_values_match_spec():
    """Enum degerleri spec §2/§3 BIREBIR — mockup'un kapali kumeleri."""
    assert [e.value for e in PaymentTerms] == ["cash", "days_15", "days_30", "days_60"]
    assert [e.value for e in PurchasePriority] == ["normal", "urgent", "critical"]
    # §7 S1 ALTILI kume — "Revize" TUREVI YOK (mockup'ta yok).
    assert [e.value for e in PurchaseRequestStatus] == [
        "draft",
        "pending_approval",
        "quote_wait",
        "ordered",
        "delivered",
        "rejected",
    ]
    # SIP 34 filtresi birebir.
    assert [e.value for e in PurchaseOrderStatus] == ["approved", "in_transit", "delivered"]


def test_supplier_columns_match_spec():
    columns = Supplier.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "name",
        "category",
        "tax_no",
        "phone",
        "payment_terms",
        "is_active",
        "created_at",
        "updated_at",
    }
    assert columns["name"].type.length == 200
    # `category` SERBEST METINDIR (TED alt-etiketi) — enum ICAT EDILMEZ.
    assert columns["category"].type.length == 100
    assert columns["category"].nullable
    # 🔴 SUP-TCKN (2026-08-25): 10 -> 11. Alan TEK basina hem VKN'yi (10) hem
    # SAHIS SIRKETININ TCKN'sini (11) tasir; eski sinir sahis tedarikcisini
    # 422 ile reddediyordu. DB tarafi `test_suptckn_migration.py`de.
    assert columns["tax_no"].type.length == 11
    assert columns["tax_no"].nullable
    assert columns["phone"].type.length == 30
    assert columns["phone"].nullable
    assert not columns["payment_terms"].nullable
    assert not columns["is_active"].nullable


def test_purchase_request_columns_and_delete_semantics():
    columns = PurchaseRequest.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "request_no",
        "request_date",
        "priority",
        "project_id",
        "site_id",
        "section_id",
        "needed_by",
        "justification",
        "status",
        "quote_deadline",
        "approved_by_user_id",
        "approved_at",
        "rejected_at",
        "rejection_reason",
        "created_by_user_id",
        "created_at",
        "updated_at",
    }
    assert columns["request_no"].unique, "ayni numara iki talebe verilemez"
    (project_fk,) = tuple(columns["project_id"].foreign_keys)
    assert project_fk.ondelete == "CASCADE", "repo deseni: proje silinince kayitlari duser"
    # Santiye/bolum talebin DARALTMASIDIR (FST 57, istege bagli): silinince talep
    # KALIR, yalnizca bagi kopar. CASCADE burada satinalma tarihini silerdi.
    assert columns["site_id"].nullable
    (site_fk,) = tuple(columns["site_id"].foreign_keys)
    assert site_fk.ondelete == "SET NULL"
    assert columns["section_id"].nullable
    (section_fk,) = tuple(columns["section_id"].foreign_keys)
    assert section_fk.ondelete == "SET NULL"
    assert columns["needed_by"].nullable
    assert columns["quote_deadline"].nullable
    assert columns["approved_by_user_id"].nullable
    assert columns["approved_at"].nullable
    assert columns["rejected_at"].nullable
    assert columns["rejection_reason"].nullable
    assert not columns["created_by_user_id"].nullable
    (creator_fk,) = tuple(columns["created_by_user_id"].foreign_keys)
    assert creator_fk.ondelete == "RESTRICT", "talebi acan kullanici sessizce silinemez"


def test_purchase_request_line_columns_and_delete_semantics():
    columns = PurchaseRequestLine.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "request_id",
        "stock_item_id",
        "free_text_name",
        "free_text_unit",
        "quantity",
        "estimated_unit_price",
        # T3: FST kalem tablosu SIRALIDIR — kullanicinin girdigi sira korunur.
        "sort_order",
    }
    (request_fk,) = tuple(columns["request_id"].foreign_keys)
    assert request_fk.ondelete == "CASCADE", "yetim kalem"
    # Katalogsuz kalem (FST "yeni malzeme tanimla"): stok karti ZORUNLU DEGIL.
    assert columns["stock_item_id"].nullable
    (item_fk,) = tuple(columns["stock_item_id"].foreign_keys)
    assert item_fk.ondelete == "RESTRICT", "talepte gecen kart silinemez"
    assert columns["free_text_name"].nullable
    assert columns["free_text_unit"].nullable
    assert not columns["quantity"].nullable
    assert columns["estimated_unit_price"].nullable
    # SUNUCU VARSAYILANI YOKTUR: her yazma yolu sirayi acikca doldurmak
    # zorundadir (varsayilan 0 olsaydi eksik doldurulan yol sessizce keyfi dizerdi).
    assert not columns["sort_order"].nullable
    assert columns["sort_order"].server_default is None


def test_purchase_quote_columns_and_delete_semantics():
    columns = PurchaseQuote.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "request_id",
        "supplier_id",
        "unit_price",
        "delivery_time",
        "warranty_note",
        "payment_terms",
        "shipping_included",
        "shipping_cost",
        "is_selected",
        "created_at",
        "updated_at",
    }
    (request_fk,) = tuple(columns["request_id"].foreign_keys)
    assert request_fk.ondelete == "CASCADE"
    (supplier_fk,) = tuple(columns["supplier_id"].foreign_keys)
    assert supplier_fk.ondelete == "RESTRICT", "teklifi olan tedarikci silinemez"
    # TEK 67: "3 is gunu" / "Yarin sabah" — SERBEST metin, gun sayisina ZORLANMAZ.
    assert columns["delivery_time"].type.length == 100
    assert columns["warranty_note"].type.length == 200
    assert columns["warranty_note"].nullable
    assert not columns["shipping_included"].nullable
    assert columns["shipping_cost"].nullable, "dahilse tutar YOKTUR (TEK 90)"
    assert not columns["is_selected"].nullable


def test_purchase_order_columns_and_delete_semantics():
    columns = PurchaseOrder.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "order_no",
        "request_id",
        "quote_id",
        "supplier_id",
        "project_id",
        "total_amount",
        "expected_delivery",
        "status",
        "note",
        "created_by_user_id",
        "created_at",
        "updated_at",
    }
    assert columns["order_no"].unique
    # §7 S3: TALEPSIZ siparis MESRU (SIP 35 "+ Siparis Olustur"; SP-035'in talebi yok).
    assert columns["request_id"].nullable
    (request_fk,) = tuple(columns["request_id"].foreign_keys)
    assert request_fk.ondelete == "RESTRICT", "siparisi olan talep silinemez"
    assert columns["quote_id"].nullable
    (quote_fk,) = tuple(columns["quote_id"].foreign_keys)
    assert quote_fk.ondelete == "SET NULL"
    (supplier_fk,) = tuple(columns["supplier_id"].foreign_keys)
    assert supplier_fk.ondelete == "RESTRICT"
    assert not columns["total_amount"].nullable
    assert columns["expected_delivery"].nullable
    assert not columns["created_by_user_id"].nullable


def test_stock_entry_gained_purchase_order_link():
    """ST bagi (spec §2 additive): SG 85 "Ilgili Siparis" gercege doner.

    `supplier_name` SERBEST METNI DEGISMEZ (kayitli karar) — FK'ye cevrilmedi.
    SET NULL: siparis kaydi bir gun dusurulse bile stok hareketi KALIR.
    """
    columns = StockEntry.__table__.columns
    assert "purchase_order_id" in columns
    assert columns["purchase_order_id"].nullable
    (order_fk,) = tuple(columns["purchase_order_id"].foreign_keys)
    assert order_fk.column.table.name == "purchase_orders"
    assert order_fk.ondelete == "SET NULL"
    assert "supplier_name" in columns, "ST'nin serbest metni kaldirilmis"
    assert not columns["supplier_name"].foreign_keys, (
        "supplier_name FK'ye cevrilmis (kayitli karar)"
    )


def test_forbidden_columns_are_absent():
    """Kalici kararlar (spec §5): onay MOTORU · tedarikci PUANI · adres/IBAN ·
    turev tutarlar · rozetler · kismi teslim alanlari ACILMAZ."""
    for name in FORBIDDEN_SUPPLIER_COLUMNS:
        assert name not in Supplier.__table__.columns, f"suppliers.{name} acilmamaliydi"
    for name in FORBIDDEN_REQUEST_COLUMNS:
        assert name not in PurchaseRequest.__table__.columns, (
            f"purchase_requests.{name} acilmamaliydi"
        )
    for name in FORBIDDEN_LINE_COLUMNS:
        assert name not in PurchaseRequestLine.__table__.columns, (
            f"purchase_request_lines.{name} acilmamaliydi — TUREVDIR"
        )
    for name in FORBIDDEN_QUOTE_COLUMNS:
        assert name not in PurchaseQuote.__table__.columns, f"purchase_quotes.{name} acilmamaliydi"
    for name in FORBIDDEN_ORDER_COLUMNS:
        assert name not in PurchaseOrder.__table__.columns, f"purchase_orders.{name} acilmamaliydi"


def test_satinalmanin_KENDI_onay_tablosu_ACILMAZ():
    """§7 S2 tripwire'i — 🔴 **UYARLANDI (OK-1A T2), cunku GOREVINI YAPTI.**

    Eski hâli `approval_chains` / `approval_steps` tablolarinin HIC olmadigini
    iddia ediyordu ("SA T1'de cok adimli onay motoru ACILMAZ"). OK-1A tam da o
    motoru aciyor (kullanici karari K1/K4) ve bu test kirilarak KAPSAM
    GENISLEMESINI GORUNUR kildi — tripwire'in var olma sebebi budur.

    Uyarlama iddiayi ZAYIFLATMAZ, YERINI DEGISTIRIR: yasak olan sey artik
    "onay motoru" degil, **SATINALMANIN KENDI onay tablosunu acmasi**dir.
    Motor ORTAKTIR ve `app/modules/approvals/` altinda yasar; satinalma ona
    `document_type='purchase_request'` ile baglanir (T3). `purchase_approvals`
    gibi aileye ozel ikinci bir tablo acilsaydi ayni kavram iki yerde yasar ve
    onay kutusu iki kaynaktan okumak zorunda kalirdi.

    SA'nin oteki kalici kararlari (`supplier_ratings`, `goods_receipts`)
    DEGISMEDI ve aynen bekcilenmeye devam eder.
    """
    from app.core.db import Base

    for table in ("purchase_approvals", "supplier_ratings", "goods_receipts"):
        assert table not in Base.metadata.tables, f"{table} acilmamaliydi (SA §5)"

    # Ortak motor VARDIR ve `approvals` modulunundur — satinalmanin degil.
    from app.modules.approvals.models import ApprovalChain, ApprovalStep

    assert ApprovalChain.__table__.name == "approval_chains"
    assert ApprovalStep.__table__.name == "approval_steps"
    assert ApprovalChain.__module__.startswith("app.modules.approvals"), (
        "onay zinciri satinalma modulune tasinmis — motor ORTAK kalmali"
    )


def test_permission_module_already_seeded():
    """Spec §2: `procurement` ("Satinalma & Teklif", STOK_SATINALMA) izin modulu
    seed'de ZATEN VARDIR — yeni modul ACILMAZ, izin migration'i YOKTUR. Anahtar
    bir gun degisirse bu test SA uclarinin izin varsayimini dusurur."""
    from app.core.sayfalar import MODUL_ANAHTARLARI
    from app.modules.roles.seed_data import MATRIX

    keys = set(MODUL_ANAHTARLARI)
    assert "procurement" in keys, "SA uclarinin dayandigi izin modulu seed'den kalkmis"
    assert "procurement" in MATRIX
    assert "purchasing" not in keys, (
        "ikinci bir satinalma modulu acilmis — tek anahtar `procurement`"
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
    assert len(heads) == 1, f"tek head bekleniyordu, cikti:\n{result.stdout}"


async def test_upgrade_downgrade_upgrade_round_trip():
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", SA_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for table in TABLES:
                assert await _table_exists(conn, table), table
            for enum_name in ENUMS:
                assert await _enum_exists(conn, enum_name), enum_name
            for index in INDEXES:
                assert await _index_exists(conn, index), index
            assert await _column_exists(conn, "stock_entries", "purchase_order_id")
            assert await _enum_labels(conn, "payment_terms") == [
                "cash",
                "days_15",
                "days_30",
                "days_60",
            ]
            assert await _enum_labels(conn, "purchase_priority") == ["normal", "urgent", "critical"]
            assert await _enum_labels(conn, "purchase_request_status") == [
                "draft",
                "pending_approval",
                "quote_wait",
                "ordered",
                "delivered",
                "rejected",
            ]
            assert await _enum_labels(conn, "purchase_order_status") == [
                "approved",
                "in_transit",
                "delivered",
            ]
            assert await _current_revision(conn) == SA_REVISION
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
            # Additive kolon da geri alinmali; kalirsa `purchase_orders` FK'si
            # olmayan bir sutun olarak sarkar ve ikinci upgrade patlar.
            assert not await _column_exists(conn, "stock_entries", "purchase_order_id")
            assert await _table_exists(conn, "stock_entries"), "ST tablosu downgrade'de dusurulmus"
            assert await _current_revision(conn) == PARENT_REVISION
        finally:
            await conn.close()

        _run_alembic("upgrade", SA_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for table in TABLES:
                assert await _table_exists(conn, table), table
            assert await _column_exists(conn, "stock_entries", "purchase_order_id")
            assert await _current_revision(conn) == SA_REVISION
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_db_level_semantics():
    """DB seviyesinde: numara UQ · CASCADE · RESTRICT · SET NULL · miktar CHECK'i."""
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", SA_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            project_id = await _seed_project(conn, "P-SA1")
            site_id = await _seed_site(conn, project_id, "S-SA1")
            user_id = await _seed_user(conn, "sa@ornek.test")
            supplier_id = await _seed_supplier(conn, "Beton A.S.")
            request_id = await _seed_request(conn, project_id, user_id, "SAT-2026-0001", site_id)

            # 1) `request_no` GLOBAL tekil — ayni numara iki talebe verilemez.
            with pytest.raises(asyncpg.UniqueViolationError):
                await _seed_request(conn, project_id, user_id, "SAT-2026-0001")

            # 2) Kalem: katalogsuz (free_text) kalem MESRU — `stock_item_id` NULL.
            line_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO purchase_request_lines (id, request_id, free_text_name, "
                "free_text_unit, quantity, sort_order) "
                "VALUES ($1, $2, 'Ozel Kalip', 'Adet', $3, 0)",
                line_id,
                request_id,
                Decimal("10.000"),
            )

            # 2b) `sort_order` NOT NULL ve SUNUCU VARSAYILANI YOKTUR (T3): kolonu
            #     atlayan bir yazma REDDEDILIR. Varsayilan 0 olsaydi eksik
            #     doldurulan bir yol tum satirlari ayni sirada birakip FST kalem
            #     tablosunu sessizce keyfi dizerdi.
            with pytest.raises(asyncpg.NotNullViolationError):
                await conn.execute(
                    "INSERT INTO purchase_request_lines (id, request_id, free_text_name, "
                    "free_text_unit, quantity) VALUES ($1, $2, 'Sirasiz', 'Adet', 1)",
                    uuid.uuid4(),
                    request_id,
                )

            # 3) Miktar POZITIF olmak ZORUNDA (spec §2) — ST'nin negatif duzeltme
            #    istisnasi burada YOKTUR: sifir/negatif talep kalemi anlamsizdir.
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO purchase_request_lines (id, request_id, free_text_name, "
                    "quantity, sort_order) VALUES ($1, $2, 'Sifir', 0, 0)",
                    uuid.uuid4(),
                    request_id,
                )

            # 4) Teklif eklenir; teklifi olan tedarikci SILINEMEZ (RESTRICT).
            quote_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO purchase_quotes (id, request_id, supplier_id, unit_price, "
                "delivery_time, payment_terms, shipping_included, is_selected) "
                "VALUES ($1, $2, $3, $4, '3 is gunu', 'days_30', false, true)",
                quote_id,
                request_id,
                supplier_id,
                Decimal("1250.00"),
            )
            _restrict = (asyncpg.RestrictViolationError, asyncpg.ForeignKeyViolationError)
            with pytest.raises(_restrict):
                await conn.execute("DELETE FROM suppliers WHERE id = $1", supplier_id)

            # 5) Siparis: talepli. Siparisi olan talep SILINEMEZ (RESTRICT).
            order_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO purchase_orders (id, order_no, request_id, quote_id, supplier_id, "
                "project_id, total_amount, status, created_by_user_id) "
                "VALUES ($1, 'SP-2026-0001', $2, $3, $4, $5, $6, 'approved', $7)",
                order_id,
                request_id,
                quote_id,
                supplier_id,
                project_id,
                Decimal("12500.00"),
                user_id,
            )
            with pytest.raises(_restrict):
                await conn.execute("DELETE FROM purchase_requests WHERE id = $1", request_id)

            # 6) TALEPSIZ siparis MESRU (§7 S3).
            await conn.execute(
                "INSERT INTO purchase_orders (id, order_no, supplier_id, project_id, "
                "total_amount, status, created_by_user_id) "
                "VALUES ($1, 'SP-2026-0002', $2, $3, $4, 'approved', $5)",
                uuid.uuid4(),
                supplier_id,
                project_id,
                Decimal("500.00"),
                user_id,
            )

            # 7) Stok girisi siparise baglanir; siparis dusurulse bile giris KALIR.
            warehouse_id, entry_id = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO warehouses (id, name, site_id) VALUES ($1, 'D-1', $2)",
                warehouse_id,
                site_id,
            )
            await conn.execute(
                "INSERT INTO stock_entries (id, entry_type, entry_date, warehouse_id, "
                "purchase_order_id) VALUES ($1, 'purchase', $2, $3, $4)",
                entry_id,
                date(2026, 8, 12),
                warehouse_id,
                order_id,
            )
            # Siparisi dusurmek icin once talep bagini kopar (RESTRICT degil, sira).
            await conn.execute("DELETE FROM purchase_orders WHERE id = $1", order_id)
            row = await conn.fetchrow("SELECT * FROM stock_entries WHERE id = $1", entry_id)
            assert row is not None, "siparis silinince stok girisi de silindi (CASCADE kacagi)"
            assert row["purchase_order_id"] is None

            # 8) Talep silinince kalemleri ve teklifleri DUSER (CASCADE).
            await conn.execute("DELETE FROM purchase_requests WHERE id = $1", request_id)
            assert await conn.fetchval("SELECT count(*) FROM purchase_request_lines") == 0
            assert await conn.fetchval("SELECT count(*) FROM purchase_quotes") == 0

            # 9) Santiye silinince talep KALIR, bagi kopar (SET NULL).
            kalan_id = await _seed_request(conn, project_id, user_id, "SAT-2026-0002", site_id)
            # Once hareket, sonra depo: hareketi olan depo RESTRICT'lidir (ST).
            await conn.execute("DELETE FROM stock_entries WHERE id = $1", entry_id)
            await conn.execute("DELETE FROM warehouses WHERE id = $1", warehouse_id)
            await conn.execute("DELETE FROM sites WHERE id = $1", site_id)
            row = await conn.fetchrow("SELECT * FROM purchase_requests WHERE id = $1", kalan_id)
            assert row is not None, "santiye silinince talep de silindi"
            assert row["site_id"] is None
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

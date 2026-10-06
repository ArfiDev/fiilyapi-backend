"""İK-3 T1 — bordro şeması: model katmanı + migration tur dönüşü + 2026 oran SEED'i.

Spec: `docs/superpowers/specs/2026-08-13-ik3-bordro-design.md` §4.

NEDEN AYRI BİR TUR DÖNÜŞÜ TESTİ (SA/ST emsali): bu migration ÜÇ YENİ TABLO, İKİ
YENİ ENUM ve **paylaşılan `worker_source` tipinin TAKASINI** getiriyor. Enum'ları
düşürmeyi unutan bir `downgrade` ikinci upgrade'i "type already exists" ile
patlatırdı (d4e5f6a7b8c9 dersi) ve bu yalnız CANLIDA görülürdü.

`worker_source` TAKASI (f1b2c3d4e5a6 deseni): İK-1 `models.py:31` "Serbest
Meslek/Stajyer PE 90 seçenekleri bu enum'a EKLENMEZ — **takas** SGK 4a/4b ayrımı
netleşince **İK-3'te yapılır**" diyerek bu işi bu dilime ertelemişti. BY 243
(Serbest Meslek) ve BY 271 (Stajyer) bölümleri oran tablosunun DÖRT tip
gerektirdiğini gösteriyor; mevcut tip yalnız üç değer taşıyor. `ALTER TYPE ...
ADD VALUE` KULLANILAMAZ: eklenen değer AYNI işlemde kullanılamaz (d4e5f6a7b8c9
notu) ve seed onu aynı işlemde kullanır — bu yüzden tip TAKAS edilir (yeni tip
aynı işlemde yaratıldığı için değerleri hemen kullanılabilir) ve takas ayrıca
GERİ ALINABİLİRDİR.

Test kendi TEK KULLANIMLIK veritabanını açar ve sonunda düşürür; `.env` ve
`TEST_DATABASE_URL` veritabanı ELLENMEZ. Alembic alt süreçte koşturulur çünkü
`alembic/env.py` kendi `asyncio.run()` döngüsünü kurar (SA/P11 deseni).
"""

import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest

from app.core.config import settings
from app.modules.payroll.models import (
    PayrollLine,
    PayrollLineStatus,
    PayrollPeriod,
    PayrollPeriodStatus,
    PayrollRate,
)
from app.modules.site_diary.models import WorkerSource

BACKEND_DIR = Path(__file__).parents[3]
# `python -m alembic`: yerelde `.venv/bin/python`, CI'da sistem Python'u.
ALEMBIC_CMD = (sys.executable, "-m", "alembic")

# Revizyonlara AÇIKÇA çıkılır; `head` / `-1` KULLANILMAZ — sonraki dilimler
# revizyon ekledikçe bu test sessizce yanlış şeyi ölçerdi.
PARENT_REVISION = "b2c3d4e5f6a7"  # İK-2 izin yönetimi
IK3_REVISION = "c5d6e7f8a9b0"

TABLES = ("payroll_periods", "payroll_lines", "payroll_rates")
NEW_ENUMS = ("payroll_period_status", "payroll_line_status")
INDEXES = (
    "ix_payroll_periods_status",
    "ix_payroll_lines_payroll_period_id",
    "ix_payroll_lines_personnel_id",
    "ix_payroll_lines_status",
    "ix_payroll_rates_year",
)

# S1: BY tablosundaki tutarlar temsilîdir, SGK 70-81'de AÇIKÇA yazılı oranlar
# SEED olur. Yedi oranın mockup kanıtı:
#   SGK İşçi %14            → SGK 70
#   İşsizlik İşçi %1        → SGK 71
#   Gelir Vergisi %10       → SGK 72
#   Damga %0,759            → SGK 73
#   SGK İşveren %20,5       → SGK 79
#   İşsizlik İşveren %2     → SGK 80
#   Kısa Çalışma %1         → SGK 81
SGK_4A_RATES = {
    "sgk_employee_pct": Decimal("14.000"),
    "unemployment_employee_pct": Decimal("1.000"),
    "income_tax_pct": Decimal("10.000"),
    "stamp_tax_pct": Decimal("0.759"),
    "sgk_employer_pct": Decimal("20.500"),
    "unemployment_employer_pct": Decimal("2.000"),
    "short_work_pct": Decimal("1.000"),
}
ZERO_RATES = dict.fromkeys(SGK_4A_RATES, Decimal("0.000"))
# BY 243 "SERBEST MESLEK — Serbest Makbuz · %20 Stopaj"; BY 254-255 veriyle de
# doğruluyor (12.500 brüt → 2.500 kesinti = tam %20). SGK payı YOK.
SERBEST_RATES = {**ZERO_RATES, "income_tax_pct": Decimal("20.000")}

EXPECTED_SEED = {
    # BY 127 "ŞİRKET KADROSU — SGK 4a".
    "company": SGK_4A_RATES,
    # BY 175 "TAŞERON İŞÇİSİ — SGK Taşeron". Kesinti sütunu "—" DEĞİLDİR
    # (BY 186: 26.400 brüt → 7.064 kesinti), yani taşeron işçisi de kesintiye
    # tabidir → 4a oranlarının AYNISI seed edilir. (Ödeme onayına girmemesi K2
    # kararıdır ve SERVİS katmanının işidir; oran tablosuyla ilgisi yoktur.)
    "subcontractor": SGK_4A_RATES,
    "freelance": SERBEST_RATES,
    # BY 285: stajyer satırında kesinti sütunu "—" → TÜM oranlar 0.
    "intern": ZERO_RATES,
}


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
    return await conn.fetchval("SELECT enum_range(NULL::" + name + ")::text[]")


async def _index_exists(conn: asyncpg.Connection, name: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = $1)", name
    )


async def _current_revision(conn: asyncpg.Connection) -> str | None:
    return await conn.fetchval("SELECT version_num FROM alembic_version")


async def _create_scratch_database() -> str:
    database = f"payroll_ik3_{uuid.uuid4().hex[:8]}"
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
# Model katmani
# --------------------------------------------------------------------------- #


def test_worker_source_gained_freelance_and_intern():
    """S2: oran seti DÖRT tip üzerindedir; paylaşılan tip iki değer kazanır.

    Yeni bir `personnel_source` tipi AÇILMAZ — aynı anlam kümesinin iki DB tipi
    doğardı (puantaj spec §2, personnel/models.py:27-31).
    """
    assert [e.value for e in WorkerSource] == [
        "company",
        "subcontractor",
        "general",
        "freelance",
        "intern",
    ]


def test_period_and_line_status_enums_match_spec():
    """S8 geçiş zinciri birebir (spec §4)."""
    assert [e.value for e in PayrollPeriodStatus] == [
        "draft",
        "pending_approval",
        "approved",
        "paid",
    ]
    assert [e.value for e in PayrollLineStatus] == [
        "uncomputed",
        "pending",
        "approved",
        "paid",
        "excluded",
    ]


def test_payroll_period_columns_match_spec():
    columns = PayrollPeriod.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "year",
        "month",
        "status",
        "payment_due_date",
        "approved_by_id",
        "approved_at",
        "paid_at",
        "sgk_submitted_at",
        "created_at",
        "updated_at",
    }
    # UQ (year, month): bir ay için TEK bordro (spec §4, uç §5 → 409).
    assert any(
        set(c.name for c in uq.columns) == {"year", "month"}
        for uq in PayrollPeriod.__table__.constraints
        if hasattr(uq, "columns") and uq.__class__.__name__ == "UniqueConstraint"
    ), "uq_payroll_periods_year_month yok — aynı ay iki kez açılabilirdi"
    assert columns["payment_due_date"].nullable
    assert columns["approved_by_id"].nullable
    (approver_fk,) = tuple(columns["approved_by_id"].foreign_keys)
    # SET NULL: onaylayan kullanıcı silinse de dönem ve onay zamanı AYAKTA kalır
    # (İK-2 `decided_by` emsali).
    assert approver_fk.ondelete == "SET NULL"
    assert columns["sgk_submitted_at"].nullable


def test_payroll_line_columns_and_delete_semantics():
    columns = PayrollLine.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "payroll_period_id",
        "personnel_id",
        "personnel_source",
        "days",
        "gross_amount",
        "deduction_amount",
        "net_amount",
        "bank_amount",
        "cash_amount",
        # IK3-GV K1 — vergi SNAPSHOT'ı (üçü de nullable, sunucu varsayılansız).
        "tax_base_amount",
        "cumulative_tax_base",
        "income_tax_amount",
        "is_overridden",
        "overridden_by_id",
        "overridden_at",
        "previous_gross_amount",
        "status",
        "excluded_reason",
        "created_at",
        "updated_at",
    }
    (period_fk,) = tuple(columns["payroll_period_id"].foreign_keys)
    assert period_fk.ondelete == "CASCADE", "yetim bordro satırı"
    (personnel_fk,) = tuple(columns["personnel_id"].foreign_keys)
    assert personnel_fk.ondelete == "RESTRICT", (
        "bordro satırı olan personel silinemez — PARA izi (spec §4)"
    )
    # S4 fail-closed: ücretsiz personelde 0 BASILMAZ, `null` durur.
    for para in ("gross_amount", "deduction_amount", "net_amount", "bank_amount", "cash_amount"):
        assert columns[para].nullable, f"{para} NOT NULL olursa S4 uydurma 0'a zorlanır"
        assert columns[para].type.precision == 12
        assert columns[para].type.scale == 2, "S3 kuruş hassasiyeti"
        assert columns[para].server_default is None, "sunucu varsayılanı = uydurma 0"
    # S7: serbest meslekte gün YOKTUR (BY 254 "—").
    assert columns["days"].nullable
    # K3 izi.
    assert columns["previous_gross_amount"].nullable
    assert columns["overridden_by_id"].nullable
    (overrider_fk,) = tuple(columns["overridden_by_id"].foreign_keys)
    assert overrider_fk.ondelete == "SET NULL"
    # Tip SNAPSHOT'ıdır: personelin tipi sonradan değişse geçmiş bordro değişmez.
    assert not columns["personnel_source"].nullable


def test_payroll_line_unique_period_personnel():
    """S6: dönem+personel için TEK satır."""
    assert any(
        set(c.name for c in uq.columns) == {"payroll_period_id", "personnel_id"}
        for uq in PayrollLine.__table__.constraints
        if uq.__class__.__name__ == "UniqueConstraint"
    )


def test_payroll_rate_columns_match_spec():
    columns = PayrollRate.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "year",
        "personnel_source",
        *SGK_4A_RATES,
        "is_active",
        "created_at",
        "updated_at",
    }
    for oran in SGK_4A_RATES:
        # %0,759 (SGK 73) ÜÇ ondalık gerektirir — Numeric(5,2) onu 0,76'ya
        # yuvarlar ve damga vergisini sessizce şişirirdi.
        assert columns[oran].type.scale == 3, f"{oran} %0,759'u taşıyamaz"
        assert columns[oran].type.precision == 6
        if oran == "income_tax_pct":
            # 🔴 IK3-GV K3 — TEK nullable oran: `NULL` = DİLİMLİ MOTOR
            # (`payroll_tax_brackets`), dolu = düz oran. Rejim seçimi de
            # VERİDİR; koda gömülü bir `if source in (...)` K1 ile çelişirdi.
            # `NULL` "vergi yok" DEMEK DEĞİLDİR: dilim seti bulunamazsa satır
            # `uncomputed`a düşer, 0 vergi ASLA yazılmaz.
            assert columns[oran].nullable
            continue
        assert not columns[oran].nullable
    assert any(
        set(c.name for c in uq.columns) == {"year", "personnel_source"}
        for uq in PayrollRate.__table__.constraints
        if uq.__class__.__name__ == "UniqueConstraint"
    ), "UQ (year, personnel_source) yok — aynı yıla iki oran seti girebilirdi"


def test_forbidden_columns_are_absent():
    """Bilinçli sınırlar (spec §7) ve türevler kolon OLARAK açılmaz."""
    period_columns = set(PayrollPeriod.__table__.columns.keys())
    for yasak in (
        # BG kartları TÜREVDİR (satırlardan toplanır).
        "total_gross",
        "total_net",
        "total_cost",
        "employee_count",
        "employer_sgk",
        # Çok aşamalı onay MOTORU açılmaz (spec §1).
        "approval_step",
        "current_approver_id",
    ):
        assert yasak not in period_columns, yasak

    line_columns = set(PayrollLine.__table__.columns.keys())
    for yasak in (
        # Kümülatif matrah TAKİP EDİLMEZ (spec §7 / K1 düz oran).
        "cumulative_base",
        "tax_bracket",
        # AGİ / asgari ücret istisnası YOK (spec §7).
        "agi_amount",
        "minimum_wage_exemption",
        # Avans/icra kesintisi YOK — brüt override'ı karşılar (spec §7).
        "advance_amount",
        "garnishment_amount",
        # Oranlar SATIRA kopyalanmaz: tek gerçek kaynak `payroll_rates` (K1).
        "sgk_employee_pct",
        "income_tax_pct",
    ):
        assert yasak not in line_columns, yasak


def test_permission_module_already_seeded():
    """S9 "21. modül" der ama `payroll` izin modülü seed'de ZATEN VARDIR
    (IZN-B6b: `core/sayfalar.py` MODUL_ANAHTARLARI; eskiden `seed_data.MODULES`, sort_order 8)
    — ST/`inventory` emsali: yeni izin modülü AÇILMAZ, izin migration'ı YAZILMAZ."""
    from app.core.sayfalar import MODUL_ANAHTARLARI
    from app.modules.roles.seed_data import MATRIX

    keys = set(MODUL_ANAHTARLARI)
    assert "payroll" in keys, "İK-3 uçlarının dayandığı izin modülü seed'den kalkmış"
    assert "payroll" in MATRIX
    assert "bordro" not in keys, "ikinci bir bordro modülü açılmış — tek anahtar `payroll`"


# --------------------------------------------------------------------------- #
# Migration tur donusu
# --------------------------------------------------------------------------- #


def test_alembic_has_single_head():
    """İki head = canlıda deploy kilitlenmesi (`alembic upgrade head` patlar)."""
    result = subprocess.run(
        [*ALEMBIC_CMD, "heads"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, f"tek head bekleniyordu, çıktı:\n{result.stdout}"


async def test_upgrade_downgrade_upgrade_round_trip():
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", IK3_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for table in TABLES:
                assert await _table_exists(conn, table), table
            for enum_name in NEW_ENUMS:
                assert await _enum_exists(conn, enum_name), enum_name
            for index in INDEXES:
                assert await _index_exists(conn, index), index
            assert await _enum_labels(conn, "payroll_period_status") == [
                "draft",
                "pending_approval",
                "approved",
                "paid",
            ]
            assert await _enum_labels(conn, "payroll_line_status") == [
                "uncomputed",
                "pending",
                "approved",
                "paid",
                "excluded",
            ]
            # Takas: paylaşılan tip BEŞ değerli, ADI DEĞİŞMEDİ.
            assert await _enum_labels(conn, "worker_source") == [
                "company",
                "subcontractor",
                "general",
                "freelance",
                "intern",
            ]
            # Takasın kurbanı olmaması gereken iki kolon halen ayakta:
            assert await _table_exists(conn, "personnel")
            assert await _table_exists(conn, "site_diary_worker_counts")
            assert await _current_revision(conn) == IK3_REVISION
        finally:
            await conn.close()

        _run_alembic("downgrade", PARENT_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for table in TABLES:
                # Kalan bir tablo İKİNCİ upgrade'i "already exists" ile patlatırdı.
                assert not await _table_exists(conn, table), table
            for enum_name in NEW_ENUMS:
                # Enum tablolarla BİRLİKTE düşmez: açıkça DROP TYPE gerekir.
                assert not await _enum_exists(conn, enum_name), (
                    f"{enum_name} tipi downgrade'de kalmış — ikinci upgrade patlar"
                )
            # Takas GERİ ALINIR: paylaşılan tip üç değerine döner.
            assert await _enum_labels(conn, "worker_source") == [
                "company",
                "subcontractor",
                "general",
            ]
            assert await _current_revision(conn) == PARENT_REVISION
        finally:
            await conn.close()

        _run_alembic("upgrade", IK3_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for table in TABLES:
                assert await _table_exists(conn, table), table
            assert await _current_revision(conn) == IK3_REVISION
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_2026_rate_seed_matches_mockups():
    """S1/S2: dört tip için 2026 oranları — SGK 70-81 + BY 243/285 birebir."""
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", IK3_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            rows = await conn.fetch(
                "SELECT personnel_source::text AS src, sgk_employee_pct, "
                "unemployment_employee_pct, income_tax_pct, stamp_tax_pct, "
                "sgk_employer_pct, unemployment_employer_pct, short_work_pct, is_active "
                "FROM payroll_rates WHERE year = 2026"
            )
            seeded = {row["src"]: row for row in rows}
            assert set(seeded) == set(EXPECTED_SEED), (
                f"2026 seed'i dört tipi kapsamıyor: {sorted(seeded)}"
            )
            for source, beklenen in EXPECTED_SEED.items():
                row = seeded[source]
                assert row["is_active"] is True, source
                for oran, deger in beklenen.items():
                    assert row[oran] == deger, f"{source}.{oran}: {row[oran]} != {deger}"
            # `general` (şantiye günlüğü "genel işçi") bordro tipi DEĞİLDİR —
            # BY dört bölüm çiziyor, beşincisi yok. Uydurma satır açılmaz.
            assert "general" not in seeded
            # Başka yıl seed EDİLMEZ: oran tablosu yıllıktır ve 2027'yi
            # uydurmak mevzuatı icat etmek olurdu (K1).
            assert await conn.fetchval("SELECT count(*) FROM payroll_rates") == 4
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_round_trip_with_existing_data_preserves_rows():
    """VERİ VARKEN tur dönüşü: takas mevcut satırları BOZMAZ.

    Boş şemada koşan bir tur dönüşü `USING` dönüşümünü hiç sınamaz — kolonun
    sunucu varsayılanını düşürmeyi unutan bir takas ancak DOLU tabloda patlar.
    Burada eski değerleri (`company`/`general`) taşıyan satırlar downgrade+upgrade
    boyunca AYNEN kalmalı.
    """
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", IK3_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for ad, kaynak in (("Ali Şirket", "company"), ("Veli Taşeron", "subcontractor")):
                await conn.execute(
                    "INSERT INTO personnel (id, full_name, source, is_active) "
                    "VALUES ($1, $2, $3::worker_source, true)",
                    uuid.uuid4(),
                    ad,
                    kaynak,
                )
        finally:
            await conn.close()

        _run_alembic("downgrade", PARENT_REVISION, database=database)
        _run_alembic("upgrade", IK3_REVISION, database=database)

        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            rows = await conn.fetch(
                "SELECT full_name, source::text AS src FROM personnel ORDER BY full_name"
            )
            assert [(row["full_name"], row["src"]) for row in rows] == [
                ("Ali Şirket", "company"),
                ("Veli Taşeron", "subcontractor"),
            ], "takas mevcut satırları bozdu"
            # Seed de ikinci upgrade'de bir kez daha yazılır, çoğalmaz.
            assert await conn.fetchval("SELECT count(*) FROM payroll_rates") == 4
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_downgrade_fails_loudly_when_new_labels_in_use():
    """Eklenen değeri KULLANAN satır varsa downgrade DURUR (fail-loud).

    Postgres enum değeri düşüremez; tip yeniden kurulur. `freelance` bir satırı
    sessizce `general`e çevirmek personelin VERGİ REJİMİNİ değiştirirdi (serbest
    meslek %20 stopaj, genel işçi %25,759) ve bir sonraki `compute` yanlış
    kesinti üretirdi — PARA sınıfı yalan. Operatör önce kaydı çözmelidir.
    """
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", IK3_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            await conn.execute(
                "INSERT INTO personnel (id, full_name, source, is_active) "
                "VALUES ($1, 'Kemal Tunç', 'freelance', true)",
                uuid.uuid4(),
            )
        finally:
            await conn.close()

        env = {**os.environ, "DATABASE_URL": _asyncpg_dsn(database)}
        result = subprocess.run(
            [*ALEMBIC_CMD, "downgrade", PARENT_REVISION],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode != 0, (
            "downgrade sessizce geçti — `freelance` satırı çevrilmiş ya da düşürülmüş olabilir"
        )
        assert "personnel.source" in result.stderr, result.stderr

        # Kapı DURDURDUĞU için şema ve satır OLDUĞU GİBİ kalmalı: yarım
        # uygulanmış bir downgrade (tablolar düşmüş, tip dönmemiş) canlıyı
        # kurtarılamaz halde bırakırdı.
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _current_revision(conn) == IK3_REVISION
            for table in TABLES:
                assert await _table_exists(conn, table), table
            assert (
                await conn.fetchval(
                    "SELECT source::text FROM personnel WHERE full_name = $1", "Kemal Tunç"
                )
            ) == "freelance"
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_db_level_semantics():
    """DB seviyesinde: (year, month) UQ · dönem CASCADE · personel RESTRICT ·
    (period, personnel) UQ · ay aralığı CHECK'i."""
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", IK3_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            period_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO payroll_periods (id, year, month, status) "
                "VALUES ($1, 2026, 7, 'draft')",
                period_id,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO payroll_periods (id, year, month, status) "
                    "VALUES ($1, 2026, 7, 'draft')",
                    uuid.uuid4(),
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO payroll_periods (id, year, month, status) "
                    "VALUES ($1, 2026, 13, 'draft')",
                    uuid.uuid4(),
                )

            personnel_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO personnel (id, full_name, source, is_active) "
                "VALUES ($1, 'Kemal Tunç', 'freelance', true)",
                personnel_id,
            )
            line_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO payroll_lines (id, payroll_period_id, personnel_id, "
                "personnel_source, status) VALUES ($1, $2, $3, 'freelance', 'uncomputed')",
                line_id,
                period_id,
                personnel_id,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO payroll_lines (id, payroll_period_id, personnel_id, "
                    "personnel_source, status) VALUES ($1, $2, $3, 'freelance', 'uncomputed')",
                    uuid.uuid4(),
                    period_id,
                    personnel_id,
                )
            # RESTRICT: bordro satırı olan personel silinemez (PARA izi).
            # DAR tuple: PG RESTRICT ihlalini 23001 (`RestrictViolationError`)
            # ile bildirir, NO ACTION ise 23503'tür — sürüme özgü SQLSTATE
            # iddia edilmez (yerel PG 18 / CI PG 16).
            with pytest.raises((asyncpg.RestrictViolationError, asyncpg.ForeignKeyViolationError)):
                await conn.execute("DELETE FROM personnel WHERE id = $1", personnel_id)
            # CASCADE: dönem silinince satırları düşer (yetim satır kalmaz).
            await conn.execute("DELETE FROM payroll_periods WHERE id = $1", period_id)
            assert await conn.fetchval("SELECT count(*) FROM payroll_lines") == 0
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

"""MK-1 T1 — ekipman şeması: model katmanı + migration tur dönüşü + 21. izin modülü.

Spec: `docs/superpowers/specs/2026-08-13-mk1-makine-cekirdegi-design.md` §2, §5, §6.

NEDEN AYRI BİR TUR DÖNÜŞÜ TESTİ (İK-3/SA/ST emsali): bu migration ÜÇ YENİ TABLO,
**DOKUZ YENİ ENUM** ve bir izin modülü getiriyor. Enum'ları düşürmeyi unutan bir
`downgrade` ikinci upgrade'i "type already exists" ile patlatırdı (d4e5f6a7b8c9
dersi) ve bu yalnız CANLIDA görülürdü. Dokuz tip, tek tipli migration'lara göre
bu hatayı dokuz kat olası kılar.

Test kendi TEK KULLANIMLIK veritabanını açar ve sonunda düşürür; `.env` ve
`TEST_DATABASE_URL` veritabanı ELLENMEZ. Alembic alt süreçte koşturulur çünkü
`alembic/env.py` kendi `asyncio.run()` döngüsünü kurar (İK-3/SA/P11 deseni).
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.core.config import settings
from app.modules.equipment.models import (
    DEFAULT_MONTHLY_CAPACITY_HOURS,
    Equipment,
    EquipmentCategory,
    EquipmentFinancing,
    EquipmentFuelLog,
    EquipmentFuelType,
    EquipmentMaintenancePeriod,
    EquipmentNormUnit,
    EquipmentOwnership,
    EquipmentRatePeriod,
    EquipmentStatus,
    EquipmentWorkLog,
    WorkLogType,
)

BACKEND_DIR = Path(__file__).parents[3]
# `python -m alembic`: yerelde `.venv/bin/python`, CI'da sistem Python'u.
ALEMBIC_CMD = (sys.executable, "-m", "alembic")

# Revizyonlara AÇIKÇA çıkılır; `head` / `-1` KULLANILMAZ — sonraki dilimler
# revizyon ekledikçe bu test sessizce yanlış şeyi ölçerdi.
PARENT_REVISION = "c5d6e7f8a9b0"  # İK-3 bordro çekirdeği
MK1_REVISION = "d7e8f9a0b1c2"

TABLES = ("equipment", "equipment_work_logs", "equipment_fuel_logs")

# Spec §5'in DOKUZ tipi — downgrade HEPSİNİ düşürmek zorundadır.
NEW_ENUMS = (
    "equipment_category",
    "equipment_status",
    "equipment_ownership",
    "equipment_financing",
    "equipment_rate_period",
    "equipment_fuel_type",
    "equipment_norm_unit",
    "equipment_maintenance_period",
    "work_log_type",
)

EXPECTED_ENUM_LABELS = {
    "equipment_category": ["crane", "machinery", "truck", "concrete", "compressor", "hand_tool"],
    "equipment_status": ["working", "maintenance", "broken", "idle"],
    "equipment_ownership": ["owned", "rented"],
    "equipment_financing": ["cash", "bank_loan", "leasing"],
    "equipment_rate_period": ["hourly", "daily", "monthly"],
    "equipment_fuel_type": ["diesel", "gasoline", "electric", "none"],
    "equipment_norm_unit": ["lt_hour", "lt_km"],
    "equipment_maintenance_period": ["hours_250", "hours_500", "hours_1000", "monthly"],
    "work_log_type": ["worked", "breakdown"],
}

INDEXES = (
    "ix_equipment_category",
    "ix_equipment_site_id",
    "ix_equipment_status",
    "ix_equipment_work_logs_equipment_id",
    "ix_equipment_work_logs_work_date",
    "ix_equipment_work_logs_site_id",
    "ix_equipment_work_logs_record_type",
    "ix_equipment_fuel_logs_equipment_id",
    "ix_equipment_fuel_logs_fuel_date",
    "ix_equipment_fuel_logs_site_id",
)

MODULE_KEY = "equipment"
# spec §6: admin · full · full · view · none · full · full · view
EXPECTED_PERMISSIONS = {
    "system_admin": ("admin", "all"),
    "patron": ("full", "all"),
    "site_chief": ("full", "all"),
    "field_engineer": ("view", "all"),
    "hr_manager": ("none", "all"),
    "accounting": ("full", "all"),
    "project_manager": ("full", "all"),
    "procurement": ("view", "all"),
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
    database = f"equipment_mk1_{uuid.uuid4().hex[:8]}"
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
# Model katmani — dokuz enum
# --------------------------------------------------------------------------- #


def test_nine_enums_match_spec_exactly():
    """Spec §5 tablosu birebir. Değer adları DB'ye yazılır: sonradan
    düzeltmek bir enum TAKASI (migration) gerektirir, bu yüzden burada kilitli."""
    actual = {
        "equipment_category": [e.value for e in EquipmentCategory],
        "equipment_status": [e.value for e in EquipmentStatus],
        "equipment_ownership": [e.value for e in EquipmentOwnership],
        "equipment_financing": [e.value for e in EquipmentFinancing],
        "equipment_rate_period": [e.value for e in EquipmentRatePeriod],
        "equipment_fuel_type": [e.value for e in EquipmentFuelType],
        "equipment_norm_unit": [e.value for e in EquipmentNormUnit],
        "equipment_maintenance_period": [e.value for e in EquipmentMaintenancePeriod],
        "work_log_type": [e.value for e in WorkLogType],
    }
    assert actual == EXPECTED_ENUM_LABELS
    # K21: `idle` mockup KPI'larında sayaç olarak basılmıyor ama açılır —
    # sunucu mockup'tan FAZLA veri verebilir, EKSİK veremez.
    assert EquipmentStatus.idle.value == "idle"
    # K5: birim İKİ değerlidir; M4:62 `Lt/km` örneğini basıyor.
    assert len(EquipmentNormUnit) == 2
    # K6: dört bakım periyodu; "Aylık" saat kolonuna SIKIŞTIRILMAZ.
    assert EquipmentMaintenancePeriod.monthly.value == "monthly"


# --------------------------------------------------------------------------- #
# Model katmani — kolonlar ve silme semantigi
# --------------------------------------------------------------------------- #


def test_equipment_columns_match_spec():
    columns = Equipment.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "name",
        # URL-4: okunabilir URL kimliği (`/makine/beko-loder`).
        "slug",
        "category",
        "brand",
        "model",
        "serial_no",
        "plate_no",
        "model_year",
        "ownership",
        "purchase_amount",
        "purchase_date",
        "depreciation_years",
        "supplier_id",
        "financing",
        "market_value",
        "rate_amount",
        "rate_period",
        "site_id",
        "operator_id",
        "status",
        "status_note",
        "status_expected_date",
        "fuel_type",
        "norm_consumption",
        "norm_unit",
        "maintenance_period",
        "monthly_capacity_hours",
        # 🔴 MK-4 — Ekipman Detay ekranının SAKLANAN on alanı. Bu küme bilerek
        # TAM kümedir (bkz. altındaki gerekçe): genişleten dilim onu burada
        # GÖRÜNÜR kılmak zorundadır, sessizce değil.
        "engine_power_kw",
        "capacity_description",
        "hourmeter_hours",
        "rental_contract_no",
        "rental_start_date",
        "rental_end_date",
        "rental_min_monthly_hours",
        "rental_payment_terms",
        "last_service_date",
        "last_service_hourmeter",
        "is_company_asset",
        "is_active",
        "created_at",
        "updated_at",
    }
    # M2:84-85: yalnız ad ve kategori zorunludur.
    assert not columns["name"].nullable
    assert not columns["category"].nullable
    # K2: DB'de nullable — kural SERVİStedir (kiralık makinenin alış bedeli yok).
    assert columns["purchase_amount"].nullable
    assert columns["purchase_amount"].server_default is None
    # K5: norm tüketim SAYIDIR (String değil) ve birimi AYRI enum'dur.
    assert columns["norm_consumption"].type.precision == 10
    assert columns["norm_consumption"].type.scale == 2
    # K7: kapasite VERİDİR ve varsayılanı 200'dür (mockup'tan doğrulandı).
    assert not columns["monthly_capacity_hours"].nullable
    assert columns["monthly_capacity_hours"].default.arg == DEFAULT_MONTHLY_CAPACITY_HOURS
    # Para ölçeği: `float` YOK, kuruş hassasiyeti (K19).
    for para in ("purchase_amount", "market_value", "rate_amount"):
        assert columns[para].type.scale == 2, para
        assert columns[para].type.precision == 18, para


def test_equipment_foreign_keys_are_set_null():
    """K3/K4: tedarikçi, şantiye ya da personel kaydı kalksa makine ve maliyet
    geçmişi AYAKTA kalır — bağ kopar, veri kaybolmaz."""
    columns = Equipment.__table__.columns
    for kolon, hedef in (
        ("supplier_id", "suppliers.id"),
        ("site_id", "sites.id"),
        ("operator_id", "personnel.id"),
    ):
        (fk,) = tuple(columns[kolon].foreign_keys)
        assert fk.ondelete == "SET NULL", kolon
        assert fk.target_fullname == hedef, kolon
        assert columns[kolon].nullable, kolon


def test_work_log_columns_and_restrict_semantics():
    columns = EquipmentWorkLog.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "equipment_id",
        "work_date",
        "site_id",
        "operator_id",
        "record_type",
        "start_time",
        "end_time",
        "hours",
        "note",
        "created_by_id",
        "created_at",
        "updated_at",
    }
    # 🔴 Maliyet izi: kaydı olan ekipman silinemez (`payroll_lines`→`personnel`).
    (equipment_fk,) = tuple(columns["equipment_id"].foreign_keys)
    assert equipment_fk.ondelete == "RESTRICT"
    # K9: kaydın KENDİ şantiyesi — nullable ve SET NULL.
    (site_fk,) = tuple(columns["site_id"].foreign_keys)
    assert site_fk.ondelete == "SET NULL"
    assert columns["site_id"].nullable
    # Arıza kaydında operatör YOKTUR (M3:280).
    assert columns["operator_id"].nullable
    # K11: `hours` NOT NULL'dır (sunucu her zaman hesaplar), aralık nullable'dır.
    assert not columns["hours"].nullable
    assert columns["start_time"].nullable
    assert columns["end_time"].nullable


def test_work_log_has_no_unique_constraint():
    """Spec §2.2: UQ YOKTUR — bir ekipman aynı gün birden çok vardiya/arıza
    kaydı taşıyabilir. UQ konsaydı ikinci vardiya sessizce reddedilirdi; tavan
    (K12) bir EŞİK denetimidir ve serviste KİLİTLE uygulanır."""
    assert not [
        c
        for c in EquipmentWorkLog.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    ]


def test_fuel_log_columns_and_derived_amount():
    columns = EquipmentFuelLog.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "equipment_id",
        "fuel_date",
        "site_id",
        "liters",
        "unit_price",
        "entered_by_id",
        "note",
        "created_at",
        "updated_at",
    }
    # `amount` KOLON DEĞİLDİR: iki yerde yaşayan para zamanla ayrışır (P10).
    assert "amount" not in columns
    (equipment_fk,) = tuple(columns["equipment_id"].foreign_keys)
    assert equipment_fk.ondelete == "RESTRICT"
    # K14: "Giren" ROL değil KULLANICIDIR.
    (user_fk,) = tuple(columns["entered_by_id"].foreign_keys)
    assert user_fk.target_fullname == "users.id"
    assert user_fk.ondelete == "SET NULL"
    # K13: birim fiyat SATIR bazlıdır ve DÖRT ondalıklıdır (M4:111).
    assert not columns["unit_price"].nullable
    assert columns["unit_price"].type.scale == 4
    assert columns["liters"].type.scale == 2


def test_forbidden_columns_are_absent():
    """Bilinçli sınırlar (spec §2, §5, §9) — türevler ve icatlar kolon OLMAZ."""
    equipment_columns = set(Equipment.__table__.columns.keys())
    for yasak in (
        # K4: ikinci bir atama hedefi "makine nerede"ye iki cevap üretirdi.
        "warehouse_id",
        # M2'de taslak butonu YOKTUR (personel formunun aksine).
        "is_draft",
        # K3: satıcı ve kiralama firması TEK kolondur.
        "rental_company",
        "rental_company_id",
        "supplier_name",
        # K5: norm tüketim METİN olarak saklanmaz.
        "norm_consumption_text",
        # K15/K16/K18: türevler kolon DEĞİLDİR.
        "usage_pct",
        "total_hours",
        "monthly_cost",
        "last_maintenance_date",
        # M1 emojisi DB'de tutulmaz — kategoriden türer (spec §5).
        "icon",
        # MK-2'ye devredildi (spec §9): belge slotları ve kira hakedişi.
        "inspection_document_id",
        "insurance_expiry_date",
    ):
        assert yasak not in equipment_columns, yasak

    work_log_columns = set(EquipmentWorkLog.__table__.columns.keys())
    for yasak in (
        # K10: arıza AYRI KAYIT TİPİDİR, aynı kayıtta ikinci saat kolonu değil.
        "breakdown_hours",
        "idle_hours",
        # K18: maliyet TÜREVDİR ve formülü TEK yerdedir.
        "cost",
        "hourly_rate",
    ):
        assert yasak not in work_log_columns, yasak


# --------------------------------------------------------------------------- #
# Izin modulu — seed katmani
# --------------------------------------------------------------------------- #


def test_permission_module_is_seeded_as_21st():
    """Spec §6: `equipment` 21. modüldür ve `sort_order` SONA eklenir —
    mevcut modüllerin sırası KAYDIRILMAZ (boq 17 / contracts 18 / sales 19 /
    documents 20 deseni).

    IZN-B6b: `modules` tablosu/`seed_data.MODULES` uygulamadan kalktı; modül kaydının
    meta verisi (ad/grup/sıra) yalnız migration'ın tarihidir → DONMUŞ kopyadan okunur
    (`tests/_donmus_eski_matris.py`; migration eşitliği
    `test_seed_migration_matches_seed_data.py`ta). Canlı kapı modülü olduğu ayrıca çakılır.
    """
    from app.core.sayfalar import MODUL_ANAHTARLARI
    from app.modules.roles.seed_data import MATRIX
    from tests._donmus_eski_matris import MODULES, ModuleGroup

    (row,) = [module for module in MODULES if module["key"] == MODULE_KEY]
    assert row["name"] == "Makine & Ekipman"
    assert row["group"] is ModuleGroup.SAHA
    assert row["sort_order"] == 21
    # 🔴 AI-0b: `equipment` artık SON modül DEĞİL — 22. modül `ai` (sort_order
    # 22) eklendi. Ölçülen şey "sona eklendi" olgusu değil, **`equipment`in
    # kendi sırasının KAYMADIĞI**dır; `max(...)` iddiası her yeni modülde
    # ritüelle güncellenen bir sihirli sayıya dönüşürdü. Yerine: `equipment`ten
    # SONRA gelen modüller ADIYLA sayılır.
    assert {m["key"] for m in MODULES if m["sort_order"] > row["sort_order"]} == {
        "ai",
        "earned_value",  # PLN-B1: 23. sira
    }
    assert MODULE_KEY in MATRIX
    assert MODULE_KEY in MODUL_ANAHTARLARI


def test_permission_row_matches_spec_semantics():
    """IZN-B6b: canlı `seed_data.MATRIX` yalnız DÜZEY taşır (Scope kalktı) → düzeyler canlıdan,
    (düzey, kapsam) çiftinin tamamı migration'ın donmuş kopyasından çakılır."""
    from app.modules.roles.seed_data import MATRIX, ROLE_ORDER
    from tests import _donmus_eski_matris as eski_seed

    cells = dict(zip(ROLE_ORDER, MATRIX[MODULE_KEY], strict=True))
    actual = {role: level.value for role, level in cells.items()}
    assert actual == {role: level for role, (level, _scope) in EXPECTED_PERMISSIONS.items()}
    eski_cells = dict(zip(eski_seed.ROLE_ORDER, eski_seed.MATRIX[MODULE_KEY], strict=True))
    eski_actual = {role: (level.value, scope.value) for role, (level, scope) in eski_cells.items()}
    assert eski_actual == EXPECTED_PERMISSIONS
    # Silme YALNIZ system_admin'dedir: `full` silmeyi kapsamaz (core/access.py).
    assert actual["patron"] == "full"


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
    # Head'in KİMLİĞİ iddia EDİLMEZ: her yeni dilim head'i ileri taşır ve bu
    # testi ilgisiz yere kırardı (repo kanonu — P11/ST testleri de yalnız SAYIYI
    # ölçer). MK-1'in kendi revizyonu aşağıdaki tur dönüşünde AÇIKÇA kullanılır;
    # burada ölçülen şey yalnız "çatallanma yok"tur. (MK-2 bu dersi kanıtladı:
    # `e8f9a0b1c2d3` head olunca kimlik iddiası kırıldı.)
    assert len(heads) == 1, f"tek head bekleniyordu, çıktı:\n{result.stdout}"


async def test_upgrade_downgrade_upgrade_round_trip():
    """🔴 Dokuz enum'un HEPSİ downgrade'de düşmeli — biri kalırsa ikinci
    `upgrade` "type already exists" ile patlar ve bu YALNIZ CANLIDA görülür."""
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", MK1_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for table in TABLES:
                assert await _table_exists(conn, table), table
            for enum_name in NEW_ENUMS:
                assert await _enum_exists(conn, enum_name), enum_name
                assert await _enum_labels(conn, enum_name) == EXPECTED_ENUM_LABELS[enum_name]
            for index in INDEXES:
                assert await _index_exists(conn, index), index
            assert await _current_revision(conn) == MK1_REVISION
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
            assert await _current_revision(conn) == PARENT_REVISION
        finally:
            await conn.close()

        _run_alembic("upgrade", MK1_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            for table in TABLES:
                assert await _table_exists(conn, table), table
            assert await _current_revision(conn) == MK1_REVISION
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_permission_module_exists_after_migration_and_is_removed_on_downgrade():
    """🔴 Yeni izin modülü MIGRATION ister (BC/`documents` emsali): `seed_data.py`yi
    değiştirmek canlıdaki MEVCUT kayıtlara satır EKLEMEZ."""
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", MK1_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            module = await conn.fetchrow(
                'SELECT name, "group"::text AS grp, sort_order FROM modules WHERE key = $1',
                MODULE_KEY,
            )
            assert module is not None, "21. izin modülü migration'da açılmamış"
            assert module["name"] == "Makine & Ekipman"
            assert module["grp"] == "SAHA"
            assert module["sort_order"] == 21
            # Diğer modüllerin sırası KAYDIRILMADI (sona eklendi).
            assert (
                await conn.fetchval("SELECT sort_order FROM modules WHERE key = 'documents'")
            ) == 20

            rows = await conn.fetch(
                "SELECT r.key AS role_key, p.access_level::text AS lvl, p.scope::text AS scp "
                "FROM role_permissions p "
                "JOIN roles r ON r.id = p.role_id "
                "JOIN modules m ON m.id = p.module_id "
                "WHERE m.key = $1",
                MODULE_KEY,
            )
            assert {row["role_key"]: (row["lvl"], row["scp"]) for row in rows} == (
                EXPECTED_PERMISSIONS
            )
        finally:
            await conn.close()

        _run_alembic("downgrade", PARENT_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert (
                await conn.fetchval("SELECT count(*) FROM modules WHERE key = $1", MODULE_KEY)
            ) == 0
            # Yetim izin satırı KALMAZ: modül geri gelince matris ikilenirdi.
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM role_permissions p "
                    "LEFT JOIN modules m ON m.id = p.module_id WHERE m.id IS NULL"
                )
            ) == 0
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_db_level_semantics():
    """DB seviyesinde: ekipman RESTRICT (çalışma VE yakıt kaydı) · şantiye
    SET NULL · pozitif litre/birim fiyat CHECK'leri · zaman çifti CHECK'i."""
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", MK1_REVISION, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            equipment_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO equipment (id, name, category) VALUES ($1, $2, 'crane')",
                equipment_id,
                "Kule Vinç KV-01",
            )
            # K7 sunucu varsayılanı: kapasite 200'dür (mockup rozetleri).
            assert (
                await conn.fetchval(
                    "SELECT monthly_capacity_hours FROM equipment WHERE id = $1", equipment_id
                )
            ) == DEFAULT_MONTHLY_CAPACITY_HOURS

            await conn.execute(
                "INSERT INTO equipment_work_logs (id, equipment_id, work_date, record_type, "
                "start_time, end_time, hours) "
                "VALUES ($1, $2, DATE '2026-08-13', 'worked', TIME '06:00', TIME '15:00', 9)",
                uuid.uuid4(),
                equipment_id,
            )
            # K11: aralığın YALNIZ bir ucu verilemez.
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO equipment_work_logs (id, equipment_id, work_date, record_type, "
                    "start_time, hours) "
                    "VALUES ($1, $2, DATE '2026-08-13', 'worked', TIME '06:00', 9)",
                    uuid.uuid4(),
                    equipment_id,
                )

            await conn.execute(
                "INSERT INTO equipment_fuel_logs (id, equipment_id, fuel_date, liters, unit_price) "
                "VALUES ($1, $2, DATE '2026-08-13', 45, 39.7000)",
                uuid.uuid4(),
                equipment_id,
            )
            # Sıfır litre / bedelsiz kayıt sapma hesabını sessizce sulandırırdı.
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO equipment_fuel_logs (id, equipment_id, fuel_date, liters, "
                    "unit_price) VALUES ($1, $2, DATE '2026-08-13', 0, 39.7000)",
                    uuid.uuid4(),
                    equipment_id,
                )

            # 🔴 RESTRICT: kaydı olan ekipman silinemez (maliyet izi).
            # DAR tuple: PG RESTRICT ihlalini 23001 ile, NO ACTION 23503 ile
            # bildirir — sürüme özgü SQLSTATE iddia edilmez (yerel 18 / CI 16).
            with pytest.raises((asyncpg.RestrictViolationError, asyncpg.ForeignKeyViolationError)):
                await conn.execute("DELETE FROM equipment WHERE id = $1", equipment_id)
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

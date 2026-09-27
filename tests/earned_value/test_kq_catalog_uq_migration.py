"""KATALOG-UQ — `aab10fbf5471` migration: dondurulmus normalize (v1) + doldurma + cift durdurma.

`Dockerfile` acilista `alembic upgrade head` kosar; patlarsa uvicorn hic baslamaz. Bu yuzden:
* dondurulmus `_normalize` (v1) bu dosyadaki TARIHSEL sabit `_V1_HISTORICAL` ile TUM kod
  noktalarinda ayni kalmali (migration `app`i import etmez; bu test aab10'un dondurulmus
  kopyasini KORUR). 🔴 KATALOG-UQ-2 (2026-09-27): `app.modules.earned_value.labels.
  normalize_label` v2'ye GENISLEDI (NFKC + casefold + genis Cf silme) — bu dosya artik
  BUGUNKU app fonksiyonuyla KARSILASTIRMAZ; v1 dondurulmus kopya sonsuza dek `_V1_HISTORICAL`
  ile sinanir (bkz. `tests/earned_value/test_kq2_catalog_normalize_v2_migration.py` v2 icin
  ayni desen);
* doldurma Python iledir: saklanan anahtar = `_normalize(ham)` BAYT BAYT (SQL `lower()` YOK);
* canlida normalize cift varsa migration ACIK MESAJLA durur, sema DEGISMEZ, mesaj
  `_duplicate_groups`un (canli sayim betiginin kullandigi TEK tanim) gruplarini listeler;
* upgrade → downgrade → upgrade temiz doner (eski birebir UQ geri gelir).

Tek kullanimlik veritabani; `TEST_DATABASE_URL` veritabani ELLENMEZ (HZ-1 deseni).
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import unicodedata
import uuid
from types import ModuleType

import asyncpg

from app.modules.earned_value.labels import normalize_label
from tests.modules.treasury.test_hz1_migration import (
    ALEMBIC_CMD,
    BACKEND_DIR,
    _asyncpg_dsn,
    _create_scratch_database,
    _drop_scratch_database,
    _run_alembic,
)

PREVIOUS = "c7d1e2f3a4b5"
KQ = "aab10fbf5471"
OLD_UQ = "uq_ev_catalog_items_disc_name_uom"
NEW_UQ = "uq_ev_catalog_items_disc_name_key_uom_key"
MIGRATION_PATH = next((BACKEND_DIR / "alembic" / "versions").glob(f"{KQ}_*.py"))

#: KATALOG-UQ K1 vakalari (baglamli: final sigma, NFD, bosluk ortada/uclarda).
K1_CASES = [
    "Beton döküm",
    "  BETON   DÖKÜM ",
    "KİREÇ SIVA",
    "IŞIK",
    "ÇĞÖŞÜ",
    "ÂÎÛ",
    "Ÿ",
    "Ø",
    "Å",
    "Æ",
    "Œ",
    "Ł",
    "Ć",
    "Ž",
    "Š",
    "Ý",
    "Ã",
    "Ñ",
    "Ő",
    "Ę",
    "Ř",
    "ΒΕΤΟΝ",
    "ΟΔΟΣ",
    "Σ",
    "ΟΔΟΣ ΑΣ",
    "БЕТОН",
    "ß",
    "ẞ",
    "STRAẞE",
    "i̇",
    "İ",  # NFD "İ": capital I + birlesik nokta ustu (KATALOG-UQ madde 1, sira testi)
    "İ",
    "I",
    "K",
    "Ω",
    "ＡＢ",
    "Ⅻ",
    "Ⓐ",
    "ǅ",
    "𐐀",
    "m³",
    "M²",
    "m¹",
    "m⁴",
    "é",
    "é",
    "É",
    "a b",
    " a ",
    "a b",
    "a　b",
    "a\u0085b",
    "a\u001cb",
    "a​b",
    "a﻿b",
    "\ta\n\rb ",
    "a᠎b",
    "",
]


def _load() -> ModuleType:
    name = f"_migration_{MIGRATION_PATH.stem}"
    spec = importlib.util.spec_from_file_location(name, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ DB'siz: dondurulmus kopya

#: TARIHSEL SABIT — KATALOG-UQ v1 kuralinin (2026-09-25) BIREBIR kopyasi, app'ten BAGIMSIZ.
#: `app.modules.earned_value.labels.normalize_label` KATALOG-UQ-2'de v2'ye GENISLEDIGI icin
#: bu dosyadaki testler artik app ile DEGIL, bu sabitle karsilastirir: aab10'un dondurulmus
#: `_normalize`si SONSUZA DEK bu tarihsel davranisi korumali (migration UYGULANMIS, DEGISMEZ).
_V1_ZERO_WIDTH_HISTORICAL = str.maketrans("", "", "​‌‍﻿")


def _v1_historical(text: str) -> str:
    s = unicodedata.normalize("NFC", text)
    s = s.replace("İ", "i").replace("I", "ı").lower()
    s = s.replace("³", "3").replace("²", "2")
    s = s.translate(_V1_ZERO_WIDTH_HISTORICAL)
    s = re.sub(r"\s+", " ", s).strip()
    return unicodedata.normalize("NFC", s)


def test_KQ_frozen_normalize_equals_v1_historical_on_k1_cases() -> None:
    mig = _load()
    diffs = [c for c in K1_CASES if mig._normalize(c) != _v1_historical(c)]  # noqa: SLF001
    assert diffs == []


def test_KQ_frozen_normalize_equals_v1_historical_on_every_code_point() -> None:
    """Her kod noktasi (vekil haric) 'x'+c+'x' icinde ve TEK BASINA (uc kirpma)."""
    mig = _load()
    frozen = mig._normalize  # noqa: SLF001
    diffs = []
    for cp in range(1, 0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        c = chr(cp)
        for s in (f"x{c}x", c):
            if frozen(s) != _v1_historical(s):
                diffs.append((hex(cp), s))
    assert diffs == []


def test_KQ_normalize_is_idempotent_on_every_code_point() -> None:
    """normalize(normalize(x)) == normalize(x) — dondurulmus kopya VE uygulama, her kod
    noktasi ('x'+c+'x' icinde ve tek basina). NFC + sifir-genislik silme ekledikten sonra
    ikinci gecis ilk gecisi bozmamali (KATALOG-UQ madde 1)."""
    mig = _load()
    frozen = mig._normalize  # noqa: SLF001
    diffs = []
    for cp in range(1, 0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        c = chr(cp)
        for s in (f"x{c}x", c):
            once = frozen(s)
            if frozen(once) != once:
                diffs.append(("frozen", hex(cp), s, once, frozen(once)))
            once_app = normalize_label(s)
            if normalize_label(once_app) != once_app:
                diffs.append(("app", hex(cp), s, once_app, normalize_label(once_app)))
    assert diffs == []


def test_KQ_duplicate_groups_shape() -> None:
    mig = _load()
    kab, duv = uuid.uuid4(), uuid.uuid4()
    rows = [
        (1, kab, "KAB", "Beton döküm", "m³"),
        (2, kab, "KAB", "  BETON DÖKÜM", "M3"),
        (3, duv, "DUV", "BETON DÖKÜM", "m3"),  # baska disiplin: cift DEGIL
        (4, kab, "KAB", "ΟΔΟΣ", "ad"),
        (5, kab, "KAB", "οδοσ", "ad"),  # Python final sigma: cift DEGIL
        (6, kab, "KAB", "οδος", "ad"),  # 4 ile cift
    ]
    assert mig._duplicate_groups(rows) == [  # noqa: SLF001
        ("KAB", "beton döküm", "m3", [("2", "  BETON DÖKÜM", "M3"), ("1", "Beton döküm", "m³")]),
        ("KAB", "οδος", "ad", [("4", "ΟΔΟΣ", "ad"), ("6", "οδος", "ad")]),
    ]


# ------------------------------------------------------------------ gecici DB: migration turu


async def _seed(conn: asyncpg.Connection, rows: list[tuple[str, str, str]]) -> dict[str, uuid.UUID]:
    """rows: (disiplin_kodu, ad, birim). Donus: kod → disiplin id."""
    discs: dict[str, uuid.UUID] = {}
    for code in sorted({r[0] for r in rows}):
        discs[code] = uuid.uuid4()
        await conn.execute(
            "INSERT INTO ev_disciplines (id, code, name, color, default_contractor_type) "
            "VALUES ($1, $2, $2, '#2563EB', 'own')",
            discs[code],
            code,
        )
    for code, name, uom in rows:
        await conn.execute(
            "INSERT INTO ev_catalog_items "
            "(id, discipline_id, name, uom, standard_unit_mhr, default_contractor_type) "
            "VALUES ($1, $2, $3, $4, 1, 'own')",
            uuid.uuid4(),
            discs[code],
            name,
            uom,
        )
    return discs


async def _raw(conn: asyncpg.Connection) -> list[tuple]:
    return [
        tuple(r)
        for r in await conn.fetch(
            "SELECT c.id, c.discipline_id, d.code, c.name, c.uom FROM ev_catalog_items c "
            "JOIN ev_disciplines d ON d.id = c.discipline_id"
        )
    ]


async def _schema(conn: asyncpg.Connection) -> tuple[set[str], dict[str, str]]:
    cols = {
        r["column_name"]: r["is_nullable"]
        for r in await conn.fetch(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'ev_catalog_items' AND column_name IN ('name_key', 'uom_key')"
        )
    }
    uqs = {
        r["conname"]
        for r in await conn.fetch(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'ev_catalog_items'::regclass AND contype = 'u'"
        )
    }
    return uqs, cols


def _alembic_expect_failure(*args: str, database: str) -> str:
    env = {**os.environ, "DATABASE_URL": _asyncpg_dsn(database)}
    result = subprocess.run(
        [*ALEMBIC_CMD, *args], cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=300
    )
    assert result.returncode != 0, "cift varken migration BASARILI oldu"
    return result.stdout + result.stderr


NON_DUP = [
    ("KAB", "Beton döküm", "m³"),
    ("KAB", "Beton döküm", "ton"),
    ("DUV", "  BETON   DÖKÜM", "M3"),  # baska disiplin
    ("KAB", "ΟΔΟΣ", "ad"),
    ("KAB", "οδοσ", "ad"),  # en_US SQL lower() bunu ΟΔΟΣ'la CIFT sayardi; Python saymaz
    *[("MIX", name, f"u{i}") for i, name in enumerate(K1_CASES) if name.strip()],
]
DUPS = [
    ("KAB", "BETON DÖKÜM", "m3"),
    ("KAB", "οδος", "ad"),
    ("DUV", "beton döküm", "m³"),
    # KATALOG-UQ madde 1: NFC ile NFD artik AYNI anahtar — DB duzeyinde cift.
    ("KAB", "é", "ad"),  # NFC (U+00E9)
    ("KAB", "é", "ad"),  # NFD (e + U+0301)
    # Sifir genislikli karakter SILINIR → "Beton" ile "Be<ZWSP>ton" ayni anahtar.
    ("KAB", "Beton", "zw"),
    ("KAB", "Be​ton", "zw"),
]


async def test_KQ_migration_backfill_duplicate_stop_and_round_trip() -> None:
    mig = _load()
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            codes = await _seed(conn, NON_DUP)
            for code, name, uom in DUPS:  # ayni disiplinlere: normalize ciftler
                await conn.execute(
                    "INSERT INTO ev_catalog_items "
                    "(id, discipline_id, name, uom, standard_unit_mhr, default_contractor_type) "
                    "VALUES ($1, $2, $3, $4, 1, 'own')",
                    uuid.uuid4(),
                    codes[code],
                    name,
                    uom,
                )
            expected = mig._duplicate_groups(await _raw(conn))  # noqa: SLF001
        finally:
            await conn.close()
        assert [(g[0], g[1], g[2], len(g[3])) for g in expected] == [
            ("DUV", "beton döküm", "m3", 2),
            ("KAB", "beton", "zw", 2),
            ("KAB", "beton döküm", "m3", 2),
            ("KAB", "é", "ad", 2),
            ("KAB", "οδος", "ad", 2),
        ]

        # 1) cift var → ACIK mesajla dur, sema DEGISMEZ
        out = _alembic_expect_failure("upgrade", KQ, database=database)
        assert "KATALOG-UQ" in out and f"{len(expected)} normalize cift grubu" in out
        for _code, _nk, _uk, members in expected:
            for row_id, _name, _uom in members:
                assert row_id in out
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
            assert await _schema(conn) == ({OLD_UQ}, {})
            # 2) kullanici karari: her gruptan biri yeniden adlandirilir (burada: silinir)
            for _code, _nk, _uk, members in expected:
                await conn.execute(
                    "DELETE FROM ev_catalog_items WHERE id = $1", uuid.UUID(members[0][0])
                )
            before = await _raw(conn)
            assert mig._duplicate_groups(before) == []  # noqa: SLF001
        finally:
            await conn.close()

        # 3) temiz → upgrade: anahtar = dondurulmus Python normalize, BAYT BAYT
        _run_alembic("upgrade", KQ, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _schema(conn) == ({NEW_UQ}, {"name_key": "NO", "uom_key": "NO"})
            rows = await conn.fetch("SELECT name, uom, name_key, uom_key FROM ev_catalog_items")
            assert len(rows) == len(before)
            for r in rows:
                expect = (mig._normalize(r["name"]), mig._normalize(r["uom"]))  # noqa: SLF001
                assert (r["name_key"], r["uom_key"]) == expect, r["name"]
                # `mig._normalize` v1'dir (bu migration `aab10fbf5471`); app artik v2 —
                # tarihsel sabitle karsilastirilir (bkz. dosya basi `_v1_historical`).
                assert expect == (_v1_historical(r["name"]), _v1_historical(r["uom"]))
        finally:
            await conn.close()

        # 4) downgrade → eski birebir UQ, kolonlar yok; tekrar upgrade temiz
        _run_alembic("downgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _schema(conn) == ({OLD_UQ}, {})
        finally:
            await conn.close()
        _run_alembic("upgrade", KQ, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _schema(conn) == ({NEW_UQ}, {"name_key": "NO", "uom_key": "NO"})
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

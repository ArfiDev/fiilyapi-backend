"""KATALOG-UQ-2 — `3102e435238c` migration: dondurulmus v2 normalize + anahtar yeniden hesabi.

KATALOG-UQ (`aab10fbf5471`) `name_key`/`uom_key`i v1 kuraliyla doldurmustu.
`app.modules.earned_value.labels.normalize_label` v2'ye GENISLEDI (NFKC + `.casefold()` +
genis Cf/degiskec/CGJ silme + son NFKC — gerekce `labels.py` docstring'inde). Bu test:

* dondurulmus `_normalize_v2` (migrationdaki) BUGUNKU `labels.normalize_label` ile TUM
  Unicode kod noktalarinda ayni (migration `app`i import etmez; bu test ikisini baglar);
* dondurulmus `_normalize_v1` (migrationdaki, downgrade icin) `aab10fbf5471`teki
  `_normalize` ile ayni (iki dosyadaki v1 kopyalar birbirinden SAPMAMALI);
* v1→v2 "cokluk" varsayimi ("v2'de ayri olan cift v1'de de ayridir") CURUTULDU: 13 kod
  noktasinda v1-ESIT ama v2-FARKLI (ligatur/Roman rakami/dairesel/tam-genislik "I/i"
  varyantlari) — bu yuzden DOWNGRADE de cift kontrolu YAPAR;
* canlida normalize cift varsa migration (her iki yonde de) ACIK MESAJLA durur, sema/
  anahtar DEGISMEZ;
* upgrade(b5858) → tohum (v1 anahtarli, v2'de birlesecek bir cift + ayri kalanlar) →
  upgrade(3102e435238c) ciftte RuntimeError ve anahtarlar DOKUNULMAMIS → cifti gider →
  upgrade temiz, anahtarlar v2 → downgrade → v1 anahtarlar → upgrade temiz.

Tek kullanimlik veritabani; `TEST_DATABASE_URL` veritabani ELLENMEZ (HZ-1 deseni).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unicodedata
import uuid
from types import ModuleType

import asyncpg

from app.modules.earned_value.labels import normalize_label
from tests.earned_value.test_kq_catalog_uq_migration import _v1_historical
from tests.modules.treasury.test_hz1_migration import (
    ALEMBIC_CMD,
    BACKEND_DIR,
    _asyncpg_dsn,
    _create_scratch_database,
    _drop_scratch_database,
    _run_alembic,
)

PREVIOUS = "b5858dd66531"
KQ2 = "3102e435238c"
NEW_UQ = "uq_ev_catalog_items_disc_name_key_uom_key"
MIGRATION_PATH = next((BACKEND_DIR / "alembic" / "versions").glob(f"{KQ2}_*.py"))

#: v2'de v1'den FARKLI davranan kod noktalari — NFKC uyumluluk ayristirmasi getirdi.
V2_CASES = [
    "m³",
    "m¹",
    "m⁴",
    "㎥",  # KOMPLE ISARET U+339D SQUARE M CUBED
    "Ⅻ",
    "Ⓘ",
    "ⅰ",
    "ｉ",
    "Ĳ",
    "ĳ",
    "½",
    "™",
    "µ",
    "ﬁ",  # LATIN SMALL LIGATURE FI
    "Ｂｅｔｏｎ",
]


def _load() -> ModuleType:
    name = f"_migration_{MIGRATION_PATH.stem}"
    spec = importlib.util.spec_from_file_location(name, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ DB'siz: dondurulmus kopyalar


def test_KQ2_unidata_version_is_pinned_15_0_0() -> None:
    """🔴 Surum bekcisi: `_normalize_v2`/`_normalize_v1` bu Unicode surumuyle hesaplandi.
    CPython'un Unicode veritabani surumu degisirse (yeni Python minor surumu) bu kirmizi
    olmali — koddaki hicbir sey yanlis DEGIL, ama dondurulmus varsayimlar YENIDEN olculmeli."""
    assert unicodedata.unidata_version == "15.0.0", (
        f"unicodedata surumu degisti ({unicodedata.unidata_version}); "
        "KATALOG-UQ-2 dondurulmus kopyalari 15.0.0 ile hesaplandi, YENIDEN OLCULMELI"
    )


def test_KQ2_frozen_v2_equals_app_normalize_on_v2_cases() -> None:
    mig = _load()
    diffs = [c for c in V2_CASES if mig._normalize_v2(c) != normalize_label(c)]  # noqa: SLF001
    assert diffs == []


def test_KQ2_frozen_v2_equals_app_normalize_on_every_code_point() -> None:
    """Her kod noktasi (vekil haric) 'x'+c+'x' icinde ve TEK BASINA (uc kirpma)."""
    mig = _load()
    frozen = mig._normalize_v2  # noqa: SLF001
    diffs = []
    for cp in range(1, 0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        c = chr(cp)
        for s in (f"x{c}x", c):
            if frozen(s) != normalize_label(s):
                diffs.append((hex(cp), s))
    assert diffs == []


def test_KQ2_frozen_v1_equals_v1_historical_on_every_code_point() -> None:
    """Migration'daki v1 kopya, `aab10fbf5471` icin kullanilan tarihsel sabitle AYNI —
    iki dosyadaki v1 kopyalar birbirinden SAPMAMALI."""
    mig = _load()
    frozen = mig._normalize_v1  # noqa: SLF001
    diffs = []
    for cp in range(1, 0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        c = chr(cp)
        for s in (f"x{c}x", c):
            if frozen(s) != _v1_historical(s):
                diffs.append((hex(cp), s))
    assert diffs == []


def test_KQ2_v1_lengthens_on_composition_exclusions() -> None:
    """🔴 "v1 metni UZATMAZ" varsayimi YANLISTI (migration docstring'inde onceden boyleydi,
    duzeltildi): NFC'nin "composition exclusion" listesindeki kod noktalari NFD'de ikiye
    AYRISIR ama NFC GERI BIRLESTIRMEZ; `_normalize_v1`in ilk VE son adimi NFC oldugundan TEK
    BASINA bir girdi kod noktasi cikista 2 kod noktasina UZAYABILIR. Tum kod noktalari (vekil
    haric) tek basina taranir; SAYI ONCE OLCULUR SONRA SABITLENIR (regresyon bekcisi): kume
    buyurse/kuculse (yeni bir Unicode surumu ya da `_normalize_v1` degisirse) bu test kirmizi
    olur — 85 sayisi migration docstring'indeki iddiayla AYNI olmali."""
    mig = _load()
    v1 = mig._normalize_v1  # noqa: SLF001
    lengthened = {
        hex(cp)
        for cp in range(1, 0x110000)
        if not (0xD800 <= cp <= 0xDFFF) and len(v1(chr(cp))) > 1
    }
    assert len(lengthened) == 85
    assert "0x344" in lengthened  # BIRLESIK YUNAN TONOS (COMBINING GREEK YPOGEGRAMMENI komsusu)
    assert "0x958" in lengthened  # DEVANAGARI HARF QA (NUKTA'li)


def test_KQ2_normalize_v2_is_idempotent_on_every_code_point() -> None:
    """normalize_v2(normalize_v2(x)) == normalize_v2(x) — dondurulmus kopya VE uygulama,
    her kod noktasi ('x'+c+'x' icinde ve tek basina)."""
    mig = _load()
    frozen = mig._normalize_v2  # noqa: SLF001
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


def test_KQ2_idempotency_needs_final_nfkc_across_deleted_invisible_boundary() -> None:
    """Tek-kod-noktasi taramasi (yukaridaki test) SON NFKC'nin gerekliligini YAKALAMAZ:
    adim 4 (gorunmez silme) iki AYRI orijinal kod noktasini bitistirebilir ve bu YENI
    komsuluk NFKC-disi kalabilir (ör. 'e' + ZWSP + BIRLESIK INCE VURGU → silme sonrasi
    'e'+U+0301 — 'é'nin AYRISIK hali, NFKC'siz 'é'ye (U+00E9) COKMEZ). Son NFKC OLMADAN
    bu deger `normalize(normalize(x)) != normalize(x)` verir — KATALOG-UQ madde 1/5'teki
    v1 gerekcesiyle AYNI mekanizma, v2'de de GECERLI."""
    mig = _load()
    x = "e" + "​" + "́"  # e, ZWSP (Cf, silinir), birlesik ince vurgu (Mn, KALIR)
    once = mig._normalize_v2(x)  # noqa: SLF001
    assert once == "é"
    assert mig._normalize_v2(once) == once  # noqa: SLF001 — SON NFKC sayesinde idempotent
    once_app = normalize_label(x)
    assert once_app == "é"
    assert normalize_label(once_app) == once_app


def test_KQ2_v1_to_v2_coarsening_claim_is_false_documented_violations() -> None:
    """v1-esit oldugu halde v2-FARKLI olan kod noktalari VAR (13 tane, docstring'te
    listelendi) — bu yuzden downgrade de cift kontrolu YAPMALI. Bu test o iddiayi
    dogrudan olcer ve BEKLENEN ihlal kumesini SABITLER (regresyon bekcisi): kume
    buyurse/kuculse bu test kirmizi olur, migration docstring'i guncellenmeli."""
    mig = _load()
    v1, v2 = mig._normalize_v1, mig._normalize_v2  # noqa: SLF001
    group: dict[str, str] = {}
    violations: set[str] = set()
    for cp in range(1, 0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        c = chr(cp)
        k1, k2 = v1(c), v2(c)
        if k1 in group:
            if group[k1] != k2:
                violations.add(hex(cp))
        else:
            group[k1] = k2
    expected = {
        "0x133",  # ĳ
        "0x2170",  # ⅰ
        "0x2171",  # ⅱ
        "0x2172",  # ⅲ
        "0x2173",  # ⅳ
        "0x2175",  # ⅵ
        "0x2176",  # ⅶ
        "0x2177",  # ⅷ
        "0x2178",  # ⅸ
        "0x217a",  # ⅺ
        "0x217b",  # ⅻ
        "0x24d8",  # ⓘ
        "0xff49",  # ｉ
    }
    assert violations == expected


def test_KQ2_normalize_and_duplicate_groups_script_contract() -> None:
    """(2a) `katalog-tekillik-grupla.v2.py` sozlesmesi: migration `NORMALIZE` (== _normalize_v2,
    KIMLIK karsilastirmasiyla — AYNI fonksiyon nesnesi) VE `duplicate_groups(rows)` (==
    `_duplicate_groups(rows, NORMALIZE)`) saglar. MUTASYON: sozlesme (NORMALIZE/
    duplicate_groups adlari) KALDIRILIRSA bu test AttributeError ile KIRMIZI olur."""
    mig = _load()
    assert mig.NORMALIZE is mig._normalize_v2  # noqa: SLF001
    kab_id = uuid.uuid4()
    rows = [
        (1, kab_id, "KAB", "Beton döküm", "m³"),
        (2, kab_id, "KAB", "  BETON DÖKÜM", "M3"),
        (3, kab_id, "KAB", "Kalıp", "m²"),
    ]
    assert mig.duplicate_groups(rows) == mig._duplicate_groups(rows, mig._normalize_v2)  # noqa: SLF001
    assert [(g[0], g[1], g[2], len(g[3])) for g in mig.duplicate_groups(rows)] == [
        ("KAB", "beton döküm", "m3", 2)
    ]


class _FakeRow:
    """`_overflowing_rows` yalniz `.id`/`.name`/`.uom` bekler — gercek SELECT satiri gerekmez."""

    def __init__(self, name: str, uom: str) -> None:
        self.id = uuid.uuid4()
        self.name = name
        self.uom = uom


def test_KQ2_overflowing_rows_detects_name_and_uom_overflow_both_directions() -> None:
    """(1c) `_overflowing_rows` (backfill ONCESI on-kontrolun cekirdegi): v2 GENISLETILMIS
    sinirlari (800/200) ASAN VE v1 ESKI sinirlari (200/50) ASAN satirlari ayri ayri
    yakalar; sinir icindeki satiri RAPORLAMAZ. MUTASYON: fonksiyon `>` yerine `>=`
    kullansaydi ya da hic cagrilmasaydi bu test KIRMIZI olurdu."""
    mig = _load()
    long_name = "ﷺ" * 45  # v2 anahtar: 810 > 800 (NEW_NAME_KEY_LEN)
    long_uom = "ﷺ" * 12  # v2 anahtar: 216 > 200 (NEW_UOM_KEY_LEN)
    ok = _FakeRow("beton", "ad")
    bad_name = _FakeRow(long_name, "ad")
    bad_uom = _FakeRow("beton", long_uom)

    overflow_v2 = mig._overflowing_rows(  # noqa: SLF001
        [ok, bad_name, bad_uom],
        mig._normalize_v2,  # noqa: SLF001
        name_max=mig.NEW_NAME_KEY_LEN,
        uom_max=mig.NEW_UOM_KEY_LEN,
    )
    assert {(row_id, column) for row_id, _raw, column, _key, _max in overflow_v2} == {
        (str(bad_name.id), "name_key"),
        (str(bad_uom.id), "uom_key"),
    }

    # Ayni kontrol v1 (dar) sinirlariyla: NFKC olmadigindan normal bir metin tasmaz,
    # ama fonksiyonun KENDISI yonden bagimsiz calisir (yalniz sinir parametreleri degisir).
    overflow_v1 = mig._overflowing_rows(  # noqa: SLF001
        [ok, bad_name, bad_uom],
        mig._normalize_v1,  # noqa: SLF001
        name_max=mig.OLD_NAME_KEY_LEN,
        uom_max=mig.OLD_UOM_KEY_LEN,
    )
    # 🔴 v1 GENEL OLARAK metni UZATMAZ DEMEK DEGIL (85 kod noktasinda uzatir — bkz.
    # `test_KQ2_v1_lengthens_on_composition_exclusions`); ARAP LIGATUR (ﷺ) bu 85'in
    # DISINDA oldugundan burada v1 anahtari TASMAZ.
    assert overflow_v1 == []


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


async def _keys(conn: asyncpg.Connection) -> list[tuple]:
    return [
        tuple(r)
        for r in await conn.fetch("SELECT id, name, uom, name_key, uom_key FROM ev_catalog_items")
    ]


def _alembic_expect_failure(*args: str, database: str) -> str:
    env = {**os.environ, "DATABASE_URL": _asyncpg_dsn(database)}
    result = subprocess.run(
        [*ALEMBIC_CMD, *args], cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=300
    )
    assert result.returncode != 0, "cift varken migration BASARILI oldu"
    return result.stdout + result.stderr


#: v1'de AYRI (ayri name_key/uom_key uretir) ama v2'de AYNI anahtara coken cift.
V1_SEPARATE_V2_DUPLICATE = ("KAB", "m¹", "m1")  # "m¹" (v1: "m¹") vs "m1" (v1: "m1") → v2: "m1"/"m1"


async def test_KQ2_migration_backfill_duplicate_stop_and_round_trip() -> None:
    mig = _load()
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            kab_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO ev_disciplines (id, code, name, color, default_contractor_type) "
                "VALUES ($1, 'KAB', 'Kaba İnşaat', '#2563EB', 'own')",
                kab_id,
            )
            # aab10 (v1) kurali ile anahtarlar dolduruldu (bu asamada v1 kuraliyla ayrik).
            rows = [
                (kab_id, "m¹ döşeme", "ad"),
                (kab_id, "m1 döşeme", "ad"),  # v1: ayri (m¹ ≠ m1) · v2: ayni (ikisi de "m1")
                (kab_id, "Beton döküm", "m³"),
                (kab_id, "Kalıp", "m²"),
            ]
            for _, name, uom in rows:
                await conn.execute(
                    "INSERT INTO ev_catalog_items "
                    "(id, discipline_id, name, uom, name_key, uom_key, standard_unit_mhr, "
                    "default_contractor_type) VALUES ($1, $2, $3, $4, $5, $6, 1, 'own')",
                    uuid.uuid4(),
                    kab_id,
                    name,
                    uom,
                    mig._normalize_v1(name),  # noqa: SLF001
                    mig._normalize_v1(uom),  # noqa: SLF001
                )
            before = await _raw(conn)
            expected = mig._duplicate_groups(before, mig._normalize_v2)  # noqa: SLF001
        finally:
            await conn.close()
        assert [(g[0], g[1], g[2], len(g[3])) for g in expected] == [("KAB", "m1 döşeme", "ad", 2)]

        # 1) v2'de cift var → ACIK mesajla dur, anahtarlar DOKUNULMAMIS
        out = _alembic_expect_failure("upgrade", KQ2, database=database)
        assert "KATALOG-UQ-2" in out and "1 normalize cift grubu" in out
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
            untouched = await _keys(conn)
            assert {(str(r[0]), r[3], r[4]) for r in untouched} == {
                (str(rid), mig._normalize_v1(name), mig._normalize_v1(uom))  # noqa: SLF001
                for rid, _d, _c, name, uom in before
            }
            # 2) kullanici karari: cifti gider (burada: birini siler)
            dup_id = next(uuid.UUID(m[0]) for m in expected[0][3][:1])
            await conn.execute("DELETE FROM ev_catalog_items WHERE id = $1", dup_id)
        finally:
            await conn.close()

        # 3) temiz → upgrade: anahtar = dondurulmus v2, BAYT BAYT
        _run_alembic("upgrade", KQ2, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            rows2 = await _keys(conn)
            for _id, name, uom, name_key, uom_key in rows2:
                expect = (mig._normalize_v2(name), mig._normalize_v2(uom))  # noqa: SLF001
                assert (name_key, uom_key) == expect, name
                assert expect == (normalize_label(name), normalize_label(uom))
        finally:
            await conn.close()

        # 4) downgrade → v1 anahtarlar geri turer; tekrar upgrade temiz
        _run_alembic("downgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            rows3 = await _keys(conn)
            for _id, name, uom, name_key, uom_key in rows3:
                assert (name_key, uom_key) == (
                    mig._normalize_v1(name),  # noqa: SLF001
                    mig._normalize_v1(uom),  # noqa: SLF001
                )
        finally:
            await conn.close()
        _run_alembic("upgrade", KQ2, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            rows4 = await _keys(conn)
            for _id, name, uom, name_key, uom_key in rows4:
                assert (name_key, uom_key) == (
                    mig._normalize_v2(name),  # noqa: SLF001
                    mig._normalize_v2(uom),  # noqa: SLF001
                )
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_KQ2_downgrade_stops_on_v1_duplicate_without_mutating() -> None:
    """v2'de AYRI ama v1'e donuste ESITLENEN bir cift: downgrade cift-durdurma korumasi.
    `Ĳ` (v2: "ıj") ile `ĳ` (v2: "ij") v2'de AYRI kalirlar (KQ2 upgrade sorunsuz), ama
    ikisi de v1'de `"ĳ"`e coker → downgrade DURMALI, sema/anahtar DEGISMEMELI."""
    mig = _load()
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", KQ2, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            kab_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO ev_disciplines (id, code, name, color, default_contractor_type) "
                "VALUES ($1, 'KAB', 'Kaba İnşaat', '#2563EB', 'own')",
                kab_id,
            )
            for name in ("Ĳsseldam", "ĳsseldam"):
                await conn.execute(
                    "INSERT INTO ev_catalog_items "
                    "(id, discipline_id, name, uom, name_key, uom_key, standard_unit_mhr, "
                    "default_contractor_type) VALUES ($1, $2, $3, 'ad', $4, $5, 1, 'own')",
                    uuid.uuid4(),
                    kab_id,
                    name,
                    mig._normalize_v2(name),  # noqa: SLF001
                    mig._normalize_v2("ad"),  # noqa: SLF001
                )
            before = await _keys(conn)
        finally:
            await conn.close()

        out = _alembic_expect_failure("downgrade", PREVIOUS, database=database)
        assert "KATALOG-UQ-2" in out and "downgrade" in out

        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == KQ2
            assert await _keys(conn) == before
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def _column_lengths(conn: asyncpg.Connection) -> dict[str, int]:
    return {
        r["column_name"]: r["character_maximum_length"]
        for r in await conn.fetch(
            "SELECT column_name, character_maximum_length FROM information_schema.columns "
            "WHERE table_name = 'ev_catalog_items' AND column_name IN ('name_key', 'uom_key')"
        )
    }


async def test_KQ2_upgrade_stops_on_key_overflow_without_widening_or_mutating() -> None:
    """(1c) Backfill ONCESI uzunluk on-kontrolu: v2 anahtari GENISLETILMIS sinirlari
    (800/200) bile asarsa migration ACIK RuntimeError'la durur — kolon GENISLETILMEZ,
    anahtar DEGISMEZ (ALTER COLUMN'dan ONCE kontrol yapildiginin kaniti: hala eski/dar
    200/50 sinirda kalmali).

    MUTASYON: bu on-kontrol kaldirilirsa (ya da ALTER COLUMN kontrolden ONCEYE alinirsa)
    upgrade ya PostgreSQL `22001` (deger kolonun sinirini asiyor) ile ya da BASARILI ama
    yanlis (kirpilmis/tasan) bir anahtarla biter — bu test HER IKI durumda da KIRMIZI olur."""
    mig = _load()
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            kab_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO ev_disciplines (id, code, name, color, default_contractor_type) "
                "VALUES ($1, 'KAB', 'Kaba İnşaat', '#2563EB', 'own')",
                kab_id,
            )
            # 45 <= 200 (v1 semasindaki 'name' kolonu) ama v2 anahtari 810 > 800 (NEW_NAME_KEY_LEN).
            overflow_name = "ﷺ" * 45
            assert len(overflow_name) <= 200
            assert len(mig._normalize_v2(overflow_name)) > mig.NEW_NAME_KEY_LEN  # noqa: SLF001
            row_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO ev_catalog_items "
                "(id, discipline_id, name, uom, name_key, uom_key, standard_unit_mhr, "
                "default_contractor_type) VALUES ($1, $2, $3, 'ad', $4, $5, 1, 'own')",
                row_id,
                kab_id,
                overflow_name,
                mig._normalize_v1(overflow_name),  # noqa: SLF001
                mig._normalize_v1("ad"),  # noqa: SLF001
            )
            before = await _keys(conn)
            before_lengths = await _column_lengths(conn)
        finally:
            await conn.close()
        assert before_lengths == {"name_key": 200, "uom_key": 50}

        out = _alembic_expect_failure("upgrade", KQ2, database=database)
        assert "KATALOG-UQ-2" in out
        assert str(row_id) in out
        assert "asiyor" in out or "sinirini asiyor" in out

        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
            # Kolon GENISLETILMEDI (ALTER COLUMN hic calismadi — kontrol ONDEN durdu).
            assert await _column_lengths(conn) == {"name_key": 200, "uom_key": 50}
            # Anahtar DOKUNULMAMIS (hala v1).
            assert await _keys(conn) == before
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_KQ2_downgrade_stops_on_v1_key_overflow_without_narrowing_or_mutating() -> None:
    """(3) Downgrade'in backfill-ONCESI uzunluk on-kontrolu: bir satirin v1 (dar 200/50)
    anahtari eski sinirlari ASIYORSA (`_normalize_v1`in composition-exclusion uzatmasi —
    bkz. `test_KQ2_v1_lengthens_on_composition_exclusions`), downgrade ACIK RuntimeError'la
    durur — kolon DARALTILMAZ (hala genis 800/200), anahtar DEGISMEZ (hala v2), alembic_version
    KQ2'de kalir. U+0958 (DEVANAGARI QA+NUKTA) tek basina composition exclusion nedeniyle v1
    VE v2'de AYNI oranda (2x) uzar; 105 tekrari orijinal ad 105 <= 200 (v1 semasindaki 'name'
    kolonu) icinde kalirken v1 anahtari 210 > 200 (OLD_NAME_KEY_LEN) asar, v2 anahtari ise
    210 <= 800 (NEW_NAME_KEY_LEN) icinde kalir — yani satir KQ2'de sorunsuz yasar, yalniz
    downgrade'de tikanir.

    MUTASYON: bu on-kontrol kaldirilirsa (ya da ALTER COLUMN kontrolden ONCEYE alinirsa)
    downgrade ya PostgreSQL `22001` ile ya da BASARILI ama kirpilmis/tasan bir anahtarla
    biter — bu test HER IKI durumda da KIRMIZI olur."""
    mig = _load()
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", KQ2, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            kab_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO ev_disciplines (id, code, name, color, default_contractor_type) "
                "VALUES ($1, 'KAB', 'Kaba İnşaat', '#2563EB', 'own')",
                kab_id,
            )
            # 105 <= 200 ('name' kolonu, migration DEGISTIRMEDI) ama v1 anahtari 210 > 200
            # (OLD_NAME_KEY_LEN); v2 anahtari 210 <= 800 (NEW_NAME_KEY_LEN) — satir KQ2'de gecerli.
            overflow_name = chr(0x958) * 105  # U+0958 DEVANAGARI HARF QA
            assert len(overflow_name) <= 200
            assert len(mig._normalize_v1(overflow_name)) > mig.OLD_NAME_KEY_LEN  # noqa: SLF001
            assert len(mig._normalize_v2(overflow_name)) <= mig.NEW_NAME_KEY_LEN  # noqa: SLF001
            row_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO ev_catalog_items "
                "(id, discipline_id, name, uom, name_key, uom_key, standard_unit_mhr, "
                "default_contractor_type) VALUES ($1, $2, $3, 'ad', $4, $5, 1, 'own')",
                row_id,
                kab_id,
                overflow_name,
                mig._normalize_v2(overflow_name),  # noqa: SLF001 — KQ2'deyken anahtarlar v2'dir
                mig._normalize_v2("ad"),  # noqa: SLF001
            )
            before = await _keys(conn)
            before_lengths = await _column_lengths(conn)
        finally:
            await conn.close()
        assert before_lengths == {"name_key": 800, "uom_key": 200}

        out = _alembic_expect_failure("downgrade", PREVIOUS, database=database)
        assert "KATALOG-UQ-2" in out and "downgrade" in out
        assert str(row_id) in out
        assert "asiyor" in out or "sinirini asiyor" in out

        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == KQ2
            # Kolon DARALTILMADI (ALTER COLUMN hic calismadi — kontrol ONDEN durdu).
            assert await _column_lengths(conn) == {"name_key": 800, "uom_key": 200}
            # Anahtar DOKUNULMAMIS (hala v2).
            assert await _keys(conn) == before
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)


async def test_KQ2_migration_round_trip_widens_then_narrows_column() -> None:
    """(1a) Uc kolon uzunlugu dogrudan olculur: PREVIOUS'ta 200/50 → upgrade(KQ2) sonrasi
    800/200 → downgrade(PREVIOUS) sonrasi tekrar 200/50. `information_schema` ile BIREBIR."""
    database = await _create_scratch_database()
    try:
        _run_alembic("upgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _column_lengths(conn) == {"name_key": 200, "uom_key": 50}
        finally:
            await conn.close()

        _run_alembic("upgrade", KQ2, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _column_lengths(conn) == {"name_key": 800, "uom_key": 200}
        finally:
            await conn.close()

        _run_alembic("downgrade", PREVIOUS, database=database)
        conn = await asyncpg.connect(_asyncpg_dsn(database))
        try:
            assert await _column_lengths(conn) == {"name_key": 200, "uom_key": 50}
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(database)

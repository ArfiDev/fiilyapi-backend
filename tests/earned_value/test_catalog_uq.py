"""KATALOG-UQ — normalize katalog tekilligi DB DUZEYINDE (EV-BORC-5'in emniyet agi).

EV-BORC-5 tekilligi yalniz servis SELECT'iyle sinadi; DB UQ'su BIREBIR (harf duyarli)
kaldi. Servisi atlayan her yazar (yaris, ORM dogrudan yazma, eski konteyner) "Beton" ve
"BETON"u yan yana yazabilirdi. Artik `name_key`/`uom_key` kolonlarini UYGULAMA yazar
(`labels.normalize_label`, TEK kaynak) ve DB yalniz ESITLIGI zorlar:
`uq_ev_catalog_items_disc_name_key_uom_key`.

Neden ifade indeksi DEGIL (KATALOG-UQ raporu K1/K2): Postgres `lower()` DB'nin
LC_CTYPE'ina bagli ("C"de 1381 kod noktasi Python'dan farkli), libc'de Yunan final
sigma'yi bilmez ve glibc'nin Unicode surumu Python'unkinden farkli. Duz varchar
esitligi (deterministik harmanlama) ise bayt esitligidir: ctype'tan bagimsiz.

Buradaki ciftler K1 olcumunden gelir: `esit` ciftlerde Python ayni anahtari uretir →
DB REDDETMELI; `farkli` ciftlerde Python farkli anahtar uretir → DB KABUL ETMELI (DB,
Python'dan fazla normalize etmemeli).

KATALOG-UQ-2 (2026-09-27): `normalize_label` v2'ye genisledi (NFKC + `.casefold()` +
genis Cf/degiskec/CGJ silme). Uc vaka v1'de FARKLI iken v2'de AYNI oldu — `.casefold()`
`.lower()`den DAHA GENIS katlar (Yunan final sigma, ß→ss) ve NFKC ust simge rakamlari
(yalniz ²/³ degil, TUMU) sayiya coker — bu yuzden ESIT'e TASINDI:
* `yunan-sigma-final-degil` → `yunan-sigma-tum-formlar` (casefold final sigma AYRIMINI
  SILER: ΟΔΟΣ/οδος/οδοσ ucu de ayni anahtar);
* `eszett-ss` → `esitlendi` (casefold ß/ẞ → "ss", zaten `buyuk-eszett` ile ayni sonuc);
* `ust-simge-1` → `ust-simge-tum-rakamlar` (NFKC ¹→1, ⁴→4, … TUMU, yalniz ²/³ DEGIL).
Yeni v2-ozel vakalar: Roma rakami (Ⅻ→"xıı"), kesir (½ ↛ "1/2", KAPSAM DISI FARK) ve
"ISPARTA" ≠ "isparta" (ikisi de "ı" ile FARKLI kelimeye normalize olur, esitlenmez).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EarnedValueValidationError
from app.modules.catalog.models import EvDiscipline
from app.modules.earned_value.labels import NAME_KEY_MAX_LEN, UOM_KEY_MAX_LEN, normalize_label

UQ = "uq_ev_catalog_items_disc_name_key_uom_key"

#: (a, b, uom_a, uom_b) — Python'a gore AYNI anahtar.
ESIT = {
    "buyuk-kucuk": ("Beton döküm", "BETON DÖKÜM", "m³", "m³"),
    "bosluk": ("Beton döküm", "  beton \t  döküm ", "m³", "m³"),
    "ust-simge-birim": ("Beton döküm", "beton döküm", "m³", "M3"),
    "turkce-noktali-I": ("kireç sıva", "KİREÇ SIVA", "m²", "m2"),
    "turkce-noktasiz-I": ("ışık", "IŞIK", "ad", "AD"),
    "latin-Y-umlaut": ("ÿ", "Ÿ", "ad", "ad"),
    "latin-O-slash": ("Øre", "øre", "ad", "ad"),
    "yunan-final-sigma": ("ΟΔΟΣ", "οδος", "ad", "ad"),
    "kiril": ("БЕТОН", "бетон", "ad", "ad"),
    "buyuk-eszett": ("STRAẞE", "straße", "ad", "ad"),
    "nbsp": ("a b", "a b", "ad", "ad"),
    "ideografik-bosluk": ("a　b", "a b", "ad", "ad"),
    "u001c-bosluk-sayilir": ("a\u001cb", "a b", "ad", "ad"),
    "kelvin": ("K", "k", "ad", "ad"),
    # KATALOG-UQ madde 1: NFC normalizasyonu — "é" (U+00E9, NFC) ile "e"+U+0301 (NFD) ayni anahtar.
    "nfc-nfd-esit": ("é", "é", "ad", "ad"),
    # Sifir genislikli karakterler SILINIR (bosluga cevrilmez, iki harf bitisir), bosluk DEGIL.
    "zwsp-silinir": ("a​b", "ab", "ad", "ad"),
    "zwnj-silinir": ("Be‌ton", "Beton", "ad", "ad"),
    "zwj-silinir": ("Be‍ton", "Beton", "ad", "ad"),
    "bom-silinir": ("Be﻿ton", "Beton", "ad", "ad"),
    # KATALOG-UQ-2: casefold() final sigma ayrimini de siler (v1'de FARKLI idi).
    "yunan-sigma-tum-formlar": ("ΟΔΟΣ", "οδοσ", "ad", "ad"),
    "eszett-esitlendi": ("straße", "strasse", "ad", "ad"),
    # KATALOG-UQ-2: NFKC TUM ust simge rakamlarini coker, yalniz ²/³ DEGIL (v1'de FARKLI idi).
    "ust-simge-tum-rakamlar": ("m¹", "m1", "ad", "ad"),
    # KATALOG-UQ-2: NFKC Roma rakamini "XII"ye acar, sonra Turkce I→ı kurali uygulanir.
    "roma-rakami": ("Ⅻ", "XII", "ad", "ad"),
    # KATALOG-UQ-2: kompozisyon/kare isareti de NFKC ile ayni "m3"e coker.
    "kare-metre-kup-isareti": ("㎥", "m3", "ad", "ad"),
    "ticari-marka": ("™ ürün", "TM ürün", "ad", "ad"),
    "mikro-mu": ("µ değer", "μ değer", "ad", "ad"),
}

#: Python'a gore FARKLI anahtar — DB de ayri kabul etmeli.
FARKLI = {
    "birim-farkli": ("Beton döküm", "Beton döküm", "m³", "ton"),
    # KATALOG-UQ-2 bilincli yan etki: kesir egik cizgisi (U+2044) duz "/" ile ESLESMEZ.
    "kesir-esit-degil": ("½ ölçek", "1/2 ölçek", "ad", "ad"),
    # I harfi hala Turkce kuraliyla ayrisiyor: "ısparta" ile "isparta" FARKLI kalir.
    "isparta-isparta-farkli": ("ISPARTA", "isparta", "ad", "ad"),
    # Tire cesitleri KAPSAM DISI: en-dash NFKC ile ASCII tireye ESITLENMEZ.
    "tire-cesitleri-farkli": ("beton–döküm", "beton-döküm", "ad", "ad"),
}


def _violates(exc: IntegrityError) -> bool:
    """Dogru sebep: BU kisit (baska bir NOT NULL/FK/CHECK degil)."""
    return f'unique constraint "{UQ}"' in str(exc)


@pytest.mark.parametrize(("a", "b", "ua", "ub"), list(ESIT.values()), ids=list(ESIT))
async def test_KQ_db_rejects_python_equal_pair(
    seeded_db: AsyncSession, kab: EvDiscipline, katalog_fabrikasi, a, b, ua, ub
) -> None:
    assert (normalize_label(a), normalize_label(ua)) == (normalize_label(b), normalize_label(ub))
    await katalog_fabrikasi(kab, a, uom=ua)
    with pytest.raises(IntegrityError) as exc:
        await katalog_fabrikasi(kab, b, uom=ub)
    assert _violates(exc.value), exc.value


@pytest.mark.parametrize(("a", "b", "ua", "ub"), list(FARKLI.values()), ids=list(FARKLI))
async def test_KQ_db_accepts_python_different_pair(
    seeded_db: AsyncSession, kab: EvDiscipline, katalog_fabrikasi, a, b, ua, ub
) -> None:
    assert (normalize_label(a), normalize_label(ua)) != (normalize_label(b), normalize_label(ub))
    await katalog_fabrikasi(kab, a, uom=ua)
    await katalog_fabrikasi(kab, b, uom=ub)


async def test_KQ_same_key_in_other_discipline_is_allowed(
    seeded_db: AsyncSession, kab: EvDiscipline, disiplin_fabrikasi, katalog_fabrikasi
) -> None:
    duv = await disiplin_fabrikasi("DUV", "Duvar")
    await katalog_fabrikasi(kab, "Tuğla")
    await katalog_fabrikasi(duv, "TUĞLA")


async def test_KQ_orm_rename_into_variant_is_rejected_by_db(
    seeded_db: AsyncSession, kab: EvDiscipline, katalog_fabrikasi
) -> None:
    """Anahtar ad/birim YAZILDIGINDA yeniden turer: servisi atlayan ORM yeniden adlandirmasi da
    DB'de takilir (bayat anahtar olsaydi "Kalıp"in anahtari kalir, cift gecerdi)."""
    await katalog_fabrikasi(kab, "Beton döküm")
    kalip = await katalog_fabrikasi(kab, "Kalıp")
    kalip.name = "BETON  DÖKÜM"
    with pytest.raises(IntegrityError) as exc:
        await seeded_db.flush()
    assert _violates(exc.value), exc.value


async def test_KQ_uom_change_into_variant_is_rejected_by_db(
    seeded_db: AsyncSession, kab: EvDiscipline, katalog_fabrikasi
) -> None:
    await katalog_fabrikasi(kab, "Beton döküm", uom="m³")
    ton = await katalog_fabrikasi(kab, "Beton döküm", uom="ton")
    ton.uom = "M3"
    with pytest.raises(IntegrityError) as exc:
        await seeded_db.flush()
    assert _violates(exc.value), exc.value


async def test_KQ_stored_keys_are_python_normalize_byte_for_byte(
    seeded_db: AsyncSession, kab: EvDiscipline, katalog_fabrikasi
) -> None:
    """DB'deki anahtar, Python `normalize_label` ciktisinin KENDISIDIR (DB normalize etmez)."""
    names = sorted({v for pair in (*ESIT.values(), *FARKLI.values()) for v in pair[:2]})
    for i, name in enumerate(names):
        await katalog_fabrikasi(kab, name, uom=f"U{i} ³")
    rows = (
        await seeded_db.execute(
            text(
                "SELECT name, uom, name_key, uom_key FROM ev_catalog_items WHERE discipline_id = :d"
            ),
            {"d": kab.id},
        )
    ).all()
    assert len(rows) == len(names)
    for name, uom, name_key, uom_key in rows:
        assert (name_key, uom_key) == (normalize_label(name), normalize_label(uom)), name


def test_KQ_normalize_is_idempotent_for_all_pairs() -> None:
    """normalize(normalize(x)) == normalize(x) — ESIT/FARKLI ciftlerinde (kod noktasi
    genisligi `test_kq_catalog_uq_migration.py`da; burada senaryo duzeyinde)."""
    values = {v for pair in (*ESIT.values(), *FARKLI.values()) for v in pair[:2]}
    for value in values:
        once = normalize_label(value)
        assert normalize_label(once) == once, value


# ------------------------------------------------------------------ KATALOG-UQ-2 duzeltme turu


#: NFKC en genis bilinen tek-kod-noktasi acilimi (U+FDFA, ARAPCA LIGATUR SALLALLAHOU
#: ALEYHE VESSELLEM) — 18 karaktere acilir. Kolon-sinir testlerinde "ad/birim kolon
#: sinirinda kalsa da anahtar kolon sinirini asar" senaryosunu ucuza kurar.
_LONG_EXPANDER = "ﷺ"


def test_KQ2_expander_absolute_expansion_ratio_is_18() -> None:
    """Varsayimi dogrudan olcer: bu karakterin normalize genisligi TAM 18 — asagidaki
    testlerdeki tekrar sayilari (45/12/40/20) bu sabite dayanir."""
    assert len(normalize_label(_LONG_EXPANDER)) == 18


#: (4) Bilincli yan etkiler — MUTLAK deger testleri (opus curutmesi madde 4).
ABSOLUTE_VALUES = {
    "roma-rakami-xii": ("Ⅻ", "xıı"),
    "kesir-yarim": ("½", "1⁄2"),
    "ticari-marka": ("™", "tm"),
    "mikro-isareti": ("µ", "μ"),
    "kup-metre-isareti": ("㎥", "m3"),
    "tam-genislik-I": ("Ｉ", "ı"),
}


@pytest.mark.parametrize(
    ("raw", "expected"), list(ABSOLUTE_VALUES.values()), ids=list(ABSOLUTE_VALUES)
)
def test_KQ2_normalize_label_absolute_value(raw: str, expected: str) -> None:
    assert normalize_label(raw) == expected


async def test_KQ2_name_key_overflow_raises_clear_turkish_error(
    seeded_db: AsyncSession, kab: EvDiscipline, katalog_fabrikasi
) -> None:
    """(1b) Ad kolon sinirinda (200) kalsa da turetilen `name_key` kolon sinirini (800)
    asarsa ACIK Turkce hata beklenir — sessiz DB `22001`/yaniltici jenerik 422 DEGIL.

    MUTASYON: kolon genisletme (models.py/migration) geri alinirsa bu deger DB'ye hic
    ULASMAZ (app katmani zaten durdurur) — DB seviyesindeki etkiyi
    `test_KQ2_name_key_between_old_and_new_limit_fits_widened_column` olcer."""
    name = _LONG_EXPANDER * 45  # 45 <= 200 (name kolonu); normalize 810 > 800 (name_key)
    assert len(name) <= 200
    assert len(normalize_label(name)) > NAME_KEY_MAX_LEN
    with pytest.raises(EarnedValueValidationError) as exc:
        await katalog_fabrikasi(kab, name)
    assert "Ad" in str(exc.value)
    assert str(NAME_KEY_MAX_LEN) in str(exc.value)


async def test_KQ2_uom_key_overflow_raises_clear_turkish_error(
    seeded_db: AsyncSession, kab: EvDiscipline, katalog_fabrikasi
) -> None:
    """(1b) Birim icin ayni kontrol, ayri alan etiketiyle."""
    uom = _LONG_EXPANDER * 12  # 12 <= 50 (uom kolonu); normalize 216 > 200 (uom_key)
    assert len(uom) <= 50
    assert len(normalize_label(uom)) > UOM_KEY_MAX_LEN
    with pytest.raises(EarnedValueValidationError) as exc:
        await katalog_fabrikasi(kab, "Normal ad", uom=uom)
    assert "Birim" in str(exc.value)
    assert str(UOM_KEY_MAX_LEN) in str(exc.value)


async def test_KQ2_name_key_between_old_and_new_limit_fits_widened_column(
    seeded_db: AsyncSession, kab: EvDiscipline, katalog_fabrikasi
) -> None:
    """(1a) `(200, 800]` araligindaki bir anahtar: ESKI (200) kolonda TASARDI, YENI (800)
    kolonda SIGAR. `seeded_db.flush()` DB'ye gercekten yazar — kolon genisletme MUTASYONLA
    geri alinirsa (ya da app sinirlari `800`den DB'den BAGIMSIZ kalirsa) bu satir gercek
    DB `22001`/500 ile KIRMIZI olur; yalniz app-katmani kontrolu (yukaridaki testler)
    BUNU YAKALAMAZ."""
    name = _LONG_EXPANDER * 20  # 20*18=360: eski (200) kolonu asar, yeni (800) kolona sigar
    key = normalize_label(name)
    assert 200 < len(key) <= NAME_KEY_MAX_LEN
    item = await katalog_fabrikasi(kab, name)
    assert item.name_key == key

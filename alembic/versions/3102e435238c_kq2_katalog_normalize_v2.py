"""KATALOG-UQ-2 — `ev_catalog_items` normalize anahtarlari v2 kuralla YENIDEN HESAPLANIR

KATALOG-UQ (`aab10fbf5471`) `name_key`/`uom_key` kolonlarini v1 kuraliyla (NFC + Turkce
İ/I + `³`/`²` elle cevirisi + sabit sifir-genislik listesi) doldurmustu. `labels.py`
docstring'inin kendi uyarisi gerçekleşti: "`normalize_label` ileride DEGISIRSE mevcut
anahtarlar eski kuralla kalir → o degisiklik anahtarlari yeniden hesaplayan YENI bir
migration getirmelidir." KATALOG-UQ-2 kurali GENISLETTI (v2 — NFKC + `.casefold()` +
tum Cf/degiskeç/CGJ silme + son NFKC; gerekce `app/modules/earned_value/labels.py`
docstring'inde). Bu migration:

1. mevcut `name`/`uom` satirlarini Python'daki DONDURULMUS v2 kopyasiyla yeniden hesaplar,
2. yeni anahtarlarla normalize cift olusuyorsa (v1'de ayrik iki satir v2'de birlesebilir —
   ör. "m¹" ile "m1" artik ayni anahtar) ACIK HATAYLA durur, HICBIR SEY DEGISMEZ,
3. cift yoksa `name_key`/`uom_key`i YERINDE UPDATE eder (yalnizca degisenler),
4. UQ SEMASI DEGISMEZ (`uq_ev_catalog_items_disc_name_key_uom_key` zaten vardi; bu
   migration yalniz anahtar DEGERLERINI degistirir).

## DAGITIM NOTU
Canli katalogda 2026-09-26 sayimiyla 0 (sifir) satir vardi (`scripts/olc/canli-sayim.sh`
ile olculdu) — bu migration'in canlida cift/bayat anahtar riski YOKTU o tarihte. Yine de
kural genel oldugu icin (bos tablo disinda) upgrade/downgrade cift kontrolu KOSULSUZ
calisir.

## v1→v2 "COKLUK" VARSAYIMI ÖLÇÜLDÜ VE ÇÜRÜDÜ
Ilk beklenti "v2 anahtarlari v1'den daha kaba, yani v2'de AYRI olan her cift v1'de de
AYRIDIR" idi (yani v1-esit ⟹ v2-esit, downgrade'de YENI cift TURMEZ sanildi). Butun
Unicode kod noktalari TEK BASINA taranarak olculdu (bkz. asagidaki script) ve bu YANLIS
cikti: 13 kod noktasinda v1 ESIT ama v2 FARKLI (ör. `Ĳ` (U+0132) ile `ĳ` (U+0133) v1'de
ikisi de `"ĳ"`e coker — Python `.lower()` ligatur harf ceviriyi bilir — ama v2'de NFKC
ONCE `Ĳ`yi `"IJ"`ye acar (Turkce I→ı burada devreye girer: `"ıj"`), `ĳ`yi ise `"ij"`ye
acar (kucuk harf compat decomposition, I yok): `"ıj"` ≠ `"ij"`. Benzer 12 vaka daha:
Roman rakami kucuk harfleri (ⅰ,ⅱ,ⅲ,ⅳ,ⅵ,ⅶ,ⅷ,ⅸ,ⅺ,ⅻ — U+2170..217B), dairesel `ⓘ`
(U+24D8), tam-genislik `ｉ` (U+FF49): v1'de bunlar DEGISMEDEN kalirdi (Turkce İ/I
`replace`'i yalniz duz "I"/"İ" harflerine bakar, bu simgeleri GORMEZ; `.lower()` de
onlari ZATEN kucuk/degismez birakir → v1 cikti bu karakterlerin KENDISI ya da NFC'nin
verdigi haliyle kalir), v2'de ise NFKC ONCE onlari "I"/"i" iceren ASCII dizilere acar,
bu da Turkce I→ı kuralina TABI olur.

SONUC: iddia TERS yonde de dogru degil (v2-esit ⟹ v1-esit de garanti degil, ayni
mekanizma), bu yuzden DOWNGRADE de UPGRADE ile AYNI cift-durdurma korumasina sahiptir
(asagida `downgrade()`). Olcen script (calistirilan, depoya YAZILMADI — burada
tekrarlanabilir):

    for cp in range(1, 0x110000):
        if 0xD800 <= cp <= 0xDFFF: continue
        c = chr(cp)
        k1, k2 = _normalize_v1(c), _normalize_v2(c)
        # k1 ayni oldugu halde k2 farkliysa -> yukaridaki 13 vaka

Katalog kelime hazinesinde (Turkce/Ingilizce insaat terimleri) bu kod noktalarinin
pratik ihtimali sifira yakindir, ama migration FORMEL kod-noktasi kanitina degil GERCEK
veriye bakar — kontrolun kendisi ucuzdur (tek SELECT + Python grouplama), bu yuzden
gerceklesme ihtimali dusuk olsa da SILINMEDI.

## Dondurulmus v2 kopya, dondurulmus v1 kopya
Depo kanonu (KATALOG-UQ ile ayni): migration `app`i import ETMEZ. `_normalize_v2` bu
dosyada 2026-09-27 tarihli `app.modules.earned_value.labels.normalize_label` (v2) ile
BIREBIR (bkz. `tests/earned_value/test_kq2_catalog_normalize_v2_migration.py`).
`_normalize_v1` `aab10fbf5471`teki `_normalize`nin BIREBIR kopyasidir (downgrade'in
anahtarlari geri hesaplamasi ve cift kontrolu icin).

## Betik sozlesmesi (KATALOG-UQ-2 duzeltme turu)
`katalog-tekillik-grupla.py` (kok, SALT OKU) eski `aab10fbf5471` sozlesmesini
(`_normalize` + `_duplicate_groups(raw)`) bekler ve bu dosyada YOKTUR — `--migration` ile
bu dosyaya isaret edilirse TypeError verir, varsayilan glob da hala v1 migration'ini
secer (dagitim oncesi sahte "TEMIZ" riski). Bu migration bu yuzden ACIK, KARARLI bir
sozlesme sunar (`NORMALIZE`, `duplicate_groups(rows)` — asagida) ve betigin DUZELTILMIS
kopyasi `katalog-tekillik-grupla.v2.py`dir (scratchpad'de, kok SALT OKU birakildi): o
kopya alembic revizyon zincirini head'den geriye yururek bu sozlesmeyi taniyan ILK
revizyonu bulur, `--migration`/eski sozlesme ile GERIYE UYUMLUDUR.

## Anahtar kolon genisletme (KATALOG-UQ-2 duzeltme turu, opus curutmesi)
`labels.py`deki "normalize metni UZATMAZ" varsayimi YANLISTI: NFKC 1268 kod noktasinda
metni UZATIR (en fazla 18 kat, `…`→`"..."` gibi). `name` (200) sinirinda kalan bir ad
`name_key`i (eskiden 200) asabilirdi → DB `22001` (metin tasmasi), yaniltici 422 (hangi
alanin tastigi belli degil). Bu migration AYNI ALTER'DA:
1. `name_key`i 200→800, `uom_key`i 50→200 GENISLETIR (backfill'den ONCE — backfill genis
   kolona yazar),
2. backfill ONCESI, hesaplanan v2 anahtarlardan HERHANGI biri YENI sinirlari (800/200)
   da asiyorsa ACIK HATAYLA durur (satir listesiyle) — 800/200 pratikte cok genis olsa
   da FORMEL garanti degil, kontrol KOSULSUZ kalir,
3. downgrade AYNI deseni TERS sirada uygular: v1 anahtarlar (daha kisa; DEGISTIRME'YE
   bkz. asagidaki not) hesaplanir + sinir kontrolu, backfill YAPILIR, SONRA kolonlar
   200/50'ye DARALTILIR (daraltma backfill'den SONRA — daralan kolon o an ZATEN kisa
   degerler tasimalidir).

`app.modules.earned_value.models.EvCatalogItem._sync_key` (uygulama tarafi) ayni sinirlari
`labels.NAME_KEY_MAX_LEN`/`UOM_KEY_MAX_LEN` ile PAYLASIR ve asan degeri kolona ULASMADAN
ACIK Turkce 422 ile reddeder — bu migration'daki kontrol yalniz CANLI VERIDEKI (dagitim
oncesi zaten var olan) satirlar icindir.

🔴 "v1 metni UZATMAZ" varsayimi YANLISTI — OLCULDU VE CURUTULDU (KATALOG-UQ-2 duzeltme
turu, opus curutmesi): `_normalize_v1`in TEK BASINA girdi olarak `len(_normalize_v1(chr(cp)))
> 1` verdigi 85 kod noktasi VAR (tum Unicode kod noktalari tek tek taranarak olculdu,
vekiller haric; ör. U+0344 (BIRLESIK YUNAN TONOS), U+0958–095F (DEVANAGARI NUKTA harfleri),
U+09DC/09DD, U+0A33/0A36, U+0F43…). Sebep NFC'nin "composition exclusion" listesi: bu kod
noktalari NFD'de iki parcaya AYRISIR ama NFC bunlari GERI BIRLESTIRMEZ (composition exclusion
tablosunda oldugu icin) — `_normalize_v1`in ilk VE son adimi da NFC oldugundan tek bir girdi
kod noktasi cikista 2 kod noktasina UZAR. Bu yuzden downgrade'deki kontrol (madde 3) SAVUNMA
IKINCI KATMANI DEGIL, GEREKLI bir bekcidir — pratikte 200/50 sinirini gercekten asan bir satir
son derece nadir olsa da (85 kod noktasi katalog kelime hazinesinde ender), mekanizma GERCEK
ve OLCULMUS: bkz. `tests/earned_value/test_kq2_catalog_normalize_v2_migration.py::
test_KQ2_v1_lengthens_on_composition_exclusions`.

## Kilit / cift durdurma / dagitim sirasi
KATALOG-UQ (`aab10fbf5471`) ile AYNI: `LOCK TABLE … SHARE ROW EXCLUSIVE`, cift varsa
`transaction_per_migration=True` sayesinde HICBIR SEY degismeden `RuntimeError`,
Dockerfile `alembic upgrade head && uvicorn` oldugu icin patlarsa konteyner ACILMAZ.

## 🔴 unicodedata SURUM RISKI
`_normalize_v2` yerel Python 3.12.13 (unicodedata.unidata_version == "15.0.0") ile
hesaplanmistir. Dockerfile `python:3.12-slim` ve CI `.github/workflows/ci.yml`
`python-version: "3.12"` kullanir — ikisi de CPython 3.12 hattinda, dolayisiyla ayni
Unicode veritabani surumunu (15.0.0) tasir. Surum sabitleme bekcisi:
`tests/earned_value/test_kq2_catalog_normalize_v2_migration.py::
test_KQ2_unidata_version_is_pinned_15_0_0`.

Revision ID: 3102e435238c
Revises: b5858dd66531
Create Date: 2026-09-27

"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "3102e435238c"
down_revision: str | Sequence[str] | None = "b5858dd66531"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "ev_catalog_items"
NEW_UQ = "uq_ev_catalog_items_disc_name_key_uom_key"

#: Kolon sinirlari — `app.modules.earned_value.labels.NAME_KEY_MAX_LEN`/`UOM_KEY_MAX_LEN`
#: ve `models.py`deki kolon tanimlariyla AYNI olmali (KATALOG-UQ-2 duzeltme turu).
OLD_NAME_KEY_LEN = 200
OLD_UOM_KEY_LEN = 50
NEW_NAME_KEY_LEN = 800
NEW_UOM_KEY_LEN = 200

# --------------------------------------------------------------------------- v1 (DONDURULMUS)
#: Sifir genislikli karakterler: bosluk DEGIL, SILINIR (iki harf arasindaysa bitisir).
_V1_ZERO_WIDTH = str.maketrans("", "", "​‌‍﻿")


def _normalize_v1(text: str) -> str:
    """DONDURULMUS kopya: `aab10fbf5471`teki `_normalize` (== KATALOG-UQ v1). DEGISTIRME.
    Yalniz downgrade'in anahtarlari geri hesaplamasi ve v1 cift kontrolu icin."""
    s = unicodedata.normalize("NFC", text)
    s = s.replace("İ", "i").replace("I", "ı").lower()
    s = s.replace("³", "3").replace("²", "2")
    s = s.translate(_V1_ZERO_WIDTH)
    s = re.sub(r"\s+", " ", s).strip()
    return unicodedata.normalize("NFC", s)


# --------------------------------------------------------------------------- v2 (DONDURULMUS)
_V2_VARIATION_SELECTOR_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))
_V2_COMBINING_GRAPHEME_JOINER = "͏"


def _v2_is_invisible(c: str) -> bool:
    if unicodedata.category(c) == "Cf":
        return True
    if c == _V2_COMBINING_GRAPHEME_JOINER:
        return True
    cp = ord(c)
    return any(lo <= cp <= hi for lo, hi in _V2_VARIATION_SELECTOR_RANGES)


def _normalize_v2(text: str) -> str:
    """DONDURULMUS kopya: 2026-09-27'deki `app.modules.earned_value.labels.normalize_label`
    (== KATALOG-UQ-2 v2). DEGISTIRME — esitligi
    `tests/earned_value/test_kq2_catalog_normalize_v2_migration.py` tum Unicode kod
    noktalariyla sinar."""
    s = unicodedata.normalize("NFKC", text)
    s = s.replace("İ", "i").replace("I", "ı")
    s = s.casefold()
    s = "".join(c for c in s if not _v2_is_invisible(c))
    s = re.sub(r"\s+", " ", s).strip()
    return unicodedata.normalize("NFKC", s)


# --------------------------------------------------------------------------- ortak yardimcilar


def _duplicate_groups(
    rows: Iterable[Any], normalize: Any
) -> list[tuple[str, str, str, list[tuple[str, ...]]]]:
    """(id, discipline_id, discipline_code, name, uom) satirlarindan `normalize` ciftleri.

    Donus: [(disiplin_kodu, name_key, uom_key, [(id, ad, birim), …])], adet azalan sirada.
    `aab10fbf5471`teki ayni-adli fonksiyonla AYNI SEKIL; `normalize` parametreli hale
    getirildi ki upgrade (v2) VE downgrade (v1) ayni kontrolu paylassin.
    """
    groups: dict[tuple[str, str, str], list[tuple[str, ...]]] = {}
    codes: dict[str, str] = {}
    for row_id, discipline_id, code, name, uom in rows:
        key = (str(discipline_id), normalize(name), normalize(uom))
        codes[str(discipline_id)] = code
        groups.setdefault(key, []).append((str(row_id), name, uom))
    return sorted(
        (
            (codes[disc], name_key, uom_key, sorted(members, key=lambda m: (m[1], m[0])))
            for (disc, name_key, uom_key), members in groups.items()
            if len(members) > 1
        ),
        key=lambda g: (-len(g[3]), g[0], g[1], g[2]),
    )


def _duplicate_message(
    groups: list[tuple[str, str, str, list[tuple[str, ...]]]], *, direction: str
) -> str:
    lines = [
        f"KATALOG-UQ-2 ({direction}): ev_catalog_items'ta {len(groups)} normalize cift "
        f"grubu var; {NEW_UQ} anahtarlari YENIDEN HESAPLANAMAZ. Hicbir sey degismedi. Her "
        "gruptan birini PATCH ile yeniden adlandirip yeniden dagitin "
        "(bkz. katalog-tekillik-grupla.py):"
    ]
    for code, name_key, uom_key, members in groups:
        lines.append(f"  [{code}] ({name_key!r}, {uom_key!r}) x{len(members)}")
        lines.extend(f"      {row_id} | {name!r} | {uom!r}" for row_id, name, uom in members)
    return "\n".join(lines)


#: Betik sozlesmesi (KATALOG-UQ-2 duzeltme turu) — `katalog-tekillik-grupla.v2.py` bu
#: ikisini ACIK tanimlarla yukler (aab10'un `_normalize`/`_duplicate_groups(raw)` sozlesmesi
#: bu revizyonda YOK; geriye uyumluluk betikte cozulur, burada DEGIL). DEGISTIRME —
#: yalniz IMZALARI, degil de ADLARI kararlidir.
NORMALIZE = _normalize_v2


def duplicate_groups(
    rows: Iterable[Any],
) -> list[tuple[str, str, str, list[tuple[str, ...]]]]:
    """`katalog-tekillik-grupla.v2.py` icin sabit imza: `_duplicate_groups(rows, NORMALIZE)`."""
    return _duplicate_groups(rows, NORMALIZE)


def _overflowing_rows(
    rows: Iterable[Any], normalize: Any, *, name_max: int, uom_max: int
) -> list[tuple[str, str, str, str, int]]:
    """(id, ad, birim, turetilen_asan_anahtar, sinir) — `normalize(name)`/`normalize(uom)`
    hedef kolon sinirini ASAN satirlar. KATALOG-UQ-2 duzeltme turu: NFKC normalizasyonu
    metni UZATABILIR (1268 kod noktasi, en fazla 18 kat) — "normalize metni UZATMAZ"
    varsayimi YANLISTI; bu kontrol olmadan ALTER COLUMN/UPDATE bir DB `22001`ye duserdi."""
    overflow: list[tuple[str, str, str, str, int]] = []
    for r in rows:
        nk, uk = normalize(r.name), normalize(r.uom)
        if len(nk) > name_max:
            overflow.append((str(r.id), r.name, "name_key", nk, name_max))
        if len(uk) > uom_max:
            overflow.append((str(r.id), r.uom, "uom_key", uk, uom_max))
    return overflow


def _overflow_message(overflow: list[tuple[str, str, str, str, int]], *, direction: str) -> str:
    lines = [
        f"KATALOG-UQ-2 ({direction}): {len(overflow)} satirda turetilen anahtar kolon "
        "sinirini asiyor; kolon degistirilmedi, hicbir sey guncellenmedi. Once ilgili "
        "ad/birimi kisaltin, sonra migration'i tekrar calistirin:"
    ]
    for row_id, raw, column, key, max_len in overflow:
        lines.append(
            f"  {row_id} | {column}: {len(key)} > {max_len} | ham={raw!r} | anahtar={key!r}"
        )
    return "\n".join(lines)


def _select_rows(bind: Any) -> Sequence[Any]:
    """(id, discipline_id, code, name, uom) — `_duplicate_groups` bu sirayi bekler."""
    return bind.execute(
        sa.text(
            "SELECT c.id, c.discipline_id, d.code, c.name, c.uom "
            f"FROM {TABLE} c JOIN ev_disciplines d ON d.id = c.discipline_id"
        )
    ).all()


def _select_current_keys(bind: Any) -> dict[Any, tuple[str, str]]:
    return {
        r.id: (r.name_key, r.uom_key)
        for r in bind.execute(sa.text(f"SELECT id, name_key, uom_key FROM {TABLE}")).all()
    }


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(f"LOCK TABLE {TABLE} IN SHARE ROW EXCLUSIVE MODE"))
    rows = _select_rows(bind)
    groups = _duplicate_groups(rows, _normalize_v2)
    if groups:
        raise RuntimeError(_duplicate_message(groups, direction="upgrade — v1→v2"))

    # KATALOG-UQ-2 duzeltme turu: kolonu GENISLETMEDEN once, genisletilmis sinirlarin
    # (800/200) bile yetmeyecegi satir var mi kontrol et — ACIK RuntimeError, hicbir
    # sey degismez (ALTER COLUMN/UPDATE hic calismaz).
    overflow = _overflowing_rows(
        rows, _normalize_v2, name_max=NEW_NAME_KEY_LEN, uom_max=NEW_UOM_KEY_LEN
    )
    if overflow:
        raise RuntimeError(_overflow_message(overflow, direction="upgrade — v1→v2"))

    op.alter_column(TABLE, "name_key", type_=sa.String(length=NEW_NAME_KEY_LEN))
    op.alter_column(TABLE, "uom_key", type_=sa.String(length=NEW_UOM_KEY_LEN))

    current = _select_current_keys(bind)
    updates = [
        {"id": r.id, "nk": nk, "uk": uk}
        for r in rows
        for nk, uk in [(_normalize_v2(r.name), _normalize_v2(r.uom))]
        if current[r.id] != (nk, uk)
    ]
    if updates:
        bind.execute(
            sa.text(f"UPDATE {TABLE} SET name_key = :nk, uom_key = :uk WHERE id = :id"),
            updates,
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(f"LOCK TABLE {TABLE} IN SHARE ROW EXCLUSIVE MODE"))
    rows = _select_rows(bind)
    # KATALOG-UQ-2 v1→v2 "cokluk" varsayimi olculup CURUTULDU (yukaridaki docstring):
    # v2-ayri iki satir v1'e donuste ESITLENEBILIR. Bu yuzden downgrade de upgrade ile
    # AYNI cift-durdurma korumasina sahip olmalidir.
    groups = _duplicate_groups(rows, _normalize_v1)
    if groups:
        raise RuntimeError(_duplicate_message(groups, direction="downgrade — v2→v1"))

    # KATALOG-UQ-2 duzeltme turu: v1 anahtarlarin eski (dar) sinirlari (200/50) asmayacagi
    # varsayimi OLCULDU VE CURUTULDU — NFC composition exclusion nedeniyle 85 kod noktasinda
    # `_normalize_v1` metni UZATIR (bkz. migration docstring, `test_KQ2_v1_lengthens_on_
    # composition_exclusions`). Bu yuzden asagidaki kontrol savunma ikinci katmani DEGIL,
    # GEREKLI bir bekcidir; daraltmadan ONCE KOSULSUZ calisir.
    overflow = _overflowing_rows(
        rows, _normalize_v1, name_max=OLD_NAME_KEY_LEN, uom_max=OLD_UOM_KEY_LEN
    )
    if overflow:
        raise RuntimeError(_overflow_message(overflow, direction="downgrade — v2→v1"))

    current = _select_current_keys(bind)
    updates = [
        {"id": r.id, "nk": nk, "uk": uk}
        for r in rows
        for nk, uk in [(_normalize_v1(r.name), _normalize_v1(r.uom))]
        if current[r.id] != (nk, uk)
    ]
    if updates:
        bind.execute(
            sa.text(f"UPDATE {TABLE} SET name_key = :nk, uom_key = :uk WHERE id = :id"),
            updates,
        )

    # Backfill (kisa v1 degerler) TAMAMLANDIKTAN SONRA kolonu daralt — narrow ALTER
    # o an kolondaki her deger yeni sinira uyuyor mu diye bakar; sirali oldugu icin
    # yukaridaki UPDATE ile bu ALTER arasinda kolon HER ZAMAN gecerli genislikte kalir.
    op.alter_column(TABLE, "name_key", type_=sa.String(length=OLD_NAME_KEY_LEN))
    op.alter_column(TABLE, "uom_key", type_=sa.String(length=OLD_UOM_KEY_LEN))

"""Ad/birim NORMALIZASYONU — katalog eslesmesinin VE katalog tekilliginin TEK kurali.

BLF-B1.1: `app/modules/earned_value/labels.py`den CEKIRDEGE tasindi (bolum tipi tekilligi de
ayni kurali kullanir; `sites` planlamayi import edemez). EV modulu buradan yeniden ihrac eder.

EV-BORC-5 (cürütme bulgusu): tekillik BIREBIR (harf duyarli) karsilastirmayla sinaniyor,
oneri eslesmesi ise normalize ediyordu → "Beton" ve "BETON" ikisi de kaydedilebilir, sonra
oneri kalemi BELIRSIZ sayardi. Artik ikisi de bu fonksiyonu kullanir.

KATALOG-UQ madde 1 (NFC + sifir genislikli karakterler): kural anahtar kolonuna (name_key/
uom_key) DOGARKEN eklendi — sonradan eklemek anahtarlari yeniden hesaplayan bir migration
gerektirirdi ("normalize_label ileride DEGISIRSE mevcut anahtarlar eski kuralla kalir").

KATALOG-UQ-2 (2026-09-27): kural GENISLETILDI (v2). v1'den farklari:
* NFC → NFKC: uyumluluk ayristirmasi eklendi. `³`/`²` icin elle `replace` KALDIRILDI —
  NFKC zaten ³→3, ²→2 yapar (ve genel olarak TUM ust/alt simge rakamlarini, Roma
  rakamlarini, genislik varyantlarini vb. kapsar). Ust simge `²`/`³` disinda kalan (ör.
  `⁴`, `¹`) rakamlar da artik sayiya coker — v1'de yalniz ikisi ozeldi.
* Gorunmez karakter silme GENISLETILDI: yalniz sabit ZW listesi (200B/C/D, FEFF) degil,
  `unicodedata.category(c) == "Cf"` (Format) olan TUM kod noktalari + varyasyon secicileri
  (U+FE00–U+FE0F, U+E0100–U+E01EF) + Birlesik Grafem Birlestirici (U+034F, CGJ). Eski ZW
  listesi Cf kategorisinin ALT KUMESIDIR (200B/C/D/FEFF hepsi Cf) — ayri sabit KALKTI.
* SON adimda tekrar NFKC (v1'de NFC'ydi): idempotenslik icin — adim 1 NFKC'ye
  yukseldigi icin son adim da NFKC olmali (aksi halde normalize(normalize(x)) bazi kod
  noktalarinda x'ten farkli cikabilir; testlerle tum kod noktasi taranir).

Bu genisletme mevcut `ev_catalog_items.name_key`/`uom_key` kolonlarini eskitir — onlari
YENIDEN HESAPLAYAN migration `3102e435238c` revizyonundadir (KATALOG-UQ
madde 1'deki uyari burada gerceklesti).

KATALOG-UQ-2 duzeltme turu (2026-09-27, opus curutmesi): "normalize metni UZATMAZ"
varsayimi bozuktu — NFKC 1268 kod noktasinda metni UZATIR (en fazla U+FDFA, 18 kat).
`name_key`/`uom_key` kolonlari bu yuzden GENISLETILDI (name_key 200→800, uom_key
50→200, ayni migrationda) VE bu modul (`normalize_label` cagirani `EvCatalogItem.
_sync_key`) sonuc kolon sinirini asarsa ACIK Turkce hatayla durur (bkz. `models.py`
`_sync_key`) — sessiz DB `22001` kesimi/yaniltici 422 yerine.

## Sira (olculdu, gerekce asagida) — v2
1. `unicodedata.normalize("NFKC", text)` ONCE:
   a) NFC alt-adimi: NFD "I"+U+0307 (birlesik nokta) → NFC "İ" (U+0130) → sonraki adim
      bunu "i" yapar. Bunu İ/I replace'inden SONRAYA koysaydik, ayrisik "I" tabani ile
      ayrisik birlesik nokta (U+0307) BIRLESMEDEN "ı" + U+0307 kalirdi (yanlis, "i" DEGIL).
   b) Uyumluluk alt-adimi: ust/alt simge rakamlari, Roma rakamlari, tam-genislik Latin
      harfler, ligatur/isaretler vb. "sıradan" karsiliklarina coker (³→3, Ⅻ→XII, Ｂ→B, …).
      Bu adim İ/I replace'inden ONCE gelmeli: bazi coklenmis kod noktalari (ör. Roma
      rakami Ⅰ) NFKC'den SONRA duz "I" harfine donusur ve Turkce kuralina tabi olmalidir.
2. Turkce İ/I elle cevirisi — NFKC'den SONRA, casefold'dan ONCE: `.casefold()` Turkce
   ayrimini (İ→i, I→ı) BILMEZ ("İ".casefold() == "i̇" — i + birlesik nokta, "i" DEGIL).
   Once I/İ elle cevrilir, boylece Turkce bir ad kendi kucuk harfli yaziminla eslesir.
3. `.casefold()` — `.lower()` YERINE: Unicode'un tanimladigi TAM katlama (ör. bazi
   Latin/Kiril/Ermeni harflerinde `.lower()`den daha genis esitleme; ß→ss).
4. Gorunmez karakter SILME (bosluga cevrilmez, iki harf bitisir): `unicodedata.category(c)
   == "Cf"` olan TUM kod noktalari (ZWSP/ZWNJ/ZWJ/BOM dahil, alt kume) + U+034F (CGJ,
   kategorisi Mn ama gorunmez birlestirici) + degiskec secicileri U+FE00–U+FE0F ve
   U+E0100–U+E01EF (Cf DEGIL, ek duzlem — emoji/CJK varyant secimi, katalog metninde
   anlamsiz gurultudur).
5. Bosluk daraltma + kirpma.
6. SON NFKC: `.casefold()` ve adim 4'un silmesi bazi kod noktalarinda NFKC-disi cikti
   verebilir; son adimda tekrar NFKC'ye sikistirmak `normalize(normalize(x)) ==
   normalize(x)` idempotensligini garanti eder. 🔴 Bu gereklilik TEK kod noktasi
   taramasiyla YAKALANMAZ — silme (adim 4) iki AYRI orijinal kod noktasini bitistirip
   NFKC-disi YENI bir komsuluk yaratabilir (ör. "e" + ZWSP + birlesik ince vurgu →
   silme sonrasi "e"+U+0301, "é"nin AYRISIK hali); bunu yakalayan test COK KARAKTERLI
   girdi kurar (bkz. `test_KQ2_idempotency_needs_final_nfkc_across_deleted_invisible_
   boundary`), tek-kod-noktasi taramasi DEGIL.

## Bilincli yan etkiler (v2)
* `Ⅻ` (Roma rakami U+216B) → `"xıı"` — NFKC once `"XII"` yapar, sonra Turkce I→ı kurali
  uygulanir; klavyeden yazilan `"XII"` ile TUTARLI sonuc.
* `½` (U+00BD) → `"1⁄2"` (kesir egik cizgisi U+2044) — ASCII `"1/2"` (U+002F) ile
  EŞLEŞMEZ. NFKC kesri sayi+kesir-egikcizgisi+sayiya acar, duz bolu isaretine DEGIL;
  bilincli — istenirse ayri bir normalizasyon katmani (tire/tirnak/kesir esitleme)
  KAPSAM DISI birakildi.
* `™` (U+2122) → `"tm"`, `µ` (mikro isareti, U+00B5) → `"μ"` (Yunanca kucuk mu, U+03BC) —
  NFKC ayristirmasi.
* Tire ve tirnak cesitleri (ör. en-dash/em-dash, duz/egri tirnak) KAPSAM DISI: NFKC bunlari
  BIRLESTIRMEZ, bu fonksiyon da elle esitlemez.

🔴 unicodedata SURUM RISKI: bu fonksiyon `unicodedata.category`/`normalize`/`.casefold()`
kullanir; bunlarin ciktisi Python'un derlendigi Unicode veritabani surumune baglidir
(bkz. `unicodedata.unidata_version`). Migration'in DONDURULMUS kopyasi ve bu modul
AYNI Python surumuyle (3.12, Unicode 15.0.0) hesaplanmistir — bkz.
`tests/earned_value/test_kq2_catalog_normalize_v2_migration.py::
test_KQ2_unidata_version_is_pinned_15_0_0`. Surum degisirse o test KIRMIZI olmalidir.
"""

from __future__ import annotations

import re
import unicodedata

#: Degiskec seciciler (Cf DISI): metinde/katalogda anlamsiz gorsel-varyant gurultusu.
_VARIATION_SELECTOR_RANGES = (
    (0xFE00, 0xFE0F),
    (0xE0100, 0xE01EF),
)

#: Birlesik Grafem Birlestirici — gorunmez, harfleri "yapiskan" kilar; kategorisi Mn'dir
#: (Cf DEGIL), bu yuzden ayri eklenir.
_COMBINING_GRAPHEME_JOINER = "͏"


def _is_invisible(c: str) -> bool:
    if unicodedata.category(c) == "Cf":
        return True
    if c == _COMBINING_GRAPHEME_JOINER:
        return True
    cp = ord(c)
    return any(lo <= cp <= hi for lo, hi in _VARIATION_SELECTOR_RANGES)


#: `ev_catalog_items.name_key`/`uom_key` kolon sinirlari (KATALOG-UQ-2 duzeltme turu) —
#: `models.py`deki kolon tanimlariyla VE `3102e435238c` migration'in ALTER COLUMN
#: genisligiyle AYNI olmali (uc yer, TEK sayi kaynagi burada).
NAME_KEY_MAX_LEN = 800
UOM_KEY_MAX_LEN = 200


def normalize_label(text: str) -> str:
    """Ad/birim karsilastirmasi (v2): NFKC + Turkce harf duzeltmesi + casefold + gorunmez
    karakter silme + bosluk + son NFKC. Sira BAGLAYICI, gerekce modul docstring'inde.

    🔴 `"İ".casefold()` "i̇" (i + birlesik nokta) verir, "i" DEGIL — Turkce bir ad
    kendi kucuk harfli yaziminla eslesmezdi. Once I/İ elle cevrilir.
    """
    s = unicodedata.normalize("NFKC", text)
    s = s.replace("İ", "i").replace("I", "ı")
    s = s.casefold()
    s = "".join(c for c in s if not _is_invisible(c))
    s = re.sub(r"\s+", " ", s).strip()
    return unicodedata.normalize("NFKC", s)

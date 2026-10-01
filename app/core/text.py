"""Serbest metin alanlarının paylaşılan uzunluk tavanı (TB4 S3, onay 2026-08-09).

Kolonu `Text` olan (DB'de sınırsız) kullanıcı metinlerine tavanı ŞEMA koyar;
kolon tipi değişmez, migration gerekmez. Değer 2000'dir — `roles.description`
ve belge arşivi (`documents.schemas.DESCRIPTION_MAX_LENGTH`) emsaliyle aynı.

Sabit `app.core`'dadır çünkü İKİ aile (`boq` + `contracts`) onu paylaşır; aynı
sayı iki modüle ayrı ayrı yazılsaydı biri güncellenip diğeri unutulurdu ve
tavan o uçtan atlatılabilir olurdu (belge arşivi T4 bulgusu #2'nin dersi).

Zaten `String(N)` ile sınırlı alanlar buraya BAĞLANMAZ: onların tavanı kolon
sınırıdır ve şemanın onunla birebir kalması gerekir.
"""

FREE_TEXT_MAX_LENGTH = 2000


def like_contains_pattern(q: str) -> str:
    """`ILIKE '%q%'` kalibi; ters bolu, `%` ve `_` KACIRILIR (kullanici metni joker olmaz).
    Cagiran `escape` olarak TEK ters bolu verir. Katalog listeleri (cekirdek + EV) tek kopyayi
    paylasir."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"

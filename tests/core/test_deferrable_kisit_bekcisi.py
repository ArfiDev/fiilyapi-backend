"""🔴 YAPISAL BEKÇİ (kayıt 2 + 3, `kalan_is` madde "YAPISAL BEKÇİ"): yeni bir
`deferrable=True` kısıt sessizce eklenemez.

## Neden bu bekçi var (TEARDOWN-B1 sonrası: İKİNCİ SAVUNMA HATTI)

`app/core/db.py`deki `await session.commit()` `get_db`nin TEARDOWN'undadır.
Eskiden `get_db` çıplak `Depends` ile hesaplanmış `scope="request"` alır,
teardown yanıttan SONRA koşar ve istemci `200 {"ok": true}` görürdü. Artık
uçlar `DbSession` takma adını (`scope="function"`) kullanır: commit yanıttan
ÖNCE koşar, ertelenmiş bir UQ ihlali commit'te patlayınca `IntegrityError`
kayıtlı handler'a ulaşır ve istemci **409** görür
(`tests/core/test_teardown_commit_http_olcum.py`). Yani "istemci 200 görüp veri
yazılmamış olur" öncülü GEÇERSİZDİR.

Bu bekçi bu yüzden artık BİRİNCİL güvenlik ağı değil, İKİNCİ savunma hattıdır:
ertelenmiş kısıtta ihlal flush'ta değil COMMIT'te doğar; commit'te 409 dönmesi
kabul edilebilir ama (a) hata mesajı/uç bazlı özel 409 gövdesi yerine genel
"Veri bütünlüğü hatası" görünür, (b) `SET CONSTRAINTS IMMEDIATE`/kilit desenleri
ihlali daha erken ve anlamlı bir hatayla yakalar, (c) yazma sırası garantileri
(yanıt öncesi commit) kapsam kayarsa yine bozulur. Yeni bir deferrable kısıt
bilinçli seçim gerektirsin diye allowlist korunur.

`DEFERRABLE INITIALLY DEFERRED` bir UNIQUE kısıt tam olarak bu pencereyi açar:
ihlal `flush()`ta DEĞİL, transaction COMMIT'inde patlar — yani tam bu
teardown'da. Bugün iki böyle kısıt var ve İKİSİ DE ayrı bir mekanizmayla
korunuyor:

* `site_plan_rows` (`uq_site_plan_rows_site_kind_section_label`) —
  `write.save_rows` her yazmada `repository.enforce_row_uniqueness_now` ile
  `SET CONSTRAINTS ... IMMEDIATE` çalıştırır (`site_planning/write.py:143`),
  yani ihlal artık `flush()`ta patlar ve normal `IntegrityError` yoluna
  (409) girer — teardown'a hiç ulaşmaz.
* `boq_item_section_allocations` (`uq_boq_item_section_allocations_item_section`)
  — `repository.lock_item` poz satırını `FOR UPDATE` kilitler ve UQ anahtarı
  poz-başınadır; çapraz-istek yarışı bu kilitle SERİLEŞTİRİLİR, ihlal fiilen
  hiç doğmaz.

Bu bekçi YENİ bir `deferrable=True` kısıtın bu ALLOWLIST'e bilerek
eklenmesini zorunlu kılar — eklenmezse test KIRMIZI olur ve yazarı yukarıdaki
iki desenden birini (flush'ta erken yakalama ya da kilitle serileştirme)
seçmeye iter.

Bu bekçi teardown deliğini kapatan mekanizma DEĞİLDİR (onu `DbSession`in
function kapsamı ve `test_getdb_kapsam_bekcisi.py` kapatır); yalnız yeni
ertelenmiş kısıtların sessizce, savunmasız eklenmesini engeller.
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint

import app.main  # noqa: F401  — tüm modelleri import ederek Base.metadata'yı doldurur
from app.core.db import Base

#: (tablo_adı, kısıt_adı) -> korunma mekanizmasının kısa açıklaması.
#: Yeni bir satır eklerken açıklama alanı BOŞ bırakılmaz; bu dosyanın
#: docstring'i güncellenmelidir.
BILINEN_DEFERRABLE_KISITLAR: dict[tuple[str, str], str] = {
    (
        "site_plan_rows",
        "uq_site_plan_rows_site_kind_section_label",
    ): "write.save_rows -> repository.enforce_row_uniqueness_now (SET CONSTRAINTS IMMEDIATE)",
    (
        "boq_item_section_allocations",
        "uq_boq_item_section_allocations_item_section",
    ): "repository.lock_item (FOR UPDATE poz satır kilidi, UQ anahtarı poz-başına)",
}


def _metadata_deferrable_kisitlari() -> set[tuple[str, str]]:
    bulunanlar: set[tuple[str, str]] = set()
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            if not constraint.deferrable:
                continue
            assert constraint.name is not None, (
                f"Adsız deferrable UQ kısıt bulundu ({table.name}); bekçi yalnız "
                "adlandırılmış kısıtları izleyebilir."
            )
            bulunanlar.add((table.name, constraint.name))
    return bulunanlar


def test_YENI_deferrable_kisit_allowlistsiz_eklenemez() -> None:
    """🔴 ASIL BEKÇİ. Mutasyon: allowlist'ten bir satır silinirse ya da
    modelde yeni bir `deferrable=True` UQ eklenip allowlist'e işlenmezse bu
    test KIRMIZI olur."""
    bulunanlar = _metadata_deferrable_kisitlari()
    bilinen = set(BILINEN_DEFERRABLE_KISITLAR)

    yeni_ve_korunmasiz = bulunanlar - bilinen
    assert not yeni_ve_korunmasiz, (
        "YENİ bir deferrable=True UNIQUE kısıt bulundu ama ALLOWLIST'te yok: "
        f"{yeni_ve_korunmasiz}. Bu kısıt `app/core/db.py`deki teardown "
        "commit'inde patlayabilir (function kapsamında 409 döner, bkz. "
        "tests/core/test_teardown_commit_http_olcum.py) ama flush'ta yakalanmaz. "
        "Ya `site_plan_rows` desenini (SET CONSTRAINTS IMMEDIATE) ya da "
        "`boq_item_section_allocations` desenini (FOR UPDATE serileştirme) "
        "uygula, sonra bu dosyadaki BILINEN_DEFERRABLE_KISITLAR'a ekle."
    )

    artik_yok = bilinen - bulunanlar
    assert not artik_yok, (
        f"Allowlist'te olup metadata'da artık bulunmayan kısıt(lar): {artik_yok}. "
        "Kısıt kaldırıldıysa (ör. migration ile) bu satırı da BILINEN_DEFERRABLE_KISITLAR'dan sil."
    )

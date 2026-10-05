"""Satınalma modülünün hassas alan etiketli sayısal tipleri (IZN-B4d).

🔴 GECE KARARI (IZN-B4d, procurement):
* talep kalemi tahmini birim fiyat / satır tutarı / tahmini toplam, teklif birim fiyatı /
  nakliye bedeli / toplam maliyet, sipariş tutarı, tedarikçi "bu yıl toplam sipariş" ve özet
  "bu ay sipariş tutarı" = `maliyet_kar`. Maskeli alan `null` döner, bu yüzden yanıt tipleri
  `| None`'dır.
* miktar, mevcut stok, talep toplam miktarı, sayaçlar = `yok` (miktar/sayaç, para değil).
* tedarikçi adı / telefon / vergi no GİZLENMEZ (`yok`): şirket katalog verisidir, kişi değil.
* tedarikçi IBAN / banka hesabı / ödeme alanı bu modülde YOKTUR (şema §5, kapsam dışı) →
  `banka_kasa` etiketi kullanılmadı.
* ₺500K onay eşiği karşılaştırması SUNUCU içi iş mantığıdır (`transitions`): maske yalnız yanıt
  serileştirmesinde çalışır, servis katmanı maskesiz değerle çalışır.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from app.core.field_mask import Hassas

#: Para (`maliyet_kar`): `| None` — maske `null` yazar.
Maliyet = Annotated[Decimal | None, Hassas.maliyet_kar]
#: Aynı etiket, İSTEK gövdesinde `None` taşımayan alan.
MaliyetGirdi = Annotated[Decimal, Hassas.maliyet_kar]
#: Miktar / stok: açıkça hassas DEĞİL.
Yok = Annotated[Decimal | None, Hassas.yok]
#: Aynı, `None` taşımayan alan.
YokGirdi = Annotated[Decimal, Hassas.yok]
#: Sayaç (adet): para değil.
Sayac = Annotated[int, Hassas.yok]
#: Tedarikçi telefon / vergi no: katalog verisi, gizlenmez.
YokMetin = Annotated[str | None, Hassas.yok]

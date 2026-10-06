"""Stok modülünün hassas alan etiketli sayısal tipleri (IZN-B4d).

🔴 GECE KARARI (IZN-B4d, inventory):
* birim fiyat, son giriş fiyatı, stok değeri, bölüm tüketim değeri = `maliyet_kar`.
  Maskeli alan `null` döner, bu yüzden yanıt tipleri `| None`'dır.
* miktar, bakiye, eşik (`min_stock`), kalem/satır sayaçları, bekleyen sipariş / aylık ihtiyaç
  zarfı = `yok` (miktar/sayaç, para değil).
* ağırlıklı ortalama maliyet bu modülde YOKTUR (son giriş fiyatı esastır, spec §7 S6).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from app.core.field_mask import Hassas
from app.modules.projects.schemas import MetricPlaceholder

#: Para (`maliyet_kar`): `| None` — maske `null` yazar.
Maliyet = Annotated[Decimal | None, Hassas.maliyet_kar]
#: Miktar / bakiye / eşik: açıkça hassas DEĞİL.
Yok = Annotated[Decimal | None, Hassas.yok]
#: Aynı, `None` taşımayan alan.
YokGirdi = Annotated[Decimal, Hassas.yok]
#: Sayaç (kalem / satır adedi).
Sayac = Annotated[int, Hassas.yok]
#: Yer tutucu zarf (para değil): bekleyen sipariş, aylık ihtiyaç.
YokZarf = Annotated[MetricPlaceholder, Hassas.yok]

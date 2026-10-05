"""Ekipman modülünün hassas alan etiketli sayısal tipleri (IZN-B4d).

🔴 GECE KARARI (IZN-B4d, equipment):
* makine alış/piyasa bedeli, kira birim fiyatı, kira hakedişi ve yakıt TUTARLARI, işçilik/maliyet
  = `maliyet_kar`. Maskeli alan `null` döner, bu yüzden yanıt tipleri `| None`'dır.
* kira hakedişi ÖDEMESİ (kümülatif ödenen, ödenecek toplam) = YALNIZ `maliyet_kar` (`Maliyet`).
  ONARIM KARARI: ilk sürüm `banka_kasa` + `maliyet_kar` etiketliydi ama `payable_total =
  invoice_amount + vat_amount` ve `cumulative_paid` görünen `our_total`lardan TÜRETİLEBİLİR;
  türetilebilen alanı ayrı bir kategoriyle gizlemek anlamsızdı (etiket boşa çıkıyordu).
* saat, litre, sayaç, kW, yüzde, norm tüketim = `yok` (miktar/oran, para değil).
* operatör TC/telefon bu modülde YOKTUR (kişi bağı personel modülündedir).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from app.core.field_mask import Hassas

#: Para (`maliyet_kar`): `| None` — maske `null` yazar.
Maliyet = Annotated[Decimal | None, Hassas.maliyet_kar]
#: Aynı etiket, İSTEK gövdesinde `None` taşımayan alan.
MaliyetGirdi = Annotated[Decimal, Hassas.maliyet_kar]
#: Miktar / oran / sayaç: açıkça hassas DEĞİL.
Yok = Annotated[Decimal | None, Hassas.yok]
#: Aynı, `None` taşımayan alan.
YokGirdi = Annotated[Decimal, Hassas.yok]

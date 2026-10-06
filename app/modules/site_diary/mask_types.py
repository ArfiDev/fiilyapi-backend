"""Şantiye günlüğü modülünün hassas alan etiketli sayısal tipleri (IZN-B4d).

🔴 GECE KARARI (IZN-B4d, site_diary):
* satır birim fiyatı (BOQ snapshot'ı), satır ₺ katkısı (`line_amount`), günlük/liste satır ₺
  toplamı (`lines_total`), özet dönem tutarı / toplamı / sözleşme tutarı ve sözleşme kalemi birim
  fiyatı = `sozlesme_fiyat` + `maliyet_kar` (hepsi BOQ / işveren sözleşmesi fiyatından türer; BOQ
  modülüyle tutarlı, iki kategoriden biri gizli olan rol `null` görür — fail-closed). Maskeli alan
  `null` döner, bu yüzden OKUMA tipleri `| None`'dır.
* miktar (günlük/kümülatif/planlı/kalan/BOQ/sözleşme miktarı), tamamlanma oranı, işçi saati,
  sıcaklık, rüzgâr = `yok` (ölçü/oran, para değil).
* işçi kişisel bilgisi (TC, telefon, ücret) günlükte YOKTUR → `maas_kisisel` kullanılmadı.
* Yazma gövdesinde parasal alan YOKTUR (fiyat BOQ'dan gelir, istemciden alınmaz: `extra=forbid`)
  → PUT/PATCH'te "gizli alan dolu → 403" kuralı bu modülde uygulanacak alan bulmaz.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from app.core.field_mask import Hassas

#: Para (BOQ/sözleşme fiyatından türer): `| None` — maske `null` yazar.
Maliyet = Annotated[Decimal | None, Hassas.sozlesme_fiyat, Hassas.maliyet_kar]
#: Miktar / ölçü / oran: açıkça hassas DEĞİL (`None` taşıyabilir).
Yok = Annotated[Decimal | None, Hassas.yok]
#: Aynı, `None` taşımayan alan.
YokGirdi = Annotated[Decimal, Hassas.yok]
#: İşçi sayısı toplamı (adet): para değil.
Sayac = Annotated[int, Hassas.yok]

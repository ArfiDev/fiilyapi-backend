"""Planlama / kazanılmış değer (EV) modülünün hassas alan etiketli sayısal tipleri (IZN-B4d, 1/2).

🔴 GECE KARARI (IZN-B4d, earned_value 1/2 — bütçe, katalog, günlük dağıtım, ayarlar):
* Bu modülün bütçe / katalog / günlük dağıtım / ayar yüzeyi TL TAŞIMAZ: bütçe **adam-saat**
  (`budget_mhr`, `unit_mhr` = adam-saat/birim), kazanılmış / harcanan **adam-saat**
  (`earned_day`, `spent_day`), `pf_day` ve `share` oran/endeks, `hours` saat, `planned_qty` /
  `qty` miktar, `required_people` kişi sayısı, bantlar ve tolerans yüzde/puan. Hiçbiri
  `maliyet_kar` / `sozlesme_fiyat` / `maas_kisisel` DEĞİLDİR (adam-saat ücret değildir; sözleşme
  birim fiyatı bu şemalarda yoktur) → hepsi `yok`. Bu bilinçli bir etikettir, unutulmuş alan
  değil: bekçi `yok`u açıkça ister.
* Kişi × kod × saat hücreleri (günlük dağıtım) KVKK nedeniyle AI'ya kapalıdır ama TL değildir;
  maske kapsamı dışıdır (saat = `yok`).
* TL türevi (kazanılmış değer TL'si, planlanan değer TL'si, maliyet farkı) rapor şemalarında
  aranır → earned_value 2/2 (raporlar).
* Yazma gövdelerinde parasal alan YOKTUR → PUT/PATCH'te "gizli alan dolu → 403" kuralı bu
  yüzeyde uygulanacak alan bulmaz (testte: gizli roller yine de yazabilir).
"""

from __future__ import annotations

from typing import Annotated

from app.core.field_mask import Hassas
from app.modules.earned_value.decimal_out import EvDecimal

#: Adam-saat / saat / miktar / oran / endeks / kişi sayısı: AÇIKÇA hassas değil.
Yok = Annotated[EvDecimal, Hassas.yok]
#: Aynı, `None` taşıyabilen alan.
YokOpt = Annotated[EvDecimal | None, Hassas.yok]

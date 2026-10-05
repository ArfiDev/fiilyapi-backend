"""Ekipman Detay ekranının TÜREV blokları (MK-4).

Mockup: `projedesign/Makine - Ekipman Detay.dc.html`.

## Bu dosya niçin `schemas.py`den AYRI

`EquipmentResponse` liste ucunun da gövdesidir ve docstring'i türev alanları
BİLEREK dışarıda tutar: liste her çizilişte hareket tablosunu taramak zorunda
kalırdı. Detay ekranının altı sayısı (sonraki bakım saati · kalan çalışma saati
· tahmini bakım tarihi · `%57` çubuğu · kümülatif ödenen) TÜREVDİR ve yalnız
TEK BİR ekipman için hesaplanır — bu yüzden kendi ucunda, kendi şemasında yaşar.

`GET /equipment/{id}` DEĞİŞMEDİ: sözleşme genişlemesi ADDITIVE'dir, yeni uç
`GET /equipment/{id}/detail`tir. Mevcut ucun gövdesi değiştirilseydi listeyi
ve detayı aynı şemadan okuyan her istemci kırılırdı.
"""

from datetime import date

from pydantic import BaseModel

from app.modules.equipment.mask_types import Odeme, Yok
from app.modules.equipment.models import EquipmentMaintenancePeriod
from app.modules.equipment.schemas import EquipmentResponse


class EquipmentMaintenanceBlock(BaseModel):
    """🔧 Bakım Bilgileri kartı (MD:145-160) + `Sonraki Bakım` KPI'ı (MD:46-48).

    İlk dört alan SAKLANAN girdinin aynasıdır (kart onları da basar), son beşi
    `maintenance.py`de her okumada TÜRER.

    🔴 **Her türev AYRI AYRI `None` olabilir** (MK-1 K16 deseni): periyodu
    `monthly` olan bir makinede saat cinsinden pencere YOKTUR ama son bakım
    TARİHİ bilinir. Tek bir "bakım bilgisi yok" bayrağına indirgenselerdi
    bilinen bir olgu, eksik bir ölçüt yüzünden ekrandan silinirdi.
    """

    period: EquipmentMaintenancePeriod | None
    #: `monthly` periyotta `None` — saat cinsinden bir pencere YOKTUR.
    period_hours: int | None
    last_service_date: date | None
    last_service_hourmeter: Yok
    hourmeter_hours: Yok
    next_service_hourmeter: Yok
    #: MD:160 `286 / 500 saat çalışıldı` — çubuğun PAYI.
    used_hours: Yok
    #: MD:155 `214 sa`. NEGATİF olabilir: bakımı geçmiş makine gerçektir.
    remaining_hours: Yok
    #: MD:159 `%57`. Yüzde SUNUCU DAMGASIDIR (F-P10 kanonu) — istemci payı
    #: paydaya kendi bölseydi iki ekran aynı çubuğu farklı doldururdu.
    usage_pct: Yok
    #: MD:157 `~05.09.2026` — son `ESTIMATE_WINDOW_DAYS` günün temposundan.
    estimated_service_date: date | None


class EquipmentRentalTotals(BaseModel):
    """📋 Kiralama Bilgileri kartının TEK türevi: MD:82 `Kümülatif Ödenen`.

    Kartın öteki yedi alanı SAKLANIR ve `EquipmentResponse`ta durur.

    🔴 Tutarı hesaplanamayan satır UYDURMA `0` ile toplama GİRMEZ ama SESSİZ de
    KALMAZ (MK-1 `summarize` / MK-2 `our_total_unknown_count` kanonu): atlanır
    ve ADETÇE bildirilir. Toplamın kendisi `None` yapılmadı — tek bedelsiz satır
    yüzünden bütün kartı gizlemek kullanıcıyı ekranın tamamından ederdi.
    """

    #: YALNIZ `paid` hakedişlerin `rented` satırlarından (MK-2 K3: `owned` ve
    #: `breakdown` hiçbir ödenecek toplamın kaynağı değildir).
    cumulative_paid: Odeme
    cumulative_paid_unknown_count: int
    #: Toplamı üreten ÖDENMİŞ hakediş adedi — 0 ise `cumulative_paid`in `0`ı
    #: "hiç ödeme yok" demektir, "hepsi hesaplanamadı" değil.
    paid_invoice_count: int


class EquipmentDetailResponse(BaseModel):
    """`GET /equipment/{equipment_id}/detail` — ekranın TAMAMININ veri tabanı.

    `equipment` SAKLANAN künyedir (`GET /equipment/{id}` ile BİREBİR aynı şema —
    ikinci bir künye şeması iki ekranda iki farklı kart üretirdi); yanındaki iki
    blok TÜREVDİR.
    """

    equipment: EquipmentResponse
    maintenance: EquipmentMaintenanceBlock
    rental: EquipmentRentalTotals
    #: Tahmini bakım tarihinin ve çalışma temposu penceresinin DAYANAK GÜNÜ.
    #: Yanıtta AÇIKÇA durur: "~05.09.2026" hangi güne göre hesaplandığı
    #: bilinmeden okunamaz ve testte de sabitlenemezdi.
    as_of: date

"""Çalışma özeti (M3 ana tablosu · K15).

🔴 **K15: tfoot SATIRLARDAN türer.** 🔴 **K18: maliyet `cost.py`den**,
🔴 **K7: kullanım % `consumption.py`den** gelir — ikisi de burada YENİDEN
yazılmaz. Hafta sınırının tek tanımı `periods._monday`dedir.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.equipment import consumption, cost, repository
from app.modules.equipment.models import WorkLogType
from app.modules.equipment.schemas import (
    WorkSummaryResponse,
    WorkSummaryRow,
    WorkSummaryTotals,
    WorkSummaryWeek,
)
from app.modules.equipment.service.core import _visible_project_ids
from app.modules.equipment.service.periods import _monday, month_bounds
from app.modules.users.models import User


def _week_buckets(ilk: date, son: date, gunluk: list[Row]) -> list[WorkSummaryWeek]:
    """🔴 M3:219-243 haftalık kovaları.

    **Hafta sınırı = PAZARTESİ başlangıçlı ISO haftası**, ayın 1'ini içeren
    haftadan sayılır (`index` 1'den başlar). Takvim haftası seçildi çünkü
    kullanıcı "bu hafta" derken takvim haftasını kastediyor; "ayın 1'inden
    itibaren 7'şer gün" seçilseydi aynı pazartesi ayın başında H1, sonunda H5
    olur ve ardışık iki ayın grafikleri karşılaştırılamazdı.

    Sınırlar AYA KIRPILIR: kova "1–5 Temmuz" der, haziranın son günlerini
    saymaz — grafiğin altındaki toplam, tablonun toplamıyla tutmalıdır (K15).

    İzoleli yıl sonu sorunu YOKTUR: hafta indeksi ISO hafta NUMARASINDAN değil
    pazartesiler arasındaki GÜN FARKINDAN türer.
    """
    ilk_pazartesi = _monday(ilk)
    kova_sayisi = ((_monday(son) - ilk_pazartesi).days // 7) + 1

    saatler: list[dict[WorkLogType, Decimal]] = [
        dict.fromkeys(WorkLogType, Decimal("0")) for _ in range(kova_sayisi)
    ]
    for gun, tip, saat in gunluk:
        saatler[(_monday(gun) - ilk_pazartesi).days // 7][tip] += saat

    kovalar: list[WorkSummaryWeek] = []
    for sira in range(kova_sayisi):
        pazartesi = ilk_pazartesi + timedelta(weeks=sira)
        kova = saatler[sira]
        toplam = sum(kova.values(), Decimal("0"))
        kovalar.append(
            WorkSummaryWeek(
                index=sira + 1,
                start_date=max(pazartesi, ilk),
                end_date=min(pazartesi + timedelta(days=6), son),
                hours=toplam,
                # Beraberlikte `worked` kazanır: bir haftayı arıza rengine
                # boyamak, o hafta en az onun kadar çalışılmışken yanıltıcıdır.
                dominant_record_type=(
                    None
                    if not toplam
                    else (
                        WorkLogType.breakdown
                        if kova[WorkLogType.breakdown] > kova[WorkLogType.worked]
                        else WorkLogType.worked
                    )
                ),
            )
        )
    return kovalar


async def work_summary(
    session: AsyncSession, actor: User, *, year: int, month: int, site_id: uuid.UUID | None
) -> WorkSummaryResponse:
    """`GET /equipment/work-summary` — M3'ün TAMAMI.

    🔴 **K15: tfoot SATIRLARDAN türer.** Mockup'ın 428 saat / ₺124.800 / %69'u
    kendi satırlarıyla tutarsızdır (692 / ₺144.200 / %57,7) ve KOPYALANMAZ.
    🔴 **K18: maliyet `cost.py`den**, 🔴 **K7: kullanım % `consumption.py`den**
    gelir — ikisi de burada YENİDEN yazılmaz.
    🔴 **K16:** maliyeti bilinmeyen satır `null` durur ve toplama UYDURMA bir 0
    ile GİRMEZ; toplamın kendisi `null` yapılmaz (tek bilinmeyen makine yüzünden
    bütün tabloyu gizlemek kullanıcıyı ekranın tamamından ederdi).
    """
    project_ids = await _visible_project_ids(session, actor, liste=True)
    ilk, son = month_bounds(year, month)
    ham = await repository.work_summary_rows(
        session, project_ids, date_from=ilk, date_to=son, site_id=site_id
    )

    satirlar: list[WorkSummaryRow] = []
    for (
        equipment_id,
        name,
        equipment_site_id,
        hours,
        breakdown_hours,
        rate_amount,
        rate_period,
        capacity,
    ) in ham:
        kullanim = consumption.compute_usage(hours=hours, monthly_capacity_hours=capacity)
        satirlar.append(
            WorkSummaryRow(
                equipment_id=equipment_id,
                equipment_name=name,
                site_id=equipment_site_id,
                hours=hours,
                usage_pct=kullanim.usage_pct,
                usage_reason=kullanim.usage_reason,
                breakdown_hours=breakdown_hours,
                cost=cost.compute_cost(
                    hours=hours,
                    rate_amount=rate_amount,
                    rate_period=rate_period,
                    monthly_capacity_hours=capacity,
                ),
            )
        )

    bilinen_kullanimlar = [s.usage_pct for s in satirlar if s.usage_pct is not None]
    toplamlar = WorkSummaryTotals(
        hours=sum((s.hours for s in satirlar), Decimal("0")),
        breakdown_hours=sum((s.breakdown_hours for s in satirlar), Decimal("0")),
        cost=sum((s.cost for s in satirlar if s.cost is not None), Decimal("0")),
        usage_pct_avg=(
            consumption.quantize_ratio(sum(bilinen_kullanimlar) / len(bilinen_kullanimlar))
            if bilinen_kullanimlar
            else None
        ),
    )
    gunluk = await repository.daily_hours_by_type(
        session, project_ids, date_from=ilk, date_to=son, site_id=site_id
    )
    return WorkSummaryResponse(
        year=year,
        month=month,
        rows=satirlar,
        totals=toplamlar,
        weeks=_week_buckets(ilk, son, gunluk),
    )

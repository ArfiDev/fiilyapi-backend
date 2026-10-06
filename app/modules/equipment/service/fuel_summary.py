"""Yakıt özeti (M4 üst blok + tablo · K15/K16/K17/K19 · T5).

🔴 **K16/K17:** sapma + rozet `consumption.py`den, 🔴 **K19:** satır tutarı
`cost.fuel_amount`ten gelir; eşikler ve formül burada YENİDEN yazılmaz.
Buradaki tek yerel aritmetik ortalama litre fiyatının YUVARLAMA adımıdır
(`_quantize_unit_price`) — ölçeği `cost.quantize_money`den (tam sayı) farklı
olduğu için ayrı durur.
"""

import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.equipment import consumption, cost, repository
from app.modules.equipment.schemas import FuelSummaryResponse, FuelSummaryRow
from app.modules.equipment.service.core import _visible_project_ids
from app.modules.equipment.service.periods import month_bounds
from app.modules.users.models import User

#: Ortalama litre fiyatının yuvarlaması: K19 (`ROUND_HALF_UP`), `unit_price`
#: kolonuyla AYNI ölçek (4 ondalık) — `cost.quantize_money` (tam sayı) burada
#: YANLIŞ ölçektir, bu yüzden AYRI bir sabit/işlev (formülü İKİNCİ KEZ YAZMAZ,
#: yalnız yuvarlama ADIMI farklıdır).
_UNIT_PRICE_QUANTUM = Decimal("0.0001")


def _quantize_unit_price(value: Decimal) -> Decimal:
    return value.quantize(_UNIT_PRICE_QUANTUM, rounding=ROUND_HALF_UP)


async def fuel_summary(
    session: AsyncSession, actor: User, *, year: int, month: int, equipment_id: uuid.UUID | None
) -> FuelSummaryResponse:
    """`GET /equipment/fuel-summary` — M4'ün TAMAMI.

    🔴 **K15:** toplamlar HAM satırlardan (`repository.fuel_summary_rows`)
    üretilir; her satırın tutarı `cost.fuel_amount`ten (K19) TEK TEK
    yuvarlanıp toplanır — SQL'de tek seferde `SUM(litre*fiyat)` alınıp SONDA
    yuvarlansaydı K19'un satır bazlı doğrulaması (4 satır) bozulurdu.

    🔴 **K16/K17:** sapma + rozet `consumption.evaluate_consumption`ten gelir,
    eşikler burada YENİDEN yazılmaz. `lt_per_hour_avg` payda 0 ise `null`dur
    (dönemin ÇALIŞMA KAYDI saat toplamı — modüller arası bağ, M4:39).
    """
    project_ids = await _visible_project_ids(session, actor, liste=True)
    ilk, son = month_bounds(year, month)
    ham = await repository.fuel_summary_rows(
        session, project_ids, date_from=ilk, date_to=son, equipment_id=equipment_id
    )
    saat_haritasi = await repository.work_hours_by_equipment(
        session, project_ids, date_from=ilk, date_to=son
    )

    gruplar: dict[uuid.UUID, dict] = {}
    for eid, name, site_id, norm_consumption, norm_unit, liters, unit_price in ham:
        grup = gruplar.setdefault(
            eid,
            {
                "name": name,
                "site_id": site_id,
                "norm_consumption": norm_consumption,
                "norm_unit": norm_unit,
                "liters": Decimal("0"),
                "amount": Decimal("0"),
            },
        )
        grup["liters"] += liters
        grup["amount"] += cost.fuel_amount(liters=liters, unit_price=unit_price)

    satirlar: list[FuelSummaryRow] = []
    for eid, grup in gruplar.items():
        saat = saat_haritasi.get(eid, Decimal("0"))
        sonuc = consumption.evaluate_consumption(
            total_liters=grup["liters"],
            total_hours=saat,
            norm_consumption=grup["norm_consumption"],
            norm_unit=grup["norm_unit"],
        )
        satirlar.append(
            FuelSummaryRow(
                equipment_id=eid,
                equipment_name=grup["name"],
                site_id=grup["site_id"],
                liters=grup["liters"],
                amount=grup["amount"],
                actual=sonuc.actual,
                norm=grup["norm_consumption"],
                deviation_pct=sonuc.deviation_pct,
                deviation_reason=sonuc.deviation_reason,
                consumption_status=sonuc.status,
            )
        )
    satirlar.sort(key=lambda s: (s.equipment_name, str(s.equipment_id)))

    toplam_litre = sum((s.liters for s in satirlar), Decimal("0"))
    toplam_tutar = sum((s.amount for s in satirlar), Decimal("0"))
    # 🔴 Filo düzeyinde AYNI formül (`actual_consumption`, M4:39 `2.840/428=6,6`):
    # `equipment_id` süzgeci verildiğinde payda TEK makinenin kendi saatidir,
    # verilmediğinde GÖRÜNÜR filonun tamamıdır.
    toplam_saat = (
        saat_haritasi.get(equipment_id, Decimal("0"))
        if equipment_id is not None
        else sum(saat_haritasi.values(), Decimal("0"))
    )
    lt_per_hour_avg = consumption.actual_consumption(
        total_liters=toplam_litre, total_hours=toplam_saat
    )
    avg_unit_price = _quantize_unit_price(toplam_tutar / toplam_litre) if toplam_litre else None
    abnormal_count = sum(
        1
        for s in satirlar
        if s.consumption_status
        in (consumption.ConsumptionStatus.warning, consumption.ConsumptionStatus.critical)
    )

    return FuelSummaryResponse(
        year=year,
        month=month,
        total_liters=toplam_litre,
        total_amount=toplam_tutar,
        lt_per_hour_avg=lt_per_hour_avg,
        avg_unit_price=avg_unit_price,
        abnormal_count=abnormal_count,
        rows=satirlar,
    )

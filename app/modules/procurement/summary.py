"""Satınalma KPI şeridi (SA spec §4) — T4.

SAT 69-86 ve SIP 38-43 kartlarının TEK veri kaynağı. Ayrı bir modülde durur
çünkü `service.py` zaten üç varlığın iş kurallarını taşır ve bu dosya onların
HİÇBİRİNİ değiştirmez — yalnızca sayar.

## Mockup karşılıkları (kart etiketleri birebir)

| Kart | Alan | Tanım |
|---|---|---|
| SAT 71 "Açık Talepler" | `open_requests` | `transitions.OPEN_REQUEST_STATUSES` |
| SAT 75 "Teklif Bekleniyor" | `quote_wait_requests` | `quote_wait` |
| SAT 79 "Bu Ay Sipariş" | `orders_this_month_total` | bu ayın sipariş tutarı |
| SAT 83 "Onay Bekleyen" | `pending_approval_requests` | `pending_approval` |
| SIP 39 "Aktif Siparişler" | `active_orders` | `transitions.PENDING_ORDER_STATUSES` |
| SIP 40 "Bu Ay Toplam" | `orders_this_month_total` | SAT 79 ile **AYNI ALAN** |
| SIP 41 "Yolda" | `in_transit_orders` | `in_transit` |
| SIP 42 "Teslim Edildi" | `delivered_orders` | `delivered` |

İki mockup'ın para kartı TEK alandır: ayrı hesaplansaydı aynı ay için iki farklı
tutar gösterebilirlerdi. "Aktif Siparişler" de ST'nin "Bekleyen Sipariş"
zarfıyla tek kümeden (`PENDING_ORDER_STATUSES`) beslenir.

## N+1 YOKTUR

Üç sorgu koşar ve sayısı DURUM SAYISINDAN BAĞIMSIZDIR (`repository`nin gruplu
sayaçları). Yedi kart için yedi `count` sorgusu açmak şeridin N+1'i olurdu.
"""

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import today
from app.modules.procurement import repository, transitions
from app.modules.procurement.guards import PERMISSION_MODULE
from app.modules.procurement.models import PurchaseOrderStatus, PurchaseRequestStatus
from app.modules.procurement.schemas import PurchasingSummaryResponse
from app.modules.projects.service import sirket_liste_ciftleri, visible_projects
from app.modules.users.models import User

__all__ = ["build_summary"]

_MONEY = Decimal("0.01")


async def build_summary(
    session: AsyncSession, actor: User, *, project_id: uuid.UUID | None
) -> PurchasingSummaryResponse:
    """KPI şeridini kurar.

    **KAPSAM:** yalnız görünen projeler. `project_id` süzgeci kapsamı
    GENİŞLETMEZ, daraltır — görünmeyen bir proje kimliği verildiğinde kesişim
    boştur ve sayaçlar sıfır kalır (liste uçlarındaki kuralın aynısı).
    """
    project_ids = [
        p.id
        for p in await visible_projects(
            session, actor, sirket_ciftleri=sirket_liste_ciftleri(PERMISSION_MODULE)
        )
    ]
    bugun = today()

    talepler = await repository.request_status_counts(session, project_ids, project_id=project_id)
    siparisler = await repository.order_status_counts(session, project_ids, project_id=project_id)
    ay_tutari = await repository.orders_total_in_month(
        session, project_ids, project_id=project_id, year=bugun.year, month=bugun.month
    )

    return PurchasingSummaryResponse(
        open_requests=sum(talepler.get(d, 0) for d in transitions.OPEN_REQUEST_STATUSES),
        quote_wait_requests=talepler.get(PurchaseRequestStatus.quote_wait, 0),
        pending_approval_requests=talepler.get(PurchaseRequestStatus.pending_approval, 0),
        # Para her zaman iki hanelidir: kart "₺0" değil "₺0,00" tabanından
        # biçimlenir ve boş kurulumda alan `0` değil `0.00` döner.
        orders_this_month_total=ay_tutari.quantize(_MONEY),
        active_orders=sum(siparisler.get(d, 0) for d in transitions.PENDING_ORDER_STATUSES),
        in_transit_orders=siparisler.get(PurchaseOrderStatus.in_transit, 0),
        delivered_orders=siparisler.get(PurchaseOrderStatus.delivered, 0),
    )

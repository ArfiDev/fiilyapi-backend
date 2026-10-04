"""Talep okuma yolu ve turevleri — FST detay govdesi + SAT tablosu.

Iki ucun da ortak vaadi: **sorgu sayisi satir/kalem sayisindan BAGIMSIZDIR**
(N+1 yok). `can_delete` bayragi silme ucuyla AYNI fonksiyondan beslenir
(`request_access`), ekran dugmeyi gosterip sonra 403 yemesin.
"""

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.slug import url_safe_key
from app.modules.inventory.models import StockItem
from app.modules.procurement import repository
from app.modules.procurement.models import (
    PurchasePriority,
    PurchaseRequest,
    PurchaseRequestLine,
    PurchaseRequestStatus,
)
from app.modules.procurement.schemas import (
    PurchaseRequestLineResponse,
    PurchaseRequestListResponse,
    PurchaseRequestListRow,
    PurchaseRequestResponse,
)
from app.modules.procurement.service.core import _visible_project_ids
from app.modules.procurement.service.request_access import (
    can_delete_request,
)
from app.modules.users.models import User


def _line_total(line: PurchaseRequestLine) -> Decimal | None:
    if line.estimated_unit_price is None:
        return None
    return line.quantity * line.estimated_unit_price


def _to_line_response(
    line: PurchaseRequestLine, item: StockItem | None, balances: dict[uuid.UUID, Decimal]
) -> PurchaseRequestLineResponse:
    """`name`/`unit` iki kapinin ORTAK yuzeyidir: stok kartli kalemde kartin,
    katalogsuz kalemde girilen degerler. Ekran iki dal icin ayri sutun okumak
    zorunda kalmasin."""
    return PurchaseRequestLineResponse(
        id=line.id,
        sort_order=line.sort_order,
        stock_item_id=line.stock_item_id,
        stock_item_code=None if item is None else item.code,
        free_text_name=line.free_text_name,
        free_text_unit=line.free_text_unit,
        name=item.name if item is not None else (line.free_text_name or ""),
        unit=item.unit if item is not None else line.free_text_unit,
        quantity=line.quantity,
        estimated_unit_price=line.estimated_unit_price,
        line_total=_line_total(line),
        # Katalogsuz kalemde bakiye YOKTUR (`null`); kartli kalemde hic hareket
        # gormemis kart 0 doner — "hic alinmadi" ile "stok karti yok" farkli.
        current_stock=None
        if line.stock_item_id is None
        else balances.get(line.stock_item_id, Decimal("0")),
    )


async def build_request_detail(
    session: AsyncSession, actor: User, request: PurchaseRequest
) -> PurchaseRequestResponse:
    """FST detay govdesi. UC sorgu kosar ve sayisi KALEM SAYISINDAN BAGIMSIZDIR
    (N+1 yok): kalemler+kartlar · bakiyeler · silme yetkisi."""
    lines = await repository.list_request_lines(session, request.id)
    item_ids = [line.stock_item_id for line, _ in lines if line.stock_item_id is not None]
    balances = await repository.current_stock_by_item(
        session, await _visible_project_ids(session, actor), item_ids
    )
    satirlar = [_to_line_response(line, item, balances) for line, item in lines]
    toplam = sum((s.line_total for s in satirlar if s.line_total is not None), Decimal("0"))

    return PurchaseRequestResponse(
        **_base_fields(request),
        estimated_total=toplam,
        can_delete=await can_delete_request(session, actor),
        lines=satirlar,
    )


def _base_fields(request: PurchaseRequest) -> dict:
    # `slug` LISTEDE DE ZORUNLUDUR: liste ucu link uretir ve URL-2'de
    # `SiteOptionListResponse`e slug EKLENMEDIGI icin secici slug uretememisti
    # (`routes.ts:34-45`de kural olarak yazili). Ayni tuzak tekrarlanmaz —
    # `_base_fields` hem listeyi hem detayi besledigi icin TEK yerde eklenir.
    return {
        "slug": url_safe_key(request.request_no),
        **{
            alan: getattr(request, alan)
            for alan in (
                "id",
                "request_no",
                "request_date",
                "priority",
                "project_id",
                "site_id",
                "section_id",
                "needed_by",
                "justification",
                "status",
                "quote_deadline",
                "approved_by_user_id",
                "approved_at",
                "rejected_at",
                "rejection_reason",
                "created_by_user_id",
                "created_at",
            )
        },
    }


async def list_requests(
    session: AsyncSession,
    actor: User,
    *,
    status: PurchaseRequestStatus | None,
    project_id: uuid.UUID | None,
    priority: PurchasePriority | None,
    q: str | None,
    limit: int,
    offset: int,
) -> PurchaseRequestListResponse:
    """SAT tablosunun veri kaynagi.

    Uc sorgu kosar ve sayisi SATIR SAYISINDAN BAGIMSIZDIR: sayfa (tahmini
    toplam ve kalem sayisi JOIN'li alt sorgudan) · sayim · aktorun izin
    seviyesi · gorunur projeler.
    """
    project_ids = await _visible_project_ids(session, actor)
    totals = repository.request_totals()
    suzgec = {"status": status, "project_id": project_id, "priority": priority, "q": q}

    rows = await repository.list_requests(
        session, project_ids, totals, limit=limit, offset=offset, **suzgec
    )
    total = await repository.count_requests(session, project_ids, **suzgec)
    can_delete = await can_delete_request(session, actor)

    return PurchaseRequestListResponse(
        items=[
            PurchaseRequestListRow(
                **_base_fields(row[0]),
                estimated_total=row.estimated_total,
                line_count=row.line_count,
                can_delete=can_delete,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )

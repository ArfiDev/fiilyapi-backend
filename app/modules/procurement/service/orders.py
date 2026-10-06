"""Siparis (SIP) — teklif secimiyle ATOMIK uretim + dogrudan siparis.

`select_and_order` burada durur cunku urettigi sey bir SIPARISTIR; teklif
tarafindan yalniz gorunurluk kapisini (`quotes._visible_quote`) odunc alir.
Ters yon YOKTUR: `quotes` bu dosyayi ithal etmez, cember olusmaz.

`delivered` damgasini stok girisi ATAR (§7 S4) — matris disi her hedef 409'dur.
"""

import uuid

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.modules.audit import messages
from app.modules.procurement import guards, numbering, repository, transitions
from app.modules.procurement.models import (
    PurchaseOrder,
    PurchaseOrderStatus,
    PurchaseRequest,
)
from app.modules.procurement.schemas import (
    PurchaseOrderCreate,
    PurchaseOrderListResponse,
    PurchaseOrderResponse,
    PurchaseOrderUpdate,
)
from app.modules.procurement.service.core import _visible_project_ids
from app.modules.procurement.service.quotes import _visible_quote
from app.modules.procurement.service.suppliers import get_supplier
from app.modules.users.models import User


async def select_and_order(
    session: AsyncSession, actor: User, request: PurchaseRequest, quote_id: uuid.UUID
) -> tuple[PurchaseOrderResponse, str]:
    """ "Siparis Ver": teklifi sec + siparisi uret + talebi `ordered` yap.

    ## Neden acik SAVEPOINT

    Uc yazma TEK ISLEMDIR ve arada bir hata cikarsa HICBIRI kalmamalidir:
    yarim kalmis bir "secili teklif" ekranda siparissiz gorunur ve o talep bir
    daha duzeltilemezdi (teklif yazimi artik `ordered`da kapali olurdu). Istek
    yasam dongusune GUVENILMEZ — `get_db` yalnizca uc bir ISTISNA hattina
    dusunce geri alir; servis ic bir hatayi yakalayip yanit uretmeye kalksa ya
    da cagiran baska bir baglamda (T4'un ST zinciri, toplu is) kossa atomiklik
    kaybolurdu. `begin_nested` bunu KODUN ozelligi yapar, hattin degil.

    ## Sira

    1. durum (409) ve teklif (404) — yazimdan ONCE,
    2. talebin toplam miktari (tutarin carpani),
    3. savepoint: rakip teklifleri sifirla → teklifi isaretle → talebi gecir,
    4. numara uretimi EN SONDA (`pg_advisory_xact_lock` bosuna tutulmasin) ve
       siparisin yazimi.
    """
    quote = await _visible_quote(session, request, quote_id)
    # Durum kontrolu MATRISTEDIR: `quote_wait` disi her durum 409 verir ve
    # ikinci bir `if` yazilmaz.
    quantity_total = await repository.request_quantity_total(session, request.id)
    total_amount = transitions.order_total_from_quote(
        quote.unit_price, quantity_total, quote.shipping_included, quote.shipping_cost
    )

    async with session.begin_nested():
        await transitions.apply_request_transition(
            session, actor, request, transitions.RequestAction.select_and_order
        )
        await repository.clear_selected_quotes(session, request.id, quote.id)
        quote.is_selected = True
        await session.flush()

        order = PurchaseOrder(
            order_no=await numbering.generate_order_number(session),
            request_id=request.id,
            quote_id=quote.id,
            supplier_id=quote.supplier_id,
            project_id=request.project_id,
            total_amount=total_amount,
            status=PurchaseOrderStatus.approved,
            created_by_user_id=actor.id,
        )
        session.add(order)
        await session.flush()

    return await _order_response(session, order.id), messages.purchase_order_created_from_quote(
        order.order_no, request.request_no
    )


async def _order_response(session: AsyncSession, order_id: uuid.UUID) -> PurchaseOrderResponse:
    row = await repository.get_order_row(session, order_id)
    if row is None:  # pragma: no cover - cagiranlar kaydi zaten cozmustur
        raise NotFoundError(guards.ORDER_MISSING)
    return _to_order_response(row)


def _to_order_response(row: Row) -> PurchaseOrderResponse:
    order: PurchaseOrder = row[0]
    return PurchaseOrderResponse(
        **{
            alan: getattr(order, alan)
            for alan in (
                "id",
                "order_no",
                "request_id",
                "quote_id",
                "supplier_id",
                "project_id",
                "total_amount",
                "expected_delivery",
                "status",
                "note",
                "created_by_user_id",
                "created_at",
            )
        },
        supplier_name=row.supplier_name,
        request_no=row.request_no,
    )


async def visible_order(session: AsyncSession, actor: User, order_id: uuid.UUID) -> PurchaseOrder:
    """Siparise tekil erisimin TEK kapisi. Gorunmeyen proje → **404**."""
    order = await repository.get_order(session, order_id)
    if order is None or order.project_id not in await _visible_project_ids(session, actor):
        raise NotFoundError(guards.ORDER_MISSING)
    return order


async def visible_order_locked(
    session: AsyncSession, actor: User, order_id: uuid.UUID
) -> PurchaseOrder:
    """Kapsam suzgeci + `SELECT … FOR UPDATE` — SIPARIS gecislerinin giris kapisi.

    `request_access.visible_request_locked`in siparis tarafindaki ikizi; ayni
    sozlesme, ayni sira. OKUMA uclari kilit ALMAZ (`visible_order` orada kalir):
    gereksiz satir kilidi listeleri ve detay okumalarini yazmalarin arkasinda
    bekletirdi.

    Kapsam karari (404) kilitten ONCE verilir: gorunmeyen bir kaydin satiri
    bosuna kilitlenmez ve govde var OLMAYAN kimliginkiyle BIREBIR AYNIDIR.

    🔴 KILIT SIRASI: siparis satiri -> (varsa) talep satiri. `stock_link.
    stamp_delivery` de bu sirayla ilerler. Ters yonde ilerleyen yol YOKTUR:
    `select_and_order` talebi kilitler ama siparisi YAZAR (yeni satir, cekisme
    olamaz), MEVCUT bir siparis satirini hicbir zaman kilitlemez.
    """
    order = await visible_order(session, actor, order_id)
    locked = await repository.get_order_locked(session, order.id)
    if locked is None:
        # Yarista silinmis olabilir — var olmayan kayitla AYNI 404.
        raise NotFoundError(guards.ORDER_MISSING)
    return locked


async def get_order_detail(
    session: AsyncSession, actor: User, order_id: uuid.UUID
) -> PurchaseOrderResponse:
    order = await visible_order(session, actor, order_id)
    return await _order_response(session, order.id)


async def list_orders(
    session: AsyncSession,
    actor: User,
    *,
    status: PurchaseOrderStatus | None,
    project_id: uuid.UUID | None,
    supplier_id: uuid.UUID | None,
    q: str | None,
    limit: int,
    offset: int,
) -> PurchaseOrderListResponse:
    """SIP tablosunun veri kaynagi — suzgecler AND'lidir.

    Uc sorgu kosar ve sayisi SATIR SAYISINDAN BAGIMSIZDIR (N+1 yok): gorunur
    projeler · sayfa (tedarikci adi ve talep numarasi JOIN'li) · sayim.
    """
    project_ids = await _visible_project_ids(session, actor, liste=True)
    suzgec = {"status": status, "project_id": project_id, "supplier_id": supplier_id, "q": q}
    rows = await repository.list_orders(session, project_ids, limit=limit, offset=offset, **suzgec)
    total = await repository.count_orders(session, project_ids, **suzgec)
    return PurchaseOrderListResponse(
        items=[_to_order_response(row) for row in rows], total=total, limit=limit, offset=offset
    )


async def create_order(
    session: AsyncSession, actor: User, data: PurchaseOrderCreate
) -> tuple[PurchaseOrderResponse, str]:
    """DOGRUDAN (talepsiz) siparis — §7 S3, SIP 35.

    `request_id` semada YOKTUR (govdede gonderilse Pydantic yok sayar): talebe
    bagli siparisin tek yolu `select-and-order`dir, aksi halde talebin durum
    makinesi atlanirdi.

    Sira: proje kapsami (404) → tedarikci (404) → numara → yazma. Numara EN
    SONDA uretilir (T2 kurali: danisma kilidi bosuna tutulmasin).
    """
    if data.project_id not in await _visible_project_ids(session, actor):
        raise NotFoundError(guards.REQUEST_PROJECT_INVALID)
    supplier = await get_supplier(session, data.supplier_id)

    order = PurchaseOrder(
        order_no=await numbering.generate_order_number(session),
        request_id=None,
        quote_id=None,
        supplier_id=supplier.id,
        project_id=data.project_id,
        total_amount=data.total_amount,
        expected_delivery=data.expected_delivery,
        status=PurchaseOrderStatus.approved,
        note=data.note,
        created_by_user_id=actor.id,
    )
    session.add(order)
    await session.flush()
    return await _order_response(session, order.id), messages.purchase_order_created(
        order.order_no, supplier.name
    )


async def update_order(
    session: AsyncSession, order: PurchaseOrder, data: PurchaseOrderUpdate
) -> tuple[PurchaseOrderResponse, str]:
    """Durum gecisi + duzeltilebilir alanlar.

    `status` GONDERILMEZSE gecis denetimi hic kosmaz: not/tarih duzeltmesi bir
    gecis DEGILDIR. Gonderildiyse matris karar verir (`transitions.
    assert_order_transition`) — `delivered` DAHIL her matris disi hedef 409'dur
    (teslim damgasini stok girisi atar, §7 S4).
    """
    verilen = data.model_dump(exclude_unset=True)
    hedef = verilen.get("status")
    if hedef is not None:
        transitions.assert_order_transition(order, hedef)
        order.status = hedef

    for alan in ("expected_delivery", "note"):
        if alan in verilen:
            setattr(order, alan, verilen[alan])

    await session.flush()
    return await _order_response(session, order.id), messages.purchase_order_updated(order.order_no)

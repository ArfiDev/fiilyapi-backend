"""Tedarikci katalogu (TED) — spec §2, §4, §5.

**Katalogda kapsam suzgeci YOKTUR** (paket docstring'i): tabloda `project_id`
kolonu bile yoktur, ayni "Demirsan A.S." her projede kullanilir. IDOR unutulmus
DEGILDIR — sonraki okuyucu buraya proje suzgeci EKLEMESIN.

**Ama kartin PARA turevi ("Bu Yil Toplam Siparis") KAPSAMLIDIR:** gorunmeyen
projenin siparisi tutara girmez. Turev alanlar model nesnesinden DEGIL sorgu
satirindan gelir.

DELETE ucu yoktur; kullanimdan kaldirma `is_active=false` PATCH'idir.
"""

import uuid

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.timezone import today
from app.modules.audit import messages
from app.modules.procurement import guards, repository
from app.modules.procurement.models import Supplier
from app.modules.procurement.schemas import (
    SupplierCard,
    SupplierCreate,
    SupplierResponse,
    SupplierUpdate,
)
from app.modules.procurement.service.core import _strip, _visible_project_ids
from app.modules.users.models import User


async def list_suppliers(
    session: AsyncSession,
    actor: User,
    *,
    q: str | None,
    category: str | None,
    is_active: bool | None,
    limit: int,
    offset: int,
) -> tuple[list[SupplierCard], int]:
    """TED kart izgarasinin veri kaynagi. **Katalogda kapsam suzgeci YOK**
    (modul docstring'i); kapsam yalniz PARA turevine uygulanir."""
    totals = repository.supplier_order_totals(
        await _visible_project_ids(session, actor, liste=True), today().year
    )
    rows = await repository.list_suppliers(
        session, totals, q=q, category=category, is_active=is_active, limit=limit, offset=offset
    )
    total = await repository.count_suppliers(session, q=q, category=category, is_active=is_active)
    return [_to_supplier_card(row) for row in rows], total


def _to_supplier_card(row: Row) -> SupplierCard:
    """`(Supplier, orders_total, orders_count)` uclusunu TED kartina cevirir.

    Turev alanlar kart nesnesinden DEGIL satirdan gelir — modelde karsiliklari
    yoktur ve olmamalidir (spec §2).
    """
    supplier: Supplier = row[0]
    return SupplierCard(
        **SupplierResponse.model_validate(supplier).model_dump(),
        orders_total_this_year=row.orders_total,
        orders_count_this_year=row.orders_count,
    )


async def get_supplier_card(
    session: AsyncSession, actor: User, supplier_id: uuid.UUID
) -> SupplierCard:
    """Detay ucu liste ile AYNI turetmeyi kullanir (`repository` gerekcesi)."""
    totals = repository.supplier_order_totals(
        await _visible_project_ids(session, actor, liste=True), today().year
    )
    row = await repository.get_supplier_with_totals(session, totals, supplier_id)
    if row is None:
        raise NotFoundError(guards.SUPPLIER_MISSING)
    return _to_supplier_card(row)


async def get_supplier(session: AsyncSession, supplier_id: uuid.UUID) -> Supplier:
    supplier = await repository.get_supplier(session, supplier_id)
    if supplier is None:
        raise NotFoundError(guards.SUPPLIER_MISSING)
    return supplier


async def create_supplier(session: AsyncSession, data: SupplierCreate) -> tuple[Supplier, str]:
    """Yeni tedarikci karti.

    **AD TEKILLIGI ZORLANMAZ** ve bu bilinclidir: "Demirsan A.S." ile "Demirsan
    Ltd." mesru sekilde iki ayri firmadir, ayni grubun iki sirketi de olabilir.
    `tax_no` da UNIQUE degildir (T1 karari: alan zorunlu bile degil, bosluk
    birakan kayitlarin cakismasi kullaniciyi kilitlerdi).
    """
    supplier = Supplier(
        name=data.name.strip(),
        category=_strip(data.category),
        tax_no=_strip(data.tax_no),
        phone=_strip(data.phone),
        payment_terms=data.payment_terms,
        is_active=data.is_active,
    )
    session.add(supplier)
    await session.flush()
    return supplier, messages.supplier_created(supplier.name)


async def update_supplier(
    session: AsyncSession, supplier_id: uuid.UUID, data: SupplierUpdate
) -> tuple[Supplier, str]:
    """Kismi guncelleme. Gonderilmeyen alan ile `null` gonderilen alan
    `exclude_unset` ile ayrilir: `category: null` etiketi SILER, hic
    gondermemek ona DOKUNMAZ (`StockItemUpdate` dersi).

    **KULLANIMDAN KALDIRMA DA BURADAN GECER** (`is_active: false`) — DELETE ucu
    yoktur (spec §4).
    """
    supplier = await get_supplier(session, supplier_id)
    verilen = data.model_dump(exclude_unset=True)
    for alan in ("name", "category", "tax_no", "phone"):
        if alan in verilen:
            verilen[alan] = _strip(verilen[alan])
    # `name` bosaltilamaz: sema `min_length=1` uygular, `_strip` ise sadece
    # bosluklu bir degeri `None`a cevirebilirdi — o durumda alan atlanir.
    if verilen.get("name") is None:
        verilen.pop("name", None)

    for alan, deger in verilen.items():
        setattr(supplier, alan, deger)
    await session.flush()
    return supplier, messages.supplier_updated(supplier.name)

"""Talep yazma yolu (SAT/FST) — olustur · guncelle · sil.

Uc ucun da ortak omurgasi: **dogrulamalarin HEPSI yazimdan ONCEDIR** ve
numara EN SONDA uretilir (`pg_advisory_xact_lock` islem boyu tutulur, dogrulama
basarisiz olacaksa kilit bosuna alinmaz).

Kapilar `request_access`tedir; burada `if status` YOKTUR.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import today
from app.modules.audit import messages
from app.modules.procurement import numbering, repository
from app.modules.procurement.models import (
    PurchaseRequest,
    PurchaseRequestLine,
    PurchaseRequestStatus,
)
from app.modules.procurement.schemas import (
    PurchaseRequestCreate,
    PurchaseRequestLineCreate,
    PurchaseRequestUpdate,
)
from app.modules.procurement.service.core import _strip
from app.modules.procurement.service.request_access import (
    _assert_draft,
    _assert_scope,
    _assert_stock_items_exist,
)
from app.modules.users.models import User


def _new_lines(
    request_id: uuid.UUID, lines: list[PurchaseRequestLineCreate]
) -> list[PurchaseRequestLine]:
    """Kalemleri govdedeki SIRAYLA kurar.

    `sort_order` DIZININ KENDISIDIR — istemci ayri bir alan gondermez (sema
    gerekcesi): gonderseydi cakisan ya da bosluklu siralar dogar ve sunucunun
    onlari yeniden numaralandirmasi gerekirdi. REPLACE yolu da bu fonksiyondan
    gectigi icin siralama iki yazma yolunda TEK kopyadir.
    """
    return [
        PurchaseRequestLine(
            request_id=request_id,
            stock_item_id=data.stock_item_id,
            free_text_name=_strip(data.free_text_name),
            free_text_unit=_strip(data.free_text_unit),
            quantity=data.quantity,
            estimated_unit_price=data.estimated_unit_price,
            sort_order=sira,
        )
        for sira, data in enumerate(lines)
    ]


async def create_request(
    session: AsyncSession, actor: User, data: PurchaseRequestCreate
) -> tuple[PurchaseRequest, str]:
    """Baslik + kalemler ATOMIK yazilir: dogrulamalarin HEPSI yazimdan ONCEDIR.

    Sira bilinclidir:
      1. XOR / miktar / uzunluk — semada cozulur, DB'ye hic dokunulmaz (**422**);
      2. kapsam: proje · santiye · bolum (**404**, `_assert_scope`);
      3. kalemlerin stok kartlari (**404**, tek toplu sorgu);
      4. ancak bundan sonra numara uretimi ve `session.add`.

    Numara EN SONDA uretilir: `pg_advisory_xact_lock` islem boyu tutulur ve
    dogrulama basarisiz olacaksa kilidi bosuna almamak gerekir.

    **DURUM HER ZAMAN `draft`tir** — gecisler T3'undur.
    """
    await _assert_scope(session, actor, data.project_id, data.site_id, data.section_id)
    await _assert_stock_items_exist(session, data.lines)

    request_no = await numbering.generate_request_number(session)
    request = PurchaseRequest(
        request_no=request_no,
        request_date=data.request_date or today(),
        priority=data.priority,
        project_id=data.project_id,
        site_id=data.site_id,
        section_id=data.section_id,
        needed_by=data.needed_by,
        justification=data.justification,
        status=PurchaseRequestStatus.draft,
        quote_deadline=data.quote_deadline,
        created_by_user_id=actor.id,
    )
    session.add(request)
    await session.flush()

    if data.lines:
        session.add_all(_new_lines(request.id, data.lines))
        await session.flush()

    return request, messages.purchase_request_created(request.request_no)


async def update_request(
    session: AsyncSession, actor: User, request: PurchaseRequest, data: PurchaseRequestUpdate
) -> tuple[PurchaseRequest, str]:
    """YALNIZ taslakta (409 aksi halde). Kalemler gonderilirse REPLACE edilir.

    Kapsam UCLUSU (proje · santiye · bolum) BIRLIKTE dogrulanir: kullanici
    yalniz projeyi degistirse bile eski `site_id` yeni projeye ait olmayabilir
    ve talep sessizce tutarsiz kalirdi. Bu yuzden dogrulama, gonderilen ve
    mevcut degerlerin BIRLESIMI uzerinde kosar.
    """
    _assert_draft(request)
    verilen = data.model_dump(exclude_unset=True)

    project_id = verilen.get("project_id", request.project_id)
    site_id = verilen.get("site_id", request.site_id)
    section_id = verilen.get("section_id", request.section_id)
    await _assert_scope(session, actor, project_id, site_id, section_id)

    if data.lines is not None:
        await _assert_stock_items_exist(session, data.lines)

    # `project_id`/`priority`/`request_date` NOT NULL kolonlardir: `null`
    # gonderilirse mevcut deger KORUNUR (sema hepsini `| None` yazar cunku PATCH
    # govdesi kismidir). Geri kalan alanlar nullable'dir ve `null` onlari SILER.
    for alan in ("project_id", "priority", "request_date"):
        if verilen.get(alan) is not None:
            setattr(request, alan, verilen[alan])
    for alan in ("site_id", "section_id", "needed_by", "justification", "quote_deadline"):
        if alan in verilen:
            setattr(request, alan, verilen[alan])

    if data.lines is not None:
        for eski in await repository.load_request_lines(session, request.id):
            await session.delete(eski)
        await session.flush()
        if data.lines:
            session.add_all(_new_lines(request.id, data.lines))
        await session.flush()

    await session.flush()
    return request, messages.purchase_request_updated(request.request_no)


async def delete_request(session: AsyncSession, actor: User, request: PurchaseRequest) -> str:
    """Yalniz taslak talep silinir (409). Yetki kapisi router'dadir: YALNIZ Sistem Yoneticisi
    (SIL-B1, K4); "kendi taslagini sahibi siler" istisnasi KALDIRILDI.

    Kalemler `ON DELETE CASCADE` ile gider (T1 semasi). Denetim metni satir YOK
    OLMADAN once kurulur (`warehouse_deleted` dersi).
    """
    _assert_draft(request)
    detail = messages.purchase_request_deleted(request.request_no)
    await session.delete(request)
    await session.flush()
    return detail

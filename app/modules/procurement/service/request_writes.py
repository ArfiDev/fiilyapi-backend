"""Talep yazma yolu (SAT/FST) — olustur · guncelle · sil.

Uc ucun da ortak omurgasi: **dogrulamalarin HEPSI yazimdan ONCEDIR** ve
numara EN SONDA uretilir (`pg_advisory_xact_lock` islem boyu tutulur, dogrulama
basarisiz olacaksa kilit bosuna alinmaz).

Kapilar `request_access`tedir; burada `if status` YOKTUR.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ProcurementValidationError
from app.core.timezone import today
from app.modules.audit import messages
from app.modules.procurement import guards, numbering, repository
from app.modules.procurement.models import (
    PurchaseRequest,
    PurchaseRequestLine,
    PurchaseRequestStatus,
)
from app.modules.procurement.schemas import (
    PurchaseRequestCreate,
    PurchaseRequestLineCreate,
    PurchaseRequestLineUpdate,
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


def _birlestir_satir(
    mevcut: PurchaseRequestLine, data: PurchaseRequestLineUpdate, sira: int
) -> None:
    """`id` ile eslesen satira SADECE gonderilen alanlari yazar (satir kimligi korunur).

    Kalem kaynagi degisirse (stok karti <-> serbest tanim) karsi taraf temizlenir; birlesik
    sonuc yine XOR'a uymak zorundadir (`ProcurementValidationError`, 422).
    """
    verilen = data.model_fields_set
    if data.stock_item_id is not None and "free_text_name" not in verilen:
        mevcut.free_text_name = None
    if data.stock_item_id is not None and "free_text_unit" not in verilen:
        mevcut.free_text_unit = None
    if "stock_item_id" in verilen:
        mevcut.stock_item_id = data.stock_item_id
    elif {"free_text_name", "free_text_unit"} & verilen and (
        data.free_text_name is not None or data.free_text_unit is not None
    ):
        mevcut.stock_item_id = None
    if "free_text_name" in verilen:
        mevcut.free_text_name = _strip(data.free_text_name)
    if "free_text_unit" in verilen:
        mevcut.free_text_unit = _strip(data.free_text_unit)
    if "quantity" in verilen and data.quantity is not None:
        mevcut.quantity = data.quantity
    if "estimated_unit_price" in verilen:
        mevcut.estimated_unit_price = data.estimated_unit_price
    mevcut.sort_order = sira
    if mevcut.stock_item_id is not None:
        serbest_dolu = mevcut.free_text_name is not None or mevcut.free_text_unit is not None
        if serbest_dolu:
            raise ProcurementValidationError(guards.REQUEST_LINE_SOURCE_CONFLICT)
    elif mevcut.free_text_name is None or mevcut.free_text_unit is None:
        raise ProcurementValidationError(guards.REQUEST_LINE_SOURCE_CONFLICT)


async def _satirlari_degistir(
    session: AsyncSession, request: PurchaseRequest, lines: list[PurchaseRequestLineUpdate]
) -> None:
    """`lines` REPLACE + satir bazinda kismi birlestirme (IZN-B4d onarimi).

    `id`li satir TALEBIN mevcut satirlariyla eslesmek zorundadir (yabanci/olmayan `id` ->
    404, ayni `id` iki kez -> 422); eslesen satir YERINDE guncellenir, `id`siz satir yenidir,
    govdede olmayan eski satir silinir. `sort_order` govdedeki sira olur.
    """
    mevcutlar = {s.id: s for s in await repository.load_request_lines(session, request.id)}
    gelen_idler = [d.id for d in lines if d.id is not None]
    if len(set(gelen_idler)) != len(gelen_idler):
        raise ProcurementValidationError(guards.REQUEST_LINE_DUPLICATE)
    if any(i not in mevcutlar for i in gelen_idler):
        raise NotFoundError(guards.REQUEST_LINE_INVALID)
    for eski_id, eski in mevcutlar.items():
        if eski_id not in gelen_idler:
            await session.delete(eski)
    yeniler: list[PurchaseRequestLine] = []
    for sira, data in enumerate(lines):
        if data.id is None:
            yeniler.append(_yeni_satir(request.id, data, sira))
        else:
            _birlestir_satir(mevcutlar[data.id], data, sira)
    await session.flush()
    if yeniler:
        session.add_all(yeniler)
        await session.flush()


def _yeni_satir(
    request_id: uuid.UUID, data: PurchaseRequestLineUpdate, sira: int
) -> PurchaseRequestLine:
    return PurchaseRequestLine(
        request_id=request_id,
        stock_item_id=data.stock_item_id,
        free_text_name=_strip(data.free_text_name),
        free_text_unit=_strip(data.free_text_unit),
        quantity=data.quantity,
        estimated_unit_price=data.estimated_unit_price,
        sort_order=sira,
    )


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
    """YALNIZ taslakta (409 aksi halde). Kalemler gonderilirse REPLACE edilir; `id`li satir
    kismi birlestirilir (gonderilmeyen alan korunur, bkz. `_satirlari_degistir`).

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
        # Yalniz `stock_item_id` okunur (ördek tipleme); servis yuzeyi anlik goruntusu icin imza
        # `PurchaseRequestLineCreate` kalir.
        await _assert_stock_items_exist(session, data.lines)  # type: ignore[arg-type]

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
        await _satirlari_degistir(session, request, data.lines)

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

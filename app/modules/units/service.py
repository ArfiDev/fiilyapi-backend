"""Blok/unite okuma uclari ve TEKIL yazma uclari (spec §7.1-§7.6, §7.9).

Toplu yollar (`bulk`, `import`, `allocation`) `batch.py`'dedir; ortak korkuluklar
ve Turkce hata metinleri `guards.py`'de, saf toplama/sunum `summary.py`'dedir.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit import messages
from app.modules.projects import costs
from app.modules.projects import repository as projects_repository
from app.modules.projects.models import Project
from app.modules.sites import repository as sites_repository

# Gorunurluk suzgeci P1'DEN GELIR (spec §8): kopya bir erisim mantigi YAZILMAZ.
# `guards` uzerinden yeniden disa acilir — mevcut cagiranlar `service.visible_projects`
# adini kullanir ve bu ad korunur.
from app.modules.units import codes, guards, repository
from app.modules.units.guards import visible_projects
from app.modules.units.models import Block, Unit, UnitKind, UnitSalesStatus
from app.modules.units.schemas import (
    BLOCK_FORM_FIELDS,
    UNIT_FORM_FIELDS,
    BlockCreate,
    BlockListResponse,
    BlockResponse,
    BlockUpdate,
    UnitBlockGroup,
    UnitCreate,
    UnitListResponse,
    UnitOwnerSideFilter,
    UnitResponse,
    UnitUpdate,
)
from app.modules.units.summary import (
    VALUE_BASIS_BY_TYPE,
    UnitCostInfo,
    UnitSaleInfo,
    to_block,
    to_unit,
    totals,
)
from app.modules.users.models import User

# `Block`'un NOT NULL sutunlari: PATCH'te `null` ile bosaltilamazlar.
_BLOCK_NOT_NULL_FIELDS = ("name", "sort_order")

__all__ = [
    "block_response",
    "create_block",
    "create_unit",
    "list_blocks",
    "list_units",
    "to_block",
    "to_unit",
    "unit_response",
    "units_for_project",
    "update_block",
    "update_unit",
    "visible_projects",
]


# --- Denetim gunlugu (spec §9) ---
#
# Yazma fonksiyonlari sonucun YANINDA hazir denetim METNINI dondurur; satiri
# `record_audit` ile yazan yine router'dir (B5 deseni). Metin neden router'da
# KURULMUYOR: (1) SILME uclarinda proje/blok/unite adlari kayit yok olmadan
# ONCE okunmak zorundadir — router silme sonrasi onlari hicbir sorguyla geri
# getiremez; (2) adlar zaten gorunurluk cozumu sirasinda elde olur, router'da
# yeniden kurmak her yazma ucuna fazladan SELECT eklerdi. Metinlerin kendisi
# `audit/messages.py`'de MERKEZIDIR — f-string servise de gomulmez (P4 T7).


# --- Okuma uclari (spec §7.1, §7.4) ---


async def _blocks_with_units(
    session: AsyncSession, project_id: uuid.UUID
) -> tuple[list[tuple[Block, str]], dict[uuid.UUID, list[Unit]], list[Unit]]:
    """Bloklar + unitelerin bloklara dagilmis hâli + duz unite listesi.

    Uniteler TEK sorguda cekilir (repository notu); dagitim Python'dadir.
    """
    blocks = await repository.list_blocks_for_project(session, project_id)
    units = await repository.list_units_for_project(session, project_id)
    by_block: dict[uuid.UUID, list[Unit]] = {block.id: [] for block, _ in blocks}
    for unit in units:
        by_block.setdefault(unit.block_id, []).append(unit)
    return blocks, by_block, units


async def list_blocks(
    session: AsyncSession, actor: User, project_id: uuid.UUID
) -> BlockListResponse:
    """Spec §7.1. Blok seciciler (unite formu, toplu uretim formu) bu ucu kullanir."""
    await guards.visible_project(session, actor, project_id)
    blocks, by_block, _ = await _blocks_with_units(session, project_id)
    return BlockListResponse(
        blocks=[to_block(block, site_name, by_block[block.id]) for block, site_name in blocks]
    )


def _matches(
    unit: Unit,
    kind: UnitKind | None,
    owner_side: UnitOwnerSideFilter | None,
    floor: str | None,
    sales_status: UnitSalesStatus | None,
) -> bool:
    if kind is not None and unit.unit_kind is not kind:
        return False
    # Kat METINDIR (karar 4) → TAM ESLESME. Parcali eslesme "3" ile "3. Kat"i
    # birbirine karistirirdi ve bu sessiz bir veri karisikligi olurdu.
    if floor is not None and unit.floor != floor:
        return False
    if sales_status is not None and unit.sales_status is not sales_status:
        return False
    if owner_side is None:
        return True
    if owner_side is UnitOwnerSideFilter.unassigned:
        return unit.owner_side is None
    return unit.owner_side is not None and unit.owner_side.value == owner_side.value


async def list_units(
    session: AsyncSession,
    actor: User,
    project_id: uuid.UUID,
    *,
    block_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    kind: UnitKind | None = None,
    owner_side: UnitOwnerSideFilter | None = None,
    floor: str | None = None,
    sales_status: UnitSalesStatus | None = None,
) -> UnitListResponse:
    """Spec §7.4. Suzgecler YALNIZ listeyi daraltir; `totals` daima projenin
    tamamini sayar. `site_id` suzgeci blok uzerinden calisir — `units`'te
    `site_id` sutunu YOKTUR (spec §4.0). Unitesi olmayan blok listede KALIR."""
    project = await guards.visible_project(session, actor, project_id)
    return await units_for_project(
        session,
        project,
        block_id=block_id,
        site_id=site_id,
        kind=kind,
        owner_side=owner_side,
        floor=floor,
        sales_status=sales_status,
    )


async def units_for_project(
    session: AsyncSession,
    project: Project,
    *,
    block_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    kind: UnitKind | None = None,
    owner_side: UnitOwnerSideFilter | None = None,
    floor: str | None = None,
    sales_status: UnitSalesStatus | None = None,
) -> UnitListResponse:
    """`list_units`'in gorunurluk kapisi COZULDUKTEN sonraki cekirdegi.

    P9 T4: Excel ucu projeyi dosya adi icin ZATEN cozmek zorundadir; ayni
    kapiyi ikinci kez calmak yerine cozulmus projeyi buraya verir
    (`timesheet`in `visible_site` → `matrix.build` deseni). Zarf tek noktada
    kuruldugu icin ekran ile dosya AYRISAMAZ.
    """
    project_id = project.id
    blocks, by_block, units = await _blocks_with_units(session, project_id)
    basis = VALUE_BASIS_BY_TYPE[project.project_type]
    # P8 T5: satis fiyati/alicisi (KY 275/277) ve ciro (KY 93/267) TEK sorgudan
    # gelir — unite basina SELECT atmak 24 daireli blokta N+1 demekti.
    sales_by_unit = await _open_sales_by_unit(session, project_id)
    # P9 T3 (KKP 91): hissedar adlari EK SORGU ACMADAN gelir — `project` zaten
    # gorunurluk sorgusuyla yuklendi ve `Project.shareholders` `lazy="selectin"`
    # oldugu icin koleksiyon elimizdedir. `sales_by_unit` ile ayni gerekce:
    # unite basina `session.get(LandShareShareholder, ...)` 24 daireli blokta 24
    # gidis-donus demekti (spec §4.3 "N+1 yok").
    shareholder_names = _shareholder_names(project)
    # P10 T3 (UE 91/97-99): maliyet dagitiminin paydasi projenin TUM uniteleridir
    # (`units` degiskeni) — suzgecten ETKILENMEZ, aksi hâlde "3. kat" suzgeci
    # ayni dairenin maliyetini degistirirdi (`totals` kuralinin aynisi).
    allocation = costs.allocation(project, units)

    selected = [
        (block, site_name)
        for block, site_name in blocks
        if (block_id is None or block.id == block_id)
        and (site_id is None or block.site_id == site_id)
    ]
    groups = [
        UnitBlockGroup(
            block=to_block(block, site_name, by_block[block.id]),
            units=[
                to_unit(
                    unit,
                    block.name,
                    sales_by_unit.get(unit.id),
                    _shareholder_name(shareholder_names, unit),
                    _cost_info(allocation, unit),
                )
                for unit in by_block[block.id]
                if _matches(unit, kind, owner_side, floor, sales_status)
            ],
        )
        for block, site_name in selected
    ]
    return UnitListResponse(totals=totals(units, basis, sales_by_unit), blocks=groups)


# --- Yanit zarflari ---


async def block_response(session: AsyncSession, block: Block) -> BlockResponse:
    site = await sites_repository.get_site(session, block.site_id)
    units = await repository.list_units_for_block(session, block.id)
    # `site` None olamaz: `site_id` NOT NULL + FK. Kosul yalnizca tip
    # daraltmasi icindir, sessiz bir dusus degil.
    return to_block(block, site.name if site is not None else "", units)


async def _block_name(session: AsyncSession, block_id: uuid.UUID) -> str:
    """Denetim metni ve yanit zarfi icin blok adi. Blok yetki/dogrulama sirasinda
    zaten yuklendigi icin bu cagri kimlik haritasindan doner, ek sorgu uretmez.

    `block` None olamaz: `block_id` NOT NULL + FK. Kosul yalnizca tip
    daraltmasi icindir, sessiz bir dusus degil.
    """
    block = await repository.get_block(session, block_id)
    return block.name if block is not None else ""


async def _open_sales_by_unit(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[uuid.UUID, UnitSaleInfo]:
    """P8 T5: `unit_id` → acik satis kaydinin sunum verisi.

    Sozluk ORM nesnesi DEGIL `UnitSaleInfo` tasir (bkz. `summary.py`): saf
    toplama cekirdegi `sales` ORM'una baglanmaz.
    """
    return {
        sale.unit_id: UnitSaleInfo(
            sale_price=sale.sale_price, customer_name=customer.name, status=sale.status
        )
        for sale, customer in await repository.list_open_sales_for_project(session, project_id)
    }


def _cost_info(allocation: costs.UnitCostAllocation, unit: Unit) -> UnitCostInfo:
    """UE 91/97-99 — unitenin maliyet/kâr ikilisi, EK SORGU ACMADAN.

    Dagitim baglami (proje bütce maliyeti + toplam brüt m²) istek basina BIR KEZ
    kurulur ve her unite icin yeniden hesaplanmaz (`_open_sales_by_unit` ile ayni
    N+1 gerekcesi). Hesabin kendisi `projects.costs`tadir; burada ikinci bir
    dagitim formulu YAZILMAZ.
    """
    return UnitCostInfo(
        cost=allocation.for_unit(unit.gross_area_m2),
        expected_profit=allocation.expected_profit(unit.gross_area_m2, unit.list_price),
    )


def _shareholder_names(project: Project) -> dict[uuid.UUID, str]:
    """`shareholder_id` → ad sozlugu, ZATEN yuklu koleksiyondan (spec §4.3)."""
    return {row.id: row.name for row in project.shareholders}


def _shareholder_name(names: dict[uuid.UUID, str], unit: Unit) -> str | None:
    """Hissedari olmayan unitede `None` — sozlukte aranmaz bile."""
    return None if unit.shareholder_id is None else names.get(unit.shareholder_id)


async def unit_response(session: AsyncSession, unit: Unit) -> UnitResponse:
    """Tekil yanit da satis/hissedar bilgisini LISTEYLE AYNI kaynaktan alir —
    POST/PATCH sonrasi ekranin gordugu satir listedekiyle ayrisamaz."""
    row = await repository.get_open_sale_for_unit(session, unit.id)
    sale = (
        UnitSaleInfo(sale_price=row[0].sale_price, customer_name=row[1].name, status=row[0].status)
        if row is not None
        else None
    )
    # Tek satirlik yanitta projenin TUM hissedarlarini cekmenin anlami yok; ad
    # yalnizca atama VARSA sorulur (`get_open_sale_for_unit` ile ayni desen).
    shareholder_name = (
        None
        if unit.shareholder_id is None
        else await repository.get_shareholder_name(session, unit.shareholder_id)
    )
    return to_unit(
        unit,
        await _block_name(session, unit.block_id),
        sale,
        shareholder_name,
        await _cost_allocation_info(session, unit),
    )


async def _cost_allocation_info(session: AsyncSession, unit: Unit) -> UnitCostInfo:
    """TEKIL yanitin maliyet ikilisi (P10 T3).

    Burada IKI ek sorgu kacinilmazdir: dagitimin paydasi projenin TUM unitelerinin
    brüt m² toplamidir (S3) ve tekil yanitta ne proje ne o liste elimizde olur.
    Bedeli bilincli kabul edilir — alternatifi, POST/PATCH sonrasi ekranin
    listedekinden FARKLI (bos) bir maliyet gormesiydi. Bu bir SATIR yanitidir,
    liste degil; sorgular unite sayisiyla BUYUMEZ.
    """
    project = await projects_repository.get_project(session, unit.project_id)
    if project is None:  # pragma: no cover - FK garantisi
        return UnitCostInfo(cost=None, expected_profit=None)
    units = await repository.list_units_for_project(session, unit.project_id)
    return _cost_info(costs.allocation(project, units), unit)


# --- Blok yazma uclari (spec §7.2, §7.3) ---


async def _resolve_block_code(
    session: AsyncSession,
    project_id: uuid.UUID,
    code: str | None,
    name: str,
    exclude_block_id: uuid.UUID | None = None,
) -> str:
    """Spec §3.2. Elle girilen kod AYNEN kabul edilir (yalniz benzersizligi
    dogrulanir → 409); bos birakilirsa addan URETILIR.

    Uretim TEK YERDEDIR: yazma yolu. Okuma yolunda gizli bir geri dusus yoktur
    ve canli bloklara `UPDATE` yazan bir veri migration'i da yoktur (karar 8).
    """
    if code:
        await guards.ensure_block_code_unique(session, project_id, code, exclude_block_id)
        return code
    taken = await repository.project_block_codes(session, project_id)
    return codes.resolve_block_code(name, taken)


async def create_block(
    session: AsyncSession, actor: User, project_id: uuid.UUID, data: BlockCreate
) -> tuple[Block, str]:
    project = await guards.visible_project(session, actor, project_id)
    site = await guards.resolve_site(session, project.id, data.site_id)
    await guards.ensure_block_name_unique(session, project.id, data.name)
    form = data.model_dump(include=set(BLOCK_FORM_FIELDS))
    form["code"] = await _resolve_block_code(session, project.id, data.code, data.name)
    block = Block(
        project_id=project.id,
        site_id=site.id,
        name=data.name,
        sort_order=data.sort_order,
        **form,
    )
    session.add(block)
    await session.flush()
    await session.refresh(block)
    return block, messages.block_created(project.name, block.name)


async def update_block(
    session: AsyncSession, actor: User, block_id: uuid.UUID, data: BlockUpdate
) -> tuple[Block, str]:
    """Spec §7.3. `site_id` degistirilebilir (blok yanlis santiyeye acilmissa);

    yeni santiye AYNI projede olmali, degilse **404** (spec §4.5 son paragrafi ve
    plan B5 test 13). Spec §7.3'un hata listesinde bu durum icin "422" yaziyor —
    §4.5 ve plan 404 dedigi icin 404 uygulanmistir.
    """
    block, project = await guards.visible_block(session, actor, block_id)
    updates = data.model_dump(exclude_unset=True)
    if updates.get("site_id") is not None:
        site = await guards.resolve_site(session, project.id, updates["site_id"])
        updates["site_id"] = site.id
    else:
        # `site_id: null` gonderimi sutunu bosaltamaz (NOT NULL) — yok sayilir.
        updates.pop("site_id", None)
    if updates.get("name") is not None:
        await guards.ensure_block_name_unique(session, project.id, updates["name"], block.id)
    if updates.get("code"):
        await guards.ensure_block_code_unique(session, project.id, updates["code"], block.id)
    for field, value in updates.items():
        # NOT NULL sutunlar `null` ile bosaltilamaz; nullable olanlar bosalir
        # (`update_unit` ile ayni ayrim — BE 102 "Not" alani temizlenebilmeli).
        if value is None and field in _BLOCK_NOT_NULL_FIELDS:
            continue
        setattr(block, field, value)
    if not block.code:
        # Karar 8: canli bloklarin kodu NULL dogar ve ILK PATCH'te uretilir —
        # backfill migration'i YOKTUR (spec §3.2).
        block.code = await _resolve_block_code(session, project.id, None, block.name)
    await session.flush()
    await session.refresh(block)
    return block, messages.block_updated(project.name, block.name)


# --- Unite yazma uclari (spec §7.5, §7.6, §3.3) ---


async def create_unit(
    session: AsyncSession, actor: User, project_id: uuid.UUID, data: UnitCreate
) -> tuple[Unit, str]:
    """Spec §7.5. `taahhut` projede unite tanimlamak SERBEST (§3.3 — kisit icat
    edilmez); iki fiyat sutunu da her tipte kabul edilir (§4.4)."""
    project = await guards.visible_project(session, actor, project_id)
    block = await guards.block_in_project(session, project, data.block_id)
    guards.ensure_owner_side_allowed(project, data.owner_side)
    guards.ensure_opening_sales_status(data.sales_status)
    guards.ensure_net_le_gross(data.gross_area_m2, data.net_area_m2)
    await guards.ensure_unit_no_unique(session, block.id, data.unit_no)
    unit = Unit(
        project_id=project.id,
        block_id=block.id,
        unit_no=data.unit_no,
        unit_kind=data.unit_kind,
        layout=data.layout,
        gross_area_m2=data.gross_area_m2,
        net_area_m2=data.net_area_m2,
        list_price=data.list_price,
        appraisal_value=data.appraisal_value,
        owner_side=data.owner_side,
        sort_order=data.sort_order,
        **data.model_dump(include=set(UNIT_FORM_FIELDS)),
    )
    session.add(unit)
    await session.flush()
    await session.refresh(unit)
    return unit, messages.unit_created(project.name, block.name, unit.unit_no)


async def update_unit(
    session: AsyncSession, actor: User, unit_id: uuid.UUID, data: UnitUpdate
) -> tuple[Unit, str]:
    """Spec §7.6. `exclude_unset` ayrimi kritiktir: GONDERILMEYEN alan degismez,
    `null` GONDERILEN alan bosalir (P1/P2/P4 deseni).

    Kurallar birlesmis (mevcut + gonderilen) deger uzerinde yeniden calisir:
    yalniz `net_area_m2` gonderen bir istek, mevcut `gross_area_m2` ile
    karsilastirilmazsa DB CHECK'ine duser ve kullanici anlamsiz bir 409 gorur.
    """
    unit, project = await guards.visible_unit(session, actor, unit_id)
    updates = data.model_dump(exclude_unset=True)

    target_block_id = unit.block_id
    if "block_id" in updates and updates["block_id"] is not None:
        target_block_id = (await guards.block_in_project(session, project, updates["block_id"])).id
    updates.pop("block_id", None)  # NOT NULL: `null` gonderimi sutunu bosaltamaz

    owner_side = updates["owner_side"] if "owner_side" in updates else unit.owner_side
    guards.ensure_owner_side_allowed(project, owner_side)
    gross = updates["gross_area_m2"] if "gross_area_m2" in updates else unit.gross_area_m2
    net = updates["net_area_m2"] if "net_area_m2" in updates else unit.net_area_m2
    guards.ensure_net_le_gross(gross, net)

    target_unit_no = updates.get("unit_no") or unit.unit_no
    if target_block_id != unit.block_id or target_unit_no != unit.unit_no:
        await guards.ensure_unit_no_unique(session, target_block_id, target_unit_no, unit.id)

    unit.block_id = target_block_id
    for field, value in updates.items():
        # NOT NULL sutunlar `null` ile bosaltilamaz; nullable olanlar bosaltilir.
        if value is None and field in ("unit_no", "unit_kind", "sort_order"):
            continue
        setattr(unit, field, value)
    await session.flush()
    await session.refresh(unit)
    return unit, messages.unit_updated(
        project.name, await _block_name(session, unit.block_id), unit.unit_no
    )

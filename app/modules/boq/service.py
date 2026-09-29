import uuid
from collections.abc import Iterable
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import (
    UNRESTRICTED,
    DisciplineScope,
    visible_group_set,
    visible_item_set,
)
from app.core.errors import (
    BoqGroupSiteMismatchError,
    ConflictError,
    DuplicateError,
    NotFoundError,
    RelatedRecordsExistError,
    SiteValidationError,
)
from app.core.permissions import can_read
from app.modules.boq import progress, repository
from app.modules.boq.models import BoqGroup, BoqItem, BoqItemSectionAllocation
from app.modules.boq.schemas import (
    BoqGroupCreate,
    BoqGroupResponse,
    BoqGroupUpdate,
    BoqItemAllocation,
    BoqItemAllocationsReplace,
    BoqItemAllocationsResponse,
    BoqItemCreate,
    BoqItemResponse,
    BoqItemUpdate,
    BoqListResponse,
    BoqTotals,
    MetricPlaceholder,
    quantize_money,
    quantize_quantity,
)

# Gorunurluk suzgeci P2'DEN GELIR (plan T3 notu): site->proje cozumu kopyalanmaz,
# `sites.service._visible_site` yeniden kullanilir. Ayni desen zaten
# `projects.service`'in `sites.service._next_site_code`'u yeniden
# kullanmasinda var (bkz. app/modules/projects/service.py).
from app.modules.projects.schemas import metric, restricted
from app.modules.sites import guards as sites_guards
from app.modules.sites import repository as sites_repository
from app.modules.sites.models import Section, Site
from app.modules.sites.service import _visible_site
from app.modules.users.models import User

# Spec §5.4 Turkce hata metinleri. Grup/kalem "yok" ile "gorunmuyor" ayni
# mesaji doner (P2 §5.2 deseninin devami, bkz. sites.service _visible_section) —
# kaydin varligi sizdirilmaz.
_GROUP_MISSING = "İş kalemi grubu bulunamadı"
_ITEM_MISSING = "İş kalemi bulunamadı"
_GROUP_SITE_MISMATCH = "Grup bu şantiyeye ait değil"
_DUPLICATE_CODE = "Bu poz numarası bu şantiyede zaten kullanılıyor"
# TB3-C: kalemi olan grup silinemez. `contracts/guards.py.GROUP_HAS_ITEMS`
# deseninin aynısı — metinde ADET VERİLMEZ, eyleme dönüktür.
_GROUP_HAS_ITEMS = "Bu grupta iş kalemi var, önce kalemleri silin"
_ITEM_HAS_DIARY_LINES = (
    "Bu kaleme yazılmış günlük kayıt satırları var, önce o günlerden kalemi çıkarın"
)
# --- BOQ-SEC (bölüm tahsisi) mesajları ---
_ALLOCATION_EXCEEDS_QUANTITY = "Bölümlere dağıtılan miktar poz miktarını aşamaz"
_ALLOCATION_DUPLICATE_SECTION = "Aynı bölüm gövdede birden fazla kez gönderildi"
_QUANTITY_BELOW_ALLOCATED = "Poz miktarı bölümlere dağıtılan toplamın altına indirilemez"

# B6 sozlesmesindeki `pending_module` anahtarlari (kullaniciya gosterilecek
# metin DEGILDIR).
#
# 🔴 P-YT3 DENETIMI (2026-08-23): ikisi de CANLI bir izin modulunu adlandirir.
# Yani zarfin ilk tasarimindaki *"bu modul henuz yok"* anlami burada ARTIK
# GECERLI DEGIL — bekleyen sey modul degil, o modulun VERISINI bu ucta basma
# IZNIDIR (K4). Gerekcenin tamami `schemas.BoqTotals` docstring'indedir;
# ayrismanin bugunku hâli `test_boq_pyt3_yer_tutucu_denetimi.py::test_K4_*`
# ile ISIMLE cakilidir.
_CONTRACTS = "contracts"
_PROGRESS_PAYMENTS = "progress_payments"
# ILR-1: FIZIKSEL ilerlemenin sahibi gunluktur (isveren hakedisi DEGIL).
_SITE_DIARY = "site_diary"


def _metric(pending_module: str) -> MetricPlaceholder:
    return MetricPlaceholder(pending_module=pending_module)


def _progress_metric(
    realized: Decimal | None, taban: Decimal, *, izinli: bool
) -> MetricPlaceholder:
    """ILR-1 zarfi — UC hâl, biri IZIN (K-ZARF).

    🔴 `izinli=False` ⇒ `restricted()`: `pending_module` TASIMAZ. `site_diary`
    yazmak, "yetkin yok"u "modul bekleniyor" diye gostermek olurdu; ekran
    yalan soylerdi (kullanici karari 2026-08-27).
    """
    if not izinli:
        return restricted()
    return metric(progress.weighted_pct(realized or _ZERO_QUANTITY, taban), _SITE_DIARY)


_ZERO_QUANTITY = Decimal("0.000")


def to_item(
    item: BoqItem,
    *,
    allocated: Decimal,
    quantity: Decimal | None = None,
    realized: Decimal | None = None,
    izinli: bool = False,
) -> BoqItemResponse:
    """`allocated` ANAHTAR KELIMEDIR ve varsayilani YOKTUR (BOQ-SEC K6).

    Bilincli bir zorlamadir: varsayilani `0` olsaydi yeni bir cagri yeri sessizce
    "hic tahsis yok" derdi ve ekran atanmamis miktari OLDUGUNDAN BUYUK gosterirdi.

    `quantity` YALNIZ bolum suzgecinde verilir (K5): o zaman poz kotasi degil O
    BOLUME tahsis edilen miktar basilir ve `amount` ondan turer. `allocated`/
    `unallocated` ise HER ZAMAN pozun GERCEK kotasi uzerinden hesaplanir — bkz.
    `BoqItemResponse`taki "iki anlam" notu.
    """
    taban = item.quantity if quantity is None else quantity
    return BoqItemResponse(
        id=item.id,
        code=item.code,
        description=item.description,
        unit=item.unit,
        quantity=taban,
        unit_price=item.unit_price,
        # 🔴 PAYDA = SUNULAN miktar: bolum suzgecinde o bolumun TAHSISI, aksi
        # hâlde pozun santiye kotasi. Iki anlam notuyla (schemas.py:88) birebir
        # tutarli — ekranda gorunen miktarin yuzdesi basilir.
        progress_pct=_progress_metric(realized, taban, izinli=izinli),
        sort_order=item.sort_order,
        allocated_quantity=allocated,
        unallocated_quantity=item.quantity - allocated,
    )


async def item_response(session: AsyncSession, item: BoqItem, actor: User) -> BoqItemResponse:
    """Tekil kalem yaniti — tahsis toplamini DB'den okur (yazma uclari icin).

    🔴 `actor` ILR-1'de EKLENDI ve varsayilani YOKTUR: yazma ucunun yaniti da
    OKUMA ucuyle AYNI zarfi tasimalidir, aksi hâlde ekran kaydettikten sonra
    yuzdeyi KAYBEDER (`build_site_detail` docstring'indeki ayni kanon). Izin de
    burada olculur — yazma ucu okuma kapisini atlayamaz.
    """
    izinli = await can_read(session, actor, _SITE_DIARY)
    realized = (
        (await progress.realized_by_item(session, [item.id])).get(item.id) if izinli else None
    )
    return to_item(
        item,
        allocated=await repository.allocated_total_for_item(session, item.id),
        realized=realized,
        izinli=izinli,
    )


def to_group(
    group: BoqGroup,
    *,
    allocated_totals: dict[uuid.UUID, Decimal],
    section_quantities: dict[uuid.UUID, Decimal] | None = None,
    realized_totals: dict[uuid.UUID, Decimal] | None = None,
    izinli: bool = False,
    visible: set[uuid.UUID] | None = None,
) -> BoqGroupResponse:
    """`section_quantities` verilmisse (bolum suzgeci, K5) o bolume TAHSISI OLMAYAN
    kalemler listeden DUSER — sifir miktarli hayalet satir basilmaz.

    `visible` (DSC-B1): `None` = kisitsiz (suzgec yok); kume verilirse yalniz o kalemler
    listelenir ve `group_total` (kalem tutarlarinin toplami) de BUNLARDAN turer."""
    items = []
    for item in group.items:
        if visible is not None and item.id not in visible:
            continue
        allocated = allocated_totals.get(item.id, _ZERO_QUANTITY)
        realized = (realized_totals or {}).get(item.id)
        if section_quantities is None:
            items.append(to_item(item, allocated=allocated, realized=realized, izinli=izinli))
            continue
        section_quantity = section_quantities.get(item.id)
        if section_quantity is None:
            continue
        items.append(
            to_item(
                item,
                allocated=allocated,
                quantity=section_quantity,
                realized=realized,
                izinli=izinli,
            )
        )
    return BoqGroupResponse(
        id=group.id,
        name=group.name,
        sort_order=group.sort_order,
        items=items,
    )


async def group_response(session: AsyncSession, group: BoqGroup) -> BoqGroupResponse:
    """Tekil grup yaniti (yazma uclari icin) — kalemlerinin tahsis toplamlarini
    santiye capinda TEK sorguda okur. Grup yeni acilmissa koleksiyon bostur ve
    sozluk kullanilmaz; yine de sorgu ATLANMAZ: "yeni grup her zaman bostur"
    varsayimi PATCH yolunda YANLIStir ve sessizce sifir tahsis basardi."""
    allocated_totals = await repository.allocated_totals_for_site(session, group.site_id)
    return to_group(group, allocated_totals=allocated_totals)


def _totals(
    groups: list[BoqGroupResponse], grand_progress: MetricPlaceholder | None = None
) -> BoqTotals:
    """Spec §5.1: `grand_total` GERCEK (gruplarin toplami), geri kalani yer
    tutucu. Toplama Decimal ile yapilir (float ASLA); bos BOQ "0.00" doner."""
    grand_total = quantize_money(sum((group.group_total for group in groups), Decimal("0")))
    return BoqTotals(
        contract_total=_metric(_CONTRACTS),
        realized_total=_metric(_PROGRESS_PAYMENTS),
        remaining_total=_metric(_PROGRESS_PAYMENTS),
        revision_total=_metric(_CONTRACTS),
        grand_total=grand_total,
        # 🔴 TEK KAYNAK: bolum/santiye kapsaminin yuzdesi `boq.progress`ten
        # gelir — grup satirlarindan YENIDEN toplanmaz. Ikinci bir toplama,
        # `sites` ekraniyla `boq` ekraninin ayni bolum icin farkli "%" basmasi
        # demekti (presenters.py:203 gerekcesi).
        grand_progress_pct=grand_progress
        if grand_progress is not None
        else _metric(_PROGRESS_PAYMENTS),
    )


async def visible_section_in_site(
    session: AsyncSession, site: Site, section_id: uuid.UUID | None
) -> Section | None:
    """Okuma suzgecinin bolumu (K5). BASKA SANTIYENIN bolumu **404**tur.

    Bos liste donmek, kullaniciya "bu bolume hic is kalemi atanmamis" YALANINI
    soylerdi (`timesheet.service.visible_section` kanonu); var olmayan bolumle
    AYNI 404 ise kaydin varligini sizdirmaz. Mesaj `sites` modulunun TEK
    cumlesidir — kopya uretilmez.
    """
    if section_id is None:
        return None
    section = await sites_repository.get_section(session, section_id)
    if section is None or section.site_id != site.id:
        raise NotFoundError(sites_guards.SECTION_MISSING)
    return section


async def get_boq_export_for_site(
    session: AsyncSession,
    actor: User,
    site_id: uuid.UUID,
    section_id: uuid.UUID | None = None,
    scope: DisciplineScope = UNRESTRICTED,
) -> tuple[Site, BoqListResponse]:
    """Spec §5.1/§5.3 ortak okuma yolu. Gorunmeyen santiye 404 doner (P2 §5.2
    deseni), 403 degil — varligin kendisi sizdirilmaz. `Site` de donulur:
    T8 disa aktarim ucu dosya adi icin `site.code`'a ihtiyac duyar.

    BOQ-SEC K5: `section_id` VERILMEZSE davranis BIREBIR eskisidir. Verilirse
    yalniz o bolume tahsisi olan kalemler doner, `quantity` o bolumun payidir ve
    BOSALAN GRUPLAR listeden DUSER (aksi hâlde ekran bos basliklarla dolardi).
    Disa aktarim ucu bu AYNI cagriyi kullanir — iki ayri suzme kodu yazilmaz
    (`timesheet/router.py:57↔93` emsali), yoksa Excel ile ekran ayrisirdi.

    DSC-B1 `scope`: kisitlida yalniz gorunur kalemler (disiplinsiz/NULL gorunmez, Ü1); grup
    ya tumden gorunur ya hic (kalem disiplini grubundan turer) ve gorunur kalemi kalmayan
    grup DUSER. `grand_total` suzulmus gruplardan, `grand_progress_pct` AYNI kume uzerinden
    `progress`in `scope` parametresiyle hesaplanir — yarim suzme (satirlar suzuk, agrega
    sirket geneli) yoktur. Kisitsizda hicbir suzgec eklenmez.
    """
    site, _ = await _visible_site(session, actor, site_id)
    section = await visible_section_in_site(session, site, section_id)
    allocated_totals = await repository.allocated_totals_for_site(session, site.id)
    section_quantities = (
        None
        if section is None
        else await repository.section_allocations_for_site(session, site.id, section.id)
    )
    boq_groups = await repository.list_groups_for_site(session, site.id)
    gorunur = await visible_item_set(
        session, scope, [item.id for group in boq_groups for item in group.items]
    )

    # --- ILR-1 FIZIKSEL ILERLEME (izne duyarli) ---
    #
    # 🔴 K4: `boq`yu okuyup `site_diary`yi OKUYAMAYAN roller VAR (olculdu:
    # `accounting`, `procurement`). Onlara gunlukten turemis bir yuzde basmak,
    # `site_diary` kapisi hic calismadan o veriyi BOQ ekranindan acardi.
    izinli = await can_read(session, actor, _SITE_DIARY)
    realized_totals: dict[uuid.UUID, Decimal] = {}
    grand_progress: MetricPlaceholder | None = restricted()
    if izinli:
        item_ids = [
            item.id
            for group in boq_groups
            for item in group.items
            if gorunur is None or item.id in gorunur
        ]
        kapsam_section = None if section is None else section.id
        realized_totals = await progress.realized_by_item(session, item_ids, kapsam_section)
        pct = (
            await progress.physical_for_site(session, site.id, scope)
            if section is None
            else await progress.physical_for_section(session, section.id, scope)
        )
        grand_progress = metric(pct, _SITE_DIARY)

    groups = [
        to_group(
            group,
            allocated_totals=allocated_totals,
            section_quantities=section_quantities,
            realized_totals=realized_totals,
            izinli=izinli,
            visible=gorunur,
        )
        for group in boq_groups
    ]
    if section is not None or gorunur is not None:
        # DSC-B2: kisitlida KENDI disiplinindeki BOS grup de gorunur (yeni kalem eklenebilsin);
        # yabanci/eslenmemis bos grup gorunmez. Bolum suzgecinde bos grup eskisi gibi duser.
        bos_gorunur = await visible_group_set(
            session, scope, [g.id for g in boq_groups if not g.items]
        )
        groups = [
            group
            for group in groups
            if group.items or (section is None and group.id in (bos_gorunur or ()))
        ]
    return site, BoqListResponse(totals=_totals(groups, grand_progress), groups=groups)


async def get_boq_for_site(
    session: AsyncSession,
    actor: User,
    site_id: uuid.UUID,
    section_id: uuid.UUID | None = None,
    scope: DisciplineScope = UNRESTRICTED,
) -> BoqListResponse:
    """Spec §5.1 okuma yolu — `get_boq_export_for_site`'in ince sarmalayicisi."""
    _, response = await get_boq_export_for_site(session, actor, site_id, section_id, scope)
    return response


# --- Gorunurluk — yazma uclari icin yukari cozumleme (spec §5.5) ---


async def _visible_group(
    session: AsyncSession,
    actor: User,
    group_id: uuid.UUID,
    scope: DisciplineScope = UNRESTRICTED,
) -> tuple[BoqGroup, Site]:
    """Grup -> santiye -> proje. PATCH /boq/groups/{id} bu zincirden gecer;

    gorunmeyen kayit 404 doner, 403 DEGIL (P2 IDOR dersi, spec §5.5) — en kolay
    atlanan guvenlik noktasi budur. DSC-B2 `scope`: kisitliya gorunmeyen (yabanci /
    eslenmemis) grup "yok" ile AYNI 404'u alir (S6).
    """
    group = await repository.get_group(session, group_id)
    if group is None:
        raise NotFoundError(_GROUP_MISSING)
    site, _ = await _visible_site(session, actor, group.site_id, _GROUP_MISSING)
    gorunur = await visible_group_set(session, scope, [group.id])
    if gorunur is not None and group.id not in gorunur:
        raise NotFoundError(_GROUP_MISSING)
    return group, site


async def _visible_item(
    session: AsyncSession,
    actor: User,
    item_id: uuid.UUID,
    scope: DisciplineScope = UNRESTRICTED,
) -> tuple[BoqItem, Site]:
    """Kalem -> santiye -> proje (item.site_id dogrudan tutulur, spec §5.5:

    "item→site→project"). Gorunmeyen kayit 404 doner, 403 DEGIL.

    DSC-B2 `scope`: baska disiplinin (ya da disiplinsiz) kalemi kisitliya "yok" ile AYNI 404
    govdesini alir (Ü7) — varligi sizmaz. Kisitsizda ek sorgu YOK (`visible_item_set` None).
    """
    item = await repository.get_item(session, item_id)
    if item is None:
        raise NotFoundError(_ITEM_MISSING)
    site, _ = await _visible_site(session, actor, item.site_id, _ITEM_MISSING)
    gorunur = await visible_item_set(session, scope, [item.id])
    if gorunur is not None and item.id not in gorunur:
        raise NotFoundError(_ITEM_MISSING)
    return item, site


async def _ensure_group_in_site(
    session: AsyncSession,
    group_id: uuid.UUID,
    site: Site,
    scope: DisciplineScope = UNRESTRICTED,
) -> BoqGroup:
    """Spec §3.3 invariant 1: kalemin baglandigi grubun site_id'si kalemin

    site_id'si ile ayni olmali. Grup hic yoksa da (spec §5.5 IDOR-2) ayni 422
    ile karsilanir — varliginin gizli tutulmasi gerekmez, cunku site zaten
    gorunurluk suzgecinden gecmis; yalnizca ait olmadigi bir grup enjekte
    edilmesi engellenir.

    DSC-B2 `scope`: kisitliya gorunmeyen (yabanci / eslenmemis) grup, olmayanla AYNI 422'yi
    alir (Ü7).
    """
    group = await repository.get_group(session, group_id)
    if group is None or group.site_id != site.id:
        raise BoqGroupSiteMismatchError(_GROUP_SITE_MISMATCH)
    gorunur = await visible_group_set(session, scope, [group.id])
    if gorunur is not None and group.id not in gorunur:
        raise BoqGroupSiteMismatchError(_GROUP_SITE_MISMATCH)
    return group


async def _ensure_code_unique(
    session: AsyncSession, site_id: uuid.UUID, code: str, exclude_item_id: uuid.UUID | None = None
) -> None:
    """Spec §3.3 invariant 2 / §5.4: (site_id, code) çakışması → 409, Türkçe

    mesaj. IntegrityError → 409 handler'ı yarış durumu emniyet ağı olarak KALIR
    (DuplicateError deseni, `projects.service.create_employer` emsali).

    DSC-B2 Ü7 İSTİSNASI (CEO kararı 2026-09-29, KABUL EDİLEN "poz kodu kâhini"): UQ
    `uq_boq_items_site_code` şantiye düzeyindedir; kısıtlı kullanıcı başka disiplinin kodunu
    girerse 409, olmayan kodu girerse 201 alır. YALNIZ kodun varlığı sızar (ad/tutar/kimlik
    değil). Test: `test_b2_boq.py::test_kabul_edilen_kahin_*`.
    """
    existing = await repository.get_item_by_code(session, site_id, code, exclude_item_id)
    if existing is not None:
        raise DuplicateError(_DUPLICATE_CODE)


# --- Yazma uclari (T5/T6) ---


async def create_group(
    session: AsyncSession, actor: User, site_id: uuid.UUID, data: BoqGroupCreate
) -> BoqGroup:
    site, _ = await _visible_site(session, actor, site_id)
    group = BoqGroup(site_id=site.id, name=data.name, sort_order=data.sort_order)
    session.add(group)
    await session.flush()
    await session.refresh(group)
    return group


async def create_item(
    session: AsyncSession,
    actor: User,
    site_id: uuid.UUID,
    data: BoqItemCreate,
    scope: DisciplineScope = UNRESTRICTED,
) -> BoqItem:
    """Spec §5.5 IDOR-2: govdedeki `group_id` baska santiyenin grubu olabilir —

    yol parametresi `site_id` ile karsi karsiya konur, uyusmazlik 422 doner.
    """
    site, _ = await _visible_site(session, actor, site_id)
    group = await _ensure_group_in_site(session, data.group_id, site, scope)
    await _ensure_code_unique(session, site.id, data.code)
    item = BoqItem(
        site_id=site.id,
        group_id=group.id,
        code=data.code,
        description=data.description,
        unit=data.unit,
        quantity=data.quantity,
        unit_price=data.unit_price,
        sort_order=data.sort_order,
    )
    session.add(item)
    await session.flush()
    await session.refresh(item)
    return item


async def update_group(
    session: AsyncSession,
    actor: User,
    group_id: uuid.UUID,
    data: BoqGroupUpdate,
    scope: DisciplineScope = UNRESTRICTED,
) -> BoqGroup:
    group, _ = await _visible_group(session, actor, group_id, scope)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(group, field, value)
    await session.flush()
    await session.refresh(group)
    return group


async def update_item(
    session: AsyncSession,
    actor: User,
    item_id: uuid.UUID,
    data: BoqItemUpdate,
    scope: DisciplineScope = UNRESTRICTED,
) -> BoqItem:
    """`group_id` verilirse spec §3.3 invariant 1/4 tekrar kontrol edilir

    (baska santiyenin grubuna tasima yasak); `code` degisirse §invariant 2
    tekrar kontrol edilir. `site_id` semada yok — tasima ucu kapali.
    """
    item, site = await _visible_item(session, actor, item_id, scope)
    updates = data.model_dump(exclude_unset=True)
    if "group_id" in updates:
        await _ensure_group_in_site(session, updates["group_id"], site, scope)
    if "code" in updates and updates["code"] != item.code:
        await _ensure_code_unique(session, site.id, updates["code"], exclude_item_id=item.id)
    if updates.get("quantity") is not None:
        # 🔴 INVARIANTIN IKINCI KAPISI (BOQ-SEC K3). Tahsis ucu toplami YUKARI
        # dogru sinirlar; burada kota AŞAĞI cekilerek AYNI invariant ters yonden
        # kirilabilir: 1.200'un 700'u dagitilmisken kotayi 500'e indirmek
        # SUM > quantity birakir ve hicbir uc bunu bir daha fark etmez.
        # Kilit ve kontrol tahsis ucundakiyle BIREBIR ayni sirayla alinir.
        updates["quantity"] = quantize_quantity(updates["quantity"])
        await _assert_quantity_covers_allocations(session, item, updates["quantity"])
    for field, value in updates.items():
        setattr(item, field, value)
    await session.flush()
    await session.refresh(item)
    return item


async def delete_group(session: AsyncSession, actor: User, group_id: uuid.UUID) -> str:
    """YALNIZ BOS grubu siler; denetim satiri icin grup adini doner (TB3-C).

    `BoqGroup`'ta `parent_id` YOKTUR — hiyerarsi olmadigi icin "bos" tanimi
    tektir: grupta is kalemi bulunmamasi. Kalem varsa 409
    `RelatedRecordsExistError` (`contracts.service.delete_employer_group`
    emsali): iliski `cascade="all, delete-orphan"` + DB `ON DELETE CASCADE`
    tanimli oldugu icin korkuluk olmadan tum kalemler tek istekte SESSIZCE yok
    olurdu.

    Gorunmeyen grup `_visible_group` uzerinden 404 doner, 403 DEGIL (P2 IDOR
    dersi) — var olmayan UUID ile ayirt edilemez. Ad SILMEDEN ONCE okunur
    (`delete_item` deseni): sonra okunursa `ObjectDeletedError` riski vardir.
    """
    group, _ = await _visible_group(session, actor, group_id)
    if await repository.group_has_items(session, group.id):
        raise RelatedRecordsExistError(_GROUP_HAS_ITEMS)
    name = group.name
    await session.delete(group)
    await session.flush()
    return name


async def delete_item(
    session: AsyncSession,
    actor: User,
    item_id: uuid.UUID,
    scope: DisciplineScope = UNRESTRICTED,
) -> tuple[str, str]:
    """Kalemi siler; denetim satiri icin (code, description) doner.

    Kimlik silmeden ONCE okunur — sonra okunursa satir yoktur (users/roles
    silme uclarindaki desen). Gorunmeyen kayit `_visible_item` uzerinden 404
    doner, 403 DEGIL: var olmayan UUID ile ayirt edilemez olmasi gerekir.

    Satir silindikten sonra grubun `items` koleksiyonu ELDEN CIKARILIR
    (`expire`): koleksiyon zaten yuklenmisse silme onu BAYAT birakir ve ayni
    oturumda BOQ yeniden okundugunda kalem hâlâ listede gorunup `grand_total`
    degismemis gibi doner. `expire` IO yapmaz, yalnizca bir sonraki erisimde
    yeniden yuklenmesini saglar. Grubun kendisi SILINMEZ; santiye toplamlari
    kalan kalemlerden yeniden turedigi icin ayrica guncelleme gerekmez.
    """
    # BOQ-SEC: kalemin tahsisleri `boq_item_id` CASCADE ile birlikte gider —
    # korkuluk EKLENMEZ. Tahsis satiri kalemin bir ALT PARCASIDIR (grup->kalem
    # iliskisindeki gibi bagimsiz bir kayit degil); "once tahsisleri kaldir"
    # demek, kullaniciya anlamsiz bir ara adim dayatmak olurdu.
    item, _ = await _visible_item(session, actor, item_id, scope)
    # 🔴 Borc #53: gunluk satirinin FK'si `ondelete="SET NULL"`dur — CASCADE
    # DEGIL. Tahsisin aksine gunluk satiri kalemin ALT PARCASI degildir; silme
    # onu yok etmez, BAGSIZ birakir ve o satir Hakedis Ozeti ile gunun detay
    # ekraninda KALICI olarak farkli ₺ soylemeye baslar (gonderilmis gunde
    # telafisi yok). `delete_group`/`group_has_items` deseniyle 409.
    if await repository.item_has_diary_lines(session, item.id):
        raise RelatedRecordsExistError(_ITEM_HAS_DIARY_LINES)
    identity = (item.code, item.description)
    group_id = item.group_id
    await session.delete(item)
    await session.flush()
    group = await repository.get_group(session, group_id)
    if group is not None:
        session.expire(group, ["items"])
    return identity


# --- BOQ-SEC: bölüm tahsisi (K3/K4) ---------------------------------------


async def _assert_quantity_covers_allocations(
    session: AsyncSession, item: BoqItem, quantity: Decimal
) -> None:
    """🔴 EŞİK = KİLİT (İK-2 dersi) — kontrolden ÖNCE poz satırı kilitlenir.

    Kilitsiz sıralama (oku → karşılaştır → yaz) iki eşzamanlı istekte İKİSİNİ DE
    geçirir: her ikisi de eski toplamı okur, her ikisi de "sığıyor" der ve
    şantiye kotası aşılır. `lock_item` satırı `FOR UPDATE` ile tuttuğu için
    ikinci istek BEKLER, tazelenmiş toplamı okur ve 409 alır.

    Kilit ile toplam okuması arasında hiçbir karar YOKTUR: araya giren her
    kontrol TOCTOU penceresi açardı.
    """
    locked = await repository.lock_item(session, item.id)
    if locked is None:  # pragma: no cover — `_visible_item` zaten çözdü
        raise NotFoundError(_ITEM_MISSING)
    allocated = await repository.allocated_total_for_item(session, item.id)
    if allocated > quantity:
        raise ConflictError(_QUANTITY_BELOW_ALLOCATED)


def _assert_body_shape(data: BoqItemAllocationsReplace) -> dict[uuid.UUID, Decimal]:
    """Gövdenin KENDİ İÇİNDE tutarlılığı — DB'ye hiç gitmeden (`site_planning` deseni).

    🔴 Aynı bölüm iki kez geçerse 422: sunucu SESSİZCE TOPLAMAZ. Toplamak,
    kullanıcının "400 yaz" dediği bir satırı 700'e çıkarır ve hiçbir ekranda
    görünmez. Miktarlar yazılmadan ÖNCE `Numeric(14,3)` ölçeğine çekilir ki
    kontrol edilen sayı ile saklanan sayı aynı olsun.
    """
    istenen: dict[uuid.UUID, Decimal] = {}
    for giris in data.allocations:
        if giris.section_id in istenen:
            raise SiteValidationError(_ALLOCATION_DUPLICATE_SECTION)
        istenen[giris.section_id] = quantize_quantity(giris.quantity)
    return istenen


async def _resolve_sections(
    session: AsyncSession, site: Site, section_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Section]:
    """Her bölüm bu ŞANTİYEye ait olmalı; değilse **404** (K4).

    422 DEĞİL: `timesheet`/`site_planning`te bölüm gövdenin düzeltilebilir bir
    ALANIydı, burada ise tahsisin HEDEF KAYDIDIR — var olmayan bir bölüme yazma
    isteği ile başka şantiyenin bölümüne yazma isteği AYIRT EDİLEMEZ olmalıdır,
    aksi hâlde uç bir bölüm kimliği tarayıcısına döner.
    """
    cozulmus: dict[uuid.UUID, Section] = {}
    for section_id in section_ids:
        section = await sites_repository.get_section(session, section_id)
        if section is None or section.site_id != site.id:
            raise NotFoundError(sites_guards.SECTION_MISSING)
        cozulmus[section_id] = section
    return cozulmus


async def replace_allocations(
    session: AsyncSession,
    actor: User,
    item_id: uuid.UUID,
    data: BoqItemAllocationsReplace,
    scope: DisciplineScope = UNRESTRICTED,
) -> BoqItemAllocationsResponse:
    """`PUT /boq/items/{item_id}/allocations` — pozun TÜM tahsislerini gövdeye eşitler.

    Sıra ZORUNLUDUR ve `site_planning.write` kuralının aynısıdır: ÖNCE TÜM
    DOĞRULAMALAR, SONRA TEK YAZMA — ikinci satırda patlayan bir istek birincisini
    session'a eklemiş OLMAMALIDIR.

    1. `_visible_item` — görünmeyen kalem 404 (IDOR: 403 değil),
    2. gövde şekli (aynı bölüm iki kez → 422),
    3. 🔴 poz satırı `FOR UPDATE` — buradan sonrası serileşmiştir,
    4. bölüm kapsamı (başka şantiye → 404),
    5. toplam invariantı (`SUM <= quantity`) → aşarsa 409,
    6. yazma.

    Kimlik KORUNUR: gövdede yeniden geçen bir (poz, bölüm) çifti SİLİNİP YENİDEN
    yazılmaz, `quantity`si güncellenir. Sil+yaz yapılsaydı her kaydetme
    `created_at`i sıfırlar ve DEFERRABLE olmayan bir UQ'da çakışırdı.

    Boş dizi `[]` tüm tahsisleri kaldırır ve miktar tamamen "atanmamış"a döner —
    bu bir HATA DEĞİL geçerli bir istektir (K4).
    """
    item, site = await _visible_item(session, actor, item_id, scope)
    istenen = _assert_body_shape(data)

    locked = await repository.lock_item(session, item.id)
    if locked is None:  # pragma: no cover — `_visible_item` zaten çözdü
        raise NotFoundError(_ITEM_MISSING)

    sections = await _resolve_sections(session, site, istenen)

    toplam = sum(istenen.values(), Decimal("0"))
    if toplam > locked.quantity:
        raise ConflictError(_ALLOCATION_EXCEEDS_QUANTITY)

    # --- Buradan itibaren YAZMA; doğrulama YOK (yukarıdaki sıra kısıtı). ---
    mevcut = {
        row.section_id: row for row in await repository.list_allocations_for_item(session, item.id)
    }
    for section_id, row in mevcut.items():
        if section_id not in istenen:
            await session.delete(row)
    for section_id, quantity in istenen.items():
        row = mevcut.get(section_id)
        if row is None:
            session.add(
                BoqItemSectionAllocation(
                    boq_item_id=item.id, section_id=section_id, quantity=quantity
                )
            )
        else:
            row.quantity = quantity
    await session.flush()

    satirlar = await repository.list_allocations_for_item(session, item.id)
    return BoqItemAllocationsResponse(
        item=to_item(locked, allocated=sum((row.quantity for row in satirlar), Decimal("0"))),
        allocations=[
            BoqItemAllocation(
                section_id=row.section_id,
                section_name=sections[row.section_id].name,
                quantity=row.quantity,
            )
            for row in satirlar
        ],
    )


async def get_allocations(
    session: AsyncSession,
    actor: User,
    item_id: uuid.UUID,
    scope: DisciplineScope = UNRESTRICTED,
) -> BoqItemAllocationsResponse:
    """`GET /boq/items/{item_id}/allocations` — pozun TÜM tahsislerini okur.

    🔴 NEDEN VAR: `replace_allocations` **tam küme değiştirmedir** (K4). Kümenin
    tamamını TEK çağrıda okuyan bir uç olmadan, kısmi görüşe sahip bir ekran
    (yalnız kendi bölümünü gören) o PUT'a yazınca **görmediği bölümlerin
    paylarını sessizce siler.** Kanon: tam küme değiştirme ucu, kümenin tamamını
    okuyan bir uç olmadan yazmaya açılamaz.

    Yazma yolundan AYRILAN üç nokta ve gerekçeleri:
      * **Kilit YOK** (K7): `lock_item` çağrılmaz. Okuma bir invariant KARARI
        vermez, yalnız mevcut hâli basar; kilit almak yazarları boşuna
        serileştirirdi.
      * **`record_audit` YOK** (K3, T7 kuralı): okumalar denetim günlüğüne
        yazmaz — `export_boq_endpoint` emsali.
      * **Bölüm adları GÖVDEDEN DEĞİL SATIRLARDAN çözülür** (K5): PUT gövdedeki
        `section_id`leri `_resolve_sections` ile tek tek çözebiliyordu çünkü
        onları ayrıca ŞANTİYE KAPSAMINA karşı doğrulaması gerekiyordu. Burada
        böyle bir doğrulama YOKTUR (satırlar zaten görünür bir poza ait), o
        yüzden adlar **tek `IN (...)` sorgusuyla** toplu çekilir.

    Cevap gövdesi PUT'un cevabıyla **birebir aynı şekildedir** (K4) — aynı
    `response_model`, `allocated_quantity` aynı biçimde tahsis satırlarının
    toplamından türer. Frontend'in "oku → değiştir → yaz" döngüsünde iki ayrı
    şekil ayrıştırması olmamalıdır.

    Tahsisi olmayan poz `allocations: []` ile **200** döner, 404 DEĞİL (K6):
    boş küme geçerli bir cevaptır ve PUT'un `[]` kabulüyle simetriktir.
    """
    # DSC-B1 (Ü7): baska disiplinin (ya da disiplinsiz) kalemi kisitliya "yok" ile AYNI 404
    # govdesini alir — `_visible_item(scope)` ic kontrolu.
    item, _ = await _visible_item(session, actor, item_id, scope)
    satirlar = await repository.list_allocations_for_item(session, item.id)
    adlar = await sites_repository.section_names_by_ids(
        session, [row.section_id for row in satirlar]
    )
    return BoqItemAllocationsResponse(
        item=to_item(item, allocated=sum((row.quantity for row in satirlar), Decimal("0"))),
        allocations=[
            BoqItemAllocation(
                section_id=row.section_id,
                section_name=adlar[row.section_id],
                quantity=row.quantity,
            )
            for row in satirlar
        ],
    )

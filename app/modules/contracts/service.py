"""Birleşik sözleşme listesi servis katmanı (spec §6.1, task C5).

İki katmanlı koruma (spec §6): `contracts` izni router'da (`_VIEW`) YETKİYİ
verir, bu modül `projects.service.visible_projects` ile KAPSAMI belirler —
görünmeyen projenin sözleşmesi listeye asla girmez. Bu iki katmandan biri
eksikse task başarısızdır (task brief kararı).
"""

import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    DuplicateError,
    NotFoundError,
    RelatedRecordsExistError,
    SiteValidationError,
)
from app.core.slug import matches_ref
from app.core.timezone import today
from app.modules.catalog import service as catalog_service
from app.modules.catalog.guards import CATALOG_ITEM_MISSING
from app.modules.catalog.models import EvCatalogItem
from app.modules.company.service import get_company
from app.modules.contracts import distribution_quantity, repository
from app.modules.contracts.guards import (
    CONTRACT_MISSING,
    DUPLICATE_ITEM_CODE,
    GROUP_HAS_ITEMS,
    GROUP_MISSING,
    GROUP_PROJECT_MISMATCH,
    ITEM_MISSING,
    ITEM_QUANTITY_BELOW_DISTRIBUTED,
    boq_code_taken_in_site,
)
from app.modules.contracts.models import (
    ContractStatus,
    EmployerContractGroup,
    EmployerContractItem,
    SubcontractorContract,
)
from app.modules.contracts.schemas import (
    ContractListItem,
    ContractListResponse,
    ContractSummary,
    ContractType,
    EmployerContractDetail,
    EmployerContractGroupCreate,
    EmployerContractGroupItems,
    EmployerContractGroupUpdate,
    EmployerContractItemCreate,
    EmployerContractItemResponse,
    EmployerContractItemsBulkCreate,
    EmployerContractItemsResponse,
    EmployerContractItemUpdate,
)
from app.modules.projects.models import Project, ProjectContract
from app.modules.projects.service import visible_projects
from app.modules.sites import repository as sites_repository
from app.modules.users.models import User

# TB4/B3+S7: ayna BOQ satırında sözleşme kaleminden TÜREYEN alanlar. Miktar bu
# kümede DEĞİLDİR — o dağıtımın kendi kararıdır (spec §1 B3).
#
# Küme DÖRTTÜR (S7, kullanıcı onayı): `unit_price` + `code` + `description` +
# `unit`. Dağıtımın relink yolu (`distribution._apply_allocations`) bu dört alanı
# ZATEN sözleşmeden kopyalıyordu; senkron tazelemede ikisini dışarıda bırakmak
# aynı bayatlığın yarısını açık bırakırdı.
#
# Bu sabit TEK KAYNAKTIR: hem senkron tazeleme (`_refresh_mirror_boq_rows`) hem
# relink yolu buradan beslenir — relink `apply_mirrored_fields` ile ÇAĞIRIR,
# kendi listesini TUTMAZ. İki yol ayrışırsa
# `tests/contracts/test_employer_item_boq_sync.py`nin enjeksiyon testleri kırmızı olur.
MIRRORED_ITEM_FIELDS = ("code", "description", "unit", "unit_price")


def apply_mirrored_fields(target: object, item: EmployerContractItem) -> None:
    """Ayna alan kümesini kalemden hedef BOQ satırına kopyalar (S7 tek kaynak).

    Alan listesi ÇAĞRI ANINDA modül global'inden okunur — dağıtımın relink yolu
    ile senkron tazelemenin aynı kümeyi görmesinin tek yolu budur. `quantity`
    BURADA YOK: hedef satırın miktarını çağıran belirler.
    """
    for field in MIRRORED_ITEM_FIELDS:
        setattr(target, field, getattr(item, field))


_MONEY = Decimal("0.01")


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY, rounding=ROUND_HALF_UP)


def _employer_item(
    project: Project, contract: ProjectContract, cumulative_gross: Decimal
) -> ContractListItem:
    """Alan eşlemesi spec §6.1 tablosu — işveren sütunu BİREBİR.

    `progress_pct` P7/H9'da gerçek değere döndü (spec §9.6): §8'in FİNANSAL
    ilerlemesidir (`kümülatif brüt / bedel × 100`), `projects.progress_pct`
    elle girilen alanı DEĞİL. İki kaynak yan yana durursa hangisinin "ilerleme"
    olduğu belirsizleşir; sözleşme ekranında ölçü sözleşmenin hakkedilen
    bedelidir.
    """
    from app.modules.progress_payments import summary as progress_payments_summary

    return ContractListItem(
        id=project.id,
        project_id=project.id,
        title=project.name,
        contract_no=contract.contract_no,
        counterparty_name=project.employer_name,
        amount=contract.amount if contract.amount is not None else Decimal("0"),
        start_date=project.start_date,
        end_date=project.end_date,
        progress_pct=progress_payments_summary.progress_pct(cumulative_gross, contract.amount),
        status=contract.status,
        is_draft=project.is_draft,
    )


def _subcontractor_amount(contract: SubcontractorContract) -> Decimal:
    """`Σ(line_total)` — her satır ÖNCE kuruşa yuvarlanır, SONRA toplanır

    (dal geneli son inceleme kararı: `Numeric(14,3) × Numeric(18,2)` beş
    ondalık üretebildiği için ham çarpımların toplamını TEK SEFERDE
    yuvarlamak `Σ line_total != contract_total` sapmasına yol açabilirdi —
    `schemas.SubcontractorContractItemResponse.line_total` ve
    `distribution.py`nin zaten kullandığı kuralla hizalanır). `unit_price IS
    NULL` olan satır 0 katkı verir (task brief kararı). Kalemler zaten
    `lazy="selectin"` ile yüklü, ek sorgu YOK.
    """
    line_totals = (
        _quantize_money(item.quantity * item.unit_price)
        for item in contract.items
        if item.unit_price is not None
    )
    return sum(line_totals, Decimal("0.00"))


def _subcontractor_title(contract: SubcontractorContract) -> str:
    """`subcontractor_name + " — " + work_category` (`TSD` 40 deseni, spec §6.1)."""
    name = contract.subcontractor_name or ""
    category = contract.work_category or ""
    if name and category:
        return f"{name} — {category}"
    return name or category


def _subcontractor_item(
    contract: SubcontractorContract, cumulative_gross: Decimal
) -> ContractListItem:
    """SZL 44-51 taşeron satırı. `progress_pct` P-YT4'te BAĞLANDI (2026-08-23).

    Eski hâli `None` idi ve gerekçesi *"taşeron hakedişi AYRI dilim (spec §1.2)"*
    yazıyordu. **Gerekçe ölçüldü ve BAYAT çıktı:** dilim TH ile yazıldı, modülün
    router'ı `app/main.py`de kayıtlı ve `subcontractor_progress_payments.summary.
    cumulative_gross_by_contracts` CANLI. Dahası `list_contracts` o sözlüğü
    ZATEN okuyordu (şerit KPI'ı `progress_payment_total` için) ve yalnızca
    satırlara geçirmiyordu — bağlama bu yüzden **EK SORGU AÇMAZ**.

    Yüzde işveren dalıyla AYNI formülden geçer (`progress_payments.summary.
    progress_pct`): payda sözleşme bedeli (`Σ line_total`), pay `approved|paid`
    kümülatif brüt. İkinci bir formül yazılsaydı SZL'nin TEK "İlerleme" sütunu
    sekmeye göre başka bir şey ölçerdi.

    🔴 Bu depoda İKİNCİ bir "ilerleme" tanımı daha vardır ve BURAYA UYMAZ:
    `projects/cost_summary.py::_row` yalnız `paid` sayar (mockup aritmetiğinden
    okundu, KY 209-251 / KK 213-246). O sütun proje maliyet ekranınındır;
    buradaki sütun SZL'nindir ve işveren sekmesiyle hizalı kalmak zorundadır.
    Bekçi: `tests/contracts/test_pyt4_yer_tutucu_denetimi.py::test_taban_*`.

    Hakedişi olmayan sözleşmede yüzde `0.00`dır (bilinmiyor değil, gerçekten
    sıfır); bedeli olmayan sözleşmede `None` KALIR — `progress_pct` sıfır/negatif
    paydada bölme yapmaz.
    """
    from app.modules.progress_payments import summary as progress_payments_summary

    # Bedel TEK KEZ hesaplanir: hem "Bedel" sutunu hem yuzdenin paydasidir ve
    # iki kez cagirmak, ileride biri degistiginde ikisinin AYRISMASINA acik kapi
    # birakirdi (satir ici kurus yuvarlamasi `_subcontractor_amount`tadir).
    amount = _subcontractor_amount(contract)

    return ContractListItem(
        id=contract.id,
        project_id=contract.project_id,
        title=_subcontractor_title(contract),
        contract_no=contract.contract_no,
        counterparty_name=contract.subcontractor_name,
        amount=amount,
        start_date=contract.start_date,
        end_date=contract.end_date,
        progress_pct=progress_payments_summary.progress_pct(cumulative_gross, amount),
        status=contract.status,
        is_draft=contract.is_draft,
    )


def _summary(
    items: list[ContractListItem], progress_payment_total: Decimal | None
) -> ContractSummary:
    """`SZL` 34-38 üst KPI şeridi (spec §6.1). `expiring_this_month_count`:

    durumu `active` VE bitiş tarihi sunucunun görüntüleme saat dilimindeki
    (`app/core/timezone.today`) içinde bulunulan ay içinde olan sözleşmeler.
    """
    total_amount = _quantize_money(sum((item.amount for item in items), Decimal("0")))
    active_count = sum(1 for item in items if item.status is ContractStatus.active)
    current = today()
    expiring_this_month_count = sum(
        1
        for item in items
        if item.status is ContractStatus.active
        and item.end_date is not None
        and item.end_date.year == current.year
        and item.end_date.month == current.month
    )
    return ContractSummary(
        total_amount=total_amount,
        active_count=active_count,
        progress_payment_total=progress_payment_total,
        expiring_this_month_count=expiring_this_month_count,
    )


async def list_contracts(
    session: AsyncSession,
    actor: User,
    contract_type: ContractType,
    project_id: uuid.UUID | None,
    status_filter: ContractStatus | None,
    q: str | None,
) -> ContractListResponse:
    # Yerel import: `contracts` → `progress_payments` yönü TEK taraflıdır ve
    # modül düzeyinde kurulsaydı `progress_payments.service`in `contracts.models`
    # importuyla dairesel bir zincire dönme riski taşırdı (`projects/service.py`
    # deseninin aynısı).
    from app.modules.progress_payments import summary as progress_payments_summary

    # Taşeron tarafında dairesel zincir VARSAYIM DEĞİL ÖLÇÜLMÜŞTÜR:
    # `subcontractor_progress_payments/repository.py` `contracts.models`i,
    # `…/service.py` + `…/lines.py` ise `contracts.repository`/`contracts.guards`
    # modüllerini modül düzeyinde import eder. Bu import modül düzeyine
    # çıkarılırsa `contracts` → `subcontractor_progress_payments` → `contracts`
    # halkası kapanır; yerel import ŞARTTIR.
    from app.modules.subcontractor_progress_payments import summary as subcontractor_summary

    visible_ids = [p.id for p in await visible_projects(session, actor)]
    progress_payment_total: Decimal | None = None

    if contract_type == "employer":
        rows = await repository.list_employer_contracts(
            session,
            visible_ids,
            project_id=project_id,
            status_filter=status_filter,
            q=q,
        )
        # Kümülatif brütler TEK toplu sorguda (proje başına ayrı sorgu YOK,
        # plan H9 Adım 3); kapsam süzgeci SQL'de kalır.
        cumulative = await progress_payments_summary.cumulative_gross_by_projects(
            session, [project.id for project, _ in rows]
        )
        items = [
            _employer_item(project, contract, cumulative.get(project.id, Decimal("0.00")))
            for project, contract in rows
        ]
        progress_payment_total = _quantize_money(
            sum((cumulative.get(project.id, Decimal("0")) for project, _ in rows), Decimal("0"))
        )
    else:
        contracts = await repository.list_subcontractor_contracts(
            session,
            visible_ids,
            project_id=project_id,
            status_filter=status_filter,
            q=q,
        )
        # Kümülatif brütler TEK toplu sorguda (sözleşme başına ayrı sorgu YOK) —
        # işveren dalının BİREBİR eşleniği, yalnız anahtar proje değil SÖZLEŞME
        # kimliğidir (bir projede N taşeron sözleşmesi olabilir).
        #
        # 🔴 SIRA ÖNEMLİ (P-YT4): sözlük satırlardan ÖNCE okunur, çünkü artık
        # yalnız şerit KPI'ını değil `progress_pct` sütununu da besliyor. İşveren
        # dalındaki `_employer_item(..., cumulative.get(...))` deseninin aynısı.
        cumulative = await subcontractor_summary.cumulative_gross_by_contracts(
            session, [contract.id for contract in contracts]
        )
        items = [
            _subcontractor_item(contract, cumulative.get(contract.id, Decimal("0.00")))
            for contract in contracts
        ]
        progress_payment_total = _quantize_money(
            sum(
                (cumulative.get(contract.id, Decimal("0")) for contract in contracts),
                Decimal("0"),
            )
        )

    # Hakediş toplamı İKİ dalda da doludur: işveren tarafı P7/H9'da, taşeron
    # tarafı TH dilimiyle açılan `subcontractor_progress_payments` üzerinden
    # TH-SUM diliminde bağlandı. `None` bir daha DÖNMEZ — hakedişi olmayan
    # sözleşme de, hiç sözleşme olmaması da `0.00` üretir (bilinmiyor değil,
    # gerçekten sıfır). Alanın tipi şema uyumluluğu için `Decimal | None` kalır.
    return ContractListResponse(summary=_summary(items, progress_payment_total), items=items)


# --- İşveren sözleşmesi: gruplar/kalemler (task C6, spec §6.2) ---
#
# İki katmanlı koruma spec §6'nın aynısı: router'daki `_VIEW`/`_FULL` YETKİYİ,
# aşağıdaki `_visible_project` (`sites/service.py._visible_project` deseninin
# birebiri) `visible_projects` üzerinden KAPSAMI belirler. Görünmeyen projedeki
# gerçek kayıt ile var olmayan kayıt AYNI 404 gövdesini döner.


async def _visible_project(
    session: AsyncSession,
    actor: User,
    project_ref: uuid.UUID | str,
    missing: str = CONTRACT_MISSING,
) -> Project:
    """URL-4: `project_ref` UUID **ya da** proje slug'ı olabilir.

    🔴 Çözümleme AYRI BİR SORGU DEĞİL, GÖRÜNÜR KÜMENİN İÇİNDE yapılır —
    `projects/service.py::_visible_project` ile BİREBİR aynı kanon. Önce çözüp
    sonra süzmek, kapıyı bir satırlık dikkatsizlikle atlanabilir kılardı;
    küme içinde eşleştirmek onu YAPISAL olarak atlanamaz yapar. Görmediği
    projenin slug'ıyla gelen istek, var olmayan slug'la BİREBİR AYNI 404'ü alır
    (slug TAHMİN EDİLEBİLİR, UUID değil).
    """
    visible = await visible_projects(session, actor)
    project = next((p for p in visible if matches_ref(p.id, p.slug, project_ref)), None)
    if project is None:
        raise NotFoundError(missing)
    return project


async def _visible_group(
    session: AsyncSession, actor: User, group_id: uuid.UUID
) -> tuple[EmployerContractGroup, Project]:
    """Grup -> proje. Dolaylı kimlikle erişim de görünürlük süzgecinden geçmek

    ZORUNDA (`boq/service.py._visible_group` deseninin aynısı).
    """
    group = await repository.get_employer_group(session, group_id)
    if group is None:
        raise NotFoundError(GROUP_MISSING)
    project = await _visible_project(session, actor, group.project_id, GROUP_MISSING)
    return group, project


async def _visible_item(
    session: AsyncSession, actor: User, item_id: uuid.UUID
) -> tuple[EmployerContractItem, Project]:
    item = await repository.get_employer_item(session, item_id)
    if item is None:
        raise NotFoundError(ITEM_MISSING)
    project = await _visible_project(session, actor, item.project_id, ITEM_MISSING)
    return item, project


async def _ensure_group_in_project(
    session: AsyncSession, group_id: uuid.UUID, project_id: uuid.UUID
) -> EmployerContractGroup:
    """Spec §3.2 grup->sözleşme tutarlılığı: DB'de bileşik FK ile ZORLANMAZ

    (yazma yolu tekil), servis korkuluğuyla sağlanır (`BoqItem` §3.3 invariant
    1'in aynısı). Grup hiç yoksa da aynı 422 ile karşılanır — proje zaten
    görünürlük süzgecinden geçmiş, yalnızca ait olmadığı bir grup engellenir.
    """
    group = await repository.get_employer_group(session, group_id)
    if group is None or group.project_id != project_id:
        raise SiteValidationError(GROUP_PROJECT_MISMATCH)
    return group


async def _ensure_code_unique(
    session: AsyncSession,
    project_id: uuid.UUID,
    code: str,
    exclude_item_id: uuid.UUID | None = None,
) -> None:
    existing = await repository.get_employer_item_by_code(
        session, project_id, code, exclude_item_id
    )
    if existing is not None:
        raise DuplicateError(DUPLICATE_ITEM_CODE)


async def _distributed_quantity(
    session: AsyncSession, project_id: uuid.UUID, item_id: uuid.UUID
) -> Decimal:
    """TB4/B2: "dağıtılmış" TEK KAYNAKTAN — dağıtım ekranının kalanıyla ve aşım
    kontrolüyle aynı küme (`distribution_quantity`).
    """
    totals = await distribution_quantity.load_distributed_totals(session, project_id)
    return totals.get(item_id, Decimal("0"))


def to_item_response(
    item: EmployerContractItem, distributed: Decimal
) -> EmployerContractItemResponse:
    return EmployerContractItemResponse(
        id=item.id,
        group_id=item.group_id,
        code=item.code,
        description=item.description,
        unit=item.unit,
        quantity=item.quantity,
        unit_price=item.unit_price,
        sort_order=item.sort_order,
        catalog_item_id=item.catalog_item_id,
        source_code=item.source_code,
        distributed_quantity=distributed,
        remaining_quantity=item.quantity - distributed,
    )


async def to_item_response_single(
    session: AsyncSession, project_id: uuid.UUID, item: EmployerContractItem
) -> EmployerContractItemResponse:
    return to_item_response(item, await _distributed_quantity(session, project_id, item.id))


async def get_employer_contract_detail(
    session: AsyncSession, actor: User, project_ref: uuid.UUID | str
) -> EmployerContractDetail:
    """`E14` başlığı (spec §6.2): sözleşme + `items_total` + `advance_amount` +

    yüklenici adı (`company` tek satırından). Sözleşmesi olmayan proje de
    (görünür olsa dahi) `CONTRACT_MISSING` ile 404 döner — bu ucun var oluş
    şartı bir sözleşme kaydının bulunmasıdır.
    """
    from app.modules.progress_payments import summary as progress_payments_summary

    project = await _visible_project(session, actor, project_ref)
    contract = project.contract
    if contract is None:
        raise NotFoundError(CONTRACT_MISSING)

    # 🔴 `project.id` — `project_ref` DEĞİL: ref bir slug olabilir ve `uuid`
    # bekleyen sorguya verilseydi patlardı. Kapıdan geçen kaydın KİMLİĞİ tek
    # meşru anahtardır (aynı kural aşağıdaki iki uçta da geçerli).
    groups = await repository.list_employer_groups(session, project.id)
    items_total = _quantize_money(
        sum(
            (item.quantity * item.unit_price for group in groups for item in group.items),
            Decimal("0"),
        )
    )
    amount = contract.amount if contract.amount is not None else Decimal("0")
    advance_amount = _quantize_money(amount * contract.advance_pct / Decimal("100"))
    company = await get_company(session)

    return EmployerContractDetail(
        project_id=project.id,
        contract_no=contract.contract_no,
        signature_date=contract.signature_date,
        amount=contract.amount,
        advance_pct=contract.advance_pct,
        retainage_pct=contract.retainage_pct,
        vat_pct=contract.vat_pct,
        late_penalty_daily=contract.late_penalty_daily,
        has_price_escalation=contract.has_price_escalation,
        index_type=contract.index_type,
        status=contract.status,
        start_date=project.start_date,
        end_date=project.end_date,
        employer_name=project.employer_name,
        contractor_name=company.name,
        items_total=items_total,
        items_total_diff=amount - items_total,
        advance_amount=advance_amount,
        progress_payment_summary=await progress_payments_summary.build_summary(
            session, project, contract
        ),
    )


async def get_employer_contract_items(
    session: AsyncSession, actor: User, project_ref: uuid.UUID | str
) -> EmployerContractItemsResponse:
    """Spec §6.2: gruplar + kalemler, her kalemde `distributed_quantity`/

    `remaining_quantity`. Toplamlar TEK KAYNAKTAN gelir (TB4/B2,
    `distribution_quantity`) ve sabit sayıda sorgu ile — N+1 üretmez.
    """
    project = await _visible_project(session, actor, project_ref)
    if project.contract is None:
        raise NotFoundError(CONTRACT_MISSING)

    groups = await repository.list_employer_groups(session, project.id)
    distributed = await distribution_quantity.load_distributed_totals(session, project.id)

    return EmployerContractItemsResponse(
        groups=[
            EmployerContractGroupItems(
                id=group.id,
                name=group.name,
                sort_order=group.sort_order,
                items=[
                    to_item_response(item, distributed.get(item.id, Decimal("0")))
                    for item in group.items
                ],
            )
            for group in groups
        ]
    )


async def create_employer_group(
    session: AsyncSession, actor: User, project_id: uuid.UUID, data: EmployerContractGroupCreate
) -> tuple[EmployerContractGroup, Project]:
    """`Project` da döner: router'daki denetim günlüğü satırı proje ADI ister

    (`section_created` gerekçesinin aynısı) — ikinci bir sorgu atılmasın diye.
    """
    project = await _visible_project(session, actor, project_id)
    if project.contract is None:
        raise NotFoundError(CONTRACT_MISSING)
    group = EmployerContractGroup(project_id=project.id, name=data.name, sort_order=data.sort_order)
    session.add(group)
    await session.flush()
    await session.refresh(group)
    return group, project


async def update_employer_group(
    session: AsyncSession, actor: User, group_id: uuid.UUID, data: EmployerContractGroupUpdate
) -> tuple[EmployerContractGroup, Project]:
    group, project = await _visible_group(session, actor, group_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(group, field, value)
    await session.flush()
    await session.refresh(group)
    return group, project


def _new_item(
    project_id: uuid.UUID,
    group_id: uuid.UUID,
    data: EmployerContractItemCreate,
    source_code: str | None = None,
) -> EmployerContractItem:
    """TKL-B3.1: `price_changed_at` açıkça `now` yazılır (server_default eski konteyner
    penceresi içindir) — aynı işlemde sonradan gelen fiyat değişimi ondan ÖNCE damgalanamaz.

    KAT-B2.1: `source_code` istemciden ALINMAZ (K2); katalog bağı varsa çağıran katalogdan
    okuyup verir (snapshot), bağsız kalemde NULL."""
    return EmployerContractItem(
        project_id=project_id,
        group_id=group_id,
        code=data.code,
        description=data.description,
        unit=data.unit,
        quantity=data.quantity,
        unit_price=data.unit_price,
        sort_order=data.sort_order,
        catalog_item_id=data.catalog_item_id,
        source_code=source_code,
        price_changed_at=datetime.now(UTC),
    )


async def create_employer_item(
    session: AsyncSession, actor: User, project_id: uuid.UUID, data: EmployerContractItemCreate
) -> tuple[EmployerContractItem, Project]:
    """Spec §3.3 IDOR: gövdedeki `group_id` başka projenin grubu olabilir —

    yol parametresi `project_id` ile karşı karşıya konur, uyuşmazlık 422 döner.
    TKL-B3.1: `catalog_item_id` verilirse katalog kaleminin VARLIĞI doğrulanır (yoksa 404).
    """
    project = await _visible_project(session, actor, project_id)
    if project.contract is None:
        raise NotFoundError(CONTRACT_MISSING)
    group = await _ensure_group_in_project(session, data.group_id, project.id)
    source_code: str | None = None
    if data.catalog_item_id is not None:
        source_code = (await catalog_service.get_item(session, data.catalog_item_id)).source_code
    await _ensure_code_unique(session, project.id, data.code)
    item = _new_item(project.id, group.id, data, source_code)
    session.add(item)
    await session.flush()
    await session.refresh(item)
    return item, project


async def create_employer_items_bulk(
    session: AsyncSession,
    actor: User,
    project_id: uuid.UUID,
    data: EmployerContractItemsBulkCreate,
) -> tuple[list[EmployerContractItem], Project]:
    """TKL-B3.1: çoklu katalog seçicisinin toplu ekleme ucu — HEP YA HİÇ.

    "Önce doğrula sonra yaz": TÜM kalemler doğrulanmadan hiçbir satır `session`a
    eklenmez; herhangi bir hata hiçbir şey yazılmadan fırlar. Doğrulama sırası: grup
    (422) → katalog (404) → gövde içi kod tekrarı (409) → DB'deki kodla çakışma (409).
    """
    project = await _visible_project(session, actor, project_id)
    if project.contract is None:
        raise NotFoundError(CONTRACT_MISSING)
    entries = data.items

    group_ids = {entry.group_id for entry in entries}
    for group_id in sorted(group_ids):
        await _ensure_group_in_project(session, group_id, project.id)
    catalog_ids = {e.catalog_item_id for e in entries if e.catalog_item_id is not None}
    source_codes: dict[uuid.UUID, str | None] = {}
    if catalog_ids:
        # TEK sorgu (N+1 yok); eksik varsa tekil uçla aynı mesajla 404. KAT-B2.1: aynı
        # sorgu Bakanlık poz no'sunu da okur (snapshot kopyası).
        source_codes = {
            row.id: row.source_code
            for row in await session.execute(
                select(EvCatalogItem.id, EvCatalogItem.source_code).where(
                    EvCatalogItem.id.in_(catalog_ids)
                )
            )
        }
        if source_codes.keys() != catalog_ids:
            raise NotFoundError(CATALOG_ITEM_MISSING)

    seen: set[str] = set()
    for entry in entries:
        if entry.code in seen:
            raise DuplicateError(f"{DUPLICATE_ITEM_CODE}: {entry.code}")
        seen.add(entry.code)
    taken = await repository.list_employer_item_codes(session, project.id, sorted(seen))
    for entry in entries:
        if entry.code in taken:
            raise DuplicateError(f"{DUPLICATE_ITEM_CODE}: {entry.code}")

    items = [
        _new_item(project.id, entry.group_id, entry, source_codes.get(entry.catalog_item_id))
        for entry in entries
    ]
    session.add_all(items)
    await session.flush()
    return items, project


async def _refresh_mirror_boq_rows(
    session: AsyncSession,
    project_id: uuid.UUID,
    item: EmployerContractItem,
    mirrored_updates: dict[str, object],
) -> int:
    """TB4/B3: kalemin ayna BOQ satırlarını AYNI işlemde tazeler; adedini döner.

    Ayna satırlar `distribution_quantity.index_allocations` TEK KAYNAĞINDAN
    gelir (TB4/B2 otorite kümesi): dağıtım ekranının hücre saydığı küme neyse,
    tazelenen küme de odur — ikinci bir "bağlı satırlar" sorgusu icat etmek iki
    tanımı yeniden ayrıştırırdı. Kapsam dolayısıyla projenin ŞANTİYELERİDİR;
    devredilmiş bir şantiyede kalmış kopya bu sözleşme tarafından yönetilmez.

    Otorite kümenin İKİNCİ (belgelenmemişken T5'te yazıya geçen) sınırı: aynı
    hücreye (kalem, şantiye) düşen birden çok BOQ satırından yalnız İLKİ küme
    içindedir (`index_allocations`ın `setdefault`i, `id` sırasıyla determinist).
    İkinci satır — BOQ ekranından `code` düzenlenerek doğabilir — tazelenmez ve
    BAYAT fiyat/açıklama ile BOQ ekranında kalır. Bu bilinçli sınırdır: hücre
    tekilliği kotanın tanımıdır, tazelemeyi kotanın görmediği bir satıra
    genişletmek iki kümeyi yeniden ayrıştırırdı.

    MİKTARA DOKUNULMAZ (`MIRRORED_ITEM_FIELDS`): miktar dağıtımın kararıdır,
    kalem PATCH'i onu yeniden yazarsa kullanıcının kotası sessizce kaybolurdu.

    `code` tazelemesi `uq_boq_items_site_code`'a çarpabilir (hedef şantiyede o
    numarayı tutan başka bir satır olabilir). `IntegrityError` ile 500'e düşmek
    yerine dağıtım yazma yolunun kullandığı AYNI 409 (`BOQ_CODE_TAKEN_IN_SITE`)
    verilir — kalem kodu ile şantiye BOQ'su gerçekten çelişiyordur. PATCH
    TAMAMEN reddedilir, kısmi senkron bırakılmaz (S8).

    Gövde ÇARPILAN şantiyenin adını ve kodu taşır (S8,
    `guards.boq_code_taken_in_site`): bu ekran BOQ'yu göstermez ve kalem birden
    çok şantiyeye dağıtılmış olabilir. Bildirilen şantiye ilk çakışan ayna
    satırın şantiyesidir — okuma `ORDER BY id` ile determinist geldiği için
    (`repository.list_boq_items_for_sites`) aynı veri aynı şantiyeyi bildirir.
    """
    if not mirrored_updates:
        return 0

    sites = await sites_repository.list_sites_for_project(session, project_id)
    boq_rows = await repository.list_boq_items_for_sites(session, [site.id for site in sites])
    allocations = distribution_quantity.index_allocations(boq_rows)
    mirrors = [
        row
        for (contract_item_id, _site_id), row in allocations.items()
        if contract_item_id == item.id
    ]
    if not mirrors:
        return 0

    new_code = mirrored_updates.get("code")
    if new_code is not None:
        mirror_ids = {row.id for row in mirrors}
        taken = {
            row.site_id for row in boq_rows if row.code == new_code and row.id not in mirror_ids
        }
        clash = next((row for row in mirrors if row.site_id in taken), None)
        if clash is not None:
            site_names = {site.id: site.name for site in sites}
            raise DuplicateError(boq_code_taken_in_site(site_names[clash.site_id], str(new_code)))

    for row in mirrors:
        for field, value in mirrored_updates.items():
            setattr(row, field, value)
    return len(mirrors)


async def update_employer_item(
    session: AsyncSession, actor: User, item_id: uuid.UUID, data: EmployerContractItemUpdate
) -> tuple[EmployerContractItem, Project, int]:
    """`group_id` verilirse spec §3.2 tutarlılığı tekrar kontrol edilir (başka

    sözleşmenin grubuna taşıma yasak); `code` değişirse tekillik tekrar kontrol
    edilir; `quantity` küçültülürse spec §3.3 kalan hesabı negatif OLAMAZ —
    dağıtılmış toplamın altına indirme 422 döner (task C6 kararı).

    TB4/B3+S7: `MIRRORED_ITEM_FIELDS` (`code`/`description`/`unit`/`unit_price`)
    kümesinden biri gerçekten DEĞİŞTİYSE kalemin ayna BOQ satırları
    aynı işlemde tazelenir (S2: senkron tazeleme, snapshot DEĞİL) ve tazelenen
    satır adedi döner — router mevcut `update` denetim satırının detayına yazar,
    yeni bir `AuditAction` açılmaz.

    Kilit `repository.lock_employer_items` (TB1) — dağıtımın kullandığı desenin
    aynısı, `ORDER BY id` ile: doğrulamayı ve tazelemeyi besleyen okumalardan
    ÖNCE alınır, böylece eşzamanlı bir dağıtım kaydı ile kalem güncellemesi
    birbirinin okuduğu "dağıtılmış"ı geçersizleştiremez. Aynı sıra iki yazma
    yolunda da kullanıldığı için deadlock doğmaz.
    """
    item, project = await _visible_item(session, actor, item_id)
    await repository.lock_employer_items(session, project.id)

    updates = data.model_dump(exclude_unset=True)
    if "group_id" in updates:
        await _ensure_group_in_project(session, updates["group_id"], project.id)
    if "code" in updates and updates["code"] != item.code:
        await _ensure_code_unique(session, project.id, updates["code"], exclude_item_id=item.id)
    if "quantity" in updates:
        distributed = await _distributed_quantity(session, project.id, item.id)
        if updates["quantity"] < distributed:
            raise SiteValidationError(ITEM_QUANTITY_BELOW_DISTRIBUTED)

    # DEĞİŞMEYEN alan tazeleme saymaz: aynı değeri yeniden göndermek denetim
    # satırına "N satır tazelendi" yazdırmamalı (olmayan bir etki bildirilmez).
    mirrored_updates = {
        field: updates[field]
        for field in MIRRORED_ITEM_FIELDS
        if field in updates and updates[field] != getattr(item, field)
    }
    refreshed = await _refresh_mirror_boq_rows(session, project.id, item, mirrored_updates)

    # TKL-B3.1: fiyat DEĞER olarak değiştiyse damga ilerler (1250.5 == 1250.50 değişim
    # değil; karşılaştırma DB ölçeğinde — kuruş altı fark yazılan değeri değiştirmez).
    price_changed = "unit_price" in updates and _quantize_money(
        updates["unit_price"]
    ) != _quantize_money(item.unit_price)

    for field, value in updates.items():
        setattr(item, field, value)
    if price_changed:
        item.price_changed_at = datetime.now(UTC)
    await session.flush()
    await session.refresh(item)
    return item, project, refreshed


# --- Silme uçları (task C12, spec §7) ---
#
# Kimlik SİLMEDEN ÖNCE okunur (`boq/service.py.delete_item` deseninin aynısı):
# denetim metni satır yok olduktan sonra kurulursa `project.name`/`group.name`
# güvenilir okunamaz. Primitif değerler DÖNER, ORM nesnesi DEĞİL — silinmiş
# bir nesnenin alanına flush sonrası erişmek `ObjectDeletedError` riski taşır.


async def delete_employer_group(
    session: AsyncSession, actor: User, group_id: uuid.UUID
) -> tuple[str, str]:
    """409 `GROUP_HAS_ITEMS`: grupta kalem varsa silinmez (spec §7). Kapı `_ADMIN`

    (`boq/router.py.delete_boq_item_endpoint` deseninin aynısı, `can_delete`
    istisnası burada YOK — yalnız `subcontractor_contracts` silme ucunda geçerli).
    """
    group, project = await _visible_group(session, actor, group_id)
    if await repository.employer_group_has_items(session, group.id):
        raise RelatedRecordsExistError(GROUP_HAS_ITEMS)
    project_name, group_name = project.name, group.name
    await session.delete(group)
    await session.flush()
    return project_name, group_name


async def delete_employer_item(
    session: AsyncSession, actor: User, item_id: uuid.UUID
) -> tuple[str, str, str]:
    """Engel YOK (spec §7): bağlı `boq_items.contract_item_id` DB'de `ON DELETE

    SET NULL` ile serbest kalır — şantiyenin kendi başına girdiği bir poz gibi
    (`contract_item_id IS NULL`) BOQ'da kalmaya devam eder, satır SİLİNMEZ.
    """
    item, project = await _visible_item(session, actor, item_id)
    project_name, code, description = project.name, item.code, item.description
    await session.delete(item)
    await session.flush()
    return project_name, code, description

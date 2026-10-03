"""Poz Dağılımı uçları (task C7 okuma + task C8 yazma, spec §6.3, `POZ` mockup).

`GET /projects/{project_id}/contract/distribution` — bir işveren sözleşmesi
pozunun projenin şantiyelerine nasıl bölündüğünü gösteren matris. Salt okuma:
hiçbir şey yazmaz, `record_audit` çağırmaz (okuma uçları denetim günlüğüne
yazmaz — task brief kararı).

İki katmanlı koruma `contracts/service.py`'nin aynısı: router'daki `_VIEW`
YETKİYİ verir, burada yeniden kullanılan `service._visible_project` KAPSAMI
belirler — görünmeyen projenin dağılımı asla dönmez, görünmeyen kayıt ile
var olmayan kayıt AYNI 404 gövdesini verir.

Sorgu sayısı (N+1 YOK, task brief kısıtı):
1. `visible_projects` (service._visible_project içinde, mevcut desen)
2. `list_sites_for_project` — projenin şantiyeleri
3. `list_employer_groups` — gruplar
4. gruplar `.items` erişilince `lazy="selectin"` tetiklenir — TÜM grupların
   kalemleri TEK ek sorguda (C1'in tanımı)
5. `list_boq_items_for_sites` — şantiyelerin BOQ satırları TEK `IN` sorgusunda
   (dağıtım hücreleri `distribution_quantity.index_allocations` ile süzülür —
   TB4/B2: "kalan" ile aşım kontrolü AYNI kümeden beslenir)

Toplam: kalem/şantiye sayısından BAĞIMSIZ, sabit sayıda sorgu.
"""

import uuid
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, DuplicateError, NotFoundError, SiteValidationError
from app.modules.boq import repository as boq_repository
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.boq.service import _QUANTITY_BELOW_ALLOCATED
from app.modules.contracts import distribution_quantity, repository
from app.modules.contracts.distribution_quantity import AllocationKey
from app.modules.contracts.guards import (
    BOQ_CODE_TAKEN_IN_SITE,
    CONTRACT_MISSING,
    DISTRIBUTION_EXCEEDS,
    DUPLICATE_ALLOCATION,
    ITEM_MISSING,
    SITE_PROJECT_MISMATCH,
)
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.contracts.schemas import (
    ContractAllocationInput,
    ContractDistributionAllocation,
    ContractDistributionGroup,
    ContractDistributionItem,
    ContractDistributionResponse,
    ContractDistributionSave,
    ContractDistributionSite,
    ContractDistributionSiteItem,
    ContractDistributionSiteSummary,
)
from app.modules.contracts.service import _visible_project, apply_mirrored_fields
from app.modules.sites import repository as sites_repository
from app.modules.sites.models import Site
from app.modules.users.models import User

_MONEY = Decimal("0.01")


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY, rounding=ROUND_HALF_UP)


def _allocations_by_item(
    allocations: dict[AllocationKey, BoqItem],
) -> dict[uuid.UUID, list[BoqItem]]:
    """Otorite hücre kümesini (TB4/B2) kalem başına listeye çevirir."""
    grouped: dict[uuid.UUID, list[BoqItem]] = defaultdict(list)
    for (contract_item_id, _site_id), row in allocations.items():
        grouped[contract_item_id].append(row)
    return grouped


def _to_distribution_item(
    item: EmployerContractItem, allocations: list[BoqItem], distributed_total: Decimal
) -> ContractDistributionItem:
    return ContractDistributionItem(
        id=item.id,
        code=item.code,
        source_code=item.source_code,
        description=item.description,
        unit=item.unit,
        quantity=item.quantity,
        unit_price=item.unit_price,
        allocations=[
            ContractDistributionAllocation(
                site_id=row.site_id, quantity=row.quantity, boq_item_id=row.id
            )
            for row in allocations
        ],
        remaining_quantity=item.quantity - distributed_total,
    )


def _site_summaries(
    sites: list[Site],
    items: list[EmployerContractItem],
    allocations_by_item: dict[uuid.UUID, list[BoqItem]],
) -> list[ContractDistributionSiteSummary]:
    """`POZ` 168-187: şantiye başına dağıtılmış kalemler + tutar.

    Spec §3.3: `total_amount` **sözleşme kaleminin** birim fiyatıyla hesaplanır,
    BOQ satırının değil — ikisi normalde aynıdır ama sözleşme otoritedir.
    """
    items_by_id = {item.id: item for item in items}
    summaries: list[ContractDistributionSiteSummary] = []
    for site in sites:
        site_items: list[ContractDistributionSiteItem] = []
        for contract_item_id, allocations in allocations_by_item.items():
            item = items_by_id.get(contract_item_id)
            if item is None:
                continue
            for row in allocations:
                if row.site_id != site.id:
                    continue
                site_items.append(
                    ContractDistributionSiteItem(
                        code=item.code,
                        description=item.description,
                        quantity=row.quantity,
                        unit_price=item.unit_price,
                        amount=_quantize_money(row.quantity * item.unit_price),
                    )
                )
        total_amount = _quantize_money(sum((row.amount for row in site_items), Decimal("0")))
        summaries.append(
            ContractDistributionSiteSummary(
                site_id=site.id,
                site_name=site.name,
                items=site_items,
                total_amount=total_amount,
            )
        )
    return summaries


async def build_distribution(
    session: AsyncSession, actor: User, project_ref: uuid.UUID | str
) -> ContractDistributionResponse:
    project = await _visible_project(session, actor, project_ref)
    if project.contract is None:
        raise NotFoundError(CONTRACT_MISSING)

    # `project.id` — `project_ref` slug olabilir (URL-4).
    sites = await sites_repository.list_sites_for_project(session, project.id)
    groups = await repository.list_employer_groups(session, project.id)
    all_items = [item for group in groups for item in group.items]

    boq_rows = await repository.list_boq_items_for_sites(session, [site.id for site in sites])
    allocations = distribution_quantity.index_allocations(boq_rows)
    allocations_by_item = _allocations_by_item(allocations)
    distributed = distribution_quantity.distributed_totals(allocations)

    undistributed_items = [item for item in all_items if not allocations_by_item.get(item.id)]

    return ContractDistributionResponse(
        sites=[ContractDistributionSite(id=site.id, name=site.name) for site in sites],
        groups=[
            ContractDistributionGroup(
                id=group.id,
                name=group.name,
                sort_order=group.sort_order,
                items=[
                    _to_distribution_item(
                        item,
                        allocations_by_item.get(item.id, []),
                        distributed.get(item.id, Decimal("0")),
                    )
                    for item in group.items
                ],
            )
            for group in groups
        ],
        undistributed_item_count=len(undistributed_items),
        undistributed_item_names=[item.description for item in undistributed_items],
        site_summaries=_site_summaries(sites, all_items, allocations_by_item),
        distributed_item_count=len(all_items) - len(undistributed_items),
        total_item_count=len(all_items),
    )


# --- Yazma yolu (task C8, spec §6.3 PUT kısmı, `POZ` 24 "Dağılımı Kaydet") ---
#
# TASARIM KISITI — SIRA: ÖNCE TÜM DOĞRULAMALAR, SONRA TÜM YAZMA.
# Doğrulama yazmanın arasına serpiştirilirse, ikinci satırda patlayan bir istek
# birinci satırı çoktan session'a eklemiş olur. Oturum `get_db` içinde rollback
# edilse bile (edilir: `app/core/db.py` `except: await session.rollback()`), bu
# iki katmanlı güvencenin YALNIZ dış katmanı olur; iç katman — hiç yazmamak —
# burada, `save_distribution`'ın gövde sırasıyla kurulur. `POZ` 72: "sözleşme
# miktarı = tüm şantiye kotaları toplamı" — kısmi yazılmış bir dağılım ekranı
# bu eşitliği sessizce bozar.

_AllocKey = AllocationKey  # (contract_item_id, site_id) — TB4/B2 tek kaynak tipi


def _validate_targets(
    allocations: list[ContractAllocationInput],
    items_by_id: dict[uuid.UUID, EmployerContractItem],
    site_ids: set[uuid.UUID],
) -> set[_AllocKey]:
    """Kimlik + kapsam + tekillik. Hiçbir yazma YAPILMAZ."""
    keys: set[_AllocKey] = set()
    for alloc in allocations:
        # IDOR: proje görünürlük süzgecinden geçmiş olsa da gövdedeki kalem
        # BAŞKA projenin kalemi olabilir — ayırt edilemez 404.
        if alloc.contract_item_id not in items_by_id:
            raise NotFoundError(ITEM_MISSING)
        if alloc.site_id not in site_ids:
            raise SiteValidationError(SITE_PROJECT_MISMATCH)
        key = (alloc.contract_item_id, alloc.site_id)
        if key in keys:
            raise SiteValidationError(DUPLICATE_ALLOCATION)
        keys.add(key)
    return keys


def _assert_within_contract_quantity(
    allocations: list[ContractAllocationInput],
    items_by_id: dict[uuid.UUID, EmployerContractItem],
    existing_by_key: dict[_AllocKey, BoqItem],
    body_keys: set[_AllocKey],
) -> None:
    """Σ kota ≤ `contract_item.quantity` (spec §3.3/§6.3 madde 4).

    Hesap ÇAĞRININ TAMAMI üzerinden yapılır ve gövdede YER ALMAYAN mevcut
    kotalar da eklenir: gövde ekranın tamamıdır ama bir kalem hiç gönderilmemiş
    olabilir, o kalemin mevcut hücreleri korunur. Yalnız gövdedeki satırlara
    bakmak, dokunulmamış bir şantiyenin kotasını yok sayıp aşımı kaçırırdı.

    Mevcut kotaların toplamı `distribution_quantity` TEK KAYNAĞINDAN gelir
    (TB4/B2): ekranın gösterdiği "kalan" ile bu kapının saydığı "dağıtılmış"
    aynı kümedir, biri diğerinden sapamaz.
    """
    totals: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    totals.update(distribution_quantity.distributed_totals(existing_by_key, exclude=body_keys))
    for alloc in allocations:
        if alloc.quantity is not None:
            totals[alloc.contract_item_id] += alloc.quantity
    for item_id, total in totals.items():
        item = items_by_id.get(item_id)
        if item is not None and total > item.quantity:
            raise SiteValidationError(DISTRIBUTION_EXCEEDS)


def _plan_new_rows(
    allocations: list[ContractAllocationInput],
    items_by_id: dict[uuid.UUID, EmployerContractItem],
    existing_by_key: dict[_AllocKey, BoqItem],
    site_boq_items: list[BoqItem],
) -> dict[_AllocKey, BoqItem]:
    """Yeni açılacak her çift için planı çıkarır; `uq_boq_items_site_code`
    çakışmasını `IntegrityError`'dan ÖNCE yakalar. **Hiçbir yazma YAPMAZ.**

    Dönen sözlük: yeni çift → o şantiyede kodu eşleşen BAĞSIZ (`contract_item_id
    IS NULL`) satır. Bu satır SİLİNİP yeniden açılmaz, YENİDEN BAĞLANIR
    (`_apply_allocations`). İki durumu birden çözer:

    * `quantity: null` ile bağı koparılan satır şantiyede kodu tutmaya devam
      eder; yeniden kota verilince "kod dolu" diye 409 atılırsa "kotayı kaldır"
      işlemi GERİ ALINAMAZ olurdu (düzeltme turu 1, Important 1).
    * Şantiyenin kendi başına girdiği aynı numaralı poz (spec §3.3'te meşru)
      zaten AYNI pozdur — ikinci bir satır açmak yerine sahiplenilir, sahadaki
      iş kaydı korunur.

    `BOQ_CODE_TAKEN_IN_SITE` yalnız GERÇEKTEN çözülemeyen çakışmada kalır: kodu
    tutan satır BAŞKA bir sözleşme kalemine bağlıysa (BOQ ekranından `code`
    düzenlenmişse olabilir) sahiplenmek o bağı sessizce çalmak olurdu.

    Aday havuzundan `pop` edilir: aynı çağrıda iki çift aynı bağsız satırı
    sahiplenemez (kodlar `(project_id, code)` benzersiz olduğu için pratikte
    doğmaz, yine de plan üretimi kendi içinde tutarlı kalır).
    """
    relinkable = {
        (row.site_id, row.code): row for row in site_boq_items if row.contract_item_id is None
    }
    taken_codes = {(row.site_id, row.code) for row in site_boq_items}
    plan: dict[_AllocKey, BoqItem] = {}
    for alloc in allocations:
        if alloc.quantity is None:
            continue
        key = (alloc.contract_item_id, alloc.site_id)
        if key in existing_by_key:
            continue  # güncelleme — yeni satır açılmıyor, kod zaten kendisinin
        item = items_by_id[alloc.contract_item_id]
        code_key = (alloc.site_id, item.code)
        relink_target = relinkable.pop(code_key, None)
        if relink_target is not None:
            plan[key] = relink_target
            continue
        if code_key in taken_codes:
            raise DuplicateError(BOQ_CODE_TAKEN_IN_SITE)
        taken_codes.add(code_key)
    return plan


def _resolve_boq_group(
    session: AsyncSession,
    cache: dict[tuple[uuid.UUID, str], BoqGroup],
    site_id: uuid.UUID,
    contract_group: EmployerContractGroup,
) -> BoqGroup:
    """Hedef şantiyede sözleşme grubuyla AYNI ADLI grup; yoksa o adla açılır.

    Önbellek yalnız DB'den gelen grupları değil, BU ÇAĞRIDA açılanları da tutar
    — aksi hâlde aynı gruba düşen iki kalem aynı adla İKİ grup açardı.
    """
    key = (site_id, contract_group.name)
    group = cache.get(key)
    if group is None:
        # `id` BURADA üretilir: yeni grubun kimliği aynı flush'ta açılacak BOQ
        # satırının `group_id`'sinde (NOT NULL) gerekir; ara flush atmadan
        # bağlamanın en yalın yolu budur. Insert sırasını SQLAlchemy'nin
        # tablo bağımlılık sıralaması (grup → satır) garanti eder.
        group = BoqGroup(
            id=uuid.uuid4(),
            site_id=site_id,
            name=contract_group.name,
            sort_order=contract_group.sort_order,
        )
        session.add(group)
        cache[key] = group
    return group


async def _assert_quota_covers_section_allocations(
    session: AsyncSession,
    allocations: list[ContractAllocationInput],
    existing_by_key: dict[_AllocKey, BoqItem],
    relink_plan: dict[_AllocKey, BoqItem],
) -> None:
    """🔴 K3'ün (`SUM(bölüm tahsisi) <= boq_items.quantity`) ÜÇÜNCÜ yazma kapısı.

    `boq/models.py:135-138` invariantın İKİ yazma kapısı olduğunu yazar
    (`boq.service.replace_allocations` toplamı ARTIRIR, `boq.service.update_item`
    kotayı DÜŞÜRÜR) — ama `_apply_allocations` mevcut bir BOQ satırının
    `quantity`sini de düşürür (madde 3 ve relink dalı) ve aynı invariantı ters
    yönden kırar: 1.200'ün 700'ü bölümlere tahsisliyken hücreyi 500'e çekmek
    `SUM > quantity` bırakır, `unallocated_quantity` NEGATİF serileşir ve
    hiçbir uç bunu bir daha fark etmez.

    Eşik `update_item` ile BİREBİRDİR (`allocated > quantity` → 409, eşitlik
    geçerli) ve aynı mesajı taşır — ikinci bir metin kopyası zamanla ayrışırdı.
    `_assert_within_contract_quantity` bu boşluğu kapatmaz: o toplamı YUKARI
    sınırlar, aşağı çekmeyi hiç ölçmez.

    🔴 EŞİK = KİLİT (İK-2 dersi) — ve kilit KOŞULSUZDUR. Bu fonksiyonun ilk
    hâli "kotası DÜŞEN satırlar" kararını `save_distribution`ın kilitsiz okuma
    fazından (`list_boq_items_for_sites`, `FOR UPDATE` YOK) gelen STALE
    `row.quantity` ile veriyordu — tam da `boq.service.update_item`'ın
    KAÇINDIĞI TOCTOU: T1 bu satırı 500 okur, T1 flush/commit etmeden T2
    araya girip kotayı 500→1200 yükseltir ve 700 tahsis ekler (ikisi de
    `boq.service`nin KENDİ kilidiyle geçerli), T1 kararını hâlâ 500'e göre
    verip 600'ü "artış" sanır — kilit hiç alınmaz, `allocated_total_for_item`
    hiç çağrılmaz, 600 < 700 yazılır (bkz. `tests/contracts/
    test_distribution_k3_toctou.py`). Bu yüzden karar ARTIK "artış mı"
    kararını TAŞIMAZ: gövdede yazılacak (yani `existing_by_key`/`relink_plan`
    üzerinden mevcut bir satırı hedefleyen) HER hücrenin satırı `lock_item`
    ile `FOR UPDATE` alınır (diğer iki kapının kullandığı kilidin AYNISI,
    `populate_existing` ile TAZELENİR) ve "düşüyor mu" kararı ancak kilitli/
    taze değere göre verilir. Kilit sırası `id` artan —
    `lock_employer_items`'ın `ORDER BY id` disipliniyle aynı yönde, deadlock
    doğurmaz. Gövdede HİÇ geçmeyen ("dokunulmamış") hücreler bu döngüye
    girmez — ek sorgu yalnız BU isteğin yazacağı satırlar için vardır.
    """
    hedef_miktar: dict[uuid.UUID, Decimal] = {}
    for alloc in allocations:
        if alloc.quantity is None:
            continue
        key = (alloc.contract_item_id, alloc.site_id)
        row = existing_by_key.get(key)
        if row is None:
            row = relink_plan.get(key)
        if row is None:
            continue
        hedef_miktar[row.id] = alloc.quantity

    for item_id in sorted(hedef_miktar):
        locked = await boq_repository.lock_item(session, item_id)
        if locked is None:  # pragma: no cover — satır bu işlemde okundu
            continue
        if hedef_miktar[item_id] >= locked.quantity:
            continue
        allocated = await boq_repository.allocated_total_for_item(session, item_id)
        if allocated > hedef_miktar[item_id]:
            raise ConflictError(_QUANTITY_BELOW_ALLOCATED)


def _apply_allocations(
    session: AsyncSession,
    allocations: list[ContractAllocationInput],
    items_by_id: dict[uuid.UUID, EmployerContractItem],
    group_by_item: dict[uuid.UUID, EmployerContractGroup],
    existing_by_key: dict[_AllocKey, BoqItem],
    relink_plan: dict[_AllocKey, BoqItem],
    group_cache: dict[tuple[uuid.UUID, str], BoqGroup],
) -> None:
    """Spec §6.3 madde 1-3. Buraya gelindiğinde doğrulama BİTMİŞTİR."""
    for alloc in allocations:
        key = (alloc.contract_item_id, alloc.site_id)
        row = existing_by_key.get(key)

        if alloc.quantity is None:
            # Madde 1: bağ kopar, SATIR SİLİNMEZ — sahadaki iş kaydı (günlük
            # kayıt, hakediş) bu satıra bağlı doğar (spec §3.3 `SET NULL`).
            if row is not None:
                row.contract_item_id = None
            continue

        if row is not None:
            row.quantity = alloc.quantity  # Madde 3: YALNIZ miktar
            continue

        item = items_by_id[alloc.contract_item_id]

        relink_target = relink_plan.get(key)
        if relink_target is not None:
            # Madde 2'nin ikinci hâli: satır zaten var ama BAĞSIZ — yeniden
            # bağlanır. Grup DEĞİŞTİRİLMEZ (satır şantiyenin BOQ'sunda yerini
            # korur); tanımlayıcı alanlar sözleşmeden TAZELENİR, çünkü artık
            # otorite sözleşme kalemidir.
            #
            # Alan listesi BURADA TUTULMAZ (TB4/S7): kalem PATCH'inin senkron
            # tazelemesiyle AYNI `MIRRORED_ITEM_FIELDS` kümesinden beslenir —
            # iki kopya zamanla ayrışır ve ayrışan taraf bayat ayna üretirdi.
            # (`code` zaten eşit: relink adayı koda göre seçildi.)
            relink_target.contract_item_id = item.id
            apply_mirrored_fields(relink_target, item)
            relink_target.quantity = alloc.quantity
            continue

        # Madde 2: yeni satır — tanımlayıcı alanlar sözleşme kaleminden AYNI
        # `MIRRORED_ITEM_FIELDS` kümesiyle kopyalanır (TB4/S7): üçüncü bir alan
        # listesi kopyası bırakılmaz. Kümede OLMAYAN iki alan burada AÇIKÇA
        # verilir — `quantity` dağıtımın kararıdır, `sort_order` ise yalnız
        # satır DOĞARKEN kalemden alınır (sonradan şantiyenin kendi sıralaması).
        group = _resolve_boq_group(session, group_cache, alloc.site_id, group_by_item[item.id])
        row = BoqItem(
            site_id=alloc.site_id,
            group_id=group.id,
            contract_item_id=item.id,
            quantity=alloc.quantity,
            sort_order=item.sort_order,
        )
        apply_mirrored_fields(row, item)
        session.add(row)


async def save_distribution(
    session: AsyncSession, actor: User, project_id: uuid.UUID, data: ContractDistributionSave
) -> ContractDistributionResponse:
    """`PUT /projects/{id}/contract/distribution` — ekranın tamamı tek atomik yazma.

    ## Semantik: BİRLEŞTİRME, tam-değiştirme DEĞİL (kullanıcı kararı)

    Gövde ekranın tamamıdır ama **gövdede geçmeyen (kalem, şantiye) hücresi
    KORUNUR** — dokunulmamış sayılır, silinmez. Bir hücreyi SİLMEK için o çift
    açıkça `quantity: null` ile gönderilmelidir (spec §6.3 madde 1).

    Frontend için kritik: kullanıcının boşalttığı hücreyi göndermemek "sil"
    ANLAMINA GELMEZ; boş hücre `quantity: null` olarak gönderilmelidir, aksi
    hâlde kullanıcı kotayı sildiğini sanırken bağ yerinde durur. Aşım hesabı da
    bu semantiği izler: gövdede yer almayan mevcut kotalar toplama dahildir
    (`_assert_within_contract_quantity`).

    Yanıt `build_distribution` ile üretilir: ekran kaydettikten sonra ikinci bir
    GET atmadan güncel matrisi alır (kalan/uyarı/şantiye özeti dahil).
    """
    project = await _visible_project(session, actor, project_id)
    if project.contract is None:
        raise NotFoundError(CONTRACT_MISSING)

    # --- 0. KİLİT (TB1/T2, spec §2) ---
    # Doğrulamayı besleyen HİÇBİR okumadan önce sözleşme kalemleri satır kilidi
    # altına alınır (`repository.lock_employer_items`, `get_contract_locked`
    # deseni). Aksi hâlde "önce doğrula, sonra yaz" sırası tek başına yetmez:
    # iki eşzamanlı istek aynı "kalan"ı okur, ikisi de kotayı geçerli sanır,
    # ikisi de yazar ve toplam dağıtım sözleşme kalemini AŞAR (TOCTOU). Kilit
    # ALTINDA ikinci istek beklemek zorunda kalır, kilidi alınca birincinin
    # yazdığını GÖRÜR ve `DISTRIBUTION_EXCEEDS` (422) ile döner.
    # Kilit sırası `ORDER BY id`'dir ve tek yazma yolu burasıdır — sıra
    # tutarsızlığından doğacak deadlock riski repository docstring'inde.
    await repository.lock_employer_items(session, project_id)

    sites = await sites_repository.list_sites_for_project(session, project_id)
    site_ids = {site.id for site in sites}
    groups = await repository.list_employer_groups(session, project_id)
    items_by_id = {item.id: item for group in groups for item in group.items}
    group_by_item = {item.id: group for group in groups for item in group.items}

    # --- 1. DOĞRULAMA (hiçbir yazma yok) ---
    body_keys = _validate_targets(data.allocations, items_by_id, site_ids)

    site_boq_items = await repository.list_boq_items_for_sites(session, [s.id for s in sites])
    existing_by_key = distribution_quantity.index_allocations(site_boq_items)
    _assert_within_contract_quantity(data.allocations, items_by_id, existing_by_key, body_keys)
    relink_plan = _plan_new_rows(data.allocations, items_by_id, existing_by_key, site_boq_items)
    await _assert_quota_covers_section_allocations(
        session, data.allocations, existing_by_key, relink_plan
    )

    # --- 2. YAZMA (buradan sonra doğrulama YOK) ---
    boq_groups = await repository.list_boq_groups_for_sites(session, [s.id for s in sites])
    # `BoqGroup`'ta `(site_id, name)` benzersiz DEĞİL — aynı adlı iki grup
    # varsa EN ESKİSİ seçilir (`setdefault` + repository'nin `created_at, id`
    # sıralaması): seçim nondeterministik kalırsa aynı istek farklı gruplara
    # yazabilirdi.
    group_cache: dict[tuple[uuid.UUID, str], BoqGroup] = {}
    for group in boq_groups:
        group_cache.setdefault((group.site_id, group.name), group)
    _apply_allocations(
        session,
        data.allocations,
        items_by_id,
        group_by_item,
        existing_by_key,
        relink_plan,
        group_cache,
    )
    await session.flush()

    return await build_distribution(session, actor, project_id)

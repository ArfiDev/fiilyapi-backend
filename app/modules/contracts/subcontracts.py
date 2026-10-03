"""Taşeron sözleşmesi POST/GET/PATCH servis katmanı (spec §6.5, task C10).

`sites/service.py.create_site`/`update_site` deseninin birebiri: doğrulama +
kullanıcı/kartoteks çözümü YAZMADAN ÖNCE biter (§8.2 atomiklik), taslak→yayın
geçişi BİRLEŞİK kayıt üzerinde tam doğrulama koşturur, genel PATCH dalında
zorunluluk kuralları KOŞMAZ (`sites/guards.py` §0.3/3 dersi).

Kurallar `guards.validate_subcontract`den TEK KOPYA çağrılır — burada
yeniden yazılmaz. `site_id`→proje eşleşmesi ve `subcontractor_name` anlık
görüntüsü DB erişimi gerektirdiği için `validate_subcontract` içinde değil,
burada kontrol edilir (guards.py docstring'inin aynı gerekçesi).
"""

import uuid
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel, can_delete
from app.core.errors import (
    DeleteNotAllowedError,
    DuplicateError,
    NotFoundError,
    SiteValidationError,
)
from app.core.slug import allocate_slug
from app.modules.contracts import guards, repository
from app.modules.contracts.models import (
    ContractStatus,
    EmployerContractItem,
    SubcontractorContract,
    SubcontractorContractItem,
)
from app.modules.contracts.schemas import (
    SubcontractorContractCreate,
    SubcontractorContractDetail,
    SubcontractorContractItemCreate,
    SubcontractorContractItemGroup,
    SubcontractorContractItemResponse,
    SubcontractorContractItemUpdate,
    SubcontractorContractListItem,
    SubcontractorContractListResponse,
    SubcontractorContractUpdate,
)
from app.modules.contracts.service import _subcontractor_amount, _visible_project
from app.modules.projects.models import Project
from app.modules.projects.service import visible_projects
from app.modules.roles.repository import get_permission
from app.modules.sites import repository as sites_repository
from app.modules.users.models import User

# Spec §3.5: global kısmi benzersizlik (dolu contract_no'lar çakışamaz).
# `subcontractors.py._DUPLICATE_TAX_NUMBER` deseninin aynısı — spec §4
# tablosunun BİREBİR listesinde değildir, bu yüzden `guards.py`de DEĞİL burada
# durur (task C9 kararının aynısı).
_DUPLICATE_CONTRACT_NO = "Bu sözleşme no ile kayıtlı bir sözleşme zaten var."

# PATCH'in BİRLEŞİK kayıt doğrulamasının okuduğu alanlar. `sites/service.py.
# _VALIDATED_FIELDS` deseninin aynısı: `items` burada YOK, kalemler ayrı
# uçlarla yönetilir (C11) — yayına geçişte "girilmiş kalemlerin hepsinde
# birim fiyat" kuralı MEVCUT kalemler üzerinden koşar.
_VALIDATED_FIELDS = (
    "subcontractor_id",
    "work_category",
    "contract_no",
    "signature_date",
    "start_date",
    "end_date",
)


async def _ensure_site_in_project(
    session: AsyncSession, site_id: uuid.UUID | None, project_id: uuid.UUID
) -> None:
    """Spec §4 tutarlılık kuralı — HER ZAMAN koşar (taslakta da)."""
    if site_id is None:
        return
    site = await sites_repository.get_site(session, site_id)
    if site is None or site.project_id != project_id:
        raise SiteValidationError(guards.SITE_PROJECT_MISMATCH)


async def _resolve_subcontractor_name(
    session: AsyncSession, subcontractor_id: uuid.UUID | None
) -> str | None:
    """Anlık görüntü — HER yazmada kartotekten kopyalanır (spec §3.5).

    `subcontractor_id` alan DEĞERİ olarak ele alınır (`sites._resolve_user_name`
    deseninin aynısı): geçersizse 404 DEĞİL, 422 döner.
    """
    if subcontractor_id is None:
        return None
    subcontractor = await repository.get_subcontractor(session, subcontractor_id)
    if subcontractor is None:
        raise SiteValidationError(guards.SUBCONTRACTOR_MISSING)
    return subcontractor.name


async def _ensure_contract_no_unique(
    session: AsyncSession, contract_no: str | None, *, exclude_id: uuid.UUID | None = None
) -> None:
    if contract_no is None:
        return
    existing = await repository.get_subcontractor_contract_by_contract_no(
        session, contract_no, exclude_id=exclude_id
    )
    if existing is not None:
        raise DuplicateError(_DUPLICATE_CONTRACT_NO)


async def create_subcontractor_contract(
    session: AsyncSession, actor: User, project_id: uuid.UUID, data: SubcontractorContractCreate
) -> tuple[SubcontractorContract, Project]:
    """`FORM` gövdesi — kalemler İÇ İÇE ve ATOMİK (spec §6.5, `sites` + bölümler

    deseninin aynısı): herhangi bir kalem geçersizse sözleşme de yazılmaz.
    `Project` da döner: router'daki denetim günlüğü satırı proje ADI ister.
    """
    project = await _visible_project(session, actor, project_id)
    # Tutarlılık: HER ZAMAN (taslakta da) — `validate_subcontract` içinde DEĞİL.
    await _ensure_site_in_project(session, data.site_id, project_id)
    subcontractor_name = await _resolve_subcontractor_name(session, data.subcontractor_id)
    guards.validate_subcontract(
        SimpleNamespace(
            project_id=project_id,
            subcontractor_id=data.subcontractor_id,
            work_category=data.work_category,
            contract_no=data.contract_no,
            signature_date=data.signature_date,
            start_date=data.start_date,
            end_date=data.end_date,
            items=data.items,
        ),
        is_draft=data.is_draft,
    )
    await _ensure_contract_no_unique(session, data.contract_no)
    source_codes = await _ensure_nested_items_valid(session, project_id, data.items)

    contract = SubcontractorContract(
        project_id=project_id,
        site_id=data.site_id,
        subcontractor_id=data.subcontractor_id,
        subcontractor_name=subcontractor_name,
        work_category=data.work_category,
        contract_no=data.contract_no,
        signature_date=data.signature_date,
        is_notarized=data.is_notarized,
        start_date=data.start_date,
        end_date=data.end_date,
        late_penalty_daily=data.late_penalty_daily,
        advance_pct=data.advance_pct,
        retainage_pct=data.retainage_pct,
        vat_pct=data.vat_pct,
        payment_period=data.payment_period,
        payment_term_days=data.payment_term_days,
        materials_by_contractor=data.materials_by_contractor,
        subcontractor_files_own_sgk=data.subcontractor_files_own_sgk,
        vat_withholding=data.vat_withholding,
        status=data.status,
        is_draft=data.is_draft,
        created_by=actor.id,
    )
    # URL-4: slug tabanı ÖNCE `contract_no`dur (mockup ÖLÇÜLDÜ — `Form -
    # Sözleşme Oluştur.dc.html:90`de `Sözleşme No` ZORUNLU alandır ve kolon
    # `uq_subcontractor_contracts_contract_no` kısmi indeksiyle doldurulduğunda
    # ŞİRKET GENELİ TEKİLDİR). Kolon yine de nullable'dır (taslak desteği), bu
    # yüzden boş numarada taban ad + iş kategorisine düşer; ikisi de boşsa slug
    # NULL kalır ve kayıt UUID'siyle yaşar.
    # Slug OLUŞTURULURKEN üretilir ve numara/ad değişince DEĞİŞMEZ (URL-2 kararı 4).
    contract.slug = await allocate_slug(
        session, _contract_slug_base(contract), SubcontractorContract.slug
    )
    session.add(contract)
    await session.flush()
    for index, item in enumerate(data.items):
        session.add(
            SubcontractorContractItem(
                contract_id=contract.id,
                source_contract_item_id=item.source_contract_item_id,
                source_code=source_codes.get(item.source_contract_item_id),
                code=item.code,
                description=item.description,
                unit=item.unit,
                quantity=item.quantity,
                unit_price=item.unit_price,
                sort_order=item.sort_order if item.sort_order is not None else index,
            )
        )
    await session.flush()
    await session.refresh(contract)
    return contract, project


def _contract_slug_base(contract: SubcontractorContract) -> str | None:
    """Taşeron sözleşmesinin slug TABANI: `contract_no`, yoksa ad + iş kategorisi.

    İkisi de boşsa `None` döner ve `allocate_slug` slug ÜRETMEZ — uydurulmuş bir
    taban (`sozlesme`) yazmak her taslakta çakışır ve hiçbir okunabilirlik
    kazandırmazdı (URL-2 `slug.py` docstring'i, "Boş slug").
    """
    if contract.contract_no and contract.contract_no.strip():
        return contract.contract_no
    parcalar = [p for p in (contract.subcontractor_name, contract.work_category) if p and p.strip()]
    return " ".join(parcalar) if parcalar else None


async def _visible_contract(
    session: AsyncSession, actor: User, contract_ref: uuid.UUID | str
) -> tuple[SubcontractorContract, Project]:
    """Sözleşme -> proje. Dolaylı kimlikle erişim de görünürlük süzgecinden

    geçmek ZORUNDA (`service._visible_group`/`_visible_item` deseninin aynısı).
    Görünmeyen projedeki gerçek kayıt ile var olmayan kayıt AYNI 404 gövdesini
    döner (spec §6, IDOR).
    """
    contract = await repository.get_subcontractor_contract(session, contract_ref)
    if contract is None:
        raise NotFoundError(guards.CONTRACT_MISSING)
    project = await _visible_project(session, actor, contract.project_id, guards.CONTRACT_MISSING)
    return contract, project


async def get_subcontractor_contract(
    session: AsyncSession, actor: User, contract_ref: uuid.UUID | str
) -> SubcontractorContract:
    contract, _ = await _visible_contract(session, actor, contract_ref)
    return contract


async def list_subcontractor_contracts(
    session: AsyncSession,
    actor: User,
    *,
    project_id: uuid.UUID | None,
    site_id: uuid.UUID | None,
    status_filter: ContractStatus | None,
    q: str | None,
    limit: int,
    offset: int,
) -> SubcontractorContractListResponse:
    """TB2 U1 — `GET /subcontractor-contracts` (spec §1), TB3 T2 ile sayfalamalı.

    `service.list_contracts` deseninin aynısı: kapsam `visible_projects`ten
    gelir ve SQL'de kalır — görünmeyen projenin sözleşmesi hiç ÇEKİLMEZ, filtre
    verilse bile (`project_id` süzgeci kapsamı GENİŞLETMEZ, daraltır). `total`
    de bu kapsamın İÇİNDEN sayılır: sayfalama görünürlükten SONRA uygulanır.
    """
    visible_ids = [project.id for project in await visible_projects(session, actor)]
    rows = await repository.list_subcontractor_contract_rows(
        session,
        visible_ids,
        project_id=project_id,
        site_id=site_id,
        status_filter=status_filter,
        q=q,
        limit=limit,
        offset=offset,
    )
    total = await repository.count_subcontractor_contract_rows(
        session,
        visible_ids,
        project_id=project_id,
        site_id=site_id,
        status_filter=status_filter,
        q=q,
    )
    return SubcontractorContractListResponse(
        items=[
            SubcontractorContractListItem(
                id=contract.id,
                # 🔴 LİSTEYE DE GİRER: bu uç `sozlesmeler/taseron/[contractId]`
                # bağlantısını üreten şemadır. URL-2'de `SiteOptionListResponse`e
                # slug EKLENMEDİĞİ için seçici slug üretememişti (`routes.ts:34-45`
                # kuralı) — aynı yarım göç tekrarlanmaz.
                slug=contract.slug,
                contract_no=contract.contract_no,
                subcontractor_name=contract.subcontractor_name,
                work_category=contract.work_category,
                project_id=contract.project_id,
                project_name=project_name,
                site_id=contract.site_id,
                site_name=site_name,
                status=contract.status,
                is_draft=contract.is_draft,
            )
            for contract, project_name, site_name in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


async def _visible_subcontract_item(
    session: AsyncSession, actor: User, item_id: uuid.UUID
) -> tuple[SubcontractorContractItem, SubcontractorContract, Project]:
    """Kalem -> sözleşme -> proje. Dolaylı kimlikle erişim de görünürlük

    süzgecinden geçmek ZORUNDA (`_visible_contract`/`service._visible_item`
    deseninin aynısı). Görünmeyen projedeki gerçek kalem ile var olmayan kalem
    AYNI 404 gövdesini döner (spec §6, IDOR).
    """
    item = await repository.get_subcontract_item(session, item_id)
    if item is None:
        raise NotFoundError(guards.ITEM_MISSING)
    contract = await repository.get_subcontractor_contract(session, item.contract_id)
    if contract is None:
        raise NotFoundError(guards.ITEM_MISSING)
    project = await _visible_project(session, actor, contract.project_id, guards.ITEM_MISSING)
    return item, contract, project


async def _ensure_item_code_unique(
    session: AsyncSession,
    contract_id: uuid.UUID,
    code: str,
    *,
    exclude_item_id: uuid.UUID | None = None,
) -> None:
    """409: `(contract_id, code)` çakışması (spec §6.5). Aynı Türkçe metin

    `guards.DUPLICATE_ITEM_CODE` (task C6) tek kopya yeniden kullanılır —
    her iki bağlamda da anlamı aynıdır: "bu poz numarası bu sözleşmede zaten
    kullanılıyor".
    """
    existing = await repository.get_subcontract_item_by_code(
        session, contract_id, code, exclude_item_id=exclude_item_id
    )
    if existing is not None:
        raise DuplicateError(guards.DUPLICATE_ITEM_CODE)


async def _ensure_nested_items_valid(
    session: AsyncSession,
    project_id: uuid.UUID,
    items: list[SubcontractorContractItemCreate],
) -> dict[uuid.UUID, str | None]:
    """İÇ İÇE kalem yazma yolu için "önce doğrula sonra yaz" bloğu — dal geneli

    son inceleme (CRITICAL + IMPORTANT bulguları): `create_subcontract_item`
    (tekil `POST .../items` ucu) hem `_ensure_source_item_in_project` hem
    `_ensure_item_code_unique` çağırıyordu, bu iç içe yol (C10) hiçbirini
    çağırmıyordu. `source_contract_item_id` doğrulanmadan yazılırsa başka
    projenin işveren kalemine bağlanabilir ve yanıt `_item_groups` üzerinden o
    projenin grup adını sızdırır (IDOR). Kod çakışması da DB'ye hiç gitmeden
    (henüz kayıt yokken, yalnız gövde İÇİ tekrar) yakalanır — sözleşme YENİ
    oluşturulduğu için mevcut kalemlerle çakışma yoktur, yalnız gövde
    içi tekrar mümkündür.
    """
    # KAT-B2.3 (K4): doğrulamada zaten okunan kaynak kalemlerin Bakanlık no'su döner (ek sorgu yok).
    source_codes: dict[uuid.UUID, str | None] = {}
    seen_codes: set[str] = set()
    for item in items:
        source_item = await _ensure_source_item_in_project(
            session, item.source_contract_item_id, project_id
        )
        if source_item is not None:
            source_codes[source_item.id] = source_item.source_code
        if item.code in seen_codes:
            raise DuplicateError(guards.DUPLICATE_ITEM_CODE)
        seen_codes.add(item.code)
    return source_codes


async def _ensure_source_item_in_project(
    session: AsyncSession, source_contract_item_id: uuid.UUID | None, project_id: uuid.UUID
) -> EmployerContractItem | None:
    """C11 incelemesinden devredilen ek iş: gövdedeki `source_contract_item_id`

    doğrulanmadan yazılıyordu — başka bir projenin işveren kalemine bağlanabilir
    ve yanıtta o projenin grup adı sızardı (`to_subcontract_item_response` ->
    `_item_groups`). 404 `ITEM_MISSING` seçildi, 422 DEĞİL: `_visible_item`/
    `_visible_group`'un IDOR deseninin aynısı — görünmeyen/başka projenin
    kaydı ile var olmayan kayıt AYNI gövdeyi döner, aksi hâlde elinde UUID'si
    olan biri kaydın var olduğunu ve başka bir projeye ait olduğunu ayırt
    edebilirdi.
    """
    if source_contract_item_id is None:
        return None
    source_item = await repository.get_employer_item(session, source_contract_item_id)
    if source_item is None or source_item.project_id != project_id:
        raise NotFoundError(guards.ITEM_MISSING)
    return source_item


def _ensure_price_allowed(contract: SubcontractorContract, unit_price: object) -> None:
    """`guards.ITEM_PRICES_REQUIRED` YAYIN SONRASI da korunur (borç #141).

    Kural `guards.validate_subcontract`te YALNIZ taslak→yayın geçişinde koşar;
    kalem uçları o doğrulamadan geçmediği için yayındaki (is_draft=False) bir
    sözleşmeye sonradan fiyatsız satır girilebiliyordu. O satır
    `SubcontractorContractItemResponse.line_total`da 0 sayılır (bedel eksik
    hesaplanır) ve `subcontractor_progress_payments/service.py` hakediş
    üretimini 422 ile tıkar. Taslakta fiyatsız kalem YASAL kalır (spec §3.6:
    "girilmedi" ile "0 TL" ayrımı) — kolon nullable KALIR, migration YOK.

    İş kuralı serviste durur, şemada DEĞİL: zorunluluk kalemin kendi biçimine
    değil, BAĞLI OLDUĞU sözleşmenin taslak olup olmadığına bakar.
    """
    if not contract.is_draft and unit_price is None:
        raise SiteValidationError(guards.ITEM_PRICES_REQUIRED)


async def create_subcontract_item(
    session: AsyncSession,
    actor: User,
    contract_id: uuid.UUID,
    data: SubcontractorContractItemCreate,
) -> tuple[SubcontractorContractItem, SubcontractorContract, Project]:
    contract, project = await _visible_contract(session, actor, contract_id)
    source_item = await _ensure_source_item_in_project(
        session, data.source_contract_item_id, project.id
    )
    await _ensure_item_code_unique(session, contract.id, data.code)
    # SIRA BİLİNÇLİ — fiyat kapısı EN SONDA. Önüne alınsaydı
    # `_ensure_source_item_in_project`in IDOR 404'ünü 422 ile MASKELERDİ:
    # `test_delete.py::test_source_item_baska_projeden_baglanamaz` (yayındaki
    # sözleşme + fiyatsız gövde) bunu ölçtü ve kırmızıya döndü. Mevcut
    # kapıların statü önceliği DEĞİŞMEZ.
    _ensure_price_allowed(contract, data.unit_price)
    item = SubcontractorContractItem(
        contract_id=contract.id,
        source_contract_item_id=data.source_contract_item_id,
        source_code=source_item.source_code if source_item is not None else None,
        code=data.code,
        description=data.description,
        unit=data.unit,
        quantity=data.quantity,
        unit_price=data.unit_price,
        # Tekil uçta gövde içi sıra türetimi YOK (`index` kavramı yok) — istemci
        # göndermezse model kolonunun kendi varsayılanıyla (0) aynı davranış.
        sort_order=data.sort_order if data.sort_order is not None else 0,
    )
    session.add(item)
    await session.flush()
    await session.refresh(item)
    return item, contract, project


async def update_subcontract_item(
    session: AsyncSession, actor: User, item_id: uuid.UUID, data: SubcontractorContractItemUpdate
) -> tuple[SubcontractorContractItem, SubcontractorContract, Project]:
    """PATCH kısmi günceleme (spec §6.5) — `items` gövdesinde `source_contract_item_id`

    YOK, bağ yalnız `load-from-employer` ile kurulur ve PATCH'te değiştirilemez.
    """
    item, contract, project = await _visible_subcontract_item(session, actor, item_id)
    updates = data.model_dump(exclude_unset=True)
    if "code" in updates and updates["code"] != item.code:
        await _ensure_item_code_unique(
            session, contract.id, updates["code"], exclude_item_id=item.id
        )
    # `create_subcontract_item` ile AYNI sıra gerekçesi: fiyat kapısı mevcut
    # 404/409 kapılarının ARKASINDA durur. `exclude_unset` şart — gövdede
    # `unit_price` HİÇ yoksa kural koşmaz (yayındaki sözleşmenin zaten fiyatsız
    # eski satırı `description` düzeltilerek güncellenebilir kalmalı).
    if "unit_price" in updates:
        _ensure_price_allowed(contract, updates["unit_price"])
    for field, value in updates.items():
        setattr(item, field, value)
    await session.flush()
    await session.refresh(item)
    return item, contract, project


async def to_subcontract_item_response(
    session: AsyncSession, item: SubcontractorContractItem
) -> SubcontractorContractItemResponse:
    """Tek kalem yanıtı — grup `_item_groups`'un aynı türetme mantığıyla çözülür

    (spec §3.6, POST/PATCH kalem uçlarının ortak yanıt kurucusu).
    """
    groups = await _item_groups(session, [item])
    return SubcontractorContractItemResponse(
        id=item.id,
        contract_id=item.contract_id,
        source_contract_item_id=item.source_contract_item_id,
        source_code=item.source_code,
        code=item.code,
        description=item.description,
        unit=item.unit,
        quantity=item.quantity,
        unit_price=item.unit_price,
        sort_order=item.sort_order,
        group=groups.get(item.source_contract_item_id) if item.source_contract_item_id else None,
    )


async def load_items_from_employer(
    session: AsyncSession, actor: User, contract_id: uuid.UUID
) -> tuple[int, int, SubcontractorContract, Project]:
    """`FORM` 115 / `TSD` 91 — işveren sözleşmesi kalemlerini kopyalar (spec §6.5).

    `code`/`description`/`unit`/`quantity` + `source_code` (K4 snapshot; yalnız YENİ kalemlere —
    K14: mevcut kalemin `source_code`u doldurulmaz/değiştirilmez) kopyalanır, `unit_price` BİLİNÇLİ
    olarak NULL bırakılır (taşeron fiyatını kullanıcı girer). Idempotent: aynı
    `code` sözleşmede zaten varsa atlanır, üzerine YAZILMAZ. Atomik: tek
    `flush` ile — bir kalem yazılamazsa (örn. beklenmeyen bütünlük hatası)
    hiçbiri kalıcı olmaz, ayrı ayrı commit edilmez.
    """
    contract, project = await _visible_contract(session, actor, contract_id)
    groups = await repository.list_employer_groups(session, contract.project_id)
    employer_items = [item for group in groups for item in group.items]
    if not employer_items:
        raise SiteValidationError(guards.NO_EMPLOYER_ITEMS)

    existing_codes = {item.code for item in contract.items}
    # Bu uç fiyatı BİLİNÇLİ NULL yazar; yayındaki sözleşmede bu doğrudan
    # `ITEM_PRICES_REQUIRED` ihlalidir (borç #141). Reddediliş YENİ satır
    # yazılacaksa koşar: hepsi zaten varsa çağrı idempotent no-op'tur ve
    # `test_ikinci_yukleme_idempotent`in sözleşmesi korunur.
    if not contract.is_draft and any(k.code not in existing_codes for k in employer_items):
        raise SiteValidationError(guards.ITEM_PRICES_REQUIRED)
    created_count = 0
    skipped_count = 0
    for index, employer_item in enumerate(employer_items):
        if employer_item.code in existing_codes:
            skipped_count += 1
            continue
        session.add(
            SubcontractorContractItem(
                contract_id=contract.id,
                source_contract_item_id=employer_item.id,
                source_code=employer_item.source_code,
                code=employer_item.code,
                description=employer_item.description,
                unit=employer_item.unit,
                quantity=employer_item.quantity,
                unit_price=None,
                sort_order=index,
            )
        )
        existing_codes.add(employer_item.code)
        created_count += 1
    await session.flush()
    return created_count, skipped_count, contract, project


def _merged_for_validation(contract: SubcontractorContract, changes: dict) -> SimpleNamespace:
    """Mevcut satır + patch = doğrulamanın gördüğü kayıt (`sites/service.py.

    _merged_for_validation` deseninin aynısı). `items` MEVCUT satırdan gelir:
    yayına geçişte "girilmiş kalemlerin hepsinde birim fiyat" kuralı zaten
    kaydedilmiş kalemler üzerinden koşar — PATCH gövdesi kalem TAŞIMAZ.
    """
    merged = {field: changes.get(field, getattr(contract, field)) for field in _VALIDATED_FIELDS}
    return SimpleNamespace(project_id=contract.project_id, items=contract.items, **merged)


async def update_subcontractor_contract(
    session: AsyncSession, actor: User, contract_id: uuid.UUID, data: SubcontractorContractUpdate
) -> tuple[SubcontractorContract, Project, bool, bool]:
    """PATCH GEVŞEK, YAYIN SIKI (`sites.update_site` deseninin aynısı, spec §4).

    Genel dalda zorunluluk kuralları KOŞMAZ — koşsaydı canlıdaki eksik kayıtlı
    taslaklar düzenlenemez hale gelirdi. Tek istisna `is_draft: true -> false`
    geçişidir: orada BİRLEŞİK kayıt üzerinde tüm kurallar koşar ve geçmezse
    satır TASLAK KALIR. Tutarlılık kuralları (şantiye-proje eşleşmesi dahil)
    HER İKİ dalda da koşar.

    `is_publishing` da DÖNER (`units.update_unit`/`sites.update_site` deseni):
    yayına geçiş olup olmadığı yalnız BURADA bilinir — `is_draft`in ÖNCEKİ
    değeri router'da görünmez, dolayısıyla ayrımı dışarı taşımak denetim
    günlüğünde "güncellendi" ile "yayına alındı" satırlarını karıştırırdı.

    `site_changed` de AYNI gerekçeyle döner (TB4 T6, karar S9/2): şantiyenin
    ÖNCEKİ değeri yalnız burada bilinir. Bu modül hakediş paketini TANIMAZ —
    tazelemeyi tetikleme işi kompozisyon katmanındaki router'ındır, bağımlılık
    yönü tek yönlü (`subcontractor_progress_payments → contracts`) KALIR.
    `site_id` gövdede olsa bile DEĞER aynıysa bayrak `False`tur: gereksiz
    tazeleme köprüyü boşa sorgulardı.
    """
    contract, project = await _visible_contract(session, actor, contract_id)
    changes = data.model_dump(exclude_unset=True)
    site_changed = "site_id" in changes and changes["site_id"] != contract.site_id

    # Tutarlılık: HER ZAMAN — değişen `site_id` (veya mevcut değeri) yeniden
    # kontrol edilir.
    merged_site_id = changes.get("site_id", contract.site_id)
    await _ensure_site_in_project(session, merged_site_id, contract.project_id)

    is_publishing = contract.is_draft and changes.get("is_draft") is False
    # 🔴 no=15 açık bacak: BİRLEŞİK `is_draft` (`changes`te yoksa mevcut satırın
    # değeri) kullanılmalı — `not is_publishing` yalnız taslak→yayın GEÇİŞİNİ
    # ölçer, zaten yayında olan bir kayda dokunan bir PATCH'te (is_draft gövdede
    # yokken) hep `True` (taslak) döner ve zorunluluk kuralları atlanırdı.
    merged_is_draft = changes.get("is_draft", contract.is_draft)
    guards.validate_subcontract(_merged_for_validation(contract, changes), is_draft=merged_is_draft)

    if "contract_no" in changes and changes["contract_no"] != contract.contract_no:
        await _ensure_contract_no_unique(session, changes["contract_no"], exclude_id=contract.id)

    # Anlık görüntü: `subcontractor_id` değiştiyse ad kartotekten YENİDEN
    # kopyalanır (spec §3.5) — istemciden gelen ad YOKTUR, olamaz da (şema).
    if "subcontractor_id" in changes:
        changes["subcontractor_name"] = await _resolve_subcontractor_name(
            session, changes["subcontractor_id"]
        )

    for field, value in changes.items():
        setattr(contract, field, value)
    # 🔴 URL-4 K1 — SLUG'IN GEÇ DOĞUMU (kira faturasındakiyle AYNI sınıf).
    # Taşeron sözleşmesi taslakta numarasız/adsız açılabilir; slug yalnız
    # `create`te ayrılsaydı o kayıt okunabilir URL'ini HİÇ almazdı ve özellik
    # "verinin yaşının fonksiyonu" olurdu.
    if contract.slug is None:
        contract.slug = await allocate_slug(
            session, _contract_slug_base(contract), SubcontractorContract.slug
        )
    await session.flush()
    await session.refresh(contract)
    return contract, project, is_publishing, site_changed


async def _item_groups(
    session: AsyncSession, items: list[SubcontractorContractItem]
) -> dict[uuid.UUID, SubcontractorContractItemGroup]:
    """Bağlı kalemlerin grup başlıkları — `source_contract_item_id` üzerinden

    TÜRER (spec §3.6, ayrı grup tablosu yok).
    """
    source_ids = [item.source_contract_item_id for item in items if item.source_contract_item_id]
    if not source_ids:
        return {}
    rows = await repository.get_employer_item_groups(session, source_ids)
    return {
        item_id: SubcontractorContractItemGroup(id=group_id, name=group_name)
        for item_id, group_id, group_name in rows
    }


async def to_subcontract_detail(
    session: AsyncSession, contract: SubcontractorContract
) -> SubcontractorContractDetail:
    """`TSD` başlığı + kalemler + türev toplam (spec §6.5)."""
    groups = await _item_groups(session, contract.items)
    items_missing_price = sum(1 for item in contract.items if item.unit_price is None)
    item_responses = [
        SubcontractorContractItemResponse(
            id=item.id,
            contract_id=item.contract_id,
            source_contract_item_id=item.source_contract_item_id,
            source_code=item.source_code,
            code=item.code,
            description=item.description,
            unit=item.unit,
            quantity=item.quantity,
            unit_price=item.unit_price,
            sort_order=item.sort_order,
            group=groups.get(item.source_contract_item_id)
            if item.source_contract_item_id
            else None,
        )
        for item in contract.items
    ]
    return SubcontractorContractDetail(
        id=contract.id,
        slug=contract.slug,
        project_id=contract.project_id,
        site_id=contract.site_id,
        subcontractor_id=contract.subcontractor_id,
        subcontractor_name=contract.subcontractor_name,
        work_category=contract.work_category,
        contract_no=contract.contract_no,
        signature_date=contract.signature_date,
        is_notarized=contract.is_notarized,
        start_date=contract.start_date,
        end_date=contract.end_date,
        late_penalty_daily=contract.late_penalty_daily,
        advance_pct=contract.advance_pct,
        retainage_pct=contract.retainage_pct,
        vat_pct=contract.vat_pct,
        payment_period=contract.payment_period,
        payment_term_days=contract.payment_term_days,
        materials_by_contractor=contract.materials_by_contractor,
        subcontractor_files_own_sgk=contract.subcontractor_files_own_sgk,
        vat_withholding=contract.vat_withholding,
        status=contract.status,
        is_draft=contract.is_draft,
        items=item_responses,
        contract_total=_subcontractor_amount(contract),
        items_missing_price=items_missing_price,
    )


# --- Silme uçları (task C12, spec §7) ---


async def delete_subcontractor_contract(
    session: AsyncSession, actor: User, contract_id: uuid.UUID
) -> tuple[str, str | None, str | None]:
    """`can_delete` (`app/core/access.py`) taslak istisnasının GEÇERLİ olduğu

    TEK uç (spec §5.0, §7). Kapı router'da `_ADMIN` DEĞİL `_FULL`'dir — kararı
    burada `can_delete` verir: `admin` her şeyi siler, aksi hâlde yalnız
    kaydı AÇAN aktör + kayıt hâlâ TASLAK + aktörün en az `draft` seviyesi
    varsa silinebilir. `boq`/`sites` DELETE uçlarının hiçbiri `can_delete`
    KULLANMAZ (saf `_ADMIN` kapısı yeterlidir) — bu uçtaki taslak istisnası
    P5'e özgüdür (spec §5.0, C1'in `created_by`/`is_draft` alanlarını bu yüzden
    eklediği yer). Aktörün gerçek erişim seviyesi `projects/service.py.
    visible_projects`in `get_permission` çağrısı deseninin aynısıyla okunur —
    router bağımlılığı yalnız YETKİ TABANI verir, kesin karar burada.
    """
    contract, project = await _visible_contract(session, actor, contract_id)
    permission = await get_permission(session, actor.role_id, "contracts")
    level = permission.access_level if permission is not None else AccessLevel.none
    if not can_delete(actor.id, level, contract):
        raise DeleteNotAllowedError(guards.DELETE_NOT_ALLOWED)
    project_name = project.name
    contract_no, subcontractor_name = contract.contract_no, contract.subcontractor_name
    await session.delete(contract)
    await session.flush()
    return project_name, contract_no, subcontractor_name


async def delete_subcontract_item(
    session: AsyncSession, actor: User, item_id: uuid.UUID
) -> tuple[str | None, str]:
    """Engel YOK, kapı `_ADMIN` (spec §7 tablosunda AYRICA listelenmez —

    kalem silme `subcontractor_contract_items` satırını hedefler, sözleşmenin
    kendisini DEĞİL).
    """
    item, contract, _ = await _visible_subcontract_item(session, actor, item_id)
    contract_no, code = contract.contract_no, item.code
    await session.delete(item)
    await session.flush()
    return contract_no, code

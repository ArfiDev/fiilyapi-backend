"""Birleşik sözleşme listesi için okuma sorguları (spec §6.1, task C5).

`boq/repository.py` deseninin aynısı: filtreler SQL düzeyinde uygulanır, N+1
üretilmez. Taşeron tarafında `SubcontractorContract.items` ilişkisi
`lazy="selectin"` tanımlıdır (models.py) — erişildiğinde tüm sözleşmelerin
kalemleri TEK ek sorguda (IN listesi) toplu gelir.
"""

import uuid
from datetime import date
from typing import NamedTuple

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.contracts.models import (
    ContractStatus,
    EmployerContractGroup,
    EmployerContractItem,
    Subcontractor,
    SubcontractorContract,
    SubcontractorContractItem,
)
from app.modules.projects.models import Project, ProjectContract
from app.modules.site_diary.models import SiteDiaryEntry, SiteDiaryWorkerCount
from app.modules.sites.models import Site


async def list_employer_contracts(
    session: AsyncSession,
    visible_project_ids: list[uuid.UUID],
    *,
    project_id: uuid.UUID | None,
    status_filter: ContractStatus | None,
    q: str | None,
) -> list[tuple[Project, ProjectContract]]:
    """İşveren "sözleşme kaydı" = `project_contracts` satırı olan proje (spec §6.1).

    INNER JOIN: sözleşmesi olmayan proje listede ÇIKMAZ.
    """
    if not visible_project_ids:
        return []
    stmt = (
        select(Project, ProjectContract)
        .join(ProjectContract, ProjectContract.project_id == Project.id)
        .where(Project.id.in_(visible_project_ids))
        .order_by(Project.code)
    )
    if project_id is not None:
        stmt = stmt.where(Project.id == project_id)
    if status_filter is not None:
        stmt = stmt.where(ProjectContract.status == status_filter)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                ProjectContract.contract_no.ilike(pattern),
                Project.employer_name.ilike(pattern),
            )
        )
    result = await session.execute(stmt)
    return [(row[0], row[1]) for row in result.all()]


async def list_subcontractor_contracts(
    session: AsyncSession,
    visible_project_ids: list[uuid.UUID],
    *,
    project_id: uuid.UUID | None,
    status_filter: ContractStatus | None,
    q: str | None,
) -> list[SubcontractorContract]:
    if not visible_project_ids:
        return []
    stmt = (
        select(SubcontractorContract)
        .where(SubcontractorContract.project_id.in_(visible_project_ids))
        .order_by(SubcontractorContract.created_at)
    )
    if project_id is not None:
        stmt = stmt.where(SubcontractorContract.project_id == project_id)
    if status_filter is not None:
        stmt = stmt.where(SubcontractorContract.status == status_filter)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                SubcontractorContract.contract_no.ilike(pattern),
                SubcontractorContract.subcontractor_name.ilike(pattern),
            )
        )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _subcontract_list_stmt(
    visible_project_ids: list[uuid.UUID],
    *,
    project_id: uuid.UUID | None,
    site_id: uuid.UUID | None,
    status_filter: ContractStatus | None,
    q: str | None,
):
    """TB2 U1 seçim listesinin ORTAK `WHERE` gövdesi.

    Satır sorgusu ile `total` sayımı bu tek yerden beslenir
    (`subcontractor_progress_payments.repository._list_stmt` deseninin aynısı):
    süzgeç kopyası açılırsa sayı ile tablo AYRIŞIR.
    """
    stmt = (
        select(SubcontractorContract, Project.name, Site.name)
        .join(Project, Project.id == SubcontractorContract.project_id)
        .outerjoin(Site, Site.id == SubcontractorContract.site_id)
        .where(SubcontractorContract.project_id.in_(visible_project_ids))
    )
    if project_id is not None:
        stmt = stmt.where(SubcontractorContract.project_id == project_id)
    if site_id is not None:
        # `site_id IS NULL` (proje geneli) sözleşme şantiye filtresiyle GELMEZ:
        # eşitlik NULL'ı zaten eler (SD S5 tek-anlamlılık kararıyla tutarlı).
        stmt = stmt.where(SubcontractorContract.site_id == site_id)
    if status_filter is not None:
        stmt = stmt.where(SubcontractorContract.status == status_filter)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                SubcontractorContract.contract_no.ilike(pattern),
                SubcontractorContract.subcontractor_name.ilike(pattern),
            )
        )
    return stmt


async def list_subcontractor_contract_rows(
    session: AsyncSession,
    visible_project_ids: list[uuid.UUID],
    *,
    project_id: uuid.UUID | None,
    site_id: uuid.UUID | None,
    status_filter: ContractStatus | None,
    q: str | None,
    limit: int,
    offset: int,
) -> list[tuple[SubcontractorContract, str, str | None]]:
    """TB2 U1 seçim listesi: sözleşme + proje adı + şantiye adı TEK sorguda.

    `list_subcontractor_contracts` (birleşik `/contracts` ucu) yerine ayrı bir
    sorgu: o uç bedel türetmek için kalemleri çeker ve ad JOIN'i taşımaz. Adlar
    burada JOIN'den gelir — satır başına ek sorgu (N+1) YOKTUR. Şantiye bağı
    NULL olabildiği için (K4 "proje geneli") `sites` OUTER JOIN'dir.
    """
    if not visible_project_ids:
        return []
    # Deterministik sıra sayfalamanın ÖNKOŞULU: `contract_no` NULL olabilir
    # (taslak), `id` eşitliği bozar — yoksa sayfalar arası kayıt kaçar/tekrarlar.
    stmt = _subcontract_list_stmt(
        visible_project_ids,
        project_id=project_id,
        site_id=site_id,
        status_filter=status_filter,
        q=q,
    ).order_by(SubcontractorContract.contract_no, SubcontractorContract.id)
    result = await session.execute(stmt.limit(limit).offset(offset))
    return [(row[0], row[1], row[2]) for row in result.all()]


async def count_subcontractor_contract_rows(
    session: AsyncSession,
    visible_project_ids: list[uuid.UUID],
    *,
    project_id: uuid.UUID | None,
    site_id: uuid.UUID | None,
    status_filter: ContractStatus | None,
    q: str | None,
) -> int:
    """TB3 T2 `total`: `limit`/`offset`ten BAĞIMSIZ, filtrelenmiş küme sayısı.

    Görünürlük süzgeci (`visible_project_ids`) sayımın da İÇİNDE — kullanıcı
    göremediği kaydı sayı olarak da görmez (IDOR).
    """
    if not visible_project_ids:
        return 0
    inner = _subcontract_list_stmt(
        visible_project_ids,
        project_id=project_id,
        site_id=site_id,
        status_filter=status_filter,
        q=q,
    ).with_only_columns(SubcontractorContract.id)
    stmt = select(func.count()).select_from(inner.subquery())
    return int((await session.execute(stmt)).scalar_one())


# --- İşveren sözleşmesi poz grup/kalem (task C6, `boq/repository.py` deseninin
# aynısı — spec §3.2, §6.2) ---


async def list_employer_groups(
    session: AsyncSession, project_id: uuid.UUID
) -> list[EmployerContractGroup]:
    """Bir sözleşmenin poz grupları, sıralı. Kalemler ayrı sorgu ATILMAZ:

    `EmployerContractGroup.items` ilişkisi `lazy="selectin"` tanımlıdır (C1),
    erişildiğinde tüm grupların kalemleri TEK ek sorguda toplu gelir.
    """
    result = await session.execute(
        select(EmployerContractGroup)
        .where(EmployerContractGroup.project_id == project_id)
        .order_by(EmployerContractGroup.sort_order, EmployerContractGroup.created_at)
    )
    return list(result.scalars().all())


async def lock_employer_items(session: AsyncSession, project_id: uuid.UUID) -> None:
    """Bir sözleşmenin TÜM poz kalemlerini `SELECT … FOR UPDATE` ile kilitler.

    `progress_payments/repository.py::get_contract_locked` deseninin aynısı,
    yalnız kilit kümesi çok satırlı: dağıtımın doğruladığı kota (`Σ şantiye
    kotası ≤ contract_item.quantity`) kalem satırının kendisine yazılıdır, bu
    yüzden kilitlenmesi gereken şey o kalemlerdir. Dağıtım yazımından ÖNCE,
    doğrulamayı besleyen okumalardan da ÖNCE çağrılmak ZORUNDA — aksi hâlde iki
    eşzamanlı istek aynı "kalan"ı okur, ikisi de geçerli sanılır, ikisi de yazar
    ve toplam sözleşme miktarını aşar (TOCTOU, spec §2).

    `ORDER BY id` OPSİYONEL DEĞİLDİR: kilit kümesi çok satırlı olduğu için sıra
    tutarsız olursa iki eşzamanlı istek karşılıklı kilitlenir (A kalem-1'i,
    B kalem-2'yi tutarken ikisi de diğerini bekler → deadlock). Küresel ve
    deterministik bir sıra (birincil anahtar) her iki isteğin de AYNI satırda
    buluşmasını, dolayısıyla birinin BEKLEMESİNİ garanti eder.
    """
    stmt = (
        select(EmployerContractItem.id)
        .where(EmployerContractItem.project_id == project_id)
        .order_by(EmployerContractItem.id)
        .with_for_update()
    )
    await session.execute(stmt)


async def get_employer_group(
    session: AsyncSession, group_id: uuid.UUID
) -> EmployerContractGroup | None:
    return await session.get(EmployerContractGroup, group_id)


async def get_employer_item(
    session: AsyncSession, item_id: uuid.UUID
) -> EmployerContractItem | None:
    return await session.get(EmployerContractItem, item_id)


async def list_employer_item_codes(
    session: AsyncSession, project_id: uuid.UUID, codes: list[str]
) -> set[str]:
    """TKL-B3.1: verilen kodlardan sözleşmede ZATEN kullanılanlar (toplu ekleme ön kontrolü)."""
    result = await session.execute(
        select(EmployerContractItem.code).where(
            EmployerContractItem.project_id == project_id, EmployerContractItem.code.in_(codes)
        )
    )
    return set(result.scalars())


async def get_employer_item_by_code(
    session: AsyncSession,
    project_id: uuid.UUID,
    code: str,
    exclude_item_id: uuid.UUID | None = None,
) -> EmployerContractItem | None:
    """`(project_id, code)` çakışmasını `IntegrityError`'a düşmeden ÖNCE yakalar

    (`DuplicateError` deseni, `boq/repository.py.get_item_by_code` emsali).
    """
    stmt = select(EmployerContractItem).where(
        EmployerContractItem.project_id == project_id, EmployerContractItem.code == code
    )
    if exclude_item_id is not None:
        stmt = stmt.where(EmployerContractItem.id != exclude_item_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_boq_items_for_sites(
    session: AsyncSession, site_ids: list[uuid.UUID]
) -> list[BoqItem]:
    """Verilen şantiyelerin TÜM BOQ satırları TEK sorguda (task C8).

    `contract_item_id IS NULL` satırlar da gelir — dağıtım yazarken
    `uq_boq_items_site_code` çakışması bu satırlardan da doğabilir
    (şantiyenin kendi başına girdiği poz aynı numarayı tutuyor olabilir).

    Dağıtımın "kalan" hesabının TEK kaynağı da bu sorgudur
    (`contracts.distribution_quantity`); sıralama `id`'ye göre DETERMİNİSTİK
    olmak zorundadır — aynı (kalem, şantiye) hücresine düşen birden çok satır
    varsa hangisinin hücreyi temsil ettiği sıralamaya bağlıdır.
    """
    if not site_ids:
        return []
    stmt = select(BoqItem).where(BoqItem.site_id.in_(site_ids)).order_by(BoqItem.id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_boq_groups_for_sites(
    session: AsyncSession, site_ids: list[uuid.UUID]
) -> list[BoqGroup]:
    """Verilen şantiyelerin BOQ grupları TEK sorguda (task C8 grup önbelleği).

    Sıralama DETERMİNİSTİK olmak ZORUNDA: `BoqGroup`'ta `(site_id, name)`
    benzersizliği yok, aynı adlı iki grup varsa önbelleğin hangisini seçtiği
    sıralamaya bağlıdır. `created_at, id` → her zaman EN ESKİ grup.
    """
    if not site_ids:
        return []
    stmt = (
        select(BoqGroup)
        .where(BoqGroup.site_id.in_(site_ids))
        .order_by(BoqGroup.created_at, BoqGroup.id)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# --- Taşeron kartoteksi (task C9, spec §3.4/§6.4) ---
#
# `projects/repository.py`'deki `list_employers`/`get_employer_by_tax_number`/
# `add_employer` desenlerinin birebiri.


async def list_subcontractors(
    session: AsyncSession, q: str | None, active_only: bool
) -> list[Subcontractor]:
    """Ada göre ILIKE süzgeci + aktiflik; sıralama DB'de (ORDER BY name)."""
    stmt = select(Subcontractor)
    if active_only:
        stmt = stmt.where(Subcontractor.is_active.is_(True))
    if q:
        stmt = stmt.where(Subcontractor.name.ilike(f"%{q}%"))
    stmt = stmt.order_by(Subcontractor.name)
    return list((await session.execute(stmt)).scalars().all())


async def get_subcontractor(
    session: AsyncSession, subcontractor_id: uuid.UUID
) -> Subcontractor | None:
    return await session.get(Subcontractor, subcontractor_id)


async def get_subcontractor_by_tax_number(
    session: AsyncSession, tax_number: str, exclude_id: uuid.UUID | None = None
) -> Subcontractor | None:
    stmt = select(Subcontractor).where(Subcontractor.tax_number == tax_number)
    if exclude_id is not None:
        stmt = stmt.where(Subcontractor.id != exclude_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def add_subcontractor(session: AsyncSession, subcontractor: Subcontractor) -> Subcontractor:
    session.add(subcontractor)
    await session.flush()
    await session.refresh(subcontractor)
    return subcontractor


# --- Silme korkulukları (task C12, spec §7) ---
#
# `sites/repository.py.site_has_sections` deseninin aynısı:
# `select(<altsorgu>.exists())` — SATIR ÇEKMEZ, `count(*)` KULLANILMAZ (hata
# metinlerinde adet verilmez, spec §7 tablosu).


async def subcontractor_has_contracts(session: AsyncSession, subcontractor_id: uuid.UUID) -> bool:
    """Taşerona bağlı sözleşme var mı (`subcontractor_contracts.subcontractor_id`

    -> RESTRICT). DB kendiliğinden korur ama korkuluksuz bırakılırsa kullanıcı
    "Veri bütünlüğü hatası" görür (`sites/repository.py.site_has_contracts`
    dersinin aynısı).
    """
    result = await session.execute(
        select(
            select(SubcontractorContract.id)
            .where(SubcontractorContract.subcontractor_id == subcontractor_id)
            .exists()
        )
    )
    return bool(result.scalar_one())


async def employer_group_has_items(session: AsyncSession, group_id: uuid.UUID) -> bool:
    """Grupta poz kalemi var mı (`employer_contract_items.group_id` -> CASCADE).

    `sites/repository.py.site_has_boq` gerekçesinin aynısı: DB CASCADE ile
    korur ama korkuluksuz bırakılırsa kalemler sessizce yok olur.
    """
    result = await session.execute(
        select(
            select(EmployerContractItem.id)
            .where(EmployerContractItem.group_id == group_id)
            .exists()
        )
    )
    return bool(result.scalar_one())


# --- Taşeron sözleşmesi (task C10, spec §3.5/§6.5) ---


async def get_subcontractor_contract(
    session: AsyncSession, contract_ref: uuid.UUID | str
) -> SubcontractorContract | None:
    """URL-4: kimlik YA DA slug ile okur. Slug GLOBAL tekildir
    (`uq_subcontractor_contracts_slug`).

    UUID yolunda `session.get` KALIR: kimlik haritasını (identity map) kullanır
    ve aynı istekte ikinci kez okunduğunda sorgu ATMAZ. Slug yolu bu kısayolu
    kullanamaz (anahtar PK değildir) ve `select`e düşer — davranış aynıdır,
    yalnız bir sorgu daha koşar.
    """
    if isinstance(contract_ref, uuid.UUID):
        return await session.get(SubcontractorContract, contract_ref)
    return await session.scalar(
        select(SubcontractorContract).where(SubcontractorContract.slug == contract_ref)
    )


async def get_subcontractor_contract_by_contract_no(
    session: AsyncSession,
    contract_no: str,
    exclude_id: uuid.UUID | None = None,
) -> SubcontractorContract | None:
    """`contract_no` çakışmasını `IntegrityError`'a düşmeden ÖNCE yakalar

    (`get_employer_item_by_code` deseninin aynısı) — global kısmi benzersiz
    indeks (spec §3.5), NULL değerler serbestçe çoğalabilir.
    """
    stmt = select(SubcontractorContract).where(SubcontractorContract.contract_no == contract_no)
    if exclude_id is not None:
        stmt = stmt.where(SubcontractorContract.id != exclude_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# --- Taşeron sözleşmesi kalemleri (task C11, spec §3.6/§6.5) ---


async def get_subcontract_item(
    session: AsyncSession, item_id: uuid.UUID
) -> SubcontractorContractItem | None:
    return await session.get(SubcontractorContractItem, item_id)


async def get_subcontract_item_by_code(
    session: AsyncSession,
    contract_id: uuid.UUID,
    code: str,
    exclude_item_id: uuid.UUID | None = None,
) -> SubcontractorContractItem | None:
    """`(contract_id, code)` çakışmasını `IntegrityError`'a düşmeden ÖNCE yakalar

    (`get_employer_item_by_code` deseninin aynısı).
    """
    stmt = select(SubcontractorContractItem).where(
        SubcontractorContractItem.contract_id == contract_id,
        SubcontractorContractItem.code == code,
    )
    if exclude_item_id is not None:
        stmt = stmt.where(SubcontractorContractItem.id != exclude_item_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_employer_item_groups(
    session: AsyncSession, item_ids: list[uuid.UUID]
) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    """`(employer_item_id, group_id, group_name)` üçlüleri — taşeron kalemi

    grubu `source_contract_item_id` üzerinden TÜRER (spec §3.6), ayrı grup
    tablosu yoktur.
    """
    if not item_ids:
        return []
    stmt = (
        select(EmployerContractItem.id, EmployerContractGroup.id, EmployerContractGroup.name)
        .join(EmployerContractGroup, EmployerContractItem.group_id == EmployerContractGroup.id)
        .where(EmployerContractItem.id.in_(item_ids))
    )
    result = await session.execute(stmt)
    return [(row[0], row[1], row[2]) for row in result.all()]


class DiaryUsage(NamedTuple):
    """Silme korkuluğunun metni için günlük kullanım özeti (PLN-B2.11)."""

    entry_count: int
    row_count: int
    first_date: date


async def subcontractor_has_diary_rows(
    session: AsyncSession, subcontractor_id: uuid.UUID
) -> DiaryUsage | None:
    """Taşerona bağlı günlük FİRMA işçi satırı var mı (`site_diary_worker_counts.
    subcontractor_id` -> RESTRICT, PLN-B2.1). Varsa kaç günlükte kaç satır + ilk
    tarih; yoksa `None`. DB zaten korur; korkuluk opak "Veri bütünlüğü hatası"nın
    yerine eyleme dönük metin verir. TEK toplu sorgu.

    Import yönü güvenli: `site_diary/models.py` yalnız `app.core.db`ye bağlıdır.
    """
    stmt = (
        select(
            func.count(func.distinct(SiteDiaryWorkerCount.entry_id)),
            func.count(SiteDiaryWorkerCount.id),
            func.min(SiteDiaryEntry.entry_date),
        )
        .join(SiteDiaryEntry, SiteDiaryEntry.id == SiteDiaryWorkerCount.entry_id)
        .where(SiteDiaryWorkerCount.subcontractor_id == subcontractor_id)
    )
    entry_count, row_count, first_date = (await session.execute(stmt)).one()
    if not row_count:
        return None
    return DiaryUsage(int(entry_count), int(row_count), first_date)

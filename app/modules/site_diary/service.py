"""Şantiye günlüğü CRUD servis katmanı (T2).

İKİ KATMANLI koruma (`subcontractor_progress_payments/service.py` deseninin
birebiri): `site_diary` izni router'da YETKİYİ verir (PM `view` → yazma uçlarında
403), bu modül `projects.service.visible_projects` ile KAPSAMI belirler.
Görünmeyen projedeki GERÇEK kayıt ile var OLMAYAN kimlik AYIRT EDİLEMEZ 404 döner.

KOPYALANMAYAN, ÇAĞRILAN parçalar: `visible_projects` (kapsam), `can_delete`
(silme kuralı), `sites.guards.SITE_MISSING` (404 metni).
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import day_hooks
from app.core.discipline_scope import UNRESTRICTED, DisciplineScope
from app.core.errors import (
    ConflictError,
    DuplicateError,
    NotFoundError,
    SiteValidationError,
)
from app.modules.projects.models import Project
from app.modules.projects.service import visible_projects
from app.modules.site_diary import guards, lines, repository, skeleton
from app.modules.site_diary.models import DiaryStatus, SiteDiaryEntry, SiteDiaryLine
from app.modules.site_diary.schemas import (
    SiteDiaryEntryCreate,
    SiteDiaryEntryUpdate,
    SiteDiaryLinesSave,
)
from app.modules.sites import repository as sites_repository
from app.modules.sites.models import Site
from app.modules.users.models import User

PERMISSION_MODULE = "site_diary"
"""Silme kuralının okuduğu izin modülü — seed'de hazırdır, matris DEĞİŞMEZ."""

_ZERO_QUANTITY = Decimal("0.000")

# Denetim satırındaki insan-okur durum etiketleri. `DiaryStatus` iki değerlidir:
# hakediş evrakının dört durumlu onay makinesi burada YOKTUR (model docstring'i).
_STATUS_LABELS: dict[DiaryStatus, str] = {
    DiaryStatus.draft: "Taslak",
    DiaryStatus.submitted: "Gönderildi",
}


class SiteContext(NamedTuple):
    """Kapsam süzgecinden geçmiş şantiye + projesi."""

    site: Site
    project: Project


class EntryContext(NamedTuple):
    """Kapsam süzgecinden geçmiş üçlü — router'ın denetim satırı da bunu okur."""

    entry: SiteDiaryEntry
    site: Site
    project: Project


class DeletedEntrySummary(NamedTuple):
    """`session.delete` ÖNCESİNDE çıkarılmış özet — kayıt gittiğinde bu dörtlü
    bir daha okunamaz (taşeron `DeletedPaymentSummary` deseninin aynısı)."""

    project_name: str
    site_name: str
    entry_date: date
    status_label: str
    line_count: int


# --- Kapsam ---


async def visible_project(
    session: AsyncSession, actor: User, project_id: uuid.UUID, message: str
) -> Project:
    visible = await visible_projects(session, actor)
    project = next((p for p in visible if p.id == project_id), None)
    if project is None:
        raise NotFoundError(message)
    return project


async def visible_site(session: AsyncSession, actor: User, site_id: uuid.UUID) -> SiteContext:
    """Şantiye → proje. Görünmeyen projenin şantiyesi ile var olmayan şantiye AYNI
    404 gövdesini döner; metin `sites` modülünün TEK cümlesidir (kopya üretilmez)."""
    site = await sites_repository.get_site(session, site_id)
    if site is None:
        raise NotFoundError(guards.SITE_MISSING)
    project = await visible_project(session, actor, site.project_id, guards.SITE_MISSING)
    return SiteContext(site=site, project=project)


async def visible_entry(session: AsyncSession, actor: User, entry_id: uuid.UUID) -> EntryContext:
    """Kapsam süzgeci — `read.py` de bu TEK kapıdan geçer (public olmasının nedeni).

    `entry.project_id` şantiyeden KOPYALANMIŞ bir alandır (model docstring'i);
    kapsam kararı ondan verilir, şantiye JOIN'i yalnız yanıt/denetim metni içindir.
    """
    entry = await repository.get_entry(session, entry_id)
    if entry is None:
        raise NotFoundError(guards.ENTRY_MISSING)
    project = await visible_project(session, actor, entry.project_id, guards.ENTRY_MISSING)
    site = await sites_repository.get_site(session, entry.site_id)
    if site is None:
        # FK CASCADE'i sayesinde ulaşılamaz; yine de sessizce None taşımak yerine
        # kaydın yokluğuyla AYNI 404'e düşülür.
        raise NotFoundError(guards.ENTRY_MISSING)
    return EntryContext(entry=entry, site=site, project=project)


async def visible_entry_locked(
    session: AsyncSession, actor: User, entry_id: uuid.UUID
) -> EntryContext:
    """Kapsam kararı (404) kilitten ÖNCE verilir — görünmeyen kaydın satırı
    boşuna kilitlenmez. Yazma yolları (PATCH/DELETE) bu kapıdan geçer: kilitsiz
    okunursa eşzamanlı bir `submit` durum kapısını TOCTOU ile atlatabilir."""
    context = await visible_entry(session, actor, entry_id)
    locked = await repository.get_entry_locked(session, entry_id)
    if locked is None:
        raise NotFoundError(guards.ENTRY_MISSING)
    return context._replace(entry=locked)


# --- Alan doğrulamaları ---


async def validate_section(session: AsyncSession, section_id: uuid.UUID | None, site: Site) -> None:
    """Bölüm bilgi alanıdır (GK198) ama SAHİPSİZ olamaz: günlüğün ŞANTİYESİNE
    ait olmalıdır. Var olmayan bölüm de AYNI 422'yi alır (guards.SECTION_MISMATCH).

    DET-1.B: liste süzgeci ve detayın bölüm bağlamı (`?section_id=`) da bu kapıdan geçer —
    başka şantiyenin bölümüyle süzülen liste SESSİZCE boş dönmez, 422 alır."""
    if section_id is None:
        return
    row = await repository.get_section_with_site(session, section_id)
    if row is None or row[1].id != site.id:
        raise SiteValidationError(guards.SECTION_MISMATCH)


async def _assert_date_free(
    session: AsyncSession,
    site_id: uuid.UUID,
    entry_date: date,
    *,
    exclude_entry_id: uuid.UUID | None = None,
) -> None:
    """UQ (site_id, entry_date) — `IntegrityError`a DÜŞMEDEN 409 (spec §2).

    Genel `IntegrityError` handler'ı (409, "Veri bütünlüğü hatası") yarış durumu
    emniyet ağı olarak KALIR; kullanıcının normalde göreceği cümle burada üretilir.
    """
    mevcut = await repository.get_entry_by_date(
        session, site_id, entry_date, exclude_entry_id=exclude_entry_id
    )
    if mevcut is not None:
        raise DuplicateError(guards.ENTRY_DATE_TAKEN)


async def assert_entry_days_unlocked(
    session: AsyncSession, entry: SiteDiaryEntry, *extra_days: date
) -> None:
    """PLN-B2.6 KİLİT PORTU — günlüğün HER yazma yolu bunu çağırır (409).

    Kilit kararı çekirdeğin DEĞİLDİR: port (`app.core.day_hooks`) kayıtlı modüle
    (EV rapor onayı) sorar; kayıt yoksa no-op, yani modülsüz kurulum bugünkü gibi
    çalışır (spec §2.7). `extra_days`: tarihi DEĞİŞEN PATCH'in hedef günü — kilitli
    güne kayıt TAŞINAMAZ, kilitli günden de kaçırılamaz.
    """
    await day_hooks.assert_days_unlocked(session, entry.site_id, [entry.entry_date, *extra_days])


def _assert_temperature_order(entry: SiteDiaryEntry) -> None:
    """BİRLEŞİK değer (gövde + mevcut) üzerinden min ≤ max — PATCH tek alan getirebilir."""
    if not guards.temp_order_ok(entry.temp_min_c, entry.temp_max_c):
        raise SiteValidationError(guards.TEMP_ORDER)


# --- Satır iskeleti (GK: satır ekle/sil YOK, liste BOQ'dan gelir) ---


async def _build_lines(
    session: AsyncSession, site_id: uuid.UUID, section_id: uuid.UUID | None = None
) -> list[SiteDiaryLine]:
    """İskelet satırlarını BOQ pozlarından kurar; hangi (kalem, bölüm) satırlarının
    açılacağı `skeleton.skeleton_keys`in (kural A) kararıdır — önizleme ucu AYNI
    fonksiyonu çağırır (GKS-B1; bekçi testi). Snapshot DÖRTLÜSÜ kalemden kopyalanır.

    `quantity` 0 başlar: iskelet kuralın açtığı satırları açar, o gün dokunulmayan satır
    sıfır kalır (GK228). Fiyatsız kalem kontrolü YOKTUR — `boq_items.unit_price` NOT
    NULL'dur.

    BOQ'su hiç girilmemiş şantiyede satırsız taslak açılır: günlük kayıt BOQ'ya
    BAĞIMLI değildir (hava/İSG/işçi bilgisi tek başına da anlamlıdır).
    """
    items = await repository.list_boq_items(session, site_id)
    keys = skeleton.skeleton_keys(
        [skeleton.SkeletonItem(item.id, item.quantity) for item in items],
        skeleton.group_allocations(
            await repository.allocations_for_items(session, [item.id for item in items])
        ),
        section_id,
        await repository.section_order(session, site_id),
    )
    by_id = {item.id: item for item in items}
    return [
        SiteDiaryLine(
            boq_item_id=key.item_id,
            section_id=key.section_id,
            code=by_id[key.item_id].code,
            description=by_id[key.item_id].description,
            unit=by_id[key.item_id].unit,
            unit_price=by_id[key.item_id].unit_price,
            quantity=_ZERO_QUANTITY,
        )
        for key in keys
    ]


# --- Oluşturma ---


async def create(
    session: AsyncSession,
    actor: User,
    site_id: uuid.UUID,
    data: SiteDiaryEntryCreate,
    scope: DisciplineScope = UNRESTRICTED,
) -> EntryContext:
    """`project_id` şantiyeden KOPYALANIR: görünürlük süzgeci her liste sorgusunda
    JOIN gerektirmesin diye (model docstring'i).

    Doğrulamalar `session.add`DAN ÖNCE koşar — reddedilen istek kısmi yazma
    bırakmaz. GKS-B1: iskelet başlığın `section_id`sine göre kurulur (kural A) ve
    gövdede `lines` varsa İSKELETLE BİRLEŞİR (`lines.merge_lines`: silme YOK) — gövde
    doğrulaması da yazmadan ÖNCE koşar, patlarsa günlük de yazılmaz.
    """
    site, project = await visible_site(session, actor, site_id)
    await day_hooks.assert_days_unlocked(session, site.id, [data.entry_date])
    await _assert_date_free(session, site.id, data.entry_date)
    await validate_section(session, data.section_id, site)

    # `**model_dump()` güvenlidir çünkü `SiteDiaryEntryCreate`in HER alanı bir
    # kolondur ve `status`/`submitted_at`/`created_by` şemada YOKTUR — gövdeden
    # durum ya da damga yazılamaz. Şemaya kolon olmayan bir alan eklenirse bu
    # satır `TypeError` ile patlar; sessizce yok saymaz.
    entry = SiteDiaryEntry(
        site_id=site.id,
        project_id=site.project_id,
        created_by=actor.id,
        **data.model_dump(exclude={"lines"}),
    )
    _assert_temperature_order(entry)
    entry.lines = await _build_lines(session, site.id, data.section_id)
    if data.lines:
        await lines.merge_lines(session, entry, data.lines, scope)
    session.add(entry)
    await session.flush()
    await session.refresh(entry)
    return EntryContext(entry=entry, site=site, project=project)


# --- Düzenleme (yalnız draft) ---


async def update(
    session: AsyncSession,
    actor: User,
    entry_id: uuid.UUID,
    data: SiteDiaryEntryUpdate,
) -> EntryContext:
    """Gönderilmiş kayda YAZMA YASAK (409): geri almanın tek yolu `reopen`dır (T4).

    `exclude_unset` ZORUNLUDUR: gönderilmeyen alanlar `None`a düşürülseydi tek
    alan güncelleyen bir istek diğer her alanı SESSİZCE silerdi. Aynı kural
    `worker_counts` için de geçerlidir: alan gönderilmezse kırılım KORUNUR,
    boş liste gönderilirse TEMİZLENİR (T3).
    """
    context = await visible_entry_locked(session, actor, entry_id)
    new_date = data.entry_date if "entry_date" in data.model_fields_set else None
    await assert_entry_days_unlocked(
        session, context.entry, *([new_date] if new_date is not None else [])
    )
    if context.entry.status != DiaryStatus.draft:
        raise ConflictError(guards.ENTRY_NOT_EDITABLE)

    changes = data.model_dump(exclude_unset=True)
    # İşçi kırılımı bir KOLON DEĞİL bir İLİŞKİDİR: aşağıdaki `setattr` döngüsüne
    # girseydi ham `dict` listesi ilişkiye atanır, SQLAlchemy patlardı. Pydantic
    # nesneleri `data`dan okunur — `model_dump` onları `dict`e çevirmiştir.
    worker_counts = data.worker_counts if "worker_counts" in changes else None
    changes.pop("worker_counts", None)
    if worker_counts is not None:  # yazmadan ONCE: eski istemci hicbir alani degistirmesin
        lines.assert_current_worker_client(context.entry, worker_counts)
    if "entry_date" in changes and changes["entry_date"] != context.entry.entry_date:
        await _assert_date_free(
            session, context.entry.site_id, changes["entry_date"], exclude_entry_id=entry_id
        )
    if "section_id" in changes:
        await validate_section(session, changes["section_id"], context.site)

    for field, value in changes.items():
        setattr(context.entry, field, value)
    _assert_temperature_order(context.entry)
    if worker_counts is not None:
        await lines.apply_worker_counts(session, context.entry, worker_counts)
    await session.flush()
    await session.refresh(context.entry)
    return context


# --- Poz satırları (T3) ---


async def save_lines(
    session: AsyncSession,
    actor: User,
    entry_id: uuid.UUID,
    data: SiteDiaryLinesSave,
    scope: DisciplineScope = UNRESTRICTED,
) -> tuple[EntryContext, int]:
    """`PUT /diary/{entry_id}/lines` — DEĞİŞTİRME semantiği (gövde ekranın TAMAMI).

    Bu katman YALNIZ kapsam (404), KİLİT ve durum kapısını kurar; gövdenin
    doğrulaması ve uygulanması `lines.apply_lines`tadır (ayrım bilinçlidir:
    `lines.py` görünürlük katmanını çağırsaydı `service → lines → service`
    döngüsel importu doğardı).

    Kilit ZORUNLUDUR: kilitsiz okunsaydı eşzamanlı bir `submit` (T4) durum
    kapısını TOCTOU ile atlatır, gönderilmiş kayda satır yazılabilirdi.

    İkinci öğe: gövdeden adreslenemediği için düşen bağı-kopmuş satır sayısı.

    DSC-B2 `scope`: kısıtsızda bugünkü yol AYNEN (`apply_lines`). Kısıtlıda gövde yalnız
    görünür satırları değiştirir (`lines.apply_lines_scoped`, BİRLEŞTİRME): gizli satırlar
    korunur ve düşen sayısı 0'dır. Kilit (`FOR UPDATE`) iki yolda da aynıdır; kısıtlı+kısıtsız
    eşzamanlı yazımda kısıtsızın TAM DEĞİŞTİRMESİ "son yazan kazanır" (S7, dokümante karar):
    kilit yazmaları serileştirir, ama kısıtsız gövde kısıtlının satırını içermiyorsa siler.
    """
    context = await visible_entry_locked(session, actor, entry_id)
    await assert_entry_days_unlocked(session, context.entry)
    if context.entry.status != DiaryStatus.draft:
        raise ConflictError(guards.ENTRY_NOT_EDITABLE)

    if scope.is_restricted:
        dropped = await lines.apply_lines_scoped(session, context.entry, data.lines, scope)
    else:
        lines.assert_current_line_client(context.entry, data.lines)
        dropped = await lines.apply_lines(session, context.entry, data.lines)
    await session.refresh(context.entry)
    return context, dropped


# --- Silme (iki katmanlı kural) ---


async def delete_entry(
    session: AsyncSession, actor: User, entry_id: uuid.UUID
) -> DeletedEntrySummary:
    """Katman 1: gönderilmiş kayıt ADMİN DAHİL kimseye silinmez (409).
    Kapı router'da yalnız Sistem Yöneticisi'dir (SIL-B1, K4: "kendi taslağı" istisnası YOK).
    """
    entry, site, project = await visible_entry_locked(session, actor, entry_id)
    await assert_entry_days_unlocked(session, entry)

    if entry.status != DiaryStatus.draft:
        raise ConflictError(guards.ENTRY_NOT_DELETABLE)

    # Özet `session.delete` ÖNCESİNDE kurulur — sonra okunursa denetim satırı
    # sessizce varsayılanlara düşer (taşeron H10 mutasyon denetiminin bulgusu).
    summary = DeletedEntrySummary(
        project_name=project.name,
        site_name=site.name,
        entry_date=entry.entry_date,
        status_label=_STATUS_LABELS[entry.status],
        line_count=len(entry.lines),
    )

    # `lines`/`worker_counts` cascade="all, delete-orphan" — birlikte gider.
    await session.delete(entry)
    await session.flush()
    return summary

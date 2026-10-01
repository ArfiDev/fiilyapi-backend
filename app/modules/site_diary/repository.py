"""Şantiye günlüğü okuma/yazma sorguları (T2).

`subcontractor_progress_payments/repository.py` deseninin aynısı: filtreler SQL
düzeyinde uygulanır, kapsam kararı HER ZAMAN çağıran servisten gelir — bu modül
KENDİSİ görünürlük kararı VERMEZ (iki katman kuralı).
"""

import calendar
import uuid
from collections.abc import Collection
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import (
    UNRESTRICTED,
    DisciplineScope,
    item_fk_conditions,
    item_visible_clause,
)
from app.modules.boq.models import BoqItem, BoqItemSectionAllocation
from app.modules.contracts.models import (
    EmployerContractItem,
    Subcontractor,
    SubcontractorContractItem,
)
from app.modules.site_diary.models import DiaryStatus, SiteDiaryEntry, SiteDiaryLine
from app.modules.sites.models import Section, Site
from app.modules.users.models import User


async def get_entry(session: AsyncSession, entry_id: uuid.UUID) -> SiteDiaryEntry | None:
    """`session.get` DEĞİL açık `select` + `populate_existing`.

    `session.get` kimlik haritasındaki nesneyi SORGU KOŞMADAN döndürür; o nesne
    aynı oturumda `SiteDiaryEntry(...)` ile kurulmuşsa `worker_counts` ilişkisi
    HİÇ yüklenmemiş olur ve `lazy="selectin"` devreye girmez — okuma yolu senkron
    olduğu için erişim anında `MissingGreenlet` ile patlar (ampirik olarak
    doğrulandı: T2 detay testi bu yüzden kırmızıydı).

    Açık `select` her çağrıda sorguyu koşar, `populate_existing` kimlik
    haritasındaki nesneyi TAZELER ve iki `selectin` ilişkisi de yüklenir
    (toplam 3 sorgu, N+1 yok).
    """
    stmt = (
        select(SiteDiaryEntry)
        .where(SiteDiaryEntry.id == entry_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalars().first()


async def get_entry_locked(session: AsyncSession, entry_id: uuid.UUID) -> SiteDiaryEntry | None:
    """`SELECT … FOR UPDATE`. `populate_existing=True` ZORUNLUDUR: kimlik
    haritasındaki ESKİ nesne dönerse kilit alınmış ama DURUM eski değerinden
    okunmuş olur — var gibi görünen, aslında olmayan bir koruma
    (`subcontractor_progress_payments.repository.get_payment_locked` dersi).
    """
    return await session.get(SiteDiaryEntry, entry_id, with_for_update=True, populate_existing=True)


async def get_entry_by_date(
    session: AsyncSession,
    site_id: uuid.UUID,
    entry_date: date,
    *,
    exclude_entry_id: uuid.UUID | None = None,
) -> SiteDiaryEntry | None:
    """UQ (site_id, entry_date) ÖN KONTROLÜ — `IntegrityError`a düşmeden 409.

    `exclude_entry_id` PATCH yolu içindir: kaydın kendi tarihini kendisiyle
    çakıştırması (aynı tarihi yeniden göndermek) bir çakışma DEĞİLDİR.
    """
    stmt = select(SiteDiaryEntry).where(
        SiteDiaryEntry.site_id == site_id,
        SiteDiaryEntry.entry_date == entry_date,
    )
    if exclude_entry_id is not None:
        stmt = stmt.where(SiteDiaryEntry.id != exclude_entry_id)
    return (await session.execute(stmt)).scalars().first()


async def get_section_with_site(
    session: AsyncSession, section_id: uuid.UUID
) -> tuple[Section, Site] | None:
    """Bölüm + şantiyesi TEK sorguda — `section_id` sahiplik kontrolü."""
    stmt = (
        select(Section, Site).join(Site, Site.id == Section.site_id).where(Section.id == section_id)
    )
    row = (await session.execute(stmt)).first()
    return (row[0], row[1]) if row is not None else None


async def list_boq_items(session: AsyncSession, site_id: uuid.UUID) -> list[BoqItem]:
    """Satır iskeletinin KAYNAĞI (spec §2): şantiyenin TÜM BOQ pozları.

    Poz kaynağı BOQ'dur; grup kırılımı satıra TAŞINMAZ (GK'de satır listesi
    düzdür), bu yüzden gruplar üzerinden değil doğrudan kalemler okunur.
    Sıralama `code`tur: satır ilişkisi de (`SiteDiaryLine.code`) böyle sıralanır,
    iki farklı sıra iki farklı ekran görüntüsü demek olurdu.
    """
    stmt = select(BoqItem).where(BoqItem.site_id == site_id).order_by(BoqItem.code)
    return list((await session.execute(stmt)).scalars().all())


async def get_boq_items_by_ids(
    session: AsyncSession, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, BoqItem]:
    """`PUT …/lines` gövdesindeki TÜM pozlar TEK sorguda (satır başına sorgu YOK).

    Şantiye süzgeci BURADA uygulanmaz: sahiplik kararı çağıran katmanın işidir
    (`lines._resolve`), çünkü "poz yok" ile "poz başka şantiyenin" AYNI cevabı
    almalıdır ve bu karar bir SORGU değil bir KURALDIR.
    """
    if not item_ids:
        return {}
    stmt = select(BoqItem).where(BoqItem.id.in_(item_ids))
    return {item.id: item for item in (await session.execute(stmt)).scalars().all()}


async def cumulative_quantities_before(
    session: AsyncSession, site_id: uuid.UUID, entry_date: date
) -> dict[uuid.UUID, Decimal]:
    """GK229 kümülatifinin ÖN-TOPLAMI: aynı ayda, aynı şantiyede, bu günden ÖNCE
    **gönderilmiş** kayıtların poz bazlı miktar toplamı.

    `submitted` süzgeci T4 `summary` ucuyla (spec §3) BİLEREK aynıdır: iki ekran
    aynı sayıyı söylemek zorundadır. Kaydın KENDİ miktarı buraya girmez —
    okuma katmanı onu üstüne ekler (`read._line_read`), böylece taslak da
    "gönderirsem ne olacak" değerini gösterir.

    Bağı kopmuş satır (`boq_item_id IS NULL`) hangi poza yazılacağını KAYBETMİŞTİR;
    toplamdan düşer (taşeron `completed_quantities` deseninin aynısı).
    """
    start, _ = _month_bounds(entry_date.year, entry_date.month)
    stmt = (
        select(SiteDiaryLine.boq_item_id, func.sum(SiteDiaryLine.quantity))
        .join(SiteDiaryEntry, SiteDiaryEntry.id == SiteDiaryLine.entry_id)
        .where(
            SiteDiaryEntry.site_id == site_id,
            SiteDiaryEntry.status == DiaryStatus.submitted,
            SiteDiaryEntry.entry_date >= start,
            SiteDiaryEntry.entry_date < entry_date,
            SiteDiaryLine.boq_item_id.is_not(None),
        )
        .group_by(SiteDiaryLine.boq_item_id)
    )
    return {item_id: total for item_id, total in (await session.execute(stmt)).all()}


async def leaf_cumulative_before(
    session: AsyncSession,
    site_id: uuid.UUID,
    entry_date: date,
    item_ids: Collection[uuid.UUID],
) -> dict[tuple[uuid.UUID, uuid.UUID | None], Decimal]:
    """PLN-B2.1 — YAPRAK (kalem × bölüm) kümülatifinin ÖN-TOPLAMI, TÜM ZAMANLAR.

    Aynı şantiyede, bu günden ÖNCE **gönderilmiş** kayıtların (kalem, bölüm)
    bazında toplamı; Bölümsüz (NULL) kendi anahtarıdır. `cumulative_quantities_
    before`dan FARKI: ay sınırı YOKTUR (planlı miktarla kıyaslanan budur) ve
    bölüm kırılımlıdır. `submitted` süzgeci ve "kaydın kendisi okuma katmanında
    eklenir" kuralı AYNIDIR. TEK sorgu (N+1 yok); boş istekte sorgu açılmaz.
    """
    if not item_ids:
        return {}
    stmt = (
        select(
            SiteDiaryLine.boq_item_id,
            SiteDiaryLine.section_id,
            func.sum(SiteDiaryLine.quantity),
        )
        .join(SiteDiaryEntry, SiteDiaryEntry.id == SiteDiaryLine.entry_id)
        .where(
            SiteDiaryEntry.site_id == site_id,
            SiteDiaryEntry.status == DiaryStatus.submitted,
            SiteDiaryEntry.entry_date < entry_date,
            SiteDiaryLine.boq_item_id.in_(list(item_ids)),
        )
        .group_by(SiteDiaryLine.boq_item_id, SiteDiaryLine.section_id)
    )
    return {
        (item_id, section_id): total
        for item_id, section_id, total in (await session.execute(stmt)).all()
    }


async def allocations_for_items(
    session: AsyncSession, item_ids: Collection[uuid.UUID]
) -> dict[tuple[uuid.UUID, uuid.UUID], Decimal]:
    """(kalem, bölüm) → tahsis miktarı (`boq_item_section_allocations`), TEK sorgu.

    Yazma yolunda "tahsis var mı", okuma yolunda planlı miktar bunu okur.
    """
    if not item_ids:
        return {}
    stmt = select(
        BoqItemSectionAllocation.boq_item_id,
        BoqItemSectionAllocation.section_id,
        BoqItemSectionAllocation.quantity,
    ).where(BoqItemSectionAllocation.boq_item_id.in_(list(item_ids)))
    return {
        (item_id, section_id): quantity
        for item_id, section_id, quantity in (await session.execute(stmt)).all()
    }


async def section_order(session: AsyncSession, site_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """GKS-B1: şantiyenin bölümleri → sıra (`sort_order, id`), TEK sorgu. İskeletin bölüm
    satırı sırasının tek kaynağıdır (`skeleton.skeleton_keys`)."""
    stmt = (
        select(Section.id)
        .where(Section.site_id == site_id)
        .order_by(Section.sort_order, Section.id)
    )
    return {
        section_id: rank for rank, section_id in enumerate((await session.execute(stmt)).scalars())
    }


async def section_site_ids(
    session: AsyncSession, section_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """Bölüm → şantiye, TEK sorgu (satır bölümlerinin sahiplik kontrolü)."""
    if not section_ids:
        return {}
    stmt = select(Section.id, Section.site_id).where(Section.id.in_(list(section_ids)))
    return {section_id: site_id for section_id, site_id in (await session.execute(stmt)).all()}


async def existing_subcontractor_ids(
    session: AsyncSession, subcontractor_ids: Collection[uuid.UUID]
) -> set[uuid.UUID]:
    """Var olan taşeron firmaları, TEK sorgu (işçi firma satırı doğrulaması)."""
    if not subcontractor_ids:
        return set()
    stmt = select(Subcontractor.id).where(Subcontractor.id.in_(list(subcontractor_ids)))
    return set((await session.execute(stmt)).scalars().all())


def _month_bounds(year: int, month: int | None) -> tuple[date, date]:
    """Yarı-açık aralık yerine KAPALI aralık: `entry_date` bir `Date`tir, saat
    bileşeni yoktur, bu yüzden `BETWEEN` gün sınırlarını kaçırmaz."""
    if month is None:
        return date(year, 1, 1), date(year, 12, 31)
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def period_conditions(*, year: int | None, month: int | None) -> list:
    """Dönem süzgecinin TEK kopyası — liste, sayaç ve T4 `summary` aynı gövdeyi okur.

    Süzgeci her uç kendi kopyasıyla kursaydı, aynı ekranda görünen liste ile
    hakediş özeti zamanla FARKLI günleri kapsardı. `year is None` = süzgeç yok
    (tüm dönem); `month` YALNIZ `year` ile anlamlıdır (router 422 verir).
    """
    if year is None:
        return []
    start, end = _month_bounds(year, month)
    return [SiteDiaryEntry.entry_date.between(start, end)]


def section_conditions(section_id: uuid.UUID | None, scope: DisciplineScope = UNRESTRICTED) -> list:
    """DET-1.B Kural A'nın TEK kopyası — liste, sayaç ve önceki/sonraki aynı gövdeyi okur.

    Bölümün günlüğü = başlığı (`section_id`, bilgi alanı) bu bölüm olan gün ∪ bu bölüme
    MİKTAR SATIRI yazılmış gün (kullanıcı kararı 2026-09-25). Satır kolu `EXISTS`tir, JOIN
    DEĞİL: aynı günün iki satırı bölümde olsa da kayıt BİR kez döner (JOIN çoğaltırdı).
    `None` = süzgeç yok.

    DSC-B1: kısıtlıda satır kolu YALNIZ görünür kalemin satırını sayar (`item_fk_conditions`;
    NULL satır görünmez). Başlık kolu (`entry.section_id`) ORTAK başlıktır (Ü5) ve kalır.
    Liste, sayaç ve komşular bu TEK gövdeyi okur → üçü aynı kümede kalır.
    """
    if section_id is None:
        return []
    line_in_section = exists().where(
        SiteDiaryLine.entry_id == SiteDiaryEntry.id,
        SiteDiaryLine.section_id == section_id,
        *item_fk_conditions(scope, SiteDiaryLine.boq_item_id, BoqItem),
    )
    return [or_(SiteDiaryEntry.section_id == section_id, line_in_section)]


def _list_stmt(
    site_id: uuid.UUID,
    *,
    year: int | None,
    month: int | None,
    section_id: uuid.UUID | None = None,
    scope: DisciplineScope = UNRESTRICTED,
):
    """Liste ve sayaç sorgusunun PAYLAŞTIĞI `WHERE` gövdesi.

    İki sorgu ayrı süzgeç kopyası taşısaydı `total` ile `items` zamanla farklı
    kümeleri sayardı — sayfalamanın en sinsi hatası.
    """
    return select(SiteDiaryEntry).where(
        SiteDiaryEntry.site_id == site_id,
        *period_conditions(year=year, month=month),
        *section_conditions(section_id, scope),
    )


async def list_entries(
    session: AsyncSession,
    site_id: uuid.UUID,
    *,
    year: int | None,
    month: int | None,
    limit: int,
    offset: int,
    section_id: uuid.UUID | None = None,
    scope: DisciplineScope = UNRESTRICTED,
) -> list[SiteDiaryEntry]:
    """ "Son Kayıtlar": en YENİ gün önce. Eşitlik durumu UQ nedeniyle imkânsızdır
    ama `id` ikincil sıra olarak durur — sayfalamanın deterministik olması için."""
    stmt = (
        _list_stmt(site_id, year=year, month=month, section_id=section_id, scope=scope)
        .order_by(SiteDiaryEntry.entry_date.desc(), SiteDiaryEntry.id)
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_entries(
    session: AsyncSession,
    site_id: uuid.UUID,
    *,
    year: int | None,
    month: int | None,
    section_id: uuid.UUID | None = None,
    scope: DisciplineScope = UNRESTRICTED,
) -> int:
    inner = _list_stmt(
        site_id, year=year, month=month, section_id=section_id, scope=scope
    ).with_only_columns(SiteDiaryEntry.id)
    stmt = select(func.count()).select_from(inner.subquery())
    return int((await session.execute(stmt)).scalar_one())


# --- T4: agregasyon (yalnız `submitted`) ---


def submitted_period_conditions(*, year: int | None, month: int | None) -> list:
    """ "YALNIZ gönderilmiş + seçilen dönem" süzgecinin TEK kopyası.

    T4 özeti, T3 kümülatifi ve T5 önerisi bu gövdeyi PAYLAŞIR. Ayrı kopyalar
    tutulsaydı aynı ekranın üç bölümü (kümülatif · hakediş özeti · günlükten
    doldur) zamanla FARKLI günleri kapsar, kullanıcı üç farklı sayı görürdü.
    """
    return [
        SiteDiaryEntry.status == DiaryStatus.submitted,
        *period_conditions(year=year, month=month),
    ]


async def count_submitted_entries(
    session: AsyncSession, site_id: uuid.UUID, *, year: int | None, month: int | None
) -> int:
    """Özetin kaç GÜNDEN oluştuğu. Süzgeç gövdesi liste ucuyla ORTAKTIR
    (`period_conditions`); `submitted` kısıtı spec §3'ün kuralıdır."""
    stmt = select(func.count(SiteDiaryEntry.id)).where(
        SiteDiaryEntry.site_id == site_id,
        *submitted_period_conditions(year=year, month=month),
    )
    return int((await session.execute(stmt)).scalar_one())


async def summary_lines(
    session: AsyncSession,
    site_id: uuid.UUID,
    *,
    year: int | None,
    month: int | None,
    scope: DisciplineScope = UNRESTRICTED,
) -> list[tuple[SiteDiaryLine, BoqItem, EmployerContractItem | None]]:
    """Hakediş Özeti ekranının HAM satırları: dönemdeki **gönderilmiş** günlerin
    poz satırları + BOQ kalemi + (varsa) köprülendiği işveren sözleşmesi kalemi.

    Toplama SQL'de DEĞİL bellekte yapılır (`summary.py`): satır ₺'si kuruş
    bazında satır düzeyinde yuvarlanır (`read.line_amount`) — SQL'de `SUM`
    almak para matematiğinin ikinci bir kopyasını doğururdu.

    `join` (INNER) bilinçlidir: `boq_item_id IS NULL` olan bağı-kopmuş satır
    hangi poza yazılacağını KAYBETMİŞTİR, düşer — `cumulative_quantities_before`
    ile AYNI kural (iki ekran aynı sayıyı söylemek zorundadır).

    Sıralama `BoqItem.code`tur: günlük satırları da (`SiteDiaryLine.code`) böyle
    sıralanır.
    """
    stmt = (
        select(SiteDiaryLine, BoqItem, EmployerContractItem)
        .join(SiteDiaryEntry, SiteDiaryEntry.id == SiteDiaryLine.entry_id)
        .join(BoqItem, BoqItem.id == SiteDiaryLine.boq_item_id)
        .outerjoin(EmployerContractItem, EmployerContractItem.id == BoqItem.contract_item_id)
        .where(
            SiteDiaryEntry.site_id == site_id,
            *submitted_period_conditions(year=year, month=month),
            item_visible_clause(scope, BoqItem),
        )
        .order_by(BoqItem.code)
    )
    return [
        (line, item, contract_item)
        for line, item, contract_item in (await session.execute(stmt)).all()
    ]


# --- T5: hakediş "günlükten doldur" önerisi (spec §4) ---
#
# Üç sorgunun ORTAK gövdesi: gönderilmiş günlük satırı → BOQ pozu. `join`
# (INNER) bilinçlidir — `boq_item_id IS NULL` olan bağı-kopmuş satır hangi poza
# yazılacağını KAYBETMİŞTİR ve T4 özetiyle AYNI kuralla düşer.
#
# `HAVING SUM(...) > 0`: günlük iskeleti şantiyenin TÜM pozlarını sıfır miktarla
# açar (T2). Sıfırlar süzülmeseydi öneri her ay BOQ'nun tamamını sıfır miktarla
# listeler, kullanıcının `PUT …/lines`a yapıştıracağı gövde var olan satırları
# sıfırlayan bir silme emrine dönerdi.


def _submitted_line_stmt(*, year: int | None, month: int | None):
    return (
        select(SiteDiaryLine.quantity)
        .join(SiteDiaryEntry, SiteDiaryEntry.id == SiteDiaryLine.entry_id)
        .join(BoqItem, BoqItem.id == SiteDiaryLine.boq_item_id)
        .where(*submitted_period_conditions(year=year, month=month))
    )


async def employer_suggestion_rows(
    session: AsyncSession, project_id: uuid.UUID, *, year: int | None, month: int | None
) -> list[tuple[uuid.UUID, uuid.UUID, Decimal]]:
    """İşveren önerisinin ham satırları: `(contract_item_id, site_id, miktar)`.

    Kırılım (kalem, şantiye) ÇİFTİDİR çünkü işveren hakediş satırının kimliği
    budur (`uq_progress_payment_lines_cell`): sözleşme kalemi PROJE düzeyindedir,
    aynı kaleme köprülü iki şantiyenin miktarı TEK satırda toplanamaz.

    `EmployerContractItem` JOIN'i iki iş yapar: köprüsüz pozu düşürür ve kalemin
    BAŞKA projeye ait olamayacağını (veri bozulması korkuluğu) SQL'de zorlar.
    İkisi BİRBİRİNİ TUTAR: `project_id` koşulu `WHERE`da olduğu için JOIN
    `outerjoin`a çevrilse bile köprüsüz satır (NULL kalem) yine düşer — yani
    korkuluk TEK BİR yerde değil, iki kez durur. Koşul kaldırılırsa hem yabancı
    projenin kalemi sızar hem de NULL satırlar öneriye girer.

    Sıralama kalem koduna göredir — ekranda BOQ sırasıyla aynı okunur.
    """
    total = func.sum(SiteDiaryLine.quantity)
    stmt = (
        _submitted_line_stmt(year=year, month=month)
        .join(EmployerContractItem, EmployerContractItem.id == BoqItem.contract_item_id)
        .with_only_columns(BoqItem.contract_item_id, SiteDiaryEntry.site_id, total)
        .where(
            SiteDiaryEntry.project_id == project_id,
            EmployerContractItem.project_id == project_id,
        )
        .group_by(BoqItem.contract_item_id, SiteDiaryEntry.site_id, EmployerContractItem.code)
        .having(total > 0)
        .order_by(EmployerContractItem.code, SiteDiaryEntry.site_id)
    )
    return [(row[0], row[1], row[2]) for row in (await session.execute(stmt)).all()]


def _subcontractor_scope_conditions(site_id: uuid.UUID | None, project_id: uuid.UUID) -> list:
    """Taşeron köprüsünün KAPSAM süzgeci — TEK kopya (iki sorgu da bunu çağırır).

    `site_id` DOLUYSA sözleşmenin şantiyesi; NULL ise (proje-geneli sözleşme)
    sözleşmenin projesindeki TÜM şantiyeler (kullanıcı kararı 2026-08-27 —
    spec §7 S5 TERSİNE ÇEVRİLDİ).

    **Neden tek kopya:** öneri satırları (`subcontractor_suggestion_rows`) ile
    "öneriye giremedi" sayacı (`subcontractor_unbridged_item_count`) AYNI günlük
    kümesini kapsamak zorundadır. İki kopya zamanla ayrışır, iki sorgu hangi
    günlüklerin sayıldığı konusunda ÇELİŞİR ve kullanıcı "miktar yok ama sayaç
    var" gibi kendi kendisiyle tutarsız bir ekran görürdü —
    `submitted_period_conditions` ve `_list_stmt` ile AYNI gerekçe.

    **Neden `SiteDiaryEntry.project_id`, `sites` üzerinden JOIN DEĞİL:** emsal
    işveren sorguları (`employer_suggestion_rows` / `employer_unbridged_item_count`)
    da bu snapshot sütununu kullanır; üstelik sütun BAYATLAYAMAZ, çünkü bir
    şantiye başka projeye TAŞINAMAZ (`sites/schemas.py::SiteUpdate` — `project_id`
    güncellenebilir alan değildir). JOIN eklemek ikinci bir kapsam kaynağı
    doğurur, kazancı olmazdı.
    """
    if site_id is not None:
        return [SiteDiaryEntry.site_id == site_id]
    return [SiteDiaryEntry.project_id == project_id]


async def subcontractor_suggestion_rows(
    session: AsyncSession,
    contract_id: uuid.UUID,
    site_id: uuid.UUID | None,
    project_id: uuid.UUID,
    *,
    year: int | None,
    month: int | None,
) -> list[tuple[uuid.UUID, Decimal]]:
    """Taşeron önerisinin ham satırları: `(subcontractor_contract_item_id, miktar)`.

    Köprü İKİ ADIMLIDIR: günlük satırı → `boq_items.contract_item_id` (işveren
    kalemi) → `subcontractor_contract_items.source_contract_item_id`. Kapsam
    süzgeci ÇAĞIRANDAN gelir (`_subcontractor_scope_conditions`): şantiyeye bağlı
    sözleşmede ŞANTİYE, proje-geneli sözleşmede PROJE.

    Taşeron satırında şantiye kırılımı YOKTUR (spec §2), bu yüzden gruplama
    proje-geneli sözleşmede de yalnız KALEMDİR: aynı kaleme köprülü iki
    şantiyenin miktarı TEK satırda toplanır. Sıralama sözleşme kaleminin kendi
    sırasıdır (`items` ilişkisinin `order_by`'ı ile aynı).
    """
    total = func.sum(SiteDiaryLine.quantity)
    stmt = (
        _submitted_line_stmt(year=year, month=month)
        .join(
            SubcontractorContractItem,
            SubcontractorContractItem.source_contract_item_id == BoqItem.contract_item_id,
        )
        .with_only_columns(SubcontractorContractItem.id, total)
        .where(
            *_subcontractor_scope_conditions(site_id, project_id),
            SubcontractorContractItem.contract_id == contract_id,
        )
        .group_by(
            SubcontractorContractItem.id,
            SubcontractorContractItem.sort_order,
            SubcontractorContractItem.code,
        )
        .having(total > 0)
        .order_by(SubcontractorContractItem.sort_order, SubcontractorContractItem.code)
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


async def _count_unbridged(session: AsyncSession, stmt) -> int:
    """Gruplanmış sorgunun SATIR SAYISI (`count_entries` deseninin aynısı):
    `HAVING`li bir sorguda `func.count()` doğrudan sarılamaz, alt sorgu şarttır."""
    sayac = select(func.count()).select_from(stmt.subquery())
    return int((await session.execute(sayac)).scalar_one())


async def employer_unbridged_item_count(
    session: AsyncSession, project_id: uuid.UUID, *, year: int | None, month: int | None
) -> int:
    """Miktarı OLAN ama sözleşme kalemine köprülenmemiş poz sayısı.

    Sessiz atlama YOKTUR (T3 `dropped_orphan_count` deseninin aynısı): öneri
    listesinde görünmeyen miktarların varlığı kullanıcıya SAYIYLA bildirilir,
    yoksa "günlüğe yazdım, öneride yok" durumu sessiz bir veri kaybı gibi görünür.
    """
    total = func.sum(SiteDiaryLine.quantity)
    stmt = (
        _submitted_line_stmt(year=year, month=month)
        .with_only_columns(SiteDiaryLine.boq_item_id)
        .where(SiteDiaryEntry.project_id == project_id, BoqItem.contract_item_id.is_(None))
        .group_by(SiteDiaryLine.boq_item_id)
        .having(total > 0)
    )
    return await _count_unbridged(session, stmt)


async def subcontractor_unbridged_item_count(
    session: AsyncSession,
    contract_id: uuid.UUID,
    site_id: uuid.UUID | None,
    project_id: uuid.UUID,
    *,
    year: int | None,
    month: int | None,
) -> int:
    """Kapsamda miktarı olan ama BU sözleşmede karşılığı olmayan poz sayısı.

    Kapsam satırların sorgusuyla AYNI gövdeden gelir
    (`_subcontractor_scope_conditions`): şantiyeye bağlı sözleşmede şantiye,
    proje-geneli sözleşmede projenin TÜM şantiyeleri.

    İki hâli birlikte sayar: pozun işveren kalemine köprüsü hiç yok, ya da köprü
    var ama sözleşmenin hiçbir kalemi o kaleme bağlı değil (`source_contract_item_id`).
    Kullanıcı için ikisi de aynı şeydir: "bu miktar öneriye giremedi".
    """
    total = func.sum(SiteDiaryLine.quantity)
    eslesenler = select(SubcontractorContractItem.source_contract_item_id).where(
        SubcontractorContractItem.contract_id == contract_id,
        SubcontractorContractItem.source_contract_item_id.is_not(None),
    )
    stmt = (
        _submitted_line_stmt(year=year, month=month)
        .with_only_columns(SiteDiaryLine.boq_item_id)
        .where(
            *_subcontractor_scope_conditions(site_id, project_id),
            or_(
                BoqItem.contract_item_id.is_(None),
                BoqItem.contract_item_id.not_in(eslesenler),
            ),
        )
        .group_by(SiteDiaryLine.boq_item_id)
        .having(total > 0)
    )
    return await _count_unbridged(session, stmt)


# --- DET-1.B: salt okunur detay bağlamı ---


class Neighbour(NamedTuple):
    id: uuid.UUID
    entry_date: date


async def neighbours(
    session: AsyncSession,
    site_id: uuid.UUID,
    entry_date: date,
    section_id: uuid.UUID | None,
    scope: DisciplineScope = UNRESTRICTED,
) -> tuple[Neighbour | None, Neighbour | None]:
    """(önceki, sonraki) = aynı şantiyede bu günden hemen ÖNCEKİ / SONRAKİ kayıt + TARİHİ.

    Tarih kimlikle AYNI satırdan gelir: istemci doğrudan bağlantıyla geldiği detayda komşunun
    tarihini listeden (sayfalı) güvenilir bilemez.

    Gün başına tek kayıt (`uq_site_diary_entries_site_date`) olduğundan tarih sırası
    TAM sıradır; bölüm verilirse Kural A (`section_conditions`) ile süzülür — listeyle
    AYNI küme, istemci listeden türetmek zorunda kalmaz.
    """
    base = (SiteDiaryEntry.site_id == site_id, *section_conditions(section_id, scope))
    prev_stmt = (
        select(SiteDiaryEntry.id, SiteDiaryEntry.entry_date)
        .where(*base, SiteDiaryEntry.entry_date < entry_date)
        .order_by(SiteDiaryEntry.entry_date.desc())
        .limit(1)
    )
    next_stmt = (
        select(SiteDiaryEntry.id, SiteDiaryEntry.entry_date)
        .where(*base, SiteDiaryEntry.entry_date > entry_date)
        .order_by(SiteDiaryEntry.entry_date)
        .limit(1)
    )
    prev_row = (await session.execute(prev_stmt)).first()
    next_row = (await session.execute(next_stmt)).first()
    return (
        Neighbour(*prev_row) if prev_row is not None else None,
        Neighbour(*next_row) if next_row is not None else None,
    )


async def section_names(
    session: AsyncSession, section_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not section_ids:
        return {}
    stmt = select(Section.id, Section.name).where(Section.id.in_(section_ids))
    return dict((await session.execute(stmt)).tuples().all())


async def subcontractor_names(
    session: AsyncSession, subcontractor_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not subcontractor_ids:
        return {}
    stmt = select(Subcontractor.id, Subcontractor.name).where(
        Subcontractor.id.in_(subcontractor_ids)
    )
    return dict((await session.execute(stmt)).tuples().all())


async def user_names(
    session: AsyncSession, user_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not user_ids:
        return {}
    stmt = select(User.id, User.full_name).where(User.id.in_(user_ids))
    return dict((await session.execute(stmt)).tuples().all())

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from typing import NamedTuple

from sqlalchemy import func, inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

# Silme korkuluklari icin (asagida). TEPE SEVIYEDE import edilebilirler cunku
# `boq/models` ve `units/models` yalniz `app.core.db.Base`e baglidir — sites'a
# geri bakmazlar, dolayisiyla dongusel import RISKI YOKTUR (olcum: iki dosyanin
# da tek `app.` importu `Base`). Fonksiyon ici import'a gerek kalmadi.
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.contracts.models import SubcontractorContract
from app.modules.documents.models.core import Document, DocumentFolder
from app.modules.progress_payments.models import ProgressPaymentLine

# Duz `GET /sites` proje adini JOIN'le okur. Dongusel import YOKTUR: ölçüldü —
# `projects/models.py` yalniz `app.core.db.Base` ve `contracts.models`a bakar,
# sites'a geri BAKMAZ.
from app.modules.projects.models import Project

# 59/60 — `sites.id`'ye CASCADE ile bagli ama KORKULUKSUZ kalan tablolar.
# Dongusel import RISKI YOKTUR (olcum: dordunun de tek `app.` importu `Base`).
# `documents.models.core` DOGRUDAN alinir (paket `__init__`i `links`i de ceker).
from app.modules.site_diary.models import SiteDiaryEntry, SiteDiaryLine
from app.modules.site_planning.models import SitePlanGoal, SitePlanRow, SitePlanSprint
from app.modules.sites.models import Section, SectionMilestone, SectionType, Site
from app.modules.timesheet.models import TimesheetEntry
from app.modules.units.models import Block
from app.modules.users.models import User, UserStatus


async def list_sites_for_project(session: AsyncSession, project_id: uuid.UUID) -> list[Site]:
    """Bir projenin santiyeleri, kod artan.

    Gorunurluk suzgeci BURADA UYGULANMAZ: proje erisimi servis katmaninda
    P1'in _visible_projects'i ile cozulur (spec §5.2), repository yalniz veri
    okur. Bu ayrim, yetki mantiginin tek noktada kalmasini saglar.
    """
    result = await session.execute(
        select(Site).where(Site.project_id == project_id).order_by(Site.code)
    )
    return list(result.scalars().all())


async def count_site_options(session: AsyncSession, project_ids: Sequence[uuid.UUID]) -> int:
    """Duz `GET /sites` icin SUZGECTEN GECMIS satir sayisi — SQL COUNT.

    🔴 `total` sayfaya DEGIL, gorunur kumeye aittir; ve suzgec COUNT'un da
    icindedir. Aksi hâlde kullanici goremedigi santiyeleri sayar (klasik kusur).
    """
    stmt = select(func.count()).select_from(Site).where(Site.project_id.in_(project_ids))
    return int((await session.execute(stmt)).scalar_one())


async def list_site_options(
    session: AsyncSession, project_ids: Sequence[uuid.UUID], limit: int, offset: int
) -> list[tuple[uuid.UUID, str, str, uuid.UUID, str]]:
    """Duz `GET /sites` sayfasi: (id, code, name, project_id, project_name).

    Sayfalama SQL duzeyindedir (LIMIT/OFFSET) — tum satirlar Python'a cekilip
    dilimlenmez. Siralama `code` artan, ESITLIK BOZUCU `id`: santiye kodu
    yalniz proje icinde tekildir (`uq_sites_project_code`), sirket genelinde
    tekrar edebilir; ikincil anahtar olmadan sayfa sinirlari kaymaya baglidir
    ve sayfalama testleri flaky olur.
    """
    stmt = (
        select(Site.id, Site.code, Site.name, Project.id, Project.name)
        .join(Project, Project.id == Site.project_id)
        .where(Site.project_id.in_(project_ids))
        .order_by(Site.code, Site.id)
        .limit(limit)
        .offset(offset)
    )
    return [tuple(row) for row in (await session.execute(stmt)).all()]


async def list_codes_with_prefix(session: AsyncSession, prefix: str) -> list[str]:
    """Verilen onekle baslayan TUM santiye kodlari (otomatik kod uretimi, spec §3.2).

    KAPSAM SUZGECI YOKTUR: `project_id` bilincli olarak sorulmaz. `PRJ-` emsalinin
    (`projects/repository.list_codes_with_prefix`) birebiri — santiye kodu evrakta
    (irsaliye, puantaj, hakedis) kurumsal kimlik gibi kullanildigi icin sayac
    sirket genelidir. Kisit ise proje ici tekil kalir (`uq_sites_project_code`).
    """
    stmt = select(Site.code).where(Site.code.like(f"{prefix}%"))
    return list((await session.execute(stmt)).scalars().all())


async def get_site(session: AsyncSession, site_id: uuid.UUID) -> Site | None:
    """Santiye + bolumleri + bagli proje (iliskiler lazy="selectin")."""
    return await session.get(Site, site_id)


async def list_sections(session: AsyncSession, site_id: uuid.UUID) -> list[Section]:
    result = await session.execute(
        select(Section).where(Section.site_id == site_id).order_by(Section.sort_order)
    )
    return list(result.scalars().all())


async def get_section(session: AsyncSession, section_id: uuid.UUID) -> Section | None:
    return await session.get(Section, section_id)


async def section_names_by_ids(
    session: AsyncSession, section_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Verilen bolumlerin adlari — TEK `IN (...)` sorgusu (N+1 YOK).

    NEDEN: `GET /boq/items/{id}/allocations` her tahsis satirinda `section_name`
    basar ama PUT'un aksine cozulecek bolumleri govdeden ALAMAZ (govde yoktur),
    mevcut SATIRLARDAN cozer. Bolum basina `get_section` cagirmak miktar
    yuzeyine N+1 sokardi; kapsam denetimi de gereksizdir: satirlar zaten
    gorunurluk suzgecinden gecmis bir POZA aittir.

    Bos girdide sorgu HIC ATILMAZ — `IN ()` bos kumede gereksiz gidis donustur.
    """
    if not section_ids:
        return {}
    result = await session.execute(
        select(Section.id, Section.name).where(Section.id.in_(set(section_ids)))
    )
    return {row[0]: row[1] for row in result.all()}


async def ensure_milestones_loaded(session: AsyncSession, sections: Sequence[Section]) -> None:
    """P11 — verilen bolumlerin `milestones` koleksiyonunu TEK sorguda doldurur.

    NEDEN GEREKLI: yanit donusturucusu (`service.to_section`) SENKRONDUR ve
    `section.milestones`e dokunur. Koleksiyon yuklu degilse SQLAlchemy orada
    tembel yukleme dener ve asenkron oturumda bu `MissingGreenlet` ile 500
    uretir. `lazy="selectin"` yalnizca bolumler SORGUYLA yuklendiginde kosar;
    `session.refresh(site, attribute_names=["sections"])` (router `_detail_of`)
    kimlik haritasindaki MEVCUT bolum nesnelerini oldugu gibi birakir ve onlarin
    koleksiyonu yuklenmemis kalabilir. Yani bu, teoride degil POST/PATCH
    `/sites` yolunda gerceklesen bir 500 riskidir.

    Yukleme TEK `SELECT`tir (N+1 YOK, TB3 sayaci): yalnizca YUKLENMEMIS bolumler
    sorulur, zaten yuklu olanlar (ve yeni eklenmis satirlar) dokunulmadan kalir.
    Sira `Section.milestones` iliskisiyle AYNIDIR (`sort_order`, `id`), aksi
    halde ayni veri iki yuzeyde farkli sirayla gorunurdu.
    """
    unloaded = [row for row in sections if "milestones" in inspect(row).unloaded]
    if not unloaded:
        return
    stmt = (
        select(SectionMilestone)
        .where(SectionMilestone.section_id.in_([row.id for row in unloaded]))
        .order_by(SectionMilestone.sort_order, SectionMilestone.id)
    )
    grouped: dict[uuid.UUID, list[SectionMilestone]] = defaultdict(list)
    for milestone in (await session.execute(stmt)).scalars().all():
        grouped[milestone.section_id].append(milestone)
    for row in unloaded:
        set_committed_value(row, "milestones", grouped[row.id])


async def get_section_in_site(
    session: AsyncSession, site_id: uuid.UUID, section_id: uuid.UUID
) -> Section | None:
    """Bolum, YALNIZ verilen santiyedeyse (P11 §3 oncul cozumu).

    Iki soruyu (var mi + ayni santiyede mi) TEK sorguda birlestirir, cunku
    cagiran taraf ikisini de AYNI cevaba (`DEPENDS_NOT_IN_SITE`) cevirir: ayri
    ayri sorup ayri metin uretmek, var olmayan id ile baska santiyedeki id'yi
    ayirt edilebilir kilardi.
    """
    stmt = select(Section).where(Section.id == section_id, Section.site_id == site_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_section_codes_with_prefix(
    session: AsyncSession, site_id: uuid.UUID, prefix: str
) -> list[str]:
    """Bir SANTIYEDEKI, verilen onekle baslayan bolum kodlari (P6 §5, `BLM-NN`).

    `list_codes_with_prefix` (santiye) deseninin birebiri, TEK FARKLA: burada
    `site_id` SUZGECI VARDIR. Santiye kodu evrakta (irsaliye, puantaj, hakedis)
    kurumsal kimlik gibi okundugu icin sayaci sirket genelidir; bolum kodu ise
    yalniz kendi santiyesinin ic siralamasinda okunur — her santiyenin kendi
    `BLM-01`inden baslamasi kullaniciyi yaniltmaz, aksine beklenendir. Kisit da
    zaten santiye ici tekildir (`uq_sections_site_code`).

    Kodsuz bolumler (kismi indeks onlara izin verir) `LIKE` suzgecine takilmaz.
    """
    stmt = select(Section.code).where(Section.site_id == site_id, Section.code.like(f"{prefix}%"))
    return list((await session.execute(stmt)).scalars().all())


async def get_section_by_code(
    session: AsyncSession,
    site_id: uuid.UUID,
    code: str,
    exclude_section_id: uuid.UUID | None = None,
) -> Section | None:
    """`(site_id, code)` cakismasini IntegrityError'a DUSMEDEN once yakalar.

    `get_site_by_code` deseninin birebiri ve ayni gerekce: kullaniciya alanina
    ozel Turkce mesaj ("Bu bölüm kodu bu şantiyede zaten kullanılıyor") verilsin,
    genel "Veri bütünlüğü hatası" degil. Kismi indeks `uq_sections_site_code`
    (yalniz `code IS NOT NULL`) YARIS DURUMU emniyet agi olarak KALIR.

    `exclude_section_id` PATCH icindir (`exclude_site_id` ile ayni gerekce):
    kendi kodunu yeniden gondermek cakisma DEGILDIR, aksi hâlde formun tum
    alanlarini birlikte gonderen her PATCH 409 verirdi.
    """
    stmt = select(Section).where(Section.site_id == site_id, Section.code == code)
    if exclude_section_id is not None:
        stmt = stmt.where(Section.id != exclude_section_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_site_by_code(
    session: AsyncSession,
    project_id: uuid.UUID,
    code: str,
    exclude_site_id: uuid.UUID | None = None,
) -> Site | None:
    """(project_id, code) cakismasini IntegrityError'a DUSMEDEN once yakalar.

    `boq/repository.get_item_by_code` deseninin birebiri (spec §7.2): servis once
    acik bir SELECT ile bakar ki kullaniciya alanina ozel Turkce mesaj verilsin
    ("Bu şantiye kodu bu projede zaten kullanılıyor"), genel "Veri bütünlüğü
    hatası" degil. `uq_sites_project_code` -> IntegrityError -> 409 handler'i
    YARIS DURUMU emniyet agi olarak KALIR (spec §8.3).

    Cakisma yakalanmazsa istisna FLUSH aninda, yani santiye satiri eklendikten
    SONRA atilir; oradan geri donmek transaction'a birakilir. Erken yakalamak
    atomikligi kolaylastirir: hicbir satir session'a girmeden reddedilir.
    """
    stmt = select(Site).where(Site.project_id == project_id, Site.code == code)
    if exclude_site_id is not None:
        stmt = stmt.where(Site.id != exclude_site_id)
    return (await session.execute(stmt)).scalar_one_or_none()


# Sef / ISG / bolum sorumlusu olarak ATANABILIR kullanici durumlari
# (karar 2026-07-30). `passive` bilincli olarak DISARIDA: kalici bir
# kullanilamazliktir, gecici degil.
_ASSIGNABLE_USER_STATUSES = (UserStatus.active, UserStatus.on_leave)


async def get_assignable_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Sef / ISG / bolum sorumlusu FK'leri icin: kullanici VAR MI ve ATANABILIR MI (spec §9).

    "Bu kullaniciyi gorme yetkin var mi" ARANMAZ: kullanici listesi `sites:full`
    sahibi icin zaten `GET /users` ile erisilebilir; burada ikinci bir gorunurluk
    kurali icat etmek iki ayri yetki mantigi uretir ve zamanla ayrisir.

    **`deps.py`'deki aktif-only kuralindan BILINCLI OLARAK AYRILIR** (karar
    2026-07-30). Iki soru ayni degildir:

    * `app/core/deps.py:36` **OTURUM ACMA YETKISI** sorar — "bu kullanici su an
      sisteme istek atabilir mi?". Izinli personel atamaz, dolayisiyla orada
      `active` disindaki her durum reddedilir ve reddedilmeye devam eder.
    * Burasi **VERI ATAMASI** sorar — "bu kisi bu santiyenin sefi mi?". Izin
      GECICI bir durumdur; yillik izindeki sef hâlâ o santiyenin sefidir.
      `on_leave` reddedilseydi sef tatildeyken santiye ACILAMAZDI.

    Bu yuzden yalniz gercekten kullanilamaz durum (`passive`) reddedilir; spec
    §7.2'nin "yok veya pasif" ifadesiyle birebir ortusur.
    """
    stmt = select(User).where(User.id == user_id, User.status.in_(_ASSIGNABLE_USER_STATUSES))
    return (await session.execute(stmt)).scalar_one_or_none()


# --- Bolum tipleri (BLF-B1) ---


async def list_section_types(session: AsyncSession) -> list[SectionType]:
    """Sirket geneli bolum tipleri: `(sort_order, id)` — esitlikte de deterministik."""
    stmt = select(SectionType).order_by(SectionType.sort_order, SectionType.id)
    return list((await session.execute(stmt)).scalars().all())


async def get_section_type(session: AsyncSession, type_id: uuid.UUID) -> SectionType | None:
    return await session.get(SectionType, type_id)


async def get_section_type_by_key(session: AsyncSession, name_key: str) -> SectionType | None:
    """Normalize anahtara gore tip (tekillik `uq_section_types_name_key`)."""
    stmt = select(SectionType).where(SectionType.name_key == name_key)
    return (await session.execute(stmt)).scalar_one_or_none()


async def next_section_type_sort_order(session: AsyncSession) -> int:
    """Mevcut en buyuk `sort_order` + 1 (bos tabloda 1). Yarista iki tip ayni
    degeri alabilir; siralama `id` ile tamamlandigi icin zararsizdir."""
    current = (await session.execute(select(func.max(SectionType.sort_order)))).scalar_one()
    return (current or 0) + 1


# --- Silme korkuluklari (spec §7.1) ---
#
# ⚠️ `sites.id`'yi hedefleyen DORT FK'nin da `ON DELETE CASCADE` oldugu koddan
# dogrulandi (`sections`, `boq_groups`, `boq_items`, `blocks`). Yani DB
# KENDILIGINDEN KORUMAZ: korkuluksuz tek bir DELETE bu dort tabloyu SESSIZCE
# bosaltir ve geri alinamaz. Asagidaki uc sorgu, T10'un acacagi silme ucunun
# TEK guvencesidir — biri delinirse o dalda cascade tetiklenir.
#
# Ucu de `units/repository.py:97 block_has_units` deseninin birebiridir:
# `select(<altsorgu>.exists())` — SATIR CEKMEZ. `count(*)` KULLANILMAZ: kac
# bolum/poz/blok oldugu hicbir yerde kullanilmaz (hata mesajinda adet
# verilmez, §7.1) ve saymak bos yere satir tarar.


async def site_has_sections(session: AsyncSession, site_id: uuid.UUID) -> bool:
    """Santiyede bolum var mi (`sections.site_id` -> CASCADE)."""
    result = await session.execute(
        select(select(Section.id).where(Section.site_id == site_id).exists())
    )
    return bool(result.scalar_one())


async def site_has_boq(session: AsyncSession, site_id: uuid.UUID) -> bool:
    """Santiyede is kalemi (poz) var mi — `boq_items` **VEYA** `boq_groups`.

    IKI tablo da sorulur (spec §7.1): ikisi de `sites.id`'ye CASCADE ile
    baglidir ve GRUP TEK BASINA da engeldir. Yalniz kalemlere bakmak, kalemsiz
    gruplari olan bir santiyenin silinmesinde o gruplari sessizce yok ederdi.
    Tek `SELECT`te `OR`'lanir: iki ayri gidis-donusun anlami yok.
    """
    result = await session.execute(
        select(
            or_(
                select(BoqItem.id).where(BoqItem.site_id == site_id).exists(),
                select(BoqGroup.id).where(BoqGroup.site_id == site_id).exists(),
            )
        )
    )
    return bool(result.scalar_one())


async def site_has_blocks(session: AsyncSession, site_id: uuid.UUID) -> bool:
    """Santiyede blok var mi (`blocks.site_id` -> CASCADE).

    Uniteler AYRICA sorulmaz: `units.block_id` `RESTRICT`tir ve unite her zaman
    bir bloga baglidir, dolayisiyla uniteli bir santiyenin bloklu olmasi
    zorunludur — blok kontrolu uniteleri de kapsar.
    """
    result = await session.execute(
        select(select(Block.id).where(Block.site_id == site_id).exists())
    )
    return bool(result.scalar_one())


async def site_has_contracts(session: AsyncSession, site_id: uuid.UUID) -> bool:
    """Santiyede taseron sozlesmesi var mi (`subcontractor_contracts.site_id`

    -> RESTRICT, Alt-Proje 2 P5 spec §7, task C12). Ilk uc kontrolun aksine bu
    FK RESTRICT'tir (CASCADE DEGIL) — sozlesme silinmeden santiye silinemez;
    korkuluk yine de burada erken calisir, aksi halde kullanici DB'nin
    `IntegrityError` -> 409 emniyet agina duser ve "Veri butunlugu hatasi" gibi
    eyleme donuk OLMAYAN bir metin gorur.
    """
    result = await session.execute(
        select(
            select(SubcontractorContract.id)
            .where(SubcontractorContract.site_id == site_id)
            .exists()
        )
    )
    return bool(result.scalar_one())


async def site_has_progress_payment_lines(session: AsyncSession, site_id: uuid.UUID) -> bool:
    """Santiyede hakedis satiri var mi (`progress_payment_lines.site_id` ->

    RESTRICT, Alt-Proje 2 P7 spec §4.2/§7.1, task H8). `site_has_contracts`
    deseninin BIREBIRI: FK RESTRICT'tir (CASCADE DEGIL), korkuluk yine de
    burada erken calisir — aksi halde kullanici DB'nin `IntegrityError` -> 409
    emniyet agina duser ve "Veri butunlugu hatasi" gibi eyleme donuk OLMAYAN
    generic bir metin gorur (bu servis korkulugu SPESIFIK, EYLEME DONUK metni
    erken doner).
    """
    result = await session.execute(
        select(
            select(ProgressPaymentLine.id).where(ProgressPaymentLine.site_id == site_id).exists()
        )
    )
    return bool(result.scalar_one())


# --- 59/60: CASCADE'li ama korkuluksuz kalan DORT dal ---
#
# Yukaridaki bes sorgu `sites.id`'ye bagli FK'lerin YARISINI kapsiyordu.
# Olcum (`command grep -rn 'ForeignKey("sites.id"' app/`): CASCADE olan on
# bacaktan yedisi korkuluksuzdu — `site_plan_rows`, `site_plan_goals`,
# `site_plan_sprints`, `site_diary_entries`, `timesheet_entries`,
# `documents`, `document_folders`.
#
# 🔴 `site_has_sections` bunlari KAPSAMAZ: dordunde de `section_id` NULLABLE'dir
# (ya SET NULL'dur ya da — plan ekipman satirinda — zaten NULL), yani bolumu
# olmayan bir santiye bes korkulugun besini de gecer ve tek `DELETE` bu
# tablolari sessizce bosaltir. `documents` gidince `document_blobs.document_id`
# CASCADE'i BAYTLARI da goturur.
#
# Desen yukaridakinin birebiri: `select(<altsorgu>.exists())`, SATIR CEKMEZ,
# `count(*)` KULLANILMAZ (hata metninde adet verilmez, §7.1).


async def site_has_timesheet(session: AsyncSession, site_id: uuid.UUID) -> bool:
    """Santiyede puantaj hucresi var mi (`timesheet_entries.site_id` -> CASCADE).

    Bu dal zincirin EN AGIRIDIR: puantaj bordronun girdisidir, sessizce gitmesi
    odenen ucretin kanit tabanini yok eder.
    """
    result = await session.execute(
        select(select(TimesheetEntry.id).where(TimesheetEntry.site_id == site_id).exists())
    )
    return bool(result.scalar_one())


async def site_has_diary(session: AsyncSession, site_id: uuid.UUID) -> bool:
    """Santiyede gunluk kaydi var mi (`site_diary_entries.site_id` -> CASCADE).

    Satir alti tablolari (`site_diary_lines`, `site_diary_worker_counts`) AYRICA
    sorulmaz: ikisi de gunluk kaydina baglidir ve gunluksuz var olamazlar.
    """
    result = await session.execute(
        select(select(SiteDiaryEntry.id).where(SiteDiaryEntry.site_id == site_id).exists())
    )
    return bool(result.scalar_one())


class DiaryUsage(NamedTuple):
    """Silme korkuluğunun metni için günlük kullanım özeti (PLN-B2.10)."""

    entry_count: int
    row_count: int
    first_date: date


async def section_has_diary_lines(
    session: AsyncSession, section_id: uuid.UUID
) -> DiaryUsage | None:
    """Bölüme yazılmış günlük MİKTAR satırı var mı (`site_diary_lines.section_id`
    -> RESTRICT, PLN-B2.1). Varsa kaç günlükte kaç satır + ilk tarih; yoksa `None`.

    DB zaten korur (RESTRICT ikinci katmandır); korkuluk olmadan kullanıcı opak
    "Veri bütünlüğü hatası" görürdü. TEK toplu sorgu. Başlık etiketi
    (`site_diary_entries.section_id`, SET NULL) SAYILMAZ: o bir bilgi alanıdır,
    bölümle birlikte gitmesi veri kaybı değildir.
    """
    stmt = (
        select(
            func.count(func.distinct(SiteDiaryLine.entry_id)),
            func.count(SiteDiaryLine.id),
            func.min(SiteDiaryEntry.entry_date),
        )
        .join(SiteDiaryEntry, SiteDiaryEntry.id == SiteDiaryLine.entry_id)
        .where(SiteDiaryLine.section_id == section_id)
    )
    entry_count, row_count, first_date = (await session.execute(stmt)).one()
    if not row_count:
        return None
    return DiaryUsage(int(entry_count), int(row_count), first_date)


async def site_has_documents(session: AsyncSession, site_id: uuid.UUID) -> bool:
    """Santiyede belge **VEYA** klasor var mi — ikisi de `sites.id`'ye CASCADE.

    `site_has_boq`nun grup dali dersinin birebiri: BOS KLASOR de tek basina
    engeldir, yoksa klasorleri olan bir santiyenin silinmesi o agaci sessizce
    yok ederdi. Tek `SELECT`te `OR`'lanir.
    """
    result = await session.execute(
        select(
            or_(
                select(Document.id).where(Document.site_id == site_id).exists(),
                select(DocumentFolder.id).where(DocumentFolder.site_id == site_id).exists(),
            )
        )
    )
    return bool(result.scalar_one())


async def site_has_plan(session: AsyncSession, site_id: uuid.UUID) -> bool:
    """Santiyede plan izgarasi / hedefi / AKTIF sprinti var mi — uc tablo da CASCADE.

    Ucu birden sorulur: satiri olmayan ama hedefi ya da sprinti olan bir santiye
    de planlama gecmisi tasir. `site_plan_cells` AYRICA sorulmaz — hucre satira
    baglidir, satirsiz var olamaz.

    🔴 SPRINT dalinda `is_active` SUZGECI ZORUNLUDUR — olculdu: `write.py:274-304
    save_sprint` sprint satirini ASLA SILMEZ, yalniz `is_active`i false'a ceker
    ("Kayit SILINMEZ" docstring'i) ve sprint silen BASKA bir uc YOKTUR
    (`openapi.json`: `/sites/{site_id}/plan/sprint` yalniz PUT). Suzgec olmasa
    bir kez sprint adi yazilip sonra seridi bosaltilan santiye BIR DAHA ASLA
    silinemezdi ve hata metni ("once plani temizleyin") UI'da GORUNMEYEN bir
    satiri isaret ettigi icin eyleme DONUK OLMAZDI. Kapali sprint arsivlenmis
    bir ETIKETTIR; izgara satiri, hucre ya da hedef tasimaz.
    """
    result = await session.execute(
        select(
            or_(
                select(SitePlanRow.id).where(SitePlanRow.site_id == site_id).exists(),
                select(SitePlanGoal.id).where(SitePlanGoal.site_id == site_id).exists(),
                select(SitePlanSprint.id)
                .where(SitePlanSprint.site_id == site_id, SitePlanSprint.is_active)
                .exists(),
            )
        )
    )
    return bool(result.scalar_one())


async def list_sites_by_slug(
    session: AsyncSession, project_ids: Sequence[uuid.UUID], slug: str
) -> list[Site]:
    """URL-2 — verilen PROJELERDEKI slug esleseni santiyeler.

    Gorunurluk suzgeci BURADA UYGULANMAZ (repository kanonu): cagiran taraf
    `project_ids`i ZATEN gorunur kumeden turetir. Bos liste bos sonuc doner —
    `IN ()` yazilmaz, SQLAlchemy bunu `false`a cevirir.
    """
    if not project_ids:
        return []
    result = await session.execute(
        select(Site)
        .where(Site.project_id.in_(project_ids), Site.slug == slug)
        .order_by(Site.project_id, Site.id)
    )
    return list(result.scalars().all())


async def list_site_ids_for_projects(
    session: AsyncSession, project_ids: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    """URL-2 — kapsam daraltma icin santiye kimlikleri (tam satir YUKLENMEZ)."""
    if not project_ids:
        return []
    result = await session.execute(select(Site.id).where(Site.project_id.in_(project_ids)))
    return list(result.scalars().all())


async def list_sections_by_slug(
    session: AsyncSession, site_ids: Sequence[uuid.UUID], slug: str
) -> list[Section]:
    """URL-2 — verilen SANTIYELERDEKI slug esleseni bolumler (`list_sites_by_slug` ikizi)."""
    if not site_ids:
        return []
    result = await session.execute(
        select(Section)
        .where(Section.site_id.in_(site_ids), Section.slug == slug)
        .order_by(Section.site_id, Section.id)
    )
    return list(result.scalars().all())

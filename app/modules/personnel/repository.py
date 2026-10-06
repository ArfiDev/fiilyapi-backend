"""Personel veri erişimi — `customers/repository.py` + `users/repository.py`

(sayfalama) desenlerinin birleşimi.

**`visible_projects` süzgeci YOKTUR ama `?project_id=` süzgeci VARDIR** (İK-1 spec
§5 K4): `personnel` yine şirket-geneli bir İK varlığıdır ve tüm projelerde görünür;
İK-1 ile `assigned_project_id` ATAMA kolonu açıldığından `project_id` bir
DARALTMA süzgecidir (yetki genişletmez). Puantaj diliminin "proje süzgeci
eklenmesin" notu atama kolonu YOKKEN geçerliydi; §5 K4 kararı bunu güncelledi —
kolon açıldı, `?project_id=` meşru. IDOR unutulmuş DEĞİLDİR: süzgeç bir yetki
kapısı değildir, erişim yine `personnel` izin seviyesiyle (router kapıları)
denetlenir.
"""

import uuid
from datetime import date

from sqlalchemy import Row, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.personnel.models import (
    LeaveBalance,
    LeaveRequest,
    LeaveStatus,
    LeaveType,
    Personnel,
    PersonnelDocument,
    PersonnelDocumentType,
)
from app.modules.projects.models import Project
from app.modules.site_diary.models import WorkerSource


def _filtreli(
    stmt: Select,
    q: str | None,
    source: WorkerSource | None,
    subcontractor_id: uuid.UUID | None,
    is_active: bool | None,
    project_id: uuid.UUID | None,
    is_draft: bool | None,
) -> Select:
    """Liste ve sayım AYNI süzgeçleri kullanır — `total` gösterilen listeyle uyuşsun."""
    if q:
        stmt = stmt.where(Personnel.full_name.ilike(f"%{q}%"))
    if source is not None:
        stmt = stmt.where(Personnel.source == source)
    if subcontractor_id is not None:
        stmt = stmt.where(Personnel.subcontractor_id == subcontractor_id)
    if is_active is not None:
        stmt = stmt.where(Personnel.is_active.is_(is_active))
    # İK-1 §5 K4: atama kolonuna göre DARALTMA (yetki genişletmez).
    if project_id is not None:
        stmt = stmt.where(Personnel.assigned_project_id == project_id)
    if is_draft is not None:
        stmt = stmt.where(Personnel.is_draft.is_(is_draft))
    return stmt


async def list_personnel(
    session: AsyncSession,
    q: str | None = None,
    source: WorkerSource | None = None,
    subcontractor_id: uuid.UUID | None = None,
    is_active: bool | None = None,
    project_id: uuid.UUID | None = None,
    is_draft: bool | None = None,
    limit: int | None = 50,
    offset: int = 0,
) -> list[Personnel]:
    """Arama YALNIZ `full_name` üzerindedir (spec §3) ve `ILIKE %q%` kısmi eşleşmedir.

    Sıralama DB'de (`ORDER BY full_name`) — sayfalama deterministik olsun.

    🔴 **`limit=None` EŞLEŞEN TÜM kayıtları döner** (`audit/repository.py`
    emsali, EXPORT-XLSX): Excel ucu ekranın 200'lük tavanıyla kırpılsaydı dosya
    SESSİZCE eksik olurdu — indiren kişi eksikliği göremez. Tavan LİSTE ucunun
    kendi imzasındadır (`Query(le=200)`) ve orada DEĞİŞMEZ.
    """
    stmt = _filtreli(
        select(Personnel), q, source, subcontractor_id, is_active, project_id, is_draft
    )
    stmt = stmt.order_by(Personnel.full_name).offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def count_personnel(
    session: AsyncSession,
    q: str | None = None,
    source: WorkerSource | None = None,
    subcontractor_id: uuid.UUID | None = None,
    is_active: bool | None = None,
    project_id: uuid.UUID | None = None,
    is_draft: bool | None = None,
) -> int:
    stmt = _filtreli(
        select(func.count()).select_from(Personnel),
        q,
        source,
        subcontractor_id,
        is_active,
        project_id,
        is_draft,
    )
    return (await session.execute(stmt)).scalar_one()


async def get_personnel(session: AsyncSession, personnel_ref: uuid.UUID | str) -> Personnel | None:
    """URL-4: kimlik YA DA slug ile okur. Slug GLOBAL tekildir (`uq_personnel_slug`).

    🔴 KVKK: slug YALNIZ `full_name`den türer. `tc_no` bu tablonun tek tekil
    anahtarıdır (`uq_personnel_tc_no`) ve URL'ye ASLA girmez — bu uç TCKN ile
    ARAMA YAPMAZ ve yapmamalıdır (kullanıcı kararı 2026-09-05).
    """
    if isinstance(personnel_ref, uuid.UUID):
        return await session.get(Personnel, personnel_ref)
    return await session.scalar(select(Personnel).where(Personnel.slug == personnel_ref))


async def list_personnel_by_user(session: AsyncSession, user_id: uuid.UUID) -> list[Personnel]:
    """`user_id` köprüsüne bağlı personel kayıtları — İK-2.1 self-servis çözümü.

    🔴 **Liste döner, tek kayıt DEĞİL — ve bu bilinçlidir.** `personnel.user_id`
    üzerinde UNIQUE kısıt YOKTUR (yalnız tekil OLMAYAN `ix_personnel_user_id`),
    yani iki kayıt aynı kullanıcıya bağlanabilir. `scalar_one_or_none()` bu hâlde
    ham `MultipleResultsFound` (=> 500) verirdi; `.first()` ise SESSİZCE bir kaydı
    seçip diğerini yutardı — belirsizlikte hangi personelin adına yazıldığı
    kullanıcıya sorulmadan kararlaştırılmış olurdu. Karar servise bırakılır ve
    orada FAIL-CLOSED'dır (409).

    `is_active`/`is_draft` süzgeci YOKTUR: süzgeç, iki kayıtlı belirsizliği
    sessizce "çözerek" yukarıdaki kararı by-pass ederdi.
    """
    stmt = select(Personnel).where(Personnel.user_id == user_id).order_by(Personnel.id)
    return list((await session.execute(stmt)).scalars().all())


async def get_personnel_by_tc_no(
    session: AsyncSession, tc_no: str, exclude_id: uuid.UUID | None = None
) -> Personnel | None:
    """DOLU TCKN'nin başka bir kayıtta olup olmadığı (`customers` pre-SELECT deseni).

    Servis bunu `IntegrityError`a düşmeden ÇAĞIRIR ki kullanıcıya alanına özel
    Türkçe 409 verilebilsin; `uq_personnel_tc_no` YARIŞ DURUMU emniyet ağıdır.
    """
    stmt = select(Personnel).where(Personnel.tc_no == tc_no)
    if exclude_id is not None:
        stmt = stmt.where(Personnel.id != exclude_id)
    return (await session.execute(stmt)).scalars().first()


async def add_personnel(session: AsyncSession, personnel: Personnel) -> Personnel:
    session.add(personnel)
    await session.flush()
    await session.refresh(personnel)
    return personnel


# --- İK-1 T3: belge alt-kaynağı --------------------------------------------


async def list_personnel_documents(
    session: AsyncSession, personnel_id: uuid.UUID
) -> list[Row[tuple[PersonnelDocument, PersonnelDocumentType | None]]]:
    """Bir personelin belgeleri + tip künyesi — TEK JOIN'li sorgu (N+1 YOK).

    `OUTER JOIN`: serbest etiketli kayıtta (`type_id IS NULL`) tip satırı yoktur,
    bu yüzden `LEFT JOIN` ile o kayıtlar da listede kalır ve tip sütunları None
    gelir. Belge başına ayrı bir tip sorgusu (N+1) AÇILMAZ — kanıt:
    `test_liste_tek_join_sorgusu` tip tablosuna ekstra SELECT atılmadığını sayar.

    Sıralama DB'dedir (`created_at`) — liste her yenilendiğinde aynı sırada gelsin.
    """
    stmt = (
        select(PersonnelDocument, PersonnelDocumentType)
        .outerjoin(
            PersonnelDocumentType,
            PersonnelDocument.type_id == PersonnelDocumentType.id,
        )
        .where(PersonnelDocument.personnel_id == personnel_id)
        .order_by(PersonnelDocument.created_at)
    )
    return list((await session.execute(stmt)).all())


async def get_personnel_document(
    session: AsyncSession, document_id: uuid.UUID
) -> PersonnelDocument | None:
    return await session.get(PersonnelDocument, document_id)


async def get_document_type(
    session: AsyncSession, type_id: uuid.UUID
) -> PersonnelDocumentType | None:
    return await session.get(PersonnelDocumentType, type_id)


async def add_personnel_document(
    session: AsyncSession, document: PersonnelDocument
) -> PersonnelDocument:
    session.add(document)
    await session.flush()
    await session.refresh(document)
    return document


# --- İK-1 T4: belge takibi özeti — AGGREGA sorgular (N+1 YOK, sabit sayı) ---
#
# Özet ucu (BT) SABİT SAYIDA sorgu kullanır (dashboard/progress_payments toplu
# çekim deseni): personel×tip döngüsünde per-row SELECT ATILMAZ. Aşağıdaki üç
# fonksiyon veri büyüklüğünden bağımsız 3 sorgu üretir; durum bucketleme +
# `missing` sayımı Python'da bu satırlar üzerinden yapılır (`status.py` tek
# kaynağı). Kanıt: `test_n_plus_1_sabit_sorgu` 2 vs 10 personelde aynı sayıyı ölçer.


async def list_document_types(session: AsyncSession) -> list[PersonnelDocumentType]:
    """Katalog tipleri (dağılım her tip için satır üretir) — `sort_order` sırasıyla."""
    stmt = select(PersonnelDocumentType).order_by(
        PersonnelDocumentType.sort_order, PersonnelDocumentType.name
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_active_published_personnel(session: AsyncSession) -> int:
    """AKTİF + YAYINDA personel sayısı — `missing` tabanı (spec §2/§3).

    Taslak (`is_draft=true`) ve pasif (`is_active=false`) personel SAYILMAZ:
    `missing` yalnız çalışan iş gücü için anlamlıdır.
    """
    stmt = (
        select(func.count())
        .select_from(Personnel)
        .where(Personnel.is_active.is_(True), Personnel.is_draft.is_(False))
    )
    return (await session.execute(stmt)).scalar_one()


async def list_active_published_document_rows(
    session: AsyncSession,
) -> list[Row[tuple]]:
    """AKTİF + YAYINDA personelin TÜM belgeleri + tip künyesi + proje adı — TEK sorgu.

    KPI (valid/expiring/expired), tip dağılımı kırılımı ve iki liste (süresi
    dolan/yaklaşan) hep bu tek çekimden Python'da türetilir; belge/tip/personel
    başına ek SELECT (N+1) YOKTUR.

    `INNER JOIN personnel` (+ WHERE aktif/yayın) taslak/pasif personelin
    belgelerini SQL'de eler — özet yalnız çalışan iş gücünü sayar. Tip ve proje
    `LEFT JOIN`'dir: serbest etiketli (`type_id NULL`) ya da projesi olmayan
    kayıtlar da listede kalır (tip/proje sütunları None gelir).
    """
    stmt = (
        select(
            PersonnelDocument.id,
            PersonnelDocument.personnel_id,
            PersonnelDocument.type_id,
            PersonnelDocument.free_label,
            PersonnelDocument.valid_until,
            Personnel.full_name,
            PersonnelDocumentType.name,
            PersonnelDocumentType.is_mandatory,
            PersonnelDocumentType.validity_months,
            Project.name,
            Personnel.assigned_project_id,
        )
        .join(Personnel, PersonnelDocument.personnel_id == Personnel.id)
        .outerjoin(
            PersonnelDocumentType,
            PersonnelDocument.type_id == PersonnelDocumentType.id,
        )
        .outerjoin(Project, Personnel.assigned_project_id == Project.id)
        .where(Personnel.is_active.is_(True), Personnel.is_draft.is_(False))
    )
    return list((await session.execute(stmt)).all())


# --- İK-2 T2: izin tipi kataloğu + izin talepleri ---------------------------


async def list_leave_types(session: AsyncSession, only_active: bool = True) -> list[LeaveType]:
    """Katalog tipleri — talep formunun listesi (SALT OKUMA, spec §1).

    Varsayılan yalnız AKTİF: form pasif bir tipi hiç ÖNERMEMELİ. Okuma yolu
    (`_resolve_leave_type_for_read`) pasif tipi yine getirir, çünkü eski kayıtlar
    doğru künyeyle görünmelidir — `list_document_types` ile aynı ayrım.
    """
    stmt = select(LeaveType)
    if only_active:
        stmt = stmt.where(LeaveType.is_active.is_(True))
    return list(
        (await session.execute(stmt.order_by(LeaveType.sort_order, LeaveType.name))).scalars().all()
    )


async def get_leave_type(session: AsyncSession, type_id: uuid.UUID) -> LeaveType | None:
    return await session.get(LeaveType, type_id)


def _leave_filtreli(
    stmt: Select,
    status: LeaveStatus | None,
    personnel_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
) -> Select:
    """Liste ve sayım AYNI süzgeçleri kullanır — `total` gösterilen listeyle uyuşsun.

    `project_id` PERSONELİN atandığı proje üzerindedir (`Personnel.assigned_project_id`);
    izin talebinin kendi proje kolonu YOKTUR ve açılmaz (iki gerçek kaynak doğardı).
    Bu bir DARALTMA süzgecidir, yetki genişletmez — kapsam yine `personnel` iznidir
    (liste ucundaki `?project_id=` ile aynı gerekçe, İK-1 §5 K4).
    """
    if status is not None:
        stmt = stmt.where(LeaveRequest.status == status)
    if personnel_id is not None:
        stmt = stmt.where(LeaveRequest.personnel_id == personnel_id)
    if project_id is not None:
        stmt = stmt.where(Personnel.assigned_project_id == project_id)
    return stmt


async def list_leave_requests(
    session: AsyncSession,
    status: LeaveStatus | None = None,
    personnel_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Row[tuple[LeaveRequest, Personnel, LeaveType]]]:
    """Talepler + personel ve tip künyesi — TEK JOIN'li sorgu (N+1 YOK).

    İki JOIN de INNER'dır: `personnel_id` ve `leave_type_id` NOT NULL FK'dir, yani
    her talebin tam bir personeli ve tipi VARDIR — LEFT JOIN yanlış bir iyimserlik
    (None künye) ihtimali uydururdu. Kanıt: `test_liste_n_plus_1_yok` künye
    tablolarına standalone SELECT gitmediğini sayar.

    Sıralama DB'dedir: en yeni talep önce (`created_at DESC`), eşitlikte `id` —
    sayfalama deterministik olsun (aynı `created_at` iki satırda tekrar edebilir).
    """
    stmt = _leave_filtreli(
        select(LeaveRequest, Personnel, LeaveType)
        .join(Personnel, LeaveRequest.personnel_id == Personnel.id)
        .join(LeaveType, LeaveRequest.leave_type_id == LeaveType.id),
        status,
        personnel_id,
        project_id,
    )
    stmt = (
        stmt.order_by(LeaveRequest.created_at.desc(), LeaveRequest.id).limit(limit).offset(offset)
    )
    return list((await session.execute(stmt)).all())


async def count_leave_requests(
    session: AsyncSession,
    status: LeaveStatus | None = None,
    personnel_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> int:
    stmt = _leave_filtreli(
        select(func.count())
        .select_from(LeaveRequest)
        .join(Personnel, LeaveRequest.personnel_id == Personnel.id),
        status,
        personnel_id,
        project_id,
    )
    return (await session.execute(stmt)).scalar_one()


async def get_leave_request(
    session: AsyncSession, request_id: uuid.UUID
) -> Row[tuple[LeaveRequest, Personnel, LeaveType]] | None:
    """Tek talep + künyesi — liste ile AYNI JOIN'i kullanır ki tek kayıt yanıtı
    listedeki satırdan farklı alan taşımasın."""
    stmt = (
        select(LeaveRequest, Personnel, LeaveType)
        .join(Personnel, LeaveRequest.personnel_id == Personnel.id)
        .join(LeaveType, LeaveRequest.leave_type_id == LeaveType.id)
        .where(LeaveRequest.id == request_id)
    )
    return (await session.execute(stmt)).first()


async def add_leave_request(session: AsyncSession, request: LeaveRequest) -> LeaveRequest:
    session.add(request)
    await session.flush()
    await session.refresh(request)
    return request


async def lock_personnel_for_update(session: AsyncSession, personnel_id: uuid.UUID) -> None:
    """İzin kararlarının SERİLEŞTİRME noktası (`documents.lock_folder_for_update`,
    `inventory.lock_warehouse_for_update` deseni).

    Onay kapısının HER İKİ denetimi de PERSONEL bazlıdır: çakışan onaylı izin (K3)
    ve kalan hak (K5 — hak + devreden - kullanılan). İkisi de "oku, karar ver,
    yaz" biçimindedir; kilitsiz koşarsa aynı personelin İKİ bekleyen talebi
    eşzamanlı onaylandığında her iki transaction da diğerinin HENÜZ COMMIT
    EDİLMEMİŞ onayını göremez — ikisi de eşiği geçer (hak aşılır) ve üst üste
    binen iki onaylı izin doğar.

    Kilit talebin DEĞİL personelin satırındadır: iki farklı talep satırını
    kilitlemek onları birbirinden HABERDAR ETMEZ; ortak kaynak personeldir.
    """
    await session.execute(
        select(Personnel.id).where(Personnel.id == personnel_id).with_for_update()
    )


async def get_leave_request_locked(
    session: AsyncSession, request_id: uuid.UUID
) -> LeaveRequest | None:
    """Karar geçişinin kilit satırı (`progress_payments.get_payment_locked` deseni).

    `populate_existing=True` ZORUNLUDUR: `session.get` kimlik haritasındaki ESKİ
    nesneyi döndürebilir ve o zaman kilit alınmış ama `status` KİLİTTEN ÖNCEKİ
    okumadan gelmiş olurdu — iki eşzamanlı karar isteğinden ikincisi birincinin
    commit'ini GÖREMEDEN `_assert_decidable`i geçer, karar damgasını (kim, ne
    zaman) sessizce EZERDİ.
    """
    return await session.get(LeaveRequest, request_id, with_for_update=True, populate_existing=True)


async def find_overlapping_approved_leave(
    session: AsyncSession,
    personnel_id: uuid.UUID,
    start_date: date,
    end_date: date,
    exclude_id: uuid.UUID | None = None,
) -> LeaveRequest | None:
    """Aynı personelin ÇAKIŞAN **onaylı** izni (spec §5 K3) — varsa ilk satır.

    Çakışma testi kapalı aralıklar üzerindedir: `mevcut.start <= yeni.end AND
    mevcut.end >= yeni.start`. Sınır bilinçlidir — 08'de biten iznin ardından
    09'da başlayan izin ÇAKIŞMAZ, ama 08'de başlayan ÇAKIŞIR (bir gün iki izne
    birden ait olamaz).

    YALNIZ `approved` sayılır: bekleyen talepler henüz bir taahhüt değildir ve
    ikisi birden reddedilebilir. `exclude_id` T3 içindir — onaylanmak istenen
    kaydın KENDİSİ (zaten onaylıysa, ör. yeniden değerlendirme) kendisiyle
    çakışmasın.

    T2'de HİÇBİR UÇ bunu 409'a çevirmez (spec §3: kural `approve`ta işler) —
    burada yalnız HAZIRLANIR ve `test_ik2_leave_service.py`de kanıtlanır.
    """
    stmt = select(LeaveRequest).where(
        LeaveRequest.personnel_id == personnel_id,
        LeaveRequest.status == LeaveStatus.approved,
        LeaveRequest.start_date <= end_date,
        LeaveRequest.end_date >= start_date,
    )
    if exclude_id is not None:
        stmt = stmt.where(LeaveRequest.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalars().first()


# --- İK-2 T3: bakiye satırı + kullanılan gün toplamı ------------------------


async def get_leave_balance(
    session: AsyncSession, personnel_id: uuid.UUID, year: int
) -> LeaveBalance | None:
    """O yılın bakiye satırı — YOKLUĞU meşrudur (`carried_over` sıfır demektir).

    `uq_leave_balances_personnel_year` gereği en fazla bir satır olabilir.
    """
    stmt = select(LeaveBalance).where(
        LeaveBalance.personnel_id == personnel_id, LeaveBalance.year == year
    )
    return (await session.execute(stmt)).scalars().first()


async def add_leave_balance(session: AsyncSession, balance: LeaveBalance) -> LeaveBalance:
    session.add(balance)
    await session.flush()
    await session.refresh(balance)
    return balance


async def sum_deductible_approved_days(
    session: AsyncSession, personnel_id: uuid.UUID, year: int
) -> int:
    """O yılın **kullanılan** günü: ONAYLI + yıllık haktan DÜŞEN izinlerin toplamı.

    Üç süzgeç de kuralın parçasıdır (spec §2):

    * `status == approved` — bekleyen talep henüz taahhüt değildir,
    * `LeaveType.deducts_from_annual` — hastalık/mazeret yıllık haktan DÜŞMEZ
      (İZ 87 "Rapor"), bu yüzden JOIN yapılır ve tip süzülür,
    * `start_date` o yılın içinde — talep BAŞLADIĞI yıla sayılır
      (`leave.leave_year` kararı; yıl sınırını aşan talep BÖLÜNMEZ).

    Yıl penceresi `BETWEEN` ile kurulur (`extract` DEĞİL): açık tarih aralığı
    `ix_leave_requests_personnel_range` indeksini kullanabilir, fonksiyon çağrısı
    kullanamazdı.

    `coalesce(..., 0)`: hiç satır yoksa SUM **NULL** döner. 🔴 NULL-eşik kanonunun
    burada okunuşu şudur — bu NULL "veri bilinmiyor" DEĞİL, "hiç izin kullanılmadı"
    demektir ve 0'a çevrilmesi DOĞRUDUR. Bilinmezlik `hire_date` tarafındadır ve
    orada None olarak KORUNUR (`leave.annual_entitlement`), toplamda değil.
    """
    stmt = (
        select(func.coalesce(func.sum(LeaveRequest.days), 0))
        .select_from(LeaveRequest)
        .join(LeaveType, LeaveRequest.leave_type_id == LeaveType.id)
        .where(
            LeaveRequest.personnel_id == personnel_id,
            *_deductible_approved_between(*year_window(year)),
        )
    )
    return (await session.execute(stmt)).scalar_one()


# --- İK-2 T4: izin özeti — AGGREGA sorgular (N+1 YOK, sabit sayı) -----------
#
# İZ özeti (5 KPI + bakiye tablosu) SABİT SAYIDA sorgu kullanır (İK-1 `/hr/
# documents/summary` emsali): personel döngüsünde per-row SELECT ATILMAZ.
# Aşağıdaki beş fonksiyon veri büyüklüğünden bağımsız beş sorgu üretir; türevler
# (hak/kalan/yüzde) Python'da `leave.py` TEK KAYNAĞINDAN hesaplanır.
# Kanıt: `test_n_plus_1_sabit_sorgu` 2 vs 10 personelde aynı sayıyı ölçer.


def year_window(year: int) -> tuple[date, date]:
    """Bakiye yılının açık tarih penceresi — `extract` DEĞİL `BETWEEN` için."""
    return date(year, 1, 1), date(year, 12, 31)


def _deductible_approved_between(start: date, end: date) -> tuple:
    """**`kullanılan` gün kuralının TEK KAYNAĞI** (spec §2) — pencere parametrik.

    Üç süzgeç de kuralın parçasıdır: `approved` (bekleyen taahhüt değildir) ·
    `deducts_from_annual` (İZ 87 "Rapor" yıllık haktan düşmez) · izin BAŞLADIĞI
    pencereye yazılır (`leave.leave_year` kararı, bölme YOK).

    Yıl toplamı, personel-bazlı toplam ve İZ 48'in "Bu Ay Kullanılan" KPI'ı
    AYNI predikatı çağırır: pencere değişir, KURAL DEĞİŞMEZ. Kopyalansaydı ay
    KPI'ı ile tablo sütunu sessizce ayrışırdı (biri raporu sayar, öteki saymaz).
    """
    return (
        LeaveRequest.status == LeaveStatus.approved,
        LeaveType.deducts_from_annual.is_(True),
        LeaveRequest.start_date.between(start, end),
    )


def _active_published() -> tuple:
    """Özetin personel kapsamı: AKTİF + YAYINDA (İK-1 özet kanonu).

    Taslak (henüz yayınlanmamış) ve pasif (ayrılmış) personel hiçbir KPI'ya ve
    bakiye satırına girmez — ekran ÇALIŞAN iş gücünün izin durumunu gösterir.
    """
    return (Personnel.is_active.is_(True), Personnel.is_draft.is_(False))


async def count_pending_leave_requests(session: AsyncSession) -> int:
    """İZ 46 "Bekleyen Talep": `pending` talep sayısı — TİPTEN BAĞIMSIZ.

    Sayaç onay kuyruğunun boyudur (İZ 56 "Onay Bekleyen İzin Talepleri"); hastalık
    talebi de onay bekler, `deducts_from_annual` süzgeci BURAYA girmez.
    """
    stmt = (
        select(func.count())
        .select_from(LeaveRequest)
        .join(Personnel, LeaveRequest.personnel_id == Personnel.id)
        .where(LeaveRequest.status == LeaveStatus.pending, *_active_published())
    )
    return (await session.execute(stmt)).scalar_one()


async def count_personnel_on_leave(session: AsyncSession, today: date) -> int:
    """İZ 47 "Bugün İzinli": bugünü KAPSAYAN onaylı izni olan TEKİL personel.

    `count(distinct personnel_id)`: aynı kişinin iki onaylı kaydı (örneğin ardışık
    yıllık + rapor) kişiyi iki kez saydırmaz — KPI birim "kişi"dir, "talep" değil.
    Tip süzgeci YOKTUR: raporlu personel de bugün işbaşında değildir.
    """
    stmt = (
        select(func.count(func.distinct(LeaveRequest.personnel_id)))
        .select_from(LeaveRequest)
        .join(Personnel, LeaveRequest.personnel_id == Personnel.id)
        .where(
            LeaveRequest.status == LeaveStatus.approved,
            LeaveRequest.start_date <= today,
            LeaveRequest.end_date >= today,
            *_active_published(),
        )
    )
    return (await session.execute(stmt)).scalar_one()


async def sum_deductible_approved_days_between(
    session: AsyncSession, start: date, end: date
) -> int:
    """İZ 48 "Bu Ay Kullanılan": verilen pencerede BAŞLAYAN kullanılan gün toplamı.

    `sum_deductible_approved_days` ile aynı kuralın şirket-geneli hâli (personel
    süzgeci yok, pencere ay). Ay sınırını aşan izin BÖLÜNMEZ — başladığı aya
    yazılır; bölme, tablo sütunuyla KPI'ı ayrıştırırdı.
    """
    stmt = (
        select(func.coalesce(func.sum(LeaveRequest.days), 0))
        .select_from(LeaveRequest)
        .join(LeaveType, LeaveRequest.leave_type_id == LeaveType.id)
        .join(Personnel, LeaveRequest.personnel_id == Personnel.id)
        .where(*_deductible_approved_between(start, end), *_active_published())
    )
    return (await session.execute(stmt)).scalar_one()


async def sum_deductible_approved_days_by_personnel(
    session: AsyncSession, year: int
) -> dict[uuid.UUID, int]:
    """Personel → o yılın kullanılan günü — **TEK group-by** (N+1'in kapısı).

    `sum_deductible_approved_days`i kişi başına çağırmak 50 satırlık tabloda 50
    SELECT ederdi; kural aynı predikattan gelir, yalnız gruplanır. Sözlükte
    OLMAYAN personel 0 kullanmıştır (`coalesce` yerine `dict.get(pid, 0)`).
    """
    stmt = (
        select(LeaveRequest.personnel_id, func.sum(LeaveRequest.days))
        .select_from(LeaveRequest)
        .join(LeaveType, LeaveRequest.leave_type_id == LeaveType.id)
        .where(*_deductible_approved_between(*year_window(year)))
        .group_by(LeaveRequest.personnel_id)
    )
    return {pid: int(total) for pid, total in (await session.execute(stmt)).all()}


async def list_active_published_personnel_with_balance(
    session: AsyncSession, year: int
) -> list[Row[tuple]]:
    """İZ bakiye tablosunun tabanı: aktif+yayın personel + O YILIN devredeni — TEK sorgu.

    `LEFT JOIN` bilinçlidir: bakiye satırı olmayan personel de tabloda GÖRÜNÜR
    (devreden 0). `INNER JOIN` olsaydı tablo yalnız elle devreden girilmiş
    kişileri gösterir, yeni personel ekrandan silinirdi.

    Yıl koşulu WHERE'de değil JOIN ON'da durur: WHERE'e taşınırsa `NULL` satırlar
    elenir ve LEFT JOIN sessizce INNER JOIN'e döner.
    """
    stmt = (
        select(Personnel, LeaveBalance.carried_over)
        .outerjoin(
            LeaveBalance,
            (LeaveBalance.personnel_id == Personnel.id) & (LeaveBalance.year == year),
        )
        .where(*_active_published())
        .order_by(Personnel.full_name)
    )
    return list((await session.execute(stmt)).all())

"""Personel KARTOTEKSI (puantaj spec §3 · IK-1 spec §5): okuma + create/update.

**Silme ucu YOK** — `timesheet_entries.personnel_id` FK'si RESTRICT'tir.
Kartoteksten cikarma `is_active=false` PATCH'idir.

Paketin YAPRAK katmani: butun oteki dosyalar buradaki `get_personnel`i okur."""

import uuid
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.errors import (
    DuplicateError,
    NotFoundError,
    PersonnelValidationError,
)
from app.core.page_gate import gate_ok
from app.core.slug import allocate_slug
from app.modules.personnel import guards, repository
from app.modules.personnel.models import (
    Personnel,
)
from app.modules.personnel.schemas import (
    PersonnelCreate,
    PersonnelUpdate,
)
from app.modules.projects import repository as projects_repository
from app.modules.sites import repository as sites_repository
from app.modules.users.models import User

PERMISSION_MODULE = "personnel"

# Yeni kart kolonları — tek yerde durur ki create/patch ORM ataması KOPYALANMASIN.
_CARD_FIELDS: tuple[str, ...] = (
    "tc_no",
    "birth_date",
    "gender",
    "marital_status",
    "phone",
    "email",
    "address",
    "emergency_contact_name",
    "emergency_contact_phone",
    "hire_date",
    "wage_type",
    "wage_amount",
    "payment_method",
    "iban",
    "sgk_no",
    "assigned_project_id",
    "assigned_section_id",
)


async def get_personnel(session: AsyncSession, personnel_ref: uuid.UUID | str) -> Personnel:
    personnel = await repository.get_personnel(session, personnel_ref)
    if personnel is None:
        raise NotFoundError(guards.PERSONNEL_MISSING)
    return personnel


async def _validate_tckn(
    session: AsyncSession, tc_no: str | None, exclude_id: uuid.UUID | None = None
) -> None:
    """DOLU TCKN: checksum (422) + benzersizlik (409). Boş/NULL → ATLANIR (taslak serbest)."""
    if not tc_no:
        return
    guards.validate_tckn(tc_no)
    if await repository.get_personnel_by_tc_no(session, tc_no, exclude_id):
        raise DuplicateError(guards.DUPLICATE_TCKN)


async def _validate_assignment_scope(
    session: AsyncSession,
    project_id: uuid.UUID | None,
    section_id: uuid.UUID | None,
) -> None:
    """Atama alanları BİRLEŞİK değerler üzerinde doğrulanır (spec §5 K4).

    * proje verilmiş ve YOK → 404 (gövde içi varlık ref);
    * bölüm verilmiş ama proje yok → 422 (bölüm projesiz olamaz);
    * bölüm verilmiş ve YOK → 404;
    * bölüm o projeye ait DEĞİL → 422 (`documents.SITE_NOT_IN_PROJECT` deseni).
    """
    if project_id is not None:
        project = await projects_repository.get_project(session, project_id)
        if project is None:
            raise NotFoundError(guards.PROJECT_NOT_FOUND)
    if section_id is not None:
        if project_id is None:
            raise PersonnelValidationError(guards.SECTION_REQUIRES_PROJECT)
        section = await sites_repository.get_section(session, section_id)
        if section is None:
            raise NotFoundError(guards.SECTION_NOT_FOUND)
        site = await sites_repository.get_site(session, section.site_id)
        if site is None or site.project_id != project_id:
            raise PersonnelValidationError(guards.SECTION_NOT_IN_PROJECT)


def _assert_publish_ready(merged: object) -> None:
    """Yayın (`is_draft=false`) için PE ✱ kümesi TAM olmalı — eksikse 422."""
    eksik = guards.missing_publish_fields(merged)
    if eksik:
        raise PersonnelValidationError(guards.PUBLISH_MISSING.format(", ".join(eksik)))


async def create_personnel(session: AsyncSession, data: PersonnelCreate) -> Personnel:
    guards.validate_personnel_source(data.source, data.subcontractor_id)
    await _validate_tckn(session, data.tc_no)
    await _validate_assignment_scope(session, data.assigned_project_id, data.assigned_section_id)
    if not data.is_draft:
        _assert_publish_ready(data)

    personnel = Personnel(
        full_name=data.full_name,
        trade=data.trade,
        source=data.source,
        subcontractor_id=data.subcontractor_id,
        user_id=data.user_id,
        is_active=data.is_active,
        is_draft=data.is_draft,
        **{field: getattr(data, field) for field in _CARD_FIELDS},
    )
    # URL-4: slug OLUŞTURULURKEN üretilir, ad değişince DEĞİŞMEZ (URL-2 kararı
    # 4) — bu yüzden `update_personnel` slug'a DOKUNMAZ. Aynı adlı ikinci
    # personel `-2` eki alır (`unique_slug`).
    # 🔴 KVKK: tabana YALNIZ `full_name` girer.
    personnel.slug = await allocate_slug(session, data.full_name, Personnel.slug)
    return await repository.add_personnel(session, personnel)


async def update_personnel(
    session: AsyncSession, personnel_id: uuid.UUID, data: PersonnelUpdate
) -> Personnel:
    """Kısmi güncelleme (`model_dump(exclude_unset=True)`) — gönderilmeyen alan değişmez.

    Tüm kurallar BİRLEŞİK kayıt üzerinde koşar (`customers`/`sites` P6 deseni):
    gövdedeki değerler DB'dekilerin üstüne bindirilir, sonra doğrulanır. Yalnız
    gövdeye bakmak yayın kaydını eksik alana düşürür ya da `subcontractor -> company`
    geçişinde eski taşeron bağını kayıtta bırakırdı.
    """
    personnel = await get_personnel(session, personnel_id)
    updates = data.model_dump(exclude_unset=True)

    efektif_kaynak = updates.get("source", personnel.source)
    efektif_taseron = updates.get("subcontractor_id", personnel.subcontractor_id)
    guards.validate_personnel_source(efektif_kaynak, efektif_taseron)

    # TCKN yalnız GÖNDERİLDİĞİNDE doğrulanır; benzersizlikte kendini hariç tut.
    if "tc_no" in updates:
        await _validate_tckn(session, updates["tc_no"], exclude_id=personnel.id)

    # Atama alanları yalnız biri bile değiştiyse birleşik değerlerle doğrulanır.
    if "assigned_project_id" in updates or "assigned_section_id" in updates:
        efektif_proje = updates.get("assigned_project_id", personnel.assigned_project_id)
        efektif_bolum = updates.get("assigned_section_id", personnel.assigned_section_id)
        await _validate_assignment_scope(session, efektif_proje, efektif_bolum)

    # Yayın zorunluluğu: sonuçta yayın olacaksa (zaten yayın ya da bu PATCH yayına
    # çeviriyorsa) birleşik kayıt TAM olmalı — aksi hâlde 422 ve satır YAZILMAZ.
    efektif_taslak = updates.get("is_draft", personnel.is_draft)
    if not efektif_taslak:
        merged = SimpleNamespace(
            **{
                attr: updates.get(attr, getattr(personnel, attr))
                for attr, _ in guards.PUBLISH_REQUIRED_FIELDS
            }
        )
        _assert_publish_ready(merged)

    for field, value in updates.items():
        setattr(personnel, field, value)
    await session.flush()
    await session.refresh(personnel)
    return personnel


# --- Aktör ↔ personel bağı: TEK YAZIM (İK-2 silme · OK-1A T5 onay) ---------


async def has_personnel_admin(session: AsyncSession, actor: User) -> bool:
    """Aktörün `personnel` modülünde `admin` seviyesi var mı?

    İKİ kural bunu okur ve ikisi de AYNI kapıyı kasteder: izin talebi SİLME
    istisnası (İK-2 spec §3) ve KENDİ izin talebini ONAYLAMA istisnası (OK-1A
    T5). Ayrı yazılsalardı biri gün gelip `full`e gevşer, öteki `admin`de kalırdı.

    🔴 `full` YETMEZ (`app/core/access.py`: "full silmeyi KAPSAMAZ"). `patron`
    sistem rolü `personnel=full`dur; istisna ona da açılsaydı "tek kişilik ekipte
    kilitlenmeyi önle" gerekçesi, kendi talebini onaylayan ikinci bir SINIFA
    dönüşürdü. Aynı karar `approvals/service.py::_has_document_admin`te de var.

    Seviye router kapısında DEĞİL burada okunur: router tek bir asgari seviye
    zorlar, oysa buradaki kurallar İKİ ayrı yoldan (seviye VEYA sahiplik) açılıp
    kapanır.
    """
    return await gate_ok(session, actor, PERMISSION_MODULE, AccessLevel.admin)


def is_own_personnel_record(personnel: Personnel, actor: User) -> bool:
    """Personel kaydı AKTÖRÜN kendisi mi — `Personnel.user_id` köprüsünden.

    🔴 ÖLÇÜLDÜ (`psql \\d personnel`): `user_id` **NULLABLE**dır (NOT NULL kısıtı yok)
    ve **TEKİL DEĞİLDİR** (yalnız `ix_personnel_user_id`, UNIQUE değil). Açık
    NULL denetimi bu yüzden ZORUNLUDUR: NULL'ı "eşleşti" saymak, login'i olmayan
    TÜM saha personelini aktörün kendisi sayardı.

    Tekillik BU YÖNDE sorun değildir — bir kaydın TEK `user_id`si vardır.
    Belirsizlik ters yöndedir (kullanıcıdan personele) ve İK-2.1 orada zaten
    FAIL-CLOSED 409 döner (`resolve_self_personnel`).
    """
    return personnel.user_id is not None and personnel.user_id == actor.id


# İki liste tavanı (spec §3): en kritik 50 satır gösterilir, gerisi ekranı boğmaz.
SUMMARY_LIST_LIMIT = 50

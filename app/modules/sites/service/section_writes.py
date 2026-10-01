"""Bolum yazma uclari: `create_section` (POST) + `update_section` (PATCH)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    DuplicateError,
    SiteValidationError,
)
from app.core.slug import allocate_slug

# Denetim METINLERI merkezidir (`audit/messages.py`): f-string ne servise ne
# router'a gomulur. Silme ve yayin metinleri BURADA kurulur, cunku gereken
# baglam (silinmeden onceki ad, `is_draft`in ONCEKI degeri) yalniz servis
# katmaninda vardir.
from app.modules.audit import messages
from app.modules.sites import guards, repository
from app.modules.sites.models import Section, SectionMilestone
from app.modules.sites.schemas import (
    SectionCreate,
    SectionMilestoneInput,
    SectionUpdate,
)
from app.modules.sites.service.codes import _next_section_code
from app.modules.sites.service.visibility import _visible_section, _visible_site
from app.modules.sites.service.writes_common import _merged_for_validation, _resolve_user_name
from app.modules.users.models import User

# Bolumun IKI sorumlu alani ve ad anlik goruntuleri. FK -> ad esleme TEK yerde
# durur; POST (T3) ve PATCH (T2) bunu KOPYALAMAZ, PAYLASIR — iki kopya zamanla
# ayrisir ve ayrisan taraf, adi FK'sindan farkli bir kayit uretir.
_SECTION_MANAGER_FIELDS = (
    ("manager_user_id", "manager_name"),
    ("deputy_manager_user_id", "deputy_manager_name"),
)


async def _resolved_manager_names(session: AsyncSession, values: dict) -> dict[str, str]:
    """Verilen govdedeki sorumlu FK'lerinin ad anlik goruntulerini cozer.

    Kosul `is not None`dir: FK'yi acikca NULL'lamak ad anlik goruntusunu SILMEZ
    (kullanici silinse bile evrakta kalmasiyla ayni gerekce). Cozum 422
    (`Seçilen kullanıcı bulunamadı`) uretebildigi icin cagiran taraf bunu HER
    ZAMAN ilk `session.add`den ONCE calistirir; gecersiz kullanici hicbir alani
    degistirmemelidir. Izinli (`on_leave`) personel atanabilir, pasif olan 422 —
    gerekcesi `repository.get_assignable_user` docstring'inde.
    """
    return {
        name_field: await _resolve_user_name(session, values[fk_field])
        for fk_field, name_field in _SECTION_MANAGER_FIELDS
        if values.get(fk_field) is not None
    }


async def _assert_section_type_exists(session: AsyncSession, type_id: uuid.UUID | None) -> None:
    """BLF-B1 — govdedeki `section_type_id` var mi? Yoksa 422 (404 DEGIL: tip burada bir
    ALAN DEGERIDIR). `None` gecer: tip zorunlulugu `guards.validate_section`indadir ve
    taslakta kosmaz. Her yazmadan ONCE kosar — gecersiz tip hicbir alani degistirmez."""
    if type_id is not None and await repository.get_section_type(session, type_id) is None:
        raise SiteValidationError(guards.SECTION_TYPE_MISSING)


async def _validate_dependency(
    session: AsyncSession,
    site_id: uuid.UUID,
    candidate_id: uuid.UUID | None,
    *,
    section_id: uuid.UUID | None,
) -> None:
    """P11 §3 — oncul bolum korkulugu. TARIH KISITI YOKTUR (kullanici karari S3).

    Uc kural, bu sirayla:

    1. **Self** — bolum kendisine baglanamaz (`section_id` POST'ta `None`dir:
       henuz var olmayan bir satirin kendisi de olamaz).
    2. **Ayni SANTIYE** — oncul baska santiyedeyse ya da hic yoksa AYNI 422
       (`guards.DEPENDS_NOT_IN_SITE` gerekcesi orada yazili).
    3. **Dongu** — zincir YURUYEREK aranir: oncul -> onun onculu -> ... Bir
       adimda guncellenen bolume geri donuluyorsa halka kapanir. `visited`
       kumesi, VERIDE ONCEDEN var olan (bu istekle ilgisi olmayan) bir halkada
       sonsuz donguye girmeyi engeller.

    Bag YALNIZ BILGIDIR: "oncul bitmeden basladi" diye 422 URETILMEZ — mockup'ta
    boyle bir kural yoktur ve icat edilmez.
    """
    if candidate_id is None:
        return
    if section_id is not None and candidate_id == section_id:
        raise SiteValidationError(guards.DEPENDS_SELF)
    predecessor = await repository.get_section_in_site(session, site_id, candidate_id)
    if predecessor is None:
        raise SiteValidationError(guards.DEPENDS_NOT_IN_SITE)
    if section_id is None:
        # Yeni bolumun kimligi henuz yok: hicbir mevcut satir ona bagli olamaz,
        # dolayisiyla POST bir halka KAPATAMAZ.
        return
    visited: set[uuid.UUID] = set()
    cursor: Section | None = predecessor
    while cursor is not None:
        if cursor.id == section_id:
            raise SiteValidationError(guards.DEPENDS_CYCLE)
        if cursor.id in visited:
            return
        visited.add(cursor.id)
        next_id = cursor.depends_on_section_id
        cursor = None if next_id is None else await repository.get_section(session, next_id)


def _merge_milestones(section: Section, inputs: list[SectionMilestoneInput]) -> None:
    """Kilometre taslarini KIMLIK KORUYARAK birlestirir (P9 `_merge_shareholders`
    emsali, spec §3).

    * id eslesen satir YERINDE guncellenir — birincil anahtar YASAR;
    * id'siz girdi YENI satirdir;
    * listede olmayan mevcut satir DUSER (`delete-orphan`);
    * bilinmeyen ya da BASKA bolume ait id 422'dir — sessizce yeni satira DONMEZ;
    * ayni id iki kez gelirse 422 (P9 T5 dersi: sessiz cokme).

    `sort_order` GOVDEDEN GELMEZ, dizideki siradan atanir (`_write_sections`
    deseni): tek bir sira kaynagi olur, iki kaynak celisemez.

    Dogrulamalarin TAMAMI hicbir satir degistirilmeden ONCE kosar: yarim
    uygulanmis bir liste sessiz veri hatasidir.
    """
    existing = {row.id: row for row in section.milestones}
    sent_ids = [item.id for item in inputs if item.id is not None]
    kept_ids = set(sent_ids)
    if len(sent_ids) != len(kept_ids):
        raise SiteValidationError(guards.MILESTONE_DUPLICATE_IN_PAYLOAD)
    if kept_ids - set(existing):
        # Baska bolumun (ve var olmayan) satiri da buraya duser: id bu bolumde
        # YOKTUR, nerede oldugu bu ucun konusu degildir.
        raise SiteValidationError(guards.MILESTONE_UNKNOWN)

    merged: list[SectionMilestone] = []
    for index, item in enumerate(inputs):
        if item.id is None:
            merged.append(
                SectionMilestone(
                    title=item.title, milestone_date=item.milestone_date, sort_order=index
                )
            )
            continue
        row = existing[item.id]
        row.title = item.title
        row.milestone_date = item.milestone_date
        row.sort_order = index
        merged.append(row)
    # Listede kalanlar AYNI nesnelerdir: delete-orphan yalnizca dusenleri siler.
    section.milestones = merged


async def create_section(
    session: AsyncSession, actor: User, site_id: uuid.UUID, data: SectionCreate
) -> Section:
    """P6 §5 — `Form - Bolum Ekle`. Sira `create_site`in adimlarinin aynisidir:
    gorunurluk -> dogrulama -> kullanici cozumu -> kod -> YAZMA.

    422 ureten her adim ilk `session.add`den ONCE biter (§8.2): eksik alanli ya
    da pasif kullanicili bir istek YARIM bir bolum satiri birakmaz.
    """
    site, _ = await _visible_site(session, actor, site_id)
    # Taslak-farkindalikli dogrulama (kalici karar 4): "Taslak Kaydet" (Form 242)
    # zorunlulugu kaldirir, TUTARLILIGI kaldirmaz.
    guards.validate_section(data, is_draft=data.is_draft)
    await _assert_section_type_exists(session, data.section_type_id)
    # FK verilmisse ad govdedeki serbest metnin UZERINE yazilir (create_site ile
    # ayni kural): ad FK'nin turevidir, ikinci bir gercek kaynak degildir.
    names = {"manager_name": data.manager_name, "deputy_manager_name": data.deputy_manager_name}
    names.update(await _resolved_manager_names(session, data.model_dump()))
    # Kod uretimi (bossa) + cakisma on-kontrolu -> 409 alanina ozel Turkce mesajla;
    # santiye kodununkiyle AYNI desen, yeni bir desen icat edilmez.
    code = data.code or await _next_section_code(session, site.id)
    if await repository.get_section_by_code(session, site.id, code) is not None:
        raise DuplicateError(guards.DUPLICATE_SECTION_CODE)
    # P11 — oncul korkulugu YAZMADAN ONCE (`section_id=None`: yeni satirin
    # kimligi henuz yok, dolayisiyla self/dongu dallari POST'ta tanimsizdir).
    await _validate_dependency(session, site.id, data.depends_on_section_id, section_id=None)
    # URL-2: slug OLUSTURULURKEN uretilir, AD DEGISINCE DEGISMEZ (kullanici
    # karari 2026-08-29) — `update_section` slug'a HIC dokunmaz. Kapsam SANTIYE
    # ICIDIR (`uq_sections_site_slug`), bu yuzden suzgec `site_id`dir.
    slug = await allocate_slug(session, data.name, Section.slug, Section.site_id == site.id)
    section = Section(
        site_id=site.id,
        code=code,
        slug=slug,
        name=data.name,
        status=data.status,
        manager_user_id=data.manager_user_id,
        start_date=data.start_date,
        end_date=data.end_date,
        sort_order=data.sort_order,
        # --- P6 · T3: `Form - Bolum Ekle` alanlari ---
        section_type_id=data.section_type_id,
        description=data.description,
        deputy_manager_user_id=data.deputy_manager_user_id,
        planned_worker_count=data.planned_worker_count,
        is_draft=data.is_draft,
        depends_on_section_id=data.depends_on_section_id,
        **names,
    )
    # Milestone birlestirmesi transient nesne uzerinde kosar: POST'ta mevcut satir
    # YOKTUR, dolayisiyla id TASIYAN her girdi `MILESTONE_UNKNOWN` alir —
    # guncellenecek satir henuz var olmadigi icin bu dogru cevaptir. PATCH ile
    # AYNI fonksiyon kullanilir, ikinci bir kopya kural yazilmaz.
    _merge_milestones(section, data.milestones)
    session.add(section)
    await session.flush()
    await session.refresh(section)
    return section


# `guards.validate_section`in okudugu alanlar (`_SectionLike`) — santiyedeki
# `_VALIDATED_FIELDS`in bolum karsiligi. PATCH bunlarin BIRLESIK degerini kurar:
# gonderilen alan patch'ten, gonderilmeyen MEVCUT SATIRDAN gelir.
_SECTION_VALIDATED_FIELDS = (
    "section_type_id",
    "manager_user_id",
    "manager_name",
    "start_date",
    "end_date",
)


async def update_section(
    session: AsyncSession, actor: User, section_id: uuid.UUID, data: SectionUpdate
) -> tuple[Section, str]:
    """PATCH GEVSEK, YAYIN SIKI — `update_site`in dalinin BIREBIRI (P6 T5).

    Zorunluluk dogrulamasi duz PATCH'te KOSMAZ: kossaydi canlidaki eksik alanli
    eski bolumler duzenlenemez hale gelir, yalnizca adi degistirmek isteyen
    kullanici "Bölüm tipi seçiniz." duvarina carpardi. Tek istisna
    `is_draft: true -> false` gecisidir: orada BIRLESIK kayit (mevcut satir +
    patch) uzerinde tum kurallar kosar ve gecmezse satir TASLAK KALIR.

    Bu dal OLMADAN T3'un zorunluluklari YALNIZ POST'ta baglayici kalir, yani
    etkisizdir: `is_draft: true` ile eksik bolum acip `PATCH {"is_draft": false}`
    gondermek hepsini atlatirdi.

    Denetim metnini de DONER (`update_site` / `units.update_unit` deseni):
    yayina gecis olup olmadigi yalniz BURADA bilinir — router `is_draft`in
    ONCEKI degerini goremez, dolayisiyla ayrimi disariya tasimak "Bölüm
    güncellendi" ile "yayına alındı" satirlarini birbirine karistirirdi.
    """
    section, site = await _visible_section(session, actor, section_id)
    # `milestones` DISARIDA BIRAKILIR (`update_site`in `facilities` dali ile ayni
    # gerekce): liste ALT SATIRLARDIR, duz `setattr` ile iliskiye ham Pydantic
    # nesnesi yazmak ORM'i patlatir. Asagida acikca birlestirilir.
    changes = data.model_dump(exclude_unset=True, exclude={"milestones"})
    # `false -> false` bir gecis DEGILDIR ve zorunluluk kurallarini tetiklemez.
    is_publishing = section.is_draft and changes.get("is_draft") is False
    guards.validate_section(
        _merged_for_validation(section, changes, _SECTION_VALIDATED_FIELDS),
        is_draft=not is_publishing,
    )
    # BLF-B1.5 (F-c): yayindaki bolumde tipi acikca NULL'lamak yasak — PATCH'in TEK sikilan
    # zorunlulugu (sorumlu/tarihler gevsek KALIR). Ayni govdede `is_draft: true` ile taslaga
    # cekiliyorsa serbest; taslak bolumde null serbest. `publishing` zaten yukarida tam
    # dogrulamadan gecer.
    if (
        "section_type_id" in changes
        and changes["section_type_id"] is None
        and not section.is_draft
        and changes.get("is_draft") is not True
    ):
        raise SiteValidationError(guards.SECTION_TYPE_REQUIRED)
    # Tip varligi YAZMADAN ONCE (BLF-B1): "gonderilmedi" ile "null" ayrimi `in changes`.
    if "section_type_id" in changes:
        await _assert_section_type_exists(session, changes["section_type_id"])
    # Kod cakismasi ON KONTROLU — POST'takiyle AYNI Turkce mesaj (karar
    # 2026-07-30, `update_site` ile birebir). Onceden bu dal
    # `uq_sections_site_code` -> IntegrityError'a dusuyor ve genel "Veri
    # bütünlüğü hatası" doniyordu; kullanici hangi ALANIN sorunlu oldugunu
    # goremiyordu. `exclude_section_id` sarttir: kendi kodunu yeniden gondermek
    # cakisma DEGILDIR, aksi hâlde formun tum alanlarini birlikte gonderen her
    # PATCH 409 verirdi. Kisit YARIS DURUMU emniyet agi olarak KALIR.
    # `code` acikca NULL'lanirsa kontrol KOSMAZ: kismi indeks yalniz
    # `code IS NOT NULL` satirlarini kapsar.
    if changes.get("code") is not None and changes["code"] != section.code:
        clash = await repository.get_section_by_code(
            session, section.site_id, changes["code"], exclude_section_id=section.id
        )
        if clash is not None:
            raise DuplicateError(guards.DUPLICATE_SECTION_CODE)
    # Kullanici cozumu YAZMADAN ONCE (update_site ile ayni sira): gecersiz
    # kullanici govdedeki HICBIR alani degistirmez. Esleme POST ile PAYLASILIR
    # (`_resolved_manager_names`), kopyalanmaz.
    changes.update(await _resolved_manager_names(session, changes))
    # P11 — oncul korkulugu (self/ayni santiye/dongu) HICBIR ALAN yazilmadan
    # once: reddedilen istek adi da degistirmis birakmaz. Kosul `in changes`tir,
    # `is not None` DEGIL: acikca `null` gondermek BAGI KOPARIR ve o dal
    # dogrulamadan erken doner.
    if "depends_on_section_id" in changes:
        await _validate_dependency(
            session, section.site_id, changes["depends_on_section_id"], section_id=section.id
        )
    # Alan gonderilmediyse (`None`) satirlara DOKUNULMAZ; bos liste gonderilirse
    # hepsi duser. Ayrim `SectionUpdate.milestones` docstring'inde gerekcelidir.
    # Birlestirme 422 uretebildigi icin duz alanlar yazilmadan ONCE kosar
    # (`_merge_shareholders` sirasinin birebiri): reddedilen istek adi da
    # degistirmis birakmaz.
    if data.milestones is not None:
        # Birlestirme SENKRONDUR ve mevcut satirlari okur: koleksiyon yuklu
        # degilse orada tembel yukleme -> `MissingGreenlet` olurdu (ayni gerekce
        # `repository.ensure_milestones_loaded` docstring'inde).
        await repository.ensure_milestones_loaded(session, [section])
        _merge_milestones(section, data.milestones)
    for field, value in changes.items():
        setattr(section, field, value)
    await session.flush()
    await session.refresh(section)
    detail = (
        messages.section_published(site.name, section.name)
        if is_publishing
        else messages.section_updated(site.name, section.name)
    )
    return section, detail

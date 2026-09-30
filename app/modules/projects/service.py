import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.discipline_scope import DisciplineScope
from app.core.errors import (
    ConflictError,
    DuplicateError,
    NotFoundError,
    ProjectTypeMismatchError,
    ProjectValidationError,
)
from app.core.slug import allocate_slug, slugify, unique_slug
from app.core.timezone import today
from app.modules.projects import cost_cards, messages, progress_cards, repository
from app.modules.projects.cards import (
    _contracting_card,
    _investment_card,
    _land_share_card,
)
from app.modules.projects.cost_cards import ProjectCardCosts
from app.modules.projects.models import (
    Employer,
    LandShareShareholder,
    Project,
    ProjectContract,
    ProjectInvestment,
    ProjectLandShare,
    ProjectStatus,
    ProjectType,
)
from app.modules.projects.progress_cards import CardProgress
from app.modules.projects.schemas import (
    EmployerCreate,
    EmployerResponse,
    ProjectBudgetLines,
    ProjectContractInput,
    ProjectContractResponse,
    ProjectCounts,
    ProjectCreate,
    ProjectDetailResponse,
    ProjectInvestmentInput,
    ProjectLandShareInput,
    ProjectListItem,
    ProjectListResponse,
    ProjectSiteInput,
    ProjectUpdate,
    ShareholderInput,
)
from app.modules.roles.repository import get_permission

# Project.sites ters iliskisi sites.models icinde backref ile tanimlanir; sayaci
# okuyabilmek icin o modulun yuklenmis olmasi sarttir. Dongusel import YOK:
# sites.models yalniz projects.models'i import eder, projects.service'i degil.
from app.modules.sites.models import Site  # noqa: F401

# Isci sayacinin TEK kaynagi puantaj modulüdur (T4, puantaj spec §4): donem
# karari (icinde bulunulan ay) ve DISTINCT kurali orada gerekcelenmistir.
from app.modules.timesheet import counts as timesheet_counts
from app.modules.users.models import User

_DUPLICATE_TAX_NUMBER = "Bu VKN ile kayıtlı bir işveren zaten var."
_EMPLOYER_NOT_FOUND = "İşveren bulunamadı"
_PROJECT_CODE_PREFIX = "PRJ"


# --- İşveren (employers) servisi (spec §3.1, §3.2) ---


async def list_employers(session: AsyncSession, q: str | None, active_only: bool) -> list[Employer]:
    return await repository.list_employers(session, q, active_only)


async def create_employer(session: AsyncSession, data: EmployerCreate) -> Employer:
    """Yinelenen VKN -> DuplicateError (409). Servis ONCE SELECT ile bakar ki
    kullaniciya alanina ozel Turkce mesaj verilsin; IntegrityError -> 409 handler'i
    yaris durumu emniyet agi olarak KALIR (spec §3.2)."""
    if data.tax_number is not None:
        existing = await repository.get_employer_by_tax_number(session, data.tax_number)
        if existing is not None:
            raise DuplicateError(_DUPLICATE_TAX_NUMBER)
    employer = Employer(
        name=data.name,
        tax_number=data.tax_number,
        contact_person=data.contact_person,
    )
    return await repository.add_employer(session, employer)


def _to_item(
    project: Project,
    worker_count: int,
    card_costs: ProjectCardCosts = cost_cards.EMPTY,
    progress: CardProgress = progress_cards.EMPTY,
) -> ProjectListItem:
    """ProjectListItem.model_validate(project) calisamaz: ORM nesnesinde
    contracting/investment/land_share alanlari (bunlar turetilmis karttir, DB
    sutunu degil) yok — bu yuzden ortak alanlar elle cikarilir."""
    is_contracting = project.project_type is ProjectType.taahhut
    is_investment = project.project_type is ProjectType.kendi_yatirim
    is_land_share = project.project_type is ProjectType.kat_karsiligi
    return ProjectListItem(
        id=project.id,
        code=project.code,
        slug=project.slug,
        name=project.name,
        project_type=project.project_type,
        category=project.category,
        city=project.city,
        status=project.status,
        start_date=project.start_date,
        end_date=project.end_date,
        contract_no=project.contract_no,
        contract_amount=project.contract_amount,
        employer_name=project.employer_name,
        employer=EmployerResponse.model_validate(project.employer) if project.employer else None,
        contract=(
            ProjectContractResponse.model_validate(project.contract) if project.contract else None
        ),
        budget_lines=ProjectBudgetLines(
            material=project.budget_material,
            labor=project.budget_labor,
            subcontractor=project.budget_subcontractor,
            overhead=project.budget_overhead,
        ),
        is_draft=project.is_draft,
        budget=project.budget,
        progress_pct=project.progress_pct,
        contracting=(
            _contracting_card(worker_count, card_costs, progress) if is_contracting else None
        ),
        investment=_investment_card(project, card_costs) if is_investment else None,
        land_share=_land_share_card(project, card_costs) if is_land_share else None,
    )


def to_detail(
    project: Project,
    worker_count: int,
    card_costs: ProjectCardCosts = cost_cards.EMPTY,
    progress: CardProgress = progress_cards.EMPTY,
) -> ProjectDetailResponse:
    """Saf donusturucu — DB'ye DOKUNMAZ. Maliyet turevleri de `worker_count` gibi
    PARAMETREDIR (P10 T3): toplu okuma cagirandadir, verilmezse zarflar bos kalir."""
    return ProjectDetailResponse(
        **_to_item(project, worker_count, card_costs, progress).model_dump(),
        site_count=len(project.sites),
    )


async def build_project_detail(
    session: AsyncSession, project: Project, actor: User, scope: DisciplineScope
) -> ProjectDetailResponse:
    """Proje detay zarfi + isci sayaci. YAZMA uclarinin yaniti da buradan gecer:
    okuma ve yazma ayni zarfi tasimazsa ekran kaydettikten sonra sayaci kaybeder."""
    worker_counts = await timesheet_counts.by_project(session, [project.id])
    card_costs = await cost_cards.by_projects(session, [project])
    progress = await progress_cards.by_projects(session, actor, [project], scope)
    return to_detail(
        project,
        worker_counts.get(project.id, 0),
        card_costs.get(project.id, cost_cards.EMPTY),
        progress.get(project.id, progress_cards.EMPTY),
    )


async def visible_projects(session: AsyncSession, actor: User) -> list[Project]:
    """Spec §5.2: user_project_access suzgeci; projects=admin suzgeci atlar.

    Admin istisnasi Ayarlar kilitlenme korumasidir: erisim vermek icin tum
    projeleri listeleyebilmek gerekir.

    PUBLIC: P2 santiye/bolum uclari da bu suzgecten gecer (P2 spec §5.2) ve
    kendi kopya gorunurluk mantigini YAZMAZ. Tek kaynak burasidir.
    """
    permission = await get_permission(session, actor.role_id, "projects")
    if permission is not None and permission.access_level is AccessLevel.admin:
        return await repository.list_projects(session)
    return await repository.list_projects_for_user(session, actor.id)


def _counts(projects: list[Project]) -> ProjectCounts:
    return ProjectCounts(
        all=len(projects),
        taahhut=sum(1 for p in projects if p.project_type is ProjectType.taahhut),
        kendi_yatirim=sum(1 for p in projects if p.project_type is ProjectType.kendi_yatirim),
        kat_karsiligi=sum(1 for p in projects if p.project_type is ProjectType.kat_karsiligi),
        completed=sum(1 for p in projects if p.status is ProjectStatus.completed),
        draft=sum(1 for p in projects if p.is_draft),
    )


async def list_projects_overview(
    session: AsyncSession,
    actor: User,
    type_filter: ProjectType | str | None,
    status_filter: ProjectStatus | str | None,
    scope: DisciplineScope,
    limit: int = 50,
    offset: int = 0,
) -> ProjectListResponse:
    """Proje listesi — süzgeç + sayfalama (SITE-1b / K4).

    🔴 ÜÇ AYRI KÜME, KARIŞTIRMA:
      1. `visible`  — görünür projelerin TAMAMI. `counts` YALNIZ bundan çıkar;
         süzgeçten de sayfalamadan da ETKİLENMEZ (spec §5.1: mockup sekmeleri
         hep tüm kümeyi sayar).
      2. `selected` — `type`/`status` uygulanmış küme. `total` bunun boyutudur;
         sayfa çubuğunun sayfa sayısı buradan çıkar.
      3. `page`     — `selected`ın `offset`/`limit` dilimi. `items` budur.

    🔴 PERFORMANS: pahalı toplu türev okumaları (`timesheet_counts.by_project`,
    `cost_cards.by_projects`) YALNIZ `page` için koşar. `selected` için
    koşsalardı sayfalama hiçbir maliyeti düşürmez, yalnız yanıt gövdesini
    kısaltırdı. `_counts(visible)` bundan etkilenmez — o zaten bellekteki ucuz
    bir sayımdır, ek sorgu açmaz.

    SIRALAMA: `visible_projects` her iki yolda da `code` artan döner ve
    `projects.code` KÜRESEL TEKİLdir (`unique=True`) — eşitlik bozucu ikincil
    anahtara gerek yoktur, sayfa sınırları kaymaz. Sıra DEĞİŞTİRİLMEDİ.
    """
    visible = await visible_projects(session, actor)
    selected = visible
    if type_filter is not None:
        wanted_type = ProjectType(type_filter)
        selected = [p for p in selected if p.project_type is wanted_type]
    if status_filter is not None:
        wanted_status = ProjectStatus(status_filter)
        selected = [p for p in selected if p.status is wanted_status]
    page = selected[offset : offset + limit]
    worker_counts = await timesheet_counts.by_project(session, [p.id for p in page])
    # P10 T3: kart maliyet/kâr türevleri TEK toplu okumadan gelir — proje başına
    # sorgu YASAK (spec §4, `timesheet_counts.by_project` ile aynı desen).
    card_costs = await cost_cards.by_projects(session, page)
    card_progress = await progress_cards.by_projects(session, actor, page, scope)
    return ProjectListResponse(
        counts=_counts(visible),
        items=[
            _to_item(
                p,
                worker_counts.get(p.id, 0),
                card_costs.get(p.id, cost_cards.EMPTY),
                card_progress.get(p.id, progress_cards.EMPTY),
            )
            for p in page
        ],
        total=len(selected),
        limit=limit,
        offset=offset,
    )


def project_matches_ref(project: Project, ref: uuid.UUID | str) -> bool:
    """URL-2: proje YA kimligiyle YA slug'iyla eslesir (karar 2).

    Slug NULL olan bir proje HICBIR slug'la eslesmez: slug tasimayan kayit
    yalnizca UUID'siyle erisilebilir — istenen davranis budur.

    🔴 `is not None` MUTASYONLA OLCULDU ve **ESDEGER MUTANT** cikti (durustce
    kaydediliyor): kaldirildiginda 20 API testinin hicbiri kirilmadi. Sebep
    olculdu — Python'da `None == "slug"` zaten `False`, ve `ref` hicbir zaman
    `None` olamaz (yol parametresi zorunlu, `parse_ref` `None` dondurmez). Yani
    bu kosul bugun BIR OLCUTU SAVUNMUYOR, NIYETI BELGELIYOR. Bilincli olarak
    KALIYOR: `ref`in bir gun opsiyonellesmesi hâlinde "bos referans her slugsuz
    kaydi acar" kusurunu pesinen kapatir. "Test var" ile "olcutu savunan bekci
    var" ayni sey degildir — burada IKINCISI YOK.
    """
    if isinstance(ref, uuid.UUID):
        return project.id == ref
    return project.slug is not None and project.slug == ref


async def _visible_project(
    session: AsyncSession, actor: User, project_ref: uuid.UUID | str
) -> Project:
    """Gorunur kumede olmayan proje 404 — varligi sizdirilmaz (spec §5.6).

    TEK kimlik-ile-erisim kapisi burasidir. Hem OKUMA hem YAZMA uclari bundan
    gecmek ZORUNDA: yalnizca okumayi suzmek, listede hic gorunmeyen bir projeyi
    UUID'sini bilen kullanicinin PATCH ile degistirebilmesi demektir.

    🔴 URL-2 / IDOR: slug TAHMIN EDILEBILIR, UUID degil. Bu yuzden slug
    cozumlemesi AYRI bir sorgu olarak DEGIL, TAM OLARAK BURADA — gorunur
    kumenin ICINDE — yapilir. Once cozup sonra suzmek, kapiyi bir satirlik bir
    dikkatsizlikle atlanabilir kilardi; kume icinde eslestirmek onu YAPISAL
    olarak atlanamaz yapar. Gormedigi projenin slug'iyla gelen istek, var
    olmayan slug'la BIREBIR AYNI 404'u alir.
    """
    visible = await visible_projects(session, actor)
    project = next((p for p in visible if project_matches_ref(p, project_ref)), None)
    if project is None:
        raise NotFoundError("Proje bulunamadı")
    return project


async def get_project_detail(
    session: AsyncSession, actor: User, project_ref: uuid.UUID | str, scope: DisciplineScope
) -> ProjectDetailResponse:
    project = await _visible_project(session, actor, project_ref)
    return await build_project_detail(session, project, actor, scope)


def _ensure_type_consistency(
    project_type: ProjectType,
    investment: ProjectInvestmentInput | None,
    land_share: ProjectLandShareInput | None,
) -> None:
    """Spec §3.5 korkulugu. Tek yazma yolu burasi oldugu icin kontrol tek noktada."""
    if investment is not None and project_type is not ProjectType.kendi_yatirim:
        raise ProjectTypeMismatchError(
            "Yatırım alanları yalnızca kendi yatırım projelerine girilebilir"
        )
    if land_share is not None and project_type is not ProjectType.kat_karsiligi:
        raise ProjectTypeMismatchError(
            "Arsa payı alanları yalnızca kat karşılığı projelerine girilebilir"
        )


def _apply_investment(project: Project, data: ProjectInvestmentInput) -> None:
    if project.investment is None:
        project.investment = ProjectInvestment(project_id=project.id)
    project.investment.sales_target = data.sales_target
    project.investment.land_cost = data.land_cost


async def _merge_shareholders(
    session: AsyncSession, project: Project, inputs: list[ShareholderInput]
) -> None:
    """Hissedar listesini KIMLIK KORUYARAK birlestirir (spec §4.1).

    Eski davranis listeyi toptan silip yeniden aciyordu; `units.shareholder_id`
    FK'si (P9 T1, ON DELETE SET NULL) acildiktan sonra bu, siradan bir proje
    PATCH'inde TUM unite atamalarini SESSIZCE supururdu. Kurallar:

    - id eslesen satir YERINDE guncellenir (ad/oran) — birincil anahtar yasar.
    - id'siz girdi yeni satirdir (eski govdeler geriye uyumlu).
    - listede olmayan mevcut satir silinir; ATANMIS unitesi varsa 409.

    Dogrulamalarin TAMAMI hicbir sey yazilmadan ONCE kosar: yarim uygulanmis bir
    liste (ilk satir yeniden adlandirildi, ikincisi 409'a takildi) sessiz veri
    hatasidir. Bu yuzden fonksiyon ya tamamen uygular ya hic dokunmaz.
    """
    existing = {row.id: row for row in project.shareholders}
    sent_ids = [item.id for item in inputs if item.id is not None]
    kept_ids = set(sent_ids)
    # T5 bulgusu: ayni id iki kez gonderilirse birlestirme SESSIZCE tek satira
    # cokerdi (ikinci girdinin adi kazanir, ilkinin orani kaybolur, yanit 200).
    # `units.batch.update_allocation`in DUPLICATE_IN_PAYLOAD kapisinin esi.
    if len(sent_ids) != len(kept_ids):
        raise ProjectValidationError(messages.SHAREHOLDER_DUPLICATE_IN_PAYLOAD)
    unknown = kept_ids - set(existing)
    if unknown:
        # Baska projenin hissedari da buraya duser: proje disi id, bu projede YOKTUR.
        raise ProjectValidationError(messages.SHAREHOLDER_UNKNOWN)

    removed = [row for row in project.shareholders if row.id not in kept_ids]
    if removed:
        assigned = await repository.shareholder_ids_with_units(session, [r.id for r in removed])
        blocked = [row.name for row in removed if row.id in assigned]
        if blocked:
            raise ConflictError(messages.shareholder_has_units(sorted(blocked)))

    merged: list[LandShareShareholder] = []
    for item in inputs:
        if item.id is None:
            merged.append(LandShareShareholder(name=item.name, share_pct=item.share_pct))
            continue
        row = existing[item.id]
        row.name = item.name
        row.share_pct = item.share_pct
        merged.append(row)
    # Listede kalanlar AYNI nesnelerdir: delete-orphan yalnizca dusenleri siler.
    project.shareholders = merged


async def _apply_land_share(
    session: AsyncSession, project: Project, data: ProjectLandShareInput
) -> None:
    # Hissedar dogrulamasi (422/409) DIGER alanlara dokunmadan once: reddedilen
    # istek arsa payi alanlarini da degistirmis birakmaz.
    await _merge_shareholders(session, project, data.shareholders)
    if project.land_share is None:
        project.land_share = ProjectLandShare(project_id=project.id)
    land_share = project.land_share
    land_share.landowner_name = data.landowner_name
    land_share.our_share_pct = data.our_share_pct
    land_share.owner_share_pct = data.owner_share_pct
    land_share.contract_no = data.contract_no
    land_share.notary_date = data.notary_date
    land_share.land_area_m2 = data.land_area_m2
    land_share.construction_area_m2 = data.construction_area_m2
    land_share.delivery_date = data.delivery_date
    land_share.daily_penalty = data.daily_penalty
    land_share.guarantee_amount = data.guarantee_amount


async def _next_project_code(session: AsyncSession, year: int) -> str:
    """PRJ-{YYYY}-{NNN} üretir (spec §3.5): o yılın en büyük sırası + 1, 3 hane, 1'den.

    Sayımla DEĞİL maksimum+1 ile: silinen kod yeniden kullanılmasın. Benzersizlik
    kısıtı yarış durumunu 409'a çevirir (IntegrityError handler).
    """
    prefix = f"{_PROJECT_CODE_PREFIX}-{year}-"
    codes = await repository.list_codes_with_prefix(session, prefix)
    max_seq = 0
    for code in codes:
        suffix = code[len(prefix) :]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return f"{prefix}{max_seq + 1:03d}"


async def _resolve_employer(session: AsyncSession, employer_id: uuid.UUID) -> Employer:
    employer = await repository.get_employer(session, employer_id)
    if employer is None:
        raise NotFoundError(_EMPLOYER_NOT_FOUND)
    return employer


def _validate_taahhut_required(data: ProjectCreate) -> None:
    """Spec §3.6 kural 3: taslak-dışı taahhüt projesinde zorunlu alanlar."""
    if data.employer_id is None:
        raise ProjectValidationError("İşveren firma seçiniz.")
    contract = data.contract
    if contract is None or not contract.contract_no:
        raise ProjectValidationError("Sözleşme no zorunludur.")
    if contract.signature_date is None:
        raise ProjectValidationError("İmza tarihi zorunludur.")
    if contract.amount is None:
        raise ProjectValidationError("Sözleşme bedeli sayı olmalıdır.")
    if data.start_date is None or data.end_date is None:
        raise ProjectValidationError("Başlangıç ve bitiş tarihi zorunludur.")


def _validate_project(data: ProjectCreate) -> None:
    """Sunucu doğrulaması (spec §3.6). Taslak-farkındalıklı: taslakta yalnız
    tutarlılık kuralları (4, 6, 7) uygulanır; zorunluluk kuralları (2, 3, 5) atlanır.
    """
    is_taahhut = data.project_type is ProjectType.taahhut
    # Kural 7 (her zaman): taahhüt dışı tipte sözleşme/işveren yasak.
    if not is_taahhut and (data.contract is not None or data.employer_id is not None):
        raise ProjectTypeMismatchError(
            "Sözleşme ve işveren bilgileri yalnızca taahhüt projelerine girilebilir."
        )
    # Kural 4 (her zaman): tarih sırası.
    if (
        data.start_date is not None
        and data.end_date is not None
        and data.end_date < data.start_date
    ):
        raise ProjectValidationError("Bitiş tarihi başlangıçtan önce olamaz.")
    if data.is_draft:
        return
    # Kural 2 (taslak-dışı): il/ilçe zorunlu.
    if not data.city:
        raise ProjectValidationError("İl / ilçe zorunludur.")
    # Kural 5 (taslak-dışı): fiyat farkı açıksa endeks zorunlu.
    if data.contract is not None and data.contract.has_price_escalation:
        if data.contract.index_type is None or data.contract.base_index_value is None:
            raise ProjectValidationError("Endeks tipi ve baz endeks değeri zorunludur.")
    # Kural 3 (taslak-dışı): taahhüt zorunlulukları.
    if is_taahhut:
        _validate_taahhut_required(data)


def _apply_contract(project: Project, data: ProjectContractInput) -> None:
    """project_contracts satırını yazar; contract_no/amount projeye anlık görüntü kopyalanır.

    🔴 PATCH yolundan da çağrılır. Satır ZATEN VARKEN yeniden atama SQLAlchemy'de
    "row switch"tir: aynı kimlikli DELETE+INSERT tek bir UPDATE'e indirgenir
    (ölçüldü — poz kalemleri ve gönderilmeyen `status` kolonu yerinde kalır).
    Bu, gerçek bir DELETE'in `project_contracts.project_id`ye CASCADE ile bağlı
    `employer_contract_groups`/`_items` (contracts/models.py:55,93) ve
    `progress_payments` (progress_payments/models.py:83) satırlarını götürecek
    olması yüzünden KRİTİKTİR: bu atama ile onu izleyen `flush` ARASINA başka bir
    `flush` GİRMEMELİDİR, yoksa silme tek başına gider.
    """
    project.contract = ProjectContract(
        contract_no=data.contract_no,
        signature_date=data.signature_date,
        amount=data.amount,
        advance_pct=data.advance_pct,
        retainage_pct=data.retainage_pct,
        vat_pct=data.vat_pct,
        late_penalty_daily=data.late_penalty_daily,
        has_price_escalation=data.has_price_escalation,
        index_type=data.index_type,
        base_index_value=data.base_index_value,
    )
    # contract_no/amount burada otoritedir; projeye kopyalanır (spec §2.4, §5).
    project.contract_no = data.contract_no
    project.contract_amount = data.amount


def _validate_contract_update(project: Project, contract: ProjectContractInput) -> None:
    """PATCH yolundaki sözleşme için create'in İKİ tutarlılık kuralı (spec §3.6).

    Kural 7: taahhüt dışı tipte sözleşme yasak (create'te service.py:441-443).
    Kural 5 (taslak-dışı): fiyat farkı açıksa endeks zorunlu (create'te :458-460).
    Zorunluluk kuralları (kural 3) BURADA aranmaz: PATCH kısmi güncellemedir,
    taslak bir projeyi tamamlamaya zorlamaz.
    """
    if project.project_type is not ProjectType.taahhut:
        raise ProjectTypeMismatchError(
            "Sözleşme ve işveren bilgileri yalnızca taahhüt projelerine girilebilir."
        )
    if not project.is_draft and contract.has_price_escalation:
        if contract.index_type is None or contract.base_index_value is None:
            raise ProjectValidationError("Endeks tipi ve baz endeks değeri zorunludur.")


async def _write_inline_sites(
    session: AsyncSession, project: Project, sites_input: list[ProjectSiteInput]
) -> None:
    """Satır içi şantiyeleri aynı transaction'da yazar (spec §3.4, §7.7).

    Kod üretimi P2'nin `sites.service`'inden YENİDEN KULLANILIR (kopya mantık yok):
    tek üretici `_next_site_code` → `SNT-{YYYY}-{NNN}` (spec §3.2). Ad-türevi eski
    üretici kaldırıldığı için burada da iki farklı kod deseni yan yana yaşamaz.
    Kod çakışması `uq_sites_project_code` → 409 (IntegrityError) ve proje de yazılmaz
    (tek transaction). Ayrıca `sites` izni ARANMAZ: proje oluşturmanın parçasıdır.
    """
    # Yerel import: sites.service, projects.service'i (visible_projects) import
    # ettiği için modül düzeyinde çember olurdu.
    from app.modules.sites.service import _next_site_code

    # URL-2: proje YENI oldugu icin kapsamda (proje ici) hicbir santiye YOKTUR —
    # slug'lar DB'ye sorulmadan, biriken kume uzerinde ayrilir. Ayni ada sahip
    # iki satir ici santiye (`A Blok` / `A. Blok`) burada `a-blok` ve `a-blok-2`
    # olur; sessizce cakismaz.
    taken_slugs: set[str] = set()
    for site_input in sites_input:
        code = site_input.code or await _next_site_code(session)
        slug = unique_slug(slugify(site_input.name), taken_slugs)
        if slug is not None:
            taken_slugs.add(slug)
        session.add(
            Site(
                project_id=project.id,
                code=code,
                slug=slug,
                name=site_input.name,
                site_manager_name=site_input.site_manager_name,
                construction_area_m2=site_input.construction_area_m2,
            )
        )
        # Sonraki şantiyenin _next_site_code'u bu satırı görebilsin diye hemen flush.
        await session.flush()


async def create_project(session: AsyncSession, data: ProjectCreate) -> Project:
    # Tip tutarlılığı (P1 §3.5) + proje doğrulaması (spec §3.6) yazmadan ÖNCE.
    _ensure_type_consistency(data.project_type, data.investment, data.land_share)
    _validate_project(data)
    employer = (
        await _resolve_employer(session, data.employer_id) if data.employer_id is not None else None
    )
    code = data.code or await _next_project_code(session, today().year)
    lines = data.budget_lines
    # budget = Σ kalemler (spec §2.3, §3.4): SERVİS hesaplar; istemci `budget` yok sayılır.
    total_budget = lines.material + lines.labor + lines.subcontractor + lines.overhead
    # URL-2: slug OLUSTURULURKEN uretilir ve AD DEGISINCE DEGISMEZ (kullanici
    # karari 2026-08-29) — bu yuzden `update_project` slug'a HIC dokunmaz.
    # Kapsam SIRKET GENELIDIR: `allocate_slug`e suzgec verilmez.
    slug = await allocate_slug(session, data.name, Project.slug)
    project = Project(
        code=code,
        slug=slug,
        name=data.name,
        project_type=data.project_type,
        status=data.status,
        category=data.category,
        city=data.city,
        parcel=data.parcel,
        address=data.address,
        start_date=data.start_date,
        end_date=data.end_date,
        employer_id=employer.id if employer is not None else None,
        # employer_name anlık görüntüsü (spec §2.3): işveren adı buraya kopyalanır.
        employer_name=employer.name if employer is not None else None,
        budget=total_budget,
        budget_material=lines.material,
        budget_labor=lines.labor,
        budget_subcontractor=lines.subcontractor,
        budget_overhead=lines.overhead,
        is_draft=data.is_draft,
    )
    session.add(project)
    await session.flush()
    # Yeni flush edilmis nesnenin iliskileri henuz sync eslenmemis: asagidaki
    # senkron `is None` erisimleri async ortamda MissingGreenlet patlatir.
    await session.refresh(
        project, attribute_names=["investment", "land_share", "shareholders", "contract"]
    )
    if data.contract is not None:
        _apply_contract(project, data.contract)
    if data.investment is not None:
        _apply_investment(project, data.investment)
    if data.land_share is not None:
        await _apply_land_share(session, project, data.land_share)
    await session.flush()
    # Satır içi şantiyeler tek transaction içinde; kod çakışması tüm oluşturmayı geri alır.
    await _write_inline_sites(session, project, data.sites)
    await session.refresh(project)
    return project


def _sync_contract_authority(project: Project, changes: dict[str, Any]) -> None:
    """🔴 `project_contracts.contract_no/amount` OTORİTEDİR (models.py:199-200);

    `projects.contract_no/contract_amount` onun ANLIK GÖRÜNTÜSÜDÜR. `_apply_contract`
    bu kuralı yalnız `create_project`te uygular; PATCH yolunda uygulanmazsa iki
    kaynak AYRIŞIR: `GET /projects/{id}/contract` ve `GET /contracts` otoriteyi
    okuduğu için eskiyi basar, `?q=` süzgeci de otoriteye baktığından kullanıcının
    ekranda GÖRDÜĞÜ numarayla arama SONUÇ VERMEZ, ve işveren hakediş yüzdesinin
    paydası (`contract.amount`) hiçbir uçtan düzeltilemez hâle gelir.

    Anahtarın VARLIĞI bakılır, değeri değil: `exclude_unset` sözlüğünde bir alan
    yalnız istemci onu GÖNDERDİYSE bulunur — `None` göndermek "temizle" demektir
    ve otoriteye de aynen yansımalıdır.

    Sözleşme satırı YOKSA dokunulmaz: satırın PATCH'ten geç doğması (avans /
    teminat / KDV oranları olmadan) ayrı bir ürün kararıdır, burada VERİLMEZ.
    """
    contract = project.contract
    if contract is None:
        return
    if "contract_no" in changes:
        contract.contract_no = changes["contract_no"]
    if "contract_amount" in changes:
        contract.amount = changes["contract_amount"]


_FLAT_CONTRACT_FIELDS = ("contract_no", "contract_amount")


def _validate_flat_contract_fields(project: Project, changes: dict[str, Any]) -> None:
    """`_validate_contract_update`in düz-alan ikizi (Kayıt #14/#28 açık bacağı).

    `ProjectUpdate.contract` gövdesi Kural 7'den (taahhüt dışı tipte sözleşme
    yasak) `_validate_contract_update` ile geçer, ama üst düzey `contract_no`/
    `contract_amount` aynı kapıdan HİÇ geçmiyordu: `_ensure_type_consistency`
    yalnız `investment`/`land_share` imzasını taşır (service.py:303-307).
    Ayrıca sözleşme SATIRI yokken (taahhüt dışı proje YAPISAL OLARAK asla
    satır açamaz — Kural 7, create'te service.py:441-444; taslak taahhüt
    projesi de sözleşmesiz doğabilir — service.py:452) bu alanlar
    `_sync_contract_authority`nin `contract is None` erken dönüşü yüzünden
    yalnız `projects` anlık görüntüsüne yazılıp otoriteyi hiç doğurmuyordu.
    Otoritenin GEÇ DOĞMASI ayrı bir ürün kararıdır (bkz. `_sync_contract_authority`
    docstring'i) — burada VERİLMEZ; bunun yerine create'in reddettiği durum
    PATCH'le de aynı şekilde reddedilir.
    """
    if not any(field in changes for field in _FLAT_CONTRACT_FIELDS):
        return
    if project.project_type is not ProjectType.taahhut:
        raise ProjectTypeMismatchError(
            "Sözleşme ve işveren bilgileri yalnızca taahhüt projelerine girilebilir."
        )
    if project.contract is None:
        raise ProjectValidationError(
            "Sözleşme numarası/bedeli düzenlenemedi: bu projede henüz bir sözleşme "
            "satırı yok. Önce tam sözleşme nesnesini (`contract`) gönderin."
        )


async def update_project(
    session: AsyncSession, actor: User, project_id: uuid.UUID, data: ProjectUpdate
) -> Project:
    project = await _visible_project(session, actor, project_id)
    _ensure_type_consistency(project.project_type, data.investment, data.land_share)
    if data.contract is not None:
        _validate_contract_update(project, data.contract)
    # `contract` bir İLİŞKİdir: sözlükte kalırsa setattr döngüsü ORM alanının
    # üstüne düz bir dict yazar. Sözleşme aşağıda `_apply_contract` ile işlenir.
    changes = data.model_dump(exclude_unset=True, exclude={"investment", "land_share", "contract"})
    _validate_flat_contract_fields(project, changes)
    for field, value in changes.items():
        setattr(project, field, value)
    _sync_contract_authority(project, changes)
    if data.contract is not None:
        # Tam sözleşme nesnesi gelmişse otorite ODUR: `contract_no`/`contract_amount`
        # anlık görüntüsü de burada tazelenir (spec §2.4, §5).
        _apply_contract(project, data.contract)
    if data.investment is not None:
        _apply_investment(project, data.investment)
    if data.land_share is not None:
        await _apply_land_share(session, project, data.land_share)
    await session.flush()
    await session.refresh(project)
    return project

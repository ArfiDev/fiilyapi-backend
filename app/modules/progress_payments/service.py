"""İşveren hakedişi (P7) CRUD servis katmanı — task H4.

İki katmanlı koruma (`contracts/service.py` deseninin birebiri, spec §9.0):
`progress_payments` izni router'da (`_VIEW`/`_DRAFT`/…) YETKİYİ verir, bu modül
`projects.service.visible_projects` ile KAPSAMI belirler — görünmeyen projenin
hakedişi hiçbir uçtan asla görünmez/düzenlenmez. Görünmeyen projedeki GERÇEK
hakediş ile var OLMAYAN kimlik AYIRT EDİLEMEZ 404 döner (P5 IDOR dersi, GOREV-SIRASI
§3): ikisi de `guards.PAYMENT_MISSING` ile aynı gövdeyi üretir.
"""

import uuid
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import restricted_project_ids
from app.core.errors import ConflictError, NotFoundError, SiteValidationError
from app.core.slug import allocate_slug, composite_slug
from app.core.timezone import today
from app.modules.contracts.models import EmployerContractItem
from app.modules.progress_payments import calculations, guards, lines, repository
from app.modules.progress_payments.models import (
    ProgressPayment,
    ProgressPaymentLine,
    ProgressPaymentStatus,
)
from app.modules.progress_payments.schemas import (
    PaymentCalculationBlock,
    ProgressBlock,
    ProgressPaymentCreate,
    ProgressPaymentDetail,
    ProgressPaymentGroupSummary,
    ProgressPaymentLineDetail,
    ProgressPaymentLinesSave,
    ProgressPaymentListItem,
    ProgressPaymentListResponse,
    ProgressPaymentUpdate,
)
from app.modules.projects.models import Project, ProjectContract
from app.modules.projects.service import visible_projects
from app.modules.users.models import User

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")
_DEFAULT_COEFFICIENT = Decimal("1.000")

# Denetim günlüğü (H10, spec §11) — `progress_payment_deleted` kaydın durumunu
# insan-okur Türkçe etiketle taşır (`audit/messages.py.ACCESS_LEVEL_LABELS`
# deseninin aynısı, ama izin seviyesi DEĞİL modülün kendi durum enum'u olduğu
# için audit modülüne değil BURAYA aittir — audit generic kalır).
_STATUS_LABELS: dict[ProgressPaymentStatus, str] = {
    ProgressPaymentStatus.draft: "Taslak",
    ProgressPaymentStatus.pending_approval: "Onay Bekliyor",
    ProgressPaymentStatus.approved: "Onaylandı",
    ProgressPaymentStatus.paid: "Ödendi",
}


class DeletedPaymentSummary(NamedTuple):
    """H8'den devredilen not (plan H10): silinen kaydın `session.delete`

    ÖNCESİNDE çıkarılmış özeti — kayıt gittiğinde bu üçlü bir daha okunamaz.
    """

    project_name: str
    sequence_no: int
    status_label: str
    amount: Decimal


# --- Kapsam (spec §9.0) ---


async def _visible_project(session: AsyncSession, actor: User, project_id: uuid.UUID) -> Project:
    """`contracts/service.py._visible_project` deseninin birebiri: görünmeyen proje
    ile var olmayan proje AYNI 404 gövdesini (`guards.PAYMENT_MISSING`) üretir —
    bu modülün TEK "bulunamadı" metni vardır (spec §9.0)."""
    visible = await visible_projects(session, actor)
    project = next((p for p in visible if p.id == project_id), None)
    if project is None:
        raise NotFoundError(guards.PAYMENT_MISSING)
    return project


async def _visible_payment(
    session: AsyncSession, actor: User, payment_ref: uuid.UUID | str
) -> tuple[ProgressPayment, Project]:
    """Dolaylı kimlikle erişim de görünürlük süzgecinden geçmek ZORUNDA — kayıt
    var ama projesi görünmüyorsa da `_visible_project` AYNI `PAYMENT_MISSING`
    404'ünü fırlatır (403 sızdırmaz, spec §9.0)."""
    payment = await repository.get_payment(session, payment_ref)
    if payment is None:
        raise NotFoundError(guards.PAYMENT_MISSING)
    # 🔴 Kapsam süzgeci ÇÖZÜMDEN SONRA ama HER ZAMAN koşar: slug TAHMİN
    # EDİLEBİLİR (`kopru-guclendirme-5`), UUID değil. Görünmeyen projedeki
    # hakedişin slug'ı, var olmayan slug'la BİREBİR AYNI 404'ü alır.
    project = await _visible_project(session, actor, payment.project_id)
    return payment, project


async def visible_payment_locked(
    session: AsyncSession, actor: User, payment_id: uuid.UUID
) -> tuple[ProgressPayment, Project, ProjectContract | None]:
    """Kapsam süzgeci + `SELECT … FOR UPDATE` — durum geçişlerinin (H6) giriş kapısı.

    Kilit sırası `create` ile AYNIDIR: **önce sözleşme, sonra hakediş**. Ters
    sırada kilitleyen ikinci bir yol açılırsa karşılıklı kilitlenme doğar.

    Kapsam kararı (404) kilitten ÖNCE verilir — görünmeyen bir kaydın satırı
    boşuna kilitlenmez ve 404 metni her iki halde de `PAYMENT_MISSING`'dir
    (spec §9.0).
    """
    payment, project = await _visible_payment(session, actor, payment_id)
    contract = await repository.get_contract_locked(session, project.id)
    locked = await repository.get_payment_locked(session, payment_id)
    if locked is None:
        # Yarışta silinmiş olabilir (H8 silme yolu) — var olmayan kayıtla aynı 404.
        raise NotFoundError(guards.PAYMENT_MISSING)
    return locked, project, contract


# --- Fiyat tazeleme (spec §5.1, §9.3, §14 D3/D5) ---


async def refresh_prices(
    session: AsyncSession, actor: User, payment_id: uuid.UUID
) -> tuple[ProgressPayment, Project, int]:
    """`POST …/refresh-prices` — yalnız `draft`'ta bağı kopmamış satırların
    snapshot BEŞLİSİNİ (`code/description/unit/contract_unit_price/group_name`)
    kalemden, hakedişin YÜZDE ÜÇLÜSÜNÜ (`vat_pct/advance_pct/retainage_pct`)
    sözleşmeden yeniden kopyalar (spec §5.1: "yüzdeler de aynı uçta tazelenir",
    §14 D3/D5). `coefficient`/`quantity` KULLANICI VERİSİDİR, DOKUNULMAZ.

    Kilit sırası `create`/`transitions.perform` ile AYNIDIR (önce sözleşme,
    sonra hakediş, `visible_payment_locked`) — ters sırada kilitleyen ikinci
    bir yol karşılıklı kilitlenmeye (deadlock) yol açar.

    Kalemi silinmiş satır (`contract_item_id IS NULL`, `SET NULL`) tazelenemez:
    kıyaslanacak canlı fiyat yoktur. SESSİZCE atlanmaz — satır SİLİNMEZ, yalnız
    `refreshed_count`'a girmez; bu durum zaten `get_detail`'in `is_price_stale
    =None` alanıyla kullanıcıya bildirilir (spec §5.1 dipnotu) — ayrı bir
    "düşen satır" alanı icat edilmez, çünkü hiçbir veri kaybolmaz.

    Değişmemiş satır/yüzde YAZILMAZ (gereksiz `UPDATE` yok, no-op tazeleme
    `refreshed_count=0` döner) — altı alanın (beşli + `source_code`) TAMAMI aynıysa satır sayılmaz.
    """
    payment, project, contract = await visible_payment_locked(session, actor, payment_id)
    if payment.status != ProgressPaymentStatus.draft:
        raise ConflictError(guards.INVALID_STATUS_TRANSITION)
    if contract is None:
        raise SiteValidationError(guards.NO_EMPLOYER_CONTRACT)

    if payment.vat_pct != contract.vat_pct:
        payment.vat_pct = contract.vat_pct
    if payment.advance_pct != contract.advance_pct:
        payment.advance_pct = contract.advance_pct
    if payment.retainage_pct != contract.retainage_pct:
        payment.retainage_pct = contract.retainage_pct

    item_ids = [
        line.contract_item_id for line in payment.lines if line.contract_item_id is not None
    ]
    items_with_group = await repository.get_employer_items_with_group_by_ids(session, item_ids)

    refreshed_count = 0
    for line in payment.lines:
        if line.contract_item_id is None:
            continue
        item_and_group = items_with_group.get(line.contract_item_id)
        if item_and_group is None:
            # Sözlükte yok ama FK hâlâ dolu: yarışta silinmiş olabilir — bu
            # tazeleme turunda kalemi silinmiş satırla AYNI muameleyi görür.
            continue
        item, group_name = item_and_group
        if (
            line.code == item.code
            and line.source_code == item.source_code
            and line.description == item.description
            and line.unit == item.unit
            and line.contract_unit_price == item.unit_price
            and line.group_name == group_name
        ):
            continue
        line.code = item.code
        line.source_code = item.source_code
        line.description = item.description
        line.unit = item.unit
        line.contract_unit_price = item.unit_price
        line.group_name = group_name
        refreshed_count += 1

    await session.flush()
    await session.refresh(payment)
    return payment, project, refreshed_count


# --- Oluşturma (spec §9.2, D8, kalıcı karar 4/9) ---
#
# Satır üretimi/doğrulaması TEK YOLDAN geçer: `lines.py`. H4'te burada duran
# asgari sahiplik kontrolleri (SITE_PROJECT_MISMATCH/ITEM_PROJECT_MISMATCH/
# DUPLICATE_CELL) H5'te oraya taşındı ve §6.5'in kalan kurallarıyla (dağıtım ön
# şartı, kota tavanı, FF kilidi) BİRLEŞTİRİLDİ — iki kopya kural zamanla ayrışır,
# ayrışan taraf sessiz bir veri hatası olur (guards.py'nin en üstündeki kural).


async def create(
    session: AsyncSession, actor: User, project_id: uuid.UUID, data: ProgressPaymentCreate
) -> tuple[ProgressPayment, Project]:
    """D8 + `sequence_no` üretimi AYNI kilit altında (spec §7 eşzamanlılık notu):

    `repository.get_contract_locked` sözleşme satırını `SELECT … FOR UPDATE` ile
    kilitler; bu transaction commit/rollback OLANA kadar aynı projede başka bir
    `create` çağrısı burada BEKLER — iki eşzamanlı `POST` aynı `sequence_no`'yu
    üretemez, ikisi de D8 kontrolünü boşken geçemez.
    """
    project = await _visible_project(session, actor, project_id)

    contract = await repository.get_contract_locked(session, project_id)
    if contract is None:
        raise SiteValidationError(guards.NO_EMPLOYER_CONTRACT)

    if await repository.get_open_payment(session, project_id) is not None:
        raise ConflictError(guards.OPEN_PAYMENT_EXISTS)

    # FF kilidi BAŞLIKTA da koşar (kullanıcı kararı 2026-07-31, H5 denetimi Y1):
    # FF'siz sözleşmede `default_coefficient != 1` kabul edilseydi hakediş
    # DOĞUŞTAN kullanılamaz olurdu — her yeni satır kilide takılırdı.
    guards.validate_coefficient(
        data.default_coefficient, has_price_escalation=contract.has_price_escalation
    )

    sequence_no = await repository.get_next_sequence_no(session, project_id)
    default_coefficient = data.default_coefficient or _DEFAULT_COEFFICIENT

    payment = ProgressPayment(
        project_id=project.id,
        sequence_no=sequence_no,
        period_year=data.period_year,
        period_month=data.period_month,
        description=data.description,
        vat_pct=contract.vat_pct,
        advance_pct=contract.advance_pct,
        retainage_pct=contract.retainage_pct,
        default_coefficient=default_coefficient,
        created_by=actor.id,
    )
    # URL-4: bileşik anahtarın (`project_id`, `sequence_no`) SAKLANAN karşılığı.
    # Proje slug'ı NULL ise (adı slug'lanamayan proje) `allocate_slug` `None`
    # döner ve hakediş UUID'siyle yaşar — uydurma taban YAZILMAZ.
    # Proje sonradan yeniden adlandırılsa bile bu slug DEĞİŞMEZ (URL-2 kararı 4).
    # `composite_slug`: SIRA NUMARASI kirpmada KAYBOLMAZ (K2). Naif
    # `f"{slug}-{seq}"` uzun proje adinda `-{seq}`i sessizce dusururdu.
    payment.slug = await allocate_slug(
        session,
        composite_slug(project.slug, sequence_no),
        ProgressPayment.slug,
    )
    if data.lines:
        payment.lines = await lines.build_lines(
            session,
            project,
            contract,
            data.lines,
            default_coefficient=default_coefficient,
            period=(data.period_year, data.period_month),
        )

    session.add(payment)
    await session.flush()
    await session.refresh(payment)
    return payment, project


# --- Düzenleme (spec §9.2, §7: yalnız draft) ---


async def update(
    session: AsyncSession, actor: User, payment_id: uuid.UUID, data: ProgressPaymentUpdate
) -> tuple[ProgressPayment, Project]:
    # 🔴 KRIT-HAKEDIS K5 — DURUM KAPISI KİLİT ALTINDA OKUNUR. Burası kilitsiz
    # `_visible_payment` kullanıyordu; aynı dosyadaki `save_lines` (:342) ve
    # `delete_payment` (:714) bu açığı K5 gerekçesiyle çoktan kapatmıştı, PATCH
    # taşınmamıştı. Kilitsiz okunan durum üzerinde verilen karar TOCTOU'dur:
    # eşzamanlı bir `approve` ile PATCH birbirini görmeden geçer ve dönem
    # değişimi aşağıdaki `restamp_for_period` ile ONAYLANMIŞ satırların
    # `quantity_source` damgasını yeniden yazar. Kilit sırası `create` /
    # `transitions.perform` / `save_lines` ile AYNI: önce sözleşme, sonra hakediş.
    payment, project, contract = await visible_payment_locked(session, actor, payment_id)
    if payment.status != ProgressPaymentStatus.draft:
        raise ConflictError(guards.INVALID_STATUS_TRANSITION)

    # `create` ile AYNI başlık kilidi (Y1): kural iki yazma yolunda da tek
    # kopyadan (`guards.validate_coefficient`) okunur — biri unutulursa PATCH
    # sessiz bir arka kapı olurdu. Sözleşme artık KİLİTLİ okunan satırdır
    # (`project.contract` lazy kopyası değil) — kapı bayat yüzdeyle karar vermez.
    if contract is not None:
        guards.validate_coefficient(
            data.default_coefficient,
            has_price_escalation=contract.has_price_escalation,
        )

    period_before = (payment.period_year, payment.period_month)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(payment, field, value)

    # SD-2 damgası hakedişin DÖNEMİNE bağlıdır (TB4 T5 bulgusu): dönem
    # değiştiğinde satırlar artık BAŞKA bir ayın günlüğüyle kıyaslanır, bu
    # yüzden mevcut damgalar bayat kalamaz. Dönem aynıysa sorgu HİÇ koşmaz —
    # yalnız `description` değiştiren PATCH gereksiz iş yapmaz.
    if (payment.period_year, payment.period_month) != period_before:
        await lines.restamp_for_period(session, payment, project.id)

    await session.flush()
    await session.refresh(payment)
    return payment, project


async def save_lines(
    session: AsyncSession, actor: User, payment_id: uuid.UUID, data: ProgressPaymentLinesSave
) -> tuple[ProgressPayment, Project, int]:
    """`PUT /progress-payments/{id}/lines` — DEĞİŞTİRME semantiği (spec §9.2/§10-2).

    Bu katman YALNIZ kapsam (§9.0) ve durum kapısını (§7) kurar; gövdenin
    doğrulaması ve uygulanması `lines.apply_lines`'tadır. Ayrım bilinçlidir:
    `lines.py` görünürlük katmanını (`_visible_payment`) çağırsaydı
    `service → lines → service` döngüsel importu doğardı (plan bu fonksiyonu
    `lines.save_lines` diye adlandırıyordu — sapma ve gerekçesi budur).

    🔴 **KRIT-HAKEDIS K5 — DURUM KAPISI KİLİT ALTINDA OKUNUR.**

    Kapının kendisi (`status != draft → 409`) eskiden de VARDI; açık olan şey
    kapının okuduğu durumun BAYAT olabilmesiydi. `_visible_payment` kilitsiz
    okur: eşzamanlı bir `approve` ile bu uç BİRBİRİNİ GÖRMEDEN geçebiliyordu —
    bu uç `draft` okur, `approve` `FOR UPDATE` alıp commit eder, bu uç hâlâ eski
    görüşüyle satırları YAZAR. Sonuç onaylanmış VE fişlenmiş bir hakedişin
    satırlarının değişmesidir; fişin tutarı donmuş olduğu için (`posting.py`:
    *fişin kendisi snapshot'tır*) mizan ile ekran SESSİZCE ayrışır ve hiçbir
    kolon farkı bunu ele vermez.

    `visible_payment_locked` kilit sırasını `create`/`transitions.perform`/
    `delete_payment` ile AYNI tutar (önce sözleşme, sonra hakediş) ve kilitli
    satırı `populate_existing=True` ile YENİDEN okur — kilit tek başına yetmez,
    ORM kimlik haritası kilitli satırın BAYAT alanlarını döndürebilirdi.

    🔴 Taşeron ikizi (`subcontractor_progress_payments.service.save_lines`) bu
    kilidi ZATEN alıyordu; iki aile ayrışmıştı ve simetri burada kurulur.

    İkinci öğe: gövdeden adreslenemediği için düşen **bağı kopmuş satır sayısı**
    (O3) — router bunu yanıtın `dropped_orphan_count` alanına taşır.
    """
    payment, project, contract = await visible_payment_locked(session, actor, payment_id)
    if payment.status != ProgressPaymentStatus.draft:
        raise ConflictError(guards.INVALID_STATUS_TRANSITION)
    if contract is None:
        raise SiteValidationError(guards.NO_EMPLOYER_CONTRACT)

    dropped_orphan_count = await lines.apply_lines(session, project, contract, payment, data.lines)
    await session.refresh(payment)
    return payment, project, dropped_orphan_count


# --- Hesap türevleri (spec §6.2-§6.4, §6.6, §8) — DB'den okunan tarihsel zincir ---
#
# Avans mahsubu KÜMÜLATİF TAVANLIDIR (spec §6.3): payment N'in tavanı, N'DEN
# ÖNCEKİ TÜM tamamlanmış hakedişlerin KENDİ tavanına göre kurtardığı avansa
# bağlıdır — basit toplam DEĞİL, `calculations.advance_deduction`'ın sıra ile
# zincirleme çağrısı (her adım bir öncekinin sonucunu besler).


# Zincirin gövdesi T3'te `calculations.py`ye TAŞINDI (plan T3: "kopya kod
# değil paylaşım"): taşeron hakedişi de aynı `gross_total`/`advance_or_uncapped`/
# `cumulative_state` üçlüsünü çağırır. Aşağıdaki isimler bu modülün
# çağıranlarını (özellikle `summary.service.cumulative_state`) KIRMAMAK için
# duran ince kabuklardır — ikinci bir hesap kopyası DEĞİLDİR.
CumulativeState = calculations.CumulativeState
cumulative_state = calculations.cumulative_state
_gross_total = calculations.gross_total
_advance_or_uncapped = calculations.advance_or_uncapped


def _history_state(
    prior_payments: list[ProgressPayment], contract_amount: Decimal | None
) -> tuple[Decimal, Decimal]:
    """`(advance_recovered, prior_gross_total)` — sıradaki hakedişin göreceği
    kümülatif durum (spec §6.3, §8 finansal ilerleme).

    Sorgu ARTIK BURADA KOŞMAZ (H4 denetimi O1): önceki tamamlanmış hakedişler
    çağıran tarafından toplu çekilir; bu fonksiyon yalnız zinciri okur.
    """
    state = cumulative_state(prior_payments, contract_amount)
    return state.advance_recovered, state.gross


def _prior_payments(
    completed: list[ProgressPayment], before_sequence_no: int
) -> list[ProgressPayment]:
    """§6.6 `prev` kümesi: `sequence_no` daha küçük tamamlanmış hakedişler.

    SQL'deki `sequence_no < N` süzgecinin bellek karşılığı — toplu çekim
    projenin TÜM tamamlanmış hakedişlerini getirir, sıra eşiği burada uygulanır.
    """
    return [p for p in completed if p.sequence_no < before_sequence_no]


def _calculation_block(
    payment: ProgressPayment, contract: ProjectContract, advance_recovered: Decimal
) -> PaymentCalculationBlock:
    """E15 151-172 / OLU 179-196 ödeme hesabı — liste VE detay tarafından paylaşılır.

    `advance_recovered` çağıran tarafından `_history_state`'ten önceden okunur
    (O2, H4 denetimi): `get_detail` hem bunu hem `_progress_block`'un ihtiyaç
    duyduğu `prior_gross_total`'ı AYNI `_history_state` çağrısından karşılar —
    aynı argümanlarla iki kez sorgu koşmaz.
    """
    gross = _gross_total(payment.lines)
    vat = calculations.vat_amount(gross, payment.vat_pct)
    advance = _advance_or_uncapped(gross, payment.advance_pct, contract.amount, advance_recovered)
    retention = calculations.retention_amount(gross, payment.retainage_pct)
    net = calculations.net_amount(gross, vat, advance, retention)
    return PaymentCalculationBlock(
        gross=gross, vat=vat, advance_deduction=advance, retention=retention, net=net
    )


# --- Liste (spec §9.1) ---


async def list_payments(
    session: AsyncSession,
    actor: User,
    *,
    project_id: uuid.UUID | None,
    site_id: uuid.UUID | None,
    status_filter: ProgressPaymentStatus | None,
) -> ProgressPaymentListResponse:
    """Geçmiş TOPLU çekilir (H4 denetimi O1): listedeki hakedişlerin projeleri
    için tamamlanmış hakedişler TEK sorguda okunur ve bellekte `project_id`'ye
    göre gruplanır. Hakediş başına ayrı geçmiş sorgusu KOŞMAZ — 5 hakediş için
    27 sorgu üreten eski yol ~60 hakedişli projede ~300 sorguya çıkıyordu.

    Kapsam SQL'de kalır: toplu çekimin proje kümesi `visible_projects`'ten
    süzülmüş satırlardan türer, ikinci bir görünürlük kararı VERİLMEZ (§9.0).
    """
    # IZN-B3: disiplinle KISITLI projelerin hakedisleri listeden DISARIDA kalir (Ü2; tek-proje
    # ucu 403 verir) — liste 403 vermez, proje basina suzer.
    kisitli = await restricted_project_ids(session, actor.id)
    visible_ids = [p.id for p in await visible_projects(session, actor) if p.id not in kisitli]
    rows = await repository.list_payments(
        session, visible_ids, project_id=project_id, site_id=site_id, status_filter=status_filter
    )
    completed_by_project = await repository.list_completed_payments_by_projects(
        session, sorted({payment.project_id for payment, _ in rows})
    )
    items = []
    for payment, project in rows:
        prior = _prior_payments(
            completed_by_project.get(payment.project_id, []), payment.sequence_no
        )
        advance_recovered, _ = _history_state(prior, project.contract.amount)
        calc = _calculation_block(payment, project.contract, advance_recovered)
        items.append(
            ProgressPaymentListItem(
                id=payment.id,
                # 🔴 LİSTEYE DE GİRER: `hakedisler/[paymentId]` bağlantısını
                # üreten şema budur (URL-2'nin "seçici slug üretemedi" tuzağı).
                slug=payment.slug,
                project_id=payment.project_id,
                project_name=project.name,
                sequence_no=payment.sequence_no,
                period_year=payment.period_year,
                period_month=payment.period_month,
                description=payment.description,
                status=payment.status,
                gross_total=calc.gross,
                net_total=calc.net,
            )
        )
    return ProgressPaymentListResponse(items=items)


# --- Detay (spec §9.1, §6.6, §8, §5.1) ---


def _line_detail(
    line: ProgressPaymentLine,
    previous: tuple[Decimal, Decimal],
    live_item: EmployerContractItem | None,
) -> ProgressPaymentLineDetail:
    """TEK satırın E15 96-141 görünümü (§6.6 türevleri + §5.1 bayrağı).

    `is_price_stale`: bağ koptuysa (`live_item is None`) `None` — kıyaslanacak
    canlı fiyat yoktur (spec §5.1).
    """
    prev_qty, prev_amt = previous
    this_total = calculations.line_total(line.contract_unit_price, line.coefficient, line.quantity)
    return ProgressPaymentLineDetail(
        id=line.id,
        contract_item_id=line.contract_item_id,
        site_id=line.site_id,
        code=line.code,
        source_code=line.source_code,
        description=line.description,
        unit=line.unit,
        contract_unit_price=line.contract_unit_price,
        coefficient=line.coefficient,
        quantity=line.quantity,
        group_name=line.group_name,
        sort_order=line.sort_order,
        quantity_source=line.quantity_source,
        adjusted_unit_price=calculations.adjusted_unit_price(
            line.contract_unit_price, line.coefficient
        ),
        line_total=this_total,
        previous_quantity=prev_qty,
        previous_amount=prev_amt,
        cumulative_quantity=prev_qty + line.quantity,
        cumulative_amount=prev_amt + this_total,
        is_price_stale=(
            None if live_item is None else line.contract_unit_price != live_item.unit_price
        ),
    )


def _group_summaries(
    rows: list[tuple[ProgressPaymentLineDetail, Decimal]],
) -> list[ProgressPaymentGroupSummary]:
    """E15 96-141'in grup düzeyinde toplulaştırması (spec §6.6): satır
    detayları + satır başına sözleşme tutarı payı `group_name` altında toplanır.
    """
    totals: dict[str | None, dict[str, Decimal]] = {}
    for detail, contract_amount_for_item in rows:
        group = totals.setdefault(
            detail.group_name,
            {
                "previous_amount": _ZERO,
                "this_amount": _ZERO,
                "cumulative_amount": _ZERO,
                "contract_amount": _ZERO,
            },
        )
        group["previous_amount"] += detail.previous_amount
        group["this_amount"] += detail.line_total
        group["cumulative_amount"] += detail.cumulative_amount
        group["contract_amount"] += contract_amount_for_item
    return [
        ProgressPaymentGroupSummary(group_name=group_name, **group)
        for group_name, group in totals.items()
    ]


async def _line_rows(
    session: AsyncSession,
    payment: ProgressPayment,
    prior_payments: list[ProgressPayment],
) -> tuple[list[ProgressPaymentLineDetail], list[ProgressPaymentGroupSummary], Decimal]:
    """Satır detayları + grup toplulaştırması + §8 fiziksel ilerleme payı
    (`Σ cumulative_quantity × canlı unit_price`).

    `prior_payments` ÇAĞIRANDAN gelir (H4 denetimi O1): E15 "Önceki" kolonu ile
    §6.3 avans zinciri AYNI çekimi paylaşır — eskiden ikisi aynı sorguyu ayrı
    ayrı koşuyordu. Toplama kuralı yine `lines.totals_from_payments`'ın TEK
    kopyasından okunur (§6.6 sıra tabanlı mod; kota tavanının sırasız modu
    `lines.completed_totals` üzerinden ayrı kalır, H6 denetimi K1).
    """
    prior_totals = lines.totals_from_payments(prior_payments)
    item_ids = [
        line.contract_item_id for line in payment.lines if line.contract_item_id is not None
    ]
    live_items = await repository.get_employer_items_by_ids(session, item_ids)

    rows: list[tuple[ProgressPaymentLineDetail, Decimal]] = []
    physical_numerator = _ZERO
    for line in payment.lines:
        live_item = (
            live_items.get(line.contract_item_id) if line.contract_item_id is not None else None
        )
        key = (line.contract_item_id, line.site_id) if line.contract_item_id is not None else None
        previous = prior_totals.get(key, (_ZERO, _ZERO)) if key is not None else (_ZERO, _ZERO)
        detail = _line_detail(line, previous, live_item)
        contract_amount_for_item = _ZERO
        if live_item is not None:
            contract_amount_for_item = live_item.quantity * live_item.unit_price
            physical_numerator += detail.cumulative_quantity * live_item.unit_price
        rows.append((detail, contract_amount_for_item))

    return [detail for detail, _ in rows], _group_summaries(rows), physical_numerator


async def _progress_block(
    session: AsyncSession,
    project: Project,
    contract: ProjectContract,
    gross: Decimal,
    physical_numerator: Decimal,
    prior_gross_total: Decimal,
) -> ProgressBlock:
    """E15 177-190 (spec §8). Eksik veri → `None` (zarif düşüş).

    `prior_gross_total` çağıran tarafından geçirilir (O2, H4 denetimi) — `get_detail`
    aynı `_history_state` sonucunu `_calculation_block`'un `advance_recovered`'ıyla
    PAYLAŞIR, burada YENİDEN sorgulanmaz.
    """
    cumulative_gross = prior_gross_total + gross

    financial_pct = None
    if contract.amount is not None and contract.amount > 0:
        financial_pct = calculations.quantize2(cumulative_gross / contract.amount * _HUNDRED)

    physical_pct = None
    denominator = await repository.get_contract_items_total_value(session, project.id)
    if denominator > 0:
        physical_pct = calculations.quantize2(physical_numerator / denominator * _HUNDRED)

    duration = calculations.duration_pct(project.start_date, project.end_date, today())
    return ProgressBlock(
        financial_pct=financial_pct, physical_pct=physical_pct, duration_pct=duration
    )


async def get_detail(
    session: AsyncSession, actor: User, payment_ref: uuid.UUID | str
) -> ProgressPaymentDetail:
    """Kapsam süzgeci + detay inşası (spec §9.0, §9.1).

    İnce sarmalayıcıdır (H4 denetimi O3): `(payment, project)` çiftini ZATEN
    çözmüş olan çağıranlar (`POST`/`PATCH` uçları) `build_detail`'i doğrudan
    çağırır — aksi hâlde `visible_projects` kapsam sorgusu istek başına İKİ KEZ
    koşardı.
    """
    payment, project = await _visible_payment(session, actor, payment_ref)
    return await build_detail(session, payment, project)


async def build_detail(
    session: AsyncSession, payment: ProgressPayment, project: Project
) -> ProgressPaymentDetail:
    """E15 ekranının tamamı — GÖRÜNÜRLÜK KONTROLÜ YAPMAZ.

    Çağıranın kapsam kararını (`_visible_project`/`_visible_payment`) çoktan
    vermiş olması ŞARTTIR: bu ayrım O3'ün çözümüdür, korumanın gevşetilmesi
    değil. Yeni bir çağıran eklenirken çifti mutlaka kapsam süzgecinden
    geçirilmiş bir yoldan almalıdır.
    """
    # D8 sayesinde bir hakedişin var olması sözleşmenin de var olduğunu garanti
    # eder (`project_contracts` CASCADE'i hakedişleri de götürür) — `contract`
    # burada asla None DEĞİLDİR.
    contract = project.contract

    prior_payments = await repository.list_completed_payments(
        session, project.id, before_sequence_no=payment.sequence_no
    )
    line_rows, groups, physical_numerator = await _line_rows(session, payment, prior_payments)
    advance_recovered, prior_gross_total = _history_state(prior_payments, contract.amount)
    calc = _calculation_block(payment, contract, advance_recovered)
    progress = await _progress_block(
        session, project, contract, calc.gross, physical_numerator, prior_gross_total
    )

    return ProgressPaymentDetail(
        id=payment.id,
        slug=payment.slug,
        project_id=payment.project_id,
        project_name=project.name,
        sequence_no=payment.sequence_no,
        period_year=payment.period_year,
        period_month=payment.period_month,
        description=payment.description,
        status=payment.status,
        vat_pct=payment.vat_pct,
        advance_pct=payment.advance_pct,
        retainage_pct=payment.retainage_pct,
        default_coefficient=payment.default_coefficient,
        submitted_at=payment.submitted_at,
        approved_at=payment.approved_at,
        approved_by=payment.approved_by,
        paid_at=payment.paid_at,
        created_by=payment.created_by,
        created_at=payment.created_at,
        updated_at=payment.updated_at,
        lines=line_rows,
        groups=groups,
        calculation=calc,
        progress=progress,
    )


# --- Silme (spec §7.1, §9.5, K8) ---


async def delete_payment(
    session: AsyncSession, actor: User, payment_id: uuid.UUID
) -> DeletedPaymentSummary:
    """`DELETE /progress-payments/{id}` — K8'in İKİ KATMANLI kuralı.

    Katman 1 (§7.1/1): `status ∈ {approved, paid}` → 409 `PAYMENT_NOT_DELETABLE`
    — ADMİN DAHİL kimse silemez (kalıcı karar 2'nin "silme = admin" ilkesinin
    DARALTILMASI, admin ihlali değil: kalan silinebilir kümede admin koşulsuz
    siler). Muhasebeleşmiş evrak yok edilmez; admin gerekirse önce `unapprove`
    ile (H6) durumu `pending_approval`'a geri çeker — denetim izli iki adım.

    Katman 2 (SIL-B1, K4): kapı router'da yalnız Sistem Yöneticisi'dir; eski "kendi taslağını
    sahibi siler" (`can_delete`) istisnası KALDIRILDI.

    Kapsam (§9.0) `_visible_payment` ile İLK adımda kurulur: görünmeyen
    projedeki GERÇEK kayıt ile var olmayan kimlik burada da AYIRT EDİLEMEZ
    404'tür — durum/yetki kontrolleri görünürlükten SONRA çalışır.

    Silme de bir YAZMA işlemidir (H8 denetimi K1, 2026-07-31): satır kilitsiz
    okunursa (`_visible_payment`) eşzamanlı bir `approve` katman-1 kontrolünü
    TOCTOU ile atlatıp `approved`/`paid` kaydı silebilir. Bu yüzden burada da
    `visible_payment_locked` kullanılır — kilit sırası `create`/`transitions`
    ile AYNIDIR (önce sözleşme, sonra hakediş), durum ve `can_delete`
    kontrolleri KİLİTLİ satır üzerinden yapılır. Yarışta satır zaten silinmişse
    `visible_payment_locked` mevcut `PAYMENT_MISSING` 404'ünü üretir.

    Denetim günlüğü (H8'den devredilen not, plan H10, spec §11): dönüş değeri
    kaydın `session.delete`den ÖNCE çıkarılmış özetidir (`sequence_no`/durum/
    tutar) — kayıt gittiğinde bunlar bir daha okunamaz.
    """
    payment, project, _ = await visible_payment_locked(session, actor, payment_id)

    if payment.status in (ProgressPaymentStatus.approved, ProgressPaymentStatus.paid):
        raise ConflictError(guards.PAYMENT_NOT_DELETABLE)

    # H8'den devredilen ZORUNLULUK (plan H10, spec §11): özet `session.delete`
    # ÖNCESİNDE kurulur. Mutasyon denetimi (H10) bu okumayı silmeden SONRAKİ bir
    # yeniden sorguya (`repository.get_payment`) taşıyarak doğrulandı: aynı
    # transaction kendi silme işlemini gördüğü için satır bulunamaz ve kod
    # sessizce varsayılanlara düşer (#0/"Bilinmiyor"/0.00 TL, HATA FIRLAMADAN) —
    # test kırmızıya döndü, kanıt raporda. Doğru sıra ile geri alındı.
    summary = DeletedPaymentSummary(
        project_name=project.name,
        sequence_no=payment.sequence_no,
        status_label=_STATUS_LABELS[payment.status],
        amount=_gross_total(payment.lines),
    )

    # `ProgressPayment.lines` cascade="all, delete-orphan" (H1) — satırlar
    # bu `session.delete` ile BİRLİKTE gider, ayrı bir silme çağrısı gerekmez.
    await session.delete(payment)
    await session.flush()
    return summary

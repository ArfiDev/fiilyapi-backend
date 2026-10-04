"""Ünite satışı servisi — CRUD + `units.sales_status` senkronu (P8 spec §3, §4).

## Denetim günlüğü (B5 / `units/service.py` deseni)

Yazma fonksiyonları sonucun YANINDA hazır denetim METNİNİ döner; satırı
`record_audit` ile yazan router'dır. Silmede metin kayıt yok olmadan ÖNCE
kurulmak zorundadır — router silinen satışın ünite etiketini ve alıcı adını
hiçbir sorguyla geri getiremez.

## Modülün iş bölümü

Ödeme planı **T4**'te `installments.py`ye, durum geçişleri (`activate` /
`transfer-deed` / `cancel`) **T5**'te `transitions.py`ye ayrıldı; `transitions`
kendi ünite haritasını YAZMAZ, buradaki `sync_unit_sales_status` yardımcısını
ÇAĞIRIR (aşağıdaki nota bakınız). Özetin (T5) saf toplaması `summary.py`dedir.
"""

import uuid
from decimal import Decimal

from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import timezone
from app.core.errors import DuplicateError, SiteValidationError
from app.modules.audit import messages
from app.modules.projects import costs
from app.modules.projects import repository as projects_repository
from app.modules.projects.schemas import MetricPlaceholder, metric
from app.modules.sales import guards, repository, summary
from app.modules.sales.models import SaleType, UnitSale, UnitSaleStatus
from app.modules.sales.repository import InstallmentStats
from app.modules.sales.schemas import (
    COST_MODULE,
    SALE_FORM_FIELDS,
    SalesSummaryResponse,
    UnitSaleCreate,
    UnitSaleListResponse,
    UnitSaleResponse,
    UnitSaleTotals,
    UnitSaleUpdate,
    unit_label,
)

# İzinli (`on_leave`) personel ATANABİLİR: kural `sites` modülünde tanımlıdır
# (karar 2026-07-30, `get_assignable_user` docstring'i) ve BURADA KOPYALANMAZ —
# iki ayrı "atanabilir kullanıcı" tanımı zamanla ayrışır.
from app.modules.sites.repository import get_assignable_user
from app.modules.units.models import Unit, UnitSalesStatus
from app.modules.units.repository import list_units_for_project
from app.modules.users.models import User

__all__ = [
    "create_sale",
    "delete_sale",
    "get_sale",
    "list_sales",
    "sales_summary",
    # T5'in geçiş uçları (`transitions.perform`) yanıt zarfını BURADAN kurar —
    # ikinci bir `UnitSaleResponse` inşa yolu açılmaz.
    "response_for",
    "sync_unit_sales_status",
    "unit_status_for",
    "update_sale",
]

_ZERO = Decimal("0.00")

# `UnitSale`in NOT NULL sütunları: PATCH'te `null` ile boşaltılamazlar.
_NOT_NULL_FIELDS = ("sale_price", "has_condominium_easement", "has_mortgage")

# --- `units.sales_status` otomasyonu (spec §3) ---
#
# `units/models.py:232-240`'ın tarif ettiği geçişin TEK OTORİTESİ burasıdır.
# T5'in geçiş uçları (`activate`/`transfer-deed`/`cancel`) bu haritayı
# KOPYALAMAZ, `sync_unit_sales_status`u çağırır: kopyalanan bir eşleme zamanla
# ayrışır ve ayrışan taraf üniteyi yanlış vitrinde bırakır.
_UNIT_STATUS_BY_SALE_STATUS: dict[UnitSaleStatus, UnitSalesStatus] = {
    UnitSaleStatus.reservation: UnitSalesStatus.reserved,
    UnitSaleStatus.active: UnitSalesStatus.sold,
    # Tapu devri üniteyi `sold`ta BIRAKIR: `UnitSalesStatus`ta "Tapulu" değeri
    # YOKTUR (units/models.py notu) — S haritası "Tapulu"yu satış kaydının
    # durumundan okur, ünitenin vitrin durumundan değil.
    UnitSaleStatus.deed_transferred: UnitSalesStatus.sold,
    UnitSaleStatus.cancelled: UnitSalesStatus.listed,
}

# `sale_type` → başlangıç durumu (T1 model notu: sunucu varsayılanı YOK).
# "Ön Sözleşme" (F56) bir kapora DEĞİLDİR: taraflar bedelde anlaşmıştır, bu
# yüzden `active` ile açılır ve ünite `sold` olur.
_INITIAL_STATUS_BY_SALE_TYPE: dict[SaleType, UnitSaleStatus] = {
    SaleType.sale: UnitSaleStatus.active,
    SaleType.reservation: UnitSaleStatus.reservation,
    SaleType.pre_contract: UnitSaleStatus.active,
}


def unit_status_for(sale_status: UnitSaleStatus | None) -> UnitSalesStatus:
    """`None` = ünitede AÇIK satış kaydı yok → vitrine (`listed`) döner.

    Silme yolu da bu dalı kullanır: rezervasyon kaydı silindiğinde ünite
    `reserved`ta kalsaydı bir daha satılamazdı.
    """
    if sale_status is None:
        return UnitSalesStatus.listed
    return _UNIT_STATUS_BY_SALE_STATUS[sale_status]


async def sync_unit_sales_status(
    session: AsyncSession, unit: Unit, sale_status: UnitSaleStatus | None
) -> None:
    """Ünitenin vitrin durumunu satış kaydının durumundan TÜRETİR (spec §3)."""
    unit.sales_status = unit_status_for(sale_status)
    await session.flush()


# --- Yanıt zarfı ---


def _cost_metrics(
    unit: Unit, sale_price: Decimal, allocation: costs.UnitCostAllocation | None
) -> dict[str, MetricPlaceholder]:
    """DS 62/90 — "Maliyet" ve "Bu Satıştan Kâr" zarfları (P10 T3).

    Kolon AÇILMADI (kalıcı karar 3 yaşıyor): maliyet ünitenin brüt m²'sinden
    TÜREVDİR (S3) ve kâr `costs.sale_profit`tan gelir — bu dosya ikinci bir
    "satıştan kâr" formülü yazmaz.

    DS 91'in %31,9 MARJI için alan AÇILMAZ: `UnitSaleResponse`ta marj kolonu
    yoktur ve kâr/bedel oranı ekranda tek satırda türetilir (spec §5 "başabaş
    noktası türev metin, backend alan açmaz" kuralının aynısı).

    Kaynak yoksa (m²'siz ünite, bütçesi girilmemiş proje) iki zarf da BOŞ kalır.
    """
    unit_cost = None if allocation is None else allocation.for_unit(unit.gross_area_m2)
    profit = costs.sale_profit(sale_price, unit_cost).profit
    return {
        "unit_cost": metric(unit_cost, COST_MODULE),
        "sale_profit": metric(profit, COST_MODULE),
    }


def _to_response(
    row: Row, stats: InstallmentStats, allocation: costs.UnitCostAllocation | None = None
) -> UnitSaleResponse:
    sale, unit, block, customer = row
    return UnitSaleResponse(
        **{
            field: getattr(sale, field)
            for field in UnitSaleResponse.model_fields
            if hasattr(sale, field)
        },
        **_cost_metrics(unit, sale.sale_price, allocation),
        block_name=block.name,
        unit_no=unit.unit_no,
        unit_label=unit_label(block.name, unit.unit_no),
        customer_name=customer.name,
        customer_type=customer.customer_type,
        customer_national_id=customer.national_id,
        customer_tax_number=customer.tax_number,
        paid_amount=stats.paid_amount,
        remaining_amount=sale.sale_price - stats.paid_amount,
        installment_total=stats.installment_total,
        installment_paid_count=stats.installment_paid_count,
        overdue_installment_count=stats.overdue_count,
    )


async def _sale_row(session: AsyncSession, sale_id: uuid.UUID) -> Row:
    """Kaydı etiketleriyle (ünite + blok + alıcı) okur.

    `row` None olamaz — kayıt aynı transaction içinde görüldü; koşul yalnızca
    tip daraltmasıdır, sessiz bir düşüş değildir.
    """
    row = await repository.get_sale_row(session, sale_id)
    if row is None:  # pragma: no cover - FK + aynı transaction garantisi
        raise guards.NotFoundError(guards.SALE_MISSING)
    return row


async def _cost_allocation(
    session: AsyncSession, project_id: uuid.UUID
) -> costs.UnitCostAllocation | None:
    """Ünite maliyeti dağıtımının proje bağlamı (P10 T3, S3).

    Payda projenin TÜM ünitelerinin brüt m² toplamıdır, bu yüzden satış yanıtı
    ünite tablosuna bir kez uğramak zorundadır. Sorgular satış sayısıyla
    BÜYÜMEZ: bağlam istek başına bir kez kurulur (spec §4).
    """
    project = await projects_repository.get_project(session, project_id)
    if project is None:  # pragma: no cover - FK garantisi
        return None
    return costs.allocation(project, await list_units_for_project(session, project_id))


async def response_for(session: AsyncSession, sale_id: uuid.UUID) -> UnitSaleResponse:
    stats = await repository.installment_stats(session, [sale_id], timezone.today())
    row = await _sale_row(session, sale_id)
    allocation = await _cost_allocation(session, row[0].project_id)
    return _to_response(row, stats.get(sale_id, InstallmentStats()), allocation)


# --- Okuma uçları ---


async def list_sales(
    session: AsyncSession, actor: User, project_id: uuid.UUID
) -> UnitSaleListResponse:
    """S150-212. Tahsilat türevleri TEK toplama sorgusundan gelir (N+1 yok).

    Maliyet/kâr (DS 62/90) de TEK dağıtım bağlamından gelir — satış başına ünite
    sorgusu AÇILMAZ (P10 T3, spec §4).
    """
    project = await guards.visible_project(session, actor, project_id)
    rows = await repository.list_sale_rows(session, project_id)
    stats = await repository.installment_stats(
        session, [row[0].id for row in rows], timezone.today()
    )
    allocation = costs.allocation(project, await list_units_for_project(session, project_id))
    items = [
        _to_response(row, stats.get(row[0].id, InstallmentStats()), allocation) for row in rows
    ]
    return UnitSaleListResponse(
        totals=UnitSaleTotals(
            count=len(items),
            sale_price_total=sum((item.sale_price for item in items), start=_ZERO),
            paid_total=sum((item.paid_amount for item in items), start=_ZERO),
            remaining_total=sum((item.remaining_amount for item in items), start=_ZERO),
        ),
        items=items,
    )


async def sales_summary(
    session: AsyncSession, actor: User, project_id: uuid.UUID
) -> SalesSummaryResponse:
    """S55-59 + S218-234 (T5). Toplama SAF çekirdekte (`summary.py`) yapılır.

    ÜÇ sorgu atılır ve daha fazlası GEREKMEZ: (1) iptal edilmemiş satışlar
    etiketleriyle, (2) o satışların TÜM plan satırları tek `IN` sorgusunda,
    (3) projenin üniteleri ("Boş Ünite" satış tablosundan sayılamaz — boş
    ünitenin satış kaydı yoktur). Satış başına ek SELECT atmak N+1 olurdu.

    `timezone.today()` BURADA — servis sınırında — çağrılır ve aşağı geçirilir:
    saf çekirdek saati okumaz (`summary.py` notu).
    """
    await guards.visible_project(session, actor, project_id)
    sale_rows = await repository.list_sale_rows(session, project_id, exclude_cancelled=True)
    installment_rows = await repository.list_installments_for_sales(
        session, [row[0].id for row in sale_rows]
    )
    units = await list_units_for_project(session, project_id)
    return summary.build_summary(
        project_id=project_id,
        sale_rows=sale_rows,
        installments=installment_rows,
        units=units,
        today=timezone.today(),
    )


async def get_sale(session: AsyncSession, actor: User, sale_id: uuid.UUID) -> UnitSaleResponse:
    sale, _ = await guards.visible_sale(session, actor, sale_id)
    return await response_for(session, sale.id)


# --- Yazma uçları ---


async def _resolve_advisor_name(session: AsyncSession, user_id: uuid.UUID | None) -> str | None:
    """F75 danışman adının anlık görüntüsü — `sites._resolve_user_name` deseni.

    Yok ya da pasifse **422** (404 DEĞİL): istenen kaynak SATIŞTIR, kullanıcı
    burada bir ALAN DEĞERİDİR. 404 dönmek "bu UUID'li kullanıcı yok" bilgisini
    satış ucundan sızdırmak olurdu.
    """
    if user_id is None:
        return None
    user = await get_assignable_user(session, user_id)
    if user is None:
        raise SiteValidationError(guards.USER_NOT_FOUND)
    return user.full_name


def _is_open_sale_conflict(exc: IntegrityError) -> bool:
    """`uq_unit_sales_open_unit` ihlali mi? (yarış durumu emniyet ağı)

    Kısıt ADINA bakılır: her `IntegrityError`ı "bu ünitede zaten açık satış var"a
    çevirmek, ilgisiz bir FK ihlalini yanlış Türkçe mesajla göstermek olurdu.
    """
    return getattr(exc.orig, "constraint_name", None) == "uq_unit_sales_open_unit"


async def create_sale(
    session: AsyncSession, actor: User, project_id: uuid.UUID, data: UnitSaleCreate
) -> tuple[UnitSaleResponse, str]:
    """Sıra ÖNEMLİDİR: görünürlük → ünite kapısı → alıcı → teklik → yazma.

    Doğrulamaların tamamı HİÇBİR ŞEY YAZILMADAN ÖNCE koşar (`sites` §8.2 kuralı):
    danışman çözümü 422 üretebilir ve satış satırı session'a girmiş olsaydı
    kısmi yazımın geri alınması tek başına istek transaction'ına kalırdı.
    """
    project = await guards.visible_project(session, actor, project_id)
    unit = await guards.unit_in_project(session, project, data.unit_id)
    guards.ensure_unit_sellable(unit)
    customer = await guards.existing_customer(session, data.customer_id)
    await guards.ensure_no_open_sale(session, unit.id)
    advisor_name = await _resolve_advisor_name(session, data.advisor_user_id)

    sale = UnitSale(
        project_id=project.id,
        unit_id=unit.id,
        customer_id=customer.id,
        sale_type=data.sale_type,
        status=_INITIAL_STATUS_BY_SALE_TYPE[data.sale_type],
        # F84 mockup'ta `readonly`: fiyat İSTEMCİDEN DEĞİL üniteden alınır ki
        # ünite fiyatı sonradan değişse bile satış belgesi değişmesin.
        list_price_snapshot=unit.list_price,
        sale_price=data.sale_price,
        advisor_name=advisor_name,
        created_by=actor.id,
        **data.model_dump(include=set(SALE_FORM_FIELDS)),
    )
    session.add(sale)
    try:
        await session.flush()
    except IntegrityError as exc:
        if not _is_open_sale_conflict(exc):
            raise
        raise DuplicateError(guards.UNIT_ALREADY_SOLD) from exc

    await sync_unit_sales_status(session, unit, sale.status)
    response = await response_for(session, sale.id)
    return response, messages.sale_created(
        project.name, response.unit_label, response.customer_name
    )


async def update_sale(
    session: AsyncSession, actor: User, sale_id: uuid.UUID, data: UnitSaleUpdate
) -> tuple[UnitSaleResponse, str]:
    """Kısmi güncelleme: GÖNDERİLMEYEN alan değişmez, `null` gönderilen boşalır.

    `status`, `sale_type` ve `unit_id` şemada YOKTUR (bkz. `schemas.py`
    tablosu) → gövdede gelseler bile Pydantic'in `extra='ignore'` varsayılanıyla
    düşerler. Bu yüzden burada ünite senkronu ÇALIŞTIRILMAZ: bu uçtan geçen
    hiçbir alan ünitenin vitrin durumunu değiştiremez.
    """
    sale, project = await guards.visible_sale(session, actor, sale_id)
    updates = data.model_dump(exclude_unset=True)

    # Bedel TAHSİL EDİLMİŞ paranın altına indirilemez (T4 para değişmezi).
    # Kilit doğrulamayı besleyen OKUMADAN ÖNCE (`installments.save_installments`
    # ile aynı sıra kanonu): aksi hâlde eş zamanlı bir `pay` araya girer ve
    # bedel, tahsilat toplamının altında kalırdı.
    if updates.get("sale_price") is not None:
        await repository.lock_installments(session, sale.id)
        tahsilat = sum(
            (row.paid_amount for row in await repository.list_installments(session, sale.id)),
            _ZERO,
        )
        if updates["sale_price"] < tahsilat:
            raise SiteValidationError(guards.SALE_PRICE_BELOW_COLLECTED)

    if "advisor_user_id" in updates:
        # Ad, FK ile BİRLİKTE tazelenir: ikisini ayrı ayrı güncellenebilir
        # bırakmak, kayıtta bir kullanıcıya ait FK ile başkasının adını yan yana
        # bırakırdı. FK `null`lanırsa ad da temizlenir.
        sale.advisor_name = await _resolve_advisor_name(session, updates["advisor_user_id"])

    for field, value in updates.items():
        # NOT NULL sütunlar `null` ile boşaltılamaz; nullable olanlar boşalır
        # (`units.update_unit` ile aynı ayrım).
        if value is None and field in _NOT_NULL_FIELDS:
            continue
        setattr(sale, field, value)
    await session.flush()

    response = await response_for(session, sale.id)
    return response, messages.sale_updated(
        project.name, response.unit_label, response.customer_name
    )


async def delete_sale(session: AsyncSession, actor: User, sale_id: uuid.UUID) -> str:
    """Spec §4: YALNIZ `reservation` silinir — `active`/`deed_transferred` iptal

    edilir (T5 `cancel`). Muhasebeleşmiş bir satışı yok etmek geri alınamaz veri
    kaybıdır; iptal ise denetim izi bırakır ve üniteyi yine serbest bırakır.

    Kapı router'da `require_system_admin`dir (SIL-B1, K4): `can_delete` ve taslak istisnası
    KALDIRILDI; bu fonksiyon yalnız satışın DURUM kuralını (409) uygular.

    Silinen rezervasyon ünitenin `sales_status`unu `listed`e DÖNDÜRÜR: aksi
    hâlde ünite kimsenin satamayacağı bir `reserved` durumunda kalırdı.
    """
    sale, project = await guards.visible_sale(session, actor, sale_id)
    guards.ensure_deletable_status(sale.is_draft)

    _, unit, block, customer = await _sale_row(session, sale.id)
    # `units/service.delete_unit` ile aynı gerekçe: metin satır yok olmadan
    # ÖNCE kurulur — sonrasında etiket ve alıcı adı geri getirilemez.
    detail = messages.sale_deleted(
        project.name, unit_label(block.name, unit.unit_no), customer.name
    )
    await session.delete(sale)
    await session.flush()
    await sync_unit_sales_status(session, unit, None)
    return detail

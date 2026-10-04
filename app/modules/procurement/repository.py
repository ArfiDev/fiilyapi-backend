"""Satinalma veri erisimi (T2) — yalniz SQL, yetki/kapsam KARARI yok.

Kapsam karari (`visible_projects`) bu katmanda DEGIL `service.py`dedir
(`inventory/repository.py` deseninin kardesi); buraya yalniz cozulmus proje
kimlikleri gelir.

Liste ve sayim AYNI suzgec yardimcisini paylasir (`personnel` deseni): kopya
acilsaydi `total` ile gosterilen tablo zamanla ayrisirdi.

## N+1 YASAKTIR — uc turev de TOPLU sorgudur

* "Bu Yil Toplam Siparis" → tedarikci basina degil, `GROUP BY supplier_id` ile
  TEK alt sorgu (`supplier_order_totals`).
* Talebin tahmini toplami + kalem sayisi → talep basina degil,
  `GROUP BY request_id` ile TEK alt sorgu (`request_totals`).
* Kalemlerin "Mevcut Stok"u → kalem basina degil, sayfadaki TUM kart
  kimlikleri tek `IN` ile (`current_stock_by_item`).

## ST'ye TEK YONLU import

Bu modul `inventory`den `balance.legs` ve `StockItem`i okur; `inventory` ise
`procurement`i **ASLA** import etmez. Ters yon acilsaydi P10'un `cost_cards`
import cemberi tekrarlanirdi (T1 modul docstring'i).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Row, Select, func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Subquery

from app.core.access import AccessLevel
from app.core.page_gate import effective_level
from app.modules.inventory import balance as stock_balance
from app.modules.inventory import repository as inventory_repository
from app.modules.inventory.models import StockItem
from app.modules.procurement.guards import PERMISSION_MODULE
from app.modules.procurement.models import (
    PurchaseOrder,
    PurchaseOrderStatus,
    PurchasePriority,
    PurchaseQuote,
    PurchaseRequest,
    PurchaseRequestLine,
    PurchaseRequestStatus,
    Supplier,
)
from app.modules.users.models import User


def _like_escape(deger: str) -> str:
    """LIKE joker karakterlerini KACIRIR (`inventory.repository` deseni).

    Kacirilmazsa arama kutusuna `%` yazan kullanici TUM katalogu, `_` yazan ise
    beklemedigi satirlari gorur. Kacis karakterinin kendisi ONCE kacirilir.
    """
    return deger.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- Tedarikci (TED) ---


def _supplier_filtered(
    stmt: Select, *, q: str | None, category: str | None, is_active: bool | None
) -> Select:
    """TED'in uc suzgeci: arama kutusu · kategori · aktiflik.

    `q` AD ve KATEGORI uzerinde kismi arar: TED karti ikisini ust uste basar,
    tek alanda aramak kullaniciyi "Demirsan yok" sanisina dusururdu.

    `is_active` GONDERILMEZSE suzgec uygulanmaz — pasif tedarikci sessizce
    gizlenmez; ekran hangi kumeyi istedigini acikca soyler (`personnel` karari).
    """
    if q:
        desen = f"%{_like_escape(q)}%"
        stmt = stmt.where(
            Supplier.name.ilike(desen, escape="\\") | Supplier.category.ilike(desen, escape="\\")
        )
    if category is not None:
        stmt = stmt.where(Supplier.category == category)
    if is_active is not None:
        stmt = stmt.where(Supplier.is_active.is_(is_active))
    return stmt


def supplier_order_totals(project_ids: list[uuid.UUID], year: int) -> Subquery:
    """ "Bu Yil Toplam Siparis" (TED 52) — tedarikci basina TEK GRUPLU alt sorgu.

    **YIL OLCUTU `created_at`tir:** `purchase_orders`ta ayri bir siparis TARIHI
    kolonu YOKTUR (spec §2) ve uydurulmaz. Sinirlar `>= 1 Ocak` / `< 1 Ocak
    (yil+1)` seklinde YARI ACIKTIR: `EXTRACT(year …)` yazilsaydi kolondaki
    indeks kullanilamazdi.

    **KAPSAM:** yalnizca GORUNEN projelerin siparisleri. Katalog globaldir ama
    PARA degildir — suzgec olmasaydi satinalma sorumlusu, erisimi olmayan bir
    projenin harcama hacmini tedarikci kartindan okuyabilirdi.
    """
    baslangic = datetime(year, 1, 1, tzinfo=UTC)
    bitis = datetime(year + 1, 1, 1, tzinfo=UTC)
    return (
        select(
            PurchaseOrder.supplier_id.label("supplier_id"),
            func.sum(PurchaseOrder.total_amount).label("orders_total"),
            func.count().label("orders_count"),
        )
        .where(
            PurchaseOrder.project_id.in_(project_ids),
            PurchaseOrder.created_at >= baslangic,
            PurchaseOrder.created_at < bitis,
        )
        .group_by(PurchaseOrder.supplier_id)
        .subquery()
    )


def _supplier_with_totals(base: Select, totals: Subquery) -> Select:
    """`OUTER JOIN`: hic siparis almamis tedarikci listede KALIR ve turevi
    `COALESCE` ile sifirlanir — `INNER JOIN` yeni acilan tedarikciyi gizlerdi."""
    return base.outerjoin(totals, totals.c.supplier_id == Supplier.id)


async def list_suppliers(
    session: AsyncSession,
    totals: Subquery,
    *,
    q: str | None,
    category: str | None,
    is_active: bool | None,
    limit: int,
    offset: int,
) -> list[Row]:
    """Siralama DB'de (`ORDER BY name, id`) — sayfalama deterministik olsun.

    Ikinci olcut olmasaydi ayni adli iki tedarikci her istekte farkli sirada
    gelir ve sayfalar arasinda satir kaybolup tekrarlanabilirdi.
    """
    stmt = _supplier_with_totals(
        select(
            Supplier,
            func.coalesce(totals.c.orders_total, literal(Decimal("0"))).label("orders_total"),
            func.coalesce(totals.c.orders_count, literal(0)).label("orders_count"),
        ),
        totals,
    )
    stmt = _supplier_filtered(stmt, q=q, category=category, is_active=is_active)
    stmt = stmt.order_by(Supplier.name, Supplier.id).limit(limit).offset(offset)
    return list((await session.execute(stmt)).all())


async def count_suppliers(
    session: AsyncSession, *, q: str | None, category: str | None, is_active: bool | None
) -> int:
    """Sayim liste ile AYNI suzgecten gecer; siparis alt sorgusuna GIREKMEZ
    (`OUTER JOIN` satir sayisini degistirmez, gereksiz is yapilmaz)."""
    stmt = _supplier_filtered(
        select(func.count()).select_from(Supplier), q=q, category=category, is_active=is_active
    )
    return (await session.execute(stmt)).scalar_one()


async def get_supplier(session: AsyncSession, supplier_id: uuid.UUID) -> Supplier | None:
    return await session.get(Supplier, supplier_id)


async def get_supplier_with_totals(
    session: AsyncSession, totals: Subquery, supplier_id: uuid.UUID
) -> Row | None:
    """Detay ucu liste ile AYNI turetmeyi kullanir — kart iki ekranda ayni
    tutari gostersin diye ikinci bir formul YAZILMAZ."""
    stmt = _supplier_with_totals(
        select(
            Supplier,
            func.coalesce(totals.c.orders_total, literal(Decimal("0"))).label("orders_total"),
            func.coalesce(totals.c.orders_count, literal(0)).label("orders_count"),
        ),
        totals,
    ).where(Supplier.id == supplier_id)
    return (await session.execute(stmt)).one_or_none()


# --- Talep (FST + SAT) ---


def _request_filtered(
    stmt: Select,
    project_ids: list[uuid.UUID],
    *,
    status: PurchaseRequestStatus | None,
    project_id: uuid.UUID | None,
    priority: PurchasePriority | None,
    q: str | None,
) -> Select:
    """SAT filtre cubugu + KAPSAM. Suzgecler AND'lidir.

    Kapsam suzgeci (`project_id IN gorunenler`) HER ZAMAN uygulanir ve
    kullanicinin verdigi `project_id` onun YERINE degil USTUNE gecer: gorunmeyen
    bir proje kimligi verildiginde kesisim BOSTUR, yani sizinti olmaz.

    `q` TALEP NUMARASI ve GEREKCE uzerinde kismi arar — SAT tablosu ikisini de
    gosterir.
    """
    stmt = stmt.where(PurchaseRequest.project_id.in_(project_ids))
    if status is not None:
        stmt = stmt.where(PurchaseRequest.status == status)
    if project_id is not None:
        stmt = stmt.where(PurchaseRequest.project_id == project_id)
    if priority is not None:
        stmt = stmt.where(PurchaseRequest.priority == priority)
    if q:
        desen = f"%{_like_escape(q)}%"
        stmt = stmt.where(
            PurchaseRequest.request_no.ilike(desen, escape="\\")
            | PurchaseRequest.justification.ilike(desen, escape="\\")
        )
    return stmt


def request_totals() -> Subquery:
    """Talep basina tahmini toplam + kalem sayisi — TEK GRUPLU alt sorgu.

    Fiyatsiz kalem toplama GIRMEZ (`SUM` NULL'lari atlar) ve bu bilinclidir:
    sessizce 0 sayilsaydi "tahmini toplam neden dusuk" sorusu cevapsiz kalirdi.
    Kalem SAYISI ise fiyattan bagimsiz sayilir.
    """
    return (
        select(
            PurchaseRequestLine.request_id.label("request_id"),
            func.sum(PurchaseRequestLine.quantity * PurchaseRequestLine.estimated_unit_price).label(
                "estimated_total"
            ),
            func.count().label("line_count"),
        )
        .group_by(PurchaseRequestLine.request_id)
        .subquery()
    )


async def list_requests(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
    totals: Subquery,
    *,
    status: PurchaseRequestStatus | None,
    project_id: uuid.UUID | None,
    priority: PurchasePriority | None,
    q: str | None,
    limit: int,
    offset: int,
) -> list[Row]:
    """Siralama en yeniden eskiye; son olcut TALEP NUMARASI.

    ⚠️ Numara, `id` yerine bilincli olarak secildi: `created_at`in varsayilani
    `now()`dur ve Postgres'te `now()` ISLEM BOYU SABITTIR — ayni transaction'da
    acilan iki talep BIREBIR AYNI damgayi alir. Son olcut rastgele bir UUID
    olsaydi siralama o eslikte KARARSIZ kalir, sayfalar arasinda satir
    kaybolup tekrarlanabilirdi (fiilen goruldu: ayni test iki kosuda iki farkli
    sira verdi). `request_no` hem TEKIL hem de artan bir dizidir.
    """
    stmt = select(
        PurchaseRequest,
        func.coalesce(totals.c.estimated_total, literal(Decimal("0"))).label("estimated_total"),
        func.coalesce(totals.c.line_count, literal(0)).label("line_count"),
    ).outerjoin(totals, totals.c.request_id == PurchaseRequest.id)
    stmt = _request_filtered(
        stmt, project_ids, status=status, project_id=project_id, priority=priority, q=q
    )
    stmt = (
        stmt.order_by(
            PurchaseRequest.request_date.desc(),
            PurchaseRequest.created_at.desc(),
            PurchaseRequest.request_no.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).all())


async def count_requests(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
    *,
    status: PurchaseRequestStatus | None,
    project_id: uuid.UUID | None,
    priority: PurchasePriority | None,
    q: str | None,
) -> int:
    stmt = _request_filtered(
        select(func.count()).select_from(PurchaseRequest),
        project_ids,
        status=status,
        project_id=project_id,
        priority=priority,
        q=q,
    )
    return (await session.execute(stmt)).scalar_one()


async def get_request(
    session: AsyncSession, request_ref: uuid.UUID | str
) -> PurchaseRequest | None:
    """URL-4: kimlik YA DA `request_no` ile okur.

    `request_no` GLOBAL tekildir (`unique=True`, `models.py:201`), bu yuzden
    cozum SIRKET GENELINDE tek bir satir dondurur ve gorunurluk suzgeci
    cozumden SONRA uygulanabilir (`visible_request`) — anahtar kapsam ici
    tekil olsaydi tersi gerekirdi.
    """
    if isinstance(request_ref, uuid.UUID):
        return await session.get(PurchaseRequest, request_ref)
    return await session.scalar(
        select(PurchaseRequest).where(PurchaseRequest.request_no == request_ref)
    )


async def get_request_locked(
    session: AsyncSession, request_id: uuid.UUID
) -> PurchaseRequest | None:
    """🔴 EŞİK = KİLİT (OK-1A T3). Talep satiri, onay zincirinden ONCE kilitlenir.

    Kilit SIRASI uc evrak ailesinde de AYNIDIR: **evrak satiri -> zincir satiri**.
    Ters sirada kilitleyen ikinci bir yol karsilikli kilitlenme dogururdu.

    `populate_existing=True` sarttir: satir session'da ZATEN yuklüyse
    `with_for_update` tek basina TAZE degeri geri yazmaz ve kilit alinmis
    olmasina ragmen BAYAT `status` uzerinden karar verilir — iki es zamanli
    onay ayni adimi iki kez ilerletebilirdi.
    """
    return await session.scalar(
        select(PurchaseRequest)
        .where(PurchaseRequest.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def list_request_lines(
    session: AsyncSession, request_id: uuid.UUID
) -> list[tuple[PurchaseRequestLine, StockItem | None]]:
    """Kalemler + BAGLI KART tek sorguda (`OUTER JOIN`) gelir.

    Kart adi/kodu/birimi icin kalem basina `session.get(StockItem, …)`
    kosulsaydi 40 kalemlik bir talep 40 sorgu acardi. `OUTER JOIN` katalogsuz
    kalemleri de tasir (kart tarafi `None` gelir).

    Siralama **KULLANICININ GIRDIGI SIRADIR** (`sort_order`, T3'te acilan kolon):
    FST kalem tablosu siralidir ve satirlar her aciliste kullanicinin yazdigi
    duzende gorunmelidir. T2'de bu bir BILINEN SINIRDI — `id` bir UUID4'tur,
    yani ona gore siralamak kararli ama EKLEME SIRASINDAN BAGIMSIZ bir dizilis
    verirdi; borc T3'te kolonla kapatildi.

    Ikinci olcut `id`dir: `sort_order` NOT NULL ve her yazma yolunda dizinin
    indeksinden gelse de, DB'ye elle girmis bir cift kayit siralamayi kararsiz
    birakmasin.
    """
    stmt = (
        select(PurchaseRequestLine, StockItem)
        .outerjoin(StockItem, StockItem.id == PurchaseRequestLine.stock_item_id)
        .where(PurchaseRequestLine.request_id == request_id)
        .order_by(PurchaseRequestLine.sort_order, PurchaseRequestLine.id)
    )
    return [(satir, kart) for satir, kart in (await session.execute(stmt)).all()]


async def load_request_lines(
    session: AsyncSession, request_id: uuid.UUID
) -> list[PurchaseRequestLine]:
    """Yalniz satir nesneleri (yazma yolunun REPLACE adimi + `submit` dogrulamasi).

    Siralama okuma yoluyla AYNIDIR (`sort_order`, `id`): `submit_blockers`in
    urettigi engel listesi kullanicinin gordugu satir duzeniyle ortusmelidir.
    """
    stmt = (
        select(PurchaseRequestLine)
        .where(PurchaseRequestLine.request_id == request_id)
        .order_by(PurchaseRequestLine.sort_order, PurchaseRequestLine.id)
    )
    return list((await session.execute(stmt)).scalars().all())


# --- T3: esik turevleri, teklif ve siparis ---


async def actor_level(session: AsyncSession, actor: User) -> AccessLevel:
    """Aktorun `procurement` modulundeki GERCEK seviyesi.

    Router bagimliligi yalniz YETKI TABANI verir (`approve`); ₺500K esigi ise
    seviyeyi BILMEK zorundadir. Yardimci burada durur cunku iki cagirani vardir
    (`service.can_delete_request` ve `transitions._assert_approver_level`) ve
    `transitions` → `service` ithalati donguye girerdi.
    """
    return await effective_level(session, actor, PERMISSION_MODULE)


async def request_estimated_total(session: AsyncSession, request_id: uuid.UUID) -> Decimal:
    """Talebin O ANKI tahmini toplami — ₺500K esiginin TEK kaynagi.

    ⚠️ Kayitta donmus bir toplam OKUNMAZ ve boyle bir kolon ACILMAZ (T1):
    olsaydi kalem degisiminde bayatlar ve esik sessizce atlatilabilirdi.

    Formul liste turevi (`request_totals`) ile AYNIDIR: fiyatsiz kalem toplama
    GIRMEZ (`SUM` NULL'lari atlar). Iki taban olsaydi ekranda gorulen tutar ile
    esigin baktigi tutar ayni talep icin farkli cikabilirdi.
    """
    toplam = await session.scalar(
        select(
            func.sum(PurchaseRequestLine.quantity * PurchaseRequestLine.estimated_unit_price)
        ).where(PurchaseRequestLine.request_id == request_id)
    )
    return toplam if toplam is not None else Decimal("0")


async def request_quantity_total(session: AsyncSession, request_id: uuid.UUID) -> Decimal:
    """Talebin toplam MIKTARI — teklifin toplam maliyetinin carpanidir.

    Teklif tek bir `unit_price` tasir (spec §2, TEK karti): karsiligi talebin
    tumudur. Kalemsiz talepte sifirdir ve o durumda her teklifin maliyeti de
    yalniz nakliyeden ibaret kalir — uydurma bir 1 carpani KULLANILMAZ.
    """
    toplam = await session.scalar(
        select(func.sum(PurchaseRequestLine.quantity)).where(
            PurchaseRequestLine.request_id == request_id
        )
    )
    return toplam if toplam is not None else Decimal("0")


async def list_quotes(session: AsyncSession, request_id: uuid.UUID) -> list[Row]:
    """Teklifler + TEDARIKCI tek sorguda (`JOIN`).

    Kart basina `session.get(Supplier, …)` kosulsaydi 8 teklifli bir
    karsilastirma 8 sorgu acardi. `INNER JOIN` yeterlidir: `supplier_id` NOT
    NULL ve FK'dir.

    Siralama SUNUCUDA sabittir (`created_at`, `id`): TEK ekrani kartlari
    yan yana dizer ve her aciliste ayni duzeni gostermelidir.
    """
    stmt = (
        select(PurchaseQuote, Supplier.name.label("supplier_name"))
        .join(Supplier, Supplier.id == PurchaseQuote.supplier_id)
        .where(PurchaseQuote.request_id == request_id)
        .order_by(PurchaseQuote.created_at, PurchaseQuote.id)
    )
    return list((await session.execute(stmt)).all())


async def get_quote_in_request(
    session: AsyncSession, request_id: uuid.UUID, quote_id: uuid.UUID
) -> PurchaseQuote | None:
    """YOL CAPRAZININ kapisi: teklif BU talebin altinda degilse `None`.

    `session.get(PurchaseQuote, quote_id)` + ayri bir `request_id` kontrolu
    YAZILMAZ — iki adim arasinda birinin unutulmasi tam olarak caprazi acardi.
    Tek `WHERE` her iki olcutu de tasir.
    """
    stmt = select(PurchaseQuote).where(
        PurchaseQuote.id == quote_id, PurchaseQuote.request_id == request_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def clear_selected_quotes(
    session: AsyncSession, request_id: uuid.UUID, keep_quote_id: uuid.UUID
) -> None:
    """Talepte TEK secili teklif kalir. Toplu `UPDATE` — satir basina dongu YOK.

    Kismi UNIQUE indeks tercih EDILMEDI (T1 karari): teklif duzenleme sirasinda
    gecici iki-secili durumu imkansiz kilarak ucu kilitlerdi. Tekillik bu tek
    yazma yolunun sorumlulugudur.
    """
    await session.execute(
        update(PurchaseQuote)
        .where(
            PurchaseQuote.request_id == request_id,
            PurchaseQuote.id != keep_quote_id,
            PurchaseQuote.is_selected.is_(True),
        )
        .values(is_selected=False)
    )


def _order_filtered(
    stmt: Select,
    project_ids: list[uuid.UUID],
    *,
    status: PurchaseOrderStatus | None,
    project_id: uuid.UUID | None,
    supplier_id: uuid.UUID | None,
    q: str | None,
) -> Select:
    """SIP filtre cubugu + KAPSAM. Suzgecler AND'lidir.

    Kapsam (`project_id IN gorunenler`) HER ZAMAN uygulanir ve kullanicinin
    verdigi `project_id` onun USTUNE gecer: gorunmeyen bir proje kimligi
    verildiginde kesisim BOSTUR, yani sizinti olmaz.

    `q` siparis NUMARASI ve NOT uzerinde kismi arar — SIP tablosu ikisini de
    gosterir. Tedarikci ADINDA aranmaz: tedarikci icin AYRI ve kesin bir
    suzgec (`supplier_id`) vardir, metin aramasi onu belirsizlestirirdi.
    """
    stmt = stmt.where(PurchaseOrder.project_id.in_(project_ids))
    if status is not None:
        stmt = stmt.where(PurchaseOrder.status == status)
    if project_id is not None:
        stmt = stmt.where(PurchaseOrder.project_id == project_id)
    if supplier_id is not None:
        stmt = stmt.where(PurchaseOrder.supplier_id == supplier_id)
    if q:
        desen = f"%{_like_escape(q)}%"
        stmt = stmt.where(
            PurchaseOrder.order_no.ilike(desen, escape="\\")
            | PurchaseOrder.note.ilike(desen, escape="\\")
        )
    return stmt


def _order_select() -> Select:
    """Siparis + tedarikci adi + (varsa) talep numarasi TEK sorguda.

    Talep tarafi `OUTER JOIN`dir: talepsiz siparis MESRUDUR (§7 S3) ve `INNER
    JOIN` tam olarak SIP 35'in dogrudan siparisini listeden dusururdu.
    """
    return (
        select(
            PurchaseOrder,
            Supplier.name.label("supplier_name"),
            PurchaseRequest.request_no.label("request_no"),
        )
        .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
        .outerjoin(PurchaseRequest, PurchaseRequest.id == PurchaseOrder.request_id)
    )


async def list_orders(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
    *,
    status: PurchaseOrderStatus | None,
    project_id: uuid.UUID | None,
    supplier_id: uuid.UUID | None,
    q: str | None,
    limit: int,
    offset: int,
) -> list[Row]:
    """Siralama en yeniden eskiye; son olcut siparis NUMARASI.

    `id` DEGIL numara (T2'nin `list_requests` dersi): `created_at`in varsayilani
    `now()`dur ve Postgres'te ISLEM BOYU SABITTIR — ayni transaction'da acilan
    iki siparis birebir ayni damgayi alir. Rastgele bir UUID son olcut olsaydi
    siralama o eslikte KARARSIZ kalir, sayfalar arasinda satir kaybolurdu.
    """
    stmt = _order_filtered(
        _order_select(),
        project_ids,
        status=status,
        project_id=project_id,
        supplier_id=supplier_id,
        q=q,
    )
    stmt = (
        stmt.order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.order_no.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).all())


async def count_orders(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
    *,
    status: PurchaseOrderStatus | None,
    project_id: uuid.UUID | None,
    supplier_id: uuid.UUID | None,
    q: str | None,
) -> int:
    """Sayim liste ile AYNI suzgecten gecer; JOIN'lere GIRMEZ (satir sayisini
    degistirmezler, gereksiz is yapilmaz)."""
    stmt = _order_filtered(
        select(func.count()).select_from(PurchaseOrder),
        project_ids,
        status=status,
        project_id=project_id,
        supplier_id=supplier_id,
        q=q,
    )
    return (await session.execute(stmt)).scalar_one()


async def get_order_row(session: AsyncSession, order_id: uuid.UUID) -> Row | None:
    """Detay ucu liste ile AYNI turetmeyi kullanir (tek formul kurali)."""
    stmt = _order_select().where(PurchaseOrder.id == order_id)
    return (await session.execute(stmt)).one_or_none()


async def get_order(session: AsyncSession, order_id: uuid.UUID) -> PurchaseOrder | None:
    return await session.get(PurchaseOrder, order_id)


async def get_order_locked(session: AsyncSession, order_id: uuid.UUID) -> PurchaseOrder | None:
    """🔴 SA-KILIT — siparisin DURUM GECISLERI icin `SELECT … FOR UPDATE`.

    `get_request_locked`in siparis tarafindaki ikizi ve gerekcesi AYNIDIR:
    `transitions.assert_order_transition` kararini BELLEKTEKI `status` uzerinden
    verir. Satir kilitlenmeden okunursa iki es zamanli yazici da ayni bayat
    durumu gorur; ikincisi bloke olsa bile karar YENIDEN sorulmaz.

    Olculdu (2026-08-23): stok girisinin `delivered` damgasi ile es zamanli bir
    `PATCH {"status": "in_transit"}` ayni siparise gelince siparis
    **`in_transit`**, bagli talep **`delivered`** kaliyordu — teslim damgasi
    KAYBOLUYOR ve ikili CELISKILI oluyordu.

    `populate_existing=True` sarttir: satir session'da ZATEN yukluyse
    `with_for_update` tek basina TAZE degeri geri YAZMAZ ve kilit alinmis
    olmasina ragmen yine BAYAT durum uzerinden karar verilirdi.
    """
    return await session.scalar(
        select(PurchaseOrder)
        .where(PurchaseOrder.id == order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


# --- T4: ozet sayaclari (SAT 69-86 / SIP 38-43) ---
#
# UC sorgu kosar ve sayisi DURUM SAYISINDAN BAGIMSIZDIR: talep sayimlari ·
# siparis sayimlari · bu ayin tutari. Durum basina ayri bir `count` sorgusu
# acilsaydi tek bir KPI seridi yedi sorgu ederdi (N+1'in serit hâli).


async def request_status_counts(
    session: AsyncSession, project_ids: list[uuid.UUID], *, project_id: uuid.UUID | None
) -> dict[PurchaseRequestStatus, int]:
    """Talep sayilari — durum basina TEK GRUPLU sorgu.

    Kapsam (`project_id IN gorunenler`) HER ZAMAN uygulanir; kullanicinin
    verdigi `project_id` onun USTUNE gecer (liste uclarindaki kural).
    """
    stmt = select(PurchaseRequest.status, func.count()).where(
        PurchaseRequest.project_id.in_(project_ids)
    )
    if project_id is not None:
        stmt = stmt.where(PurchaseRequest.project_id == project_id)
    rows = (await session.execute(stmt.group_by(PurchaseRequest.status))).all()
    return {durum: sayi for durum, sayi in rows}


async def order_status_counts(
    session: AsyncSession, project_ids: list[uuid.UUID], *, project_id: uuid.UUID | None
) -> dict[PurchaseOrderStatus, int]:
    """Siparis sayilari — durum basina TEK GRUPLU sorgu.

    ZAMAN SUZGECI YOKTUR ve bu bilinclidir: "Aktif Siparişler"/"Yolda"/"Teslim
    Edildi" kartlari bir DURUM fotografidir; ay penceresi yalniz PARA kartina
    (`orders_total_in_month`) uygulanir cunku mockup "Bu Ay"i yalniz orada yazar.
    """
    stmt = select(PurchaseOrder.status, func.count()).where(
        PurchaseOrder.project_id.in_(project_ids)
    )
    if project_id is not None:
        stmt = stmt.where(PurchaseOrder.project_id == project_id)
    rows = (await session.execute(stmt.group_by(PurchaseOrder.status))).all()
    return {durum: sayi for durum, sayi in rows}


async def orders_total_in_month(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
    *,
    project_id: uuid.UUID | None,
    year: int,
    month: int,
) -> Decimal:
    """SAT 79 "Bu Ay Sipariş" = SIP 40 "Bu Ay Toplam" — TEK turetme.

    Yil olcutu `supplier_order_totals` ile AYNI kolondur (`created_at`):
    `purchase_orders`ta ayri bir siparis TARIHI kolonu yoktur (spec §2) ve
    uydurulmaz. Sinirlar yari aciktir (`>= ay basi`, `< sonraki ay basi`) —
    `EXTRACT(month …)` yazilsaydi kolondaki indeks kullanilamazdi.
    """
    baslangic = datetime(year, month, 1, tzinfo=UTC)
    bitis = (
        datetime(year + 1, 1, 1, tzinfo=UTC)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=UTC)
    )
    stmt = select(func.sum(PurchaseOrder.total_amount)).where(
        PurchaseOrder.project_id.in_(project_ids),
        PurchaseOrder.created_at >= baslangic,
        PurchaseOrder.created_at < bitis,
    )
    if project_id is not None:
        stmt = stmt.where(PurchaseOrder.project_id == project_id)
    toplam = await session.scalar(stmt)
    return toplam if toplam is not None else Decimal("0")


async def existing_stock_item_ids(
    session: AsyncSession, item_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """Kalemlerdeki kartlarin TAMAMI TEK sorguda dogrulanir — ve YAZIMDAN ONCE.

    Atomikligin tasiyicisi budur: kart basina `session.get` ile ilerlenseydi
    hem N sorgu acilir hem de ilk satirlar yazildiktan sonra hata cikardi
    (ST `_assert_items_exist` deseni).
    """
    if not item_ids:
        return set()
    stmt = select(StockItem.id).where(StockItem.id.in_(item_ids))
    return set((await session.execute(stmt)).scalars().all())


async def current_stock_by_item(
    session: AsyncSession, project_ids: list[uuid.UUID], item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """ "Mevcut Stok" (FST 75) — TEK toplu sorgu, kalem basina sorgu ACILMAZ.

    Bakiye formulu KOPYALANMAZ: ST'nin kanonik kaynagi (`inventory.balance.legs`,
    CIFT BACAK dahil) cagrilir. Ikinci bir formul yazilsaydi FST'deki "Mevcut
    Stok" ile stok ekranindaki bakiye ayni kalem icin farkli sayi gosterirdi.

    **KAPSAM:** ST genel ozetiyle AYNI — gorunen TUM depolar (merkez dahil,
    spec ST §7 S2b). Talebin santiyesine daraltilmadi cunku santiye ISTEGE
    BAGLIDIR (FST 57) ve satinalma karari sirket genelindeki stogu bilerek
    verilir: merkez ambardaki 40 ton demir icin yeni talep acilmamalidir.
    """
    if not item_ids:
        return {}
    warehouse_ids = inventory_repository.visible_warehouse_ids(project_ids)
    bacaklar = stock_balance.legs(warehouse_ids)
    stmt = (
        select(bacaklar.c.item_id, func.sum(bacaklar.c.quantity).label("balance"))
        .where(bacaklar.c.item_id.in_(item_ids))
        .group_by(bacaklar.c.item_id)
    )
    return {row.item_id: row.balance for row in (await session.execute(stmt)).all()}


__all__ = [
    "actor_level",
    "clear_selected_quotes",
    "count_orders",
    "count_requests",
    "count_suppliers",
    "current_stock_by_item",
    "existing_stock_item_ids",
    "get_order",
    "get_order_locked",
    "get_order_row",
    "get_quote_in_request",
    "get_request",
    "get_request_locked",
    "get_supplier",
    "get_supplier_with_totals",
    "list_orders",
    "list_quotes",
    "list_request_lines",
    "list_requests",
    "list_suppliers",
    "load_request_lines",
    "order_status_counts",
    "orders_total_in_month",
    "request_estimated_total",
    "request_status_counts",
    "request_quantity_total",
    "request_totals",
    "supplier_order_totals",
]

# NOT: `selectinload` bu modulde HIC kullanilmaz cunku `PurchaseRequest`in kalem
# ILISKISI T1'de tanimlanmadi (async oturumda tembel iliskiye dokunmak
# `MissingGreenlet` = 500 uretir — P11 dersi). Kalemler her zaman ACIK sorguyla
# okunur; sonraki okuyucu buraya `relationship` eklemesin.

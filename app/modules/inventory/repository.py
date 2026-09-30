"""Stok çekirdeği veri erişimi (T2) — yalnız SQL, yetki/kapsam KARARI yok.

Kapsam kararı (`visible_projects`) bu katmanda DEĞİL `service.py`dedir
(`documents/repository.py` deseninin kardeşi); buraya yalnız çözülmüş proje
kimlikleri gelir.

Liste ve sayım AYNI süzgeç yardımcısını paylaşır (`personnel` deseni): kopya
açılsaydı `total` ile gösterilen tablo zamanla ayrışırdı.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import (
    ColumnElement,
    Row,
    Select,
    and_,
    case,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import Subquery

from app.modules.boq.models import BoqItem
from app.modules.inventory import balance
from app.modules.inventory.models import (
    StockCategory,
    StockEntry,
    StockEntryLine,
    StockEntryType,
    StockItem,
    Warehouse,
)
from app.modules.sites.models import Section, Site


def _like_escape(deger: str) -> str:
    """LIKE joker karakterlerini KAÇIRIR (`documents.repository` deseni).

    Kaçırılmazsa arama kutusuna `%` yazan kullanıcı TÜM kataloğu, `_` yazan ise
    beklemediği satırları görür — serbest metin aradığını sanarak. Kaçış
    karakterinin kendisi ÖNCE kaçırılır, yoksa sonraki değişimler onu ikinci kez
    bozardı.
    """
    return deger.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- Malzeme kartı (katalog) ---


def _item_filtered(
    stmt: Select, category: StockCategory | None, q: str | None, is_active: bool | None
) -> Select:
    """E3'ün üç süzgeci: kategori select'i (99) · arama kutusu · aktiflik.

    Durum süzgeci (Kritik/Normal/Fazla) BURADA YOKTUR: durum bakiyeden TÜREVDİR
    (spec §3) ve hareket toplamı gerektirir — T3'ün işidir.
    """
    if category is not None:
        stmt = stmt.where(StockItem.category == category)
    if q:
        desen = f"%{_like_escape(q)}%"
        stmt = stmt.where(
            StockItem.code.ilike(desen, escape="\\") | StockItem.name.ilike(desen, escape="\\")
        )
    if is_active is not None:
        stmt = stmt.where(StockItem.is_active.is_(is_active))
    return stmt


async def list_stock_items(
    session: AsyncSession,
    *,
    category: StockCategory | None,
    q: str | None,
    is_active: bool | None,
    limit: int,
    offset: int,
) -> list[StockItem]:
    """Sıralama DB'de (`ORDER BY name, id`) — sayfalama deterministik olsun.

    İkinci ölçüt olmasaydı aynı adlı iki kart her istekte farklı sırada gelir ve
    sayfalar arasında satır kaybolup tekrarlanabilirdi.
    """
    stmt = _item_filtered(select(StockItem), category, q, is_active)
    stmt = stmt.order_by(StockItem.name, StockItem.id).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def count_stock_items(
    session: AsyncSession,
    *,
    category: StockCategory | None,
    q: str | None,
    is_active: bool | None,
) -> int:
    stmt = _item_filtered(select(func.count()).select_from(StockItem), category, q, is_active)
    return (await session.execute(stmt)).scalar_one()


async def get_stock_item(session: AsyncSession, item_id: uuid.UUID) -> StockItem | None:
    return await session.get(StockItem, item_id)


async def find_stock_item_by_code(
    session: AsyncSession, code: str, *, exclude_id: uuid.UUID | None = None
) -> StockItem | None:
    """`exclude_id` PATCH içindir: kaydın KENDİSİ çakışma sayılmaz, aksi hâlde
    aynı kodla ikinci kez "Kaydet" basmak 409 verirdi."""
    stmt = select(StockItem).where(StockItem.code == code)
    if exclude_id is not None:
        stmt = stmt.where(StockItem.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalars().first()


# --- Depo ---


def _warehouse_scope(stmt: Select, project_ids: list[uuid.UUID]) -> Select:
    """Görünürlük süzgeci — spec §7 **S2b**.

    İKİ dallıdır ve dallar OR'ludur:
      * `site_id IS NULL` (MERKEZ depo) → kapsam süzgecine TABİ DEĞİL;
      * şantiyeli depo → şantiyesinin projesi görünen projeler içinde olmalı.

    Merkez dalı OR'dan çıkarılsaydı şirketin ana ambarı hiç kimseye görünmezdi
    (hiçbir projeye bağlı değildir). Şantiye dalı çıkarılsaydı başka projenin
    deposu sızardı.

    Alt sorgu tek seferliktir — depo başına sorgu (N+1) AÇILMAZ.
    """
    gorunen_santiyeler = select(Site.id).where(Site.project_id.in_(project_ids))
    return stmt.where(Warehouse.site_id.is_(None) | Warehouse.site_id.in_(gorunen_santiyeler))


async def list_warehouses(
    session: AsyncSession, project_ids: list[uuid.UUID], *, limit: int, offset: int
) -> list[Warehouse]:
    stmt = _warehouse_scope(select(Warehouse), project_ids)
    stmt = stmt.order_by(Warehouse.name, Warehouse.id).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def count_warehouses(session: AsyncSession, project_ids: list[uuid.UUID]) -> int:
    """Sayım liste ile AYNI kapsam süzgecinden geçer: `total` görünen kümeyi
    sayar, tablonun tamamını değil."""
    stmt = _warehouse_scope(select(func.count()).select_from(Warehouse), project_ids)
    return (await session.execute(stmt)).scalar_one()


async def get_warehouse(session: AsyncSession, warehouse_id: uuid.UUID) -> Warehouse | None:
    return await session.get(Warehouse, warehouse_id)


async def find_warehouse_by_name(
    session: AsyncSession,
    site_id: uuid.UUID | None,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> Warehouse | None:
    """UQ ile BİREBİR aynı ikili üzerinden mevcut-ad kontrolü.

    NULL karşılaştırması `IS NULL` ile yazılır: `== None` SQL'de `= NULL`
    üretseydi MERKEZ depolarda kontrol HİÇBİR ŞEY bulamaz ve DB kısıtının zaten
    çalışmadığı tam o dalda tekillik tamamen kaybolurdu (`documents` dersi).
    """
    stmt = select(Warehouse).where(
        Warehouse.site_id.is_(None) if site_id is None else Warehouse.site_id == site_id,
        Warehouse.name == name,
    )
    if exclude_id is not None:
        stmt = stmt.where(Warehouse.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalars().first()


async def lock_warehouse_for_update(session: AsyncSession, warehouse_id: uuid.UUID) -> None:
    """Silme yolunun DIŞLAYICI kilidi (`documents.lock_folder_for_update` deseni).

    "Hareketi var mı?" kontrolü ile `DELETE` arasına başka bir isteğin hareket
    yazmasını engeller. ⚠️ T3 NOTU: hareket yazan uç bu satırı `FOR SHARE` ile
    kilitlemelidir; yoksa yarış hareket ayağında açık kalır ve DB'nin RESTRICT'i
    kullanıcıya 500 olarak döner.
    """
    await session.execute(
        select(Warehouse.id).where(Warehouse.id == warehouse_id).with_for_update()
    )


async def warehouse_has_entries(session: AsyncSession, warehouse_id: uuid.UUID) -> bool:
    """HEDEF ve KAYNAK bacakların İKİSİ de sayılır (`guards` gerekçesi)."""
    stmt = (
        select(func.count())
        .select_from(StockEntry)
        .where(
            (StockEntry.warehouse_id == warehouse_id)
            | (StockEntry.source_warehouse_id == warehouse_id)
        )
    )
    return bool((await session.execute(stmt)).scalar_one())


# --- Hareket (T3) ---


async def lock_warehouses_for_share(session: AsyncSession, warehouse_ids: list[uuid.UUID]) -> None:
    """Hareket yazmadan ÖNCE hedef ve kaynak depoyu `FOR SHARE` ile kilitler.

    ⚠️ T2'nin DEVİR NOTU: silme yolu satırı `FOR UPDATE` ile kilitler ve
    "hareketi var mı?" kontrolü ile `DELETE` arasını kapatır. Bu ayak
    kilitlenmeseydi eşzamanlı `DELETE /warehouses/{id}` penceresinde INSERT,
    DB'nin `RESTRICT` kısıtına düşer ve kullanıcıya **500** dönerdi.

    `FOR SHARE` (paylaşımlı) yeterlidir: iki hareket birbirini beklemez, yalnız
    silme ile hareket birbirini dışlar.

    Kimlikler SIRALI kilitlenir: iki depo arasında karşılıklı transfer yazan iki
    istek farklı sırada kilitleseydi kilitlenme (deadlock) doğardı.
    """
    if not warehouse_ids:
        return
    await session.execute(
        select(Warehouse.id)
        .where(Warehouse.id.in_(warehouse_ids))
        .order_by(Warehouse.id)
        .with_for_update(read=True)
    )


async def existing_item_ids(session: AsyncSession, item_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Satırlardaki kartların TAMAMI TEK sorguda doğrulanır.

    Kart başına `session.get` koşulsaydı 40 kalemlik bir irsaliye 40 sorgu
    açardı; üstelik atomiklik için hepsinin YAZIMDAN ÖNCE bilinmesi gerekir.
    """
    stmt = select(StockItem.id).where(StockItem.id.in_(item_ids))
    return set((await session.execute(stmt)).scalars().all())


def _entry_scope(stmt: Select, project_ids: list[uuid.UUID]) -> Select:
    """Hareket görünürlüğü: HEDEF ya da KAYNAK bacağı görünen depoda olan kayıt.

    `OR`dur, `AND` DEĞİL: kendi şantiyemden BAŞKA bir projenin deposuna çıkan
    transfer benim stoğumu düşürür ve hareket listemde GÖRÜNMEK ZORUNDADIR.
    Bilinçli sınır: böyle bir satırda karşı deponun UUID'si yanıta girer — adı
    ya da şantiyesi girmez, yalnız kimliği.
    """
    gorunen = _warehouse_scope(select(Warehouse.id), project_ids)
    return stmt.where(
        StockEntry.warehouse_id.in_(gorunen) | StockEntry.source_warehouse_id.in_(gorunen)
    )


def _entry_filtered(
    stmt: Select,
    project_ids: list[uuid.UUID],
    entry_type: StockEntryType | None,
    warehouse_id: uuid.UUID | None,
    date_from: date | None,
    date_to: date | None,
) -> Select:
    """Liste ve sayım AYNI süzgeçten geçer (`_item_filtered` gerekçesi)."""
    stmt = _entry_scope(stmt, project_ids)
    if entry_type is not None:
        stmt = stmt.where(StockEntry.entry_type == entry_type)
    if warehouse_id is not None:
        # Depo süzgeci İKİ BACAĞI da kapsar: kullanıcı "bu deponun hareketleri"
        # derken oraya GİRENİ de oradan ÇIKANI da kasteder.
        stmt = stmt.where(
            (StockEntry.warehouse_id == warehouse_id)
            | (StockEntry.source_warehouse_id == warehouse_id)
        )
    if date_from is not None:
        stmt = stmt.where(StockEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(StockEntry.entry_date <= date_to)
    return stmt


async def list_entries(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
    *,
    entry_type: StockEntryType | None,
    warehouse_id: uuid.UUID | None,
    date_from: date | None,
    date_to: date | None,
    limit: int,
    offset: int,
) -> list[StockEntry]:
    """Satırlar `selectinload` ile TEK ek sorguda gelir — hareket başına sorgu
    (N+1) açılmaz. Sıralama en yeniden eskiye, ikinci ölçüt kimlik: aynı güne
    düşen iki hareket sayfalar arasında kaybolup tekrarlanmasın."""
    stmt = _entry_filtered(
        select(StockEntry), project_ids, entry_type, warehouse_id, date_from, date_to
    )
    stmt = (
        stmt.options(selectinload(StockEntry.lines))
        .order_by(StockEntry.entry_date.desc(), StockEntry.created_at.desc(), StockEntry.id)
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_entries(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
    *,
    entry_type: StockEntryType | None,
    warehouse_id: uuid.UUID | None,
    date_from: date | None,
    date_to: date | None,
) -> int:
    stmt = _entry_filtered(
        select(func.count()).select_from(StockEntry),
        project_ids,
        entry_type,
        warehouse_id,
        date_from,
        date_to,
    )
    return (await session.execute(stmt)).scalar_one()


# --- Türev okuma: bakiye / durum / KPI (spec §3) ---


def visible_warehouse_ids(project_ids: list[uuid.UUID]) -> Select:
    """Genel özetin kapsamı: merkez depoların HEPSİ + görünen şantiye depoları."""
    return _warehouse_scope(select(Warehouse.id), project_ids)


def site_warehouse_ids(site_id: uuid.UUID) -> Select:
    """Şantiye özetinin kapsamı: YALNIZ o şantiyenin depoları.

    Merkez depo (`site_id IS NULL`) BURAYA GİRMEZ — spec §3'ün kararı:
    "şantiye bakiyesi = o şantiyenin depoları". Girseydi aynı merkez stok her
    şantiyede tekrar sayılır ve şantiye toplamları şirket toplamını aşardı.
    """
    return select(Warehouse.id).where(Warehouse.site_id == site_id)


class SummaryContext(NamedTuple):
    """Özet uçlarının ORTAK iskeleti — üç sorgu da (sayfa, sayım, KPI) bunu
    paylaşır ki bakiye/durum/fiyat formülü tek yerde kalsın."""

    balance_subq: Subquery
    price_subq: Subquery
    balance: ColumnElement
    status: ColumnElement
    last_price: ColumnElement


def summary_context(warehouse_ids: Select) -> SummaryContext:
    bacaklar = balance.legs(warehouse_ids)
    bakiye_kaynagi = (
        select(
            bacaklar.c.item_id.label("item_id"),
            func.sum(bacaklar.c.quantity).label("balance"),
        )
        .group_by(bacaklar.c.item_id)
        .subquery()
    )
    fiyat_kaynagi = (
        select(
            StockEntryLine.item_id.label("item_id"),
            StockEntryLine.unit_price.label("unit_price"),
        )
        .join(StockEntry, StockEntry.id == StockEntryLine.entry_id)
        .where(
            StockEntryLine.unit_price.is_not(None),
            StockEntry.warehouse_id.in_(warehouse_ids),
        )
        .distinct(StockEntryLine.item_id)
        .order_by(
            StockEntryLine.item_id,
            StockEntry.entry_date.desc(),
            StockEntry.created_at.desc(),
            StockEntryLine.id,
        )
        .subquery()
    )
    bakiye = func.coalesce(bakiye_kaynagi.c.balance, literal(Decimal("0")))
    return SummaryContext(
        balance_subq=bakiye_kaynagi,
        price_subq=fiyat_kaynagi,
        balance=bakiye,
        status=balance.status_case(bakiye, StockItem.min_stock),
        last_price=fiyat_kaynagi.c.unit_price,
    )


def _summary_joined(base: Select, ctx: SummaryContext, *, only_moved: bool) -> Select:
    """Kartı bakiye ve son fiyat kaynaklarına bağlar.

    `only_moved=False` (genel özet, E3): `OUTER JOIN` — hiç hareket görmemiş
    katalog kartı 0 bakiye ile listede KALIR, yoksa "min 10 olan kalem hiç
    alınmamış" uyarısı hiç doğmazdı.

    `only_moved=True` (şantiye özeti, ŞS): `INNER JOIN` — ŞS "o şantiyenin
    malzemeleri" ekranıdır; şantiyeye hiç girmemiş kart listeyi doldurmaz.
    """
    stmt = base.join(
        ctx.balance_subq,
        ctx.balance_subq.c.item_id == StockItem.id,
        isouter=not only_moved,
    )
    return stmt.outerjoin(ctx.price_subq, ctx.price_subq.c.item_id == StockItem.id)


def gorunur_kalem_kosulu(ctx: SummaryContext) -> ColumnElement:
    """Pasif kalem görünürlüğü — yönetim kararı 2026-08-12 (F-ST canlı smoke).

    * **pasif + bakiye 0 → SÜZÜLÜR**: pasifleştirilmiş ve elde kalmamış kart
      E3/ŞS kataloğunda yer kaplamaz (canlıda `SMOKE-FST-01` böyle takılı kaldı).
    * **pasif + bakiye ≠ 0 → LİSTELENİR**: envanter gerçeği gizlenmez; elde
      stoğu olan kartı saklamak "depoda yok" izlenimi verir ve yanıltıcıdır.
    * **aktif → her zaman listelenir** (bakiye 0 olsa da).

    Koşul `_summary_filtered` içinden TEK yerden uygulanır ki liste, `total`
    sayımı ve KPI şeridi aynı kümeyi görsün: üçü ayrışsaydı ekranda "3 kalem"
    yazıp 2 satır görünürdü. Süzgeç Python'da değil SQL'de olmak ZORUNDADIR —
    sayfalanmış satırları elde eleseydik `limit` dolmadan sayfa kırpılırdı.
    """
    return or_(StockItem.is_active.is_(True), ctx.balance != literal(Decimal("0")))


def _summary_filtered(
    stmt: Select,
    ctx: SummaryContext,
    *,
    status: str | None,
    category: StockCategory | None,
    q: str | None,
    item_ids: Select | None,
) -> Select:
    """E3 filtre çubuğu: durum sekmeleri · kategori select'i · arama kutusu.

    **Durum süzgeci SQL'de uygulanır**, Python'da değil: türev bir alana göre
    süzüp sonra sayfalasaydık `total` ile gösterilen satır sayısı ayrışırdı.

    Pasif kalem süzgeci (`gorunur_kalem_kosulu`) da BURADADIR: her iki uç ve her
    üç sorgu (sayfa/sayım/KPI) bu tek kapıdan geçer."""
    stmt = _item_filtered(stmt, category, q, None)
    stmt = stmt.where(gorunur_kalem_kosulu(ctx))
    if status is not None:
        stmt = stmt.where(ctx.status == status)
    if item_ids is not None:
        # STOK-BOLUM `section_id` suzgeci. SATIR KUMESINI daraltir, `balance`i
        # DEGISTIRMEZ (gerekce `item_ids_attributed_to_section`da). Suzgec
        # BURADADIR ki liste, `total` ve KPI ayni kumeyi gorsun — ucu
        # ayrissaydi ekranda "3 kalem" yazip 2 satir gorunurdu.
        stmt = stmt.where(StockItem.id.in_(item_ids))
    return stmt


async def list_summary_rows(
    session: AsyncSession,
    ctx: SummaryContext,
    *,
    only_moved: bool,
    status: str | None,
    category: StockCategory | None,
    q: str | None,
    item_ids: Select | None = None,
    limit: int,
    offset: int,
) -> list[Row]:
    stmt = _summary_joined(
        select(
            StockItem,
            ctx.balance.label("balance"),
            ctx.status.label("status"),
            ctx.last_price.label("last_price"),
        ),
        ctx,
        only_moved=only_moved,
    )
    stmt = _summary_filtered(stmt, ctx, status=status, category=category, q=q, item_ids=item_ids)
    stmt = stmt.order_by(StockItem.name, StockItem.id).limit(limit).offset(offset)
    return list((await session.execute(stmt)).all())


async def count_summary_rows(
    session: AsyncSession,
    ctx: SummaryContext,
    *,
    only_moved: bool,
    status: str | None,
    category: StockCategory | None,
    q: str | None,
    item_ids: Select | None = None,
) -> int:
    stmt = _summary_joined(select(func.count()).select_from(StockItem), ctx, only_moved=only_moved)
    stmt = _summary_filtered(stmt, ctx, status=status, category=category, q=q, item_ids=item_ids)
    return (await session.execute(stmt)).scalar_one()


async def count_items_without_threshold(
    session: AsyncSession, ctx: SummaryContext, *, only_moved: bool
) -> int:
    """Esigi GIRILMEMIS (`min_stock IS NULL`) gorunur kalem sayisi.

    🔴 NICIN AYRI BIR SAYAC: `status_case` `min_stock` NULL iken durumu `NULL`
    birakir (uydurma yok) — yani esigi girilmemis kalem HICBIR kovaya dusmez ve
    "kritik" sayimina da girmez. Bu sayac olmadan bos bir kritik listesi hem
    "risk yok" hem "risk BILINMIYOR" demeye devam ederdi.

    Emsal AYNI DOSYADADIR: `summary_kpis.items_without_price` de fiyatsiz kalemi
    sessizce 0 saymaz, AYRICA raporlar. Bu, o kuralin `min_stock` yuzudur.

    Gorunurluk/pasiflik suzgeci KOPYALANMAZ: sayim `_summary_joined` +
    `_summary_filtered` uzerinden gecer, yani liste/sayim/KPI ile AYNI kumeyi
    gorur. Ikinci bir suzgec, "3 kalem esiksiz" deyip listede baska bir kume
    gostermek demekti.
    """
    stmt = _summary_joined(select(func.count()).select_from(StockItem), ctx, only_moved=only_moved)
    stmt = _summary_filtered(stmt, ctx, status=None, category=None, q=None, item_ids=None)
    stmt = stmt.where(StockItem.min_stock.is_(None))
    return (await session.execute(stmt)).scalar_one()


async def summary_kpis(
    session: AsyncSession,
    ctx: SummaryContext,
    *,
    only_moved: bool,
    status: str | None,
    category: StockCategory | None,
    q: str | None,
    item_ids: Select | None = None,
) -> Row:
    """KPI şeridi SAYFAYI değil SÜZÜLEN KÜMEYİ özetler (E3 72-89 / ŞS 86-91).

    Sayfa üzerinden hesaplansaydı ikinci sayfaya geçen kullanıcı "toplam stok
    değeri"nin değiştiğini görürdü.

    **Toplam değer = kalemin SON giriş fiyatı × bakiye** (§7 S6). Ağırlıklı
    ortalama İCAT EDİLMEZ. **Fiyatsız kalem toplama GİRMEZ** ve sessizce 0
    sayılmaz: bakiyesi olup fiyatı olmayan kalemler `items_without_price` ile
    AYRICA raporlanır, yoksa "değer neden düşük" sorusu cevapsız kalırdı.
    """
    stmt = _summary_joined(
        select(
            func.count().label("total_items"),
            func.count()
            .filter(ctx.status == balance.StockStatus.critical.value)
            .label("critical_count"),
            func.count().filter(ctx.status == balance.StockStatus.low.value).label("low_count"),
            func.coalesce(
                func.sum(
                    case(
                        (ctx.last_price.is_not(None), ctx.last_price * ctx.balance),
                        else_=literal(Decimal("0")),
                    )
                ),
                literal(Decimal("0")),
            ).label("total_value"),
            func.count()
            .filter(and_(ctx.last_price.is_(None), ctx.balance != literal(Decimal("0"))))
            .label("items_without_price"),
        ).select_from(StockItem),
        ctx,
        only_moved=only_moved,
    )
    stmt = _summary_filtered(stmt, ctx, status=status, category=category, q=q, item_ids=item_ids)
    return (await session.execute(stmt)).one()


async def warehouse_breakdown(
    session: AsyncSession, warehouse_ids: Select, item_ids: list[uuid.UUID]
) -> list[Row]:
    """E3 "Depo" sütunu: sayfadaki kalemlerin depo bazında bakiyesi.

    Kalem başına sorgu (N+1) AÇILMAZ — sayfanın TÜM kimlikleri tek `IN` ile
    sorulur ve sorgu sayısı veri hacminden bağımsız kalır.

    Bakiyesi SIFIRA düşmüş depo kırılımda KALIR: "bu depoda artık yok" bilgisi
    "bu depoda hiç olmadı"dan farklıdır ve kullanıcı ikisini ayırt etmelidir.
    """
    if not item_ids:
        return []
    bacaklar = balance.legs(warehouse_ids)
    stmt = (
        select(
            bacaklar.c.item_id,
            Warehouse.id.label("warehouse_id"),
            Warehouse.name.label("warehouse_name"),
            Warehouse.site_id,
            func.sum(bacaklar.c.quantity).label("balance"),
        )
        .join(Warehouse, Warehouse.id == bacaklar.c.warehouse_id)
        .where(bacaklar.c.item_id.in_(item_ids))
        .group_by(bacaklar.c.item_id, Warehouse.id, Warehouse.name, Warehouse.site_id)
        .order_by(Warehouse.name, Warehouse.id)
    )
    return list((await session.execute(stmt)).all())


# --- Bolum malzeme kirilimi (STOK-BOLUM) ---


def _section_line_scope(section_id: uuid.UUID) -> Select:
    """Bir bölüme atfedilmiş satırların ORTAK iskeleti.

    🔴 **KAPSAM SÜZGECİ DEPO ÜZERİNDEN KURULMAZ ve buna gerek YOKTUR.** Yazma
    kapısı (`service._resolve_line_attribution`) bölümün, hareketin deposunun
    şantiyesine ait olmasını ZORLAR; şantiyesiz merkez depoda ise bölüm+poz
    birbirine çapa olur. Yani `section_id` dolu bir satır zaten o bölümün
    şantiyesine bağlıdır. Buraya ikinci bir depo süzgeci konsaydı, kapının
    tuttuğu invariant SESSİZCE bir daha uygulanır ve kapı gevşerse fark
    edilmezdi — kapının kendisi bekçilidir (`test_stok_bolum_tutarlilik`).

    🔴 **`transfer` SATIRI BURAYA HİÇ GİREMEZ**: şema katmanı transfer
    hareketinde atıf yazılmasını 422 ile reddeder. Bu yüzden ÇİFT BACAK
    sorunu bu türetmede YOKTUR — her satır tek bacaklıdır ve `balance.legs()`
    burada ÇAĞRILMAZ.
    """
    return (
        select(StockEntryLine)
        .join(StockEntry, StockEntry.id == StockEntryLine.entry_id)
        .where(StockEntryLine.section_id == section_id)
    )


def _section_aggregates() -> tuple[ColumnElement, ColumnElement, ColumnElement, ColumnElement]:
    """`assigned` / `issued` / `net` / `value` ifadeleri — TEK yerde.

    `issued` NEGATİF miktarların MUTLAK toplamıdır: sarf, `adjustment`
    satırının eksi miktarıdır (§7 S4 — sarf için ayrı tip AÇILMAZ). İşaretli
    tek bir toplam basılsaydı `+5 alım` ile `−5 sarf` birbirini götürür ve
    ekran "hiç kullanılmadı" derdi.
    """
    sifir = literal(Decimal("0"))
    artilar = func.coalesce(
        func.sum(case((StockEntryLine.quantity > 0, StockEntryLine.quantity), else_=sifir)),
        sifir,
    )
    eksiler = func.coalesce(
        func.sum(case((StockEntryLine.quantity < 0, -StockEntryLine.quantity), else_=sifir)),
        sifir,
    )
    net = func.coalesce(func.sum(StockEntryLine.quantity), sifir)
    # Fiyatsiz satir toplam degere GIRMEZ (§7 S6): `unit_price` NULL ise carpim
    # da NULL olur ve `sum` onu atlar.
    deger = func.coalesce(func.sum(StockEntryLine.quantity * StockEntryLine.unit_price), sifir)
    return artilar, eksiler, net, deger


def _section_rows_stmt(section_id: uuid.UUID) -> Select:
    """(malzeme, poz) çifti başına tek satır. Poz NULL olabilir (fail-open)."""
    artilar, eksiler, net, deger = _section_aggregates()
    return (
        _section_line_scope(section_id)
        .join(StockItem, StockItem.id == StockEntryLine.item_id)
        .outerjoin(BoqItem, BoqItem.id == StockEntryLine.boq_item_id)
        .with_only_columns(
            StockItem.id.label("item_id"),
            StockItem.code.label("code"),
            StockItem.name.label("name"),
            StockItem.category.label("category"),
            StockItem.unit.label("unit"),
            BoqItem.id.label("boq_item_id"),
            BoqItem.code.label("boq_code"),
            BoqItem.description.label("boq_description"),
            artilar.label("assigned_quantity"),
            eksiler.label("issued_quantity"),
            net.label("net_quantity"),
            deger.label("total_value"),
        )
        .group_by(
            StockItem.id,
            StockItem.code,
            StockItem.name,
            StockItem.category,
            StockItem.unit,
            BoqItem.id,
            BoqItem.code,
            BoqItem.description,
        )
    )


async def list_section_stock_rows(
    session: AsyncSession, section_id: uuid.UUID, *, limit: int, offset: int
) -> list[Row]:
    """Sıralama DETERMİNİSTİKTİR: kart kodu → poz kodu (NULL'lar SONA) → kimlik.

    Sayfalanan bir sorguda belirsiz sıralama, iki ardışık sayfada AYNI satırı
    gösterip başkasını hiç göstermez — `nulls_last` olmasaydı poza bağlanmamış
    satır motorun keyfine kalırdı.
    """
    stmt = (
        _section_rows_stmt(section_id)
        .order_by(
            StockItem.code,
            BoqItem.code.nulls_last(),
            StockItem.id,
        )
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).all())


async def count_section_stock_rows(session: AsyncSession, section_id: uuid.UUID) -> int:
    """`total` — sayfa değil KÜMEYİ sayar; liste ile AYNI ifadeden türer."""
    alt = _section_rows_stmt(section_id).subquery()
    return int(await session.scalar(select(func.count()).select_from(alt)) or 0)


async def section_stock_kpis(session: AsyncSession, section_id: uuid.UUID) -> Row:
    """Şerit SAYFAYI değil TÜM kümeyi özetler (`summary_kpis` deseni).

    `item_count` DISTINCT KART sayısıdır, satır sayısı değil: aynı kart iki
    ayrı poza çıkmışsa ekranda "2 malzeme" yazmak yanlış olurdu.
    """
    sifir = literal(Decimal("0"))
    _, eksiler, _, deger = _section_aggregates()
    stmt = _section_line_scope(section_id).with_only_columns(
        func.coalesce(
            func.sum(
                case(
                    (
                        StockEntryLine.quantity < 0,
                        -StockEntryLine.quantity * StockEntryLine.unit_price,
                    ),
                    else_=sifir,
                )
            ),
            sifir,
        ).label("issued_value"),
        deger.label("total_value"),
        func.count(func.distinct(StockEntryLine.item_id)).label("item_count"),
        func.count().filter(StockEntryLine.unit_price.is_(None)).label("lines_without_price"),
    )
    return (await session.execute(stmt)).one()


async def section_names_by_item(
    session: AsyncSession, site_id: uuid.UUID, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """ŞS "Bölüm" sütunu: sayfadaki kartların ATFEDİLDİĞİ bölüm adları.

    N+1 AÇILMAZ — sayfanın tüm kimlikleri tek `IN` ile sorulur.

    Kapsam `sections.site_id`dir, deponun değil: bölüm zaten şantiyeye bağlıdır
    (`sections.site_id` NOT NULL) ve yazma kapısı bölüm ile deponun şantiyesini
    eşitler. `Section` üzerinden süzmek, hareketin deposu sonradan silinse
    (`warehouses.site_id` SET NULL) bile sütunun doğru kalmasını sağlar.
    """
    if not item_ids:
        return {}
    stmt = (
        select(StockEntryLine.item_id, Section.name)
        .join(Section, Section.id == StockEntryLine.section_id)
        .where(Section.site_id == site_id, StockEntryLine.item_id.in_(item_ids))
        .distinct()
        .order_by(StockEntryLine.item_id, Section.name)
    )
    sonuc: dict[uuid.UUID, list[str]] = {}
    for item_id, ad in (await session.execute(stmt)).all():
        sonuc.setdefault(item_id, []).append(ad)
    return sonuc


def item_ids_attributed_to_section(section_id: uuid.UUID) -> Select:
    """ŞS `section_id` SÜZGECİ: o bölüme atfı olan kartların kimlikleri.

    🔴 Süzgeç SATIR KÜMESİNİ daraltır, `balance`ı DEĞİŞTİRMEZ. Bakiye depo
    düzeyindedir; bölüme göre süzülmüş bir "bakiye" bakiye DEĞİLDİR ve o adla
    basılsaydı ekran iki farklı anlamı aynı sütunda gösterirdi. Süzgecin
    cümlesi şudur: *"bu bölümde kullanılmış malzemelerin ŞANTİYE bakiyesi"*.
    """
    return select(StockEntryLine.item_id).where(StockEntryLine.section_id == section_id)


async def section_sites(session: AsyncSession, section_ids: set[uuid.UUID]) -> dict[uuid.UUID, Row]:
    """`section_id` → (`site_id`, `project_id`). TEK sorgu, N+1 yok."""
    if not section_ids:
        return {}
    stmt = (
        select(Section.id, Section.site_id, Site.project_id)
        .join(Site, Site.id == Section.site_id)
        .where(Section.id.in_(section_ids))
    )
    return {satir.id: satir for satir in (await session.execute(stmt)).all()}


async def boq_item_sites(session: AsyncSession, item_ids: set[uuid.UUID]) -> dict[uuid.UUID, Row]:
    """`boq_item_id` → (`site_id`, `project_id`). TEK sorgu, N+1 yok."""
    if not item_ids:
        return {}
    stmt = (
        select(BoqItem.id, BoqItem.site_id, Site.project_id)
        .join(Site, Site.id == BoqItem.site_id)
        .where(BoqItem.id.in_(item_ids))
    )
    return {satir.id: satir for satir in (await session.execute(stmt)).all()}


async def get_section(session: AsyncSession, section_id: uuid.UUID) -> Section | None:
    return await session.get(Section, section_id)

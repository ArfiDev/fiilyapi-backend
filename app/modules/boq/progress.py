"""ILR-1 — FIZIKSEL ilerleme: **PARA AGIRLIKLI** gerceklesme orani.

🔴 **TEK KAYNAK BOQ'DUR.** `sites/service/presenters.py`in bilerek acik biraktigi
formul burada, BIR kez yazilir; bolum ekrani da santiye ekrani da proje karti da
bu modulu CAGIRIR. Ikinci bir carpim yazmak (K3) ayni bolum icin iki farkli "%"
uretirdi — presenter'in yer tutucuyu yerinde birakma gerekcesi tam olarak buydu.

FORMUL
------
    ilerleme % = Σ(gerceklesen miktar × birim fiyat) / Σ(taban miktar × birim fiyat)

🔴 **NICIN PARA AGIRLIKLI, MIKTAR AGIRLIKLI DEGIL:** m² ile ton'un ortalamasi
alinamaz. Canlidaki gercek ornek (`A1 · Kenar Ayak`): 7.440 adet filiz ekimi
(412,50 ₺/adet) ve 98 ton demir (96.250 ₺/ton). Duz miktar ortalamasi filiz
ekimini demirden **75 kat onemli** sayar ve sayi SESSIZCE sacmalar — yuzde yine
0-100 arasinda ve "makul" gorunur. Bekcisi:
`tests/modules/test_ilr_fiziksel_ilerleme.py::test_MUTANT_para_agirligi_*`.

🔴 **FIYAT IKI TARAFTA DA CANLI `BoqItem.unit_price`TIR** — gunluk satirinin
kendi `unit_price` anlik goruntusu DEGIL. Gerekce: oran ancak PAY ve PAYDA ayni
fiyatla tartilirsa anlamlidir; snapshot kullanilsaydi bir fiyat guncellemesi
oranin kendisini kaydirir, %100'u asabilirdi. Emsal AYNIDIR:
`progress_payments/service.py:534` de `live_item.unit_price` kullanir.

KAPSAM (K-IKIZ1 karsit kanit bekcileri bunlari cakar)
----------------------------------------------------
* YALNIZ `submitted` gunluk sayilir — `draft` girmez (henuz beyan degildir).
* Poz baginin KOPMUS oldugu satir (`boq_item_id IS NULL`) girmez.
* Kapsam suzgeci `boq_items.site_id` / gunlugun `section_id`i uzerindendir;
  baska santiyenin/bolumun gunlugu ASLA girmez.
"""

import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import DisciplineScope, item_visible_clause
from app.modules.boq.models import BoqItem, BoqItemSectionAllocation
from app.modules.site_diary.models import DiaryStatus, SiteDiaryEntry, SiteDiaryLine
from app.modules.sites.models import Site

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")
_PCT = Decimal("0.01")


def quantize_pct(value: Decimal) -> Decimal:
    """Yuzde hassasiyeti 0,01 — `progress_payments.calculations.quantize2` emsali."""
    return value.quantize(_PCT, rounding=ROUND_HALF_UP)


def weighted_pct(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """🔑 ORANIN TEK TANIMI. Payda 0/negatifse yuzde YOKTUR (`None`) — sifira
    bolmek yerine zarf bos kalir; "0 %" demek "hic is yok"u "hic ilerleme yok"
    sanmaktir ve ikisi ayni sey degildir.
    """
    if denominator <= _ZERO:
        return None
    return quantize_pct(numerator / denominator * _HUNDRED)


def _line_section() -> ColumnElement[uuid.UUID]:
    """🔑 Uretimin BOLUMUNUN tek tanimi (PLN-B2.12): SATIRIN bolumu; satir bolumsuzse
    (eski veri ya da "Bolumsuz" yaprak) gunluk BASLIGININ bolumu geri donus olarak.

    PLN-B2.1'den beri bolum kirilimi SATIRDADIR (yaprak = kalem × bolum). Yalniz
    baslik okunsaydi baslik "A Blok" derken "B Blok"a yazilmis satir A'nin
    yuzdesine girerdi; geri donus olmasaydi baslik etiketiyle girilmis ESKI
    gunlukler hicbir bolum yuzdesine girmezdi. Iki bolum sorgusu (tekil ve
    toplu) BUNU cagirir — ikinci bir kopya iki ekranda iki farkli "%" uretirdi.
    """
    return func.coalesce(SiteDiaryLine.section_id, SiteDiaryEntry.section_id)


def _realized_line_sums(
    section_id: uuid.UUID | None = None,
) -> Select[tuple[uuid.UUID, Decimal]]:
    """`boq_item_id` -> GONDERILMIS gunluklerdeki toplam miktar.

    🔴 `DiaryStatus.submitted` suzgeci BURADA, tek yerdedir: her cagri yerinde
    tekrarlansaydi biri unutuldugunda taslak uretim sessizce yuzdeye girerdi.

    🔴 `section_id` verilirse PAY da o bolume daraltilir. Bolum `_line_section()`
    ile okunur: satirin bolumu, yoksa baslik etiketi (PLN-B2.12). Ikisi de bossa
    uretim santiye yuzdesine girer, hicbir BOLUM yuzdesine girmez — o uretimin
    hangi bolume ait oldugu BEYAN EDILMEMISTIR.
    """
    stmt = (
        select(
            SiteDiaryLine.boq_item_id.label("boq_item_id"),
            func.coalesce(func.sum(SiteDiaryLine.quantity), 0).label("realized"),
        )
        .join(SiteDiaryEntry, SiteDiaryEntry.id == SiteDiaryLine.entry_id)
        .where(
            SiteDiaryLine.boq_item_id.is_not(None),
            SiteDiaryEntry.status == DiaryStatus.submitted,
        )
        .group_by(SiteDiaryLine.boq_item_id)
    )
    if section_id is not None:
        stmt = stmt.where(_line_section() == section_id)
    return stmt


async def realized_by_item(
    session: AsyncSession,
    item_ids: list[uuid.UUID],
    section_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, Decimal]:
    """Poz basina GERCEKLESEN miktar. Bos istekte sorgu ACILMAZ (N+1 kurali).

    `section_id` verilirse yalniz o bolumun gunlukleri sayilir — BOQ ekrani
    bolume suzuldugunde `quantity` de o bolumun tahsisi oldugu icin PAY ve
    PAYDA ayni kapsamda kalir.
    """
    if not item_ids:
        return {}
    alt = _realized_line_sums(section_id).where(SiteDiaryLine.boq_item_id.in_(item_ids)).subquery()
    rows = await session.execute(select(alt.c.boq_item_id, alt.c.realized))
    return {row[0]: Decimal(row[1]) for row in rows}


async def _weighted_for_scope(
    session: AsyncSession,
    taban: Select[tuple[uuid.UUID, Decimal, Decimal]],
    section_id: uuid.UUID | None = None,
) -> tuple[Decimal, Decimal]:
    """`(poz_id, taban_miktar, birim_fiyat)` uclusunden PAY ve PAYDA'yi toplar.

    Kapsami cagiran belirler (santiye / bolum / proje); agirliklandirma ve
    `submitted` suzgeci HER kapsamda ayni kalir.
    """
    taban_alt = taban.subquery()
    realized_alt = _realized_line_sums(section_id).subquery()
    # 🔴 PAY **KIRPILMAZ** (`least(...)` YOK): tabani asan uretim gercek bir
    # olgudur (fazla imalat ya da hatali girdi) ve %100'e kirpmak onu SESSIZCE
    # gizlerdi. Emsal ayni: `progress_payments/service.py:563` de kirpmaz.
    stmt = select(
        func.coalesce(
            func.sum(func.coalesce(realized_alt.c.realized, 0) * taban_alt.c.unit_price),
            0,
        ),
        func.coalesce(func.sum(taban_alt.c.taban * taban_alt.c.unit_price), 0),
    ).select_from(
        taban_alt.join(
            realized_alt, realized_alt.c.boq_item_id == taban_alt.c.boq_item_id, isouter=True
        )
    )
    pay, payda = (await session.execute(stmt)).one()
    return Decimal(pay), Decimal(payda)


def _kapsam_maddeleri(scope: DisciplineScope | None) -> list[ColumnElement[bool]]:
    """DSC-B1: `scope` VERILMISSE (BOQ ekrani) tabana gorunur-kalem maddesi eklenir. `None`
    (kartlar, projeler — B4'e kadar) → madde YOK, SQL bugunku gibi kalir. `BoqItem` bu
    tabanlarin FROM'unda oldugu icin `item_visible_clause` korelasyonu meşrudur."""
    return [] if scope is None else [item_visible_clause(scope, BoqItem)]


def _site_taban(
    site_id: uuid.UUID, scope: DisciplineScope | None = None
) -> Select[tuple[uuid.UUID, Decimal, Decimal]]:
    """PAYDA = SANTIYE BOQ'u: pozun KENDI `quantity`si (santiye kotasi).

    `scope` (DSC-B1): PAY ve PAYDA ayni gorunur kalem kumesinden hesaplanir — B4'un
    kartlara baglayacagi parametre budur."""
    return select(
        BoqItem.id.label("boq_item_id"),
        BoqItem.quantity.label("taban"),
        BoqItem.unit_price.label("unit_price"),
    ).where(BoqItem.site_id == site_id, *_kapsam_maddeleri(scope))


def _section_taban(
    section_id: uuid.UUID, scope: DisciplineScope | None = None
) -> Select[tuple[uuid.UUID, Decimal, Decimal]]:
    """PAYDA = BOLUM TAHSISI (`BoqItemSectionAllocation`), pozun kotasi DEGIL.

    🔴 Gerekce: bolumun is evreni o boluma TAHSIS EDILEN miktardir. Pozun
    santiye kotasini payda yapmak, 1.200 m³ betonun 400'u tahsis edilmis bir
    bolumu 1.200 uzerinden olcerdi ve bolum %100 imal etse bile %33 gosterirdi.
    """
    return (
        select(
            BoqItemSectionAllocation.boq_item_id.label("boq_item_id"),
            BoqItemSectionAllocation.quantity.label("taban"),
            BoqItem.unit_price.label("unit_price"),
        )
        .join(BoqItem, BoqItem.id == BoqItemSectionAllocation.boq_item_id)
        .where(BoqItemSectionAllocation.section_id == section_id, *_kapsam_maddeleri(scope))
    )


def _project_taban(project_id: uuid.UUID) -> Select[tuple[uuid.UUID, Decimal, Decimal]]:
    """PAYDA = PROJE geneli BOQ: projenin TUM santiyelerinin pozlari."""
    return (
        select(
            BoqItem.id.label("boq_item_id"),
            BoqItem.quantity.label("taban"),
            BoqItem.unit_price.label("unit_price"),
        )
        .join(Site, Site.id == BoqItem.site_id)
        .where(Site.project_id == project_id)
    )


# --------------------------------------------------------------------------- #
# Genel API — hepsi TEK sorgu acar, hicbiri dongu icinde cagrilmaz (N+1 kurali)
# --------------------------------------------------------------------------- #


async def physical_for_site(
    session: AsyncSession, site_id: uuid.UUID, scope: DisciplineScope | None = None
) -> Decimal | None:
    pay, payda = await _weighted_for_scope(session, _site_taban(site_id, scope))
    return weighted_pct(pay, payda)


async def physical_for_section(
    session: AsyncSession, section_id: uuid.UUID, scope: DisciplineScope | None = None
) -> Decimal | None:
    pay, payda = await _weighted_for_scope(
        session, _section_taban(section_id, scope), section_id=section_id
    )
    return weighted_pct(pay, payda)


async def physical_for_project(session: AsyncSession, project_id: uuid.UUID) -> Decimal | None:
    pay, payda = await _weighted_for_scope(session, _project_taban(project_id))
    return weighted_pct(pay, payda)


async def physical_for_sections(
    session: AsyncSession, section_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal | None]:
    """TOPLU bolum yuzdesi — bolum LISTESI ekrani icin TEK sorgu.

    🔴 Bolum basina `physical_for_section` cagirmak N+1 acardi; `sites` liste
    ucu bolumleri dongu icinde sunar (`presenters.to_section`).
    """
    if not section_ids:
        return {}
    taban = (
        select(
            BoqItemSectionAllocation.section_id.label("section_id"),
            BoqItemSectionAllocation.boq_item_id.label("boq_item_id"),
            BoqItemSectionAllocation.quantity.label("taban"),
            BoqItem.unit_price.label("unit_price"),
        )
        .join(BoqItem, BoqItem.id == BoqItemSectionAllocation.boq_item_id)
        .where(BoqItemSectionAllocation.section_id.in_(section_ids))
        .subquery()
    )
    realized = (
        select(
            _line_section().label("section_id"),
            SiteDiaryLine.boq_item_id.label("boq_item_id"),
            func.coalesce(func.sum(SiteDiaryLine.quantity), 0).label("realized"),
        )
        .join(SiteDiaryEntry, SiteDiaryEntry.id == SiteDiaryLine.entry_id)
        .where(
            SiteDiaryLine.boq_item_id.is_not(None),
            SiteDiaryEntry.status == DiaryStatus.submitted,
            _line_section().in_(section_ids),
        )
        .group_by(_line_section(), SiteDiaryLine.boq_item_id)
        .subquery()
    )
    stmt = (
        select(
            taban.c.section_id,
            func.coalesce(func.sum(func.coalesce(realized.c.realized, 0) * taban.c.unit_price), 0),
            func.coalesce(func.sum(taban.c.taban * taban.c.unit_price), 0),
        )
        .select_from(
            taban.join(
                realized,
                (realized.c.section_id == taban.c.section_id)
                & (realized.c.boq_item_id == taban.c.boq_item_id),
                isouter=True,
            )
        )
        .group_by(taban.c.section_id)
    )
    rows = await session.execute(stmt)
    olculen = {row[0]: weighted_pct(Decimal(row[1]), Decimal(row[2])) for row in rows}
    # Tahsisi HIC olmayan bolum sorgudan DONMEZ; yuzdesi "yok"tur (0 DEGIL).
    return {sid: olculen.get(sid) for sid in section_ids}


async def physical_for_projects(
    session: AsyncSession, project_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal | None]:
    """TOPLU proje yuzdesi — proje KARTLARI icin TEK sorgu.

    🔴 `cost_cards.py` modul kuralı: kart turevleri PROJE BASINA sorgu ACMAZ.
    `physical_for_project`i dongu icinde cagirmak liste ucunda N+1 acardi.
    """
    if not project_ids:
        return {}
    taban = (
        select(
            Site.project_id.label("project_id"),
            BoqItem.id.label("boq_item_id"),
            BoqItem.quantity.label("taban"),
            BoqItem.unit_price.label("unit_price"),
        )
        .join(Site, Site.id == BoqItem.site_id)
        .where(Site.project_id.in_(project_ids))
        .subquery()
    )
    realized = _realized_line_sums().subquery()
    stmt = (
        select(
            taban.c.project_id,
            func.coalesce(func.sum(func.coalesce(realized.c.realized, 0) * taban.c.unit_price), 0),
            func.coalesce(func.sum(taban.c.taban * taban.c.unit_price), 0),
        )
        .select_from(
            taban.join(realized, realized.c.boq_item_id == taban.c.boq_item_id, isouter=True)
        )
        .group_by(taban.c.project_id)
    )
    rows = await session.execute(stmt)
    olculen = {row[0]: weighted_pct(Decimal(row[1]), Decimal(row[2])) for row in rows}
    return {pid: olculen.get(pid) for pid in project_ids}


async def physical_for_sites(
    session: AsyncSession, site_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal | None]:
    """TOPLU santiye yuzdesi — santiye KARTI listesi icin TEK sorgu.

    PAYDA = santiyenin BOQ'u (pozun kendi `quantity`si). `physical_for_site` ile
    AYNI tanimdir; bu yalnizca `IN (…)` kapsamli hâlidir.
    """
    if not site_ids:
        return {}
    taban = (
        select(
            BoqItem.site_id.label("site_id"),
            BoqItem.id.label("boq_item_id"),
            BoqItem.quantity.label("taban"),
            BoqItem.unit_price.label("unit_price"),
        )
        .where(BoqItem.site_id.in_(site_ids))
        .subquery()
    )
    realized = _realized_line_sums().subquery()
    stmt = (
        select(
            taban.c.site_id,
            func.coalesce(func.sum(func.coalesce(realized.c.realized, 0) * taban.c.unit_price), 0),
            func.coalesce(func.sum(taban.c.taban * taban.c.unit_price), 0),
        )
        .select_from(
            taban.join(realized, realized.c.boq_item_id == taban.c.boq_item_id, isouter=True)
        )
        .group_by(taban.c.site_id)
    )
    rows = await session.execute(stmt)
    olculen = {row[0]: weighted_pct(Decimal(row[1]), Decimal(row[2])) for row in rows}
    return {sid: olculen.get(sid) for sid in site_ids}

"""Disiplin kapsami (PORT) — cekirdek okuma/yazma yollarinin "kullanici hangi disiplinleri
gorur" sorusu (DISIPLIN-KAPSAMI-SPEC Ü9, `day_hooks` emsali).

Kural: kullaniciya disiplin atanmissa yalniz o disiplinlerin BOQ kalemlerini gorur;
atamasi olmayan kullanici KISITSIZdir. Cekirdek moduller planlamayi (EV) IMPORT ETMEZ
(§2.7); bu dosya sozlesmedir, bir modul (bugun `earned_value/discipline_adapter.py`)
uygulamasini `register_provider` ile KAYDEDER.

* Kayit YOKSA port bostur: `user_scope` → `UNRESTRICTED`, `item_visible_clause` →
  `ItemVisible(true())`, `item_discipline_expr` → `null()` (modulsuz kurulum bugunku gibi
  calisir).
* Bagimlilik yonu tek: modul → `app.core.discipline_scope`. Bu dosya hicbir urun modulunu
  import etmez (`BoqItem` bile) — kalem varligi PARAMETRE gelir (`BoqItem` ya da alias'i).
* `day_hooks`tan FARKLI olarak TEK saglayici tutulur (liste degil): "kalem hangi
  disiplinde" sorusunun tek dogru cevabi olmali; iki saglayici iki farkli tanim demektir ve
  sessizce ilki/sonuncusu kazansa gorunurluk ile yazma denetimi ayrisirdi. Ayni nesneyi
  ikinci kez kaydetmek no-op (idempotent), FARKLI nesne `RuntimeError`.

## Disiplin cozumu — TEK TANIM
Kalemin disiplini SQL ifadesidir (`item_discipline_expr`); Python tarafinda ikinci bir
tanim YOKTUR — `item_disciplines` ayni ifadeyi `select(BoqItem.id, expr)` ile kosar.

🔴 R(site) = AKTIF revizyon ?? TASLAK (arsiv yok sayilir). `budget_service.current_revision`
(taslak ?? aktif) ile BILINCLI olarak TERSTIR: gorunurluk calisma zamani verisiyle (gunluk
dagitim, ev_input, raporlar, baseline — hepsi AKTIF revizyona bakar) tutarli olmali. Taslakta
yeniden esleme, dondurmaya kadar gorunurlugu DEGISTIRMEZ. Revizyon yoksa / grup eslenmemisse
disiplin NULL'dur ve kisitli kullaniciya GORUNMEZ (fail-closed, spec Ü1).

🔴 SUZMEDE `NOT IN` / `!=` ASLA KULLANILMAZ. PostgreSQL'de OLCULDU: `NULL NOT IN (bos olmayan
kume)` → 0 satir (elenir), ama `NULL NOT IN (bos kume)` → 1 satir (FAIL-OPEN). Yani tumleyen
kume ("kullanicinin OLMAYAN disiplinleri") bos oldugunda — kullaniciya sirketin TUM
disiplinleri atandiginda — NULL disiplinli kalem gorunur olur. Yalniz `IN (kullanicinin
disiplinleri)`: NULL hicbir zaman `IN`e uymaz.

## Test uclusu
`unregister_all()` / `registered()` / `restore()` — `day_hooks` ile ayni sozlesme.

## Yapisal bekci (DSC-B1): `ItemVisible`
`item_visible_clause` HER ZAMAN `ItemVisible` doner (kisitsizda icindeki ifade `true`,
SQL'e yalniz `true` yazilir — atamasiz yanit degismez). `ItemVisible` DERLEME ANINDA kalem
varliginin FROM'u kapsayan SELECT'te (kendi FROM'u ya da korele edilmis dis FROM) olup
olmadigina bakar; yoksa `RuntimeError` — korelasyon dusup FAIL-OPEN olacagina sorgu
DERLENMEZ. Kisitsizda da sarmaladigimiz icin bekci her testte (atamasiz aktorle bile)
calisir. AST bekcisi `item_discipline_expr` sonucunun port disinda `.in_`/karsilastirma ile
kullanimini yasaklar (`tests/core/test_disiplin_korelasyon_bekcisi.py`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import ColumnElement, false, null, select, true
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import Select
from sqlalchemy.sql.visitors import InternalTraversal
from sqlalchemy.types import Boolean


@dataclass(frozen=True, slots=True)
class DisciplineScope:
    """Kullanicinin disiplin kapsami. `discipline_ids=None` → KISITSIZ.

    IZN-B3: disiplin PROJE BASINA atanir (`project_member_disciplines`). Iki bicim vardir:

    * TEK PROJE kapsami (`by_project=None`): istegin projesindeki atama. `discipline_ids` o
      projedeki kume; bos/None = o projede kisitsiz. Eski kodun tamami (`is_restricted`,
      `discipline_ids`, `item_visible_clause`) AYNEN bu bicimle calisir.
    * COK PROJE kapsami (`by_project` dolu, `discipline_ids=None`): proje baglamsiz liste
      uclari. `by_project` YALNIZ kisitli oldugu projeleri tasir (eksik proje = kisitsiz).
      `is_restricted` True'dur (herhangi bir projede kisitli), ama duz `discipline_ids` YOKTUR:
      tek-proje kodu bu kapsamla FAIL-CLOSED calisir (`item_visible_clause` → hicbir sey).
      Proje basina dogru sonuc `for_project` / `partition_by_project` ile alinir.

    Bos kume de kisitsizdir (bos atama = atamasiz, spec: "bos = kisitsiz"); saglayici
    `of()` ile uretir. `is_restricted` bu yuzden `bool(ids)`dir — bos kumeyle kurulmus bir
    nesne "hicbir sey gorunmez" DEMEZ.
    """

    discipline_ids: frozenset[uuid.UUID] | None
    by_project: Mapping[uuid.UUID, frozenset[uuid.UUID]] | None = None

    @property
    def is_restricted(self) -> bool:
        return bool(self.discipline_ids) or bool(self.by_project)

    @property
    def is_multi_project(self) -> bool:
        """Proje baglamsiz (cok proje) kapsam: duz `discipline_ids` yok, proje basina harita var."""
        return bool(self.by_project)

    @classmethod
    def of(cls, ids: frozenset[uuid.UUID] | set[uuid.UUID] | list[uuid.UUID]) -> DisciplineScope:
        """Atama kumesinden kapsam: bos → `UNRESTRICTED`."""
        return cls(frozenset(ids)) if ids else UNRESTRICTED

    @classmethod
    def of_projects(cls, by_project: Mapping[uuid.UUID, Iterable[uuid.UUID]]) -> DisciplineScope:
        """Proje → atama haritasindan cok proje kapsami; bos atamali projeler (kisitsiz) atilir."""
        kisitli = {pid: frozenset(ids) for pid, ids in by_project.items() if ids}
        return cls(None, kisitli) if kisitli else UNRESTRICTED

    def for_project(self, project_id: uuid.UUID) -> DisciplineScope:
        """Tek bir projenin kapsami. Tek-proje / kisitsiz kapsamda kendisi."""
        if not self.by_project:
            return self
        return DisciplineScope.of(self.by_project.get(project_id, ()))


UNRESTRICTED = DisciplineScope(None)


class DisciplineProvider(Protocol):
    """Modulun uyguladigi sozlesme. Uc yetenek + kalem→disiplin toplu esleme; hepsi AYNI
    SQL tanimindan turer (`item_discipline_expr`)."""

    async def user_scope(
        self, session: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID | None = None
    ) -> DisciplineScope:
        """`project_id` verilirse O PROJEDEKI tek-proje kapsami; `None` → cok proje kapsami
        (kullanicinin kisitli oldugu tum projeler)."""
        ...

    def item_discipline_expr(self, item: Any) -> ColumnElement[Any]:
        """`item` (BoqItem ya da alias'i) icin disiplin id'si (ya da NULL) SQL ifadesi."""
        ...

    def group_discipline_expr(self, group: Any) -> ColumnElement[Any]:
        """`group` (BoqGroup ya da alias'i) icin disiplin id'si (ya da NULL) SQL ifadesi —
        kalem ifadesiyle AYNI R(site) alt sorgusunu kullanir (tek tanim)."""
        ...

    async def item_disciplines(
        self, session: AsyncSession, item_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID | None]:
        """Kalem → disiplin (esleme yoksa None). Uygulama `item_discipline_expr`i KULLANMALI."""
        ...

    async def group_disciplines(
        self, session: AsyncSession, group_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID | None]:
        """Grup → disiplin (esleme yoksa None). Uygulama `group_discipline_expr`i KULLANMALI."""
        ...


_provider: DisciplineProvider | None = None


def register_provider(provider: DisciplineProvider) -> None:
    """Saglayiciyi kaydet. Ayni nesne tekrar → no-op; FARKLI nesne → `RuntimeError`."""
    global _provider
    if _provider is None:
        _provider = provider
    elif _provider is not provider:
        raise RuntimeError("Disiplin saglayicisi zaten kayitli (tek saglayici sozlesmesi)")


def unregister_all() -> None:
    """Yalniz testler icin: portu bosalt (modulsuz kurulumu taklit)."""
    global _provider
    _provider = None


def registered() -> DisciplineProvider | None:
    return _provider


def restore(snapshot: DisciplineProvider | None) -> None:
    """Yalniz testler icin: `registered()` fotografini GERI yukle. 🔴 `unregister_all()`
    kullanan her fikstur sonunda bunu cagirmali — yoksa ayni isci surecinde sonra kosan
    testler EV kaydini kaybeder ve SAHTE-YESIL gecer (kisit hic uygulanmaz)."""
    global _provider
    _provider = snapshot


async def user_scope(
    session: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID | None = None
) -> DisciplineScope:
    """Kullanicinin kapsami: `project_id` verilirse o projede, yoksa COK PROJE kapsami. Kayit
    yoksa `UNRESTRICTED`."""
    if _provider is None:
        return UNRESTRICTED
    return await _provider.user_scope(session, user_id, project_id)


def partition_by_project(
    scope: DisciplineScope, project_ids: Iterable[uuid.UUID]
) -> list[tuple[DisciplineScope, list[uuid.UUID]]]:
    """Projeleri AYNI tek-proje kapsamini paylasanlara boler (cok proje toplulastirmasi icin).

    Kisitsiz projeler tek parcada (`UNRESTRICTED`), kisitli her proje kendi kapsamiyla ayri parca
    olur; sira girdi sirasini korur. Tek-proje / kisitsiz kapsamda tek parca doner. Cagiran her
    parcayi mevcut tek-proje sorgusuyla kosup sonuclari birlestirir.
    """
    ids = list(dict.fromkeys(project_ids))
    if not scope.is_multi_project:
        return [(scope, ids)] if ids else []
    parcalar: dict[DisciplineScope, list[uuid.UUID]] = {}
    sirali: list[DisciplineScope] = []
    for project_id in ids:
        kapsam = scope.for_project(project_id)
        if kapsam not in parcalar:
            parcalar[kapsam] = []
            sirali.append(kapsam)
        parcalar[kapsam].append(project_id)
    return [(kapsam, parcalar[kapsam]) for kapsam in sirali]


def item_discipline_expr(item: Any) -> ColumnElement[Any]:
    """Kalemin disiplini (SQL, tek tanim). Kayit yoksa `NULL` sabiti."""
    if _provider is None:
        return null()
    return _provider.item_discipline_expr(item)


def group_discipline_expr(group: Any) -> ColumnElement[Any]:
    """BOQ grubunun disiplini (SQL, tek tanim). Kayit yoksa `NULL` sabiti."""
    if _provider is None:
        return null()
    return _provider.group_discipline_expr(group)


#: `item_disciplines` tek sorguda en fazla bu kadar id gonderir: asyncpg sorgu basina 32767
#: parametre sinirina sahiptir (40k id'de `InterfaceError` OLCULDU).
ITEM_ID_CHUNK = 10_000


async def item_disciplines(
    session: AsyncSession, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID | None]:
    """Kalem → disiplin toplu esleme. Kayit yoksa her kalem None. Bos girdi → bos sozluk.
    Id'ler `ITEM_ID_CHUNK` parcalarla sorgulanir (parametre siniri) ve birlestirilir."""
    if not item_ids:
        return {}
    if _provider is None:
        return {item_id: None for item_id in item_ids}
    result: dict[uuid.UUID, uuid.UUID | None] = {}
    for start in range(0, len(item_ids), ITEM_ID_CHUNK):
        result.update(
            await _provider.item_disciplines(session, item_ids[start : start + ITEM_ID_CHUNK])
        )
    return result


async def group_disciplines(
    session: AsyncSession, group_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID | None]:
    """Grup → disiplin toplu esleme (`item_disciplines` ile ayni sozlesme, ayni parcalama)."""
    if not group_ids:
        return {}
    if _provider is None:
        return {group_id: None for group_id in group_ids}
    result: dict[uuid.UUID, uuid.UUID | None] = {}
    for start in range(0, len(group_ids), ITEM_ID_CHUNK):
        result.update(
            await _provider.group_disciplines(session, group_ids[start : start + ITEM_ID_CHUNK])
        )
    return result


class ItemVisible(ColumnElement[bool]):
    """Kalem gorunurluk maddesi SARMALAYICISI: `inner` ifadesini oldugu gibi uretir, ama
    derlenirken `item` (kalem varligi) kapsayan SELECT'in FROM'unda degilse `RuntimeError`
    atar (`_derle_item_visible`). `_traverse_internals` onbellek anahtarina hem `inner`
    (kapsam parametreleri) hem `item_from` (BoqItem mi alias mi) girer."""

    __visit_name__ = "item_visible"
    inherit_cache = True
    type = Boolean()
    _traverse_internals = [
        ("inner", InternalTraversal.dp_clauseelement),
        ("item_from", InternalTraversal.dp_clauseelement),
    ]

    def __init__(self, inner: ColumnElement[Any], item: Any) -> None:
        self.inner = inner
        self.item_from = sa_inspect(item).selectable


@compiles(ItemVisible)
def _derle_item_visible(element: ItemVisible, compiler: Any, **kw: Any) -> str:
    """Kalem FROM'u kapsayan SELECT'te yoksa DERLEME hatasi (korelasyon dustu → fail-open).

    Ust duzey (kapsayan SELECT'siz) `str(...)` derlemesi hata DEGILDIR: korele edilecek bir
    dis sorgu yoktur, yalniz gosterim amaclidir."""
    if compiler.stack:
        kapsam = compiler.stack[-1]
        froms = set(kapsam.get("correlate_froms", ())) | set(kapsam.get("asfrom_froms", ()))
        if element.item_from not in froms:
            raise RuntimeError(
                "ItemVisible: kalem varligi kapsayan sorgunun FROM'unda degil — korelasyon "
                "dusup kisit FAIL-OPEN olurdu. Baska tablodan suzerken `item_fk_conditions` / "
                "`visible_item_ids` kullanin."
            )
    return compiler.process(element.inner, **kw)


def item_visible_clause(scope: DisciplineScope, item: Any) -> ColumnElement[bool]:
    """WHERE maddesi: kisitsizda `true`, kisitlida `disiplin IN (kapsam)` — HER ZAMAN
    `ItemVisible` ile sarili (bkz. modul docstring'i, "Yapisal bekci").

    🔴 `NOT IN` / `!=` YOK — NULL disiplinli kalem kisitliya GORUNMEZ (fail-closed).

    🔴 KALEM VARLIGI DIS SORGUNUN FROM'UNDA OLMALI. Disiplin alt sorgusu `item`a korele
    edilir; `item` dis FROM'da yoksa korelasyon SESSIZCE dusar ve alt sorgu
    `FROM ev_group_disciplines, boq_items` olur: tek esleme varsa FAIL-OPEN (OLCULDU:
    `select(SiteDiaryLine.code).where(item_visible_clause(scope, BoqItem))` {'X','C'} yerine
    {'C'} donmeliydi), cok esleme varsa `CardinalityViolation`. Bu durum artik DERLEMEDE
    `RuntimeError`dur. Baska tablodan suzerken `item_fk_conditions` / `visible_item_ids`.
    """
    if not scope.is_restricted:
        return ItemVisible(true(), item)
    if scope.is_multi_project:  # proje baglamsiz kapsam: duz kume yok → FAIL-CLOSED
        return ItemVisible(false(), item)
    return ItemVisible(
        item_discipline_expr(item).in_(sorted(scope.discipline_ids or (), key=str)), item
    )


def group_visible_clause(scope: DisciplineScope, group: Any) -> ColumnElement[bool]:
    """Grup icin `item_visible_clause` esi (DSC-B2): kisitsizda `true`, kisitlida
    `grup disiplini IN (kapsam)`. AYNI `ItemVisible` sarmalayicisi (kurucusu varlik
    bagimsiz) → grup FROM'u kapsayan sorguda yoksa derleme `RuntimeError` verir.
    🔴 `NOT IN` / `!=` YOK — eslenmemis grup kisitliya GORUNMEZ (fail-closed)."""
    if not scope.is_restricted:
        return ItemVisible(true(), group)
    if scope.is_multi_project:  # proje baglamsiz kapsam: duz kume yok → FAIL-CLOSED
        return ItemVisible(false(), group)
    return ItemVisible(
        group_discipline_expr(group).in_(sorted(scope.discipline_ids or (), key=str)), group
    )


def visible_item_ids(scope: DisciplineScope, item: Any) -> Select[Any]:
    """KENDI KENDINE YETEN alt sorgu: kapsamdaki kalemlerin id'leri (`select(item.id)`).

    Baska tablodan suzme kalibi: `X.boq_item_id.in_(visible_item_ids(scope, BoqItem))`.
    Kalem varligi ic sorgunun kendi FROM'unda oldugu icin korelasyon bozulamaz
    (`item_visible_clause`in dis-FROM tuzagi yok). Kisitsizda TUM kalemleri doner (NULL
    disiplinliler dahil); cagiran `scope.is_restricted` False ise filtreyi HIC eklememelidir
    (gereksiz alt sorgu + `boq_item_id IS NULL` satirlarini eleme riski).
    """
    return select(item.id).where(item_visible_clause(scope, item))


def item_fk_conditions(
    scope: DisciplineScope, fk_column: Any, item: Any
) -> list[ColumnElement[bool]]:
    """Baska tablonun `boq_item_id` FK'sini gorunur kalemle sinirlayan WHERE maddeleri.

    Kisitsizda BOS liste (filtre HIC eklenmez: `boq_item_id IS NULL` satirlari da kalir,
    atamasiz yanit degismez); kisitlida `fk IN (gorunur kalem id'leri)` — NULL FK gorunmez
    (Ü1). Kalem varligi ic sorgunun kendi FROM'unda oldugu icin korelasyon bozulamaz.

    Ic sorgu `item`in ALIAS'i ile kurulur: cagiran sorgu ayni kalem varligini kendi
    FROM'unda tasiyorsa (ornegin `outerjoin(BoqItem)`) SQLAlchemy ic sorguyu ona OTOMATIK
    korele edip FROM'unu dusururdu ("no FROM clauses" ya da yanlis satir kumesi).
    """
    if not scope.is_restricted:
        return []
    return [fk_column.in_(visible_item_ids(scope, aliased(item)))]


async def visible_item_set(
    session: AsyncSession, scope: DisciplineScope, item_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID] | None:
    """Verilen kalem id'lerinden kapsamda GORUNENLER; kisitsizda `None` (= hepsi gorunur,
    filtre yok). Python tarafi ayni tek tanimdan (`item_disciplines` → `item_discipline_expr`)
    turer; disiplinsiz (None) kalem hicbir kumeye uymaz (fail-closed)."""
    if not scope.is_restricted:
        return None
    ids = list(dict.fromkeys(item_ids))
    disiplin = await item_disciplines(session, ids)
    izinli = scope.discipline_ids or frozenset()
    return {item_id for item_id, discipline_id in disiplin.items() if discipline_id in izinli}


async def visible_group_set(
    session: AsyncSession, scope: DisciplineScope, group_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID] | None:
    """Verilen grup id'lerinden kapsamda GORUNENLER; kisitsizda `None` (= hepsi gorunur).
    `visible_item_set` ile ayni tanim: eslenmemis grup hicbir kumeye uymaz (fail-closed)."""
    if not scope.is_restricted:
        return None
    ids = list(dict.fromkeys(group_ids))
    disiplin = await group_disciplines(session, ids)
    izinli = scope.discipline_ids or frozenset()
    return {group_id for group_id, discipline_id in disiplin.items() if discipline_id in izinli}

"""Disiplin kapsami (PORT) — cekirdek okuma/yazma yollarinin "kullanici hangi disiplinleri
gorur" sorusu (DISIPLIN-KAPSAMI-SPEC Ü9, `day_hooks` emsali).

Kural: kullaniciya disiplin atanmissa yalniz o disiplinlerin BOQ kalemlerini gorur;
atamasi olmayan kullanici KISITSIZdir. Cekirdek moduller planlamayi (EV) IMPORT ETMEZ
(§2.7); bu dosya sozlesmedir, bir modul (bugun `earned_value/discipline_adapter.py`)
uygulamasini `register_provider` ile KAYDEDER.

* Kayit YOKSA port bostur: `user_scope` → `UNRESTRICTED`, `item_visible_clause` → `true()`,
  `item_discipline_expr` → `null()` (modulsuz kurulum bugunku gibi calisir).
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
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import ColumnElement, null, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import ScalarSelect, Select


@dataclass(frozen=True, slots=True)
class DisciplineScope:
    """Kullanicinin disiplin kapsami. `discipline_ids=None` → KISITSIZ.

    Bos kume de kisitsizdir (bos atama = atamasiz, spec: "bos = kisitsiz"); saglayici
    `of()` ile uretir. `is_restricted` bu yuzden `bool(ids)`dir — bos kumeyle kurulmus bir
    nesne "hicbir sey gorunmez" DEMEZ.
    """

    discipline_ids: frozenset[uuid.UUID] | None

    @property
    def is_restricted(self) -> bool:
        return bool(self.discipline_ids)

    @classmethod
    def of(cls, ids: frozenset[uuid.UUID] | set[uuid.UUID] | list[uuid.UUID]) -> DisciplineScope:
        """Atama kumesinden kapsam: bos → `UNRESTRICTED`."""
        return cls(frozenset(ids)) if ids else UNRESTRICTED


UNRESTRICTED = DisciplineScope(None)


class DisciplineProvider(Protocol):
    """Modulun uyguladigi sozlesme. Uc yetenek + kalem→disiplin toplu esleme; hepsi AYNI
    SQL tanimindan turer (`item_discipline_expr`)."""

    async def user_scope(self, session: AsyncSession, user_id: uuid.UUID) -> DisciplineScope: ...

    def item_discipline_expr(self, item: Any) -> ColumnElement[Any]:
        """`item` (BoqItem ya da alias'i) icin disiplin id'si (ya da NULL) SQL ifadesi."""
        ...

    def user_discipline_ids_subquery(self, user_id: uuid.UUID) -> ScalarSelect[Any] | Any:
        """`expr.in_(...)` icin kullanicinin atanmis disiplinleri (alt sorgu)."""
        ...

    async def item_disciplines(
        self, session: AsyncSession, item_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID | None]:
        """Kalem → disiplin (esleme yoksa None). Uygulama `item_discipline_expr`i KULLANMALI."""
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


async def user_scope(session: AsyncSession, user_id: uuid.UUID) -> DisciplineScope:
    """Kullanicinin kapsami. Kayit yoksa `UNRESTRICTED`."""
    if _provider is None:
        return UNRESTRICTED
    return await _provider.user_scope(session, user_id)


def item_discipline_expr(item: Any) -> ColumnElement[Any]:
    """Kalemin disiplini (SQL, tek tanim). Kayit yoksa `NULL` sabiti."""
    if _provider is None:
        return null()
    return _provider.item_discipline_expr(item)


def user_discipline_ids_subquery(user_id: uuid.UUID) -> Any:
    """`IN` icin atanmis disiplin alt sorgusu; kayit yoksa None. Yalniz KISITLI kullanici icin
    anlamlidir (atamasiz kullanici bos alt sorgu = hicbir sey gorunmez olurdu)."""
    if _provider is None:
        return None
    return _provider.user_discipline_ids_subquery(user_id)


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


def item_visible_clause(scope: DisciplineScope, item: Any) -> ColumnElement[bool]:
    """WHERE maddesi: kisitsizda `true()`, kisitlida `disiplin IN (kapsam)`.

    🔴 `NOT IN` / `!=` YOK — NULL disiplinli kalem kisitliya GORUNMEZ (fail-closed).

    🔴 KALEM VARLIGI DIS SORGUNUN FROM'UNDA OLMALI. Disiplin alt sorgusu `item`a korele
    edilir; `item` dis FROM'da yoksa korelasyon SESSIZCE dusar ve alt sorgu
    `FROM ev_group_disciplines, boq_items` olur: tek esleme varsa FAIL-OPEN (OLCULDU:
    `select(SiteDiaryLine.code).where(item_visible_clause(scope, BoqItem))` {'X','C'} yerine
    {'C'} donmeliydi), cok esleme varsa `CardinalityViolation`. Baska tablodan suzerken
    `visible_item_ids` kullanin.
    """
    if not scope.is_restricted:
        return true()
    return item_discipline_expr(item).in_(sorted(scope.discipline_ids or (), key=str))


def visible_item_ids(scope: DisciplineScope, item: Any) -> Select[Any]:
    """KENDI KENDINE YETEN alt sorgu: kapsamdaki kalemlerin id'leri (`select(item.id)`).

    Baska tablodan suzme kalibi: `X.boq_item_id.in_(visible_item_ids(scope, BoqItem))`.
    Kalem varligi ic sorgunun kendi FROM'unda oldugu icin korelasyon bozulamaz
    (`item_visible_clause`in dis-FROM tuzagi yok). Kisitsizda TUM kalemleri doner (NULL
    disiplinliler dahil); cagiran `scope.is_restricted` False ise filtreyi HIC eklememelidir
    (gereksiz alt sorgu + `boq_item_id IS NULL` satirlarini eleme riski).
    """
    return select(item.id).where(item_visible_clause(scope, item))

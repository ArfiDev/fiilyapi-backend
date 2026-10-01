"""Son fiyat (PORT) — bir katalog kaleminin en son gorulen birim fiyati (TKL-B3.2, plan §3).

Kaynaklar (sozlesme, hakedis, ileride teklif/satinalma) ayri moduldedir; katalog hepsini
import etmez. Bu dosya SOZLESMEDIR: katalog yalniz `latest()` cagirir; her kaynak modul
kendi `Provider`ini `register_provider` ile KAYDEDER (emsal: `day_hooks`).

* Kayit YOKSA port bostur → `latest` `{}` doner (modulsuz kurulum).
* Bagimlilik yonu tek: modul → `app.core.last_price`. Bu dosya hicbir urun modulunu import
  etmez.
* Her saglayici TEK TOPLU sorgu calistirir (`DISTINCT ON (catalog_item_id)`); kalem sayisi
  sorgu sayisini DEGISTIRMEZ (N+1 yok). Bos `catalog_ids` icin saglayici CAGRILMAZ.

## Birlestirme kurali (kalem basina)

1. En yeni `at` kazanir.
2. `at` ESIT ise (deterministik, kayit sirasindan bagimsiz): once `SOURCE_ORDER`daki sira
   (listede onde olan kazanir; listede olmayan kaynak en sonda, kendi adina gore), sonra
   `doc_no` (buyuk olan), sonra `str(doc_id)` (buyuk olan; None < her id).

## Hata yalitimi (bilincli: YOK)

Bir saglayici hata verirse `latest` hatayi yukseltir; saglayicilar arasinda yalitim yoktur
(katalog listesi 500 doner). Sessizce eksik fiyat gostermek yanlis fiyat gostermekten
kotudur — bilincli karar.

## Kapsam notu (izin turu)

Son fiyat SIRKET GENELIDIR: kaynaklar proje kapsamli belgelerden (sozlesme/hakedis) turer
ama burada proje kapsami SUZULMEZ — kisitli kullanici erisemedigi projenin fiyatini
gorebilir. Katalog yanitinda alanin tamami `para` kovasindadir (`limited` gormez); proje
kapsami sizintisi izin turunun konusudur.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class LastPrice:
    price: Decimal
    at: datetime
    source: str  # "SZL" | "HK" | (ileride) "TKL" | "SA"
    doc_no: str
    doc_id: uuid.UUID | None


Provider = Callable[[AsyncSession, Sequence[uuid.UUID]], Awaitable[dict[uuid.UUID, LastPrice]]]

#: Esit `at`te kaynak onceligi (once gelen kazanir). Yeni kaynak eklenince BURAYA yazilir.
SOURCE_ORDER: tuple[str, ...] = ("HK", "SZL", "TKL", "SA")

_providers: list[tuple[str, Provider]] = []


def register_provider(source: str, provider: Provider) -> None:
    """Ayni `(source, provider)` ikilisi no-op; ayni `source` + FARKLI saglayici → RuntimeError."""
    for existing_source, existing in _providers:
        if existing_source != source:
            continue
        if existing is provider:
            return
        raise RuntimeError(f"'{source}' kaynağı için farklı bir son fiyat sağlayıcısı kayıtlı")
    _providers.append((source, provider))


def unregister_all() -> None:
    """Yalniz testler icin: portu bosalt (modulsuz kurulumu taklit)."""
    _providers.clear()


def registered() -> tuple[tuple[str, Provider], ...]:
    return tuple(_providers)


def restore(snapshot: tuple[tuple[str, Provider], ...]) -> None:
    """Yalniz testler icin: `registered()` fotografini GERI yukle. 🔴 `unregister_all()`
    kullanan her fikstur sonunda bunu cagirmali — yoksa ayni isci surecinde sonra kosan
    testler kaydi kaybeder ve SAHTE-YESIL gecer."""
    _providers[:] = list(snapshot)


def _rank(source: str) -> int:
    return SOURCE_ORDER.index(source) if source in SOURCE_ORDER else len(SOURCE_ORDER)


def _key(price: LastPrice) -> tuple:
    """Buyuk olan kazanir: en yeni `at`, sonra onde gelen kaynak, sonra doc_no, doc_id."""
    return (
        price.at,
        -_rank(price.source),
        price.source if price.source not in SOURCE_ORDER else "",
        price.doc_no,
        str(price.doc_id) if price.doc_id is not None else "",
    )


async def latest(
    session: AsyncSession, catalog_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, LastPrice]:
    """Her kalem icin kayitli kaynaklarin en yenisi; fiyati olmayan kalem sonucta YOKTUR.
    Her saglayici tek toplu sorgu; bos girdide saglayici cagrilmaz."""
    ids = list(dict.fromkeys(catalog_ids))
    if not ids or not _providers:
        return {}
    best: dict[uuid.UUID, LastPrice] = {}
    for _source, provider in tuple(_providers):
        for catalog_id, candidate in (await provider(session, ids)).items():
            current = best.get(catalog_id)
            if current is None or _key(candidate) > _key(current):
                best[catalog_id] = candidate
    return best

"""Sozlesme tohumu (PORT) — teklif→proje donusturmesinin sozlesme kalemlerini diger modullere
duyurmasi (TKL-B6, TKL-PLAN §4.4-§4.5).

Donusturme (cekirdek) yeni projenin sozlesme grup/kalemlerini ve BOQ dagitimini yazar; ardindan
istege bagli modullere (bugun Planlama/EV: `earned_value/contract_adapter.py`, B6.3) "su
sozlesme kalemleri tohumlandi" der. Bu dosya o temasin SOZLESMESIDIR; `day_hooks` emsali.

* Kayit YOKSA port bostur → `run_seed_hooks` `[]` doner (modulsuz kurulum, §2.7).
* Bagimlilik yonu tek: modul → `app.core.contract_seed`. Bu dosya hicbir urun modulunu import
  etmez (EV dahil).
* Birden cok kayit desteklenir (liste); sira kayit sirasidir; ayni nesne tekrar kaydi no-op.
* Hata YUKSELIR (yutulmaz): kanca patlarsa cagiranin islemi (donusturme) GERI ALINIR.
  Uyarilar hata degildir: yapisal `code` + Turkce metin, cagiranin yanitina tasinir.
* Commit ETMEZ: kancalar cagiranin oturumunda ve islemindedir.

Tasarim notu (TKL-PLAN §4.4'e gore sapma, olcumle): `freeze` yok (T34: dondurma Planlama'dan);
`actor` yerine `actor_id` (EV adaptoru `User`i kendi cozer); kalem basina disiplin yerine grup
basina elle esleme (`group_disciplines`, SO-31); donus `None` degil uyari listesi.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ContractItemSeed:
    """Tohumlanan TEK sozlesme kalemi."""

    contract_item_id: uuid.UUID
    catalog_item_id: uuid.UUID | None
    unit_mhr: Decimal | None
    #: True = adam-saat tekliftekidir ve katalog standardindan FARKLIDIR (SO-33) → kaynak "teklif".
    rate_is_offer: bool


@dataclass(frozen=True, slots=True)
class ContractSeedRequest:
    project_id: uuid.UUID
    #: Donusturmede santiye acildiysa o santiye; yoksa None (oran yuvasi yine yazilir).
    site_id: uuid.UUID | None
    start: date | None
    end: date | None
    items: tuple[ContractItemSeed, ...]
    #: Sozlesme grup id → disiplin id (elle esleme, SO-31). Eslenmeyen grup = disiplinsiz.
    group_disciplines: Mapping[uuid.UUID, uuid.UUID]
    actor_id: uuid.UUID | None
    #: Taslak revizyon etiketi (ornegin "Rev.0 — TKL-2026-014").
    label: str


@dataclass(frozen=True, slots=True)
class SeedWarning:
    """Yapisal `code` (istemci metne bakmaz) + Turkce metin; gruba ozgu ise `group_id`."""

    code: str
    message: str
    group_id: uuid.UUID | None = None


SeedHook = Callable[[AsyncSession, ContractSeedRequest], Awaitable[Sequence[SeedWarning]]]

_hooks: list[SeedHook] = []


def register_seed_hook(hook: SeedHook) -> None:
    """Ayni nesne ikinci kez kaydedilirse no-op (import yan etkisi tekrarlanabilir)."""
    if hook not in _hooks:
        _hooks.append(hook)


def unregister_all() -> None:
    """Yalniz testler icin: portu bosalt (modulsuz kurulumu taklit)."""
    _hooks.clear()


def registered() -> tuple[SeedHook, ...]:
    return tuple(_hooks)


def restore(snapshot: tuple[SeedHook, ...]) -> None:
    """Yalniz testler icin: `registered()` fotografini GERI yukle. 🔴 `unregister_all()`
    kullanan her fikstur sonunda bunu cagirmali — yoksa ayni isci surecinde sonra kosan
    testler modul kaydini (ornegin EV adaptoru) kaybeder ve SAHTE-YESIL gecer."""
    _hooks[:] = list(snapshot)


async def run_seed_hooks(session: AsyncSession, req: ContractSeedRequest) -> list[SeedWarning]:
    """Kayitli kancalari kayit sirasiyla kosar; uyarilari birlestirip dondurur. Kayit yoksa `[]`.
    Kanca hatasi YUKSELIR → cagiranin islemi geri alinir."""
    warnings: list[SeedWarning] = []
    for hook in tuple(_hooks):
        warnings.extend(await hook(session, req))
    return warnings

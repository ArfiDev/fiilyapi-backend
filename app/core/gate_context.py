"""Kapı bağlamı (IZN-B3): istek boyunca GEÇİLEN kapıların proje içi (sayfa, bayrak) çiftleri.

`core/page_gate` kararı verir ve BU modüle yazar (kapı yazmayı unutamaz); `projects.service.
visible_projects` buradan okur (`core/project_access.roles_satisfying`). Bu dosya `page_gate`e
BAĞIMLI DEĞİLDİR (döngüyü önler).

HTTP isteği içinde hiçbir kapı geçmediyse (`gate_ran` yanlış) ve çağıran (sayfa, bayrak) çiftlerini
açıkça vermediyse `visible_projects` HATA atar (test/dev) ya da boş döner (üretim): üyelik tek
başına yetki DEĞİLDİR. HTTP DIŞI doğrudan servis çağrılarında (test/betik) yalnız üyelik süzgeci.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import DbSession
from app.core.sayfalar import SAYFA_BY_KEY, PageKind

Flag = Literal["view", "edit", "approve"]
PageFlag = tuple[str, Flag]

GATE_KEY: Final = "izn_b3.gate_context"

#: Bir kapının (VEYA'lı) çift grubu: `require_any_permission` birden çok kapıyı tek grupta toplar.
GateGroup = tuple[PageFlag, ...]


class MissingGateContextError(RuntimeError):
    """HTTP isteği İÇİNDE hiçbir kapı geçmeden `visible_projects` çağrıldı (fail-closed bekçisi).

    Kapısız bir yol (onay kutusu, AI aracı, gösterge paneli, servis içi çağrı) gerekli (sayfa,
    bayrak) çiftlerini `visible_projects(..., pairs=...)` ile AÇIKÇA vermelidir; yoksa proje
    süzgeci yalnız üyeliğe düşer ve başka projedeki ekip rolü yetki gibi kullanılırdı. Test/dev
    ortamında bu hata atılır; üretimde fail-closed çalışır (boş küme, ekip rolü sayılmaz).
    """


@dataclass(slots=True)
class GateContext:
    """İstek boyunca geçilen kapılar. `ran`: en az bir kapı geçti (şirket geneli olsa bile);
    `groups`: kapıların PROJE İÇİ (sayfa, bayrak) grupları (hepsi VE'lenir)."""

    groups: list[GateGroup] = field(default_factory=list)
    ran: bool = False


def project_pairs(pairs: GateGroup) -> GateGroup:
    """Bayrak çiftlerinden YALNIZ proje içi sayfalar (`tur=proje`): ekip rolü yalnız onlarda."""
    return tuple((key, flag) for key, flag in pairs if SAYFA_BY_KEY[key].tur is PageKind.proje)


def _context(session: AsyncSession) -> GateContext | None:
    return session.info.get(GATE_KEY)


def in_request(session: AsyncSession) -> bool:
    """`gate_request_scope` bu oturum için bir HTTP isteği başlattı mı?"""
    return _context(session) is not None


def gate_ran(session: AsyncSession) -> bool:
    context = _context(session)
    return context is not None and context.ran


def record_gate(session: AsyncSession, pairs: GateGroup) -> None:
    """Geçilen kapıyı bağlama yazar (YALNIZ HTTP isteği içinde). Şirket geneli kapı da "kapı
    çalıştı" işaretini koyar ama grup eklemez: onda ekip rolünün söyleyeceği bir şey yoktur.

    `core/page_gate`in `gate_ok`/`pages_ok`/`page_ok` fonksiyonları bunu KENDİ İÇİNDE çağırır:
    kapı yazmayı unutamaz (IZN-B3 onarımı, KRİTİK bulgu: onay uçları kaydı unutmuştu)."""
    context = _context(session)
    if context is None:
        return
    context.ran = True
    scoped = project_pairs(pairs)
    if scoped:
        context.groups.append(scoped)


def recorded_groups(session: AsyncSession) -> list[GateGroup]:
    context = _context(session)
    return list(context.groups) if context is not None else []


async def gate_request_scope(session: DbSession) -> AsyncGenerator[None, None]:
    """Uygulama bağımlılığı: istek başında BOŞ bağlam açar, sonunda kapatır (testlerdeki ortak
    oturumda bir önceki isteğin kapısı sızmasın)."""
    session.info[GATE_KEY] = GateContext()
    try:
        yield
    finally:
        session.info.pop(GATE_KEY, None)

"""Rol hücrelerinden sayfa haritası: katalogdan TAMAMLAMA (ortak: /roles, /auth/me)."""

from collections.abc import Iterable
from typing import Protocol

from app.core.sayfalar import SAYFA_ANAHTARLARI, SAYFA_BY_KEY, PageLevel
from app.modules.pages.schemas import PageGrant


class _Cell(Protocol):
    page_key: str
    level: PageLevel
    can_approve: bool


def grants_from_cells(cells: Iterable[_Cell]) -> dict[str, PageGrant]:
    """Rolün 100 sayfası: satırı olmayan sayfa `none` (approve=false) sayılır, katalogdan
    kalkmış anahtar süzülür. Kapılar eksik hücreyi zaten `none` sayar; yanıt da aynısını yapar."""
    by_key = {cell.page_key: cell for cell in cells if cell.page_key in SAYFA_BY_KEY}
    return {
        key: PageGrant(level=by_key[key].level, approve=by_key[key].can_approve)
        if key in by_key
        else PageGrant(level=PageLevel.none, approve=False)
        for key in SAYFA_ANAHTARLARI
    }

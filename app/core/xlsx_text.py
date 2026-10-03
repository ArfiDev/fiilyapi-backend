"""Excel metin hucresi yardimcisi (formul enjeksiyonu kanonu; KAT-B1.1 D4, KAT-B3)."""

from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

#: Excel'de hucreyi FORMUL yapan ilk karakterler (CSV/formul enjeksiyonu); sekme ve CR dahil.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def write_text_cell(sheet: Worksheet, row: int, column: int, value: str) -> None:
    """Metin hucresini HER ZAMAN string yazar (`data_type='s'`): `=HYPERLINK(...)` gibi bir
    ad/kod formul olarak calismaz."""
    cell = sheet.cell(row=row, column=column)
    cell.value = value
    if value.startswith(FORMULA_PREFIXES):
        cell.data_type = "s"

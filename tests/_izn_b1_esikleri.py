"""IZN-B1 eşiklerinin (B2 düzeltmesinden ÖNCEKİ değerler) test kopyası.

`izn_b1` migration'ı DONMUŞTUR ve B1 eşikleriyle türetti; B2 katalogu 6 sayfanın Onaylar eşiğini
değiştirdi (`core/sayfalar.ESIK_SPEC_B1_FARKLARI`). B1 migration testleri beklenen hücreleri bu
yardımcıyla B1 eşiklerinden üretir.
"""

from app.core.access import AccessLevel, Scope
from app.core.sayfalar import (
    ESIK_SPEC_B1_FARKLARI,
    MODULSUZ_VARSAYILAN,
    SAYFALAR,
    PageLevel,
    esik_coz,
    esik_karsilaniyor,
    esik_spec,
)
from app.modules.roles import seed_data

#: IZN-B5a (CEO onaylı): B1 migration'ının DONMUŞ eşik metinlerinden B5a'nın değiştirdikleri.
#: madde 3 → 36/37/39/40 görme eşiği draft → view; madde 5 → #2 Onay Kutusu onay eşiği f → "-"
#: (katalogda `onay_var` True → False). Bu hücrelerin DB'deki eski değerleri için lider tarafından
#: veri migration'ı gerekir (B5a ajanı migration yazmaz); B1/B2 migration testleri bu farkı bilir.
ESIK_SPEC_B5A_FARKLARI: dict[int, str] = {
    2: "n|-|f",
    36: "d|f|-",
    37: "d|f|-",
    39: "d|f|-",
    40: "d|f|-",
}
B5A_ONAY_VAR_ESKI: frozenset[str] = frozenset({"genel.onay_kutusu"})


def b1_onay_var(sayfa) -> bool:
    """B1 migration'ının DONMUŞ `has_approval` değeri (B5a madde 5 öncesi)."""
    return sayfa.onay_var or sayfa.key in B5A_ONAY_VAR_ESKI


def b1_spec(sayfa) -> str:
    """B1 migration eşik metni (DONMUŞ): B2 ve B5a düzeltmelerinin değiştirdiği sayfalarda eski."""
    return (
        ESIK_SPEC_B1_FARKLARI.get(sayfa.envanter_no)
        or ESIK_SPEC_B5A_FARKLARI.get(sayfa.envanter_no)
        or esik_spec(sayfa.envanter_no, sayfa.eski_modul)
    )


def b1_matrisi(cells) -> dict[str, tuple[PageLevel, bool]]:
    """`core/sayfalar.sayfa_matrisi`nin B1 eşikleriyle çalışan kopyası."""
    sonuc: dict[str, tuple[PageLevel, bool]] = {}
    for sayfa in SAYFALAR:
        if sayfa.eski_modul is None:
            sonuc[sayfa.key] = (MODULSUZ_VARSAYILAN[sayfa.key], False)
            continue
        gorme, yazma, onay = (
            esik_coz(parca, sayfa.eski_modul) for parca in b1_spec(sayfa).split("|")
        )
        if not esik_karsilaniyor(gorme, cells):
            sonuc[sayfa.key] = (PageLevel.none, False)
            continue
        level = (
            PageLevel.edit
            if yazma is not None and esik_karsilaniyor(yazma, cells)
            else PageLevel.view
        )
        sonuc[sayfa.key] = (level, onay is not None and esik_karsilaniyor(onay, cells))
    return sonuc


def app_cells(matrix, order, role_key) -> dict[str, tuple[str, str]]:
    index = order.index(role_key)
    return {m: (cells[index][0].value, cells[index][1].value) for m, cells in matrix.items()}


def b1_rows(matrix, role_key: str) -> dict[str, tuple[str, bool]]:
    order = seed_data.ROLE_ORDER if matrix is seed_data.MATRIX else seed_data.IZN_ROLE_ORDER
    cells = app_cells(matrix, order, role_key)
    typed = {m: (AccessLevel(a), Scope(sc)) for m, (a, sc) in cells.items()}
    return {k: (lv.value, ap) for k, (lv, ap) in b1_matrisi(typed).items()}

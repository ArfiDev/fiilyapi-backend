"""Panel kartlarının SAYFA kümeleri (IZN-B5d, CEO kararı) — kart başına açık sabit.

Kapı modül düzeyinde değil SAYFA düzeyindedir: kartı yalnız burada adı geçen sayfaların Görür
bayrağı açar (komşu sayfa açmaz). Kümeler bugünkü modül kümesinin ALT kümesidir (genişleme yok).
`module` / `pending_module` yanıt dizeleri değişmez (`risks.py` / `service.py`).

`HAKEDIS_ISVEREN` / `HAKEDIS_TASERON` iki hakediş router'ındaki sabitlerin KOPYASIDIR (servis
router import edemez); eşitliği `tests/modules/test_izn_b5d_panel_kartlari.py` çakar.
"""

from typing import Final

#: Proje kartları: Projeler listesi + proje özeti (ekipte proje.ozet).
PROJE_KARTLARI: Final[tuple[str, ...]] = ("genel.projeler", "proje.ozet")

#: İşveren hakediş ailesi (B5b).
HAKEDIS_ISVEREN: Final[tuple[str, ...]] = (
    "mali.hakedis_isveren",
    "proje.isveren_hakedis",
    "santiye.hakedisler",
)
#: Taşeron hakediş ailesi (B5b).
HAKEDIS_TASERON: Final[tuple[str, ...]] = ("mali.hakedis_taseron", "proje.taseron_hakedis")

#: Portföy (işveren hakediş hasılatı).
PORTFOY: Final[tuple[str, ...]] = HAKEDIS_ISVEREN
#: Risk › Stok kritik / eşiksiz (bugünkü `inventory:view` sayfaları, açık sabit).
RISK_STOK: Final[tuple[str, ...]] = ("stok.stok_depo", "santiye.stok", "bolum.malzeme")
#: Risk › Hakediş gecikmiş (TAŞERON hakedişi; şantiye hakediş sekmesi taşeron listesini de okur).
RISK_GECIKME: Final[tuple[str, ...]] = (*HAKEDIS_TASERON, "santiye.hakedisler")
#: Risk › Takvim (bölüm bitişi).
RISK_TAKVIM: Final[tuple[str, ...]] = ("santiye.bolumler", "bolum.detay")

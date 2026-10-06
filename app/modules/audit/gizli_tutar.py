"""Denetim gunlugu — TUTAR tasiyan olay turleri ve OKUMA aninda maskeleme (IZN-B5a, madde 19/21b).

## Sorun

Denetim metinleri tutari DUZ METIN tasir (`Hakedis silindi: ... 1.250,00 TL`). Yanit semalarindaki
para alanlari kategoriyle etiketli ve maskeleniyor; ama bu metin tek bir `str` oldugu icin ilgili
kategoriyi gizleyen bir rol, denetim gunlugunu GOREBILIYORSA tutari buradan okurdu.

## Politika A (CEO karari)

Metin URETIMI degismez (`messages/*` AYNEN). OKUMA aninda, tutar tasiyan bir olayin metni,
okuyucunun
ilgili kategorisi gizliyse `GIZLI_TUTAR_METNI` ile degistirilir; tur (`action`), aktor, zaman ve
varlik baglantisi KALIR. Excel disa aktarimi ayni kurala uyar.

## Neden metinden taninir

`audit_log` satirinda olay ANAHTARI saklanmaz (yalniz `action` + `detail`; ayrica `before/after` ya
da JSON yuk sutunu YOKTUR — `models.AuditLog` olculdu). Bu yuzden her tutarli mesajin SABIT oneki
(+ gerekirse ayirt edici isaret) tabloya yazilir. `silme.deleted_with_dependents` mevcut metni
BASA koyup sonuna ekler; bu yuzden `startswith` yeterlidir.

## Tek kaynak ve bekci

`TUTAR_OLAYLARI` tek kaynaktir. `tests/modules/test_izn_b5a_denetim_tutar.py` mesaj paketini AST ile
tarar: tutar bicimlendiren/tasiyan her mesaj fonksiyonu ya `TUTAR_OLAYLARI`nda ya da gerekcesiyle
`TUTAR_DEGIL`dedir; ikisinde de yoksa KIRMIZI. Onekler de mesaj fonksiyonlariyla karsilastirilir.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel

from app.core.field_mask import Hassas
from app.core.mask_route import maskele_baglamli

#: Gizli okuyucunun gordugu sabit ifade (mevcut Turkce uslup).
GIZLI_TUTAR_METNI = "Bu kayıt gizli tutar içerdiği için ayrıntısı gösterilmiyor"


@dataclass(frozen=True)
class TutarOlayi:
    """Tutar tasiyan bir mesaj fonksiyonu: sabit `onek` (+ `isaret`) ve tutarin kategorileri.

    `kategoriler`: HERHANGI BIRI gizliyse (ya da `tum_tutarlar`) metin gizlenir (en kisitlayici).
    """

    mesaj: str
    onek: str
    kategoriler: frozenset[Hassas]
    isaret: str | None = None

    def eslesir(self, detay: str) -> bool:
        return detay.startswith(self.onek) and (self.isaret is None or self.isaret in detay)


TUTAR_OLAYLARI: tuple[TutarOlayi, ...] = (
    # Katalog referans fiyati (`catalog.schemas.ref_price` = sozlesme_fiyat). Onek, tutarsiz
    # `work_item_updated` ile ORTAKTIR; ayirt eden `referans fiyat` isaretidir.
    TutarOlayi(
        "work_item_price_updated",
        "İş kalemi kataloğu kalemi güncellendi: ",
        frozenset({Hassas.sozlesme_fiyat}),
        isaret=" · referans fiyat ",
    ),
    # Isveren hakedisi tutari (sozlesme/hakedis bedeli = sozlesme_fiyat).
    TutarOlayi("progress_payment_deleted", "Hakediş silindi: ", frozenset({Hassas.sozlesme_fiyat})),
    # Taseron hakedisi: semada maliyet_kar; hakedis ailesi sozlesme_fiyat → IKISI de (fail-closed).
    TutarOlayi(
        "subcontractor_progress_payment_deleted",
        "Taşeron hakedişi silindi: ",
        frozenset({Hassas.maliyet_kar, Hassas.sozlesme_fiyat}),
    ),
    # Odenen net bordro toplami (bordro tutarlari = maas_kisisel).
    TutarOlayi("payroll_period_paid", "Bordro dönemi ödendi: ", frozenset({Hassas.maas_kisisel})),
    # Taksit tahsilati (satis tutari = satis_alici).
    TutarOlayi(
        "sale_installment_paid", "Taksit tahsilatı işlendi: ", frozenset({Hassas.satis_alici})
    ),
)

#: Tutar GIBI gorunen ama olmayan (ya da baska kayitla kapsanan) mesaj fonksiyonlari + GEREKCE.
#: Bekci, bunlarin hala var oldugunu ve `TUTAR_OLAYLARI`nda OLMADIGINI dogrular.
TUTAR_DEGIL: dict[str, str] = {
    "_price_text": "yardimci; `work_item_price_updated` tarafindan kapsanir",
    "offer_setting_pct_changed": "yuzde (`Hassas.yok`), tutar degil",
    "leave_balance_updated": "devreden GUN sayisi, tutar degil",
    "payroll_period_updated": "`object` parametresi son odeme TARIHIdir",
    "payroll_rate_updated": "kesinti ORANLARI mevzuat parametresi (payroll.schemas `Hassas.yok`)",
    "payroll_tax_brackets_updated": "vergi dilimi sinirlari mevzuat (payroll.schemas `Hassas.yok`)",
}


class _GizlilikProbu(BaseModel):
    """Etkin rolun gizli kategorilerini MEVCUT maske motoruyla okumak icin sentetik sema.

    Her alan `1`dir; maske onu `None`a ceker ⇔ kategori gizli YA DA `tum_tutarlar` gizli
    (sayisal alan kurali). Motoru (`maskele_baglamli`) yeniden yazmadan ayni karar kullanilir.
    """

    sozlesme_fiyat: Annotated[int | None, Hassas.sozlesme_fiyat] = 1
    maliyet_kar: Annotated[int | None, Hassas.maliyet_kar] = 1
    maas_kisisel: Annotated[int | None, Hassas.maas_kisisel] = 1
    banka_kasa: Annotated[int | None, Hassas.banka_kasa] = 1
    satis_alici: Annotated[int | None, Hassas.satis_alici] = 1


async def gizli_kategoriler() -> frozenset[Hassas]:
    """Bu istekte okuyucunun TUTARLARI gizli olan kategoriler (`MaskeRotasi` baglaminda)."""
    sonuc = await maskele_baglamli(_GizlilikProbu())
    return frozenset(k for k in Hassas if k is not Hassas.yok and getattr(sonuc, k.value) is None)


def gizli_olaylar(gizli: frozenset[Hassas]) -> tuple[TutarOlayi, ...]:
    """Verilen gizli kategori kumesinde METNI gizlenecek olaylar."""
    return tuple(o for o in TUTAR_OLAYLARI if o.kategoriler & gizli)


def detay_maskele(detay: str, olaylar: Iterable[TutarOlayi]) -> str:
    """Tutar tasiyan bir olayin metnini sabit ifadeyle degistirir; digerlerine dokunmaz."""
    return GIZLI_TUTAR_METNI if any(o.eslesir(detay) for o in olaylar) else detay

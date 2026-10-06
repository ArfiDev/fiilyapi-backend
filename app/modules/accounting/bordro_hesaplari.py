"""Yalniz BORDRO'nun besledigi hesaplar (IZN-B5a, madde 21a).

## Sorun

Bordro otomatik fisi (`payroll.posting.PAYROLL_POSTING_RULES`) donem toplamlarini hesaplara yazar:
`730` (brut + isveren primi), `335` (net), `360` (gelir vergisi + damga), `361` (SGK). Mizan/bilanco
bu hesaplarin satir tutarini `maas_kisisel` kategorisiyle AYRICA gizlemiyordu: `maas_kisisel`
gizli bir rol `335` satirindan aylik net bordroyu okurdu.

## Olculen kume

Bordronun yazdigi {730, 335, 360, 361} − DIGER tum otomatik fis kurallarinin yazdigi hesaplar
(`invoicing` 740/320/120/600/191/391/360/136 · `progress_payments` 120/600 ·
`subcontractor_progress_payments` 740/320 · `treasury` 102/100/103/101/320/120 ·
`treasury.instruments` 102/100/103/101/… · `equipment.rental` 740/320):

    {730, 335, 361}

`360` DISARIDA kalir: fatura stopaji (`ROLE_WITHHOLDING_PAYABLE`) da `360`a yazar; yani `360` satiri
karisiktir ve bordro tutari ondan CIKARILAMAZ ama gizlenmesi fatura vergisini de gizlerdi.
Bunun bedeli (acik kalan kismi sizinti) raporda belirtilmistir.
Kume kodda `tests/modules/accounting/test_izn_b5a_mizan_bordro.py` ile yedi `*_POSTING_RULES`
sabitinden TURETILEREK dogrulanir; yeni bir kural bu hesaplardan birine yazarsa test KIRMIZI olur.

## Kural

Bu hesaplarin (ve `335.01` gibi alt hesaplarinin) tutar alanlari `maas_kisisel` ile DE gizlenir
(`KATEGORI_COZ`). Hesabi gosteren her ARA/GENEL toplam da ayni kategoriyle gizlenir (cikarma yoluyla
turetmeyi onlemek icin; fail-closed). Elle girilmis fis satirlari da bu hesaplarda ayni kurala
tabidir (hesap bazli, fail-closed).
"""

from __future__ import annotations

from collections.abc import Iterable

from app.core.field_mask import Hassas

#: Yalniz bordro otomatik fisinin yazdigi ANA hesap kodlari (modul docstring'i: olcum).
BORDRO_BESLENEN_HESAPLAR: frozenset[str] = frozenset({"335", "361", "730"})


def bordro_hesabi_mi(code: str) -> bool:
    """`335` ve alt hesaplari (`335.01`) bordro beslenen kumededir."""
    return code.split(".", 1)[0] in BORDRO_BESLENEN_HESAPLAR


def bordro_hesabi_iceriyor_mu(codes: Iterable[str]) -> bool:
    return any(bordro_hesabi_mi(code) for code in codes)


def bordro_ek_kategori(codes: Iterable[str], etiketler: frozenset[Hassas]) -> frozenset[Hassas]:
    """`KATEGORI_COZ` govdesi: bordro hesabi iceren alan `maas_kisisel` ile DE gizlenir."""
    return etiketler | {Hassas.maas_kisisel} if bordro_hesabi_iceriyor_mu(codes) else etiketler

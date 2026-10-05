"""`kablolu_moduller()` — kapsam köprüsünün KODDAN türetilmiş kümesi (kalan iş #4).

Bu bekçi `app.modules.roles.scope_wiring.kablolu_moduller()`in ölçülmüş
gerçeği doğru yansıttığını çakar: köprü yalnız `boq · contracts · dashboard ·
projects · sales · sites`de kurulu; geri kalan 16 modülde (payroll dahil)
yoktur. `tests/core/test_kapsam_baglantisi.py` bu fonksiyonun İÇ MEKANİZMASINI
(router-başına mutasyon) ölçer; burası yalnız DIŞARIDAN görünen sözleşmeyi
(sonuç kümesi) ölçer.
"""

from app.modules.roles.scope_wiring import kablolu_moduller


def test_kablolu_moduller_IZN_B4_sonrasi_BOSTUR() -> None:
    """IZN-B4a: altı eski modül de yeni maskeye (`MaskeRotasi`) geçti; eski köprüyü taşıyan modül
    kalmadı. Küme B6'da (`scope_wiring` sökümü) testle birlikte silinir. Yeni bir modül eski
    köprüyü yeniden kurarsa burası kırmızı olur."""
    assert kablolu_moduller() == frozenset(), sorted(kablolu_moduller())


def test_kablolu_moduller_PARA_AGIRLIKLI_16_MODULU_DISLAR() -> None:
    """🔴 Kaydın etki cümlesinin doğrudan ölçümü: en hassas modüller DIŞARIDA.

    `payroll`/`treasury`/`accounting`/`invoicing` tam da "Sınırlı"nin en çok
    anlam taşıdığı yerler ve köprü onlara BAĞLI DEĞİL.
    """
    para_agirlikli = {"payroll", "treasury", "accounting", "invoicing", "personnel"}
    assert para_agirlikli.isdisjoint(kablolu_moduller()), (
        f"Beklenmedik biçimde kablolu: {para_agirlikli & kablolu_moduller()}"
    )


def test_kablolu_moduller_ONBELLEKLIDIR() -> None:
    """İki ardışık çağrı AYNI (ve `is` ile aynı) frozenset'i döner — `lru_cache`."""
    assert kablolu_moduller() is kablolu_moduller()

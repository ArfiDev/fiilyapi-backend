"""Hassas alan maskesi MOTORU (IZN-B4) — DB'siz, sentetik şemalarla.

Kuralların her biri İKİ YÖNDE ölçülür (gizlenen / gizlenmeyen): tek yönlü bir test "her şeyi
gizle" ya da "hiçbir şeyi gizleme" hâlinde de yeşil kalırdı.
"""

import uuid
from decimal import Decimal
from typing import Annotated, ClassVar

import pytest
from pydantic import BaseModel, computed_field

from app.core.field_mask import (
    BOS_KUMELER,
    HEPSI_GIZLI,
    Hassas,
    MaskeKumeleri,
    gizli_mi,
    maskele,
    pii_adi_mi,
    sayisal_mi,
    sema_plani,
    yazilan_hassas_alanlar,
)
from app.core.sayfalar import HiddenCategory

H = HiddenCategory


def _kume(*kategoriler: HiddenCategory) -> MaskeKumeleri:
    return MaskeKumeleri(varsayilan=frozenset(kategoriler))


class _Zarf(BaseModel):
    available: bool = True
    value: Decimal | None = None

    def kisitli(self) -> "_Zarf":
        return _Zarf(available=False)


class _Kalem(BaseModel):
    ad: str
    metraj: Annotated[Decimal | None, Hassas.yok]
    birim_fiyat: Annotated[Decimal | None, Hassas.sozlesme_fiyat]
    maliyet: Annotated[Decimal | None, Hassas.maliyet_kar]
    tc: Annotated[str | None, Hassas.maas_kisisel]
    zarf: Annotated[_Zarf, Hassas.sozlesme_fiyat]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tutar(self) -> Decimal | None:
        if self.birim_fiyat is None or self.metraj is None:
            return None
        return self.birim_fiyat * self.metraj


class _Grup(BaseModel):
    kalemler: list[_Kalem]
    ozet: dict[str, _Kalem] = {}


def _kalem() -> _Kalem:
    return _Kalem(
        ad="Kazı",
        metraj=Decimal("10"),
        birim_fiyat=Decimal("5"),
        maliyet=Decimal("3"),
        tc="12345678901",
        zarf=_Zarf(value=Decimal("50")),
    )


def test_bos_kume_HICBIR_seyi_gizlemez_ayni_nesne_doner() -> None:
    kalem = _kalem()
    assert maskele(kalem, BOS_KUMELER) is kalem


def test_kategori_yalniz_KENDI_alanlarini_gizler() -> None:
    sonuc = maskele(_kalem(), _kume(H.sozlesme_fiyat))

    assert sonuc.birim_fiyat is None
    assert sonuc.maliyet == Decimal("3"), "başka kategori gizlendi"
    assert sonuc.tc == "12345678901"
    assert sonuc.metraj == Decimal("10"), "`yok` etiketli alan gizlendi"
    assert sonuc.ad == "Kazı"


def test_ZARFLI_alan_None_degil_kisitli_ucuncu_hale_duser() -> None:
    sonuc = maskele(_kalem(), _kume(H.sozlesme_fiyat))

    assert sonuc.zarf.available is False
    assert sonuc.zarf.value is None


def test_TUREV_girdisi_maskeliyse_None_doner_degilse_degeri_korur() -> None:
    """Birim fiyat gizliyken tutar `birim fiyat / metraj` ile geri hesaplanamaz."""
    assert maskele(_kalem(), _kume(H.sozlesme_fiyat)).tutar is None
    assert maskele(_kalem(), _kume(H.maliyet_kar)).tutar == Decimal("50")  # POZİTİF KONTROL


def test_tum_tutarlar_YALNIZ_sayisal_etiketli_alanlari_gizler_metni_degil() -> None:
    sonuc = maskele(_kalem(), _kume(H.tum_tutarlar))

    assert sonuc.birim_fiyat is None
    assert sonuc.maliyet is None
    assert sonuc.zarf.available is False
    assert sonuc.tc == "12345678901", "`tum_tutarlar` kişisel METİN alanını gizlememeli"
    assert sonuc.metraj == Decimal("10"), "`yok` etiketli sayı gizlenmemeli"


def test_kisisel_kategori_TC_gizler_tutarlari_gizlemez() -> None:
    sonuc = maskele(_kalem(), _kume(H.maas_kisisel))

    assert sonuc.tc is None
    assert sonuc.birim_fiyat == Decimal("5")


def test_COK_kategorili_alan_herhangi_biri_gizliyse_gizlenir() -> None:
    class _Kar(BaseModel):
        kar: Annotated[Decimal | None, Hassas.maliyet_kar, Hassas.sozlesme_fiyat]

    ornek = _Kar(kar=Decimal("1"))
    assert maskele(ornek, _kume(H.maliyet_kar)).kar is None
    assert maskele(ornek, _kume(H.sozlesme_fiyat)).kar is None
    assert maskele(ornek, _kume(H.banka_kasa)).kar == Decimal("1")  # ilgisiz kategori


def test_IC_ICE_liste_ve_sozluk_maskelenir_orijinal_DEGISMEZ() -> None:
    grup = _Grup(kalemler=[_kalem(), _kalem()], ozet={"a": _kalem()})

    sonuc = maskele(grup, _kume(H.sozlesme_fiyat))

    assert all(k.birim_fiyat is None for k in sonuc.kalemler)
    assert sonuc.ozet["a"].birim_fiyat is None
    # 🔴 MUTASYON YOK: kaynak nesne aynen kalır (paylaşılan zarf/önbellek maskelenmez).
    assert all(k.birim_fiyat == Decimal("5") for k in grup.kalemler)
    assert grup.ozet["a"].birim_fiyat == Decimal("5")


def test_degisiklik_yoksa_alt_agac_AYNEN_paylasilir() -> None:
    grup = _Grup(kalemler=[_kalem()])
    sonuc = maskele(grup, _kume(H.banka_kasa))  # hiçbir alan banka_kasa değil
    assert sonuc is grup


def test_LISTE_modeli_maskelenir() -> None:
    sonuc = maskele([_kalem(), _kalem()], _kume(H.sozlesme_fiyat))
    assert [k.birim_fiyat for k in sonuc] == [None, None]


def test_hassas_etiket_tasimayan_sema_ATLANIR() -> None:
    class _Duz(BaseModel):
        ad: str
        adet: int

    assert sema_plani(_Duz).hassas_agac is False
    duz = _Duz(ad="a", adet=1)
    assert maskele(duz, HEPSI_GIZLI) is duz


class _SatirliProje(BaseModel):
    project_id: uuid.UUID
    fiyat: Annotated[Decimal | None, Hassas.sozlesme_fiyat]


def test_SATIR_kendi_projesindeki_rolle_maskelenir() -> None:
    """A'da PM (gizli yok), B'de Görüntüleyici (`tum_tutarlar`): aynı listede iki satır."""
    a, b, baska = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    kumeler = MaskeKumeleri(
        varsayilan=frozenset({H.tum_tutarlar}),  # ana rol: Görüntüleyici
        proje_basina={a: frozenset(), b: frozenset({H.tum_tutarlar})},
    )
    satirlar = [
        _SatirliProje(project_id=a, fiyat=Decimal("1")),
        _SatirliProje(project_id=b, fiyat=Decimal("2")),
        _SatirliProje(project_id=baska, fiyat=Decimal("3")),  # ekipte değil → varsayılan (ana rol)
    ]

    sonuc = maskele(satirlar, kumeler)

    assert [s.fiyat for s in sonuc] == [Decimal("1"), None, None]


def test_PROJE_ALANI_id_olan_sema_icin_bildirilir() -> None:
    class _ProjeSatiri(BaseModel):
        PROJE_ALANI: ClassVar[str] = "id"
        id: uuid.UUID
        butce: Annotated[Decimal | None, Hassas.maliyet_kar]

    a = uuid.uuid4()
    kumeler = MaskeKumeleri(varsayilan=frozenset({H.maliyet_kar}), proje_basina={a: frozenset()})

    assert maskele(_ProjeSatiri(id=a, butce=Decimal("9")), kumeler).butce == Decimal("9")
    assert maskele(_ProjeSatiri(id=uuid.uuid4(), butce=Decimal("9")), kumeler).butce is None


@pytest.mark.parametrize(
    ("tip", "beklenen"),
    [
        (Decimal | None, True),
        (int, True),
        (float, True),
        (list[Decimal], True),
        (_Zarf, True),  # `kisitli()` zarf protokolü
        (str, False),
        (bool, False),
    ],
)
def test_sayisal_mi(tip, beklenen) -> None:
    assert sayisal_mi(tip) is beklenen


def test_pii_adi_deseni_enum_degil_ad_uzerinden_calisir() -> None:
    for ad in (
        "national_id",
        "tc_no",
        "iban",
        "wage_amount",
        "phone",
        "birth_date",
        "address_line",
    ):
        assert pii_adi_mi(ad), ad
    for ad in ("etc_value", "description", "payment_id", "total_count"):
        assert not pii_adi_mi(ad), ad


def test_YAZILAN_hassas_alanlar_yalniz_DOLU_gonderilenleri_bulur() -> None:
    class _Girdi(BaseModel):
        ad: str | None = None
        fiyat: Annotated[Decimal | None, Hassas.sozlesme_fiyat] = None
        metraj: Annotated[Decimal | None, Hassas.yok] = None

    yok = _Girdi(ad="x", metraj=Decimal("1"))
    var = _Girdi(fiyat=Decimal("2"))
    acik_null = _Girdi.model_validate({"fiyat": None})

    assert yazilan_hassas_alanlar(yok) == []
    assert [a.ad for a in yazilan_hassas_alanlar(var)] == ["fiyat"]
    # Açık `null` da bir YAZMA girişimidir (alanı silmek): gövdede GÖNDERİLDİ sayılır.
    assert [a.ad for a in yazilan_hassas_alanlar(acik_null)] == ["fiyat"]
    # iç içe + liste
    assert [a.ad for a in yazilan_hassas_alanlar([var, yok])] == ["fiyat"]


def test_gizli_mi_hucresi_kategori_ve_tum_tutarlar() -> None:
    alan = sema_plani(_Kalem).hassas[0]  # birim_fiyat (sozlesme_fiyat, sayısal)
    assert alan.ad == "birim_fiyat"
    assert gizli_mi(alan, frozenset({H.sozlesme_fiyat}))
    assert gizli_mi(alan, frozenset({H.tum_tutarlar}))
    assert not gizli_mi(alan, frozenset({H.maas_kisisel}))
    assert not gizli_mi(alan, frozenset())


class _Dugum(BaseModel):
    """Özyinelemeli şema (ağaç): çocuklar da maskelenmeli."""

    fiyat: Annotated[Decimal | None, Hassas.sozlesme_fiyat]
    cocuklar: list["_Dugum"] = []


class _Kutu(BaseModel):
    """Hassas alanı YOK; yalnız döngü üzerinden hassas ağaca bağlı."""

    icerik: "_Cevre | None" = None


class _Cevre(BaseModel):
    fiyat: Annotated[Decimal | None, Hassas.sozlesme_fiyat]
    kutu: _Kutu | None = None


_Kutu.model_rebuild()


def test_OZYINELEMELI_sema_cocuklari_da_maskelenir() -> None:
    kok = _Dugum(fiyat=Decimal("1"), cocuklar=[_Dugum(fiyat=Decimal("2"), cocuklar=[])])

    sonuc = maskele(kok, _kume(H.sozlesme_fiyat))

    assert sonuc.fiyat is None
    assert sonuc.cocuklar[0].fiyat is None


def test_DONGU_uzerinden_hassas_agaca_bagli_ara_sema_ATLANMAZ() -> None:
    """A → B → A: B'nin kendi hassas alanı yok. Plan döngüde `hassas_agac=False` önbelleğe
    alınsaydı `_Kutu` tek başına maskelenirken içindeki fiyat SIZARDI."""
    kutu = _Kutu(icerik=_Cevre(fiyat=Decimal("5")))

    assert sema_plani(_Kutu).hassas_agac is True
    assert maskele(kutu, _kume(H.sozlesme_fiyat)).icerik.fiyat is None

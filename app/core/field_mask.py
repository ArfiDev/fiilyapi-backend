"""HASSAS ALAN MASKESİ — motor (IZN-B4, IZN-PLAN §3). `field_scope` (`limited`/`finance`) yerine.

## Kural (tek cümle)

Bir yanıt alanı, ETKİN rolün `hidden_fields` bayraklarından biri alanın `Hassas` etiketlerinden
birini kapsıyorsa `None`a (zarflı alanda `kisitli()`ye) çekilir; girdisi maskeli olan türev
kendiliğinden `None` döner. Kim ETKİN rol? Bkz. `core/mask_context.py` (proje bağlamlı uçta o
projedeki rol, `all_projects` ve şirket geneli uçta ana rol, çok proje satırında satırın projesi).

## Kategoriler

| etiket | anlamı |
|---|---|
| `sozlesme_fiyat` | sözleşme ve birim fiyatlar, sözleşme/hakediş/teklif bedeli |
| `maliyet_kar` | maliyet, bütçe, kâr, harcanan; taşeron/satınalma/stok/ekipman TUTARLARI |
| `maas_kisisel` | maaş, bordro tutarları + TC, IBAN, doğum, telefon, adres (personel) |
| `banka_kasa` | banka/kasa/çek bakiyeleri ve hareketleri, şirketin IBAN'ı |
| `satis_alici` | satış bedeli, ünite fiyatı + alıcı kimliği (ad, TCKN, telefon…) |
| `yok` | AÇIKÇA hassas DEĞİL (metraj, oran, yüzde, sayaç, kimlik/ad); etiketsiz kabul EDİLMEZ |

`tum_tutarlar` bir ETİKET DEĞİLDİR, rol bayrağıdır: açıksa yukarıdaki kategorilerden herhangi
biriyle etiketli ve SAYISAL tipli (Decimal / int / float / zarf) her alan gizlenir. Metin
alanları (alıcı adı, TC, IBAN) `tum_tutarlar` ile gizlenmez, yalnız kendi kategorileriyle:
"tüm TUTARLAR" bir kişisel bilgi anahtarı değildir.

Bir alan BİRDEN ÇOK kategori taşıyabilir (`Annotated[..., Hassas.maliyet_kar,
Hassas.sozlesme_fiyat]`): kategorilerinden HERHANGİ BİRİ gizliyse alan gizlenir (en kısıtlayıcı).

## 🔴 Fail-open KALKTI — ama bekçiyle

Eski modül "etiketsiz alan kimliktir" idi ve güvenliği bekçiye bırakıyordu. Aynı kalır, ama bekçi
sıkılaştı: HER `Decimal`/zarf alanı ve PII adlı HER `str` alan ya bir para/kişisel kategoriyle ya
da AÇIKÇA `Hassas.yok` ile etiketli olmalıdır (`tests/core/test_hassas_alan_bekcisi.py`).

## Maskeli alanın yanıt biçimi

* düz alan → `null`. Tip `X | None` OLMALIDIR (OpenAPI'de KIRICI: `X` → `X | None`).
* zarflı alan (`MetricPlaceholder.kisitli()`) → ÜÇÜNCÜ hâl "rolün izni yok" (`available=false`,
  `pending_module=null`): ekran "veri yok" ile "yetkin yok"u ayırt eder.

## Maske MUTASYON YAPMAZ

Değişen her nesne `model_copy(update=...)` ile YENİ üretilir; değişmeyen alt ağaç AYNEN paylaşılır
(hiçbir yerde yerinde yazma yoktur). `computed_field`lar kopyada yeniden hesaplanır.

## Yazma yolu (kural burada, uygulaması `core/mask_route.py`)

Gizli kategoriye ait bir alanı PUT/PATCH GÖVDESİNDE gönderen aktör 403 alır (POST/oluşturma
SERBEST, CEO kararı: gizlilik okuma içindir). Eski kural ("kapsamı
kısıtlı rol hiçbir yazma ucuna giremez") yeni modelde fazla kaba: maaşı gizleyen bir Proje
Müdürü yine de BOQ yazabilmelidir. Maskeli alan forma `null` olarak düştüğü için istemci onu
göndermez; gönderirse bilmediği bir değeri ezmeye çalışıyordur.
"""

from __future__ import annotations

import enum
import re
import typing
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from app.core.sayfalar import HiddenCategory

__all__ = [
    "BOS_KUMELER",
    "TUM_KATEGORILER",
    "AlanPlani",
    "Hassas",
    "MaskeKumeleri",
    "SemaPlani",
    "etiketler",
    "gizli_mi",
    "maskele",
    "pii_adi_mi",
    "sayisal_mi",
    "semalar_icinde",
    "sema_plani",
    "yazilan_hassas_alanlar",
]


class Hassas(str, enum.Enum):
    """Alanın hassasiyet etiketi. `Annotated[Decimal | None, Hassas.maliyet_kar]`."""

    sozlesme_fiyat = "sozlesme_fiyat"
    maliyet_kar = "maliyet_kar"
    maas_kisisel = "maas_kisisel"
    banka_kasa = "banka_kasa"
    satis_alici = "satis_alici"
    yok = "yok"


#: Rol bayrağı olarak karşılığı olan (yani `yok` OLMAYAN) kategoriler.
TUM_KATEGORILER: frozenset[Hassas] = frozenset(h for h in Hassas if h is not Hassas.yok)


def _bayrak(kategori: Hassas) -> HiddenCategory:
    return HiddenCategory(kategori.value)


@dataclass(frozen=True)
class MaskeKumeleri:
    """Bir isteğin gizli kategori kümeleri.

    * `varsayilan`: isteğin kendi bağlamı — proje bağlamlı uçta o projedeki rolün bayrakları;
      proje ÇÖZÜLEMEYEN uçta (şirket geneli / çok proje / belirsiz) ana rol ile kullanıcının TÜM
      ekip rollerinin BİRLEŞİMİ (IZN-PLAN §3, fail-closed). Yazma kapısı bunu kullanır.
    * `proje_basina`: kullanıcının EKİP olduğu her projenin rolünün bayrakları. Çok proje
      LİSTELERİNDE `project_id` taşıyan satır kendi projesindeki rolle maskelenir (satır başına
      maske: birleşimin aşırı maskelemesini önler). "Tüm projeler" kullanıcıda boştur.
    * `ana`: ana rolün bayrakları — ekibinde OLMADIĞI projenin satırı (erişimi ana rolledir)
      için. `None` ise `varsayilan` kullanılır.
    """

    varsayilan: frozenset[HiddenCategory] = frozenset()
    proje_basina: Mapping[uuid.UUID, frozenset[HiddenCategory]] = field(default_factory=dict)
    ana: frozenset[HiddenCategory] | None = None

    @property
    def bos_mu(self) -> bool:
        return not self.varsayilan and not any(self.proje_basina.values())


BOS_KUMELER = MaskeKumeleri()

#: Çözülemeyen kimlik için FAIL-CLOSED küme: her kategori + `tum_tutarlar` gizli.
HEPSI_GIZLI = MaskeKumeleri(varsayilan=frozenset(HiddenCategory))


# --- Alan planı (sınıf başına BİR kez) ----------------------------------------------------------

_PII_ADI = re.compile(
    r"(?:^|_)(?:tc|tckn|iban|wage|salary|phone|mobile|birth|sgk|address|email|e_mail)(?:_|$)"
    r"|birthdate|date_of_birth|national_id|tax_number|tax_no|tax_id|vergi_no|identity_number"
    r"|(?:^|_)(?:buyer|customer|party)_name(?:_|$)",
    re.IGNORECASE,
)

#: Tipi SAYISAL sayan tipler: `Decimal` / `int` / `float` ve para zarfları (`bool` hariç).
#: Zarf = adı listede olan ya da `kisitli()` protokolünü taşıyan model.
_SAYISAL_TIPLER = (Decimal, int, float)
_ZARF_ADLARI = frozenset({"MetricPlaceholder", "CountPlaceholder"})


def pii_adi_mi(ad: str) -> bool:
    """Alan adı kişisel veri desenine uyuyor mu (bekçinin ad sezgisi; `wage_type` enum'u ayrıca
    tipten elenir)."""
    return _PII_ADI.search(ad) is not None


def _tip_dugumleri(annotation: Any) -> typing.Iterator[Any]:
    """Bir tip ifadesinin tüm yaprak tipleri (`Annotated`/`Union`/`list`/`dict` açılır)."""
    if annotation is Any:
        yield Any
        return
    args = typing.get_args(annotation)
    if not args:
        yield annotation
        return
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        yield from _tip_dugumleri(args[0])
        return
    yield origin
    for alt in args:
        yield from _tip_dugumleri(alt)


def sayisal_mi(annotation: Any) -> bool:
    """Alan tutar/sayı taşıyor mu? (`tum_tutarlar` yalnız sayısal alanları kapsar.)"""
    for dugum in _tip_dugumleri(annotation):
        if dugum is bool:
            continue
        if isinstance(dugum, type) and (
            issubclass(dugum, _SAYISAL_TIPLER)
            or dugum.__name__ in _ZARF_ADLARI
            or callable(getattr(dugum, "kisitli", None))  # zarf protokolü
        ):
            return True
    return False


def semalar_icinde(annotation: Any) -> typing.Iterator[type[BaseModel]]:
    for dugum in _tip_dugumleri(annotation):
        if isinstance(dugum, type) and issubclass(dugum, BaseModel):
            yield dugum


def _dinamik_mi(annotation: Any) -> bool:
    """`Any`/`object` içeren alan: içine çalışma anında bakılır (şema statik bilinmiyor)."""
    return any(d is Any or d is object for d in _tip_dugumleri(annotation))


def etiketler(alan: Any) -> frozenset[Hassas]:
    """`FieldInfo.metadata`daki `Hassas` etiketleri (etiketsiz → boş küme)."""
    return frozenset(meta for meta in alan.metadata if isinstance(meta, Hassas))


@dataclass(frozen=True)
class AlanPlani:
    ad: str
    kategoriler: frozenset[Hassas]  # `yok` hariç; boş değil
    sayisal: bool


@dataclass(frozen=True)
class SemaPlani:
    hassas: tuple[AlanPlani, ...]  # kendi hassas alanları
    ic_ice: tuple[str, ...]  # içine inilecek alanlar (hassas ağaç taşıyan alt şema / dinamik)
    hassas_agac: bool  # kendisi ya da alt ağacı hassas alan taşıyor


_PLANLAR: dict[type[BaseModel], SemaPlani] = {}
_HESAPLANIYOR: set[type[BaseModel]] = set()


def sema_plani(sema: type[BaseModel]) -> SemaPlani:
    """Şemanın maske planı (önbellekli; özyinelemeli şemalarda döngü-güvenli)."""
    plan = _PLANLAR.get(sema)
    if plan is not None:
        return plan
    if sema in _HESAPLANIYOR:
        # Özyinelemeli şema (A → B → A): döngüdeki dalın sonucu henüz bilinmiyor. MUHAFAZAKÂR
        # `True`: ara şema (B) "hassas ağaç yok" diye YANLIŞ önbelleğe alınmasın; çalışma
        # anında inilen örnek grafı sonludur, fazladan iniş yalnız birkaç karşılaştırmadır.
        return SemaPlani((), (), True)
    _HESAPLANIYOR.add(sema)
    try:
        hassas: list[AlanPlani] = []
        ic_ice: list[str] = []
        for ad, alan in sema.model_fields.items():
            kategoriler = etiketler(alan) - {Hassas.yok}
            if kategoriler:
                hassas.append(AlanPlani(ad, frozenset(kategoriler), sayisal_mi(alan.annotation)))
            # Etiketli ama GİZLENMEYEN bir alanın (ör. `tum_tutarlar` sayısal olmayan kapsayıcıyı
            # gizlemez) İÇİNDEKİ etiketli alanlar yine maskelenir: kapsayıcı etiketi alt ağacı
            # korumaz, yalnız kapsayıcının KENDİSİNİ gizler.
            if _dinamik_mi(alan.annotation) or any(
                sema_plani(alt).hassas_agac for alt in semalar_icinde(alan.annotation)
            ):
                ic_ice.append(ad)
        plan = SemaPlani(tuple(hassas), tuple(ic_ice), bool(hassas or ic_ice))
    finally:
        _HESAPLANIYOR.discard(sema)
    _PLANLAR[sema] = plan
    return plan


def gizli_mi(
    alan: AlanPlani,
    kume: frozenset[HiddenCategory],
    kategoriler: frozenset[Hassas] | None = None,
) -> bool:
    """Bu alan, gizli kategori kümesinde gizlenir mi? `kategoriler`: satıra göre ETKİN küme
    (`KATEGORI_COZ`); verilmezse alanın statik etiketleri."""
    if any(_bayrak(k) in kume for k in (alan.kategoriler if kategoriler is None else kategoriler)):
        return True
    return alan.sayisal and HiddenCategory.tum_tutarlar in kume


# --- Maske ---------------------------------------------------------------------------------------

#: Şemanın PROJESİNİ veren alan adı (varsayılan `project_id`). `id`si proje olan şemalar
#: (`ProjectResponse`) `PROJE_ALANI = "id"` bildirir.
PROJE_ALANI_OZNITELIGI = "PROJE_ALANI"

#: Alanın kategorisi SATIRA göre değişiyorsa (onay kutusunda evrak tipi, yevmiyede kaynak tipi)
#: şema `@staticmethod KATEGORI_COZ(model, alan_adi, etiketler) -> frozenset[Hassas]` bildirir.
#: Statik `Hassas` etiketleri bekçi ve OpenAPI için ÜST KÜME olarak durur; çözücü satırın ETKİN
#: kategorilerini döner (daraltabilir ya da ek kategori ekleyebilir). Çözücü yoksa etiketler.
KATEGORI_COZ_OZNITELIGI = "KATEGORI_COZ"


def _satir_projesi(model: BaseModel) -> uuid.UUID | None:
    ad = getattr(type(model), PROJE_ALANI_OZNITELIGI, "project_id")
    deger = getattr(model, ad, None)
    return deger if isinstance(deger, uuid.UUID) else None


def _gizle(mevcut: Any) -> Any:
    """Gizlenen alanın YERİNE: zarflı alanda `kisitli()` (üçüncü hâl), aksi hâlde `None`."""
    kisitli = getattr(mevcut, "kisitli", None)
    return kisitli() if callable(kisitli) else None


def _liste(oge_donusumu: Callable[[Any], Any], oge: list[Any]) -> list[Any]:
    yeni = [oge_donusumu(o) for o in oge]
    return oge if all(a is b for a, b in zip(yeni, oge, strict=True)) else yeni


def _deger(deger: Any, kume: frozenset[HiddenCategory], kumeler: MaskeKumeleri) -> Any:
    if isinstance(deger, BaseModel):
        return _model(deger, kume, kumeler)
    if isinstance(deger, list):
        return _liste(lambda o: _deger(o, kume, kumeler), deger)
    if isinstance(deger, tuple):
        yeni = tuple(_deger(o, kume, kumeler) for o in deger)
        return deger if all(a is b for a, b in zip(yeni, deger, strict=True)) else yeni
    if isinstance(deger, dict):
        yeni_sozluk = {k: _deger(v, kume, kumeler) for k, v in deger.items()}
        degisti = any(yeni_sozluk[k] is not v for k, v in deger.items())
        return yeni_sozluk if degisti else deger
    return deger


def _model[TModel: BaseModel](
    model: TModel, kume: frozenset[HiddenCategory], kumeler: MaskeKumeleri
) -> TModel:
    plan = sema_plani(type(model))
    if not plan.hassas_agac:
        return model
    proje = _satir_projesi(model)
    if proje is not None:
        # Satır KENDİ projesindeki rolle maskelenir; ekipte değilse ana rolle (erişimi odur).
        kume = kumeler.proje_basina.get(
            proje, kumeler.varsayilan if kumeler.ana is None else kumeler.ana
        )
    guncel: dict[str, Any] = {}
    coz = getattr(type(model), KATEGORI_COZ_OZNITELIGI, None)
    for alan in plan.hassas:
        etkin = (
            alan.kategoriler if coz is None else frozenset(coz(model, alan.ad, alan.kategoriler))
        )
        if gizli_mi(alan, kume, etkin):
            guncel[alan.ad] = _gizle(getattr(model, alan.ad))
    for ad in plan.ic_ice:
        if ad in guncel:  # kapsayıcı zaten gizlendi
            continue
        mevcut = getattr(model, ad)
        yeni = _deger(mevcut, kume, kumeler)
        if yeni is not mevcut:
            guncel[ad] = yeni
    # `model_copy` YENİ nesne üretir; türev alanlar (`computed_field`) okunduklarında YENİ
    # girdilerden yeniden hesaplanır — maske türevlere kendiliğinden yayılır.
    return model.model_copy(update=guncel) if guncel else model


def maskele(deger: Any, kumeler: MaskeKumeleri) -> Any:
    """Modeli / model listesini / model sözlüğünü maskeler; YENİ ağaç döner (mutasyon yok)."""
    if kumeler.bos_mu:
        return deger
    return _deger(deger, kumeler.varsayilan, kumeler)


# --- Yazma yolu ---------------------------------------------------------------------------------


def yazilan_hassas_alanlar(deger: Any) -> list[AlanPlani]:
    """Gövdede DOLU gönderilen (`model_fields_set`) hassas alanlar (iç içe modeller dahil).

    Maske kümesine BAKMAZ (DB yok): çağıran, dönen alanları etkin kümeyle `gizli_mi`den geçirir.
    """
    bulunan: list[AlanPlani] = []

    def gez(d: Any) -> None:
        if isinstance(d, BaseModel):
            plan = sema_plani(type(d))
            if not plan.hassas_agac:
                return
            bulunan.extend(a for a in plan.hassas if a.ad in d.model_fields_set)
            for ad in plan.ic_ice:
                gez(getattr(d, ad, None))
        elif isinstance(d, list | tuple):
            for oge in d:
                gez(oge)
        elif isinstance(d, dict):
            for oge in d.values():
                gez(oge)

    gez(deger)
    return bulunan

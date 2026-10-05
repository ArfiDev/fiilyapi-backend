"""TÜREV alanlar maskeye DUYARLI mıdır — IZN-B4 ZORUNLU modül kümesinin TAMAMI için bekçi.

## Neyi kapatıyor

`core/field_mask.py` şunu kanon ilan eder: *"türev, girdisi `None` olduğunda
`None` DÖNMELİDİR"*. Maske `computed_field`'ı doğrudan yazamaz (property'dir);
yalnız GİRDİLERİNİ `None`a çeker ve türevin kendiliğinden düşmesini bekler.
Türev bu sözleşmeyi tutmazsa iki ayrı kusur doğar ve ikisi de CANLIDA görülür:

1. **500** — türev `None` ile çarparsa (`None * Decimal` → `TypeError`)
   serileştirme anında patlar; uç, o kapsamdaki rol için DETERMİNİSTİK olarak
   çöker. (`contracts.line_total`, `finance` kapsamı, 2026-09-19 denetimi.)
2. **SESSİZCE YANLIŞ SAYI** — türev `None` yerine `0` dönerse ekran "—" değil
   "0,00 TL" basar. Bu, gizlemekten DAHA KÖTÜDÜR: kullanıcı yanlış bir sayıya
   bakar ve onun gizlendiğini bilmez (`frontend/src/lib/format.ts` yalnız
   `null` görünce `EMPTY_CELL` basar).

## 🔴 Neden SAYI değil BEKÇİ

Bu dosya "şu üç türev şöyle davranır" demez. Taranan modülleri bekçinin
`ZORUNLU_MODULLER` sabitinden (`tests/core/test_hassas_alan_bekcisi.py`; B4b/c/d
modül ekledikçe tarama kendiliğinden genişler), şemaları PAKETTEN, türevleri
`model_computed_fields`ten okur. Yarın bir
modüle yeni bir `computed_field` eklendiğinde kimsenin bu dosyayı açması
gerekmez — yeni türev kendiliğinden taranır. Elle tutulan bir liste, listeyi
güncellemeyi unutan ilk kişide çürürdü.

## Ölçülen kural: "MASKE DEĞERİ DEĞİŞTİRİYORSA TÜREV `None` OLMALIDIR"

Türevin hangi alanları okuduğunu KAYNAK METNİNDEN çıkarmıyoruz — metin okumak
kırılgandır (bir yardımcı fonksiyona taşınan formül kaçardı). Bunun yerine
FARKI ölçüyoruz: aynı nesnenin maskeli ve maskesiz hâlinde türev değeri
DEĞİŞİYORSA, türev maskelenmiş bir girdiden beslenmiş demektir; o hâlde `None`
dönmek ZORUNDADIR. Değişmiyorsa türev o kovadan beslenmiyordur ve gerçek
değerini korumakta serbesttir.

Bu formülasyon iki hâli de tek kuralla yakalar: patlama (değer okunamaz) ve
sahte sıfır (değer değişti ama `None` değil).

## 🔴 POZİTİF KONTROL

Üç ayrı yerde var, çünkü bu bekçinin en olası ölüm biçimi "hiçbir şey taramadan
yeşil kalmak"tır:

* `all` kapsamında her türev GERÇEK değerini döndürmelidir (maske "her şeyi
  gizle" hâline çökerse burası kırmızı olur),
* tarama ZORUNLU kümenin tüm modüllerini ve bir eşik sayıda şemayı görmelidir (ithal kırılırsa
  kırmızı),
* gizli kategori kümelerinde EN AZ BİR türev maskeden ETKİLENMELİDİR (etiketler
  kaybolur ya da fabrika boş nesne üretirse kırmızı).
"""

import datetime
import decimal
import enum
import importlib
import inspect
import itertools
import pkgutil
import typing
import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from app.core.field_mask import MaskeKumeleri, maskele
from app.core.sayfalar import HiddenCategory
from tests.core.test_hassas_alan_bekcisi import ZORUNLU_MODULLER, ZORUNLU_SEMALAR

#: Gizli kategori SENARYOLARI (eski `Scope.limited`/`finance` yerine): her kategori tek başına,
#: `tum_tutarlar` ve hepsi. `acik` = hiçbir şey gizli değil (pozitif kontrol).
SENARYOLAR: dict[str, MaskeKumeleri] = {
    "sozlesme_fiyat": MaskeKumeleri(varsayilan=frozenset({HiddenCategory.sozlesme_fiyat})),
    "maliyet_kar": MaskeKumeleri(varsayilan=frozenset({HiddenCategory.maliyet_kar})),
    "satis_alici": MaskeKumeleri(varsayilan=frozenset({HiddenCategory.satis_alici})),
    "tum_tutarlar": MaskeKumeleri(varsayilan=frozenset({HiddenCategory.tum_tutarlar})),
    "hepsi": MaskeKumeleri(varsayilan=frozenset(HiddenCategory)),
}
ACIK = MaskeKumeleri()

#: İstek (request) gövdeleri taranmaz: maske YALNIZ yanıt yolunda çalışır
#: (`core/mask_route.py`). Bir istek şemasındaki türevi maskeye göre yargılamak
#: yanlış kırmızı üretirdi — o nesne hiçbir zaman `maskele`den geçmez.
_ISTEK_EKI = ("Create", "Update", "Input", "Save", "Replace")

#: Fabrikanın ürettiği `Decimal`ler BİRBİRİNDEN FARKLI olmalıdır.
#:
#: 🔴 Hepsine aynı sayıyı verseydik `a - b` biçimli bir türev maskesiz de
#: maskeli de `0` döner, "değer değişmedi" görünür ve bekçi o türevde KÖR
#: kalırdı. Ardışık tam sayılar çarpım/fark çakışmalarını pratikte imkânsız
#: kılar.
_SAYAC = itertools.count(2)

#: Özyinelemeli şemalarda (A → B → A) sonsuz derinliğe karşı tavan. Tavana
#: çarpan dal `None`a düşer; o dalın türevi varsa zaten kendi kök şeması olarak
#: ayrıca taranır.
_DERINLIK_TAVANI = 8


def _taranan_moduller() -> list[str]:
    """Bekçinin ZORUNLU kümesi (+ şema-zorunlu modüller): etiketli modüller.

    Liste burada elle yazılmaz: modülünü ZORUNLU kümeye taşıyan dilim türevlerini de otomatik
    bu bekçiye sokar.
    """
    return sorted(ZORUNLU_MODULLER | set(ZORUNLU_SEMALAR))


def _paket_semalari(modul_key: str) -> list[tuple[str, type[BaseModel]]]:
    """Modül paketindeki HER dosyada tanımlı yanıt şemaları.

    🔴 Yalnız `schemas.py` taransaydı `projects/land_share_schemas.py` gibi
    ikinci şema dosyaları ve `cards.py` benzeri üretici modüllerde tanımlanmış
    zarflar kaçardı. `pkgutil` ile tüm paketi gezmek, "yeni dosya açınca bekçi
    kör kalır" tuzağını kökten kapatır.
    """
    pkg = importlib.import_module(f"app.modules.{modul_key}")
    bulunan: list[tuple[str, type[BaseModel]]] = []
    for info in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"app.modules.{modul_key}.{info.name}")
        for ad, obj in vars(mod).items():
            if not (inspect.isclass(obj) and issubclass(obj, BaseModel)):
                continue
            if obj is BaseModel or obj.__module__ != mod.__name__:
                continue
            if ad.startswith("_") or any(ek in ad for ek in _ISTEK_EKI):
                continue
            bulunan.append((f"{modul_key}.{ad}", obj))
    return bulunan


def _turevli_semalar() -> list[tuple[str, type[BaseModel]]]:
    """Taranan modüllerde `computed_field` TAŞIYAN şemalar."""
    return [
        (ad, sema)
        for modul_key in _taranan_moduller()
        for ad, sema in _paket_semalari(modul_key)
        if sema.model_computed_fields
    ]


def _ornek_deger(tip: Any, derinlik: int) -> Any:
    """Tipe uygun, DOLU bir örnek üretir — hiçbir alan `None` bırakılmaz.

    🔴 `None` bırakılan bir alan maskenin etkisini GİZLERDİ: türev zaten `None`
    dönerdi ve "maske bunu düşürdü mü" sorusu ölçülemez hâle gelirdi.
    """
    if derinlik > _DERINLIK_TAVANI:
        return None
    if tip is None or tip is type(None) or tip is Any:
        return None

    koken = typing.get_origin(tip)
    argumanlar = typing.get_args(tip)

    if koken is typing.Literal:
        return argumanlar[0]
    if koken in (list, set, frozenset, tuple):
        # 🔴 EN AZ BİR öge: boş liste, toplam alan bir türevi maskeye DUYARSIZ
        # gösterirdi (boş toplam her kapsamda aynı çıkar) ve bekçi körleşirdi.
        icerik = [_ornek_deger(argumanlar[0], derinlik + 1)] if argumanlar else []
        return koken(icerik) if koken is not tuple else tuple(icerik)
    if koken is dict:
        return {}
    if argumanlar:  # `X | None`, `X | Y` — ilk NULL OLMAYAN dalı kur.
        for arg in argumanlar:
            if arg is not type(None):
                return _ornek_deger(arg, derinlik + 1)
        return None

    if isinstance(tip, type):
        if issubclass(tip, BaseModel):
            return _ornek_model(tip, derinlik + 1)
        if issubclass(tip, enum.Enum):
            return next(iter(tip))
        if issubclass(tip, bool):
            return True
        if issubclass(tip, decimal.Decimal):
            return Decimal(next(_SAYAC))
        if issubclass(tip, int):
            return next(_SAYAC)
        if issubclass(tip, float):
            return float(next(_SAYAC))
        if issubclass(tip, uuid.UUID):
            return uuid.uuid4()
        if issubclass(tip, datetime.datetime):
            return datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)
        if issubclass(tip, datetime.date):
            return datetime.date(2026, 1, 1)
        if issubclass(tip, str):
            return "X"
    return None


def _ornek_model[TModel: BaseModel](sema: type[TModel], derinlik: int = 0) -> TModel:
    """Şemanın DOĞRULAMASIZ örneği (`model_construct`).

    🔴 `model_validate` kullanılsaydı fabrikanın her alan kısıtını
    (`min_length`, `ge`, `model_validator`) taklit etmesi gerekirdi ve bekçi,
    ölçmek istediği şeyle ilgisiz doğrulama hatalarıyla kırmızıya düşerdi.
    Maskenin kendisi de zaten doğrulamasız çalışır (`model_copy(update=...)`),
    yani ölçülen yol ile kurulan yol AYNI mekanizmadır.
    """
    degerler = {
        ad: _ornek_deger(alan.annotation, derinlik) for ad, alan in sema.model_fields.items()
    }
    return sema.model_construct(**degerler)


def _turev_oku(model: BaseModel, ad: str) -> Any:
    """Türevi okur; patlarsa istisnayı DEĞER olarak döndürür (karşılaştırılabilsin)."""
    try:
        return getattr(model, ad)
    except Exception as hata:  # noqa: BLE001 — istisnanın TÜRÜ değil OLUŞU ölçülüyor
        return hata


@pytest.mark.parametrize("senaryo", list(SENARYOLAR))
def test_TUREVLER_hicbir_senaryoda_SERILESTIRMEYI_patlatmaz(senaryo: str) -> None:
    """Maskeli girdiyle türev okunurken patlayan şema, o gizli kümedeki rol için
    uca 500 döndürür (maske serileştirmeden ÖNCE uygulanır, `mask_route`)."""
    patlayan: dict[str, str] = {}
    for ad, sema in _turevli_semalar():
        model = maskele(_ornek_model(sema), SENARYOLAR[senaryo])
        try:
            model.model_dump(mode="json", warnings=False)
        except Exception as hata:  # noqa: BLE001
            patlayan[ad] = f"{type(hata).__name__}: {hata}"
    assert not patlayan, (
        f"`{senaryo}` gizli kümesinde türev alan serileştirmeyi PATLATIYOR → uç 500 verir. "
        "Türev, girdisi maskelenmişse `None` dönmelidir (`core/field_mask.py` kanonu; "
        f"doğru emsal: `boq/schemas.py::amount`): {patlayan}"
    )


def test_MASKE_deger_degistiriyorsa_TUREV_None_DONER() -> None:
    """Sahte sıfır bekçisi: maskelenmiş girdiden beslenen türev `0` DÖNEMEZ.

    Değer değiştiği hâlde `None` olmayan bir türev, gizlenmiş bir bedeli ekrana
    GERÇEK BİR SAYI olarak basar — bu, gizlemekten daha kötüdür.
    """
    kusurlu: dict[str, str] = {}
    for ad, sema in _turevli_semalar():
        ham = _ornek_model(sema)
        for senaryo, kumeler in SENARYOLAR.items():
            maskeli = maskele(ham, kumeler)
            for turev in sema.model_computed_fields:
                ham_deger = _turev_oku(ham, turev)
                maskeli_deger = _turev_oku(maskeli, turev)
                if isinstance(maskeli_deger, Exception):
                    kusurlu[f"{ad}.{turev}/{senaryo}"] = (
                        f"okunamadı: {type(maskeli_deger).__name__}"
                    )
                elif maskeli_deger != ham_deger and maskeli_deger is not None:
                    kusurlu[f"{ad}.{turev}/{senaryo}"] = (
                        f"maskesiz={ham_deger!r} → maskeli={maskeli_deger!r} (None olmalıydı)"
                    )
    assert not kusurlu, (
        "Maskelenmiş girdiden beslenen türev `None` yerine BAŞKA bir değer dönüyor; "
        "ekran gizlenmiş veriyi gerçek sayı sanır: " + repr(kusurlu)
    )


def test_ACIK_kumede_turevler_GERCEK_degeri_doner() -> None:
    """🔴 POZİTİF KONTROL — maske "her şeyi gizle"ye çökerse ya da türevler
    koşulsuz `None` dönmeye başlarsa (kusuru "kapatmanın" en kolay yanlış yolu)
    burası kırmızı olur."""
    bos: list[str] = []
    for ad, sema in _turevli_semalar():
        ham = _ornek_model(sema)
        acik = maskele(ham, ACIK)
        for turev in sema.model_computed_fields:
            deger = _turev_oku(acik, turev)
            if isinstance(deger, Exception) or deger is None:
                bos.append(f"{ad}.{turev} → {deger!r}")
    assert not bos, (
        f"açık kümede (hiçbir şey gizlenmezken) türev GERÇEK değer dönmeli; boş dönenler: {bos}"
    )


def test_BEKCI_gercekten_TARIYOR() -> None:
    """🔴 POZİTİF KONTROL — bu dosyanın en olası ölüm biçimi "hiçbir şey
    taramadan yeşil kalmak"tır (ithal kırılır, matris `all`a çöker, adlandırma
    süzgeci her şeyi eler). Tarama boşalırsa yukarıdaki üç bekçi de sessizce
    yeşile döner; burası o hâli çakar."""
    moduller = _taranan_moduller()
    assert len(moduller) >= 10, f"Taranan modül sayısı beklenenden az: {moduller}"

    # 🔴 Modül BAŞINA en az bir şema: tek bir modülün ithali kırıldığında ya da
    # adlandırma süzgeci o modülün tüm sınıflarını yediğinde global bir sayı
    # bunu gizlerdi (kalan beş modül eşiği tek başına doldururdu).
    bos_moduller = [modul for modul in moduller if not _paket_semalari(modul)]
    assert not bos_moduller, f"Bu modüllerde HİÇ şema görülmedi: {bos_moduller}"

    # Tabandaki sayı bir ENVANTER değil ÇÖKÜŞ DEDEKTÖRÜDÜR: ölçüldüğünde 97
    # şema vardı ve eşik kasten çok altına konmuştur — şema eklemek/silmek bu
    # bekçiyi kırmamalı, ama tarama tümden çökerse kırmalıdır.
    semalar = [ad for modul in moduller for ad, _ in _paket_semalari(modul)]
    assert len(semalar) >= 50, f"Şema taraması çöktü, yalnız {len(semalar)} şema görüldü"

    turevli = _turevli_semalar()
    assert turevli, "Taranan modüllerde HİÇ `computed_field` bulunamadı — tarama kırık."


@pytest.mark.parametrize("senaryo", ["sozlesme_fiyat", "tum_tutarlar", "hepsi"])
def test_HER_gizli_kumede_en_az_bir_turev_MASKEDEN_ETKILENIR(senaryo: str) -> None:
    """🔴 POZİTİF KONTROL — üstteki kural ("değişiyorsa `None` olmalı") hiçbir
    türev maskeden etkilenmezse BOŞ YERE yeşil kalır. Etiketler silinse ya da
    fabrika `None` dolu nesneler üretse bu hâl oluşurdu; burası onu çakar."""
    etkilenen: list[str] = []
    for ad, sema in _turevli_semalar():
        # 🔴 TEK nesne kurulur ve maskesi ondan türetilir: her çağrıda yeni bir
        # örnek üretilseydi alanlar farklı sayılar alır, türevler maske yüzünden
        # değil FABRİKA yüzünden farklı çıkar ve bu kontrol yalan söylerdi.
        ham = _ornek_model(sema)
        maskeli = maskele(ham, SENARYOLAR[senaryo])
        etkilenen += [
            f"{ad}.{turev}"
            for turev in sema.model_computed_fields
            if _turev_oku(maskeli, turev) != _turev_oku(ham, turev)
        ]
    assert etkilenen, (
        f"`{senaryo}` gizli kümesinde HİÇBİR türev maskeden etkilenmiyor — "
        "bekçi ölçtüğünü sandığı şeyi ölçmüyor olabilir."
    )

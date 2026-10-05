"""HASSAS ALAN BEKÇİSİ (IZN-B4): hassas bir alan ETİKETSİZ kalamaz.

## Ne bekçilenir

`ROUTERS`un HER rotasının şemaları taranır (zorunlu/rapor ayrımı aşağıda):

1. ZORUNLU modüllerin HER rotası `MaskeRotasi`dir (maske bağlamı bağımlılığı unutulamaz; rapor
   modundaki modüller geçince zorunluya girer).
2. Yanıt şemalarında (özyinelemeli: alan, liste, sözlük, türev dönüş tipi) her `Decimal`/zarf alan
   `Hassas` etiketi taşır — para kategorilerinden biri ya da AÇIKÇA `Hassas.yok`.
3. Adı kişisel desene uyan (`pii_adi_mi`) her düz `str` alan etiketlidir (ya da `yok`).
4. İSTEK gövdesi şemalarında da 2. ve 3. madde: gizli kategorili alanı yazma kapısı
   (`mask_route._yazma_kapisi`) etikete bakar; etiketsiz istek alanı = kapıda delik.
5. Para kategorisiyle etiketli alan `None` TAŞIYABİLİR (ya da zarftır): maske zorunlu alana `None`
   yazarsa şema kırılır (`model_copy` doğrulamaz, OpenAPI'de `required` alan `null` taşırdı).
6. Etiketli girdiden beslenen TÜREV (`computed_field`) `None` dönebilir.
7. AI araç şemaları (`ai/tools/schemas.py`) aynı kurallardan geçer.
8. Export uçları (gövdesini kendi üreten) tek tek kayıtlıdır (`EXPORT_UCLARI`).

## Kademeli geçiş: `ZORUNLU_MODULLER` (yalnız BÜYÜR)

Modül grupları sırayla etiketlenir. `ZORUNLU_MODULLER` (ve `ZORUNLU_SEMALAR`) içindeki bir modülde
etiketsiz Decimal/PII alanı KIRMIZIdır. Küme dışındaki modüller RAPOR modundadır: test geçer, modül
başına ihlal sayısı yazdırılır (`pytest -s`); B4b/c/d dilimleri sayıları buradan alır ve modülünü
ZORUNLU kümeye taşır. Küme dışındaki modülde ihlal kalmayınca `test_KAPANIS_*` yeşile döner
(şimdilik `xfail(strict=True)`: XPASS olursa işaret kaldırılır).
"""

from __future__ import annotations

import inspect
import re
import typing
from collections import defaultdict
from decimal import Decimal

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from app.core.field_mask import (
    Hassas,
    etiketler,
    pii_adi_mi,
    sayisal_mi,
    semalar_icinde,
)
from app.core.mask_route import MaskeRotasi
from app.core.router_registry import ROUTERS

#: ZORUNLU küme: bu rota-sahibi modüllerde etiketsiz alan KIRMIZI. Sonraki dilimler BURAYA ekler.
ZORUNLU_MODULLER: frozenset[str] = frozenset(
    {"boq", "catalog", "contracts", "customers", "dashboard", "offers", "projects", "sales",
     "sites", "units"}
)  # fmt: skip

#: Modülün TAMAMI değil yalnız bazı şemaları zorunlu olanlar: modül → şema sınıf adları.
ZORUNLU_SEMALAR: dict[str, frozenset[str]] = {
    "progress_payments": frozenset({"ProgressPaymentSummary"}),
}

#: Zarf sınıflarının KENDİSİ taranmaz (anlam kullanım yerinde etiketlenir).
_ZARF_SINIFLARI = {"MetricPlaceholder", "CountPlaceholder"}

#: Gövdesini kendi üreten uçlar: (metot, yol) → durum. "MASKELI": uç `maskele_baglamli` çağırır;
#: "HASSAS_ICERMEZ": ölçülmüş gerekçeyle; "RAPOR": sahibi modül ZORUNLU kümede DEĞİL (zorunluya
#: geçince MASKELI/HASSAS_ICERMEZ olur).
EXPORT_UCLARI: dict[tuple[str, str], str] = {
    ("GET", "/sites/{site_id}/boq/export"): "MASKELI",
    ("GET", "/audit-log/export.xlsx"): "RAPOR",
    ("GET", "/catalog/items/export"): "MASKELI",
    ("GET", "/chart-of-accounts/export.xlsx"): "RAPOR",
    ("GET", "/journal/export.xlsx"): "RAPOR",
    ("GET", "/trial-balance/export.xlsx"): "RAPOR",
    ("GET", "/equipment/work-summary/export.xlsx"): "RAPOR",
    ("GET", "/offers/{offer_id}/revisions/{rev_no}/export"): "MASKELI",
    ("GET", "/payroll/periods/export.xlsx"): "RAPOR",
    ("GET", "/payroll/periods/{period_id}/export"): "RAPOR",
    ("GET", "/personnel/export.xlsx"): "RAPOR",
    ("GET", "/projects/{project_id}/units/export.xlsx"): "MASKELI",
    ("GET", "/purchase-requests/{request_id}/quotes/export.xlsx"): "RAPOR",
    ("GET", "/sites/{site_id}/timesheet/export.xlsx"): "RAPOR",
}


def _rotalar(router: object):
    for rota in getattr(router, "routes", ()):
        if isinstance(rota, APIRoute):
            yield rota
        elif hasattr(rota, "original_router"):
            yield from _rotalar(rota.original_router)


def _tum_rotalar() -> list[APIRoute]:
    return [rota for router in ROUTERS for rota in _rotalar(router)]


def _sahip(rota: APIRoute) -> str:
    """Rotanın sahibi modül (uç fonksiyonunun paketi; `functools.wraps` `__module__`u korur)."""
    return rota.endpoint.__module__.split(".")[2]


def _ulasilan(kok_tipler: typing.Iterable[typing.Any]) -> dict[str, type[BaseModel]]:
    bulunan: dict[str, type[BaseModel]] = {}

    def ekle(sema: type[BaseModel]) -> None:
        ad = f"{sema.__module__}.{sema.__name__}"
        if ad in bulunan:
            return
        bulunan[ad] = sema
        for alan in sema.model_fields.values():
            for alt in semalar_icinde(alan.annotation):
                ekle(alt)
        for turev in sema.model_computed_fields.values():
            for alt in semalar_icinde(turev.return_type):
                ekle(alt)

    for tip in kok_tipler:
        for sema in semalar_icinde(tip):
            ekle(sema)
    return bulunan


def _duz_str_mi(annotation: typing.Any) -> bool:
    """Alan düz `str` mi (enum alt sınıfları DEĞİL: `wage_type` bir enum'dur)."""
    return any(d is str for d in _tip_yapraklari(annotation))


def _tip_yapraklari(annotation: typing.Any) -> typing.Iterator[typing.Any]:
    args = typing.get_args(annotation)
    if not args:
        yield annotation
        return
    if typing.get_origin(annotation) is typing.Annotated:
        yield from _tip_yapraklari(args[0])
        return
    for alt in args:
        yield from _tip_yapraklari(alt)


def _none_tasir_mi(annotation: typing.Any) -> bool:
    return type(None) in typing.get_args(annotation) or annotation is type(None)


def _zarf_mi(annotation: typing.Any) -> bool:
    return any(
        isinstance(d, type) and callable(getattr(d, "kisitli", None))
        for d in _tip_yapraklari(annotation)
    )


#: PARA anlamı taşıyan ad parçaları (`int`/`float` alanlar için) ve sayaç/ölçü imzaları (para
#: adını EZER: `items_missing_price` bir SAYIDIR, `net_area_m2` bir ALANDIR). `total` yalnız
#: `*_total`/`total_*` biçiminde paradır; çıplak `total` sayfalama sayacıdır (`_para_adi_mi`).
_PARA_ADLARI = (
    "amount", "price", "cost", "tutar", "bedel", "fiyat", "maliyet", "revenue", "profit",
    "budget", "butce", "balance", "kurus", "total", "gross", "net", "margin", "discount",
    "deposit", "payment", "income", "expense",
)  # fmt: skip
_SAYAC_ADLARI = (
    "count", "adet", "sayi", "missing", "pending", "index", "order", "sort", "area", "m2",
    "limit", "offset", "pct", "percent", "page", "days", "term", "units", "rows",
    "installment_total",
)  # fmt: skip


def _para_adi_mi(ad: str) -> bool:
    kucuk = ad.lower()
    if kucuk == "total":
        return False
    return not any(s in kucuk for s in _SAYAC_ADLARI) and any(p in kucuk for p in _PARA_ADLARI)


def _etiket_gerektiren_sayisal(ad: str, alan: FieldInfo) -> bool:
    """`Decimal`/para zarfı HER ZAMAN; `int`/`float` yalnız PARA adıysa."""
    if "Decimal" in str(alan.annotation) or _zarf_mi(alan.annotation):
        return True
    return sayisal_mi(alan.annotation) and _para_adi_mi(ad)


def sema_ihlalleri(sema: type[BaseModel], *, istek: bool) -> list[str]:
    """Tek şemanın ihlalleri (`Sema.alan: sebep`)."""
    ihlal: list[str] = []
    ad = f"{sema.__module__}.{sema.__name__}"
    for alan_adi, alan in sema.model_fields.items():
        etiket = etiketler(alan)
        kimlik = f"{ad}.{alan_adi}"
        if Hassas.yok in etiket and len(etiket) > 1:
            ihlal.append(f"{kimlik}: `yok` başka etiketle birlikte")
        if not etiket and _etiket_gerektiren_sayisal(alan_adi, alan):
            ihlal.append(f"{kimlik}: Decimal/zarf alan ETİKETSİZ")
        elif _duz_str_mi(alan.annotation) and pii_adi_mi(alan_adi) and not etiket:
            ihlal.append(f"{kimlik}: kişisel veri adlı str alan ETİKETSİZ")
        para_etiketli = bool(etiket - {Hassas.yok})
        if (
            not istek
            and para_etiketli
            and not _none_tasir_mi(alan.annotation)
            and not _zarf_mi(alan.annotation)
        ):
            ihlal.append(f"{kimlik}: maskeli olabilir ama tipi None TAŞIMIYOR")
    if not istek:
        ihlal.extend(_turev_ihlalleri(sema, ad))
    return ihlal


def _alt_agacta_maskeli_alan_var_mi(sema: type[BaseModel], gorulen: set[str] | None = None) -> bool:
    gorulen = set() if gorulen is None else gorulen
    ad = f"{sema.__module__}.{sema.__name__}"
    if ad in gorulen:
        return False
    gorulen.add(ad)
    for alan in sema.model_fields.values():
        if etiketler(alan) - {Hassas.yok}:
            return True
        if any(
            _alt_agacta_maskeli_alan_var_mi(a, gorulen) for a in semalar_icinde(alan.annotation)
        ):
            return True
    return False


def _turev_ihlalleri(sema: type[BaseModel], ad: str) -> list[str]:
    if not sema.model_computed_fields or not _alt_agacta_maskeli_alan_var_mi(sema):
        return []
    return [
        f"{ad}.{turev_adi}: türev girdisi maskelenebilir ama dönüş tipi None TAŞIMIYOR"
        for turev_adi, turev in sema.model_computed_fields.items()
        if ("Decimal" in str(turev.return_type) or sayisal_mi(turev.return_type))
        and not _none_tasir_mi(turev.return_type)
    ]


def _yanit_semalari(rota: APIRoute) -> dict[str, type[BaseModel]]:
    return {
        ad: s
        for ad, s in _ulasilan([rota.response_model]).items()
        if s.__name__ not in _ZARF_SINIFLARI
    }


def _istek_semalari(rota: APIRoute) -> dict[str, type[BaseModel]]:
    return _ulasilan([b.field_info.annotation for b in rota.dependant.body_params])


def modul_ihlalleri() -> dict[str, list[str]]:
    """Rota-sahibi modül → ihlal listesi (yanıt + istek + AI araç şemaları)."""
    sonuc: dict[str, set[str]] = defaultdict(set)
    for rota in _tum_rotalar():
        sahip = _sahip(rota)
        sonuc[sahip]  # modül listede görünsün (ihlalsiz de)
        for sema in _yanit_semalari(rota).values():
            sonuc[sahip].update(sema_ihlalleri(sema, istek=False))
        for sema in _istek_semalari(rota).values():
            sonuc[sahip].update(sema_ihlalleri(sema, istek=True))
    sonuc["ai_araclari"].update(
        i for s in _ai_arac_semalari() for i in sema_ihlalleri(s, istek=False)
    )
    return {modul: sorted(v) for modul, v in sonuc.items()}


def _ai_arac_semalari() -> list[type[BaseModel]]:
    from app.modules.ai.tools import schemas

    return [
        obj
        for obj in vars(schemas).values()
        if inspect.isclass(obj)
        and issubclass(obj, BaseModel)
        and obj.__module__ == schemas.__name__
    ]


def _export_rotalari() -> dict[tuple[str, str], APIRoute]:
    """Gövdesini kendi üreten (response_model yok) okuma uçlarından export/indirme olanlar."""
    return {
        ("GET", rota.path): rota
        for rota in _tum_rotalar()
        if rota.response_model is None
        and rota.methods == {"GET"}
        and re.search(r"export", rota.path)
    }


# --- TESTLER -------------------------------------------------------------------------------------


def test_ZORUNLU_modullerin_HER_rotasi_MaskeRotasidir() -> None:
    """RAPOR modundaki modüllerin rotaları B4b/c/d'de geçer (o zaman ZORUNLU kümeye girerler)."""
    disarida = [
        f"{sorted(r.methods)} {r.path}"
        for r in _tum_rotalar()
        if _sahip(r) in ZORUNLU_MODULLER and not isinstance(r, MaskeRotasi)
    ]
    assert not disarida, f"MaskeRotasi taşımayan rota: {disarida}"
    assert len(_tum_rotalar()) > 400, "tarama kümesi küçüldü"


def test_hassas_semayi_donduren_HER_rota_MaskeRotasidir() -> None:
    """K2 (IZN-B4a): sahibi hangi modül olursa olsun, yanıt şemasının AĞACINDA etiketli alan taşıyan
    HER rota `MaskeRotasi`dir. Aksi hâlde uç maskesiz döner (`progress_payments` özeti, router
    ZORUNLU kümede olmadığı için kaçmıştı). Rota tablosu + `response_model` taraması."""
    from app.core.field_mask import sema_plani

    acik = [
        f"{sorted(r.methods)} {r.path} ({_sahip(r)})"
        for r in _tum_rotalar()
        if r.response_model is not None
        and any(sema_plani(sema).hassas_agac for sema in semalar_icinde(r.response_model))
        and not isinstance(r, MaskeRotasi)
    ]
    assert not acik, f"hassas şema döndüren ama MaskeRotasi taşımayan rota: {acik}"


def zorunlu_ihlaller() -> dict[str, list[str]]:
    """ZORUNLU kümedeki modüllerin (ve ZORUNLU şemaların) ihlalleri; yalnız ihlalli olanlar."""
    sonuc: dict[str, list[str]] = {}
    for modul, ihlal in modul_ihlalleri().items():
        if modul in ZORUNLU_MODULLER:
            secili = ihlal
        elif modul in ZORUNLU_SEMALAR:
            oneks = tuple(f"app.modules.{modul}.schemas.{sema}." for sema in ZORUNLU_SEMALAR[modul])
            secili = [i for i in ihlal if i.startswith(oneks)]
        else:
            continue
        if secili:
            sonuc[modul] = secili
    return sonuc


def rapor_ihlal_sayilari() -> dict[str, int]:
    """RAPOR modundaki (ZORUNLU olmayan) modüllerin ihlal sayısı (şema-zorunlu modüller dahil:
    zorunlu şemaların DIŞINDAKİ ihlaller)."""
    sayilar: dict[str, int] = {}
    for modul, ihlal in modul_ihlalleri().items():
        if modul in ZORUNLU_MODULLER:
            continue
        sayilar[modul] = len(ihlal) - len(zorunlu_ihlaller().get(modul, []))
    return {m: n for m, n in sorted(sayilar.items()) if n}


def test_ZORUNLU_moduller_TEMIZDIR() -> None:
    """ZORUNLU kümede etiketsiz Decimal/PII alanı KIRMIZI."""
    ihlaller = zorunlu_ihlaller()
    assert not ihlaller, "ZORUNLU modüllerde etiketsiz alan var:\n" + "\n".join(
        f"  {m}: {v[:8]} (+{max(len(v) - 8, 0)})" for m, v in sorted(ihlaller.items())
    )


def test_ZORUNLU_kume_gercek_modulleri_gosterir() -> None:
    """Bayat/yazım hatalı küme girdisi bekçiyi sessizce boşaltırdı."""
    mevcut = set(modul_ihlalleri())
    assert ZORUNLU_MODULLER <= mevcut, sorted(ZORUNLU_MODULLER - mevcut)
    assert set(ZORUNLU_SEMALAR) <= mevcut
    assert not (ZORUNLU_MODULLER & set(ZORUNLU_SEMALAR)), "modül hem tam hem şema-zorunlu"
    for modul, semalar in ZORUNLU_SEMALAR.items():
        gorulen = {
            s.__name__
            for r in _tum_rotalar()
            if _sahip(r) == modul
            for s in (_yanit_semalari(r) | _istek_semalari(r)).values()
        }
        assert semalar <= gorulen, (modul, sorted(semalar - gorulen))


def test_RAPOR_modu_ihlal_sayilarini_yazar(capsys: pytest.CaptureFixture[str]) -> None:
    """Geçer; B4b/c/d için modül başına kalan ihlal sayısını `-s` ile yazdırır."""
    sayilar = rapor_ihlal_sayilari()
    with capsys.disabled():
        print("\nHASSAS-ALAN RAPOR (zorunlu olmayan modül -> ihlal):")
        for modul, sayi in sayilar.items():
            print(f"  {modul}: {sayi}")
        print(f"  TOPLAM: {sum(sayilar.values())}")
    assert all(modul not in ZORUNLU_MODULLER for modul in sayilar)


@pytest.mark.xfail(strict=True, reason="IZN-B4 devam ediyor: RAPOR modunda ihlalli modül var")
def test_KAPANIS_hicbir_modul_rapor_modunda_degil() -> None:
    assert not rapor_ihlal_sayilari()


def test_EXPORT_uclari_TEK_tek_kayitlidir() -> None:
    gercek = set(_export_rotalari())
    assert gercek == set(EXPORT_UCLARI), (
        f"kayıtsız export: {sorted(gercek - set(EXPORT_UCLARI))}; "
        f"bayat kayıt: {sorted(set(EXPORT_UCLARI) - gercek)}"
    )
    for kimlik, rota in _export_rotalari().items():
        durum = EXPORT_UCLARI[kimlik]
        assert durum in {"MASKELI", "HASSAS_ICERMEZ", "RAPOR"}, (kimlik, durum)
        if durum == "MASKELI":
            assert "maskele_baglamli(" in inspect.getsource(rota.endpoint.__wrapped__), kimlik
        if durum == "RAPOR":
            assert _sahip(rota) not in ZORUNLU_MODULLER, f"{kimlik}: sahibi modül ZORUNLU"


# --- POZİTİF KONTROLLER: bekçi her kuralda sentetik ihlalle FİİLEN ateşlenir ---------------


def test_bekci_sentetik_ihlalleri_yakalar_etiketliyi_gecirir() -> None:
    class _Kotu(BaseModel):
        tutar: Decimal  # etiketsiz Decimal
        tc_no: str  # etiketsiz PII
        fiyat: typing.Annotated[Decimal, Hassas.maliyet_kar]  # etiketli ama None taşımıyor

    class _Iyi(BaseModel):
        tutar: typing.Annotated[Decimal | None, Hassas.maliyet_kar]
        adet: typing.Annotated[Decimal, Hassas.yok]
        tc_no: typing.Annotated[str | None, Hassas.maas_kisisel]
        wage_type: typing.Literal["monthly", "daily"]  # enum benzeri: PII sayılmaz

    kotu = " ".join(sema_ihlalleri(_Kotu, istek=False))
    assert "tutar: Decimal/zarf alan ETİKETSİZ" in kotu
    assert "tc_no: kişisel veri adlı str alan ETİKETSİZ" in kotu
    assert "fiyat: maskeli olabilir ama tipi None TAŞIMIYOR" in kotu
    assert sema_ihlalleri(_Iyi, istek=False) == []
    # istek gövdesinde None-taşıma şartı YOK (zorunlu fiyat girilebilir), etiket şartı VAR
    assert [i for i in sema_ihlalleri(_Kotu, istek=True) if "None TAŞIMIYOR" in i] == []
    assert any("tutar" in i for i in sema_ihlalleri(_Kotu, istek=True))


def test_bekci_TUREV_None_donemeyen_turevi_yakalar() -> None:
    from pydantic import computed_field

    class _Maskeli(BaseModel):
        fiyat: typing.Annotated[Decimal | None, Hassas.sozlesme_fiyat]

        @computed_field  # type: ignore[prop-decorator]
        @property
        def tutar(self) -> Decimal:
            return self.fiyat or Decimal(0)

    assert any("tutar" in i for i in sema_ihlalleri(_Maskeli, istek=False))


def test_tarama_kumesi_BOS_degildir_boq_temizdir() -> None:
    """Küme boşalırsa tüm bekçi sessizce yeşil kalırdı; BOQ ilk tam etiketli modüldür."""
    ihlaller = modul_ihlalleri()
    assert "boq" in ihlaller
    assert ihlaller["boq"] == []
    boq_semalari = {
        ad for rota in _tum_rotalar() if _sahip(rota) == "boq" for ad in _yanit_semalari(rota)
    }
    assert any(ad.endswith("BoqItemResponse") for ad in boq_semalari)
    etiketli = [
        alan
        for rota in _tum_rotalar()
        if _sahip(rota) == "boq"
        for sema in _yanit_semalari(rota).values()
        for alan in sema.model_fields.values()
        if etiketler(alan) - {Hassas.yok}
    ]
    assert len(etiketli) >= 10, "BOQ'nun para etiketleri kayboldu"

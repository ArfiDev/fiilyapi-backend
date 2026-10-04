"""🔴 ROTA BEKÇİSİ İSKELETİ (DSC-B0): "disipline duyarlı" her rota ya SÜZÜLMÜŞ olarak
İŞARETLİ ya da izin listesinde (DISIPLIN-KAPSAMI-SPEC dilimleri B1-B5).

## İşaret
Rota, `app.core.discipline_deps.resolve_discipline_scope` bağımlılığını (`DisciplineScoped`)
bağımlılık ağacında taşıyorsa süzülmüş sayılır (gerekçe: o modülün docstring'i). Bugün
HİÇBİR rota taşımaz → izin listesi bugünkü duyarlı rotaların TAMAMIdır; B1-B5 rotaları
işaretledikçe girdiyi siler, B6 listeyi boşaltır (`IZIN_LISTESI == {}`).

## Sınıflandırıcı — TAM DEĞİLDİR
"Duyarlı rota" = spec'in dilim listesindeki modüllerin uçları (ucun `endpoint.__module__`
kökü): BOQ, günlük (site_diary), earned_value, hakediş (progress_payments +
subcontractor_progress_payments) ve inventory'nin YALNIZ stok satırı uçları. Bu önek
kümesinin DIŞINDA kalan duyarlı uçlar (ör. `boq/progress.py` agregatlarını okuyan
sites/sections/projects kartları — B4) sınıflandırıcıya B dilimlerinde EKLENİR; bu bekçi
onları bugün görmez. Bilinçli dışlananlar: `/users/{id}/disciplines` (kullanıcı yönetimi),
EV `settings` GET/PUT (santiye yapılandırması; satır verisi yok), inventory kalem/depo
tanımları.

## Modül-kökü dışı EK duyarlı rotalar (CEO kararı 2026-09-29) — `EK_DUYARLI_ROTALAR`
(a) `GET/PUT /projects/{project_id}/contract/distribution` (`contracts/distribution.py:154,498`)
    → DSC-B5: ticari/proje düzeyi belge, Ü2 (hakediş) ile aynı sınıf → kısıtlıya 403.
(b) `GET /dashboard/summary` (`dashboard/risks.py:141-143`) → DSC-B5: yalnız hakediş kaynakları
    duyarlı (S2); stok riskleri disiplinsiz (kullanıcı kararı 2026-09-29).

## DSC-B1 yeniden etiketleri (CEO 2026-09-29) — gerekçeler `_dilim`in yorumlarında
* iki `diary-suggestion` ucu → DSC-B5 (hakediş sınıfı, 403) · `/stock/summary` +
  `/sites/{site_id}/stock` → (B4) DUYARLI DEĞİL: stok disiplinsiz,
  `STOK_DUYARSIZ_ROTALAR` · `GET …/budget/revisions` sınıflandırıcıdan
  ÇIKTI (disiplin verisi yok) · `POST …/budget/preview` → DSC-B3 (VIEW kapılı okuma).
* `test_izin_listesi_etiketi_duyarli_rota_hedef_dilimine_esittir`: etiket ↔ hedef dilim eşitliği.

⚠️ `iter_route_contexts` + EFEKTİF bağlam (`ctx.dependant`): FastAPI 0.141 `include_router`ı
`_IncludedRouter` tutar, `app.routes` düz dolaşımı alt yönlendirici rotalarını GÖRMEZ
(`test_getdb_kapsam_bekcisi.py`). "Sıfır rota tarandı" ve "her aile ≥1 rota" kontrolleri
sessiz-boş taramayı (sınıflandırıcı bozulması) KIRMIZI yapar.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
import typing

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts

from app.core.discipline_deps import require_unrestricted, resolve_discipline_scope
from app.main import app

Rota = tuple[str, str]

#: endpoint modülü kökü -> aile adı (ailelerin hepsi ≥1 rota taramalı).
AILE_KOKLERI: dict[str, str] = {
    "app.modules.boq.": "boq",
    "app.modules.site_diary.": "gunluk",
    "app.modules.earned_value.": "ev",
    "app.modules.progress_payments.": "hakedis",
    "app.modules.subcontractor_progress_payments.": "tas_hakedis",
    "app.modules.inventory.": "stok",
}

#: Duyarlı OLMAYAN uçlar (gerekçe modül docstring'inde).
KAPSAM_DISI_EV_MODULLERI = ("app.modules.earned_value.user_discipline_router",)
STOK_SATIR_YOLLARI = frozenset(
    {"/stock/entries", "/stock/summary", "/sites/{site_id}/stock", "/sections/{section_id}/stock"}
)
_STOK_GEREKCE = (
    "Kullanıcı kararı 2026-09-29: stok disiplinsiz — gerçek depo bakiyesi ve tüm hareketler "
    "herkese açık"
)
#: Duyarlı OLMAYAN stok OKUMA uçları (kullanıcı kararı): (yöntem, yol) -> gerekçe. `_dilim` bu
#: yolları None döner; listeden çıkarılırsa rota duyarlı sayılır ve ne işaretli ne izin
#: listesinde olduğu için bekçi KIRMIZI olur. Yazma (POST /stock/entries) da dahildir (S5).
STOK_DUYARSIZ_ROTALAR: dict[Rota, str] = {
    ("GET", "/stock/summary"): _STOK_GEREKCE,
    ("GET", "/sites/{site_id}/stock"): _STOK_GEREKCE,
    ("GET", "/stock/entries"): _STOK_GEREKCE,
    ("GET", "/sections/{section_id}/stock"): _STOK_GEREKCE,
    # DSC-B5 (S5): yazma da disiplinsiz — gerçek depo bakiyesi tek kaynak.
    ("POST", "/stock/entries"): _STOK_GEREKCE,
}
#: IZN-B3: disiplin PROJE BAŞINA atanır; ŞİRKET GENELİ katalog uçları (disiplin listesi/CRUD +
#: birim oran kataloğu) kullanıcı kapsamıyla SÜZÜLMEZ ve `RequireUnrestricted` taşımaz (yönetim
#: yalnız sayfa izniyle). Duyarlı sayılmazlar; tersine işaretli olmaları KIRMIZIdır
#: (`test_sirket_geneli_katalog_uclari_kapsam_ve_u6_tasimaz`).
SIRKET_GENELI_KATALOG_ROTALARI: frozenset[Rota] = frozenset(
    {
        ("GET", "/earned-value/catalog"),
        ("POST", "/earned-value/catalog"),
        ("PATCH", "/earned-value/catalog/{item_id}"),
        ("POST", "/earned-value/catalog/{item_id}/adopt-actual"),
        ("GET", "/earned-value/disciplines"),
        ("POST", "/earned-value/disciplines"),
        ("PATCH", "/earned-value/disciplines/{discipline_id}"),
        ("DELETE", "/earned-value/disciplines/{discipline_id}"),
    }
)
_EV_B3_YOLU = re.compile(r"/earned-value/(panel|reports/|settings/preview)")
_EV_AYAR_YOLU = re.compile(r"/earned-value/settings$")
#: Modül kökü sınıflandırıcısının DIŞINDA kalan duyarlı rotalar: (yöntem, yol) -> (dilim,
#: gerekçe). Her girdi gerçekten var olmalı (bayat → kırmızı).
_B4_GEREKCE = (
    "proje/şantiye/bölüm kartları boq/progress.py fiziksel % + boq_item_count/budget "
    "agregatlarını okur (S3/S5/S6: kendi disiplini, kalem yoksa —); "
    "worker_count/section_count/budget_amount değişmez"
)
EK_DUYARLI_ROTALAR: dict[Rota, tuple[str, str]] = {
    ("GET", "/projects/{project_id}/contract/distribution"): (
        "DSC-B5",
        "sözleşme dağıtımı ticari/proje düzeyi belge; Ü2 (hakediş) ile aynı sınıf → 403",
    ),
    ("PUT", "/projects/{project_id}/contract/distribution"): (
        "DSC-B5",
        "sözleşme dağıtımı ticari/proje düzeyi belge; Ü2 (hakediş) ile aynı sınıf → 403",
    ),
    ("GET", "/dashboard/summary"): (
        "DSC-B5",
        "yalnız hakediş kaynakları duyarlı (S2); stok riskleri disiplinsiz (kullanıcı kararı)",
    ),
    ("GET", "/projects/{project_id}/sites"): (
        "DSC-B4",
        _B4_GEREKCE,
    ),
    ("POST", "/projects/{project_id}/sites"): (
        "DSC-B4",
        _B4_GEREKCE,
    ),
    ("GET", "/sites/{site_id}"): (
        "DSC-B4",
        _B4_GEREKCE,
    ),
    ("PATCH", "/sites/{site_id}"): (
        "DSC-B4",
        _B4_GEREKCE,
    ),
    ("GET", "/sites/{site_id}/sections"): (
        "DSC-B4",
        _B4_GEREKCE,
    ),
    ("POST", "/sites/{site_id}/sections"): (
        "DSC-B4",
        _B4_GEREKCE,
    ),
    ("GET", "/sections/{section_id}"): (
        "DSC-B4",
        _B4_GEREKCE,
    ),
    ("PATCH", "/sections/{section_id}"): (
        "DSC-B4",
        _B4_GEREKCE,
    ),
    ("GET", "/projects"): (
        "DSC-B4",
        _B4_GEREKCE,
    ),
    ("POST", "/projects"): (
        "DSC-B4",
        _B4_GEREKCE,
    ),
    ("GET", "/projects/{project_id}"): (
        "DSC-B4",
        _B4_GEREKCE,
    ),
    ("PATCH", "/projects/{project_id}"): (
        "DSC-B4",
        _B4_GEREKCE,
    ),
}
_YAZMA = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _aile(modul: str) -> str | None:
    if modul.startswith(KAPSAM_DISI_EV_MODULLERI):
        return None
    return next((ad for kok, ad in AILE_KOKLERI.items() if modul.startswith(kok)), None)


def _dilim(aile: str, yontem: str, yol: str) -> str | None:
    """Duyarlı rotanın HEDEF dilimi (spec dilim tanımı); duyarlı değilse None."""
    yazma = yontem in _YAZMA
    if (yontem, yol) in SIRKET_GENELI_KATALOG_ROTALARI:
        return None
    if aile in ("hakedis", "tas_hakedis"):
        return "DSC-B5"
    if aile == "stok":
        if yol not in STOK_SATIR_YOLLARI:
            return None
        return None if (yontem, yol) in STOK_DUYARSIZ_ROTALAR else ("DSC-B2" if yazma else "DSC-B1")
    if aile == "ev" and _EV_AYAR_YOLU.search(yol):
        # DSC-B3 (S11/Ü6): GET pacal kartları kapsama göre gizler; PUT yapılandırmadır → 403.
        return "DSC-B3"
    if aile == "ev" and not yazma and yol.endswith("/budget/revisions"):
        # DSC-B1 (CEO 2026-09-29): revizyon LİSTESİ (numara/durum/dondurma zamanı) disiplin ya
        # da kalem verisi taşımaz → duyarlı DEĞİL.
        return None
    if aile == "ev" and yazma and yol.endswith("/budget/preview"):
        # POST …/budget/preview VIEW kapılı bir OKUMADIR (eğri hesabı, yazmaz): disiplin bazında
        # eğri ürettiği için hedef B3 (EV agregatları); "yazma → B2" kuralının istisnası.
        return "DSC-B3"
    if aile == "gunluk" and not yazma and yol.endswith("/diary-suggestion"):
        # günlükten hakediş önerisi: hakediş (Ü2) ile aynı sınıf → kısıtlıya 403 (B5).
        return "DSC-B5"
    if aile == "ev" and not yazma and _EV_B3_YOLU.search(yol):
        return "DSC-B3"
    return "DSC-B2" if yazma else "DSC-B1"


def duyarli_rotalar() -> tuple[dict[Rota, str], dict[str, int]]:
    """(yöntem, yol) -> hedef dilim; ayrıca aile başına taranan rota sayısı."""
    sonuc: dict[Rota, str] = {}
    aile_sayilari: dict[str, int] = {}
    for ctx in iter_route_contexts(app.routes):
        rota = ctx.original_route
        if not isinstance(rota, APIRoute):
            continue
        aile = _aile(rota.endpoint.__module__)
        if aile is None:
            continue
        for yontem in sorted(ctx.methods):
            dilim = _dilim(aile, yontem, ctx.path)
            if dilim is None:
                continue
            sonuc[(yontem, ctx.path)] = dilim
            aile_sayilari[aile] = aile_sayilari.get(aile, 0) + 1
    mevcut = {(y, c.path) for c in iter_route_contexts(app.routes) for y in c.methods}
    for rota_anahtari, (dilim, _gerekce) in EK_DUYARLI_ROTALAR.items():
        if rota_anahtari in mevcut:
            sonuc[rota_anahtari] = dilim
    return sonuc, aile_sayilari


def _isaretli(dependant: Dependant, gorulen: set[int] | None = None) -> bool:
    gorulen = gorulen if gorulen is not None else set()
    if id(dependant) in gorulen:
        return False
    gorulen.add(id(dependant))
    if dependant.call is resolve_discipline_scope:
        return True
    return any(_isaretli(alt, gorulen) for alt in dependant.dependencies)


def _bagimlilik_agacinda(
    dependant: Dependant, hedef: object, gorulen: set[int] | None = None
) -> bool:
    gorulen = gorulen if gorulen is not None else set()
    if id(dependant) in gorulen:
        return False
    gorulen.add(id(dependant))
    if dependant.call is hedef:
        return True
    return any(_bagimlilik_agacinda(alt, hedef, gorulen) for alt in dependant.dependencies)


#: DSC-B2 Ü6/Ü5: kısıtlıya 403 veren (`RequireUnrestricted`) rotalar. Her biri bağımlılık
#: ağacında `require_unrestricted` taşımalı; A2 (EV) kendi Ü6 rotalarını buraya EKLER.
U6_ROTALARI: frozenset[Rota] = frozenset(
    {
        ("POST", "/sites/{site_id}/boq/groups"),
        ("PUT", "/sites/{site_id}/earned-value/budget/distributions"),
        ("POST", "/sites/{site_id}/earned-value/budget/fill-from-catalog"),
        ("POST", "/sites/{site_id}/earned-value/budget/fill-from-contract"),
        ("POST", "/sites/{site_id}/earned-value/budget/freeze"),
        ("PUT", "/sites/{site_id}/earned-value/budget/group-disciplines"),
        ("POST", "/sites/{site_id}/earned-value/budget/revisions"),
        ("PUT", "/sites/{site_id}/earned-value/budget/windows"),
        ("POST", "/sites/{site_id}/earned-value/days/{day}/unlock"),
        ("POST", "/sites/{site_id}/earned-value/reports/daily/{day}/approve"),
        ("PUT", "/sites/{site_id}/earned-value/settings"),
        # DSC-B5 (Ü2): hakediş router'ları + diary-suggestion + sözleşme dağıtımı.
        ("GET", "/projects/{project_id}/contract/distribution"),
        ("PUT", "/projects/{project_id}/contract/distribution"),
        ("GET", "/projects/{project_id}/progress-payments/diary-suggestion"),
        ("GET", "/subcontractor-contracts/{contract_id}/progress-payments/diary-suggestion"),
        ("GET", "/progress-payments"),
        ("DELETE", "/progress-payments/{payment_id}"),
        ("GET", "/progress-payments/{payment_id}"),
        ("PATCH", "/progress-payments/{payment_id}"),
        ("POST", "/progress-payments/{payment_id}/approve"),
        ("PUT", "/progress-payments/{payment_id}/lines"),
        ("POST", "/progress-payments/{payment_id}/mark-paid"),
        ("POST", "/progress-payments/{payment_id}/refresh-prices"),
        ("POST", "/progress-payments/{payment_id}/reject"),
        ("POST", "/progress-payments/{payment_id}/submit"),
        ("POST", "/progress-payments/{payment_id}/unapprove"),
        ("POST", "/projects/{project_id}/progress-payments"),
        ("GET", "/projects/{project_id}/progress-payments/summary"),
        ("POST", "/subcontractor-contracts/{contract_id}/progress-payments"),
        ("GET", "/subcontractor-progress-payments"),
        ("GET", "/subcontractor-progress-payments/summary"),
        ("DELETE", "/subcontractor-progress-payments/{payment_id}"),
        ("GET", "/subcontractor-progress-payments/{payment_id}"),
        ("PATCH", "/subcontractor-progress-payments/{payment_id}"),
        ("POST", "/subcontractor-progress-payments/{payment_id}/approve"),
        ("PUT", "/subcontractor-progress-payments/{payment_id}/lines"),
        ("POST", "/subcontractor-progress-payments/{payment_id}/mark-paid"),
        ("POST", "/subcontractor-progress-payments/{payment_id}/refresh-prices"),
        ("POST", "/subcontractor-progress-payments/{payment_id}/reject"),
        ("POST", "/subcontractor-progress-payments/{payment_id}/submit"),
        ("POST", "/subcontractor-progress-payments/{payment_id}/unapprove"),
    }
)


#: SIL-B1 (KARARLAR §1.7, K4): silme HER KOŞULDA yalnız Sistem Yöneticisi'nindir; disiplin atanmış
#: Sistem Yöneticisi de siler. Eskiden Ü6 olan bu DELETE uçlarında `RequireUnrestricted` KALDIRILDI.
#: Disiplin süzgeci bu uçlarda KASITLI olarak uygulanmaz: "işaretli" sayılırlar ve kapıları
#: `test_silme_muaf_rotalar_yalniz_delete_ve_sistem_yoneticisi_kapili` ile kilitlidir.
SILME_MUAF_ROTALARI: frozenset[Rota] = frozenset(
    {
        ("DELETE", "/boq/groups/{group_id}"),
        ("DELETE", "/diary/{entry_id}"),
        ("DELETE", "/earned-value/disciplines/{discipline_id}"),
        ("DELETE", "/sites/{site_id}/earned-value/budget/revisions/{revision_id}"),
    }
)


def isaretli_rotalar() -> set[Rota]:
    sonuc: set[Rota] = set(SILME_MUAF_ROTALARI)
    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        dependant = ctx.dependant or ctx.original_route.dependant
        if _isaretli(dependant):
            sonuc |= {(yontem, ctx.path) for yontem in ctx.methods}
    return sonuc


#: (yöntem, yol) -> hedef dilim. B1-B5 girdi siler; B6 boşaltır.
IZIN_LISTESI: dict[Rota, str] = {}


# --------------------------------------------------------------------- testler


def test_siniflandirici_bos_donmez_ve_her_aile_rota_tarar() -> None:
    """🔴 Sessiz-boş tarama (önek/`_IncludedRouter` bozulması) KIRMIZI olur."""
    duyarli, aile_sayilari = duyarli_rotalar()
    assert duyarli, "sıfır rota tarandı — sınıflandırıcı ya da rota dolaşımı bozuk"
    # `stok` ailesi B5 (S5) sonrası tamamen duyarsız: taranan duyarlı rota 0 BEKLENİR; varlığı
    # `test_stok_okuma_uclari_duyarli_degildir_ve_suzulmez` (bayat stok rotası) kilitler.
    eksik = set(AILE_KOKLERI.values()) - aile_sayilari.keys() - {"stok"}
    assert not eksik, f"şu ailelerde HİÇ rota taranmadı (önek bozuk?): {sorted(eksik)}"


def test_ek_duyarli_rotalar_bayat_girdi_icermez() -> None:
    mevcut = {(y, c.path) for c in iter_route_contexts(app.routes) for y in c.methods}
    bayat = sorted(set(EK_DUYARLI_ROTALAR) - mevcut)
    assert not bayat, f"BAYAT ek duyarlı rota (artık yok): {bayat}"
    duyarli, _ = duyarli_rotalar()
    assert set(EK_DUYARLI_ROTALAR) <= set(duyarli)


def test_stok_okuma_uclari_duyarli_degildir_ve_suzulmez() -> None:
    """Kullanıcı kararı 2026-09-29 (stok disiplinsiz): dört stok okuma ucu vardır, duyarlı
    sayılmaz, `DisciplineScoped` taşımaz ve izin listesinde değildir."""
    mevcut = {(y, c.path) for c in iter_route_contexts(app.routes) for y in c.methods}
    assert set(STOK_DUYARSIZ_ROTALAR) <= mevcut, "bayat stok rotası"
    duyarli, _ = duyarli_rotalar()
    isaretli = isaretli_rotalar()
    for rota in STOK_DUYARSIZ_ROTALAR:
        assert rota not in duyarli, rota
        assert rota not in isaretli, rota
        assert rota not in IZIN_LISTESI, rota


def test_izin_listesinde_b4_girdisi_kalmadi() -> None:
    assert not [r for r, d in IZIN_LISTESI.items() if d == "DSC-B4"]


def test_izin_listesinde_b5_girdisi_kalmadi() -> None:
    assert not [r for r, d in IZIN_LISTESI.items() if d == "DSC-B5"]


def test_sirket_geneli_katalog_uclari_kapsam_ve_u6_tasimaz() -> None:
    """IZN-B3: katalog/disiplin yönetimi şirket geneli; `DisciplineScoped`/`RequireUnrestricted`
    TAŞIMAZ (taşırsa proje başına disiplinle çelişir: kısıtlı kişi şirket kataloğunu göremezdi)."""
    bulunan: set[Rota] = set()
    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        dependant = ctx.dependant or ctx.original_route.dependant
        for yontem in ctx.methods:
            rota = (yontem, ctx.path)
            if rota not in SIRKET_GENELI_KATALOG_ROTALARI:
                continue
            bulunan.add(rota)
            assert not _isaretli(dependant), rota
            assert not _bagimlilik_agacinda(dependant, require_unrestricted), rota
    assert bulunan == set(SIRKET_GENELI_KATALOG_ROTALARI), "bayat şirket geneli katalog rotası"


def test_kullanici_disiplin_uclari_duyarli_degildir() -> None:
    duyarli, _ = duyarli_rotalar()
    assert ("GET", "/users/{user_id}/disciplines") not in duyarli
    assert ("PUT", "/users/{user_id}/disciplines") not in duyarli
    assert not [r for r in duyarli if "/disciplines" in r[1] and r[1].startswith("/users")]


def test_duyarli_her_rota_isaretli_ya_da_izin_listesinde() -> None:
    duyarli, _ = duyarli_rotalar()
    isaretli = isaretli_rotalar()
    sorunlu = sorted(r for r in duyarli if r not in isaretli and r not in IZIN_LISTESI)
    assert not sorunlu, (
        "Duyarlı ama ne `DisciplineScoped` işaretli ne de izin listesinde olan rotalar "
        f"(yeni uç mü eklendi? süz + işaretle ya da hedef dilimle listeye ekle): {sorunlu}"
    )


def test_izin_listesi_bayat_girdi_icermez() -> None:
    duyarli, _ = duyarli_rotalar()
    isaretli = isaretli_rotalar()
    yok = sorted(r for r in IZIN_LISTESI if r not in duyarli)
    assert not yok, f"BAYAT: izin listesindeki rota artık yok / duyarlı değil: {yok}"
    islenmis = sorted(r for r in IZIN_LISTESI if r in isaretli)
    assert not islenmis, f"BAYAT: rota artık işaretli, izin listesinden SİL: {islenmis}"


def test_izin_listesi_hedef_dilim_etiketleri_gecerli() -> None:
    gecersiz = {r: d for r, d in IZIN_LISTESI.items() if not re.fullmatch(r"DSC-B[1-5]", d)}
    assert not gecersiz, f"geçersiz dilim etiketi: {gecersiz}"


def test_izin_listesi_tam_olarak_duyarli_eksi_isaretli_kumedir() -> None:
    """KALICI DEĞİŞMEZ: izin listesi anahtarları == duyarlı ∖ işaretli. Bugün işaretli yok →
    liste tüm duyarlı rotalar; B1-B5 işaretledikçe girdi silinir (bu test SİLİNMEZ), B6'da
    liste boş kalır."""
    duyarli, _ = duyarli_rotalar()
    assert set(IZIN_LISTESI) == set(duyarli) - isaretli_rotalar()


def test_izin_listesi_etiketi_duyarli_rota_hedef_dilimine_esittir() -> None:
    """KALICI DEĞİŞMEZ (DSC-B1): izin listesindeki her etiket sınıflandırıcının hedef dilimine
    EŞİT — etiket eskirse (rota başka dilime taşındı) liste sessizce yalan söylemez."""
    duyarli, _ = duyarli_rotalar()
    farkli = {
        r: (IZIN_LISTESI[r], duyarli[r]) for r in IZIN_LISTESI if IZIN_LISTESI[r] != duyarli[r]
    }
    assert not farkli, f"izin listesi etiketi != sınıflandırıcı hedefi: {farkli}"


def test_silme_muaf_rotalar_yalniz_delete_ve_sistem_yoneticisi_kapili() -> None:
    """Muaf rotalar DELETE'tir, `require_system_admin` taşır ve `require_unrestricted` TAŞIMAZ."""
    bulunan: set[Rota] = set()
    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        dependant = ctx.dependant or ctx.original_route.dependant
        for yontem in ctx.methods:
            rota = (yontem, ctx.path)
            if rota not in SILME_MUAF_ROTALARI:
                continue
            bulunan.add(rota)
            assert yontem == "DELETE", rota
            assert not _bagimlilik_agacinda(dependant, require_unrestricted), rota
    assert bulunan == set(SILME_MUAF_ROTALARI), (
        f"bayat muaf rota: {set(SILME_MUAF_ROTALARI) - bulunan}"
    )


def test_require_unrestricted_delete_isteklerini_atlar_diger_metotlarda_kisitliyi_durdurur() -> (
    None
):
    """Router düzeyinde `RequireUnrestricted` taşıyan router'lar (teklifler, hakedişler) için DELETE
    istisnası bağımlılığın İÇİNDE yaşar; DELETE dışı metotların kapısı DEĞİŞMEDİ."""
    import asyncio  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    from fastapi import HTTPException  # noqa: PLC0415

    kisitli = SimpleNamespace(is_restricted=True)
    asyncio.run(require_unrestricted(SimpleNamespace(method="DELETE"), kisitli))  # type: ignore[arg-type]
    for metot in ("GET", "POST", "PUT", "PATCH"):
        try:
            asyncio.run(require_unrestricted(SimpleNamespace(method=metot), kisitli))  # type: ignore[arg-type]
        except HTTPException as hata:
            assert hata.status_code == 403
        else:
            raise AssertionError(f"{metot} kısıtlıyı durdurmadı")


def test_u6_rotalari_require_unrestricted_tasir_ve_isaretli_sayilir() -> None:
    """🔴 Ü6 rotası `require_unrestricted` bağımlılığını KAYBEDERSE (kısıtlı kullanıcı yapısal
    işlemi yapabilir) kırmızı. Alt bağımlılık `DisciplineScoped`ı çözdüğü için rota aynı zamanda
    süzülmüş (işaretli) sayılır; imzada `scope` parametresi olmadığından "gövdede kullanılır"
    kuralı bu rotaları kendiliğinden atlar."""
    bulunan: set[Rota] = set()
    isaretli = isaretli_rotalar()
    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        dependant = ctx.dependant or ctx.original_route.dependant
        for yontem in ctx.methods:
            rota = (yontem, ctx.path)
            if rota not in U6_ROTALARI:
                continue
            bulunan.add(rota)
            assert _bagimlilik_agacinda(dependant, require_unrestricted), rota
            assert rota in isaretli, rota
            assert not _isaretli_parametreler(ctx.original_route.endpoint), rota
    assert bulunan == set(U6_ROTALARI), f"bayat/eksik Ü6 rotası: {set(U6_ROTALARI) - bulunan}"


def _isaretli_parametreler(fonksiyon) -> list[str]:  # noqa: ANN001
    """İmzada `DisciplineScoped` (Depends(resolve_discipline_scope)) taşıyan parametre adları."""
    ipuclari = typing.get_type_hints(fonksiyon, include_extras=True)
    adlar = []
    for ad, ipucu in ipuclari.items():
        meta = getattr(ipucu, "__metadata__", ())
        if any(getattr(m, "dependency", None) is resolve_discipline_scope for m in meta):
            adlar.append(ad)
    return adlar


def _govdede_yuklenen_adlar(fonksiyon) -> set[str]:  # noqa: ANN001
    kaynak = textwrap.dedent(inspect.getsource(inspect.unwrap(fonksiyon)))
    govde = ast.parse(kaynak).body[0]
    assert isinstance(govde, ast.FunctionDef | ast.AsyncFunctionDef)
    return {
        d.id
        for ust in govde.body
        for d in ast.walk(ust)
        if isinstance(d, ast.Name) and isinstance(d.ctx, ast.Load)
    }


def test_isaretli_her_rota_kapsam_parametresini_govdede_kullanir() -> None:
    """🔴 "İşaretli ama süzmeyen" rota: `scope: DisciplineScoped` imzada durup gövdede HİÇ
    kullanılmazsa rota bekçiyi geçer ama süzmez (sahte-yeşil). Parametre adı gövdede en az bir
    kez `ast.Name` (Load) olarak geçmeli. Sıfır işaretli rota = bozuk tarama → kırmızı."""
    taranan = 0
    ihlal: list[str] = []
    for ctx in iter_route_contexts(app.routes):
        rota = ctx.original_route
        if not isinstance(rota, APIRoute):
            continue
        adlar = _isaretli_parametreler(rota.endpoint)
        if not adlar:
            continue
        taranan += 1
        kullanilan = _govdede_yuklenen_adlar(rota.endpoint)
        ihlal += [
            f"{rota.endpoint.__module__}.{rota.endpoint.__name__}:{a}"
            for a in adlar
            if a not in kullanilan
        ]
    assert taranan > 0, "işaretli rota bulunamadı — tarama bozuk"
    assert not ihlal, f"DisciplineScoped parametresi gövdede KULLANILMIYOR: {ihlal}"

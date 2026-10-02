"""Kapsam maskesinin KAÇAK UÇLARI — `BaseModel` DÖNMEYEN uçlar.

## Delik neydi

`core/scoped_route.py` maskeyi rota sınıfında TEK NOKTADAN uygular ve bunu
*"unutulacak bir şey yoktur"* diye anlatır. Bu, ucun dönüşü bir `BaseModel`
OLDUĞU sürece doğrudur: sarmalayıcı `isinstance(sonuc, BaseModel)` değilse
sonucu **aynen** geçirir.

`GET /sites/{site_id}/boq/export` bir `Response` (xlsx baytları) döndürüyordu.
Yani `boq = view/limited` olan rol (şantiye şefi, satınalma) ekranda `—` gördüğü
birim fiyatı ve tutarı Excel'den TAM DEĞERİYLE indiriyordu. Kapı aynı kapıydı
(`boq:view`); maske yalnız JSON yolunda vardı.

## Bu dosya NE ölçer — iki katman, ikisi de gerekli

1. **YAPISAL bekçi** (`test_KAPSAMLI_routerda_GOVDELI_maskesiz_uc_IZIN_LISTESINDE`):
   kapsamlı her routerın HER ucunu gezer; `BaseModel` dönmeyen ve gövde taşıyan
   her uç, GEREKÇESİYLE birlikte `_GOVDELI_MASKESIZ_UCLAR`da olmalıdır. Yarın
   biri ikinci bir dosya/`dict` dönen uç eklerse bu test KIRILIR ve yazarını
   *"bu uç maskeden nasıl geçiyor"* sorusunu yanıtlamaya zorlar.

2. **DAVRANIŞ bekçisi** (`test_EXPORT_*`): izin listesindeki gerekçe bir
   İDDİADIR; tek başına bir rubber stamp'tır. Gerçek rolle gerçek dosyayı indirip
   hücrelere BAKAN testler o iddiayı ÖLÇER.

🔴 İzin listesi neden 204 DELETE'leri TAŞIMIYOR: onlar bir LİSTE değil bir
KURAL'la elenir (`_govdesiz`). Elle yazılmış on satırlık bir liste çürür — on
birinci DELETE eklendiğinde test, gerçek bir kusur değil bir bakım borcu yüzünden
kırmızı verirdi ve insanlar listeyi düşünmeden büyütmeyi öğrenirdi.

## Bekçi yalnız DOSYA uçlarını değil, `list`/`dict` dönen uçları da yakalar

`_sarili` `isinstance(sonuc, BaseModel)` der. `response_model=list[X]` olan bir uç
sarmalayıcıya bir **liste** verir ve liste de `BaseModel` DEĞİLDİR — yani böyle
bir uç da maskesiz geçer. Bugün kapsamlı routerlarda böyle bir uç YOK, ama ilk
eklenende bu bekçi kırmızı verir. Delik "dosya indirmeye özel" değildir.

## Taramanın 2026-09-19 ölçümü (tarihsel kayıt, bakılan bir liste DEĞİL)

Kapsamlı 69 rotanın 11'i `BaseModel` döndürmüyordu: 10'u 204 DELETE (gövdesiz →
sızdıracak bir şeyi yok, `_govdesiz` ile elenir), 1'i `GET /sites/{site_id}/boq/
export` idi ve PARA sızdırıyordu. Sayı burada bir iddia değil bir tarihtir —
güncel gerçeği aşağıdaki testler ölçer.
"""

import inspect
from decimal import Decimal
from io import BytesIO

import openpyxl
from fastapi import APIRouter, Response
from fastapi.routing import APIRoute, _IncludedRouter
from pydantic import BaseModel

from app.core.router_registry import ROUTERS
from app.core.scoped_route import kapsam_rotasi, kapsamdan_oku
from tests.modules._boq import _auth, _group, _item, _login_with_access, _site

#: Gövde TAŞIYAN ve `BaseModel` DÖNMEYEN uçların izin listesi.
#: Anahtar `(metot, yol)`, değer **GEREKÇE**: bu uç maskeden nasıl geçiyor?
#:
#: 🔴 BURAYA SATIR EKLEMEK BİR ONAY DEĞİL, BİR İDDİADIR. "Sonra bakarız" diye
#: eklenen bir satır, bekçiyi bir lastik damgaya çevirir ve tam da kapatmak için
#: yazıldığı deliği açık tutar. Bir uç ya maskeden geçer, ya 403 verir, ya da
#: gövdesinin ÖLÇÜLMÜŞ olarak veri taşımadığı burada yazılır.
_GOVDELI_MASKESIZ_UCLAR: dict[tuple[str, str], str] = {
    ("GET", "/sites/{site_id}/boq/export"): (
        "BOQ xlsx indirme. Uç maskeyi ELLE uygular (`kapsamla_maskele(boq, 'boq')`), "
        "çünkü rota sarmalayıcısı `Response` gövdesinin İÇİNE bakamaz. Dosya böylece "
        "ekranla BİREBİR aynı değerleri taşır. İDDİANIN ÖLÇÜMÜ: aşağıdaki "
        "`test_EXPORT_*` davranış bekçileri."
    ),
    ("GET", "/projects/{project_id}/units/export.xlsx"): (
        "Ünite paylaşım tablosunun xlsx indirmesi — `boq/export` kaçağının BİREBİR "
        "İKİZİ ve aynı çözümle kapatıldı: uç maskeyi ELLE uygular "
        "(`kapsamla_maskele(units, 'projects')`), çünkü sarmalayıcı `Response` "
        "gövdesinin içine bakamaz. Kapsam anahtarı `projects`tir: `units` KENDİ izin "
        "modülünü açmaz, spec §8 gereği `projects` seviyelerini kullanır. "
        "🔴 BU KAYIT BİR DENETİMİN ÜRÜNÜDÜR: uç maskelendiği hâlde bu listeye "
        "yazılmamıştı ve bekçi onu ilk koşuşunda yakaladı — yani liste bir onay "
        "damgası değil, gerçekten okunan bir iddia kümesidir. "
        "İDDİANIN ÖLÇÜMÜ: `tests/modules/units/` altındaki export davranış bekçileri."
    ),
    ("GET", "/offers/{offer_id}/revisions/{rev_no}/export"): (
        "TKL-B5.2 teklif revizyonu xlsx indirme (isveren / ic). Uc maskeyi ELLE uygular "
        "(`kapsamla_maskele(revision, 'contracts')`), cunku sarmalayici `Response` govdesinin "
        "icine bakamaz; zarf kitaba girmeden ONCE maskelenir, `limited` rolde para hucreleri "
        "BOS kalir (iki gorunumde de). Isveren sayfasi ayrica `internal` alt nesnesini HIC "
        "okumaz. IDDIANIN OLCUMU: `tests/modules/offers/test_offer_export.py` (sizinti "
        "bekcisi + `limited` testi)."
    ),
    ("GET", "/catalog/items/export"): (
        "TKL-B5.2 fiyatli katalog xlsx indirme. Uc maskeyi ELLE uygular "
        "(`kapsamla_maskele(WorkItemListResponse, 'contracts')`): `limited` rolde Referans "
        "Fiyat / Fiyat Guncelleme / Son Fiyat / Kaynak / Belge / Tarih BOS kalir; disiplin "
        "kapsami `list_items`e `DisciplineScoped` ile gecer. IDDIANIN OLCUMU: "
        "`tests/modules/catalog/test_catalog_export.py`."
    ),
    ("GET", "/projects/{project_id}/units/import/template"): (
        "Ünite içe aktarma ŞABLONU. Maskelenmez çünkü MASKELENECEK VERİ YOKTUR: "
        "`units/template.py::build_template_workbook` proje verisi ALMAZ, yalnız "
        "`importer.COLUMNS` başlıklarından tek satırlık boş bir kitap üretir "
        "(ölçüldü 2026-09-19; veri satırı koymama kararı spec §6.7'de gerekçeli). "
        "Uçtaki `project` YALNIZCA dosya adı (`project.code`) ve görünürlük kapısı "
        "için okunur — kod KİMLİK kovasındadır, her kapsamda görünür."
    ),
    ("GET", "/section-types"): (
        "BLF-B1 sirket geneli bolum tipi listesi (`list[SectionTypeRead]`). Maskelenmez "
        "cunku MASKELENECEK VERI YOKTUR: yanit yalniz `{id, name}` tasir (UUID + tip adi), "
        "hicbir para/alan/metraj/ilerleme alani yoktur ve sirket geneli referans "
        "verisidir (proje/santiye kapsamina bagli degil). Gerekce OLCULUR: "
        "`tests/modules/sites/test_blf_b1_section_types.py::"
        "test_get_lists_types_in_sort_order_with_id_and_name_only` her satirin anahtar "
        "kumesini `{id, name}`a cakar; semaya para alani eklenirse "
        "`tests/core/test_para_alani_siniflandirmasi.py` kirmizi verir."
    ),
}


def _rotalar(rotalar):
    """Rota ağacını özyinelemeli gezer.

    🔴 Düz gezinti bu depoda YANLIŞ sayar: routerlar birbirini `include_router`
    ile sarar (`_IncludedRouter` tembel bir ara katmandır) ve iç içe rotalar
    yalnız özyinelemeyle görünür — `app/modules/ai/readplane.py` aynı olguyu
    belgeler.
    """
    for rota in rotalar:
        if isinstance(rota, _IncludedRouter):
            yield from _rotalar(rota.original_router.routes)
        else:
            yield rota


def _kapsamli(rota: APIRoute) -> bool:
    """Rota `kapsam_rotasi(...)` fabrikasından mı çıktı?

    🔴 Modül ADIYLA değil SINIFLA seçilir: "kısıtlı modül = python paketi"
    varsayımı yanlıştır (aynı izin anahtarına bağlı başka paketteki routerlar
    var) ve elle yazılmış bir dosya listesi sessizce eksik kalırdı.
    """
    return type(rota).__name__ == "_KapsamRotasi"


def _basemodel_doner(rota: APIRoute) -> bool:
    model = rota.response_model
    return isinstance(model, type) and issubclass(model, BaseModel)


def _govdesiz(rota: APIRoute) -> bool:
    """Uç YAPISAL olarak gövdesiz mi (204 + dönüşü `None`)?

    🔴 Bu bir KURAL'dır, bir liste değil: 204 gövdesiz olduğunu HTTP'nin kendisi
    söyler ve dönüş açıklaması `None` olduğu sürece uç sızdıracak bir şey
    üretemez. İkinci koşul zorunludur — yalnız duruma bakan bir eleme, 204 ilan
    edip gövde döndüren bir ucu sessizce affederdi.
    """
    if rota.status_code != 204:
        return False
    return inspect.signature(rota.endpoint).return_annotation in (None, "None")


def _kapsamli_rotalar() -> list[APIRoute]:
    bulunan: list[APIRoute] = []
    for router in ROUTERS:
        for rota in _rotalar(router.routes):
            if isinstance(rota, APIRoute) and _kapsamli(rota):
                bulunan.append(rota)
    return bulunan


def _kimlik(rota: APIRoute) -> set[tuple[str, str]]:
    return {(metot, rota.path) for metot in rota.methods if metot != "HEAD"}


# --------------------------------------------------------------------------- #
# 1) YAPISAL BEKÇİ
# --------------------------------------------------------------------------- #


def test_KAPSAMLI_routerda_GOVDELI_maskesiz_uc_IZIN_LISTESINDE() -> None:
    """Maskeden kendiliğinden geçmeyen her uç AÇIKÇA gerekçelendirilmiş olmalı."""
    kacak: set[tuple[str, str]] = set()
    for rota in _kapsamli_rotalar():
        if _basemodel_doner(rota) or _govdesiz(rota):
            continue
        kacak |= _kimlik(rota) - set(_GOVDELI_MASKESIZ_UCLAR)

    assert not kacak, (
        "Kapsamlı routerda `BaseModel` DÖNMEYEN ve gövde taşıyan bir uç var; rota "
        "sarmalayıcısı onu MASKELEMEDEN geçirir. Bu uç ekranda gizlenen parayı/metrajı "
        "sızdırıyor olabilir. Ya ucu maskeden geçir (`kapsamla_maskele`), ya kapsam "
        f"kısıtlıysa 403 ver — sonra `_GOVDELI_MASKESIZ_UCLAR`a GEREKÇESİYLE ekle: {kacak}"
    )


def test_IZIN_LISTESI_BAYAT_satir_TASIMAZ() -> None:
    """🔴 Silinen/değişen bir uç listede kalırsa liste bir daha kimseyi durdurmaz:
    okuyan kişi satırların gerçeği anlattığına güvenemez olur."""
    gercek = {kimlik for rota in _kapsamli_rotalar() for kimlik in _kimlik(rota)}
    bayat = set(_GOVDELI_MASKESIZ_UCLAR) - gercek

    assert not bayat, f"İzin listesinde artık var olmayan uç var: {bayat}"


def test_BEKCI_maskesiz_ucu_GERCEKTEN_yakalar() -> None:
    """🔴 POZİTİF KONTROL — bekçinin ELEME kuralları (`_basemodel_doner`,
    `_govdesiz`) fazla cömert olsaydı üstteki test her şeyi affeder ve sessizce
    ölürdü. Burada üç hâl de sentetik bir routerda ölçülür."""
    router = APIRouter(route_class=kapsam_rotasi("boq", kapsamdan_oku))

    class _Zarf(BaseModel):
        ad: str

    @router.get("/sentetik/dosya", response_class=Response)
    async def _dosya() -> Response:
        return Response(content=b"x")

    @router.get("/sentetik/zarf", response_model=_Zarf)
    async def _zarf() -> _Zarf:
        return _Zarf(ad="a")

    @router.delete("/sentetik/sil", status_code=204)
    async def _sil() -> None:
        return None

    yakalanan = {
        rota.path for rota in router.routes if not _basemodel_doner(rota) and not _govdesiz(rota)
    }

    assert yakalanan == {"/sentetik/dosya"}, (
        "Bekçi ya dosya dönen ucu KAÇIRDI ya da zarf/204 ucunu yanlışlıkla suçladı"
    )


# --------------------------------------------------------------------------- #
# 2) DAVRANIŞ BEKÇİSİ — gerçek rol, gerçek dosya
# --------------------------------------------------------------------------- #

_MIKTAR = Decimal("1240.000")
_BIRIM_FIYAT = Decimal("280.00")
#: `_MIKTAR * _BIRIM_FIYAT`. Elle yazılmıştır ki test, ölçtüğü formülü yeniden
#: hesaplayıp kendi kendini onaylamasın.
_TUTAR_METNI = "347200.00"

#: `app/modules/boq/export.py::COLUMN_HEADERS` sırası.
_SUTUN = {"poz": 1, "tarif": 2, "birim": 3, "miktar": 4, "birim_fiyat": 5, "tutar": 6}


async def _export_sayfasi(client, db_session, user_factory, project_factory, rol: str, eposta: str):
    """Gerçek rolle gerçek xlsx'i indirir ve sayfayı geri okur."""
    project = await project_factory(f"KACAK-{rol}")
    site = await _site(db_session, project, code=f"A-{rol[:6].upper()}")
    group = await _group(db_session, site)
    await _item(db_session, site, group, quantity=_MIKTAR, unit_price=_BIRIM_FIYAT)
    token = await _login_with_access(client, db_session, user_factory, rol, eposta)

    resp = await client.get(f"/sites/{site.id}/boq/export", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return site, token, openpyxl.load_workbook(BytesIO(resp.content)).active


def _hucreler(sayfa) -> list[str]:
    """Sayfadaki BOŞ OLMAYAN tüm hücrelerin metni — "hiçbir yerde yok" iddiası
    tek bir hücreye değil DOSYANIN TAMAMINA bakmalıdır."""
    return [str(h.value) for satir in sayfa.iter_rows() for h in satir if h.value is not None]


def _kalem_satiri(sayfa) -> int:
    """Başlık(1) + grup başlığı(2) + kalem(3). Sabit değil, aranarak bulunur."""
    for satir in range(1, sayfa.max_row + 1):
        if sayfa.cell(row=satir, column=_SUTUN["poz"]).value == "01.001":
            return satir
    raise AssertionError(f"Kalem satırı bulunamadı: {_hucreler(sayfa)}")


async def test_EXPORT_ADMIN_rolunde_TUM_degerler_dosyada(
    client, db_session, user_factory, project_factory
):
    """🔴 POZİTİF KONTROL — maske "herkesten gizle" hâline gelirse ya da export
    tamamen 403'e kapanırsa burası kırmızı olur. Aşağıdaki iki test tek başına
    `build_boq_workbook` boş dosya üretse de yeşil kalırdı."""
    _site_, _token, sayfa = await _export_sayfasi(
        client, db_session, user_factory, project_factory, "system_admin", "adm@kacak.co"
    )
    satir = _kalem_satiri(sayfa)

    assert sayfa.cell(row=satir, column=_SUTUN["miktar"]).value == "1240.000"
    assert sayfa.cell(row=satir, column=_SUTUN["birim_fiyat"]).value == "280.00"
    assert sayfa.cell(row=satir, column=_SUTUN["tutar"]).value == _TUTAR_METNI
    assert _TUTAR_METNI in _hucreler(sayfa), "GENEL TOPLAM satırı da dolu olmalı"


async def test_EXPORT_LIMITED_rolde_BIRIM_FIYAT_ve_TUTAR_dosyaya_YAZILMAZ(
    client, db_session, user_factory, project_factory
):
    """`site_chief` → `boq = view/limited`. Ekranda `—` gördüğü para, indirdiği
    dosyada da OLMAMALIDIR — aynı kapı (`boq:view`) iki farklı cevap veremez."""
    _site_, _token, sayfa = await _export_sayfasi(
        client, db_session, user_factory, project_factory, "site_chief", "sc@kacak.co"
    )
    satir = _kalem_satiri(sayfa)
    hucreler = _hucreler(sayfa)

    assert sayfa.cell(row=satir, column=_SUTUN["birim_fiyat"]).value is None, "BİRİM FİYAT SIZDI"
    assert sayfa.cell(row=satir, column=_SUTUN["tutar"]).value is None, "TÜREV TUTAR SIZDI"
    assert "280.00" not in hucreler, f"birim fiyat dosyanın BAŞKA bir hücresinde: {hucreler}"
    assert _TUTAR_METNI not in hucreler, f"GENEL TOPLAM sızdı: {hucreler}"
    # 🔴 "None" METNİ de aranır: `str(None)` yazan bir uygulama testin `is None`
    # iddiasını geçemez ama hücreye çöp basardı — ve kullanıcı onu veri sanardı.
    assert "None" not in hucreler, f"maskeli hücreye 'None' metni yazılmış: {hucreler}"
    # KİMLİK ve METRAJ DURUR: dosya şantiye şefi için kullanılabilir kalmalıdır;
    # 403 vermek onu işini yapamaz hâle getirirdi.
    assert sayfa.cell(row=satir, column=_SUTUN["poz"]).value == "01.001"
    assert sayfa.cell(row=satir, column=_SUTUN["miktar"]).value == "1240.000"


async def test_EXPORT_FINANCE_rolde_METRAJ_dosyaya_YAZILMAZ(
    client, db_session, user_factory, project_factory
):
    """`accounting` → `boq = view/finance`. `limited`in AYNASIDIR: tek yönlü bir
    test "her şeyi gizle" hâlini yakalayamazdı."""
    _site_, _token, sayfa = await _export_sayfasi(
        client, db_session, user_factory, project_factory, "accounting", "acc@kacak.co"
    )
    satir = _kalem_satiri(sayfa)
    hucreler = _hucreler(sayfa)

    assert sayfa.cell(row=satir, column=_SUTUN["miktar"]).value is None, "METRAJ SIZDI"
    assert "1240.000" not in hucreler, f"metraj başka bir hücrede: {hucreler}"
    assert "None" not in hucreler, f"maskeli hücreye 'None' metni yazılmış: {hucreler}"
    assert sayfa.cell(row=satir, column=_SUTUN["birim_fiyat"]).value == "280.00", (
        "PARA yanlışlıkla gizlendi"
    )
    assert sayfa.cell(row=satir, column=_SUTUN["poz"]).value == "01.001", "KİMLİK gizlendi"


async def test_EXPORT_dosyasi_EKRANLA_ayni_degerleri_tasir(
    client, db_session, user_factory, project_factory
):
    """🔴 ASIL INVARIANT: Excel ile ekran ASLA ayrışmaz.

    Sabit beklenen değerlere bakan testler, maskenin bir gün şemada değişmesi
    hâlinde (örn. `grand_total`ın `finance` davranışı) ikisinden yalnız birini
    yakalardı. Bu test iki yolu BİRBİRİNE karşı ölçer, sabite karşı değil.
    """
    site, token, sayfa = await _export_sayfasi(
        client, db_session, user_factory, project_factory, "site_chief", "sc2@kacak.co"
    )
    ekran = (await client.get(f"/sites/{site.id}/boq", headers=_auth(token))).json()
    kalem = ekran["groups"][0]["items"][0]
    satir = _kalem_satiri(sayfa)

    for alan, sutun in (
        ("quantity", "miktar"),
        ("unit_price", "birim_fiyat"),
        ("amount", "tutar"),
    ):
        assert sayfa.cell(row=satir, column=_SUTUN[sutun]).value == kalem[alan], (
            f"`{alan}` dosyada ekrandan FARKLI: "
            f"{sayfa.cell(row=satir, column=_SUTUN[sutun]).value!r} != {kalem[alan]!r}"
        )

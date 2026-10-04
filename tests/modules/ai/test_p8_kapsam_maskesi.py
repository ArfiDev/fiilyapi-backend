"""P8 — FİİL AI okuma araçları KAPSAM MASKESİYLE birlikte çalışır mı?

2026-09-19'da alan düzeyi kapsam maskesi (`core/field_scope` + `core/
scoped_route`) altı modüle bağlandı: `limited` PARAYI, `finance` OPERASYONEL
DETAYI gizler. Bu dosya AI hattının o maskeyle **iki AYRI** ilişkisini ölçer:

| # | Soru | Bekçi |
|---|---|---|
| 1 | AI araçları maskeden GEÇİYOR MU, yoksa tutar SIZIYOR MU? | `test_SIZINTI_*` |
| 2 | Maskeli veri geldiğinde araç ÇALIŞIYOR MU? | `test_MASKELI_*` |

🔴 **İKİSİ BİRDEN GEREKLİ VE BİRİ ÖTEKİNİ İKAME ETMEZ.** Sızıntıyı ölçen bir
test tek başına "araç `ToolError` döndü" hâlinde de YEŞİL kalır (`ToolError`
gövdesinde tutar yoktur — sızıntı yoktur çünkü VERİ yoktur). Bu dosyanın ilk
hâli tam olarak öyleydi: dört araç patlarken sızıntı testi yeşildi. Çalışmayı
ölçen bir test ise maskenin HİÇ koşmadığı, yani tutarın açıktan geldiği hâlde
de yeşil kalırdı. Yalnız ikisi birlikte *"maske koşuyor VE araç yaşıyor"* der.

## Ölçülen olgu: AI araçları SERVİSİ değil UCU sarar, maske de onlarda KOŞAR

`ai/exposure.py` ve `ai/registry.py` docstring'leri *"hiçbir süzgeç
`permission.scope` okumaz, `Scope` dekoratiftir"* diyordu. **BU ARTIK
BAYATTIR** ve bayatlığı bu dosyada ölçülür: `ReadOnlyTransport` kullanıcının
KENDİ bearer'ıyla gerçek ucu GET eder, `build_read_plane` ana uygulamanın
**orijinal `APIRoute` nesnelerini** taşır — yani `kapsam_rotasi` sarmalayıcısı
ve router düzeyindeki `kapsam_kapisi` köprüsü AI hattına da GELİR.

Bu yüzden testler gerçek rol · gerçek kapsam · gerçek uç üzerinden koşar.
Handler'a elle maskeli bir sözlük vermek bu taşınmayı ÖLÇMEZDİ: köprü bir
`ContextVar`dır ve süreç-içi ASGI çağrısında kurulup kurulmadığı bir
VARSAYIMDIR.

## Araç listesi ELLE YAZILMAZ — KODDAN ÖLÇÜLÜR

`_maskeye_bagli_araclar()` rota tablosunu gezer ve ucu `kapsam_kapisi`
köprüsünü taşıyan araçları bulur (`tests/core/test_kapsam_baglantisi.py`in
kapanış-kimliği tekniği). Elle yazılmış bir liste, yedinci modül kısıtlandığı
gün SESSİZCE eksik kalırdı — yeni araç testlere hiç girmez ve kimse kırmızı
görmezdi. `_ARGUMANLAR` sözlüğünün bu kümeyle **küme eşitliği** ayrıca
çakılır: yeni bir maskeli araç argümansız kalırsa test SKIP değil KIRMIZI olur.

## Kapsam DEĞİŞKENİ testte kurulur, seed'den OKUNMAZ

`tests/modules/test_kapsam_capraz_sizinti.py` emsali: deneyin tek değişkeni
kapsamdır, seviye sabittir. Seed'e bağlansaydı matris bir gün değiştiğinde test
sessizce anlamsızlaşır — kırmızı vermeden.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.core.access import Scope
from app.main import app
from app.modules.ai import audit as ai_audit
from app.modules.ai.registry import ToolRegistry, ToolSpec
from app.modules.ai.result import Ok, ToolError, Truncated
from app.modules.ai.tools.catalog import READ_TOOLS
from app.modules.roles.models import Module, Role, RolePermission
from app.modules.users.models import ProjectMember

# `asyncio_mode = "auto"` (pyproject) — ayrıca `pytestmark` YAZILMAZ: yazılsaydı bu
# dosyadaki SENKRON keşif testi "asyncio işaretli ama async değil" uyarısı verirdi.

#: Testin rolü. `patron` 22 aracın 22 kapısını da taşıyan TEK roldür
#: (`test_ai0b_kapsam.py` ölçtü) ve `projects=full`dur — `admin` DEĞİL, yani
#: `visible_projects` süzgeci gerçekten koşar.
_ROL = "patron"

#: 🔴 HEPSİ BİRBİRİNDEN FARKLI: eşit sayılar seçilseydi "yanlış alanı okuyan"
#: bir assert de yeşil kalırdı. Metin hâlleri sızıntı taramasında aranır.
_BUTCE = Decimal("77100000.00")
_ILERLEME = Decimal("13.50")
_SOZLESME_BEDELI = Decimal("64300000.00")
_POZ_MIKTAR = Decimal("21500.000")
_POZ_BIRIM_FIYAT = Decimal("312.00")
#: 21.500 × 312 = 6.708.000 — BOQ `grand_total`ı.
_POZ_TUTAR = "6708000.00"

#: `limited` kapsamda AI zarfının HİÇBİRİNDE görünmemesi gereken PARA izleri.
#: 🔴 Noktasız/ondalıksız yazılır: zarf `model_dump(mode="json")` ile
#: serileşir ve `Decimal` metne döner, biçim değişse de rakam dizisi kalır.
_PARA_IZLERI = ("77100000", "64300000", "312.00", "6708000")
#: `finance` kapsamda görünmemesi gereken OPERASYONEL izler.
_OPERASYONEL_IZLER = ("21500.000", "13.50")


@pytest.fixture(autouse=True)
def _denetim_sussun(monkeypatch):
    """Denetim yazımı B6/B6b'nin işidir; burada ölçülen şey MASKE.

    (Gerçek yazım ayrı session açar ve testin savepoint'i dışına düşerek işçi
    veritabanına sızardı — `test_ai0b_kapsam.py` ile aynı gerekçe.)
    """

    async def _sahte(**kwargs):
        return None

    monkeypatch.setattr(ai_audit, "record_tool_call", _sahte)


# --------------------------------------------------------------------------- #
# Maskeye BAĞLI araçların KODDAN çıkarılması
# --------------------------------------------------------------------------- #

#: `kapsam_kapisi` fabrikasının ürettiği çözücünün kimliği. `kapsam_bagimligi_kur`
#: `functools.wraps(cozucu)` kullandığı için sarmalayıcı `__qualname__`i
#: çözücüden devralır; `__wrapped__` üzerinden okumak ikisini birden doğrular.
_KAPSAM_KAPANISI = "kapsam_kapisi.<locals>._cozucu"


def _api_rotalari(rotalar) -> list[APIRoute]:
    cikti: list[APIRoute] = []
    for rota in rotalar:
        if isinstance(rota, APIRoute):
            cikti.append(rota)
        elif type(rota).__name__ == "_IncludedRouter":
            cikti.extend(_api_rotalari(rota.original_router.routes))
        elif hasattr(rota, "routes"):
            cikti.extend(_api_rotalari(rota.routes))
    return cikti


def _kopru_anahtari(bagimlilik) -> str | None:
    """Bu bağımlılık `kapsam_kapisi(...)` mı? Öyleyse köprünün yazdığı anahtar."""
    fn = getattr(bagimlilik, "dependency", None)
    cozucu = getattr(fn, "__wrapped__", None)
    if cozucu is None or getattr(cozucu, "__qualname__", "") != _KAPSAM_KAPANISI:
        return None
    serbest = dict(
        # `strict=True` — gerekçe `test_kapsam_baglantisi._kapanis` ile aynı.
        zip(
            cozucu.__code__.co_freevars,
            (c.cell_contents for c in cozucu.__closure__ or ()),
            strict=True,
        )
    )
    return serbest.get("module_key")


def _maskeli_yollar() -> dict[str, str]:
    """`yol → kapsam modülü` — köprüsü OLAN her GET ucu."""
    cikti: dict[str, str] = {}
    for rota in _api_rotalari(app.routes):
        if "GET" not in (rota.methods or set()):
            continue
        for bag in rota.dependencies:
            anahtar = _kopru_anahtari(bag)
            if anahtar is not None:
                cikti[rota.path] = anahtar
    return cikti


def _maskeye_bagli_araclar() -> list[ToolSpec]:
    """Ucu kapsam köprüsüne bağlı okuma araçları. ELLE YAZILMIŞ LİSTE DEĞİL."""
    yollar = _maskeli_yollar()
    return [s for s in READ_TOOLS if any(u in yollar for u in s.ucler)]


MASKELI_ARACLAR = _maskeye_bagli_araclar()

#: Araç → argüman üreticisi. Küme eşitliği aşağıda çakılır.
_ARGUMANLAR = {
    "projeleri_listele": lambda k: {},
    "proje_detayi": lambda k: {"project_id": str(k["proje_id"])},
    "arsa_payi": lambda k: {"project_id": str(k["proje_id"])},
    "santiyeleri_listele": lambda k: {},
    "santiye_detayi": lambda k: {"site_id": str(k["santiye_id"])},
    "is_kalemleri": lambda k: {"site_id": str(k["santiye_id"])},
    "sozlesmeler": lambda k: {"contract_type": "employer"},
    "taseronlar": lambda k: {},
    "gosterge_ozeti": lambda k: {},
}


def test_MASKELI_ARAC_KUMESI_ARGUMAN_HARITASIYLA_ESITTIR() -> None:
    """🔴 Parametrize bir test EKSİK girdi için kırmızı OLMAZ, sadece az koşar.

    Yedinci modül kısıtlandığı gün yeni bir araç bu kümeye girer; argümanı
    yazılmazsa aşağıdaki bekçiler onu sessizce atlardı. Küme eşitliği o
    sessizliği kapatır.
    """
    assert MASKELI_ARACLAR, "maskeye bağlı HİÇ araç bulunamadı — keşif kırık"
    assert {s.ad for s in MASKELI_ARACLAR} == set(_ARGUMANLAR)


# --------------------------------------------------------------------------- #
# Kurulum
# --------------------------------------------------------------------------- #


@pytest.fixture
async def maske_kurulumu(seeded_db, user_factory, project_factory):
    """Bütçeli proje + pozlu şantiye + kat karşılığı + sözleşme + taşeron.

    🔴 **TOHUMLAR POZİTİF KONTROL İÇİNDİR.** Tohumsuz bir bekçi "araç patlamadı"
    derken aslında BOŞ KÜME ölçerdi: boş listede kalem şeması hiç kurulmaz,
    dolayısıyla maskeli bir `Decimal` asla doğrulanmazdı. Her tohum bir aracın
    "içeriden GÖRÜNÜR" yarısını mümkün kılar ve o yarı
    `test_POZITIF_KONTROL_all_kapsaminda_*`ta ayrıca çakılır.
    """
    from app.modules.boq.models import BoqGroup, BoqItem
    from app.modules.contracts.models import Subcontractor
    from app.modules.projects.models import ProjectContract, ProjectLandShare
    from app.modules.sites.models import Site, SiteStatus

    proje = await project_factory(
        code="P8-MSK",
        name="Maske Projesi",
        budget=str(_BUTCE),
        progress_pct=str(_ILERLEME),
    )
    santiye = Site(
        project_id=proje.id,
        code="P8-MSK-S1",
        name="Maske Şantiyesi",
        status=SiteStatus.active,
        start_date=date(2026, 1, 1),
    )
    seeded_db.add(santiye)
    await seeded_db.flush()

    grup = BoqGroup(site_id=santiye.id, name="TOPRAK İŞLERİ")
    seeded_db.add(grup)
    await seeded_db.flush()
    seeded_db.add_all(
        [
            BoqItem(
                site_id=santiye.id,
                group_id=grup.id,
                code="15.001",
                description="Kazı",
                unit="m³",
                quantity=_POZ_MIKTAR,
                unit_price=_POZ_BIRIM_FIYAT,
            ),
            ProjectLandShare(
                project_id=proje.id,
                landowner_name="Arsa Sahibi",
                our_share_pct=Decimal("50.00"),
                owner_share_pct=Decimal("50.00"),
            ),
            ProjectContract(
                project_id=proje.id,
                contract_no="P8-SZL-1",
                amount=_SOZLESME_BEDELI,
                advance_pct=Decimal("0.00"),
                retainage_pct=Decimal("0.00"),
                vat_pct=Decimal("20.00"),
            ),
            Subcontractor(name="Maske Taşeronu"),
        ]
    )

    kullanici = await user_factory("p8maske@fiil.example.com", "Sifre1234!", _ROL)
    seeded_db.add(
        ProjectMember(user_id=kullanici.id, project_id=proje.id, role_id=kullanici.role_id)
    )
    await seeded_db.flush()

    # 🔴 KİMLİK HARİTASINDAN ÇIKAR: okuma düzlemi AYNI session'ı kullanır ve
    # `get_current_user` `joinedload(User.role)` ister; nesne haritada ROLSÜZ
    # dururken `options` SESSİZCE yok sayılır ve `User.role` (`lazy="raise"`)
    # patlar. Üretimde her istek kendi session'ını alır — testin kurgusunun
    # bedelidir, ürün kusuru DEĞİL (`test_ai0b_kapsam.py` ile aynı not).
    seeded_db.expunge(kullanici)

    return {"user": kullanici, "proje_id": proje.id, "santiye_id": santiye.id}


async def _kapsam(seeded_db, kapsam: Scope, *, moduller: set[str] | None = None) -> None:
    """Rolün İLGİLİ modüllerindeki KAPSAMINI değiştirir — SEVİYEYE DOKUNMAZ.

    🔴 Seviye sabit bırakılır: deneyin tek değişkeni kapsamdır. Seviyeyi de
    yazan bir yardımcı, bir gün kapsam maskesi bozulduğunda testi seviye
    üzerinden yeşil tutabilirdi.
    """
    hedef = moduller if moduller is not None else set(_maskeli_yollar().values())
    rol_id = (await seeded_db.execute(select(Role.id).where(Role.key == _ROL))).scalar_one()
    for modul in hedef:
        modul_id = (
            await seeded_db.execute(select(Module.id).where(Module.key == modul))
        ).scalar_one()
        izin = (
            await seeded_db.execute(
                select(RolePermission).where(
                    RolePermission.role_id == rol_id, RolePermission.module_id == modul_id
                )
            )
        ).scalar_one()
        izin.scope = kapsam
    await seeded_db.flush()


async def _cagir(arac_adi, kurulum, transport_factory, actor_factory):
    kayit = ToolRegistry(READ_TOOLS)
    return await kayit.invoke(
        arac_adi=arac_adi,
        argumanlar=_ARGUMANLAR[arac_adi](kurulum),
        actor=await actor_factory(kurulum["user"]),
        transport=transport_factory(kurulum["user"]),
    )


def _metin(sonuc) -> str:
    return json.dumps(sonuc.govde(), ensure_ascii=False)


# ########################################################################### #
# ① ÇALIŞABİLİRLİK — maskeli veri geldiğinde araç YAŞIYOR MU?
# ########################################################################### #


@pytest.mark.parametrize("kapsam", [Scope.limited, Scope.finance])
@pytest.mark.parametrize("spec", MASKELI_ARACLAR, ids=lambda s: s.ad)
async def test_MASKELI_arac_UST_KAYNAK_HATASI_VERMEZ(
    spec, kapsam, seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """Maskeli bir rol AI'a soru sorduğunda araç ÇALIŞMALIDIR.

    🔴 `ust_kaynak_hatasi` burada "uç bozuk" DEMEK DEĞİLDİR: huninin son dalı
    (`registry.py`, 6a) handler İÇİNDEKİ her istisnayı bu koda düşürür. Maskeli
    bir alan `None` gelip daraltma şeması onu `Decimal` sandığında doğan
    `ValidationError` da buraya düşer ve kullanıcı "sistem hatası" görür —
    oysa ürünün kararı o alanı GİZLEMEKTİ, aracı ÖLDÜRMEK değil.
    """
    await _kapsam(seeded_db, kapsam)
    sonuc = await _cagir(spec.ad, maske_kurulumu, transport_factory, actor_factory)
    assert not (isinstance(sonuc, ToolError) and sonuc.kod == "ust_kaynak_hatasi"), (
        f"{spec.ad} aracı {kapsam.value} kapsamında PATLADI"
    )


@pytest.mark.parametrize("spec", MASKELI_ARACLAR, ids=lambda s: s.ad)
async def test_POZITIF_KONTROL_all_kapsaminda_arac_VERI_DONDURUR(
    spec, seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """K-İKİZ: tohumlar çürürse yukarıdaki bekçi BOŞ KÜME ölçerdi ve yeşil kalırdı.

    Boş bir listede kalem şeması hiç kurulmaz — maskeli bir `Decimal` asla
    doğrulanmaz. Bu bekçi her aracın gerçekten satır/kart döndürdüğünü çakar.
    """
    await _kapsam(seeded_db, Scope.all)
    sonuc = await _cagir(spec.ad, maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(sonuc, Ok | Truncated), f"{spec.ad} → {type(sonuc).__name__}"
    assert sonuc.row_count >= 1, f"{spec.ad} BOŞ döndü — tohum çürümüş"


# ########################################################################### #
# ② SIZINTI — AI araçları maskeden GEÇİYOR MU?
# ########################################################################### #


@pytest.mark.parametrize("spec", MASKELI_ARACLAR, ids=lambda s: s.ad)
async def test_SIZINTI_limited_rolde_PARA_hicbir_AI_zarfina_GIRMEZ(
    spec, seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """🔴 Asıl soru: araç UCU mu yoksa SERVİSİ mi sarıyor?

    Servisi sarsaydı `kapsam_rotasi` hiç devreye girmez ve kullanıcının ekranda
    `—` gördüğü tutar AI cevabından çıkardı. Tek bir alana değil **gövdenin
    tamamına** bakılır: daraltma katmanı alanı yeniden adlandırabilir
    (`value_total` → `toplam_deger`) ve ada bağlı bir assert onu kaçırırdı.
    """
    await _kapsam(seeded_db, Scope.limited)
    sonuc = await _cagir(spec.ad, maske_kurulumu, transport_factory, actor_factory)
    govde = _metin(sonuc)
    for iz in _PARA_IZLERI:
        assert iz not in govde, f"{spec.ad}: limited rolde PARA sızdı ({iz})"


@pytest.mark.parametrize("spec", MASKELI_ARACLAR, ids=lambda s: s.ad)
async def test_SIZINTI_finance_rolde_OPERASYONEL_hicbir_AI_zarfina_GIRMEZ(
    spec, seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    await _kapsam(seeded_db, Scope.finance)
    sonuc = await _cagir(spec.ad, maske_kurulumu, transport_factory, actor_factory)
    govde = _metin(sonuc)
    for iz in _OPERASYONEL_IZLER:
        assert iz not in govde, f"{spec.ad}: finance rolde OPERASYONEL veri sızdı ({iz})"


async def test_POZITIF_KONTROL_all_kapsaminda_PARA_da_OPERASYONEL_de_GORUNUR(
    seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """🔴 İKİ SIZINTI BEKÇİSİNİN İKİZİ.

    Onlar "gövdede şu rakam YOK" der; her zaman `ToolError` dönen bozuk bir
    araç da o iddiayı geçerdi. Bu test aynı rakamların kısıtsız kapsamda
    GERÇEKTEN göründüğünü çakar — yani bekçilerin aradığı iz üretilebiliyor.
    """
    await _kapsam(seeded_db, Scope.all)
    proje = await _cagir("proje_detayi", maske_kurulumu, transport_factory, actor_factory)
    boq = await _cagir("is_kalemleri", maske_kurulumu, transport_factory, actor_factory)
    sozlesme = await _cagir("sozlesmeler", maske_kurulumu, transport_factory, actor_factory)

    assert proje.data["budget"] == "77100000.00"
    assert proje.data["progress_pct"] == "13.50"
    kalem = boq.data["gruplar"][0]["items"][0]
    assert kalem["unit_price"] == "312.00"
    assert kalem["quantity"] == "21500.000"
    assert boq.data["grand_total"] == _POZ_TUTAR
    assert sozlesme.data["items"][0]["amount"] == "64300000.00"


# ########################################################################### #
# ③ KOVA AYRIMI — maske DOĞRU kovayı gizliyor mu?
# ########################################################################### #


async def test_MASKELI_proje_detayi_PARAYI_gizler_OPERASYONELI_BIRAKIR(
    seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """🔴 `0` DEĞİL `None`: sıfır sessizce YANLIŞ bir sayıdır ve gizlemekten kötüdür.

    Ve `progress_pct` `limited`ta GÖRÜNÜR kalmalı — "her para alanını gizle"
    ile "her şeyi gizle" ayrı şeylerdir; ikincisi de yukarıdaki sızıntı
    bekçisini geçerdi.
    """
    await _kapsam(seeded_db, Scope.limited, moduller={"projects"})
    sonuc = await _cagir("proje_detayi", maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(sonuc, Ok)
    assert sonuc.data["budget"] is None
    assert sonuc.data["progress_pct"] == "13.50"


async def test_MASKELI_is_kalemleri_IKI_KOVAYI_AYRI_gizler(
    seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """`boq` iki kovadan da alan taşır: `unit_price` PARA, `quantity` OPERASYONEL.

    Tek bir kapsamla ölçmek yetmezdi: "hepsini gizle" diyen bozuk bir maske de
    `limited` iddiasını geçerdi. İki kapsam birbirinin pozitif kontrolüdür.
    """
    await _kapsam(seeded_db, Scope.limited, moduller={"boq"})
    sinirli = await _cagir("is_kalemleri", maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(sinirli, Ok)
    kalem = sinirli.data["gruplar"][0]["items"][0]
    assert kalem["unit_price"] is None
    assert kalem["quantity"] == "21500.000"
    assert sinirli.data["grand_total"] is None

    await _kapsam(seeded_db, Scope.finance, moduller={"boq"})
    mali = await _cagir("is_kalemleri", maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(mali, Ok)
    mali_kalem = mali.data["gruplar"][0]["items"][0]
    assert mali_kalem["quantity"] is None
    assert mali_kalem["unit_price"] == "312.00"


async def test_MASKELI_arsa_payi_PAY_YUZDESINI_gizlemez(
    seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """Pay yüzdesi `kimlik` kovasındadır (`land_share_schemas.py`) — `limited`ta
    GÖRÜNÜR kalmalı. Değer toplamları ise PARA'dır ve gizlenir."""
    await _kapsam(seeded_db, Scope.limited, moduller={"projects"})
    sonuc = await _cagir("arsa_payi", maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(sonuc, Ok)
    assert sonuc.data["toplam_deger"] is None
    assert sonuc.data["our_share_pct"] == "50.00"


async def test_MASKELI_sozlesmeler_TOPLAMI_da_SATIRI_da_gizler(
    seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """🔴 Maske kanonu: maskeli bileşeni ATLAYIP toplama — eksik bir toplamı
    gerçek gibi basmak, gizlemekten kötüdür. Kart toplamı da `None` olmalı."""
    await _kapsam(seeded_db, Scope.limited, moduller={"contracts"})
    sonuc = await _cagir("sozlesmeler", maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(sonuc, Ok)
    assert sonuc.data["items"][0]["amount"] is None
    assert sonuc.data["total_amount"] is None


# ########################################################################### #
# ④ TURLAR ARASI BULAŞMA — köprü bir ContextVar'dır, AI turu ÇOK ARAÇLIDIR
# ########################################################################### #


async def test_ARDISIK_ARAC_CAGRILARI_BIRBIRININ_KAPSAMINI_TASIMAZ(
    seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """🔴 Bu bekçi AI hattına ÖZGÜDÜR ve ekran tarafında karşılığı YOKTUR.

    Kapsam köprüsü bir `ContextVar`dır (`scoped_route._KAPSAM`). Tarayıcıda her
    istek kendi görevini alır, dolayısıyla soru hiç doğmaz. AI'da ise TEK bir
    sohbet turu ardışık ONLARCA araç çağırır ve `httpx.ASGITransport` okuma
    düzlemini **çağıranın görevinde** koşturur — yani bir aracın yazdığı kapsam
    görev bağlamında ASILI KALIR.

    Bulaşma gerçek olsaydı hasar İKİ YÖNLÜ olurdu: dar kapsam sonraki aracın
    görünür verisini yutardı (ekranda görünen tutar AI'da kaybolur), geniş
    kapsam ise dar olanın gizlediğini AÇARDI — ikincisi bir SIZINTIDIR.

    Deney: iki modül BİLEREK farklı kapsamda; sıra da iki yönde denenir, çünkü
    tek yönlü bir deney yalnız "önceki dar" hâlini ölçerdi.
    """
    await _kapsam(seeded_db, Scope.limited, moduller={"projects"})
    await _kapsam(seeded_db, Scope.all, moduller={"boq"})

    proje = await _cagir("proje_detayi", maske_kurulumu, transport_factory, actor_factory)
    boq = await _cagir("is_kalemleri", maske_kurulumu, transport_factory, actor_factory)
    assert proje.data["budget"] is None, "projects=limited maskesi koşmadı"
    assert boq.data["grand_total"] == _POZ_TUTAR, (
        "önceki aracın `limited` kapsamı SONRAKİ araca bulaştı — köprü temizlenmiyor"
    )

    # Ters sıra: bu kez geniş kapsam önce koşar ve dar olanı AÇMAMALIDIR.
    await _kapsam(seeded_db, Scope.all, moduller={"projects"})
    await _kapsam(seeded_db, Scope.limited, moduller={"boq"})

    proje2 = await _cagir("proje_detayi", maske_kurulumu, transport_factory, actor_factory)
    boq2 = await _cagir("is_kalemleri", maske_kurulumu, transport_factory, actor_factory)
    assert proje2.data["budget"] == "77100000.00", "projects=all iken bütçe kayboldu"
    assert boq2.data["grand_total"] is None, (
        "önceki aracın `all` kapsamı SONRAKİ aracın maskesini AÇTI — SIZINTI"
    )


# ########################################################################### #
# ⑤ TÜRETİLMİŞ CÜMLE — maske sayıyı gizlediğinde METİN de yalan söylememeli
# ########################################################################### #


async def test_MASKELI_arsa_payi_NOTU_kullaniciyi_YANLIS_YERE_BAKTIRMAZ(
    seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """🔴 Maske bir SAYIYI gizlediğinde o sayıdan TÜRETİLEN CÜMLE de değişmeli.

    `deger_dengesi_notu`, sapma hesaplanamadığında İKİ sebep sayıyordu
    ("rayiç girilmemiş" · "hiçbir ünite atanmamış") ve kullanıcıya
    *"hangisi olduğunu `toplam_deger` ile `atanmamis_unite` birlikte söyler"*
    diyordu. `projects=limited` rolünde bu cümlenin İKİ AYRI YALANI vardır:

    1. ÜÇÜNCÜ bir sebep vardır ve sayılmaz — değer GİZLENMİŞTİR,
    2. kullanıcıyı `toplam_deger`e baktırır, oysa o alan da maskelenmiştir;
       yani verilen talimat İZLENEMEZ.

    Rakam gizlenip cümle aynı kalırsa maske sayıyı saklar ama YANLIŞ BİLGİYİ
    yayar — sessizce yanlış bir sayı basmaktan farkı yoktur.
    """
    await _kapsam(seeded_db, Scope.limited, moduller={"projects"})
    sonuc = await _cagir("arsa_payi", maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(sonuc, Ok)
    assert sonuc.data["toplam_deger"] is None, "ön koşul: para gerçekten maskeli"
    not_metni = sonuc.data["deger_dengesi_notu"]
    assert "İKİ sebebi" not in not_metni, "maskeli rolde hâlâ 'iki sebep' deniyor"
    assert "yetki" in not_metni.lower(), "üçüncü sebep (yetki) hiç anılmıyor"


async def test_POZITIF_KONTROL_all_kapsaminda_arsa_payi_NOTU_YETKIDEN_BAHSETMEZ(
    seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """K-İKİZ: her hâlde "yetkin olmayabilir" diyen bir cümle de yukarıdakini
    geçerdi — ve kısıtsız bir rolü var olmayan bir yetki sorununa baktırırdı.

    Bu kurulumda hiçbir ünite yoktur, yani sapma `all` kapsamında da
    hesaplanamaz: ölçülen tek fark KAPSAMDIR.
    """
    await _kapsam(seeded_db, Scope.all, moduller={"projects"})
    sonuc = await _cagir("arsa_payi", maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(sonuc, Ok)
    not_metni = sonuc.data["deger_dengesi_notu"]
    assert "yetki" not in not_metni.lower(), "kısıtsız rol yetki sorununa yönlendiriliyor"

"""P8 — FİİL AI okuma araçları HASSAS ALAN MASKESİYLE (`MaskeRotasi`) çalışır mı?

IZN-B4a: eski kapsam maskesi (`Scope.limited`/`finance`, `kapsam_rotasi`) söküldü; yerine rolün
`hidden_fields` bayrakları (`rol_gizle`/`RoleHiddenField`) ve alan etiketleri (`Hassas`) geldi.
Bu dosya AI hattının YENİ maskeyle **iki AYRI** ilişkisini ölçer:

| # | Soru | Bekçi |
|---|---|---|
| 1 | AI araçları maskeden GEÇİYOR MU, yoksa tutar SIZIYOR MU? | `test_SIZINTI_*` |
| 2 | Maskeli veri geldiğinde araç ÇALIŞIYOR MU? | `test_MASKELI_*` |

🔴 **İKİSİ BİRDEN GEREKLİ VE BİRİ ÖTEKİNİ İKAME ETMEZ.** Sızıntıyı ölçen bir test tek başına "araç
`ToolError` döndü" hâlinde de YEŞİL kalır (`ToolError` gövdesinde tutar yoktur). Çalışmayı ölçen
test ise maskenin HİÇ koşmadığı hâlde de yeşil kalırdı. Yalnız ikisi birlikte *"maske koşuyor VE
araç yaşıyor"* der.

## Ölçülen olgu: AI araçları SERVİSİ değil UCU sarar, maske de onlarda KOŞAR

`ReadOnlyTransport` kullanıcının KENDİ bearer'ıyla gerçek ucu GET eder; `build_read_plane` ana
uygulamanın orijinal `APIRoute` (= `MaskeRotasi`) nesnelerini taşır — rota sınıfı AI hattına da
gelir. Testler gerçek rol · gerçek bayrak · gerçek uç üzerinden koşar.

## Araç listesi ELLE YAZILMAZ — KODDAN ÖLÇÜLÜR

`_maskeye_bagli_araclar()` rota tablosunu gezer ve ucu `MaskeRotasi` olup yanıt şeması hassas alan
taşıyan araçları bulur. `_ARGUMANLAR` bu kümeyi KAPSAMALIDIR: yeni bir maskeli araç argümansız
kalırsa test SKIP değil KIRMIZI olur.

## Bayrak DEĞİŞKENİ testte kurulur, seed'den OKUNMAZ

Deneyin tek değişkeni rolün gizli kategorileridir; seviye sabittir.

## 🔴 `finance` kapsamı yoktur

Eski `finance` (OPERASYONEL gizle) kapsamı IZN-PLAN §3'te "karşılıksız" kaldırıldı: metraj /
ilerleme hiçbir rol bayrağıyla gizlenmez. Eski `test_SIZINTI_finance_*` testlerinin konusu kalmadı.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import delete, select

from app.core.field_mask import sema_plani, semalar_icinde
from app.core.mask_route import MaskeRotasi
from app.core.sayfalar import HiddenCategory
from app.main import app
from app.modules.ai import audit as ai_audit
from app.modules.ai.registry import ToolRegistry, ToolSpec
from app.modules.ai.result import Ok, ToolError, Truncated
from app.modules.ai.tools.catalog import READ_TOOLS
from app.modules.roles.models import Role, RoleHiddenField
from app.modules.users.models import ProjectMember
from tests.modules.approvals.conftest import isveren_evraki, rol_sahipleri_kur

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

#: Para kategorileri gizliyken AI zarfının HİÇBİRİNDE görünmemesi gereken PARA izleri.
#: 🔴 Noktasız/ondalıksız yazılır: zarf `model_dump(mode="json")` ile
#: serileşir ve `Decimal` metne döner, biçim değişse de rakam dizisi kalır.
_PARA_IZLERI = ("77100000", "64300000", "312.00", "6708000", "317000")
#: Onay kutusundaki işveren hakedişinin birim fiyatı: 100 m³ × 3.170 = 317.000 (brüt).
_ONAY_BIRIM_FIYAT = Decimal("3170.00")


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


def _maskeli_yollar() -> set[str]:
    """Yanıt şeması hassas alan taşıyan `MaskeRotasi` GET uçları."""
    return {
        rota.path
        for rota in _api_rotalari(app.routes)
        if "GET" in (rota.methods or set())
        and isinstance(rota, MaskeRotasi)
        and rota.response_model is not None
        and any(sema_plani(sema).hassas_agac for sema in semalar_icinde(rota.response_model))
    }


def _maskeye_bagli_araclar() -> list[ToolSpec]:
    """Ucu hassas yanıt veren `MaskeRotasi`ya bağlı okuma araçları. ELLE YAZILMIŞ LİSTE DEĞİL."""
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
    # IZN-B4b: hakediş / onay kutusu uçları da artık maskelidir (tutar etiketli); argümansızdır.
    "isveren_hakedisleri": lambda k: {},
    "taseron_hakedisleri": lambda k: {},
    "onay_kutum": lambda k: {},
}


def test_MASKELI_ARAC_KUMESI_ARGUMAN_HARITASINI_KAPSAR() -> None:
    """🔴 Parametrize bir test EKSİK girdi için kırmızı OLMAZ, sadece az koşar.

    Hassas yanıt veren yeni bir araç bu kümeye girer; argümanı yazılmazsa aşağıdaki bekçiler onu
    sessizce atlardı. Kapsama kontrolü o sessizliği kapatır.
    """
    assert MASKELI_ARACLAR, "maskeye bağlı HİÇ araç bulunamadı — keşif kırık"
    eksik = {s.ad for s in MASKELI_ARACLAR} - set(_ARGUMANLAR)
    assert not eksik, f"argümanı yazılmamış maskeli araç: {sorted(eksik)}"


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
    from app.modules.approvals import service as onay_servisi
    from app.modules.approvals.models import ApprovalDocumentType
    from app.modules.boq.models import BoqGroup, BoqItem
    from app.modules.contracts.models import Subcontractor, SubcontractorContract
    from app.modules.progress_payments.models import ProgressPayment, ProgressPaymentStatus
    from app.modules.projects.models import ProjectContract, ProjectLandShare
    from app.modules.sites.models import Site, SiteStatus
    from app.modules.subcontractor_progress_payments.models import (
        SubcontractorPaymentStatus,
        SubcontractorProgressPayment,
    )

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

    # IZN-B4b onarımı: hakediş araçları (`isveren_hakedisleri` / `taseron_hakedisleri`) için TOHUM.
    # Tohumsuz araç boş küme döner ve `gross_total`/`net_total` hiç doğrulanmazdı (regresyon:
    # `Decimal` zorunlu iken maskeli `null` araçta ValidationError → `ust_kaynak_hatasi`).
    seeded_db.add(
        ProgressPayment(
            project_id=proje.id,
            sequence_no=1,
            status=ProgressPaymentStatus.draft,
            vat_pct=Decimal("20.00"),
            advance_pct=Decimal("0.00"),
            retainage_pct=Decimal("0.00"),
            created_by=kullanici.id,
        )
    )
    taseron_sozlesmesi = SubcontractorContract(
        project_id=proje.id,
        subcontractor_name="Maske Taşeron Ltd.",
        contract_no="P8-TSZ-1",
        advance_pct=Decimal("0.00"),
        retainage_pct=Decimal("0.00"),
        vat_pct=Decimal("20.00"),
        created_by=kullanici.id,
    )
    seeded_db.add(taseron_sozlesmesi)
    await seeded_db.flush()
    seeded_db.add(
        SubcontractorProgressPayment(
            contract_id=taseron_sozlesmesi.id,
            project_id=proje.id,
            sequence_no=1,
            status=SubcontractorPaymentStatus.draft,
            vat_pct=Decimal("20.00"),
            advance_pct=Decimal("0.00"),
            retainage_pct=Decimal("0.00"),
            created_by=kullanici.id,
        )
    )
    await seeded_db.flush()

    # IZN-B4b onarımı: ONAY KUTUSU tohumu. Kutu satırının tutarı evrakın PROJESİNDEKİ rolle
    # maskelenir: kişi ikinci projede `accounting` proje rolüyle üyedir ve işveren hakedişinin
    # ilk adımı `accounting`tir → satır kutuya düşer.
    # "Tüm projeler" kişide ekip satırı YOK SAYILIR (`step_owner_clause`): ekip kişisi yapılır.
    kullanici.all_projects = False
    onay_projesi = await project_factory(code="P8-ONY", name="Onay Projesi")
    muhasebe_rol_id = (
        await seeded_db.execute(select(Role.id).where(Role.key == "accounting"))
    ).scalar_one()
    seeded_db.add(
        ProjectMember(user_id=kullanici.id, project_id=onay_projesi.id, role_id=muhasebe_rol_id)
    )
    await seeded_db.flush()
    await rol_sahipleri_kur(seeded_db, onay_projesi)
    # Görevler ayrılığı: kendi evrakı kutuya düşmez → evrakı BAŞKASI açar.
    yaratan = await user_factory("p8onay-yaratan@fiil.example.com", "Sifre1234!", _ROL)
    belge_id = await isveren_evraki(
        seeded_db, onay_projesi, yaratan, unit_price=_ONAY_BIRIM_FIYAT, quantity=Decimal("100")
    )
    await onay_servisi.create_chain(
        seeded_db,
        document_type=ApprovalDocumentType.progress_payment,
        document_id=belge_id,
        amount=Decimal("100.00"),
        created_by_user_id=yaratan.id,
    )

    # 🔴 KİMLİK HARİTASINDAN ÇIKAR: okuma düzlemi AYNI session'ı kullanır ve
    # `get_current_user` `joinedload(User.role)` ister; nesne haritada ROLSÜZ
    # dururken `options` SESSİZCE yok sayılır ve `User.role` (`lazy="raise"`)
    # patlar. Üretimde her istek kendi session'ını alır — testin kurgusunun
    # bedelidir, ürün kusuru DEĞİL (`test_ai0b_kapsam.py` ile aynı not).
    seeded_db.expunge(kullanici)

    return {"user": kullanici, "proje_id": proje.id, "santiye_id": santiye.id}


#: Eski `limited` (PARA gizle) karşılığı: tüm para kategorileri + "tüm tutarlar".
_PARA_GIZLI = {
    HiddenCategory.sozlesme_fiyat,
    HiddenCategory.maliyet_kar,
    HiddenCategory.satis_alici,
    HiddenCategory.banka_kasa,
    HiddenCategory.tum_tutarlar,
}


async def _gizli(seeded_db, kategoriler: set[HiddenCategory]) -> None:
    """Rolün maske bayraklarını (`role_hidden_fields`) TAM değiştirir.

    Bayrak ANA role (`_ROL`) VE onay kutusu projesindeki proje rolüne (`accounting`) yazılır:
    kutu satırı evrakın projesindeki rolle maskelenir (IZN-B4b onarımı)."""
    for anahtar in (_ROL, "accounting"):
        rol_id = (await seeded_db.execute(select(Role.id).where(Role.key == anahtar))).scalar_one()
        await seeded_db.execute(delete(RoleHiddenField).where(RoleHiddenField.role_id == rol_id))
        for kategori in kategoriler:
            seeded_db.add(RoleHiddenField(role_id=rol_id, category=kategori))
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


@pytest.mark.parametrize("spec", MASKELI_ARACLAR, ids=lambda s: s.ad)
async def test_MASKELI_arac_UST_KAYNAK_HATASI_VERMEZ(
    spec, seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """Maskeli bir rol AI'a soru sorduğunda araç ÇALIŞMALIDIR.

    🔴 `ust_kaynak_hatasi` burada "uç bozuk" DEMEK DEĞİLDİR: huninin son dalı
    (`registry.py`, 6a) handler İÇİNDEKİ her istisnayı bu koda düşürür. Maskeli
    bir alan `None` gelip daraltma şeması onu `Decimal` sandığında doğan
    `ValidationError` da buraya düşer ve kullanıcı "sistem hatası" görür —
    oysa ürünün kararı o alanı GİZLEMEKTİ, aracı ÖLDÜRMEK değil.
    """
    await _gizli(seeded_db, _PARA_GIZLI)
    sonuc = await _cagir(spec.ad, maske_kurulumu, transport_factory, actor_factory)
    assert not (isinstance(sonuc, ToolError) and sonuc.kod == "ust_kaynak_hatasi"), (
        f"{spec.ad} aracı para kategorileri gizliyken PATLADI"
    )


@pytest.mark.parametrize("spec", MASKELI_ARACLAR, ids=lambda s: s.ad)
async def test_POZITIF_KONTROL_bayraksiz_rolde_arac_VERI_DONDURUR(
    spec, seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """K-İKİZ: tohumlar çürürse yukarıdaki bekçi BOŞ KÜME ölçerdi ve yeşil kalırdı.

    Boş bir listede kalem şeması hiç kurulmaz — maskeli bir `Decimal` asla
    doğrulanmaz. Bu bekçi her aracın gerçekten satır/kart döndürdüğünü çakar.
    """
    await _gizli(seeded_db, set())
    sonuc = await _cagir(spec.ad, maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(sonuc, Ok | Truncated), f"{spec.ad} → {type(sonuc).__name__}"
    assert sonuc.row_count >= 1, f"{spec.ad} BOŞ döndü — tohum çürümüş"


# ########################################################################### #
# ② SIZINTI — AI araçları maskeden GEÇİYOR MU?
# ########################################################################### #


@pytest.mark.parametrize("spec", MASKELI_ARACLAR, ids=lambda s: s.ad)
async def test_SIZINTI_para_gizli_rolde_PARA_hicbir_AI_zarfina_GIRMEZ(
    spec, seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """🔴 Asıl soru: araç UCU mu yoksa SERVİSİ mi sarıyor?

    Servisi sarsaydı `kapsam_rotasi` hiç devreye girmez ve kullanıcının ekranda
    `—` gördüğü tutar AI cevabından çıkardı. Tek bir alana değil **gövdenin
    tamamına** bakılır: daraltma katmanı alanı yeniden adlandırabilir
    (`value_total` → `toplam_deger`) ve ada bağlı bir assert onu kaçırırdı.
    """
    await _gizli(seeded_db, _PARA_GIZLI)
    sonuc = await _cagir(spec.ad, maske_kurulumu, transport_factory, actor_factory)
    govde = _metin(sonuc)
    for iz in _PARA_IZLERI:
        assert iz not in govde, f"{spec.ad}: para gizli rolde PARA sızdı ({iz})"


async def test_POZITIF_KONTROL_bayraksiz_rolde_PARA_da_OPERASYONEL_de_GORUNUR(
    seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """🔴 SIZINTI BEKÇİSİNİN İKİZİ.

    O "gövdede şu rakam YOK" der; her zaman `ToolError` dönen bozuk bir araç da o iddiayı geçerdi.
    Bu test aynı rakamların bayraksız rolde GERÇEKTEN göründüğünü çakar.
    """
    await _gizli(seeded_db, set())
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
    await _gizli(seeded_db, _PARA_GIZLI)
    sonuc = await _cagir("proje_detayi", maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(sonuc, Ok)
    assert sonuc.data["budget"] is None
    assert sonuc.data["progress_pct"] == "13.50"


async def test_MASKELI_is_kalemleri_KATEGORIYE_gore_gizler(
    seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """IZN-B4: AI aracı ucu saran gerçek `GET`tir; ucun YENİ maskesi (`Hassas`) aynen geçerlidir.

    `sozlesme_fiyat` gizliyken birim fiyat + türevler düşer, metraj durur; ilgisiz kategori
    (`maliyet_kar`) gizliyken fiyat AÇIK: iki durum birbirinin pozitif kontrolüdür."""
    await _gizli(seeded_db, {HiddenCategory.sozlesme_fiyat})
    sinirli = await _cagir("is_kalemleri", maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(sinirli, Ok)
    kalem = sinirli.data["gruplar"][0]["items"][0]
    assert kalem["unit_price"] is None
    assert kalem["quantity"] == "21500.000"
    assert sinirli.data["grand_total"] is None

    await _gizli(seeded_db, {HiddenCategory.maliyet_kar})
    acik = await _cagir("is_kalemleri", maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(acik, Ok)
    assert acik.data["gruplar"][0]["items"][0]["unit_price"] == "312.00"
    assert acik.data["grand_total"] == _POZ_TUTAR


async def test_MASKELI_arsa_payi_PAY_YUZDESINI_gizlemez(
    seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """Pay yüzdesi `kimlik` kovasındadır (`land_share_schemas.py`) — `limited`ta
    GÖRÜNÜR kalmalı. Değer toplamları ise PARA'dır ve gizlenir."""
    await _gizli(seeded_db, _PARA_GIZLI)
    sonuc = await _cagir("arsa_payi", maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(sonuc, Ok)
    assert sonuc.data["toplam_deger"] is None
    assert sonuc.data["our_share_pct"] == "50.00"


async def test_MASKELI_sozlesmeler_TOPLAMI_da_SATIRI_da_gizler(
    seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """🔴 Maske kanonu: maskeli bileşeni ATLAYIP toplama — eksik bir toplamı
    gerçek gibi basmak, gizlemekten kötüdür. Kart toplamı da `None` olmalı."""
    await _gizli(seeded_db, _PARA_GIZLI)
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
    # 1) yalnız maliyet/kâr gizli: proje bütçesi (`maliyet_kar`) düşer, BOQ fiyatı
    #    (`sozlesme_fiyat`) AÇIK kalır — aynı turda iki araç, iki ayrı bayrak etkisi.
    await _gizli(seeded_db, {HiddenCategory.maliyet_kar})
    proje = await _cagir("proje_detayi", maske_kurulumu, transport_factory, actor_factory)
    boq = await _cagir("is_kalemleri", maske_kurulumu, transport_factory, actor_factory)
    assert proje.data["budget"] is None, "maliyet_kar maskesi koşmadı"
    assert boq.data["grand_total"] == _POZ_TUTAR, (
        "önceki aracın maskesi SONRAKİ araca bulaştı — bağlam temizlenmiyor"
    )

    # 2) ters bayrak: bütçe AÇILIR, BOQ fiyatı DÜŞER. Geniş görünürlük önceki dar maskeyi AÇMAZ,
    #    dar maske sonrakini YUTMAZ (iki yönlü; tek yönlü deney yalnız "önceki dar"ı ölçerdi).
    await _gizli(seeded_db, {HiddenCategory.sozlesme_fiyat})
    proje2 = await _cagir("proje_detayi", maske_kurulumu, transport_factory, actor_factory)
    boq2 = await _cagir("is_kalemleri", maske_kurulumu, transport_factory, actor_factory)
    assert proje2.data["budget"] == "77100000.00", "maliyet_kar açıkken bütçe kayboldu"
    assert boq2.data["grand_total"] is None, (
        "önceki aracın açık görünürlüğü SONRAKİ aracın maskesini AÇTI — SIZINTI"
    )

    # 3) aynı araç art arda: bayrak kalkınca maske de kalkar (bağlam çağrıya SIZMAZ).
    await _gizli(seeded_db, set())
    boq3 = await _cagir("is_kalemleri", maske_kurulumu, transport_factory, actor_factory)
    assert boq3.data["grand_total"] == _POZ_TUTAR


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
    await _gizli(seeded_db, _PARA_GIZLI)
    sonuc = await _cagir("arsa_payi", maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(sonuc, Ok)
    assert sonuc.data["toplam_deger"] is None, "ön koşul: para gerçekten maskeli"
    not_metni = sonuc.data["deger_dengesi_notu"]
    assert "İKİ sebebi" not in not_metni, "maskeli rolde hâlâ 'iki sebep' deniyor"
    assert "yetki" in not_metni.lower(), "üçüncü sebep (yetki) hiç anılmıyor"


async def test_POZITIF_KONTROL_bayraksiz_rolde_arsa_payi_NOTU_YETKIDEN_BAHSETMEZ(
    seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """K-İKİZ: her hâlde "yetkin olmayabilir" diyen bir cümle de yukarıdakini
    geçerdi — ve kısıtsız bir rolü var olmayan bir yetki sorununa baktırırdı.

    Bu kurulumda hiçbir ünite yoktur, yani sapma `all` kapsamında da
    hesaplanamaz: ölçülen tek fark KAPSAMDIR.
    """
    await _gizli(seeded_db, set())
    sonuc = await _cagir("arsa_payi", maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(sonuc, Ok)
    not_metni = sonuc.data["deger_dengesi_notu"]
    assert "yetki" not in not_metni.lower(), "kısıtsız rol yetki sorununa yönlendiriliyor"


# ########################################################################### #
# ⑥ HAKEDİŞ ARAÇLARI — IZN-B4b onarımı (gerileme: `gross_total: Decimal` zorunluydu)
# ########################################################################### #


@pytest.mark.parametrize(
    ("arac", "kategori"),
    [
        ("isveren_hakedisleri", HiddenCategory.sozlesme_fiyat),
        ("taseron_hakedisleri", HiddenCategory.maliyet_kar),
    ],
)
async def test_MASKELI_hakedis_araci_OK_doner_tutarlar_NULL_acik_rolde_GORUNUR(
    arac, kategori, seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """Uç `gross_total`/`net_total`ı gizli rolde `null` döndürür. Araç şeması `Decimal`
    ZORUNLU olsaydı `ValidationError` → `ToolError("ust_kaynak_hatasi")` olurdu (B4b'de böyleydi,
    hiçbir test yakalamıyordu). Gizli rolde `Ok` + `null`; ilgisiz kategori gizliyken AÇIK."""
    await _gizli(seeded_db, {kategori})
    gizli = await _cagir(arac, maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(gizli, Ok | Truncated), f"{arac} → {type(gizli).__name__}"
    assert gizli.row_count >= 1, "tohum çürümüş"
    satir = gizli.data[0]
    assert satir["gross_total"] is None
    assert satir["net_total"] is None

    ilgisiz = (
        HiddenCategory.maliyet_kar
        if kategori is HiddenCategory.sozlesme_fiyat
        else HiddenCategory.sozlesme_fiyat
    )
    await _gizli(seeded_db, {ilgisiz})
    acik = await _cagir(arac, maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(acik, Ok | Truncated)
    assert acik.data[0]["gross_total"] is not None
    assert acik.data[0]["net_total"] is not None


async def test_MASKELI_onay_kutum_tutari_EVRAK_TIPINE_gore_gizlenir(
    seeded_db, maske_kurulumu, transport_factory, actor_factory
) -> None:
    """IZN-B4b onarımı: onay kutusu tutarı etiketsizdi. İşveren hakedişi `sozlesme_fiyat`tır;
    `maliyet_kar` gizliyken AÇIK, `sozlesme_fiyat` gizliyken `null`."""
    await _gizli(seeded_db, {HiddenCategory.sozlesme_fiyat})
    gizli = await _cagir("onay_kutum", maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(gizli, Ok | Truncated), type(gizli).__name__
    assert gizli.data["items"][0]["gross_amount"] is None
    assert gizli.data["items"][0]["net_amount"] is None

    await _gizli(seeded_db, {HiddenCategory.maliyet_kar})
    acik = await _cagir("onay_kutum", maske_kurulumu, transport_factory, actor_factory)
    assert isinstance(acik, Ok | Truncated)
    assert acik.data["items"][0]["gross_amount"] == "317000.00"

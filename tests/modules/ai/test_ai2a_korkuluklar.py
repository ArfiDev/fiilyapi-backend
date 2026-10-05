"""AI-2a — ÜÇ KORKULUK + BİR KALIP. Hepsi mutasyonla + pozitif kontrolle.

| # | Bekçi | Mutasyon (KIRMIZI) | Pozitif kontrol (YEŞİL) |
|---|---|---|---|
| ① | `ai_exposed` ifşa seviyesi | Bayrağı `ACIK` çevir | `projects` aracı kaydedilir |
| ① | `AGREGA` kişi adı koşulu | Araca `full_name` ekle | Adsız bordro TOPLAMI GEÇER |
| ① | `veri_modulleri` ayrı eksen | `kapilar=∅` + `personnel` sar | `onay_kutum` GEÇER |
| ② | Alan maskesi (şema) | Şemaya `tc_no` ekle | Maskesiz şema geçer |
| ② | Alan maskesi (ÇALIŞMA ANI) | Zarf taramasını sil | Temiz `totals` `Ok` döner |
| ③ | `scope_note` (S10) | Çağrı yerini sil | Üç `kume` üç AYRI cümle |
| ③ | `SIRKET_GENELI` beyanı | OR dalını sil | Beyan ölçümle örtüşür |
| ④ | Sayfalamasız `Truncated` | Bildirilmemiş `params` | Bugünkü üç handler GEÇER |
| ④ | FastAPI yutması | (kanıt ölçümdür) | `limit` bildiren uçta tavan İŞLER |

🔴 **Bekçi ölçtüğü yolu KENDİSİ KURMAZ.** Bu dosyada kurulan tek şey sahte
`ToolSpec`lerdir (mutantlar); rota tablosu `app.main:app`ın, süzgeçler
`inventory`/`equipment` repository'lerinin, huni `ToolRegistry.invoke`un ve
çağrı yeri `loop.ajan_turu`nundur.
"""

from __future__ import annotations

import ast
import inspect
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.core.access import AccessLevel
from app.core.security import create_access_token
from app.main import app
from app.modules.ai import audit as ai_audit
from app.modules.ai import exposure, guards
from app.modules.ai import loop as ai_loop
from app.modules.ai.exposure import (
    AI_IFSA,
    KASTEN_DISARIDA,
    KISI_ADI_ANAHTARLARI,
    NAIF_MASKE_KOKLERI,
    S5C_ANAHTARLARI,
    YASAK_ALAN_ANAHTARLARI,
    IfsaIhlali,
    IfsaSeviyesi,
)
from app.modules.ai.loop import ajan_turu
from app.modules.ai.providers.base import (
    AiOlay,
    AracCagrisiHazir,
    Kullanim,
    Mesaj,
    MetinParcasi,
    TurBitti,
    TurSebebi,
)
from app.modules.ai.registry import (
    AracBaglami,
    ToolKapsami,
    ToolKumesi,
    ToolRegistry,
    ToolSpec,
)
from app.modules.ai.result import (
    AracSonucu,
    Empty,
    Ok,
    ScopedEmpty,
    ToolError,
    Truncated,
    sayfalamasiz_liste_sonucu,
)
from app.modules.ai.tools import schemas
from app.modules.ai.tools.catalog import CATALOG, GOSTERGE_OZETI, READ_TOOLS, REGISTRY
from app.modules.roles.seed_data import MODULES
from tests.modules.ai.conftest import sahte_aktor, tam_izin

AI_KOK = Path(__file__).parents[3] / "app" / "modules" / "ai"
UYGULAMA_KOK = Path(__file__).parents[3] / "app"


# --------------------------------------------------------------------------- #
# Ortak sahte araç kurucusu (MUTANT üretici)
# --------------------------------------------------------------------------- #


class _Bos(BaseModel):
    pass


def _spec(
    ad: str,
    *,
    veri_modulleri: frozenset[str],
    yanit_modeli: type[BaseModel] = _Bos,
    kapilar: frozenset[tuple[str, AccessLevel]] = frozenset(),
    ucler: tuple[str, ...] = (),
    kume: ToolKumesi = ToolKumesi.KAPSAMSIZ,
    calistir: Any = None,
) -> ToolSpec:
    return ToolSpec(
        ad=ad,
        aciklama=f"NE ZAMAN: test. NE SORMAZ: hiçbir şey. ({ad})",
        kapsam=ToolKapsami.KENDI_KUMESI,
        kume=kume,
        kapilar=kapilar,
        ucler=ucler,
        veri_modulleri=veri_modulleri,
        yol_parametreleri={},
        girdi=_Bos,
        yanit_modeli=yanit_modeli,
        calistir=calistir,
    )


# ############################################################################ #
# ① `ai_exposed` — MODÜL BAZLI İFŞA SEVİYESİ (A1 / K1)
# ############################################################################ #


def test_ifsa_haritasi_TUM_IZIN_MODULLERINI_ADIYLA_KAPSAR() -> None:
    """🔴 KÜME EŞİTLİĞİ — sihirli sayı yasak (B1 kanonu).

    23. modül açıldığında bu test kırmızı olur ve ekleyen kişi **bilinçli bir
    KVKK kararı** vermek zorunda kalır. Varsayılan bir `ACIK` yedeği olsaydı
    yeni bir PII modülü **sessizce** sağlayıcıya açılırdı.
    """
    tohumlu = {m["key"] for m in MODULES}
    assert set(AI_IFSA) == tohumlu, (
        f"Harita ile tohumlanan modüller ayrışıyor.\n"
        f"  haritada olmayan: {sorted(tohumlu - set(AI_IFSA))}\n"
        f"  fazlalık: {sorted(set(AI_IFSA) - tohumlu)}"
    )


def test_ifsa_UC_DURUMUN_UCU_de_GERCEKTEN_KULLANILIYOR() -> None:
    """İki durumlu bir bayrak K1'i ifade EDEMEZ; üçüncü durum süs değildir."""
    kullanilan = set(AI_IFSA.values())
    assert kullanilan == set(IfsaSeviyesi), (
        f"Kullanılmayan seviye var: {sorted(s.value for s in set(IfsaSeviyesi) - kullanilan)}. "
        "Kullanılmayan bir enum üyesi tam olarak `Scope`un düştüğü yerdir."
    )


def test_K1_kullanici_kararinin_DORT_MODULU() -> None:
    """K1 (2026-09-04) birebir: dördü de `ACIK` DEĞİLDİR."""
    # 🔴 ÖLÇÜM K1'in bir premise'ini düzeltti: `customers` bir İZİN MODÜLÜ
    # DEĞİLDİR (`MODULES`ta yok; `customers/router.py:42` kapısı `sales`).
    # Yani karar "dört modül" der ama uygulanabilir hâli ÜÇ modüldür.
    assert "customers" not in {m["key"] for m in MODULES}
    assert exposure.seviye("customers") is IfsaSeviyesi.KAPALI  # fail-closed yedek
    assert exposure.seviye("sales") is IfsaSeviyesi.KAPALI
    # Agrega istisnası YALNIZ bu ikisine tanındı ("bordro dönem toplamları" +
    # "özlük KPI'ları").
    assert exposure.seviye("personnel") is IfsaSeviyesi.AGREGA
    assert exposure.seviye("payroll") is IfsaSeviyesi.AGREGA


def test_bilinmeyen_modul_FAIL_CLOSED_kapalidir() -> None:
    assert exposure.seviye("boyle_bir_modul_yok") is IfsaSeviyesi.KAPALI


# --- KAPALI: araç KAYDEDİLEMEZ --------------------------------------------- #


def test_KAPALI_modul_verisi_tasiyan_arac_KAYDEDILEMEZ() -> None:
    mutant = _spec("musteri_listesi", veri_modulleri=frozenset({"customers"}))
    with pytest.raises(IfsaIhlali, match="KAPALI"):
        ToolRegistry((mutant,))


def test_MUTASYON_bayrak_ACIK_yapilirsa_AYNI_ARAC_KAYDEDILIR(monkeypatch) -> None:
    """🔴 BAYRAĞIN GERÇEKTEN OKUNDUĞUNUN kanıtı.

    Bu depoda ölçülmüş bir kusur var: `Scope` enum'unun 14 isabetinin hepsi
    `roles/` altındadır ve **hiçbir süzgeç okumaz** — etiket dekoratiftir.
    Buradaki mutasyon o soruyu doğrudan sorar: bayrağı çevirdiğimde davranış
    DEĞİŞİYOR MU? Değişmeseydi `ai_exposed` de dekoratif olurdu.
    """
    mutant = _spec("musteri_listesi", veri_modulleri=frozenset({"customers"}))
    monkeypatch.setitem(AI_IFSA, "customers", IfsaSeviyesi.ACIK)  # type: ignore[arg-type]
    kayit = ToolRegistry((mutant,))  # artık PATLAMAZ
    assert {s.ad for s in kayit.tum_araclar} == {"musteri_listesi"}


def test_POZITIF_KONTROL_ACIK_modul_araci_NORMAL_KAYDEDILIR() -> None:
    """K-IKIZ1: her şeyi reddeden bozuk bir kapı da yukarıdaki testi geçerdi."""
    kayit = ToolRegistry((_spec("proje_sondasi", veri_modulleri=frozenset({"projects"})),))
    assert {s.ad for s in kayit.tum_araclar} == {"proje_sondasi"}


def test_GERCEK_KATALOG_kayitli_ve_BOS_DEGIL() -> None:
    """Kapı `REGISTRY` kurulurken fiilen koştu (import anında) ve altı araç ayakta."""
    assert len(REGISTRY.tum_araclar) == len(CATALOG) >= 6
    assert any(s.veri_modulleri for s in CATALOG), (
        "Hiçbir araç veri modülü beyan etmiyorsa ① hiçbir şey ölçmüyordur."
    )


# --- Ölçülmüş delik: `kapilar=∅` ile kaçış ---------------------------------- #


def test_OLCULMUS_DELIK_kapisiz_personnel_araci_YINE_DE_REDDEDILIR() -> None:
    """🔴 AI-2a'nın var oluş sebebi.

    Ölçüldü: `personnel/repository.py::list_personnel(session, ...)` **bütün
    parametreleri varsayılanlıdır ve aktör ALMAZ**. Bugün B26 denylist'i
    `{user_management, settings, approvals, roles}`tir — `personnel` orada
    YOKTUR. Ve denylist yalnız `kapilar`a bakar; `kapilar=∅` yazan bir araç ona
    **hiç takılmaz** (bu incelik `onay_kutum` için bilinçlidir).

    Yani `kapilar=∅` + `ucler=("/personnel",)` yazan bir ToolSpec, `veri_modulleri`
    ekseni olmadan **hiçbir yapısal engelle karşılaşmazdı**. Karşılaşıyor:
    """
    kacak = _spec(
        "personel_listesi",
        veri_modulleri=frozenset({"personnel"}),
        kapilar=frozenset(),
        ucler=("/personnel",),
        yanit_modeli=_KisiliBordro,
    )
    with pytest.raises(IfsaIhlali, match="KİŞİ ADI"):
        ToolRegistry((kacak,))


def test_veri_modulleri_UC_saran_arac_icin_ZORUNLUDUR() -> None:
    beyansiz = _spec("beyansiz", veri_modulleri=frozenset(), ucler=("/projects",))
    with pytest.raises(IfsaIhlali, match="BOŞ"):
        ToolRegistry((beyansiz,))


def test_veri_modulleri_KAPILARI_KAPSAMALIDIR() -> None:
    """Beyanı daraltarak maske kaçırılamaz."""
    dar = _spec(
        "dar_beyan",
        veri_modulleri=frozenset({"sites"}),
        kapilar=frozenset({("payroll", AccessLevel.view)}),
        ucler=("/payroll",),
    )
    with pytest.raises(IfsaIhlali, match="KAPISINI"):
        ToolRegistry((dar,))


def test_POZITIF_KONTROL_ucsuz_arac_BOS_beyanla_gecer() -> None:
    """`navigate_to` hiçbir kayıt okumaz; boş beyan onun için DOĞRUdur."""
    kayit = ToolRegistry((_spec("yonlendir", veri_modulleri=frozenset(), ucler=()),))
    assert kayit.tum_araclar[0].veri_modulleri == frozenset()


# --- AGREGA: kişi adı koşulu ------------------------------------------------ #


class _AdsizBordroDonemi(BaseModel):
    """K1'in AÇTIĞI şey: bordro DÖNEM TOPLAMI. Kişi adı YOK, S5(c) YOK."""

    donem: str
    calisan_sayisi: int
    # 🔴 `sgk_premium_total` bir kişisel veri DEĞİL, bir dönem toplamıdır —
    # alt dizi eşleşmesi kullansaydık burada yanlışlıkla reddedilirdi.
    sgk_premium_total: str
    net_toplam: str


class _KisiliBordro(BaseModel):
    """Aynı toplam + KİŞİ ADI. K1'in koşulunu ihlal eder."""

    donem: str
    net_toplam: str
    full_name: str


class _MaaslıBordro(BaseModel):
    donem: str
    wage_amount: str


def test_AGREGA_modul_araci_KISI_ADI_tasiyamaz() -> None:
    mutant = _spec(
        "bordro_ozeti", veri_modulleri=frozenset({"payroll"}), yanit_modeli=_KisiliBordro
    )
    with pytest.raises(IfsaIhlali, match="KİŞİ ADI"):
        ToolRegistry((mutant,))


def test_POZITIF_KONTROL_AGREGA_modulun_ADSIZ_araci_GECER() -> None:
    """🔴 K-IKIZ1 — KARŞIT KANIT.

    Her şeyi reddeden bozuk bir kapı da yukarıdaki mutasyon testini geçerdi.
    K1 `payroll`u tümüyle kapatmadı: **dönem toplamları açılabilir**. Bu test o
    kapının gerçekten AÇIK olduğunu ölçer; kapanırsa kullanıcının verdiği izin
    sessizce geri alınmış olur.
    """
    kayit = ToolRegistry(
        (
            _spec(
                "bordro_ozeti",
                veri_modulleri=frozenset({"payroll"}),
                yanit_modeli=_AdsizBordroDonemi,
            ),
        )
    )
    assert kayit.tum_araclar[0].ad == "bordro_ozeti"


def test_KISI_ADI_yasagi_YALNIZ_AGREGA_modullerde_kosar() -> None:
    """🔴 Kapının DAR olduğunun kanıtı — ve canlı bir aracın hayatta kalması.

    `onay_kutum` `created_by_name` taşır (`AiOnayKalemi`) ve `AGREGA` bir modül
    beyan etmez. Yasak tüm araçlara uygulansaydı **bugün çalışan bir araç
    kırılırdı**; kırılmaması kapının doğru yere konduğunun ölçüsüdür.
    """
    assert "created_by_name" in KISI_ADI_ANAHTARLARI
    assert "created_by_name" in exposure.sema_anahtarlari(schemas.AiOnayKutusu)
    kayit = ToolRegistry(
        (
            _spec(
                "onay_benzeri", veri_modulleri=frozenset({"approvals"}), yanit_modeli=_KisiliBordro
            ),
        )
    )
    assert kayit.tum_araclar[0].ad == "onay_benzeri"
    # Ve gerçek `onay_kutum` katalogda ayakta:
    assert "onay_kutum" in {s.ad for s in CATALOG}


# ############################################################################ #
# ② ALAN MASKESİ — S5(c). Anahtar SONUCA HİÇ KONMAZ (`null` DEĞİL).
# ############################################################################ #


def test_S5c_ALTI_ANAHTAR_SPECTEN_BIREBIR() -> None:
    """AI-SPEC §4.2 S5(c) satırında birebir sayılan altı anahtar."""
    assert S5C_ANAHTARLARI == {
        "tc_no",
        "iban",
        "sgk_no",
        "wage_amount",
        "address",
        "birth_date",
    }
    assert S5C_ANAHTARLARI <= YASAK_ALAN_ANAHTARLARI


def test_POZITIF_KONTROL_S5c_anahtarlari_GERCEKTEN_VAR_hayalet_avlamiyoruz() -> None:
    """Yasak küme boş bir hedefte dolaşmıyor: altısı da `Personnel`de KOLONDUR.

    (`address` `String` bir kolon; ötekiler `personnel/models.py:148-168`.)
    """
    from app.modules.personnel.models import Personnel

    kolonlar = {c.name for c in Personnel.__table__.columns}
    eksik = sorted(S5C_ANAHTARLARI - kolonlar)
    assert eksik == [], (
        f"S5(c) anahtarları `personnel` tablosunda bulunamadı: {eksik}. "
        "İddia bayatlamış olabilir; yeniden ölç."
    )


def test_KASTEN_DISARIDA_ALT_DIZI_ESLESMESININ_YASAK_OLDUGUNUN_KANITI() -> None:
    """🔴 ÖLÇÜLDÜ: `sgk` alt dizisi 9 alan yakalar, 7'si bordro DÖNEM TOPLAMIDIR.

    Yani `"sgk" in ad` yazan bir maske, K1'in **açtığı** kapıyı kapatırdı.
    Aynı tuzak `wage_amount` ↔ `wage_type` ve `iban` ↔ `bank_account_id`
    çiftlerinde de var.
    """
    assert KASTEN_DISARIDA & YASAK_ALAN_ANAHTARLARI == frozenset(), (
        "Kasten dışarıda bırakılan bir anahtar yasak kümeye sızmış."
    )
    # 🔴 Ve bu karar önemsiz DEĞİL: naif bir KÖK maskesi (bir insanın elle
    # yazacağı hâl) `KASTEN_DISARIDA`nın **hepsini** yakalardı.
    for anahtar in KASTEN_DISARIDA:
        assert any(kok in anahtar for kok in NAIF_MASKE_KOKLERI), (
            f"`{anahtar}` hiçbir naif kökle eşleşmiyor — listede olmasının bir "
            "sebebi kalmamış, ölçümü tekrarla."
        )
    # Karşıt kanıt: naif kök maskesi GERÇEK yasak anahtarları da yakalardı,
    # yani kökler saçma değil, sadece FAZLA GENİŞ.
    assert all(
        any(kok in anahtar for kok in NAIF_MASKE_KOKLERI)
        for anahtar in ("wage_amount", "sgk_no", "tc_no", "birth_date", "iban")
    )
    # Sağlama: yedi bordro toplamı gerçekten oradadır.
    assert {"sgk_premium_total", "sgk_employer_total", "wage_type"} <= KASTEN_DISARIDA


def test_ESLESME_TAM_ANAHTAR_UZERINDENDIR() -> None:
    veri = {"sgk_premium_total": 1, "wage_type": "monthly", "bank_account_id": "x"}
    assert exposure.yasak_anahtarlar(veri) == []
    assert exposure.yasak_anahtarlar({"wage_amount": 1}) == ["wage_amount"]


# --- Kayıt anı: ŞEMA taraması ---------------------------------------------- #


class _IcIce(BaseModel):
    kimlik: uuid.UUID
    tc_no: str


class _DisSeviye(BaseModel):
    baslik: str
    kalemler: list[_IcIce]


class _TemizIcIce(BaseModel):
    kimlik: uuid.UUID
    kod: str


class _TemizDisSeviye(BaseModel):
    baslik: str
    kalemler: list[_TemizIcIce]


def test_sema_taramasi_IC_ICE_MODELLERI_de_gorur() -> None:
    """K1: "Bekçi iç içe modelleri de taramalı (liste içindeki nesne, ...)"."""
    assert "tc_no" in exposure.sema_anahtarlari(_DisSeviye)
    mutant = _spec("ic_ice", veri_modulleri=frozenset({"projects"}), yanit_modeli=_DisSeviye)
    with pytest.raises(IfsaIhlali, match="tc_no"):
        ToolRegistry((mutant,))


def test_POZITIF_KONTROL_temiz_ic_ice_sema_GECER() -> None:
    kayit = ToolRegistry(
        (
            _spec(
                "ic_ice_temiz", veri_modulleri=frozenset({"projects"}), yanit_modeli=_TemizDisSeviye
            ),
        )
    )
    assert kayit.tum_araclar[0].ad == "ic_ice_temiz"


def test_alan_maskesi_MODUL_SEVIYESINDEN_BAGIMSIZDIR() -> None:
    """`ACIK` bir modülün aracı da `wage_amount` taşıyamaz."""
    assert exposure.seviye("payroll") is IfsaSeviyesi.AGREGA
    mutant = _spec("maasli", veri_modulleri=frozenset({"payroll"}), yanit_modeli=_MaaslıBordro)
    with pytest.raises(IfsaIhlali, match="wage_amount"):
        ToolRegistry((mutant,))


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.ad)
def test_CANLI_araclarin_YANIT_SEMALARI_maske_TASIMAZ(spec: ToolSpec) -> None:
    anahtarlar = exposure.sema_anahtarlari(spec.yanit_modeli)
    assert anahtarlar & YASAK_ALAN_ANAHTARLARI == set()


# --- Çalışma anı: ZARF taraması (eşdeğer mutant DEĞİL) ---------------------- #


def test_SEMA_TARAMASI_SERBEST_SOZLUGU_GOREMEZ_bu_yuzden_IKINCI_KAPI_VAR() -> None:
    """🔴 İki kapının **eşdeğer olmadığının** kanıtı.

    `AiPuantajHaftasi.totals` `dict[str, Any]`dır ve `AiYetkilerim.permissions`
    `dict[str, str]`tir: bu alanların ANAHTARLARI şemada YOKTUR. Yani ucun
    gövdesine bir gün `wage_amount` girse kayıt anındaki tarama **sessiz
    kalırdı**. Çalışma anındaki tarama bu yüzden ikinci bir kapıdır, kopya
    değil.
    """
    anahtarlar = exposure.sema_anahtarlari(schemas.AiPuantajHaftasi)
    assert "totals" in anahtarlar
    assert "wage_amount" not in anahtarlar
    # Ama gövde taraması onu GÖRÜR:
    govde = {"totals": {"wage_amount": "12500.00"}}
    assert exposure.yasak_anahtarlar(govde) == ["wage_amount"]


@pytest.fixture
def _denetim_sussun(monkeypatch):
    async def _sahte(**kwargs):
        return None

    monkeypatch.setattr(ai_audit, "record_tool_call", _sahte)


async def _kos_arac(kayit: ToolRegistry, ad: str, transport) -> AracSonucu:
    return await kayit.invoke(
        arac_adi=ad,
        argumanlar={},
        actor=sahte_aktor(tam_izin()),
        transport=transport,
    )


@pytest.mark.asyncio
async def test_HUNI_calisma_aninda_sizan_anahtari_yakalar_ve_GOVDEYI_DUSURUR(
    _denetim_sussun, transport_factory
) -> None:
    async def _sizdir(ctx: AracBaglami, girdi: Any) -> AracSonucu:
        # Şemada anahtarı OLMAYAN serbest bir sözlük — kayıt kapısı bunu göremez.
        return Ok(data={"totals": [{"kisi": {"wage_amount": "12500.00"}}]}, row_count=1)

    kayit = ToolRegistry(
        (_spec("sizinti", veri_modulleri=frozenset({"timesheet"}), calistir=_sizdir),)
    )
    sonuc = await _kos_arac(kayit, "sizinti", transport_factory(bearer="x"))

    assert isinstance(sonuc, ToolError) and sonuc.kod == "alan_maskesi_ihlali"
    # 🔴 Gövde KISMEN temizlenmez, TAMAMEN düşer: `ToolError`da `data` yoktur.
    assert "veri" not in sonuc.govde()
    assert "12500.00" not in json.dumps(sonuc.govde(), ensure_ascii=False)


@pytest.mark.asyncio
async def test_POZITIF_KONTROL_temiz_serbest_sozluk_NORMAL_gecer(
    _denetim_sussun, transport_factory
) -> None:
    """K-IKIZ1: her gövdeyi düşüren bozuk bir kapı da yukarıdakini geçerdi."""

    async def _temiz(ctx: AracBaglami, girdi: Any) -> AracSonucu:
        return Ok(data={"totals": [{"kisi": {"gun": 22, "wage_type": "monthly"}}]}, row_count=1)

    kayit = ToolRegistry(
        (_spec("temiz", veri_modulleri=frozenset({"timesheet"}), calistir=_temiz),)
    )
    sonuc = await _kos_arac(kayit, "temiz", transport_factory(bearer="x"))

    assert isinstance(sonuc, Ok)
    assert sonuc.data["totals"][0]["kisi"]["gun"] == 22


@pytest.mark.asyncio
async def test_MUTASYON_calisma_ani_taramasi_SILINIRSE_sizinti_MODELE_GIDERDI(
    _denetim_sussun, transport_factory
) -> None:
    """Mutasyonun gerçekten kırıcı olduğunun kanıtı: tarama olmasaydı zarf
    `Ok` olur ve `wage_amount` `tool` mesajının içine girerdi."""

    async def _sizdir(ctx: AracBaglami, girdi: Any) -> AracSonucu:
        return Ok(data={"totals": {"wage_amount": "12500.00"}}, row_count=1)

    kayit = ToolRegistry(
        (_spec("sizinti2", veri_modulleri=frozenset({"timesheet"}), calistir=_sizdir),)
    )
    maskesiz = Ok(data={"totals": {"wage_amount": "12500.00"}}, row_count=1)
    assert "12500.00" in json.dumps(maskesiz.govde(), ensure_ascii=False)

    sonuc = await _kos_arac(kayit, "sizinti2", transport_factory(bearer="x"))
    assert isinstance(sonuc, ToolError)


def test_alan_maskesi_ihlali_HATA_METNI_KAYIT_YOK_DEMEZ() -> None:
    metin = guards.HATA_METINLERI["alan_maskesi_ihlali"]
    assert "kayıt yok" in metin and "DEĞİLDİR" in metin


# ############################################################################ #
# ②b DEĞER MASKESİ — ANAHTAR DEĞİL **DEĞERİN İÇİ** (KVKK borcu #13 + #15)
# ############################################################################ #
#
# 🔴 İki kapı da (kayıt anı ŞEMASI + çalışma anı ANAHTARI) bir şeyi göremez:
# `AiPlanGunu.text` / `AiPozKalemi.description` / `projects.name` **serbest
# metindir** ve içine yazılan TCKN/IBAN/telefon hiçbir anahtar kesişimine
# düşmez. Bu üçüncü kapı DEĞERİN İÇİNE bakar.
#
# 🔴 Neden DÜŞÜRMEZ, MASKELER: zarfı düşürmek yanlış pozitifte (sağlaması
# tesadüfen tutan bir numara) meşru bir aracı tümden kırardı. Maske yalnız
# eşleşen ALT DİZİYİ yutar; kalan cevap modele gider.

#: Sağlaması TUTAN bir TCKN (kontrol basamakları doğrulandı).
_GECERLI_TCKN = "11111111110"
#: 11 hane ama sağlaması TUTMAZ — fiş/sözleşme numarası emsali.
_GECERSIZ_11_HANE = "12345678901"
#: mod-97 TUTAN gerçek biçimli TR IBAN.
_GECERLI_IBAN = "TR330006100519786457841326"
#: Aynı IBAN'ın son hanesi bozulmuş hâli — mod-97 TUTMAZ.
_GECERSIZ_IBAN = "TR330006100519786457841327"


def test_DEGER_MASKESI_TCKNYI_YUTAR_ama_SAGLAMASI_TUTMAYAN_11_HANEYI_BIRAKIR() -> None:
    """ÇİFT YÖNLÜ. Tek yön yazılsaydı 'her 11 haneyi maskele' mutantı geçerdi."""
    maskeli = exposure.deger_maskesi({"text": f"usta {_GECERLI_TCKN} bugün gelmiyor"})
    assert _GECERLI_TCKN not in json.dumps(maskeli, ensure_ascii=False)
    assert exposure.MASKE_JETONU in maskeli["text"]
    assert "bugün gelmiyor" in maskeli["text"]

    temiz = {"text": f"fiş no {_GECERSIZ_11_HANE}"}
    assert exposure.deger_maskesi(temiz) == temiz


def test_DEGER_MASKESI_IBANI_YUTAR_ama_SAGLAMASI_TUTMAYANI_BIRAKIR() -> None:
    maskeli = exposure.deger_maskesi([f"ödeme {_GECERLI_IBAN} hesabına"])
    assert _GECERLI_IBAN not in maskeli[0]
    assert exposure.MASKE_JETONU in maskeli[0]

    bozuk = [f"ödeme {_GECERSIZ_IBAN} hesabına"]
    assert exposure.deger_maskesi(bozuk) == bozuk


def test_DEGER_MASKESI_TELEFONU_YUTAR_ama_PARAYI_BIRAKIR() -> None:
    maskeli = exposure.deger_maskesi({"not": "ara 0532 123 45 67 numarasından"})
    assert "0532" not in maskeli["not"]
    assert exposure.MASKE_JETONU in maskeli["not"]

    para = {"tutar": "12500.00", "gun": 22, "wage_type": "monthly"}
    assert exposure.deger_maskesi(para) == para


def test_DEGER_MASKESI_SERBEST_SOZLUGUN_ICINE_BAKAR_ve_KAYNAGI_BOZMAZ() -> None:
    """🔴 Şema taramasının kör noktası (`dict[str, Any]`) burada da geçerli;
    ayrıca maske KAYNAĞI DEĞİŞTİRMEZ (immutability)."""
    kaynak = {"totals": [{"aciklama": f"TC {_GECERLI_TCKN}"}]}
    maskeli = exposure.deger_maskesi(kaynak)
    assert _GECERLI_TCKN not in json.dumps(maskeli, ensure_ascii=False)
    assert kaynak["totals"][0]["aciklama"] == f"TC {_GECERLI_TCKN}"


@pytest.mark.asyncio
async def test_HUNI_DEGER_duzeyi_PIIyi_MASKELER_ve_ZARFI_DUSURMEZ(
    _denetim_sussun, transport_factory
) -> None:
    """Üretim yolu: `ToolRegistry.invoke` → zarf. Anahtar taraması burada
    KÖRDÜR (`text` yasak listede değildir)."""

    async def _sizdir(ctx: AracBaglami, girdi: Any) -> AracSonucu:
        return Ok(data={"days": [{"text": f"usta {_GECERLI_TCKN} gelmiyor"}]}, row_count=1)

    kayit = ToolRegistry(
        (_spec("deger_sizinti", veri_modulleri=frozenset({"timesheet"}), calistir=_sizdir),)
    )
    sonuc = await _kos_arac(kayit, "deger_sizinti", transport_factory(bearer="x"))

    # Anahtar kapısı bu gövdede hiçbir şey görmez — kanıt:
    assert exposure.yasak_anahtarlar({"days": [{"text": "x"}]}) == []
    # …ama değer kapısı görür ve zarf DÜŞMEZ, yalnız alt dize yutulur.
    assert isinstance(sonuc, Ok)
    assert _GECERLI_TCKN not in json.dumps(sonuc.govde(), ensure_ascii=False)
    assert "gelmiyor" in sonuc.govde()["veri"]["days"][0]["text"]


# ############################################################################ #
# ③ `scope_note` (S10) + `SIRKET_GENELI`nin İLK GERÇEK KULLANIMI
# ############################################################################ #


def test_KAPSAM_NOTLARI_ToolKumesi_ile_KUME_ESITLIGINDEDIR() -> None:
    """Yeni bir `ToolKumesi` üyesi eklenirse `KeyError` değil KIRMIZI test."""
    assert set(guards.KAPSAM_NOTLARI) == {u.value for u in ToolKumesi}


def test_UC_NOT_UC_AYRI_CUMLEDIR() -> None:
    """B18 emsali: hâlleri ayıran şey CÜMLENİN KENDİSİDİR."""
    notlar = list(guards.KAPSAM_NOTLARI.values())
    assert len(set(notlar)) == len(notlar)
    assert guards.KAPSAM_NOTU_BILINMEYEN not in notlar


def test_SIRKET_GENELI_NOTU_TERSINE_CEVRILEMEZ_oldugunu_SOYLER() -> None:
    """🔴 Ölçülmüş ince nokta: `equipment/repository.py::scope` ve
    `inventory/repository.py::_warehouse_scope` **OR**'ludur — `site_id IS NULL`
    kapsam süzgecine tabi değildir. Yani "boş küme = kapsamında yok" cümlesinin
    TERSİ geçerli değildir: **dolu küme kapsam iznini KANITLAMAZ.** Not bunu
    yanlış vaat etmemelidir.
    """
    metin = guards.KAPSAM_NOTLARI["sirket_geneli"]
    assert "KANITLAMAZ" in metin
    assert "ŞİRKET GENELİ" in metin


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.ad)
def test_her_arac_SONUCU_KAPSAM_NOTU_tasir(spec: ToolSpec) -> None:
    govde = REGISTRY.mesaj_govdesi(spec.ad, Empty())
    assert govde["kapsam_notu"] == guards.KAPSAM_NOTLARI[spec.kume.value]


def test_bilinmeyen_arac_UCUNCU_NOTU_alir_SESSIZCE_ATLANMAZ() -> None:
    govde = REGISTRY.mesaj_govdesi("boyle_bir_arac_yok", ToolError("bilinmeyen_arac"))
    assert govde["kapsam_notu"] == guards.KAPSAM_NOTU_BILINMEYEN


# --- `SIRKET_GENELI` beyanının ÖLÇÜMLE bağlanması --------------------------- #


def test_gosterge_ozeti_SIRKET_GENELI_beyan_eder() -> None:
    """`SIRKET_GENELI` beyan eden araçların TAM kümesi.

    🔴 AI-2b/2d'de küme BİRDEN BEŞE çıktı ve **sessizce değil, ADIYLA**. Her
    üyenin gerekçesi ayrı ayrı ölçüldü ve `test_ai2bd_araclar.py` her birini
    kaynak zinciriyle kilitler:

    * `gosterge_ozeti` — `inventory._warehouse_scope` OR'lu,
    * `makine_listesi` — `equipment.repository.scope` OR'lu,
    * `makine_calisma` — `equipment.repository.work_log_scope` OR'lu,
    * `makine_yakit`  — `equipment.repository.fuel_log_scope` OR'lu,
    * `makine_kira`   — `equipment.rental_repository.invoice_scope` OR'lu,
    * `taseronlar`    — kapsam süzgeci **HİÇ YOK** (uç `user` bile almaz).

    ⚠️ Altı gerekçe, beş+bir araç: `taseronlar`ın gerekçesi ötekilerden
    FARKLIDIR (OR dalı değil, süzgecin yokluğu) ve bu fark kayda geçsin diye
    burada ayrı yazılır.
    """
    assert GOSTERGE_OZETI.kume is ToolKumesi.SIRKET_GENELI
    beyan_edenler = {s.ad for s in CATALOG if s.kume is ToolKumesi.SIRKET_GENELI}
    assert beyan_edenler == {
        "gosterge_ozeti",
        "taseronlar",
        "makine_listesi",
        "makine_calisma",
        "makine_yakit",
        "makine_kira",
    }


def test_BEYANIN_GEREKCESI_HALA_KODDA_DURUYOR() -> None:
    """🔴 Beyan bir ölçüme dayanır; ölçüm değişirse beyan yeniden düşünülmeli.

    Zincir: `/dashboard/summary` → `dashboard/service.py::_risks` →
    `dashboard/risks.py::_stock_alerts` → `inventory.repository
    .visible_warehouse_ids` → `_warehouse_scope`, ve o süzgeç **OR**'ludur:
    merkez depo (`site_id IS NULL`) kapsam süzgecine TABİ DEĞİLDİR.

    Bu test S30'un pozitif kontrolünün kardeşidir: iddiayı ROUTER'da değil
    **çağrı zinciri boyunca** ölçer. Biri OR dalını silerse (araç gerçekten
    proje kapsamlı olurdu) test kırmızı olur ve `kume` beyanı bilinçli olarak
    yeniden kararlaştırılmak zorunda kalır.
    """
    from app.modules.dashboard import risks as dashboard_risks
    from app.modules.inventory import repository as inventory_repository

    risk_kaynagi = Path(dashboard_risks.__file__).read_text(encoding="utf-8")
    assert "visible_warehouse_ids" in risk_kaynagi, (
        "Risk kartı artık depo kapsamını çağırmıyor — `gosterge_ozeti.kume` yeniden ölçülmeli."
    )

    depo_kaynagi = Path(inventory_repository.__file__).read_text(encoding="utf-8")
    govde = depo_kaynagi[depo_kaynagi.index("def _warehouse_scope") :]
    govde = govde[: govde.index("def list_warehouses")]
    assert "Warehouse.site_id.is_(None)" in govde and "|" in govde, (
        "`_warehouse_scope` artık OR'lu değil: merkez depo kapsam süzgecine "
        "girmiş olabilir. `gosterge_ozeti` gerçekten proje kapsamlı hâle "
        "geldiyse `kume` beyanı DEĞİŞMELİDİR — bu bilinçli bir karardır."
    )


def test_IKIZ_OR_DALI_equipmentta_da_DURUYOR() -> None:
    """Desen tek değil ikizdir; AI-2b/c bu tuzağa aynı şekilde girecek."""
    from app.modules.equipment import repository as equipment_repository

    kaynak = Path(equipment_repository.__file__).read_text(encoding="utf-8")
    govde = kaynak[kaynak.index("def scope(") :]
    govde = govde[: govde.index("def _filtered")]
    assert "Equipment.site_id.is_(None)" in govde and "|" in govde


# --- ÇAĞRI YERİ de mutanttır: notu döngü üzerinden ölç ---------------------- #


class _SahteSaglayici:
    """Ağa çıkmaz. `test_ai1_dongu.py`deki emsalin küçük hâli."""

    ad = "sahte"

    def __init__(self, senaryolar: list[list[AiOlay]]) -> None:
        self._senaryolar = list(senaryolar)
        self.cagrilar: list[dict[str, Any]] = []

    def arac_semasi(self, spec: ToolSpec) -> dict[str, Any]:
        return {"type": "function", "function": {"name": spec.ad}}

    async def tur(
        self, *, sistem: str, gecmis: Sequence[Mesaj], araclar: Sequence[ToolSpec]
    ) -> AsyncIterator[AiOlay]:
        self.cagrilar.append({"sistem": sistem, "gecmis": list(gecmis)})
        senaryo = (
            self._senaryolar.pop(0)
            if self._senaryolar
            else [TurBitti(sebep=TurSebebi.bitti, kullanim=Kullanim())]
        )
        for olay in senaryo:
            yield olay


class _SabitOturum:
    def __init__(self, session) -> None:
        self._s = session

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *_):
        return False


@pytest.mark.asyncio
async def test_KAPSAM_NOTU_MODELE_FIILEN_ULASIR_cagri_yeri_dahil(
    db_session, okuma_duzlemi, monkeypatch, user_factory, seeded_db, _denetim_sussun
) -> None:
    """🔴 ÇAĞRI YERİ DE MUTANTTIR.

    `registry.mesaj_govdesi`i testte doğrudan çağırmak, `loop._arac_mesaji`den
    o çağrıyı SİLEN mutantı öldürmez. Bu yüzden iddia **gerçek döngü**
    üzerinden kurulur: sağlayıcıya fiilen giden `arac` rolündeki mesajın
    gövdesinde not var mı?
    """
    monkeypatch.setattr(ai_loop, "AiSessionLocal", lambda: _SabitOturum(db_session))
    user = await user_factory("ai2a-kapsam@fiil.example.com", "Sifre1234!", "site_chief")
    bearer = create_access_token(user.id, user.token_version)
    seeded_db.expunge(user)

    async def _sirket_geneli(ctx: AracBaglami, girdi: Any) -> AracSonucu:
        return Ok(data={"satir": 3}, row_count=1)

    kayit = ToolRegistry(
        (
            _spec(
                "sirket_sondasi",
                veri_modulleri=frozenset({"inventory"}),
                kume=ToolKumesi.SIRKET_GENELI,
                calistir=_sirket_geneli,
            ),
        )
    )
    saglayici = _SahteSaglayici(
        [
            [
                AracCagrisiHazir(cagri_id="c1", arac_adi="sirket_sondasi", argumanlar={}),
                TurBitti(sebep=TurSebebi.arac, kullanim=Kullanim()),
            ],
            [MetinParcasi(metin="ok"), TurBitti(sebep=TurSebebi.bitti, kullanim=Kullanim())],
        ]
    )
    istemci = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=okuma_duzlemi, raise_app_exceptions=True),
        base_url="http://ai-okuma",
    )
    [
        o
        async for o in ajan_turu(
            kayit=kayit,
            saglayici=saglayici,
            okuma_duzlemi_istemcisi=istemci,
            bearer=bearer,
            kullanici_mesaji="soru",
        )
    ]

    arac_mesajlari = [m for m in saglayici.cagrilar[1]["gecmis"] if m.rol == "arac"]
    assert len(arac_mesajlari) == 1
    govde = json.loads(arac_mesajlari[0].icerik)
    assert govde["kapsam_notu"] == guards.KAPSAM_NOTLARI["sirket_geneli"]
    # Ve not gerçekten AYIRT EDİCİ: proje kapsamlı not BAŞKA bir cümledir.
    assert govde["kapsam_notu"] != guards.KAPSAM_NOTLARI["proje_kapsamli"]


def test_PROMPT_kapsam_notunu_KULLANMAYI_emreder() -> None:
    """Not basılıyor ama modele ne yapacağı söylenmiyorsa korkuluk yarımdır."""
    from app.modules.ai.prompt import BASLIK

    assert "KAPSAM" in BASLIK
    assert "şirket geneli" in BASLIK and "KANITLAMAZ" in BASLIK


# ############################################################################ #
# ④ SAYFALAMASIZ UÇ — DÜRÜST `Truncated` + FastAPI'nin SESSİZ YUTMASI
# ############################################################################ #


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


def _get_rotalari() -> dict[str, APIRoute]:
    return {r.path: r for r in _api_rotalari(app.routes) if "GET" in (r.methods or set())}


def _sorgu_parametreleri(yol: str) -> set[str]:
    """Ucun **TEL ÜZERİNDE** kabul ettiği sorgu parametresi adları.

    🔴 **DÜZELTİLDİ (AI-2b) — bekçi yanlış ekseni ölçüyordu.** Eski hâli
    `p.name` okuyordu, yani PYTHON adını; FastAPI ise bir `alias` varsa
    **yalnız alias'ı** tanır. Ölçüldü (`/contracts`):

        NAMES   = {'contract_type', 'project_id', 'q', 'status_filter'}
        ALIASES = {'type',          'project_id', 'q', 'status'}

    Yani eski bekçi, tel üzerinde **DOĞRU** olan `params={'type': …}`ı
    "bildirilmemiş parametre" diye kırmızıya çevirir; tel üzerinde
    **SESSİZCE DÜŞECEK** olan `params={'contract_type': …}`ı ise yeşil
    geçirirdi. Bu, bekçinin önlemeye çalıştığı hatanın ta kendisidir
    (FastAPI bilinmeyen parametreyi 422 ile reddetmez, **yutar**).

    İki okumanın eşdeğer OLMADIĞI ayrıca kilitlidir:
    `test_ALIAS_ile_PYTHON_ADI_AYRISAN_bir_uc_GERCEKTEN_VAR`.
    """
    rota = _get_rotalari()[yol]
    return {p.alias for p in rota.dependant.query_params}


def _python_adlari(yol: str) -> set[str]:
    """Aynı ucun PYTHON adları — yalnız iki okumanın farkını ölçmek için."""
    rota = _get_rotalari()[yol]
    return {p.name for p in rota.dependant.query_params}


def test_ALIAS_ile_PYTHON_ADI_AYRISAN_bir_uc_GERCEKTEN_VAR() -> None:
    """🔴 POZİTİF KONTROL: iki okuma **eşdeğer değildir**.

    Alias ile python adı hiçbir uçta ayrışmasaydı yukarıdaki düzeltme eşdeğer
    bir mutant olurdu ve bu test onu söylerdi. Ölçüm `/contracts`tadır ve
    ZORUNLU parametrenin ta kendisidir — yani ayrışma teorik değil, bu dilimin
    `sozlesmeler` aracını doğrudan etkileyen bir olgudur.
    """
    tel = _sorgu_parametreleri("/contracts")
    python = _python_adlari("/contracts")
    assert tel != python, "alias/ad ayrışması kayboldu — düzeltme eşdeğer mutant olurdu"
    assert "type" in tel and "type" not in python
    assert "contract_type" in python and "contract_type" not in tel


def test_MUTASYON_PYTHON_ADINI_gonderen_handler_YENI_BEKCIDE_YAKALANIR() -> None:
    """Yeni hâlin bir şey ÖLÇTÜĞÜNÜN kanıtı — ve eski hâlin körlüğünün.

    `params={'contract_type': …}` tel üzerinde SESSİZCE düşerdi ve uç zorunlu
    `type`ı bulamayıp 422 verirdi. Eski bekçi (python adı okuyan) bunu YEŞİL
    geçirirdi; yeni bekçi kırmızı yapar.
    """
    mutant = (
        "async def sozlesmeler(ctx, girdi):\n"
        "    return await ctx.get(params={'contract_type': girdi.contract_type})\n"
    )
    gonderilen = _handlerin_gonderdigi_params(mutant)["sozlesmeler"]
    assert sorted(gonderilen - _sorgu_parametreleri("/contracts")) == ["contract_type"]
    # 🔴 Ve eski okuma bu mutantı GÖRMEZDİ:
    assert gonderilen - _python_adlari("/contracts") == set()


SAYFALAMASIZ_UCLAR = ("/progress-payments", "/contracts", "/subcontractors")


@pytest.mark.parametrize("yol", SAYFALAMASIZ_UCLAR)
def test_UC_SAYFALAMASIZ_UC_limit_BILDIRMEZ(yol: str) -> None:
    """Premise'in kendisi — koddan ölçülür, emirden alınmaz."""
    parametreler = _sorgu_parametreleri(yol)
    assert "limit" not in parametreler and "offset" not in parametreler, (
        f"`{yol}` artık sayfalama bildiriyor: {sorted(parametreler)}. "
        "Kalıbın gerekçesi değişmiş olabilir; yeniden ölç."
    )


def test_POZITIF_KONTROL_SAYFALAYAN_uclar_GERCEKTEN_VAR() -> None:
    """İddia boş bir kümede dolaşmıyor: `/projects` ve `/approvals` bildirir."""
    assert {"limit", "offset"} <= _sorgu_parametreleri("/projects")
    assert {"limit", "offset"} <= _sorgu_parametreleri("/approvals")


@pytest.mark.asyncio
async def test_FASTAPI_BILINMEYEN_SORGU_PARAMETRESINI_SESSIZCE_YUTAR(
    seeded_db, user_factory, transport_factory
) -> None:
    """🔴 KALIBIN VAR OLUŞ SEBEBİ — ve bugüne kadar bekçisi YOKTU.

    Handler `params={"limit": 200}` gönderir; `/subcontractors` `limit` diye bir
    parametre **bilmez**. FastAPI bunu 422 ile reddetmez, **SESSİZCE YOK
    SAYAR** — yani handler tavan uyguladığını *sanır*, uç bütün satırları
    döner ve `Truncated` hiç kurulmaz.

    Bu test gerçek uygulamada, gerçek kimlikle, gerçek veriyle ölçer.
    """
    from app.modules.contracts.models import Subcontractor

    seeded_db.add_all([Subcontractor(name="AI2A Taşeron 1"), Subcontractor(name="AI2A Taşeron 2")])
    await seeded_db.flush()

    # `contracts:view` taşıyan tohumlu rol: `accounting` (ölçüldü, MATRIX).
    user = await user_factory("ai2a-yut@fiil.example.com", "Sifre1234!", "accounting")
    bearer = create_access_token(user.id, user.token_version)
    seeded_db.expunge(user)

    tasima = transport_factory(bearer=bearer)
    yanit = await tasima.get(
        "/subcontractors",
        izinli_desenler=("/subcontractors",),
        params={"limit": 1, "offset": 0},
    )

    assert yanit.status_code == 200, (
        f"Beklenen 200; gelen {yanit.status_code}. Uç bilinmeyen parametreyi "
        "REDDETMEYE başladıysa kalıbın gerekçesi değişti."
    )
    kalemler = yanit.json()["items"]
    assert len(kalemler) >= 2, (
        "`limit=1` gönderildi ve uç YİNE DE birden fazla satır döndü — "
        "parametre sessizce yutuldu. Kalıbın kanıtı budur."
    )


@pytest.mark.asyncio
async def test_POZITIF_KONTROL_limit_BILDIREN_ucta_tavan_ISLER(
    seeded_db, user_factory, project_factory, transport_factory
) -> None:
    """K-IKIZ1: uçların hepsi tavanı yok saysaydı yukarıdaki test de geçerdi."""
    from app.modules.users.models import ProjectMember

    a = await project_factory(code="AI2A-1", name="Bir")
    b = await project_factory(code="AI2A-2", name="Iki")
    user = await user_factory("ai2a-tavan@fiil.example.com", "Sifre1234!", "site_chief")
    seeded_db.add_all(
        [
            ProjectMember(user_id=user.id, project_id=a.id, role_id=user.role_id),
            ProjectMember(user_id=user.id, project_id=b.id, role_id=user.role_id),
        ]
    )
    await seeded_db.flush()
    bearer = create_access_token(user.id, user.token_version)
    seeded_db.expunge(user)

    tasima = transport_factory(bearer=bearer)
    yanit = await tasima.get(
        "/projects", izinli_desenler=("/projects",), params={"limit": 1, "offset": 0}
    )
    assert yanit.status_code == 200
    govde = yanit.json()
    assert len(govde["items"]) == 1 and govde["total"] >= 2, (
        "`/projects` tavanı uygulamıyor — o zaman ④'ün ayrımı anlamsızlaşır."
    )


# --- Yapısal bekçi: handler'ın gönderdiği params ⊆ ucun bildirdiği ---------- #


def _handlerin_gonderdigi_params(kaynak: str) -> dict[str, set[str]]:
    """Handler adı → `params=` ile gönderilen sözlük anahtarları (AST).

    🔴 Hesaplanmış (sabit olmayan) bir anahtar `<HESAPLANMIS>` olarak
    işaretlenir ve **SKIP değil KIRMIZI** üretir: "çıkaramadım" ile "yok"
    farklı iki şeydir (`UNGATED_ALLOWLIST` emsali).
    """
    cikti: dict[str, set[str]] = {}
    for dugum in ast.parse(kaynak).body:
        if not isinstance(dugum, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        gonderilen: set[str] = set()
        for alt in ast.walk(dugum):
            if not isinstance(alt, ast.Call):
                continue
            for kw in alt.keywords:
                if kw.arg != "params":
                    continue
                if not isinstance(kw.value, ast.Dict):
                    gonderilen.add("<HESAPLANMIS>")
                    continue
                for anahtar in kw.value.keys:
                    if isinstance(anahtar, ast.Constant) and isinstance(anahtar.value, str):
                        gonderilen.add(anahtar.value)
                    else:
                        gonderilen.add("<HESAPLANMIS>")
        cikti[dugum.name] = gonderilen
    return cikti


def _handler_kaynaklari() -> list[Path]:
    """`tools/reads/` altındaki **TÜM** handler modülleri.

    🔴 **DÜZELTİLDİ (AI-2b).** Bekçi tek bir dosyayı (`handlers.py`) okuyordu.
    `handlers.py` 800 satır tavanını aşınca ikiye bölündü (`ai2bd.py`) ve tek
    dosya okuyan bir bekçi **on altı handler'ın hiçbirini görmezdi** — sessizce.
    Bu, §3.9'daki `SUNUCULAR` deliğinin birebir ikizi olurdu.
    """
    return sorted((AI_KOK / "tools" / "reads").glob("*.py"))


def _tum_handler_paramlari() -> dict[str, set[str]]:
    birlesik: dict[str, set[str]] = {}
    for yol in _handler_kaynaklari():
        birlesik.update(_handlerin_gonderdigi_params(yol.read_text(encoding="utf-8")))
    return birlesik


def test_POZITIF_KONTROL_TARANAN_handler_dosyalari_IKIDEN_AZ_DEGIL() -> None:
    """Tarama kümesi boş ya da eksik kalırsa bekçi sessizce körleşir."""
    adlar = {y.name for y in _handler_kaynaklari()}
    assert {"handlers.py", "ai2bd.py"} <= adlar, adlar


def test_POZITIF_KONTROL_AST_cikaricisi_GERCEKTEN_params_BULUYOR() -> None:
    """Çıkarıcı boş dönseydi aşağıdaki bekçi hiçbir şey ölçmezdi.

    İddia **her iki** handler modülünden birer örnek taşır: biri düşerse test
    kırmızı olur ve tarama kümesinin daraldığı anlaşılır.
    """
    bulunan = _tum_handler_paramlari()
    assert bulunan.get("projeleri_listele") == {"limit", "offset"}
    assert bulunan.get("puantaj_haftasi") == {"iso_year", "iso_week"}
    # AI-2b tarafı — ALIAS'lı ve ZORUNLU parametrenin ta kendisi.
    assert bulunan.get("sozlesmeler") == {"type"}
    # 🔴 Zorunlu parametreli DÖRT aracın dördü de ADIYLA sayılır: biri sessizce
    # `BosGirdi`ye düşerse (ya da parametresini göndermeyi bırakırsa) araç HER
    # çağrıda 422 alırdı ve bu, çıkarıcı "boş küme" dönerken FARK EDİLMEZDİ.
    assert bulunan.get("puantaj") == {"year", "month"}
    assert bulunan.get("gun_plani") == {"start"}
    assert bulunan.get("makine_calisma") == {"year", "month"}
    assert bulunan.get("makine_yakit") == {"year", "month"}
    # 🔴 Sayfalamasız uç `limit` GÖNDERMEZ (FastAPI onu sessizce yutardı).
    assert bulunan.get("isveren_hakedisleri") == set()
    # 🔴 `taseronlar` params GÖNDERİR ama `limit` DEĞİL: ucun `active_only`
    # varsayılanı `True`dur ve devralmak kümeyi sessizce süzerdi.
    assert bulunan.get("taseronlar") == {"active_only"}


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.ad)
def test_HANDLERIN_GONDERDIGI_PARAMS_UCUN_BILDIRDIGI_KUMENIN_ICINDEDIR(spec: ToolSpec) -> None:
    """🔴 Kalıbın yaptırımı. Bugün üç handler de geçer; bir gün `/contracts`ı
    saran biri `limit` gönderirse bu test onu **kod yazarken** yakalar."""
    if not spec.ucler:
        pytest.skip("veri okumayan araç")
    gonderilen = _tum_handler_paramlari().get(spec.calistir.__name__, set())
    bildirilen = _sorgu_parametreleri(spec.ucler[0])
    assert "<HESAPLANMIS>" not in gonderilen, (
        f"`{spec.ad}` handler'ı hesaplanmış bir `params` sözlüğü gönderiyor; "
        "bekçi anahtarları çıkaramadı. Bu SKIP değil KIRMIZIDIR."
    )
    fazlalik = sorted(gonderilen - bildirilen)
    assert fazlalik == [], (
        f"`{spec.ad}` `{spec.ucler[0]}` ucuna, ucun BİLDİRMEDİĞİ parametre "
        f"gönderiyor: {fazlalik}. FastAPI bunu 422 ile reddetmez, SESSİZCE "
        "YUTAR — handler tavan uyguladığını sanır. Dürüst çözüm: "
        "`result.sayfalamasiz_liste_sonucu`."
    )


def test_MUTASYON_bildirilmemis_parametre_gonderen_handler_YAKALANIR() -> None:
    """Bekçinin eşdeğer olmadığının kanıtı: aynı çıkarıcı, ihlalli kaynakta."""
    mutant = (
        "async def taseronlari_listele(ctx, girdi):\n"
        "    yanit = await ctx.get(params={'limit': 200, 'offset': 0})\n"
        "    return yanit\n"
    )
    gonderilen = _handlerin_gonderdigi_params(mutant)["taseronlari_listele"]
    bildirilen = _sorgu_parametreleri("/subcontractors")
    assert sorted(gonderilen - bildirilen) == ["limit", "offset"]


def test_MUTASYON_hesaplanmis_params_SKIP_degil_KIRMIZI_uretir() -> None:
    mutant = "async def x(ctx, girdi):\n    return await ctx.get(params=dict(limit=1))\n"
    assert _handlerin_gonderdigi_params(mutant)["x"] == {"<HESAPLANMIS>"}


# --- Dürüst `Truncated` kalıbı ---------------------------------------------- #


def test_sayfalamasiz_liste_sonucu_TOTALI_OLCER_UYDURMAZ() -> None:
    sonuc = sayfalamasiz_liste_sonucu([{"i": n} for n in range(7)], tavan=3)
    assert isinstance(sonuc, Truncated)
    assert sonuc.total == 7 and sonuc.returned == 3
    assert len(sonuc.data) == 3
    assert "7" in sonuc.mesaj() and "3" in sonuc.mesaj()


def test_sayfalamasiz_liste_sonucu_TOTAL_PARAMETRESI_ALMAZ() -> None:
    """🔴 Uydurulmuş bir toplam yapısal olarak imkânsız: fonksiyon onu ALMAZ."""
    imza = inspect.signature(sayfalamasiz_liste_sonucu)
    assert "total" not in imza.parameters
    assert set(imza.parameters) == {"tum_satirlar", "tavan", "kapsam_modulu"}


def test_sayfalamasiz_liste_sonucu_TAM_KUMEDE_uyari_BASMAZ() -> None:
    """B19'un pozitif kontrolü: tam küme dönerken `Truncated` KURULMAZ."""
    sonuc = sayfalamasiz_liste_sonucu([{"i": 1}, {"i": 2}], tavan=10)
    assert isinstance(sonuc, Ok) and sonuc.row_count == 2


def test_sayfalamasiz_liste_sonucu_BOS_KUMEDE_kapsam_hâlini_KORUR() -> None:
    assert isinstance(sayfalamasiz_liste_sonucu([], tavan=10), Empty)
    assert isinstance(
        sayfalamasiz_liste_sonucu([], tavan=10, kapsam_modulu="contracts"), ScopedEmpty
    )


def test_MUTASYON_tavani_ONCE_uygulayan_bir_kalip_TOPLAMI_KAYBEDER() -> None:
    """🔴 Yanlış sıranın neden yalan ürettiğinin kanıtı.

    Ucun `limit`ini onurlandırdığını sanan kalıp, eline 3 satır alır ve
    `total`ı 3 sanar → `Truncated` HİÇ kurulmaz, model 7 kaydın 3'ünden
    toplam hesaplar ve bunu bilmez.
    """
    tum = [{"i": n} for n in range(7)]
    yanlis = tum[:3]
    assert len(yanlis) == 3  # "uç zaten kırptı" varsayımı
    from app.modules.ai.result import liste_sonucu

    assert isinstance(liste_sonucu(data=yanlis, total=len(yanlis)), Ok)  # ← YALAN
    assert isinstance(sayfalamasiz_liste_sonucu(tum, tavan=3), Truncated)  # ← DOĞRU


def test_sayfalamasiz_kalip_HANDLERLARIN_ULASABILECEGI_YERDEDIR() -> None:
    """Kalıp `result.py`dedir — `Truncated` kurma kararının TEK YERİ orasıdır."""
    from app.modules.ai import result as result_modulu

    assert sayfalamasiz_liste_sonucu.__module__ == result_modulu.__name__


# ############################################################################ #
# Yaptırımın DEKORATİF olmadığının son kilidi
# ############################################################################ #


def test_YAPTIRIM_URETIM_KODUNDA_kosuyor_TESTTE_DEGIL() -> None:
    """🔴 `YONETISIM_DENYLIST`in ölçülmüş kaderi: bugüne kadar onu okuyan tek
    yer bir TEST dosyasıydı. `ai_exposed` aynı yere düşmesin diye yaptırım
    `ToolRegistry.__init__`tedir ve bu test onu kaynaktan doğrular.
    """
    kaynak = (AI_KOK / "registry.py").read_text(encoding="utf-8")
    agac = ast.parse(kaynak)
    init = next(
        d for d in ast.walk(agac) if isinstance(d, ast.FunctionDef) and d.name == "__init__"
    )
    assert "dogrula_spec" in ast.unparse(init), (
        "`ToolRegistry.__init__` artık `exposure.dogrula_spec` çağırmıyor — "
        "kapı dekoratif hâle gelmiş olabilir."
    )


def test_YONETISIM_DENYLISTI_ARTIK_URETIM_KODUNDA_da_kosuyor() -> None:
    """B26 kod tarafına taşındı: `kapilar`da yönetişim modülü olan araç
    kaydedilemez. Pozitif kontrol: `onay_kutum` (`kapilar=∅`) takılmaz."""
    mutant = _spec(
        "rol_okuyucu",
        veri_modulleri=frozenset({"roles", "settings"}),
        kapilar=frozenset({("settings", AccessLevel.view)}),
        ucler=("/settings/notifications",),
    )
    with pytest.raises(IfsaIhlali, match="denylist"):
        ToolRegistry((mutant,))
    # Pozitif kontrol — gerçek katalog kaydedilebiliyor.
    assert ToolRegistry(READ_TOOLS).tum_araclar == READ_TOOLS

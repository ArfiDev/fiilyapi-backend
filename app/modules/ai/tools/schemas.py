"""Araçların **girdi** ve **yanıt** modelleri.

🔴 **ORM ASLA DÖNMEZ** (`ToolSpec.yanit_modeli` zorunlu). Ve bu modeller ucun
kendi `response_model`ının kopyası da DEĞİLDİR: araç, ucun döndürdüğü gövdeyi
**daraltır**. Daraltmanın iki gerekçesi var ve ikisi de ölçüme dayanır:

1. **Alan maskesi VARSAYILANDIR** (§9-A1 fail-closed önerisi). Uç `tc_no`,
   `iban`, `wage_amount` taşıyorsa ve araç gövdeyi düz geçirirse o alanlar
   sağlayıcıya gider. AI-0b'nin dört aracı PII taşıyan uçlara bakmıyor ama
   **desen şimdi kurulur**, sonra değil.
2. Token bütçesi: 140 GET'in gövdeleri geniştir; model kararı için gereken
   alanlar dardır.

🔴 **`MetricPlaceholder`ın ÜÇ HÂLİ DÜZLEŞTİRİLMEZ** (S25/B18). `value or 0`
yazmak üç ayrı gerçeği ("değer 12", "modül henüz yazılmadı", "yetkin yok") tek
sayıya indirir. Burada üçü **üç ayrı sabit dizeye** çevrilir.

## 🔴 KAPSAM MASKESİ — daraltma şeması ucun NULL'ını KABUL ETMEK ZORUNDADIR

(IZN-B4/B6a: eski kapsam maskesi (`field_scope`, `scoped_route`, `kapsam_rotasi`,
`kapsam_kapisi`) SÖKÜLDÜ; yerine `core/mask_route` + `core/field_mask`.
Aşağıdaki metin TARİHSEL gerekçedir (maskeli alan yine `null` gelir).)

2026-09-19'dan beri altı modülde alan düzeyi kapsam maskesi vardır
(`core/field_scope`): `limited` PARA kovasını, `finance` OPERASYONEL kovayı
`null`a çeker. **Bu maske AI hattında da koşar** — araçlar servisi değil UCU
sarar (`ReadOnlyTransport`) ve `build_read_plane` ana uygulamanın ORİJİNAL
rota nesnelerini taşır, yani `kapsam_rotasi` sarmalayıcısı da gelir.

Sonuç bağlayıcıdır: **üst kaynakta `Decimal | None` olan bir alan burada
`Decimal` ZORUNLU olamaz.** Olursa maskeli rolde `ValidationError` doğar,
huninin son dalı (`registry.invoke`, 6a) onu `ust_kaynak_hatasi`na düşürür ve
kullanıcı gizlenmiş bir alan yüzünden "sistem hatası" görür. Ürünün kararı
alanı GİZLEMEKTİ, aracı öldürmek değil.

🔴 Ve boşluk `0` ile DOLDURULMAZ: sıfır "bilinmiyor" demez, "sıfır" der —
sessizce yanlış bir sayıdır ve gizlemekten kötüdür. Maskeli bir bileşeni
ATLAYIP toplam hesaplamak da yasaktır: eksik bir toplamı gerçek gibi basmak
aynı yalanın kart hâlidir.

Bekçisi `tests/modules/ai/test_p8_kapsam_maskesi.py`dir ve araç listesini ELLE
DEĞİL rota tablosundan çıkarır: yedinci modül kısıtlandığı gün yeni araç
kendiliğinden ölçüme girer.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.ai.navigation import EkranAnahtari

# --------------------------------------------------------------------------- #
# `MetricPlaceholder` üç hâli — SABİT metinler (B18: bayt eşitliği)
# --------------------------------------------------------------------------- #

#: `available=False` + `pending_module` DOLU — alan henüz BAĞLANMADI.
MODUL_BEKLIYOR: Final[str] = "Bu değer henüz bağlanmadı (bekleyen modül: {modul})."
#: `available=False` + `pending_module is None` — 🔴 ROLÜN İZNİ YOK (üçüncü hâl).
IZIN_YOK: Final[str] = "Bu değeri görme yetkiniz yok."
#: 🔴 Sayısal düzleştirme YASAK: `value` `None` iken de `available` `True`
#: olabilir ("hesaplandı, sonuç yok").
DEGER_YOK: Final[str] = "Hesaplandı ama bir değer üretmedi."


def metrik_metni(zarf: dict[str, Any] | None) -> str:
    """`MetricPlaceholder` gövdesini **üç ayrı cümleden birine** çevirir.

    Okuma `available` bayrağından yapılır — `projects/schemas.py` açıkça
    *"çıplak `MetricPlaceholder()` artık ValidationError ATMAZ"* der, yani
    üçüncü hâl yapısal olarak zorlanmıyor, bir **disiplindir**.
    """
    if zarf is None:
        return IZIN_YOK
    if zarf.get("available"):
        deger = zarf.get("value")
        return DEGER_YOK if deger is None else str(deger)
    bekleyen = zarf.get("pending_module")
    return IZIN_YOK if bekleyen is None else MODUL_BEKLIYOR.format(modul=bekleyen)


# --------------------------------------------------------------------------- #
# GİRDİ modelleri
# --------------------------------------------------------------------------- #


class BosGirdi(BaseModel):
    """Parametresiz araçlar. `extra="forbid"`: model uydurduğu bir alanı
    sessizce geçiremez (S21'in okuma tarafı)."""

    model_config = ConfigDict(extra="forbid")


class PuantajHaftasiGirdi(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 🔴 TİPLİ (S27). `str` olsaydı `..` argümanı tip katmanından geçerdi ve
    #: yalnız nokta-segment reddi kalırdı; iki kilit birden istiyoruz.
    #: 🔴 AI-BAĞLAM: **İSTEĞE BAĞLI**. Boş bırakılırsa `ToolRegistry.invoke`
    #: sohbet bağlamındaki kapsamı doldurur; model açıkça verirse bağlamı EZER.
    #: Zorunlu bırakılsaydı bu doldurma HİÇ ateşlenemezdi: sağlayıcı şeması
    #: `strict`tir ve `girdi_semasi` her alanı `required` yapar, yani model alanı
    #: atlayamaz — yalnız `null` gönderebilir. İkisi de yoksa huni dürüst bir
    #: `kapsam_gerekli` döner ("kayıt yok" DEMEZ).
    site_id: uuid.UUID | None = None
    iso_year: int = Field(ge=2000, le=2100)
    iso_week: int = Field(ge=1, le=53)


class YonlendirGirdi(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: KAPALI ENUM (S22). Serbest `str` DEĞİL.
    ekran: EkranAnahtari


# --------------------------------------------------------------------------- #
# YANIT modelleri
# --------------------------------------------------------------------------- #


class AiProje(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    status: str
    type: str
    #: 🔴 FİNANSAL ilerleme (`ProjectListItem.progress_pct`), FİZİKSEL DEĞİL —
    #: `projects/schemas.py` ikisini KASTEN ayırır. `None` bırakılabilir: uç
    #: alanı taşımazsa 0 yazmak "hiç ilerlemedi" demek olurdu (uydurma).
    progress_pct: Decimal | None = None


class AiProjeListesi(BaseModel):
    items: list[AiProje]
    total: int


class AiOnayKalemi(BaseModel):
    document_type: str
    document_id: uuid.UUID
    title: str | None
    subtitle: str | None
    created_by_name: str | None
    current_step_no: int
    gross_amount: Decimal | None
    net_amount: Decimal | None


class AiOnayKutusu(BaseModel):
    items: list[AiOnayKalemi]
    #: 🔴 Bu uç KAPISIZDIR ve dönen küme "bu adım SANA düştü" olgusuyla sınırlıdır. Aktör
    #: hiçbir projede adım rolü taşımıyorsa küme boştur ve bu **yetki reddi değildir** — cümle
    #: farkı burada doğar. (IZN-B3b: `my_approval_roles` kalktı; rol artık projeye bağlı.)
    total: int


class AiPuantajHaftasi(BaseModel):
    site_id: uuid.UUID
    site_name: str
    project_name: str
    iso_year: int
    iso_week: int
    start_date: str
    end_date: str
    worker_count: int
    #: Uçtaki `totals` gövdesi aynen taşınır; sayılar türetilmez.
    totals: dict[str, Any]


class AiGostergeOzeti(BaseModel):
    role_name: str
    #: 🔴 ÜÇ AYRI SAYI, biri diğerinden TÜRETİLMEZ (ölçüldü):
    #: `active_project_count` taslakları DIŞLAR; `gorunur_proje_sayisi` dizinin
    #: uzunluğudur; portföyün saydığı küme ise ÜÇÜNCÜ bir kümedir ve buradan
    #: okunamaz. Araç ikisini birbirine eşitlemez.
    active_project_count: int
    gorunur_proje_sayisi: int
    portfoy: str
    alacaklar: str
    ortalama_marj: str
    #: `risks` kartı SESSİZCE KIRPAR (`MAX_ALERTS_PER_SOURCE = 3`, üç kaynağın
    #: üçünde de SQL `.limit(3)`) ve zarfında **`total` ALANI YOKTUR**. Yani
    #: `Truncated` zarfı bu uçtan KURULAMAZ; hâl dürüstçe metinle bildirilir.
    risk_notu: str


def sayfa_yetkileri(sayfalar: dict[str, Any]) -> dict[str, str]:
    """`/auth/me.pages` → yalnız erişimi olan sayfalar: `view` | `edit` (+ `+onaylar`)."""
    sonuc: dict[str, str] = {}
    for anahtar, hucre in sayfalar.items():
        duzey, onay = str(hucre["level"]), bool(hucre["approve"])
        if duzey == "none" and not onay:
            continue
        sonuc[anahtar] = duzey + ("+onaylar" if onay else "")
    return sonuc


class AiYetkilerim(BaseModel):
    role_key: str
    #: sayfa anahtarı → `view` | `edit` | `view+onaylar` | `edit+onaylar` (IZN-B6b R1).
    #:
    #: Kaynak `/auth/me.pages`tir; modül düzeyi haritası (`permissions`) söküldü. Kapsam
    #: (disiplin / alan maskesi) bu haritada YOKTUR: "`edit`" ucun açık olduğunu söyler,
    #: hangi ALANLARIN geleceğini söylemez; alan kapsamının AI'daki ifadesi alanın
    #: KENDİSİNİN `null` gelmesidir (`core/field_scope` + `core/scoped_route`; ölçümü
    #: `tests/modules/ai/test_p8_kapsam_maskesi.py`).
    sayfalar: dict[str, str]
    #: `/auth/me.hidden_fields`: rolün hiçbir ekranda göremediği alan kategorileri.
    gizli_alanlar: list[str]
    #: Haritanın NASIL okunacağını söyleyen not (listede olmayan sayfa = yetki yok).
    yaniti_besleyen_not: str


class AiYonlendirme(BaseModel):
    ekran: EkranAnahtari
    ekran_adi: str


# --------------------------------------------------------------------------- #
# AI-2b + AI-2d — 16 okuma aracının GİRDİ modelleri
# --------------------------------------------------------------------------- #


class ProjeKimligiGirdi(BaseModel):
    """Tek proje kimliği. 🔴 TİPLİ (S27) — `str` olsaydı `..` şemadan geçerdi."""

    model_config = ConfigDict(extra="forbid")

    #: 🔴 AI-BAĞLAM: **İSTEĞE BAĞLI**. Boş bırakılırsa `ToolRegistry.invoke`
    #: sohbet bağlamındaki kapsamı doldurur; model açıkça verirse bağlamı EZER.
    #: Zorunlu bırakılsaydı bu doldurma HİÇ ateşlenemezdi: sağlayıcı şeması
    #: `strict`tir ve `girdi_semasi` her alanı `required` yapar, yani model alanı
    #: atlayamaz — yalnız `null` gönderebilir. İkisi de yoksa huni dürüst bir
    #: `kapsam_gerekli` döner ("kayıt yok" DEMEZ).
    project_id: uuid.UUID | None = None


class SantiyeKimligiGirdi(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 🔴 AI-BAĞLAM: **İSTEĞE BAĞLI**. Boş bırakılırsa `ToolRegistry.invoke`
    #: sohbet bağlamındaki kapsamı doldurur; model açıkça verirse bağlamı EZER.
    #: Zorunlu bırakılsaydı bu doldurma HİÇ ateşlenemezdi: sağlayıcı şeması
    #: `strict`tir ve `girdi_semasi` her alanı `required` yapar, yani model alanı
    #: atlayamaz — yalnız `null` gönderebilir. İkisi de yoksa huni dürüst bir
    #: `kapsam_gerekli` döner ("kayıt yok" DEMEZ).
    site_id: uuid.UUID | None = None


class PuantajAyiGirdi(BaseModel):
    """🔴 `year`/`month` ZORUNLUDUR — uç ikisini de `Query(...)` ile zorunlu
    bildirir (ölçüldü: `required=True`). `BosGirdi` kullanılsaydı araç HER
    çağrıda 422 alır ve `ust_kaynak_hatasi` dönerdi."""

    model_config = ConfigDict(extra="forbid")

    #: 🔴 AI-BAĞLAM: **İSTEĞE BAĞLI**. Boş bırakılırsa `ToolRegistry.invoke`
    #: sohbet bağlamındaki kapsamı doldurur; model açıkça verirse bağlamı EZER.
    #: Zorunlu bırakılsaydı bu doldurma HİÇ ateşlenemezdi: sağlayıcı şeması
    #: `strict`tir ve `girdi_semasi` her alanı `required` yapar, yani model alanı
    #: atlayamaz — yalnız `null` gönderebilir. İkisi de yoksa huni dürüst bir
    #: `kapsam_gerekli` döner ("kayıt yok" DEMEZ).
    site_id: uuid.UUID | None = None
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)


class GunPlaniGirdi(BaseModel):
    """`start` ZORUNLUDUR (ölçüldü). `days` GÖNDERİLMEZ: ucun kendi varsayılanı
    (5) GK bloğunun kutu sayısıdır ve `MAX_SUMMARY_DAYS` ile tavanlıdır."""

    model_config = ConfigDict(extra="forbid")

    #: 🔴 AI-BAĞLAM: **İSTEĞE BAĞLI**. Boş bırakılırsa `ToolRegistry.invoke`
    #: sohbet bağlamındaki kapsamı doldurur; model açıkça verirse bağlamı EZER.
    #: Zorunlu bırakılsaydı bu doldurma HİÇ ateşlenemezdi: sağlayıcı şeması
    #: `strict`tir ve `girdi_semasi` her alanı `required` yapar, yani model alanı
    #: atlayamaz — yalnız `null` gönderebilir. İkisi de yoksa huni dürüst bir
    #: `kapsam_gerekli` döner ("kayıt yok" DEMEZ).
    site_id: uuid.UUID | None = None
    start: datetime.date


class SozlesmelerGirdi(BaseModel):
    """🔴 `contract_type` ZORUNLU ve KAPALI KÜME.

    Uç onu `Annotated[ContractType, Query(alias="type")]` ile **varsayılansız**
    bildirir (ölçüldü: `required=True`). Serbest `str` olsaydı model uydurduğu
    bir değerle 422 üretirdi; kapalı küme bunu şema katmanında keser.
    """

    model_config = ConfigDict(extra="forbid")

    contract_type: Literal["employer", "subcontractor"]


class MakineDonemiGirdi(BaseModel):
    """`year`+`month` ZORUNLU (ölçüldü: `work-summary` ve `fuel-summary`)."""

    model_config = ConfigDict(extra="forbid")

    year: int = Field(ge=2000, le=2200)
    month: int = Field(ge=1, le=12)


# --------------------------------------------------------------------------- #
# AI-2b — proje / şantiye / poz / arsa payı
# --------------------------------------------------------------------------- #


class AiProjeDetayi(BaseModel):
    """🔴 `employer` NESNESİ DÜŞÜRÜLDÜ: `ProjectDetailResponse.employer` bir
    `EmployerResponse`tur ve `tax_number` taşır (ölçüldü). Ham şemayla bu araç
    `dogrula_spec` adım 4'e takılır ve **uygulama açılmaz**.

    `employer_name` (düz dize) KALIR: tüzel kişi adıdır ve
    `KISI_ADI_ANAHTARLARI` üyesi DEĞİLDİR (`exposure.py` bu ayrımı yazar).
    """

    id: uuid.UUID
    code: str
    name: str
    type: str
    status: str
    city: str | None
    employer_name: str | None
    start_date: str | None
    end_date: str | None
    contract_no: str | None
    contract_amount: Decimal | None
    #: 🔴 `| None` KAPSAM MASKESİNİN SONUCUDUR, kozmetik bir gevşetme DEĞİL.
    #: `ProjectListItem.budget` `Annotated[Decimal | None, Gorunurluk.para]`tır
    #: ve `projects=limited` rolünde uç `null` döner (araç UCU sarar, servisi
    #: değil — maske AI hattında da koşar). `Decimal` ZORUNLU kalsaydı
    #: `ValidationError` doğar, huninin son dalı onu `ust_kaynak_hatasi`na
    #: düşürür ve maskeli rol AI'dan "sistem hatası" alırdı; oysa ürünün kararı
    #: alanı GİZLEMEKTİ, aracı öldürmek değil.
    budget: Decimal | None
    #: 🔴 MALİ ilerleme (`AiProje.progress_pct` ile aynı sözleşme), fiziksel DEĞİL.
    #: `| None`: `Gorunurluk.operasyonel` → `projects=finance` rolünde gizlenir.
    progress_pct: Decimal | None
    site_count: int
    is_draft: bool


class AiSantiyeSecenegi(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    project_id: uuid.UUID
    project_name: str


class AiSantiyeListesi(BaseModel):
    items: list[AiSantiyeSecenegi]
    total: int


class AiSantiyeDetayi(BaseModel):
    """🔴 `address` DÜŞÜRÜLDÜ — `SiteCard.address` `YASAK_ALAN_ANAHTARLARI`
    üyesidir (S5-c) ve `SiteDetailResponse` `SiteCard`tan TÜREDİĞİ için ham
    şemayla kayıt anında `IfsaIhlali` atılır (işlevsel ölçüm).

    Aynı gerekçeyle `neighborhood` · `parcel` · `gps_coordinates` de
    düşürüldü: yasak listede değiller ama `address`in kırılmış hâlleridir ve
    üçü birden tam adresi yeniden kurar.

    `site_manager_name` · `safety_officer_name` KALIR: `veri_modulleri`nde
    AGREGA bir modül YOKTUR (`sites`/`projects` ikisi de `ACIK`) ve kapalı
    karar 6 yalnız `personnel`/`payroll`/`sales`ı kısıtlar.
    """

    id: uuid.UUID
    code: str
    name: str
    status: str
    city: str | None
    project_name: str
    site_manager_name: str | None
    safety_officer_name: str | None
    start_date: str | None
    end_date: str | None
    delivery_date: str | None
    remaining_days: int | None
    section_count: int
    planned_worker_count: int | None
    is_draft: bool


class AiPozKalemi(BaseModel):
    """🔴 İKİ SAYI DA `| None` ve ikisi AYRI kapsamda kaybolur.

    `BoqItemResponse.quantity` `Gorunurluk.operasyonel` (→ `boq=finance`
    gizler), `unit_price` `Gorunurluk.para` (→ `boq=limited` gizler). Tek bir
    kapsamı düşünüp öbürünü zorunlu bırakmak, aracı öteki rolde patlatırdı.

    🔴 **`?? 0` / `or Decimal(0)` YAZILAMAZ.** Sıfır burada "birim fiyat yok"
    demez, "birim fiyat SIFIR" der — sessizce YANLIŞ bir sayıdır ve maskeli
    rol onu gerçek sanıp hesabına katardı. Gizlemekten kötüdür.
    """

    code: str
    description: str
    unit: str
    quantity: Decimal | None
    unit_price: Decimal | None


class AiPozGrubu(BaseModel):
    name: str
    items: list[AiPozKalemi]


class AiIsKalemleri(BaseModel):
    """🔴 KART, LİSTE DEĞİL: boş poz listesi `ScopedEmpty` DEĞİL `Ok`tur —
    görünen bir şantiyenin poz cetveli gerçekten boş olabilir ve bu bir kapsam
    olgusu değildir. Kapsam farkı ucun 404'ünde konuşur.

    Üç toplam `MetricPlaceholder`dır ve **üç hâli düzleştirilmeden** metne
    çevrilir (`metrik_metni`): `grand_total` ham `Decimal`dır, ötekiler değil.
    """

    site_id: uuid.UUID
    gruplar: list[AiPozGrubu]
    kalem_sayisi: int
    #: 🔴 `| None`: `BoqTotals.grand_total` `Gorunurluk.para`dır ve
    #: `boq=limited` rolünde uç `null` döner. Kalem fiyatları gizliyken
    #: toplamı basmak, maskeyi bölmeyle geri hesaplanabilir kılardı.
    grand_total: Decimal | None
    sozlesme_toplami: str
    gerceklesen_toplam: str
    kalan_toplam: str


class AiHissedar(BaseModel):
    """🔴 Alan adı `name` DEĞİL `shareholder_name` — GÜVENLİK gerekçesiyle.

    Üst kaynakta alan `LandShareShareholderRow.name`tir. Daraltma katmanında
    onu `name`e çevirmek `dogrula_spec`in kişi-adı kapısını **yapısal olarak
    körleştirirdi**: `shareholder_name` `KISI_ADI_ANAHTARLARI` üyesidir, `name`
    değildir. `projects` bugün `ACIK` olduğu için kapı bu araçta zaten koşmuyor
    — ama bir gün `AGREGA`ya çekilirse maske bu adı GÖRMEK ZORUNDADIR.
    **Daraltma bir maskeyi devre dışı bırakmak için kullanılamaz.**

    ⚠️ Bekçisi `test_ai2bd_araclar.py::test_HISSEDAR_ADI_MASKE_TARAFINDAN_
    GORULUR`dur ve o test **yalıtılmış** bir modelle ölçer: `arsa_payi`ın
    tamamıyla ölçmek işe yaramaz, çünkü `landowner_name` kapıyı zaten
    tetikler ve rename'in etkisi görünmez olur (ölçüldü — ilk mutasyon
    denemem tam bu yüzden EŞDEĞER çıktı).
    """

    shareholder_name: str
    #: 🔴 BİLEREK ZORUNLU KALDI (`| None` DEĞİL) ve gerekçesi ölçülmüştür:
    #: `LandShareShareholderRow.share_pct` `Annotated[Decimal,
    #: Gorunurluk.kimlik]`tır — pay ORANI bir tutar değil, hissedarın
    #: kimliğinin parçasıdır ve HİÇBİR kapsamda gizlenmez. Onu da `| None`
    #: yapmak "her `Decimal`ı gevşet" refleksi olurdu ve şema, gerçekte var
    #: olan bir güvenceyi anlatmayı bırakırdı. Etiket bir gün `para`ya
    #: çekilirse `test_p8_kapsam_maskesi.py` bu aracı KIRMIZIYA çevirir.
    share_pct: Decimal
    unit_count: int


class AiArsaPayi(BaseModel):
    """🔴 **`buyer_name` TAŞIMAZ** (yönetim kararı, A1/K1'in bu şemadaki hâli).

    Ve taşıyamaz da: seçilen uç `…/land-share/summary`dir ve o gövdede
    `buyer_name` **HİÇ YOKTUR**. `…/land-share/units` seçilseydi alan gövdeye
    girer, sonra araçta düşürülürdü; burada üst kaynak zaten taşımıyor —
    düşürülmüş bir alan ile hiç var olmayan bir alan aynı güvence değildir.

    `landowner_name` ve `shareholders[].name` KALIR: ölçüldü, ikisi de
    `projects` modülünün tablolarından gelir (`ProjectLandShare` ve
    `LandShareShareholder`, `app/modules/projects/models.py`), `sales`
    zincirinden DEĞİL. `buyer_name` ise `Customer.name`tir
    (`units/repository.py::_open_sales_stmt`) ve `customers` kapısı
    `require_permission("sales", ...)`tır — `sales` KAPALIdır.
    """

    project_id: uuid.UUID
    project_name: str
    landowner_name: str
    our_share_pct: Decimal
    owner_share_pct: Decimal
    contract_no: str | None
    delivery_date: str | None
    toplam_unite: int
    #: 🔴 ÜÇ DEĞER TOPLAMI DA `| None`: üçü de `LandSharePartition.value_total`
    #: (`Gorunurluk.para`) kaynaklıdır ve `projects=limited` rolünde `null`
    #: gelir. ADET sayaçları (`*_unite`) etiketsizdir → `kimlik` → hiçbir
    #: kapsamda gizlenmez; bu yüzden onlar `int` kalır. İkisini aynı kefeye
    #: koymak, K2'nin *"adet dengesi ile değer dengesi AYRI iki olgudur"*
    #: kararını şema düzeyinde siler.
    toplam_deger: Decimal | None
    bizim_unite: int
    bizim_deger: Decimal | None
    satilan_adet: int
    arsa_sahibi_unite: int
    arsa_sahibi_deger: Decimal | None
    atanmamis_unite: int
    hissedarlar: list[AiHissedar]
    #: 🔴 İKİ DENGE TEK SAYIYA İNDİRGENMEZ (K2): bir proje ADET olarak dengede
    #: olup DEĞER olarak sapabilir.
    adet_dengesi_notu: str
    deger_dengesi_notu: str


# --------------------------------------------------------------------------- #
# AI-2b — hakediş / sözleşme / taşeron
# --------------------------------------------------------------------------- #


class AiIsverenHakedisi(BaseModel):
    id: uuid.UUID
    project_name: str
    sequence_no: int
    period_year: int | None
    period_month: int | None
    status: str
    #: 🔴 `| None`: kaynak uç (`ProgressPaymentListItem`/`SubcontractorProgressPaymentListItem`)
    #: tutarı hassas-etiketlidir; gizli rolde `null` gelir. `Decimal` ZORUNLU kalsaydı araç
    #: `ToolError` verirdi (IZN-B4b onarımı).
    gross_total: Decimal | None
    net_total: Decimal | None


class AiIsverenHakedisListesi(BaseModel):
    items: list[AiIsverenHakedisi]
    total: int


class AiTaseronHakedisi(BaseModel):
    id: uuid.UUID
    project_name: str
    #: Tüzel kişi adı — `KISI_ADI_ANAHTARLARI` üyesi DEĞİL.
    subcontractor_name: str | None
    contract_no: str | None
    work_category: str | None
    sequence_no: int
    period_year: int | None
    period_month: int | None
    status: str
    #: 🔴 `| None`: kaynak uç (`ProgressPaymentListItem`/`SubcontractorProgressPaymentListItem`)
    #: tutarı hassas-etiketlidir; gizli rolde `null` gelir. `Decimal` ZORUNLU kalsaydı araç
    #: `ToolError` verirdi (IZN-B4b onarımı).
    gross_total: Decimal | None
    net_total: Decimal | None


class AiTaseronHakedisListesi(BaseModel):
    items: list[AiTaseronHakedisi]
    total: int


class AiSozlesme(BaseModel):
    id: uuid.UUID
    title: str
    contract_no: str | None
    #: Karşı taraf TÜZEL kişidir (işveren ya da taşeron firması).
    #: `KISI_ADI_ANAHTARLARI` üyesidir ama bu araç AGREGA modül BEYAN ETMEZ,
    #: dolayısıyla kayıt anındaki kişi-adı kapısı bu araçta HİÇ KOŞMAZ.
    counterparty_name: str | None
    #: 🔴 `| None`: `ContractListItem.amount` `Gorunurluk.para`dır,
    #: `contracts=limited` rolünde `null` gelir.
    amount: Decimal | None
    start_date: str | None
    end_date: str | None
    progress_pct: Decimal | None
    status: str


class AiSozlesmeListesi(BaseModel):
    contract_type: str
    items: list[AiSozlesme]
    total: int
    #: 🔴 `| None`: `ContractSummary.total_amount` `Gorunurluk.para`dır.
    #: Maske kanonu: maskeli bileşeni ATLAYIP toplamı yeniden hesaplamak
    #: YASAK — eksik bir toplamı gerçek gibi basmak, gizlemekten kötüdür.
    total_amount: Decimal | None
    active_count: int
    expiring_this_month_count: int


class AiTaseron(BaseModel):
    """🔴 `tax_number` · `phone` · `email` DÜŞÜRÜLDÜ — üçü de
    `YASAK_ALAN_ANAHTARLARI` üyesidir (işlevsel ölçüm: `SubcontractorResponse`
    üçünü birden taşır). Ham şemayla bu araç kaydedilemez ve uygulama açılmaz.

    🔴 `SubcontractorResponse` DEĞİŞTİRİLMEDİ: bu dilim sözleşme değiştirmez;
    şemayı daraltmak frontend `typecheck`ini kırardı.
    """

    id: uuid.UUID
    name: str
    contact_person: str | None
    category: str | None
    is_active: bool


class AiTaseronListesi(BaseModel):
    items: list[AiTaseron]
    total: int


# --------------------------------------------------------------------------- #
# AI-2b — puantaj / günlük / gün planı
# --------------------------------------------------------------------------- #


class AiPuantajGunu(BaseModel):
    work_date: str
    total_hours: Decimal
    worked_day_count: int
    leave_count: int
    temporary_duty_count: int


class AiPuantajAyi(BaseModel):
    """🔴 **KİŞİ SATIRI TAŞIMAZ.** `TimesheetMatrix.rows` her satırda
    `full_name` taşır (`TimesheetMatrixRow`, ölçüldü) ve `full_name`
    `KISI_ADI_ANAHTARLARI` üyesidir. Emsal karar `puantaj_haftasi`dadır:
    araç `personnel` BEYAN ETMEZ çünkü kişi satırı BASMAZ — beyan etseydi
    (AGREGA) kayıt anında `IfsaIhlali` alır, basmaya devam etseydi kişi verisi
    sağlayıcıya giderdi. İkisi birden kapanmalıdır: **satır yok, beyan yok.**

    Kalan her şey agregadır: kişi sayısı, saat toplamı, adam-gün ve GÜN
    sütunlarının ayak satırı.
    """

    site_id: uuid.UUID
    site_name: str
    project_name: str
    year: int
    month: int
    section_name: str | None
    worker_count: int
    total_hours: Decimal
    #: `total_hours ÷ 9` — satır adam-günlerinin toplamı DEĞİL (uç böyle türetir).
    total_man_days: Decimal
    gun_toplamlari: list[AiPuantajGunu]


class AiGunlukKayit(BaseModel):
    id: uuid.UUID
    entry_date: str
    status: str
    weather: str | None
    has_incident: bool
    worker_total: int
    #: IZN-B4d: `DiaryEntryListItem.lines_total` `maliyet_kar`+`sozlesme_fiyat` etiketlidir;
    #: gizli rolde `null` gelir. `Decimal` ZORUNLU kalsaydı araç ValidationError ile ölürdü.
    lines_total: Decimal | None


class AiGunlukKayitListesi(BaseModel):
    #: `AiProjeListesi` emsali: zarf BEYAN edilir, handler ise `liste_sonucu`
    #: ile **satır listesini** döndürür. Kullanılmayan bir `site_id` alanı
    #: burada BULUNMAZ — şemada olup gövdede olmayan alan, şemayı yalancı yapar.
    items: list[AiGunlukKayit]
    total: int


class AiPlanGunu(BaseModel):
    plan_date: str
    is_weekend: bool
    #: 🔴 `has_plan` `text == ""` İLE AYNI ŞEY DEĞİLDİR: planı olmayan gün
    #: pencereden DÜŞMEZ, işaretlenir (uç sözleşmesi).
    has_plan: bool
    text: str
    planned_worker_total: int
    section_names: list[str]


class AiGunPlani(BaseModel):
    site_id: uuid.UUID
    site_name: str
    project_name: str
    start: str
    end: str
    days: list[AiPlanGunu]


# --------------------------------------------------------------------------- #
# AI-2d — makine
# --------------------------------------------------------------------------- #


class AiMakine(BaseModel):
    id: uuid.UUID
    name: str
    category: str
    brand: str | None
    model: str | None
    plate_no: str | None
    ownership: str
    status: str
    #: `None` = DEPODAKİ makine. 🔴 Bu değer kapsam notunun gerekçesidir:
    #: `equipment/repository.py::scope` `site_id IS NULL` dalını kapsam
    #: süzgecinin DIŞINDA tutar.
    site_id: uuid.UUID | None
    is_active: bool


class AiMakineListesi(BaseModel):
    items: list[AiMakine]
    total: int


class AiCalismaSatiri(BaseModel):
    equipment_name: str
    site_id: uuid.UUID | None
    hours: Decimal
    #: 🔴 `usage_pct` ve `cost` AYRI AYRI `None` olabilir (K16): saati bilinen
    #: bir makinenin bedeli bilinmiyor olabilir. Tek alana sıkıştırılmaz.
    usage_pct: Decimal | None
    breakdown_hours: Decimal
    cost: Decimal | None


class AiMakineCalismasi(BaseModel):
    year: int
    month: int
    rows: list[AiCalismaSatiri]
    total_hours: Decimal
    total_breakdown_hours: Decimal
    #: 🔴 Bedeli bilinmeyen satır UYDURMA bir 0 ile toplama GİRMEZ (K16);
    #: toplam bilinenlerden oluşur ve bu not onu söyler.
    #: IZN-B4d: `totals.cost` `maliyet_kar`dır; gizli rolde `null` (satır `cost`ları da).
    total_cost: Decimal | None
    usage_pct_avg: Decimal | None
    bilinmeyen_bedel_notu: str


class AiYakitSatiri(BaseModel):
    equipment_name: str
    site_id: uuid.UUID | None
    liters: Decimal
    #: IZN-B4d: yakıt tutarı `maliyet_kar`dır; gizli rolde `null`.
    amount: Decimal | None
    actual: Decimal | None
    norm: Decimal | None
    deviation_pct: Decimal | None
    consumption_status: str | None


class AiMakineYakiti(BaseModel):
    year: int
    month: int
    total_liters: Decimal
    #: IZN-B4d: yakıt toplam tutarı `maliyet_kar`dır; gizli rolde `null`.
    total_amount: Decimal | None
    #: 🔴 Payda 0 ise `None` — uydurma 0 basılmaz (K16).
    lt_per_hour_avg: Decimal | None
    avg_unit_price: Decimal | None
    abnormal_count: int
    rows: list[AiYakitSatiri]


class AiKiraFaturasi(BaseModel):
    id: uuid.UUID
    #: Tedarikçi TÜZEL kişidir; `procurement` modülünden gelir.
    supplier_name: str | None
    invoice_no: str | None
    period_year: int
    period_month: int
    site_name: str | None
    invoice_amount: Decimal | None
    vat_amount: Decimal | None
    payable_total: Decimal | None
    status: str


class AiMakineKirasi(BaseModel):
    items: list[AiKiraFaturasi]
    total: int

import enum
import uuid
from decimal import Decimal
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.core.field_mask import Hassas
from app.modules.projects.models import ProjectStatus

# Yer tutucu sozlesmesi TEK yerde tanimlidir (B6/P1, spec §2.3): kopyalanmaz,
# projects modulunden import edilir (`boq/schemas.py:11` emsali).
#
# 🔴 NICIN BIRLESTIRILDI (DASH-1): panelin KENDI kopyasi `pending_module`u
# ZORUNLU tutuyordu ve dogrulayicisi YOKTU — yani zarf UC hâlden yalnizca
# IKISINI ifade edebiliyordu. `portfolio` baglandiginda ucuncu hâl gerekti:
#   * `available=True`  + `pending_module is None`  -> DOLU
#   * `available=False` + `pending_module` dolu     -> soru hic sorulmadi
#   * `available=False` + `pending_module is None`  -> ROLUN IZNI YOK (ILR-1/2,
#     kullanici karari 2026-08-27; yalnizca `restricted()` fabrikasindan kurulur)
# Kopya zarf ucuncu hâli KURAMAZDI (`pending_module` zorunlu) ve ilkini de
# DOGRULAYAMAZDI. Iki tanim yerine tek tanim: kural bir kez yazilir.
#
# 🔴 `ListPlaceholder` / `PendingApprovalsPlaceholder` BILINCLI olarak DISARIDA:
# orada dolu zarfin `pending_module` tasimasi emsaldir (`CountPlaceholder`
# notu) — "dolu ⇒ modul yok" kurali YALNIZ `MetricPlaceholder`indir.
from app.modules.projects.schemas import MetricPlaceholder

__all__ = [
    "MetricPlaceholder",
    "ListPlaceholder",
    "PendingApprovalsPlaceholder",
    "RiskAlert",
    "RiskAlertsPlaceholder",
    "RiskSeverity",
    "RiskSource",
    "RiskSourceState",
    "DashboardProjectCard",
    "DashboardSummaryResponse",
]


class ListPlaceholder(BaseModel):
    """Liste tipli kart (risk uyarilari)."""

    available: bool = False
    items: list[str] = Field(default_factory=list)
    pending_module: str


class PendingApprovalsPlaceholder(ListPlaceholder):
    """Onay bekleyenler karti — rozet sayaci tasir."""

    count: int = 0


class RiskSeverity(str, enum.Enum):
    """Uyari satirinin SIDDETI — mockup'in UC seridinden OLCULDU.

    🔴 Kartin adi ("Risk & Uyarilar") icerigini KISMEN YALANLIYOR: ucuncu satir
    bir risk DEGIL, IYI HABERDIR. Kart aslinda SIDDET ETIKETLI BIR UYARI
    AKISIDIR; bu yuzden siddet bir alan olarak tasinir ve `success` gercek bir
    uyedir, suslemesi degil.

    Renkler `Ekran 1 - Gosterge Paneli.dc.html:378-395`ten BIREBIR okundu — emir
    metnindeki esleme TERSTI ve olcum onu curuttu:
      * `#f59e0b` (kehribar) -> "Stok kritik seviyede"  => `warning`
      * `#ef4444` (kirmizi)  -> "Hakedis gecikmis"      => `danger`
      * `#22c55e` (yesil)    -> "Hedef asildi"          => `success`

    🔴 RENK DEGERI SUNUCUDA URETILMEZ (K10): burada donen sey ANLAMDIR, sinif
    adi ya da hex degil. Renk/rozet karari istemcinindir — HZ-1'in "aciliyet
    rozeti sunucuda uretilmez" kanonunun aynisi.
    """

    danger = "danger"
    warning = "warning"
    success = "success"


class RiskAlert(BaseModel):
    """Kartin TEK satiri — mockup'in her satirinda UC olgu vardir.

    `title` ust satir (`13px`), `detail` alt satir (`11px`), `severity` sol
    seridin rengi. `items: list[str]` bunlarin YALNIZ BIRINI tasiyabiliyordu; uc
    olguyu tek metne yapistirmak, ekranin ayristirmak zorunda kalacagi bir sunum
    kararini sunucuda uretmek olurdu (K10, `_pending_approvals` notunun emsali).

    🔴 `module` var, `link` YOK ve bu bilinclidir. Emir bir `baglanti?` alani
    onerdi; ISTEMCI ROTASI SUNUCUDA URETILMEZ — bir URL ("/stok") frontend'in
    yonlendirme haritasini backend'e kopyalardi ve rota degistigi gun panel
    sessizce olu baglanti basardi. Bunun yerine satir KAYNAK MODULUNU tasir;
    hangi ekrana gidilecegi istemcinin karari kalir.
    """

    severity: RiskSeverity
    title: str
    detail: str
    module: str


class RiskSourceState(str, enum.Enum):
    """Bir KAYNAGIN o aktor icin durumu.

    `ok` — izin var, kaynak sorgulandi (satir cikmamis olabilir; bu OTORITER
    bir "o kaynakta uyari yok"tur).
    `restricted` — 🔴 ROLUN IZNI YOK (ILR-1/2 ucuncu hâli). Kart KAPANMAZ,
    yalnizca O KAYNAK susar: `inventory` gormeyen `accounting` stok satirlarini
    gormez ama hakedis satirlarini GORUR (ILR kanonu: "izni olana veriyi ver,
    olmayana `restricted()` dondur — kartin tamamini herkese kapatmak gereksiz
    genistir").
    """

    ok = "ok"
    restricted = "restricted"


class RiskSource(BaseModel):
    """Kartin UC kaynagindan biri ve o aktor icin durumu."""

    module: str
    state: RiskSourceState


class RiskAlertsPlaceholder(BaseModel):
    """ "Risk & Uyarilar" kartinin zarfi.

    🔴 ZARF SINIFI OLCULDU: bu kart NE `MetricPlaceholder` NE `CountPlaceholder`
    ailesine girer, cunku ikisi de TEK KAYNAKLI bir alani tarifler ve tri-state
    bilgisini TEK bir `pending_module` stringine sikistirir. Bu kartin UC AYRI
    kaynagi ve UC AYRI izin kapisi vardir; tek bir anahtar kartin ancak UCTE
    BIRINI adlandirabilirdi — servis docstring'inin 2. kusuru tam olarak buydu
    (`_RISKS_MODULE = "inventory"` yalnizca ilk satiri besliyordu).

    Bu yuzden tri-state KARTTAN KAYNAGA TASINDI: her kaynak kendi modulunu ve
    kendi durumunu bildirir (`sources`), kart ise yalnizca "en az bir kaynak
    konustu mu" bilgisini tasir. `pending_module` alani BU ZARFTA YOKTUR —
    bugun uc kaynagin ucu de BAGLIDIR, yani "modul henuz yazilmadi" hâli
    URETILEMEZ; uretilemeyen bir hâli semada tasimak `available:false`in
    sebebini yeniden belirsizlestirirdi (ILR-1/2'nin duzelttigi kusurun
    aynisi).

    🔴 BOS LISTE "RISK YOK" DEMEK DEGILDIR — ve bu zarfta oyle olmasi
    SAGLANMISTIR: esigi girilmemis (`min_stock IS NULL`) kalemler AYRI bir
    `warning` satiriyla sayilir (`risks._stock_alerts`). Yani
    `items == []` yalnizca "bilinen uyari yok VE bilinmeyen esik yok" hâlinde
    dogar. Emsal `inventory`nin `items_without_price` sayacidir: fiyatsiz kalem
    sessizce 0 sayilmaz, AYRICA raporlanir.
    """

    available: bool = False
    items: list[RiskAlert] = Field(default_factory=list)
    sources: list[RiskSource] = Field(default_factory=list)


class DashboardProjectCard(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    # Kart satırı KENDİ projesindeki rolle maskelenir (`field_mask.PROJE_ALANI_OZNITELIGI`).
    PROJE_ALANI: ClassVar[str] = "id"

    id: uuid.UUID
    code: str
    name: str
    status: ProjectStatus
    budget: Annotated[Decimal | None, Hassas.maliyet_kar]
    progress_pct: Annotated[Decimal | None, Hassas.yok]


class DashboardSummaryResponse(BaseModel):
    role_name: str
    active_project_count: int
    projects: list[DashboardProjectCard]
    portfolio: Annotated[MetricPlaceholder, Hassas.sozlesme_fiyat]
    receivables: Annotated[MetricPlaceholder, Hassas.sozlesme_fiyat]
    average_margin: Annotated[MetricPlaceholder, Hassas.maliyet_kar]
    pending_approvals: PendingApprovalsPlaceholder
    risks: RiskAlertsPlaceholder

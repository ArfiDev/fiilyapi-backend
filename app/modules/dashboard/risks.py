"""RISK-1 — "Risk & Uyarilar" karti: SIDDET ETIKETLI UYARI AKISI (mockup `:375-400`).

Kart yer tutucu olmaktan cikti. Servisin eski docstring'i UC kusuru tek tek
yazmisti; her biri burada kapaniyor:

1. **ZARF YETERSIZDI** (`items: list[str]`) -> satir artik `RiskAlert`tir:
   `severity` + `title` + `detail` + `module`.
2. **ANAHTAR KARTIN UCTE BIRINI KAPSIYORDU** (`pending_module = "inventory"`)
   -> tek anahtar SILINDI; her kaynak KENDI modulunu ve KENDI durumunu bildirir
   (`RiskAlertsPlaceholder.sources`).
3. **IZIN KAPISI** -> her kaynak KENDI kapisindan gecer ve kart KISMI dolar.

## 🔴 UC KAYNAK, UC AYRI KAPI — kart KAPANMAZ, SUSAN KAYNAK SUSAR

ILR kanonu birebir uygulanir: *"izni olana veriyi ver, olmayana `restricted()`
dondur — kartin tamamini herkese kapatmak gereksiz genistir."* Olculdu
(`roles/seed_data.py` MATRIX):

| Kaynak | Modul | Paneli acip kaynagi GOREMEYEN rol |
|---|---|---|
| Stok | `inventory` | `hr_manager`, `accounting` (`inventory = _N`) |
| Hakedis gecikmesi | `progress_payments` | `hr_manager` (`_N`) |
| Takvim | `sites` | (paneli acan her rolde en az `_LIM`) |

Yani `accounting` bu kartta stok satirlarini GORMEZ ama hakedis satirlarini
GORUR. Kartin tamamini kapatmak, gecikmis hakedisi muhasebeden gizlemek olurdu.

## 🔴 TURETILEBILIRLIK — UCUNU DE KODDAN OLCTUM

* **Stok (mockup satir 1):** TURETILEBILIR. `inventory.balance.status_case`
  kanonik durum ifadesidir; `repository.list_summary_rows(status="critical")`
  ayni suzgecten (pasif kalem kurali dahil) gecer. Formul BURADA IKINCI KEZ
  YAZILMAZ (K3) — ikinci bir esik karsilastirmasi E3 ekraniyla bu kartin ayni
  kalem icin farkli rozet basmasi demekti.
  ⚠️ SAPMA: mockup ayrintisi `"Liman Altyapi – Demir eksikligi"` yani
  PROJE + malzeme. Stok KARTI sirket geneli bir katalog satiridir ve bakiyesi
  BIRDEN COK depoya (merkez depo dahil, `visible_warehouse_ids`) yayilir — tek
  bir proje adi TURETILEMEZ, uydurulmadi. Ayrinti malzeme + kalan miktardir.
* **Hakedis gecikmesi (mockup satir 2):** TURETILEBILIR — ve vade ifadesi ZATEN
  VARDIR. `treasury.upcoming.progress_payment_due_expression()` vadeyi
  `approved_at`in TR gununden + sozlesmenin `payment_term_days`indan uretir ve
  `__all__`da DISA ACIKTIR (saat dilimi cevrimi orada bekcilenmistir; ikinci bir
  kopya CI'da/Railway'de bir gun kayardi). Emrin *"'gecikme' icin bir
  tahakkuk/vade tarihi gerekiyor, olc"* premise'i olculdu: **gerekiyor ve VAR.**
  🔑 `treasury.upcoming` docstring'i *"Gecikmis borc takibi ayri bir yuzeydir ve
  hicbir mockup'ta cizilmemistir"* diyerek gecmisi bilincli olarak DISARIDA
  birakmisti. O yuzey TAM OLARAK BURASIDIR — pencere simdi `vade < bugun`dur.
* **Takvim / "Hedef asildi" (mockup satir 3):** KISMEN. Ayrinti asagida
  `_schedule_alerts` docstring'indedir; ozeti: *hangi bolumun planlanan bitisi
  gelmeden tamamlandigi* OTORITER bir olgudur ve basilir, ama mockup'in
  **`%3`** buyuklugu URETILEMEZ (planlanan ilerleme egrisi de fiili tamamlanma
  damgasi da yoktur). Satir SILINMEDI, DURUST hâliyle basildi.

## 🔴 BOS LISTE "RISK YOK" DEMEZ

`min_stock` NULLABLE'dir (`inventory/models.py:119`) ve `status_case` esik
yokken durumu `NULL` birakir — yani esigi girilmemis kalem hicbir kovaya
dusmez. Bu yuzden esiksiz kalemler AYRI bir `warning` satiriyla SAYILIR ve
kullaniciya SOYLENIR. Emsal ayni modulden gelir: `summary_kpis`in
`items_without_price` sayaci da fiyatsiz kalemi sessizce 0 saymaz.

## N+1 — kart panelin SICAK yolundadir

Sorgu sayisi VERI HACMINDEN BAGIMSIZDIR ve satir basina sorgu ACILMAZ:
gorunur projeler (bir kez) + kaynak basina en fazla iki toplu sorgu. Her
kaynak kendi izin kapisiyla ERKEN doner, yani izni olmayan aktor o kaynagin
sorgusunu HIC odemez. Bekcisi `test_kart_sorgu_sayisi_SATIR_SAYISINDAN_BAGIMSIZ`.
"""

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import UNRESTRICTED, DisciplineScope
from app.core.permissions import can_read
from app.core.timezone import today
from app.modules.contracts.models import SubcontractorContract
from app.modules.dashboard.schemas import (
    RiskAlert,
    RiskAlertsPlaceholder,
    RiskSeverity,
    RiskSource,
    RiskSourceState,
)
from app.modules.inventory import repository as inventory_repository
from app.modules.inventory.balance import StockStatus
from app.modules.projects.models import Project
from app.modules.projects.service import visible_projects
from app.modules.sites.models import Section, SectionStatus, Site
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
)
from app.modules.treasury.upcoming import progress_payment_due_expression
from app.modules.users.models import User

#: Kaynak modulleri — izin kapisi ve `sources[].module` AYNI stringi kullanir ki
#: "hangi kapi hangi satiri susturdu" sorusu yanittan okunabilsin.
STOCK_MODULE = "inventory"
PROGRESS_PAYMENT_MODULE = "progress_payments"
SCHEDULE_MODULE = "sites"

#: Kaynak basina satir TAVANI. Kart bir liste ekrani DEGIL bir OZETTIR (mockup
#: uc satir cizer); tavansiz birakilsaydi tek bir kotu gunde panel yuzlerce
#: satirla dolar ve okunamaz hâle gelirdi. Tavan SQL'dedir (`LIMIT`), Python'da
#: kirpma degil — kirpilan satirlar bosuna cekilmezdi.
MAX_ALERTS_PER_SOURCE = 3

#: Siralama: once `danger`, sonra `warning`, en son `success`. Iyi haber kotu
#: haberin ustune cikmamalidir.
_SEVERITY_ORDER = {RiskSeverity.danger: 0, RiskSeverity.warning: 1, RiskSeverity.success: 2}


def _quantity(value: Decimal) -> str:
    """Miktari gereksiz sifirlarindan arindirir (`10.000` -> `10`).

    Sunum degil OKUNABILIRLIK: `Numeric(14, 3)` her miktari uc haneyle dondurur
    ve "kalan 10.000 Ton" bir satirda binlik ayraci sanilabilir.
    """
    normalized = value.normalize()
    # `normalize()` buyuk sayilari ussel bicime cevirebilir (1E+3); geri ac.
    return f"{normalized:f}"


async def _stock_alerts(session: AsyncSession, project_ids: list[uuid.UUID]) -> list[RiskAlert]:
    """KRITIK stok kalemleri + esigi BILINMEYEN kalemlerin sayisi.

    Durum formulu (`balance.status_case`) ve gorunurluk suzgeci
    (`visible_warehouse_ids` + pasif kalem kurali) inventory modulunden
    CAGRILIR, KOPYALANMAZ (K3): ikinci bir esik karsilastirmasi E3 stok ekrani
    ile bu kartin ayni kalem icin farkli rozet basmasi demekti.

    Kapsam `project_ids`tir ve MERKEZ DEPO DAHILDIR — `visible_warehouse_ids`in
    kurali budur ("gorunen tum depolar, merkez dahil"). Merkez depo hicbir
    santiyeye bagli degildir; disarida biraksaydik sirketin ana ambarindaki
    kritik demir bu kartta hic gorunmezdi.

    IKI sorgu acar ve ikisi de kalem sayisindan bagimsizdir.
    """
    warehouse_ids = inventory_repository.visible_warehouse_ids(project_ids)
    ctx = inventory_repository.summary_context(warehouse_ids)
    rows = await inventory_repository.list_summary_rows(
        session,
        ctx,
        only_moved=False,
        status=StockStatus.critical.value,
        category=None,
        q=None,
        limit=MAX_ALERTS_PER_SOURCE,
        offset=0,
    )
    alerts = [
        RiskAlert(
            severity=RiskSeverity.warning,
            title="Stok kritik seviyede",
            detail=f"{row[0].name} – kalan {_quantity(row.balance)} {row[0].unit}",
            module=STOCK_MODULE,
        )
        for row in rows
    ]
    esiksiz = await inventory_repository.count_items_without_threshold(
        session, ctx, only_moved=False
    )
    if esiksiz:
        alerts.append(
            RiskAlert(
                severity=RiskSeverity.warning,
                title="Stok eşiği girilmemiş",
                detail=f"{esiksiz} kalemin asgari stoğu yok — durumu bilinmiyor",
                module=STOCK_MODULE,
            )
        )
    return alerts


async def _overdue_payment_alerts(
    session: AsyncSession, project_ids: list[uuid.UUID]
) -> list[RiskAlert]:
    """Vadesi GECMIS, henuz FATURALANMAMIS, onayli taseron hakedisleri.

    Vade `treasury.upcoming.progress_payment_due_expression()`ten gelir ve
    IKINCI KEZ YAZILMAZ (K3): o ifade `approved_at`i once TR gunune cevirir ve
    ceviriyi silen mutasyonun YALNIZ UTC makinede (CI/Railway) gorunur oldugu
    orada olculmustur. Kopya bir ifade bu kartta o kusuru geri getirirdi.

    Suzgecin her parcasi `upcoming`in kardesidir, bu kart icin ICAT EDILMEDI:
      * `approved` — taslak/beklemedeki hakedis bir BORC degildir.
      * 🔴 **`~faturalanmis` SUZGECI KALDIRILDI (denetim bulgusu 4).** Eskiden
        burada "hakedis faturalandiysa satir uretme" diye bir dislama vardi ve
        gerekcesi `upcoming.py`den ODUNC ALINMISTI: *"ikisi de listelenseydi
        ayni borc iki satir uretirdi"*. O gerekce BURADA GECERSIZDIR cunku bu
        dosyada bir FATURA DALI YOKTUR — olculdu:

            command grep -rn "app.modules.invoicing" app/modules/dashboard/
            -> EXIT=1 (hic yok)

        Yani dislama hicbir cift sayimi engellemiyor, YALNIZCA gecikmis borcu
        panelden SESSIZCE siliyordu. Faturasi kesilmis olmak bir borcu gecikmis
        olmaktan CIKARMAZ.

        Gecikme uyarisini susturan sey artik BORCUN KAPANMASIDIR: `status ==
        approved` suzgeci `paid` hakedisleri zaten disarida birakir ve ODM-2'den
        sonra `paid` damgasi ancak GERCEKLESMIS para ile basilabilir. Yani
        "odendi" tek durdurucudur ve o damga artik bedava degildir.

        KABUL EDILEN SAPMA: faturasi daha ILERI bir vadeyle kesilmis bir hakedis,
        kendi TURETILMIS vadesi gectiginde erken uyari verebilir. Bilincli
        tercihtir — bu deponun fail-closed kanonu: borcun sessizce kaybolmasi,
        fazladan gorunmesinden cok daha pahalidir.
      * `project_id IN gorunur` — IDOR. Suzgec dusseydi kapsam disi bir projenin
        TASERON ADI ve gecikmesi panelde okunurdu.
    TEK FARK PENCEREDIR: `upcoming` gelecege bakar (`vade >= bugun`), bu kart
    GECMISE (`vade < bugun`) — `upcoming` docstring'inin "gecikmis borc takibi
    ayri bir yuzeydir" dedigi yuzey tam olarak burasidir.

    En gec kalan ilk sirada (`ORDER BY vade`), tavan SQL'de.
    """
    if not project_ids:
        return []
    bugun = today()
    vade = progress_payment_due_expression()
    stmt = (
        select(SubcontractorContract.subcontractor_name, vade)
        .select_from(SubcontractorProgressPayment)
        .join(
            SubcontractorContract,
            SubcontractorContract.id == SubcontractorProgressPayment.contract_id,
        )
        .where(
            SubcontractorProgressPayment.status == SubcontractorPaymentStatus.approved,
            SubcontractorProgressPayment.project_id.in_(project_ids),
            vade < bugun,
        )
        .order_by(vade, SubcontractorProgressPayment.id)
        .limit(MAX_ALERTS_PER_SOURCE)
    )
    return [
        RiskAlert(
            severity=RiskSeverity.danger,
            title="Hakediş gecikmiş",
            detail=_overdue_detail(taseron_adi, (bugun - vade_gunu).days),
            module=PROGRESS_PAYMENT_MODULE,
        )
        for taseron_adi, vade_gunu in (await session.execute(stmt)).all()
    ]


def _overdue_detail(subcontractor_name: str | None, days: int) -> str:
    """Mockup ayrintisi: `"Celik OSB – 14 gun gecikme"`.

    `subcontractor_name` NULLABLE'dir (`contracts/models.py`): ad yoksa satir
    DUSMEZ — gecikme gercek, yalnizca karsi tarafin adi bilinmiyor. Uydurma bir
    ad ("Bilinmeyen taseron") basmak yerine ayrinti gecikmeyle sinirlanir.
    """
    gecikme = f"{days} gün gecikme"
    return f"{subcontractor_name} – {gecikme}" if subcontractor_name else gecikme


async def _schedule_alerts(session: AsyncSession, project_ids: list[uuid.UUID]) -> list[RiskAlert]:
    """Mockup satir 3 — "Hedef asildi". DURUST hâliyle basilir.

    🔴 NE URETILEMEZ (olculdu, uydurulmadi): mockup `"%3 erken teslim"` yazar.
    Bir YUZDE, planlanan ilerleme egrisi ile fiili ilerlemenin farkidir. Ikisi
    de YOKTUR:
      * `projects/timeline.py` docstring'i acikca sayar: *"Ilerleme yuzdesi (S1):
        alan HIC ACILMAZ"* ve *"Gecikme vurgusu, kritik yol, baseline kiyasi:
        kaynak yok"*. Planlanan ilerleme egrisi bu depoda YOK.
      * FIILI TAMAMLANMA DAMGASI DA YOK: `Section` yalnizca `status` + planlanan
        `start_date`/`end_date` tasir; "ne zaman tamamlandi" kolonu yoktur.
        `Site.delivery_date` fiili teslim DEGIL, santiye FORMUNDA girilen bir
        plan tarihidir (`sites/models.py:132`, form spec §3.0) — onu "fiili" gibi
        okumak tam olarak K2'nin yasakladigi uydurmadir.
      * Gecen SUREYI planlanan ilerleme sanmak (dogrusal S-egrisi varsaymak) da
        bir ICATTIR; ekran gercek olmayan bir "%" basardi.

    🔑 NE URETILEBILIR: *"su an tamamlanmis, ama planlanan bitisi HENUZ
    GELMEMIS"* bolum. Bu OTORITER bir olgudur ve cikarim gerektirmez — bolum
    bugun tamamdir, planlanan bitis ileri bir tarihtir, yani hedefin ONUNDEDIR.
    Ayrinti bu yuzden GUN cinsindendir ve BUGUNE gore konusur ("planlanan
    bitise N gun kala tamamlandi"); bir yuzde ya da "N gun erken" iddiasi fiili
    tamamlanma tarihini bilmeyi gerektirirdi.

    Kanon: *"rotasi/kaynagi olmayan mockup ogesi SILINMEZ, durust hâliyle
    basilir."* Satir silinmedi, buyuklugu uydurulmadi.

    Kapsam: `Site.project_id IN gorunur` — bolum AYRI bir izin modulu DEGILDIR
    (`seed_data` `sites` satirinin yorumu: "bolum santiyenin ic kirilimidir").
    """
    if not project_ids:
        return []
    bugun = today()
    stmt = (
        select(Project.name, Section.name, Section.end_date)
        .select_from(Section)
        .join(Site, Site.id == Section.site_id)
        .join(Project, Project.id == Site.project_id)
        .where(
            Site.project_id.in_(project_ids),
            Section.status == SectionStatus.completed,
            Section.end_date > bugun,
        )
        .order_by(Section.end_date.desc(), Section.id)
        .limit(MAX_ALERTS_PER_SOURCE)
    )
    return [
        RiskAlert(
            severity=RiskSeverity.success,
            title="Hedef aşıldı",
            detail=(
                f"{proje_adi} – {bolum_adi}: planlanan bitişe "
                f"{(bitis - bugun).days} gün kala tamamlandı"
            ),
            module=SCHEDULE_MODULE,
        )
        for proje_adi, bolum_adi, bitis in (await session.execute(stmt)).all()
    ]


async def build_risks(
    session: AsyncSession, actor: User, scope: DisciplineScope = UNRESTRICTED
) -> RiskAlertsPlaceholder:
    """Kartin zarfini kurar: UC kaynak, UC kapi, KISMI dolus.

    Gorunur projeler kaynak basina degil BIR KEZ okunur; hicbir kaynagin izni
    yoksa HIC okunmaz (izinsiz aktor kapsam sorgusunu odemez).
    """
    izinler = {
        STOCK_MODULE: await can_read(session, actor, STOCK_MODULE),
        # DSC-B5 (Ü2): hakedis kaynagi ticari → disiplin kisitlisina KAPALI (gecikme uyarisi yok).
        PROGRESS_PAYMENT_MODULE: (
            await can_read(session, actor, PROGRESS_PAYMENT_MODULE) and not scope.is_restricted
        ),
        SCHEDULE_MODULE: await can_read(session, actor, SCHEDULE_MODULE),
    }
    sources = [
        RiskSource(
            module=module,
            state=RiskSourceState.ok if izinli else RiskSourceState.restricted,
        )
        for module, izinli in izinler.items()
    ]
    if not any(izinler.values()):
        return RiskAlertsPlaceholder(available=False, items=[], sources=sources)

    project_ids = [p.id for p in await visible_projects(session, actor)]
    alerts: list[RiskAlert] = []
    if izinler[STOCK_MODULE]:
        alerts += await _stock_alerts(session, project_ids)
    if izinler[PROGRESS_PAYMENT_MODULE]:
        alerts += await _overdue_payment_alerts(session, project_ids)
    if izinler[SCHEDULE_MODULE]:
        alerts += await _schedule_alerts(session, project_ids)
    # 🔴 ANAHTARDA `alert.detail` YOKTUR ve bu bir eksiklik DEGIL SARTTIR.
    # Gecikme satirlarinin `title`i SABITTIR ("Hakediş gecikmiş"); `detail`
    # anahtara girerse ayni siddet grubunun ICINDEKI tek ayirt edici anahtar o
    # STRING olur ve sira ALFABETIK dizilir — "Çelik OSB – 90 gün" metin olarak
    # "Ada Beton – 5 gün"den SONRA gelir, yani EN AGIR gecikme listenin EN ALTINA
    # duserdi. Python'un `sort`u KARARLIDIR (stable): anahtar disinda kalan her
    # sey kaynagin KENDI SQL sirasini korur — gecikmeler `ORDER BY vade` (en gec
    # kalan ilk, `_overdue_payment_alerts` docstring'inin sozu), takvim
    # `ORDER BY end_date DESC`. Bekcisi: `test_gecikme_satirlari_EN_GEC_KALAN_ILK_SIRADA`.
    alerts.sort(key=lambda alert: (_SEVERITY_ORDER[alert.severity], alert.title))
    return RiskAlertsPlaceholder(available=True, items=alerts, sources=sources)

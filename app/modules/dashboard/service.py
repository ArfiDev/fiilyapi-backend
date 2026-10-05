"""Gosterge paneli ozeti + BES KARTIN YER TUTUCU DENETIMI (P-YT2).

🔴 `pending_module` ARTIK "MODUL YOK" DEMEK DEGIL (P-YT1 bulgusu). Bu dosyadaki
BES anahtarin BESI de CANLI bir modulu adlandirir; zarfin ilk anlami tek bir
kart icin bile dogru degildir. Denetim bu yuzden UC SINIF uretti:

| Kart | anahtar | sinif | ozet gerekce |
|---|---|---|---|
| `pending_approvals` | `approvals` | **(A) BAYAT** | motor canli, kapsam ayni — **BAGLANDI** |
| `portfolio` | `progress_payments` | **(A) BAYAT** | urun karari verildi — |
| | | | **BAGLANDI** (isveren, approved+paid) |
| `receivables` | `invoicing` | **(C) TUZAK** | veri hazir ama IZIN MATRISI |
| | | | bu kapidan gecirmiyor |
| `average_margin` | `progress_payments` | **(C) TUZAK** | ortalama TANIMSIZ + anahtar |
| | | | YANLIS modulu gosteriyor |
| `risks` | (uc kaynak) | **(A) BAGLANDI** | RISK-1: zarf satirlasti, tek |
| | | | anahtar KAYNAK LISTESINE dondu |

Gerekceler kartlarin YANINDA durur, burada degil: bir kart baglandiginda ya da
kaldirilinca gerekcesi de onunla birlikte tasinsin.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import UNRESTRICTED, DisciplineScope, restricted_project_ids
from app.core.field_scope import maskele
from app.core.permissions import actor_scope, can_read, can_read_projects
from app.modules.approvals import service as approvals_service
from app.modules.dashboard.risks import build_risks
from app.modules.dashboard.schemas import (
    DashboardProjectCard,
    DashboardSummaryResponse,
    MetricPlaceholder,
    PendingApprovalsPlaceholder,
    RiskAlertsPlaceholder,
)
from app.modules.progress_payments.summary import cumulative_gross_by_projects
from app.modules.projects import service as projects_service
from app.modules.projects.costs import money_total
from app.modules.projects.models import ProjectStatus
from app.modules.projects.repository import list_projects_for_user
from app.modules.projects.schemas import metric, restricted
from app.modules.roles.models import Role
from app.modules.users.models import User

# Kart -> BESLEYEN modul anahtari. Ad "bekleyen" demeye devam etse de artik
# yalnizca KAYNAGI isaret eder (P-YT1 `_worker_count` emsali): bagli bir kart da
# anahtarini tasir, cunku ekran "bu sayi nereden geliyor" sorusunu sormaya devam
# eder. Anahtar STRING DEGERLERI DEGISTIRILMEDI — frontend bunlara dallanabilir
# ve deger degisimi sozlesme kirilmasi olurdu (bkz. `_MARGIN_MODULE` notu).
_PORTFOLIO_MODULE = "progress_payments"
_RECEIVABLES_MODULE = "invoicing"
_MARGIN_MODULE = "progress_payments"
_APPROVALS_MODULE = "approvals"

#: Proje kartlarinin ALAN kapisi. `pending_module` anahtarlarindan farklidir:
#: bu deger yanitin GOVDESINE yazilmaz, yalnizca `can_read` kapisini adlandirir.
_PROJECTS_MODULE = "projects"

#: 🔴 YALNIZ SAYIM ISTIYORUZ. `pending_for_user` sayfayi ve TOPLAMI ayri
#: sorgulardan uretir; `limit=0` sayfayi bos birakir ve satir zenginlestirmesini
#: (adimlar · kullanici adlari · UC EVRAK AILESI) HIC calistirmaz.
#: OLCULDU: `limit=0` -> 13 sorgu, `limit=1` -> 19, `limit=50` -> 19.
#: Rozet icin alti sorgu bosuna odenmez; `total` ayni suzgecten gelir.
_ONLY_TOTAL = 0


async def _pending_approvals(session: AsyncSession, user: User) -> PendingApprovalsPlaceholder:
    """(A) — "Onay Bekleyenler" rozeti. Repodaki EN TAZE bayat yer tutucuydu:
    bekledigi motor OK-1A ile canliya cikti, OK-1C ile fiilen isletilebilir oldu.

    🔴 SUZGEC KOPYALANMADI, SERVIS CAGRILDI (K3). Kutunun kurali
    `approvals/repository.py:_pending_filter`ta DORT kosuldur (adim rolu · kendi
    evraki · gorevler ayriligi · proje kapsami). Ikinci bir kopya bugun ayni
    sayiyi verse bile ilk kural degisikliginde sessizce ayrisirdi: panelde
    gorunen sayi ile kutuda gorunen SATIR SAYISI birbirini tutmazdi.

    🔴 KAPSAM AYNEN KORUNUR ve BEDAVA DEGILDIR. Motor kapsami
    `projects.service.visible_projects` uzerinden KENDI cozer; panelin elindeki
    `projects` listesi ONA VERILEMEZ, cunku iki kume AYNI DEGILDIR: panel
    `list_projects_for_user`i cagirir (admin atlamasi YOK), motor ise
    `projects: admin` rolu icin TUM projelere gecer. Kapsami disaridan
    beslemek, admin bir aktorun rozetini sessizce daraltirdi.

    ⚠️ OLCULMUS MALIYET (bu dilimde eklenen):
      * onay rolu OLMAYAN aktor (cogunluk): **+1 sorgu** — motor rol kumesi
        bosken erkenden doner.
      * onay rolu TASIYAN aktor: **+13 sorgu**; bunun **7'si** panelin zaten
        kostugu proje okumasinin (`visible_projects`) tekrarididir. Tekrari
        silmek motorun IMZASINI degistirmeyi gerektirir (kapsami disaridan
        almak) — o `approvals/` dilimidir, burada YAPILMADI.
      Ikisi de SATIR SAYISINDAN BAGIMSIZDIR (bekcisi:
      `test_panelin_sorgu_sayisi_SATIR_SAYISINDAN_BAGIMSIZ`).

    🔴 `available=True` HER ZAMAN, `count=0` olsa bile (K2). Kaynak canli
    oldugu icin sifir artik OTORITER bir olgudur ("sizden bekleyen imza yok"),
    "bilinmiyor" degil. `CountPlaceholder` notundaki emsal aynen gecerlidir:
    dolu zarf `pending_module` tasimaya DEVAM eder.

    🔴 `items` BOS KALIR ve bu bir SONUCTUR. Mockup satiri
    (`Ekran 1 - Gosterge Paneli.dc.html:310-330`) DORT olgu basar: baslik ·
    tutar · goreli zaman · aciliyet cipi. `items: list[str]` bunlardan BIRINI
    tasir; ucunu bir metne yapistirmak, ekranin ayristirmak zorunda kalacagi
    bir sunum kararini sunucuda uretmek olurdu (K10). Ustelik "Acil" cipi bir
    KARAR metnidir ve motor bilincli olarak karar alani dondurmez (kanon E).
    Satiri tasiyabilecek bir zarf ADDITIVE bir sema isidir ve F-OK'undur.

    ⚠️ KAPSAM SINIRI (yalan degil, EKSIKLIK): motor bugun UC evrak ailesi
    tanir (taseron hakedisi · isveren hakedisi · satin alma talebi); mockup'in
    bes kartindan BORDRO ve GUNLUK KAYIT OK-1B'nindir. Rozet bu yuzden kutunun
    gosterdigi kumeyle BIREBIR ayni eksigi tasir — ve tam da servisi CAGIRDIGI
    icin OK-1B geldiginde IKISI BIRDEN buyur. Bir kopya buyumezdi.
    """
    _sayfa, toplam, _roller = await approvals_service.pending_for_user(
        session, user, limit=_ONLY_TOTAL, offset=0
    )
    return PendingApprovalsPlaceholder(
        available=True, count=toplam, pending_module=_APPROVALS_MODULE
    )


async def _portfolio(
    session: AsyncSession, user: User, scope: DisciplineScope = UNRESTRICTED
) -> MetricPlaceholder:
    """(A) — "Portfoy · Toplam Hakedis" (mockup `:196`). **BAGLANDI.**

    HANGI AILE: **ISVEREN** hakedisi (taseron DEGIL). OLCULDU — kart HASILATTIR:
    mockup `Ekran 1 - Gosterge Paneli.dc.html:199` `24.870.500` basar; AYNI sayi
    AYNI `↑ %8,3` degisimiyle `Ekran 11 - Mali Tablo.dc.html:99-100`ta
    **"Is Hasilati"** olarak, `Muhasebe - Hesap Plani.dc.html:193`te ise
    **`600 · Yurt Ici Satislar · Gelir`** hesabinin altinda gecer. Taseron
    hakedisi BORCTUR (`projects/costs.py:220` onu MALIYET sayar) ve buraya
    TOPLANMAZ — toplanmasi kartin isaretini ters cevirirdi.

    HANGI KUME: yalnizca `approved` + `paid`. Taslak ve onay bekleyen HARICTIR.

    🔴 KAYNAK KOPYALANMADI, SERVIS CAGRILDI (K3).
    `progress_payments.summary.cumulative_gross_by_projects` tam bu suzgeci
    (`repository.COMPLETED_STATUSES`) zaten uygular ve TEK toplu okumadir (proje
    basina sorgu YOK). Ikinci bir suzgec kopyasi bugun ayni sayiyi verse bile
    ilk kural degisikliginde sessizce ayrisirdi. Toplama da elle yapilmaz:
    `projects.costs.money_total` "para nasil toplanir"in TEK kuralidir
    (`money_total([]) == Decimal("0.00")`).

    🔴 KAPSAM `projects.service.visible_projects` — gorunurlugun TEK kaynagi
    ("Tek kaynak burasidir"), `projects: admin` atlamasi DAHIL. Panelin kendi
    `list_projects_for_user(...)` listesi BILEREK KULLANILMADI: onda admin
    atlamasi YOKTUR, yani `projects:admin` tasiyan bir aktorun portfoyu sessizce
    EKSIK sayilirdi (ayni gerekce `_pending_approvals` notunda da var).

    🔴 IZIN KAPISI (K4 sizintisi): `can_read(..., "progress_payments")`.
    `require_permission` UCU kapatir, bu ise TUREV ALANI kapatir. OLCULDU
    (`roles/seed_data.py` MATRIX): paneli acabilen ama `progress_payments`i
    okuyamayan rol tam olarak **`hr_manager`**dir (`dashboard = _LIM`,
    `progress_payments = _N`). Kapi olmasa o rol sirketin TUM hasilat toplamini
    reddedildigi bir modulden okurdu.

    UC HAL, tam olarak:
      1. izin YOK -> `restricted()`: `available=False`, `pending_module=None`.
         "Bu modul daha yazilmadi" ile "bunu gormeye yetkin yok" FARKLI iki
         durumdur (kullanici karari 2026-08-27, ILR-1/2); buraya
         `progress_payments` yazmak ekrani YALANCI yapardi.
      2. izin VAR ama gorunur proje SIFIR -> `metric(None, ...)`: bos zarf,
         kaynagini bildirir. Soru HIC SORULMADI; uydurma bir `0.00` basilmaz.
      3. izin VAR ve >=1 gorunur proje -> `metric(money_total(...), ...)`:
         `available=True`. Buradaki `0.00` OTORITERDIR, "bilinmiyor" DEGIL —
         `progress_payments/project_progress.py:41` emsali ("Hakedis hic yoksa
         sonuc 0.00'dir — 'bilinmiyor' DEGIL") ve zaten bagli olan
         `pending_approvals` sayacinin sifiri aynidir.
    """
    # IZN-B3: izin PROJE BASINA (her projede O PROJEDEKI rolun hakedis Gorur'u; ekipte olmayan /
    # "Tum projeler" kisi ana rolle) ve DSC-B5 (Ü2): hasilat ticari → disiplinle KISITLI projeler
    # toplama GIRMEZ. Gorunur projelerin HICBIRI izinli degilse `restricted()`; proje yoksa bos.
    gorunur = await projects_service.visible_projects(session, user)
    if not gorunur:
        if scope.is_restricted or not await can_read(session, user, _PORTFOLIO_MODULE):
            return restricted()
        return metric(None, _PORTFOLIO_MODULE)
    izin = await can_read_projects(session, user, _PORTFOLIO_MODULE, [p.id for p in gorunur])
    kisitli = await restricted_project_ids(session, user.id)
    projects = [p for p in gorunur if izin[p.id] and p.id not in kisitli]
    if not projects:
        return restricted()
    totals = await cumulative_gross_by_projects(session, [p.id for p in projects])
    return metric(money_total(totals.values()), _PORTFOLIO_MODULE)


def _receivables() -> MetricPlaceholder:
    """(C) TUZAK — "Tahsil Edilecek" (mockup `:234`). 🔴 IZIN KAPISI ENGELI.

    Veri tarafi HAZIR ve tam eslesir: `invoicing/summary.py:80` hem tutari hem
    "3 fatura beklemede" sayacini TEK sorgulu bir okumadan uretir.

    BAGLANMADI, cunku iki ucun KAPISI FARKLIDIR ve baglamak yetki genislemesi
    olurdu:
      * `GET /dashboard/summary` -> `require_permission("dashboard", view)`
      * `GET /invoices/summary`  -> `require_permission("invoicing", view)`
    Tohumlanmis matriste (`roles/seed_data.py:175,195`) `hr_manager` icin
    `dashboard = _LIM` ama `invoicing = _N`; `site_chief` ve `field_engineer`
    icin de `invoicing = _N`. Yani bu kart baglanirsa fatura goremeyen UC ROL
    sirketin alacak toplamini gosterge panelinde OKUR.

    🔑 KARSITI `pending_approvals`tir ve fark YAPISALDIR: onay kutusunun AYRI
    bir yetki kapisi YOKTUR ve olmamalidir (`approvals/router.py:57`), cunku
    donen kume zaten "bu adim SANA dustu" olgusuyla sinirlidir.

    ⚠️ IKINCI kusur (kapi acilsa bile kalir): `receivable.amount` BRUT
    `Invoice.total`dir, kismi tahsilatin NETI DEGIL — %90'i tahsil edilmis bir
    fatura tutarinin tamamiyla katilir. "Tahsil EDILECEK" basligi altinda bu
    sayi sistematik olarak fazla gosterir.
    """
    return MetricPlaceholder(pending_module=_RECEIVABLES_MODULE)


def _average_margin() -> MetricPlaceholder:
    """(C) TUZAK — "Ortalama Marj" (mockup `:246`). 🔴 ANAHTAR DA YANLIS.

    ANAHTAR HATASI: `_MARGIN_MODULE = "progress_payments"` ama marjin gercek
    kaynagi `projects/costs.py:112` `_margin_pct`tir ve toplu okuyucusu
    `projects/cost_cards.py:174` `by_projects` (en fazla 3 sorgu) — yani anahtar
    HIC ILGISI OLMAYAN bir modulu isaret ediyor. String DEGISTIRILMEDI: deger
    yanitin govdesindedir ve frontend ona dallanabilir; duzeltmesi F-OK ile
    birlikte gitmelidir (kirici olabilir).

    ORTALAMA TANIMSIZ, ve bu bir isim tartismasi degil: `margin_pct` **taahhut
    projelerinde YAPISAL OLARAK `None`**dur — `card_projection`
    (`projects/costs.py:413-426`) taahhut dali TASIMAZ, cunku E4 taahhut
    kartinda marj alani hic BASILMAZ. Butcesi girilmemis ya da geliri 0 olan
    projeler de `None` doner. Taahhut agirlikli bir portfoyde "ortalama",
    projelerin SESSIZ BIR AZINLIGI uzerinden hesaplanirdi.
    🔴 K2 tam burada isirir: `None`lari 0 saymak uydurma bir sifirdir
    (`cost_summary.py:106-108` bunu ayrica yasaklar), elemek ise "hicbir
    projenin marji yok" hâli ile "ortalama %0" hâlini AYNI sayiya cevirir.
    ⛔ Once URUN KARARI: agirliksiz ortalama mi, `Σkâr / Σgelir` mi; `None`
    projeler paydada mi?

    🔴 DASH-1'de UC ENGEL DAHA OLCULDU — `portfolio` baglanirken bu kart
    BILEREK yer tutucu birakildi, cunku ucu de bagimsiz olarak tikayicidir:

    (a) HEDEF SAYI KENDI MOCKUP'INDAN URETILEMIYOR. Kart `%14,2` basar; ayni
        ekranda basilan DORT proje marjinin agirliksiz ortalamasi **14,7**,
        para-agirlikli `Σkâr / Σgelir` de **14,7** verir. Ustelik ayni kavram
        baska iki mockup'ta **14,1** ve **14,2** olarak gecer. Tutarli bir
        hedef YOK — hangi tanimi kodlasak mockup'lardan en az birini yalanlar.

    (b) KARTIN OZETLEDIGI POPULASYONDA MARJ YAPISAL OLARAK `None`. O ekrandaki
        DORT projenin DORDU de `taahhut`tur ve `projects/costs.py:427`
        `card_projection` TASARIM GEREGI taahhut dali TASIMAZ. Yani ortalama,
        tam da ozetledigi kume uzerinde tanimsizdir.

    (c) BAGLANSA BILE EKRAN YANLIS BASAR. Frontend bu karti `KpiCard` icinde
        `formatCompactCurrency` ile cizer: bagli bir YUZDE ekranda **"₺14,2"**
        gorunurdu, "%14,2" degil. Duzeltmesi frontend tarafindadir.
    """
    return MetricPlaceholder(pending_module=_MARGIN_MODULE)


async def _risks(
    session: AsyncSession, user: User, scope: DisciplineScope = UNRESTRICTED
) -> RiskAlertsPlaceholder:
    """✅ **BAGLANDI** (RISK-1) — "Risk & Uyarilar" (mockup `:375-400`).

    Eski notun UC kusuru da kapandi; gerekceler kartin YANINDA durur, yani
    `dashboard/risks.py`de. Ozeti:

    1. **ZARF** — satir artik `RiskAlert` (siddet + baslik + ayrinti + kaynak
       modul). `items: list[str]` uc olgudan yalnizca birini tasiyabiliyordu.
       🔴 Kartin ADI hâlâ icerigini kismen yalanliyor ve bu bir SEMA kusuru
       degil bir ISIMLENDIRME gercegidir: mockup'in ucuncu satiri
       ("Hedef asildi", YESIL) bir risk DEGIL iyi haberdir — bu yuzden zarf
       `RiskSeverity.success` uyesini GERCEKTEN uretir.
    2. **ANAHTAR** — tek `pending_module` SILINDI. Uc kaynagin uc ayri kapisi
       vardir; tek anahtar kartin ancak uctebirini adlandirabilirdi. Durum
       artik KAYNAK BASINA (`sources`) bildirilir.
    3. **IZIN** — her kaynak kendi kapisindan gecer ve kart KISMI dolar
       (`accounting` stok satirlarini gormez, hakedis satirlarini GORUR).

    ⚠️ K2 (`min_stock` NULLABLE) da kapandi: esigi girilmemis kalemler AYRI bir
    `warning` satiriyla SAYILIR — bos liste artik "risk yok" ile "risk
    bilinmiyor"u ayni sayiya cevirmiyor.
    """
    return await build_risks(session, user, scope)


async def build_summary(
    session: AsyncSession, user: User, scope: DisciplineScope = UNRESTRICTED
) -> DashboardSummaryResponse:
    """Gosterge paneli ozeti. Projeler + ONAY + PORTFOY + RISK gercek, iki kart bos.

    🔴 PROJE KARTLARININ ALAN KAPISI (K4 — turev alan sizintisi). Ucun kapisi
    `require_permission("dashboard", view)`tir ve o YETMEZ: kart `code` · `name`
    · `status` · **`budget`** · `progress_pct` tasir, yani AYNI VERI
    `GET /projects` ucundan da cikar ve orada `require_permission("projects",
    ...)` ile korunur. Kapi olmadan `projects` hucresi `none` yapilmis bir rol
    (`PUT /roles/{role_id}/permissions/{module_key}`) sirketin proje
    BUTCELERINI panelden okumaya devam ederdi.

    Bugune kadar kazara guvenliydi: tohum matrisinde `dashboard`
    (`seed_data.py:183`) ve `projects` (`:187`) satirlari BIREBIR ayni ve
    `test_seed_matrix.py` bunu kilitliyor — ama o kilit TOHUMUNDUR, CALISMA
    ANININ degil.

    Emsal ayni dosyadadir: `_portfolio` `can_read("progress_payments")` ile,
    `build_risks` UC ayri `can_read` ile kapalidir. Bu kart ISTISNAYDI.

    ⚠️ Kapi kapaliyken `projects=[]` doner ve `active_project_count` ondan
    TURETILDIGI icin 0 olur — sema DEGISMEZ, alanlar yerinde durur. Bos liste
    "proje yok" ile "yetkin yok"u ayirt etmez; ILR-1/2'nin ucuncu hâli AYRI bir
    zarf isterdi ve o KIRICI olurdu, bu onarimin kapsami disindadir.
    """
    # IZN-B2: kart hem SEVİYE (K4: `projects` görünmüyorsa kart hiç dolmaz; sayfa hücrelerinden
    # `gate_ok`) hem KAPSAM (aşağıdaki çapraz maske; `actor_scope` hibrit kuralı) kapısından
    # geçer. Eski tek-satır okuması ikiye ayrıldı: seviye artık sayfa hücresinden, kapsam eski
    # satırdan (ya da satırsız rolde `tum_tutarlar`dan) gelir. Sorgu sayısı
    # (`test_dashboard_pyt2_onay_sayaci.py`) bu yüzden güncellendi.
    # IZN-B3: proje karti izni PROJE BASINA (o projedeki rolun `projects` Gorur'u; ekipte olmayan /
    # "Tum projeler" kisi ana rolle).
    projects_kapsami = await actor_scope(session, user, _PROJECTS_MODULE)
    uyelik = await list_projects_for_user(session, user.id)
    izin = await can_read_projects(session, user, _PROJECTS_MODULE, [p.id for p in uyelik])
    projects = [p for p in uyelik if izin[p.id]]
    role = await session.get(Role, user.role_id)

    return DashboardSummaryResponse(
        role_name=role.name if role is not None else "",
        # Spec §5.5: taslaklar aktif proje sayacına GİRMEZ — status active AND NOT is_draft.
        active_project_count=sum(
            1 for p in projects if p.status is ProjectStatus.active and not p.is_draft
        ),
        projects=[
            # 🔴 KART, VERİNİN SAHİBİ OLAN MODÜLÜN KAPSAMIYLA DA MASKELENİR.
            #
            # Rota sınıfı `kapsam_rotasi("dashboard", …)`tır, yani sarmalayıcı
            # bu yanıta aktörün **`dashboard`** kapsamını uygular. Ama kartın
            # taşıdığı `budget`/`progress_pct` `projects` modülünün verisidir ve
            # `GET /projects` ucunda aktörün **`projects`** kapsamıyla maskelenir.
            # İki hücre AYRI AYRI ayarlanabilir (İzin Matrisi ekranı): `projects
            # = view/limited` + `dashboard = full/all` olan bir rol, projeler
            # ekranında göremediği bütçeyi panelden okurdu.
            #
            # Bu, yukarıdaki K4 notunun SEVİYE ekseninde kapattığı yan kapının
            # KAPSAM eksenidir — aynı cümle, aynı gerekçe. Sonuç fail-CLOSED'dır:
            # alan, iki kapsamdan HERHANGİ BİRİ gizliyorsa düşer (sarmalayıcı
            # `dashboard` kapsamını bunun ÜSTÜNE uygular).
            #
            # Tohumda iki satır birebir aynı olduğu için bugün sızıntı YOKTU;
            # kapatılan şey bir yapılandırma hâlidir. Bekçisi:
            # `tests/modules/test_kapsam_maskesi_uctan_uca_dashboard.py`
            # (`test_PANEL_proje_kartini_PROJECTS_kapsamiyla_da_maskeler`).
            maskele(DashboardProjectCard.model_validate(p), projects_kapsami)
            for p in projects
        ],
        portfolio=await _portfolio(session, user, scope),
        receivables=_receivables(),
        average_margin=_average_margin(),
        pending_approvals=await _pending_approvals(session, user),
        risks=await _risks(session, user, scope),
    )

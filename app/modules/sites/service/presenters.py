"""ORM satiri -> Pydantic yanit donusumleri + yer tutucu sayaclar.

DB'ye YAZMAZ, gorunurluk suzmez: yalnizca bir satiri ekranin gordugu
sekle cevirir. Yer tutucu zarflari (`MetricPlaceholder`/`CountPlaceholder`)
burada kurulur — alan TIPI degismesin diye zarf korunur, yalnizca doldurulur."""

import uuid
from collections.abc import Mapping
from decimal import Decimal

from app.core.timezone import today

# BOLUM BOQ SAYACLARININ TEK KAYNAGI `boq` modulüdür (BLM-SAY): `sites` kendi
# `SELECT`ini yazmaz — `timesheet/counts.py` ile ayni gerekce (iki sayim mantigi
# zamanla ayrisir ve bolum satiri ile bolum detayi farkli sayi gosterir).
from app.modules.boq.counts import EMPTY as _BOQ_EMPTY
from app.modules.boq.counts import SectionBoqTotals
from app.modules.projects.models import Project
from app.modules.projects.schemas import restricted
from app.modules.sites.models import Section, SectionMilestone, SectionStatus, Site, SiteStatus
from app.modules.sites.schemas import (
    CountPlaceholder,
    MetricPlaceholder,
    SectionDetailResponse,
    SectionMilestoneResponse,
    SectionResponse,
    SectionStatusCounts,
    SectionTypeRead,
    SiteCard,
    SiteCounts,
    SiteDetailResponse,
    SiteFacilities,
    SiteListTotals,
    SiteProjectSummary,
)

# Spec §3: bos durum alanlari ve bagli olduklari dilim anahtarlari. Bunlar
# MODUL ANAHTARIDIR, kullaniciya gosterilecek metin degil (B6 §2.3).
#
# 🔴 P-YT2 DENETIMI (2026-08-23) — IKI AYRI SEY OLCULDU.
#
# (1) ANAHTAR ARTIK "MODUL YOK" DEMEK DEGIL: adlandirilan modullerin hepsi
#     CANLI. Bekleyen sey modul degil, o modulden turemesi gereken DEGER ya da
#     bir URUN KARARIDIR.
# (2) 🔴 ANAHTAR UZAYI IZIN MODULU UZAYINDAN AYRISMIS. Asagidaki yorum
#     *"Bunlar MODUL ANAHTARIDIR"* diyor, ama ALTI anahtarin IKISI tohumlanmis
#     modul kaydinda (`roles/seed_data.py:MODULES`, 21 anahtar) YOKTUR:
#     `subcontracts` (hic olmadi; canlisi `contracts`) ve `project_costs`
#     (KAVRAMSAL anahtar — `projects/cards.py` de ayni adi kullanir).
#     Ikisi de DEGISTIRILMEDI (deger yanittadir) ve bir bekciyle cakildi:
#     `test_anahtar_uzayi_IZIN_MODULU_uzayindan_AYRISMIS`.
#
# Olculen tablo — 🔴 BLM-SAY (2026-08-27) IKI SATIRI KAPATTI:
#
# | anahtar | modul canli mi | sinif | tek cumlelik gerekce |
# |---|---|---|---|
# | `_TIMESHEET`   | ✅ | **(A)** | T4'te BAGLANDI (`_worker_count`, 3 yerde) |
# | `_BOQ` sayac   | ✅ | **(A)** | ✅ BLM-SAY'de BAGLANDI (`_boq_item_count`) — PAYDA basilir |
# | `_BOQ` bedel   | ✅ | **(A)** | ✅ BLM-SAY'de BAGLANDI (`_boq_budget`) — iki engel de kalkti |
# | `_PROGRESS_PAYMENTS` | ✅ | **(B)** ilerleme | turevin KENDISI besleyende de yer tutucu |
# | `_PROGRESS_PAYMENTS` | ✅ | **(C)** hakedis | santiye kirilimi YARIM (bkz. `to_detail`) |
# | `_CONTRACTS`   | ✅ | **(C)** | SANTIYE duzeyinde sozlesme bedeli SEMADA YOK |
# | `_PROJECT_COSTS` | ⚠️ kavramsal | **(C)** | "ortalama marj" TANIMSIZ (bkz. `_totals`) |
# | `_SUBCONTRACTS`| ❌ | **(C)** | 🔴 BOYLE BIR MODUL YOK — canlisi `contracts` |
#
# Gerekcelerin TAMAMI alanlarin kuruldugu yerdedir (`to_section`, `to_detail`,
# `_totals`), burada degil: bir alan baglandiginda gerekcesi de onunla gider.
_PROGRESS_PAYMENTS = "progress_payments"
_TIMESHEET = "timesheet"
#: 🔴 BAYAT ETIKET, BILINCLI OLARAK DEGISTIRILMEDI. `subcontracts` diye bir
#: modul YOKTUR ve hic olmadi; taseron sozlesmeleri `contracts` modulunde
#: yasiyor (`SubcontractorContract`). Etiket yanittaki `pending_module`
#: DEGERIDIR — frontend ona dallanabilir, degistirmek sozlesme kirmasidir.
#: Duzeltmesi F-OK ile birlikte gitmelidir (P-YT2 raporu).
_SUBCONTRACTS = "subcontracts"
#: ⚠️ IZIN MODULU DEGILDIR (`MODULES`te yok) — KAVRAMSAL bir kaynak adidir ve
#: `projects/cards.py` de ayni adi kullanir. Ayrisma bilincli olarak
#: KORUNDU; tek anahtar uzayina cekmek frontend devriyle gitmelidir.
_PROJECT_COSTS = "project_costs"
_CONTRACTS = "contracts"
_BOQ = "boq"


def _metric(pending_module: str) -> MetricPlaceholder:
    return MetricPlaceholder(pending_module=pending_module)


def _count(pending_module: str) -> CountPlaceholder:
    return CountPlaceholder(pending_module=pending_module)


def _boq_item_count(value: int) -> CountPlaceholder:
    """`_BOQ` sayac yer tutucusunun BAGLANMIS hali (BLM-SAY).

    Zarf (`CountPlaceholder`) KORUNUR, yalnizca doldurulur: `available=True` +
    gercek `count` + `pending_module` AYNEN kalir (`_worker_count` emsali —
    dolu `CountPlaceholder` kaynak modulu isaretlemeye devam eder).

    🔴 SAYAC NEYIN KUMESI: "bolume EN AZ BIR tahsis satiri dusmus FARKLI poz"
    sayisi — sorgu govdesi `boq/counts.py::by_section`dedir. Santiyenin tum
    pozlari DEGIL, "tamamlanan" poz DEGIL. Mockup'in "16 / 26" ikilisinin
    PAYDASIDIR; PAYIN kaynagi repoda YOKTUR (tahsiste "tamamlandi" bayragi yok,
    gerceklesen taraf `progress_pct` hâlâ yer tutucu) ve UYDURULMADI.

    Tahsisi olmayan bolum `0` doner, yer tutucuya DUSMEZ: birlesim anahtarlarinin
    ikisi de NOT NULL oldugu icin bos kume "kayit baglanmamis" anlamina GELEMEZ
    (K2 tuzagi burada yapisal olarak yok; gerekce `boq/counts.py`de).
    """
    return CountPlaceholder(available=True, count=value, pending_module=_BOQ)


def _boq_budget(value: Decimal) -> MetricPlaceholder:
    """`_BOQ` bedel yer tutucusunun BAGLANMIS hali (BLM-SAY).

    Deger = Σ (bolume tahsis edilen miktar × pozun birim fiyati), carpim SATIR
    BASINA `boq.schemas.quantize_money` ile yuvarlanir — o para formulunun TEK
    kopyasidir (K3). Ikinci bir kopya (SQL'de `SUM(quantity*unit_price)`) BOQ
    ekraninin kendi toplamindan kurus farkli bir "Bolum Bedeli" uretirdi.

    🔴 Dolu `MetricPlaceholder` `pending_module` TASIMAZ (P10 T3 zarf kurali,
    pydantic duzeyinde bagli) — `CountPlaceholder`taki emsalin TERSI. Iki zarf
    sinifinin kurallari FARKLIDIR ve ikisi de kendi siniflarinin notunda
    gerekcelidir; burada tek yerde uygulanir.
    """
    return MetricPlaceholder(available=True, value=value)


def _worker_count(value: int) -> CountPlaceholder:
    """T4 — `_TIMESHEET` yer tutucusunun BAGLANMIS hali (spec §4).

    Zarf (`CountPlaceholder`) KORUNUR, yalnizca doldurulur: `available=True` +
    gercek `count`. Kartin diger sayaclari (`boq_item_count`, `subcontractor_count`,
    `progress_pct`...) hâlâ yer tutucudur; alanin TIPINI degistirmek ekranin ayni
    seridinde iki farkli sozlesme birakirdi. `pending_module` kaynak modulu
    isaretlemeye devam eder — artik "bekleyen" degil "besleyen" moduldur.
    """
    return CountPlaceholder(available=True, count=value, pending_module=_TIMESHEET)


def _remaining_days(site: Site) -> int | None:
    """Spec §4.2. `completed` veya `end_date` yoksa null; gecmisse NEGATIF.

    Kirpma YAPILMAZ: gecikmeyi 0'a yuvarlamak backend'in gercegi bastirmasidir,
    gecikmeyi kirmizi gostermek frontend'in isidir.
    """
    if site.status is SiteStatus.completed or site.end_date is None:
        return None
    return (site.end_date - today()).days


def _resolve_city(site: Site, project: Project) -> tuple[str | None, bool]:
    """Spec §4.3: santiye sehri bossa PROJENIN sehri doldurulur ve bayraklanir.

    Boylece frontend "Kuyubasi Mah. Ankara" satirini her zaman basabilir, null
    dallanmasi tasimaz. Ikisi de bossa devralma YOKTUR — bayrak false kalir.
    """
    if site.city:
        return site.city, False
    if project.city:
        return project.city, True
    return None, False


def _section_counts(sections: list[Section]) -> SectionStatusCounts:
    return SectionStatusCounts(
        planned=sum(1 for s in sections if s.status is SectionStatus.planned),
        active=sum(1 for s in sections if s.status is SectionStatus.active),
        completed=sum(1 for s in sections if s.status is SectionStatus.completed),
    )


def _facilities(site: Site) -> SiteFacilities:
    """DB'deki 8 duz Boolean kolonu API'nin GRUPLU sozlesmesine cevirir (§4.1).

    Donusum SERVIS katmanindadir: sema kendi basina DB bilmez.
    """
    return SiteFacilities(
        closed_warehouse=site.has_closed_warehouse,
        open_storage=site.has_open_storage,
        cold_storage=site.has_cold_storage,
        site_office=site.has_site_office,
        canteen=site.has_canteen,
        changing_room_wc=site.has_changing_room_wc,
        dormitory=site.has_dormitory,
        infirmary=site.has_infirmary,
    )


def _to_milestone(row: SectionMilestone) -> SectionMilestoneResponse:
    return SectionMilestoneResponse(
        id=row.id,
        title=row.title,
        milestone_date=row.milestone_date,
        sort_order=row.sort_order,
    )


def to_section(
    section: Section,
    worker_count: int,
    boq_totals: SectionBoqTotals = _BOQ_EMPTY,
    progress: MetricPlaceholder | None = None,
) -> SectionResponse:
    """Bolum satiri. DORT yer tutucusundan IKISI BLM-SAY'de BAGLANDI.

    ✅ `progress_pct` — **ILR-1'DE BAGLANDI (2026-08-27).**

    Eski not *"(B) GECERLI, YER TUTUCU KALIR"* diyordu ve gerekcesi DOGRUYDU:
    burada bir yuzde uretmek, BOQ'nun bilerek acik biraktigi formulu ikinci ve
    daha dar bir baglamda ICAT etmek olurdu; iki ekran ayni bolum icin farkli
    "%" basardi. Mockup (`Bölüm Detay.dc.html:71-73`, %62) bu yuzdeyi BoQ
    tablosunun "Gerç. %" toplamiyla AYNI sayi olarak cizer.

    🔑 **O SART KARSILANDI: formul ICAT EDILMEDI, TEK KAYNAKTAN CAGRILIYOR.**
    Yuzde `boq/progress.py`de BIR kez yazildi; bu presenter da BOQ ekrani da
    proje karti da AYNI fonksiyonu cagirir — ikinci bir carpim YOKTUR (K3).
    Kaynak `progress_payments` DEGIL **gonderilmis santiye gunlugudur** ve
    formul PARA AGIRLIKLIDIR; PAYDA bu baglamda BOLUM TAHSISIDIR
    (`BoqItemSectionAllocation`), pozun santiye kotasi degil.

    🔴 Zarf **IZNE DUYARLIDIR**: `sites`i okuyup `site_diary`yi okuyamayan
    roller VAR (olculdu: `accounting`, `hr_manager`, `procurement`); onlarda
    `restricted()` doner. Varsayilan da `restricted()`tir (fail-closed) —
    izin olcmeyi unutan yeni bir cagri yeri sessizce yuzde BASAMAZ.

    ✅ `boq_item_count` — **BLM-SAY'de BAGLANDI.** P-YT2'nin (C) gerekcesi
    ("mockup TEK sayi basmiyor: 16 / 26") OLCULDU ve YARISI hâlâ gecerli:
    PAYIN kaynagi yok, PAYDA var. Zarf tek `int` tasidigi icin PAYDA basilir —
    "bu bolumde 26 is kalemi var" DOGRU bir cumledir; payi uydurmak yalan
    olurdu. Kalinti raporlandi (bkz. `_boq_item_count` ve `boq/counts.py`).

    ✅ `budget` — **BLM-SAY'de BAGLANDI.** P-YT2'nin (C) gerekcesi IKI ENGEL
    sayiyordu ve İKİSİ DE KALKTI:
      * *para formulu* — `boq.schemas.quantize_money` P-YT3'te ADI ACILDI (alt
        cizgi kaldirildi) ve ikinci kopyasi silindi; artik CAGRILABILIR TEK
        kopyadir, burada ikinci bir carpim YAZILMAZ.
      * *toplu okuyucu yok* — `boq/counts.py::by_section` yazildi, TEK sorgu
        (`section_allocations_for_site` BOLUM BASINA sorguydu, N+1 acardi).
    BLF-B1: eski elle `budget_amount` kolonu artik okunmaz; bedel TEK kaynak budur.

    ✅ `worker_count` — T4'te baglandi (`_worker_count`).

    `boq_totals` VARSAYILANLI degil de zorunlu olsaydi daha guvenli olurdu, ama
    varsayilan BILINCLI: `_BOQ_EMPTY` "tahsis yok" demektir ve tahsisi olmayan
    bolumun DOGRU cevabidir — cagiranin sozlugunde bulunmayan bolum icin de
    ayni deger gecer.
    """
    return SectionResponse(
        id=section.id,
        slug=section.slug,
        code=section.code,
        name=section.name,
        status=section.status,
        manager_user_id=section.manager_user_id,
        manager_name=section.manager_name,
        start_date=section.start_date,
        end_date=section.end_date,
        sort_order=section.sort_order,
        # ✅ ILR-1'DE BAGLANDI — kaynak `boq.progress` (TEK kaynak, K3).
        # 🔴 Varsayilan `restricted()`: cagiran izni OLCMEDIYSE alan KAPALI
        # dogar. Fail-closed bilinclidir — varsayilan "bagli degil" olsaydi,
        # izin olcmeyi unutan yeni bir cagri yeri sessizce yuzde basardi.
        progress_pct=progress if progress is not None else restricted(),
        boq_item_count=_boq_item_count(boq_totals.item_count),
        budget=_boq_budget(boq_totals.amount),
        worker_count=_worker_count(worker_count),
        # BLM-SAY: kayitli kolonlar LISTE yanitina da girer. BLF-B1: `budget_amount` kalkti.
        planned_worker_count=section.planned_worker_count,
        # P11 (spec §3): iki alan da TEK donusturucuden gectigi icin bolum basan
        # UC yuzeyde (detay, liste, santiye detayi) ayni anda dogar. Milestone
        # sirasi DETERMINISTIKTIR — `Section.milestones` iliskisi
        # `(sort_order, id)` ile siralidir, burada yeniden siralanmaz.
        depends_on_section_id=section.depends_on_section_id,
        milestones=[_to_milestone(row) for row in section.milestones],
    )


def to_section_detail(
    section: Section,
    worker_count: int,
    boq_totals: SectionBoqTotals = _BOQ_EMPTY,
    progress: MetricPlaceholder | None = None,
) -> SectionDetailResponse:
    """P6 §5 — bolum detay govdesi: `to_section`in TUM alanlari + T1 kolonlari.

    Zarflar `to_section`ten AYNEN devralinir (yeniden kurulmaz): bagli sayaclar
    da bos yer tutucular da tek yerde uretilir, aksi hâlde liste ve detay
    ekranlari zamanla farkli sayi/anahtar gosterirdi.

    🔴 `planned_worker_count` BURADA ARTIK VERILMEZ — BLM-SAY'de
    `to_section`e tasindi ve `model_dump()` ile gelirler. Ikisini de burada
    tekrar gecmek `TypeError` verirdi; sessiz bir ayrisma degil, gurultulu bir
    hata — istenen budur.
    """
    return SectionDetailResponse(
        **to_section(section, worker_count, boq_totals, progress).model_dump(),
        site_id=section.site_id,
        section_type=(
            SectionTypeRead.model_validate(section.section_type)
            if section.section_type is not None
            else None
        ),
        description=section.description,
        deputy_manager_user_id=section.deputy_manager_user_id,
        deputy_manager_name=section.deputy_manager_name,
        is_draft=section.is_draft,
        created_at=section.created_at,
        updated_at=section.updated_at,
    )


def _card_fields(
    site: Site, project: Project, worker_count: int, progress: MetricPlaceholder | None = None
) -> dict:
    """Santiye kartinin ortak alanlari (`to_card` + `to_detail`).

    🔴 `progress_pct` — **(B) GECERLI**, `to_section`taki AYNI gerekce: bekleyen
    sey `progress_payments` modulu degil, BOQ'nun kendi yer tutucusu olan
    gerceklesme yuzdesidir. Bolum ile santiye ayni turevden beslenmelidir;
    burada ayri bir formul acmak ikisini ilk gunden ayristirirdi.
    """
    city, city_inherited = _resolve_city(site, project)
    return {
        "id": site.id,
        "code": site.code,
        "slug": site.slug,
        "name": site.name,
        "status": site.status,
        "address": site.address,
        "city": city,
        "city_inherited": city_inherited,
        "site_manager_name": site.site_manager_name,
        "start_date": site.start_date,
        "end_date": site.end_date,
        "delivery_date": site.delivery_date,
        "remaining_days": _remaining_days(site),
        "section_count": len(site.sections),
        "worker_count": _worker_count(worker_count),
        # ✅ ILR-1'DE BAGLANDI — kaynak GUNLUK (`boq.progress`), PAYDA santiye
        # BOQ'u. Fail-closed: cagiran izni olcmediyse `restricted()`.
        "progress_pct": progress if progress is not None else restricted(),
        # --- Santiye formu genislemesi (§6.2): YALNIZ EKLEME ---
        "is_draft": site.is_draft,
        "site_manager_user_id": site.site_manager_user_id,
        "safety_officer_user_id": site.safety_officer_user_id,
        "safety_officer_name": site.safety_officer_name,
        "safety_officer_is_outsourced": site.safety_officer_is_outsourced,
        "neighborhood": site.neighborhood,
        "parcel": site.parcel,
        "gps_coordinates": site.gps_coordinates,
        "land_area_m2": site.land_area_m2,
        "construction_area_m2": site.construction_area_m2,
        "floor_info": site.floor_info,
        "budget": site.budget,
        "facilities": _facilities(site),
        "electricity_subscription_no": site.electricity_subscription_no,
        "water_subscription_no": site.water_subscription_no,
        "planned_worker_count": site.planned_worker_count,
    }


def to_card(
    site: Site, project: Project, worker_count: int, progress: MetricPlaceholder | None = None
) -> SiteCard:
    return SiteCard(**_card_fields(site, project, worker_count, progress))


def to_detail(
    site: Site,
    project: Project,
    worker_count: int,
    section_worker_counts: Mapping[uuid.UUID, int],
    section_boq_totals: Mapping[uuid.UUID, SectionBoqTotals],
    section_progress: Mapping[uuid.UUID, MetricPlaceholder] | None = None,
    site_progress: MetricPlaceholder | None = None,
) -> SiteDetailResponse:
    """Santiye detayi. IKI yer tutucusu P-YT2'de denetlendi, IKISI de **(C)**.

    🔴 `total_progress_payment` — **(C) TUZAK: SANTIYE KIRILIMI YARIM.**
    Modul canli ama santiye duzeyinde bir "toplam hakediş" ancak YARIM
    olabilir, cunku iki hakediş ailesi santiyeyi FARKLI derinlikte tanir:
      * isveren hakedişi PROJEYE baglidir, santiye yalniz SATIR duzeyindedir
        (`ProgressPaymentLine.site_id`) — toplanabilir;
      * taseron hakedişinin KENDISINDE `site_id` YOKTUR (`project_id` + bilgi
        amacli `section_id`); santiyeye ancak SOZLESME uzerinden inilebilir ve
        `SubcontractorContract.site_id` NULLABLE'dir.
    Yani buraya yazilacak sayi hem "hangi yarisi?" sorusunu cevapsiz birakir,
    hem de 🔴 K2'ye carpar: sozlesmesi santiyesiz acilmis bir santiyede sonuc
    `0` cikar ve "hakediş YOK" ile "hakediş BU SANTIYEYE BAGLANMAMIS" ayni
    sayiyi uretir. Yer tutucu bu ikisini ayirmaya devam eder.
    Mockup (`Şantiye Detay.dc.html:123-127`) tek kutu cizer ve altina
    `/ ₺11,2M` koyar — yani asagidaki `contract_amount`la BIRLIKTE anlamlidir;
    ikisinden biri uydurulursa oran da uydurulmus olur.

    🔴 `contract_amount` — **(C) TUZAK: KAYNAK SEMADA YOK.** "Santiyenin
    sozlesme bedeli" diye bir buyukluk bu semada BULUNMUYOR:
      * `ProjectContract` PROJE duzeyindedir ve santiyelere bolunmez;
      * `SubcontractorContract`in `amount` KOLONU HIC YOKTUR (bedel kalemlerden
        turer, modelin kendi notu) ve `site_id`si NULLABLE'dir.
    Bir sayi basmak, var olmayan bir DAGITIM KURALI icat etmek olurdu (projenin
    bedelini santiyelere hangi olcute gore boleriz?). Bu bir toplu okuyucu
    eksigi degil, bir URUN KARARI eksigidir.
    """
    sections = list(site.sections)
    return SiteDetailResponse(
        **_card_fields(site, project, worker_count, site_progress),
        project=SiteProjectSummary.model_validate(project),
        section_status_counts=_section_counts(sections),
        sections=[
            to_section(
                s,
                section_worker_counts.get(s.id, 0),
                section_boq_totals.get(s.id, _BOQ_EMPTY),
                (section_progress or {}).get(s.id),
            )
            for s in sections
        ],
        total_progress_payment=_metric(_PROGRESS_PAYMENTS),
        contract_amount=_metric(_CONTRACTS),
    )


def _totals(active_worker_count: int) -> SiteListTotals:
    """Alt KPI seridi (mockup `Proje Detay - Şantiyeler.dc.html:176-193`).

    T4'te YALNIZ `active_worker_count` baglandi. P-YT2 kalan UCUNU denetledi;
    UCU DE **(C)** olarak KALDI — "kendi dilimini bekliyor" ifadesi ARTIK
    DOGRU DEGIL, cunku uc modul de canli; bekleyen sey karardir.

    🔴 `total_progress_payment` — `to_detail`teki AYNI yarim kirilim; ustelik
    bu serit SANTIYE DEGIL PROJE kapsamlidir (bu fonksiyon proje toplamini
    alir), yani "santiyelerin toplami" ile "projenin toplami" ayrimi da
    cevaplanmis olmali.

    🔴 `subcontractor_count` — **CIFT KUSUR.**
    (1) Anahtar (`_SUBCONTRACTS = "subcontracts"`) VAR OLMAYAN bir modulu
        gosteriyor; canlisi `contracts`.
    (2) Mockup "18 firma" basiyor (`:183`) — yani AYRIK FIRMA sayisi, sozlesme
        sayisi DEGIL (ayni ekran ailesinde "Aktif Sözleşme" AYRI bir KPI'dir,
        `Taşeron Listesi.dc.html:35-36`). Ama `SubcontractorContract`ta
        `subcontractor_id` NULLABLE'dir ve yanina serbest metin
        `subcontractor_name` konur. `COUNT(DISTINCT subcontractor_id)` serbest
        metinle acilmis HER sozlesmeyi SESSIZCE DUSURUR.
        🔴 K2 tam burada isirir: sozlesmelerinin tamami serbest metinle
        acilmis bir projede sonuc `0` cikar — "taseron YOK" ile "taseronlar
        KAYIT ALTINDA DEGIL" ayni sayiyi uretir. Yer tutucu bu ikisini
        ayirmaya devam eder, bagli bir `0` ayirmaz.

    🔴 `average_margin` — modul canli (`projects/cost_cards.py:174`) ve marj
    formulu tek kopya (`projects/costs.py:112`), ama ORTALAMA TANIMSIZDIR:
    `margin_pct` TAAHHUT projelerinde YAPISAL OLARAK `None`dur (kart
    izdusumunde taahhut dali yoktur), butcesi girilmemis projelerde de `None`.
    Taahhut agirlikli bir portfoyde "ortalama", projelerin sessiz bir
    azinligindan hesaplanirdi. `None`lari 0 saymak uydurma sifir yasagina
    carpar; elemek ise "hicbir projenin marji yok" ile "ortalama %0"i ayni
    sayiya cevirir (yine K2).
    """
    return SiteListTotals(
        total_progress_payment=_metric(_PROGRESS_PAYMENTS),
        subcontractor_count=_count(_SUBCONTRACTS),
        active_worker_count=_worker_count(active_worker_count),
        average_margin=_metric(_PROJECT_COSTS),
    )


def _site_counts(sites: list[Site]) -> SiteCounts:
    return SiteCounts(
        all=len(sites),
        active=sum(1 for s in sites if s.status is SiteStatus.active),
        on_hold=sum(1 for s in sites if s.status is SiteStatus.on_hold),
        completed=sum(1 for s in sites if s.status is SiteStatus.completed),
        # §5.2: TEK ekleme. Taslaklar durum sayaclarindan DUSULMEZ — durumlari
        # ne ise o sayilir; bu sayac ayrica artar.
        draft=sum(1 for s in sites if s.is_draft),
    )

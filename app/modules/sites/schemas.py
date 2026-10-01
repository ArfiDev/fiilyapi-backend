import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# B6/P1 yer tutucu sozlesmesi TEK yerde tanimlidir (spec §3): kopyalanmaz,
# projects modulunden import edilir.
from app.core.field_scope import Gorunurluk
from app.modules.projects.schemas import CountPlaceholder, MetricPlaceholder
from app.modules.sites.models import SECTION_TYPE_NAME_MAX_LEN, SectionStatus, SiteStatus

__all__ = [
    "CountPlaceholder",
    "MetricPlaceholder",
    "SectionCreate",
    "SectionDetailResponse",
    "SectionListResponse",
    "SectionMilestoneInput",
    "SectionMilestoneResponse",
    "SectionResponse",
    "SectionStatusCounts",
    "SectionTypeCreate",
    "SectionTypeRead",
    "SectionUpdate",
    "SiteCard",
    "SiteCounts",
    "SiteCreate",
    "SiteDetailResponse",
    "SiteFacilities",
    "SiteFacilitiesInput",
    "SiteListResponse",
    "SiteListTotals",
    "SiteOptionListResponse",
    "SiteOptionResponse",
    "SiteProjectSummary",
    "SiteSectionInput",
    "SiteUpdate",
]


# --- Tesisler (spec §4.1) ---


class SiteFacilitiesInput(BaseModel):
    """Mockup 153-155 (depo) + 161-165 (tesis). DB'de 8 DUZ Boolean kolon (§4).

    API'de GRUPLU, DB'de duz: grup, mockup'taki gorsel kumelenmeyi tasir; duz
    kolonlar ise sorgulanabilir ve indekslenebilir kalir (§4.2, JSONB DEGIL).

    Sekizinin de varsayilani `False` — mockup'taki on-isaretler ORNEK VERIDIR,
    varsayilan degildir (§14.2, karar 2026-07-30).
    """

    closed_warehouse: bool = False
    open_storage: bool = False
    cold_storage: bool = False
    site_office: bool = False
    canteen: bool = False
    changing_room_wc: bool = False
    dormitory: bool = False
    infirmary: bool = False


class SiteFacilities(SiteFacilitiesInput):
    """Cikis karsiligi — girisle AYNI sekiz alan (§6.2). Ayri sinif, cunku
    giris/cikis sozlesmeleri zamanla ayrisabilir; bugun ayrisma yok."""


# --- Bolum ---


class SectionMilestoneResponse(BaseModel):
    """P11 — kilometre tasi cikis satiri (`Form - Bolum Ekle` 120-125).

    DURUM ALANI YOKTUR (spec §6 S2): "Tamamlandı" rozeti `milestone_date` ile
    bugunun TUREVIDIR ve istemcide hesaplanir. `sort_order` yanitta BASILIR
    cunku Gantt satirlarinin sirasi istemcide yeniden kurulabilmelidir.
    """

    id: uuid.UUID
    title: str
    milestone_date: date
    sort_order: int


class SectionTypeRead(BaseModel):
    """BLF-B1 — bolum tipi `{id, name}`. `sort_order` yanitta YOKTUR (liste sirasi
    sunucudan gelir). Hem `GET /section-types` hem bolum yanitindaki `section_type`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class SectionTypeCreate(BaseModel):
    """`POST /section-types` govdesi. Ad kirpilir; bos/yalniz-bosluk 422."""

    name: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=SECTION_TYPE_NAME_MAX_LEN
        ),
    ]


class SectionResponse(BaseModel):
    """Spec §4.1. "gecikme riski" alani KASITLI olarak yok (spec §3.3): hesabin
    girdisi henuz uretilmedigi icin yer tutucu bile dondurulmez."""

    id: uuid.UUID
    code: str | None
    # URL-2 — okunabilir URL kimligi. Frontend URL-3'te `/projeler/<slug>`u
    # buradan basacak. `None` olabilir (adi slug'lanamayan ya da servis
    # disinda acilmis kayit) — o durumda URL UUID olarak kalir.
    slug: str | None = None
    name: str
    status: SectionStatus
    # §6.2 TEK ekleme. `manager_name` anlik goruntusu KALIR: kullanici silinse
    # (FK `SET NULL`) bile liste ekrani ve denetim kirilmaz.
    manager_user_id: uuid.UUID | None = None
    manager_name: str | None
    start_date: date | None
    end_date: date | None
    sort_order: int
    progress_pct: Annotated[MetricPlaceholder, Gorunurluk.operasyonel]
    boq_item_count: CountPlaceholder
    budget: Annotated[MetricPlaceholder, Gorunurluk.para]
    worker_count: CountPlaceholder
    # --- BLM-SAY: LISTE ucuna TASINDI (kullanicinin canlida bildirdigi kusur) ---
    #
    # 🔴 Bu iki kolon KAYITLIYDI ama YALNIZ detay ucunda donuyordu; bolum karti
    # "Bolum Bedeli"ni basamiyor ("—") ve planlanan isciyi bilmedigi icin `0`
    # yaziyordu. Alanlar `SectionDetailResponse`ten BURAYA cekildi (kopyalanmadi):
    # tip iki ucta AYRISAMAZ, cunku artik TEK tanim vardir — `SectionCreate`/
    # `SectionUpdate` icin zaten uygulanan kuralin ayni.
    #
    # BLF-B1: `budget_amount` YANITTAN KALKTI — bedel TEK kaynak, yukaridaki turev `budget`.
    planned_worker_count: int | None
    # --- P11 (spec §3): YALNIZ EKLEME. Bolum basan UC yuzey de (detay, liste,
    # santiye detayi) tek donusturucuden (`service.to_section`) gectigi icin bu
    # iki alan hepsinde ayni anda dogar. Varsayilan YOKTUR: alani doldurmayi
    # unutan bir donusturucu sessizce degil, `ValidationError` ile patlamalidir.
    depends_on_section_id: uuid.UUID | None
    milestones: list[SectionMilestoneResponse]


class SectionDetailResponse(SectionResponse):
    """P6 §5 — `GET /sections/{section_id}` govdesi: `sections`in TUM kolonlari.

    `SectionResponse`ten TUREYIR, YERINE GECMEZ: liste ucu (`SectionListResponse`)
    dar govdeyi tasimaya devam eder. 🔴 BLM-SAY'de dort yer tutucudan IKISI
    BAGLANDI (`boq_item_count` · `budget`) ve ikisi de MIRAS yoluyla ayni anda
    liste + detay + santiye detayinda dogdu; `progress_pct` yer tutucu KALDI
    (hakediş turevi, ayri is), `worker_count` T4'te zaten bagliydi.

    BLF-B1 (kullanici karari F-a): bolum bedeli YALNIZ turev `budget`tir;
    `budget_amount` yanittan KALKTI (kolon ayri dilimde DROP edilir).

    Mockup'ta gorunup burada OLMAYAN her sey `Section` modelinde de yoktur
    (BOQ atamalari, taseron/makine, bagimlilik/milestone, belgeler): spec §6
    listesi — bu dilimde ARA COZUM yazilmaz.
    """

    site_id: uuid.UUID
    section_type: SectionTypeRead | None
    description: str | None
    # `manager_user_id`/`manager_name` ikilisinin birebiri: ad FK'nin ANLIK
    # GORUNTUSUDUR, kullanici silinse (FK `SET NULL`) bile evrakta KALIR.
    deputy_manager_user_id: uuid.UUID | None
    deputy_manager_name: str | None
    # `planned_worker_count` BURADA YENIDEN TANIMLANMAZ —
    # `SectionResponse`ten miras alinir (BLM-SAY). Iki yerde tanimlanan bir alan,
    # iki ucun tipini ayristirabilecek tek yerdi.
    is_draft: bool
    created_at: datetime
    updated_at: datetime


class SectionStatusCounts(BaseModel):
    """Mockup'taki "3 aktif · 2 bekliyor" kirilimi (spec §3.2)."""

    planned: int
    active: int
    completed: int


class SectionListResponse(BaseModel):
    counts: SectionStatusCounts
    items: list[SectionResponse]


# --- Santiye ---


class SiteCard(BaseModel):
    """Spec §4.1 SiteCard. `remaining_days` negatif olabilir (spec §4.2) —
    gecikmeyi kirmizi gostermek frontend'in isi, backend gercegi bastirmaz."""

    id: uuid.UUID
    code: str
    # URL-2 — okunabilir URL kimligi. Frontend URL-3'te `/projeler/<slug>`u
    # buradan basacak. `None` olabilir (adi slug'lanamayan ya da servis
    # disinda acilmis kayit) — o durumda URL UUID olarak kalir.
    slug: str | None = None
    name: str
    status: SiteStatus
    address: str | None
    city: str | None
    city_inherited: bool
    site_manager_name: str | None
    start_date: date | None
    end_date: date | None
    delivery_date: date | None
    remaining_days: int | None
    section_count: int
    worker_count: CountPlaceholder
    progress_pct: Annotated[MetricPlaceholder, Gorunurluk.operasyonel]

    # --- Santiye formu genislemesi (§6.2). YALNIZ EKLEME yapildi: yukaridaki
    # P2 alanlarinin hicbiri kaldirilmadi/yeniden adlandirilmadi, aksi hâlde
    # P2 frontend'i kirilirdi. Hicbirinin varsayilani YOKTUR: eksik birakan bir
    # donusturucu sessizce degil, ValidationError ile patlamalidir.
    is_draft: bool
    site_manager_user_id: uuid.UUID | None
    safety_officer_user_id: uuid.UUID | None
    safety_officer_name: str | None
    safety_officer_is_outsourced: bool
    neighborhood: str | None
    parcel: str | None
    gps_coordinates: str | None
    land_area_m2: Annotated[Decimal | None, Gorunurluk.operasyonel]
    construction_area_m2: Annotated[Decimal | None, Gorunurluk.operasyonel]
    floor_info: str | None
    budget: Annotated[Decimal | None, Gorunurluk.para]
    facilities: SiteFacilities
    electricity_subscription_no: str | None
    water_subscription_no: str | None
    planned_worker_count: int | None


class SiteProjectSummary(BaseModel):
    """Santiye Detay ust satiri: proje adi + isveren (spec §3.2)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # URL-2: santiye detayindan PROJE URL'i kurulabilsin diye (kirinti/breadcrumb).
    # Bu olmadan URL-3 santiye sayfasinda proje slug'ini AYRI bir istekle cekmek
    # zorunda kalirdi. `from_attributes` zaten `Project.slug`i okur.
    slug: str | None = None
    name: str
    city: str | None
    employer_name: str | None


class SiteDetailResponse(SiteCard):
    project: SiteProjectSummary
    section_status_counts: SectionStatusCounts
    sections: list[SectionResponse]
    total_progress_payment: Annotated[MetricPlaceholder, Gorunurluk.para]
    contract_amount: Annotated[MetricPlaceholder, Gorunurluk.para]


class SiteCounts(BaseModel):
    all: int
    active: int
    on_hold: int
    completed: int
    # §5.2: taslaklar durum sayaclarindan DUSULMEZ (durumlari ne ise o sayilir);
    # yalnizca bu sayac ayrica artar. `ProjectCounts.draft` deseni.
    draft: int


class SiteListTotals(BaseModel):
    """Alt KPI seridi (spec §4.1).

    🔴 "TAMAMI yer tutucu" ARTIK DOGRU DEGIL: `active_worker_count` T4'te
    baglandi. Kalan uc alanin P-YT2 denetimi ve gerekceleri zarfin KURULDUGU
    yerdedir — `sites/service/presenters.py:_totals`.
    """

    total_progress_payment: Annotated[MetricPlaceholder, Gorunurluk.para]
    subcontractor_count: CountPlaceholder
    active_worker_count: CountPlaceholder
    average_margin: Annotated[MetricPlaceholder, Gorunurluk.para]


class SiteListResponse(BaseModel):
    counts: SiteCounts
    items: list[SiteCard]
    totals: SiteListTotals


# --- Duz `GET /sites` (SITE-1a) ---
#
# 🔴 KARAR K3: bu semalar `SiteCard`tan TUREMEZ ve `SiteCard`a alan EKLENMEZ.
# Gerekce (WORKFLOW §4 ortak kanonu): "'additive' sanilan alan devri
# typecheck'i kirar" — `openapi-typescript`, sunucunun her zaman doldurdugu
# alani `required` listesinde olmasa bile ZORUNLU uretir; `SiteCard`a
# `project_id` eklemek `/projects/{id}/sites`i tuketen her frontend fikstürünü
# kirardi (MT-1'de bu fiilen 10 typecheck hatasi verdi). Ayri yalin sema =
# sifir blast radius.


class SiteOptionResponse(BaseModel):
    """Proje secmeden santiye secmek icin YALIN satir (mockup "santiye sec"
    dropdown'lari). Secenek metni *santiye · proje* basildigi icin proje
    kimligi ve adi buradadir — `SiteCard`ta ikisi de YOKTUR."""

    id: uuid.UUID
    code: str
    name: str
    project_id: uuid.UUID
    project_name: str


class SiteOptionListResponse(BaseModel):
    """K7 sayfalama zarfi (`PersonnelListResponse` ile birebir).

    ⚠️ `SiteListResponse.totals` ile KARISTIRILMAZ: o bir KPI seridi, bu
    sayfalama. `total` SUZGECTEN GECMIS kumeyi sayar (SQL COUNT), sayfayi degil.
    """

    items: list[SiteOptionResponse]
    total: int
    limit: int
    offset: int


# --- Giris semalari ---


class SiteSectionInput(BaseModel):
    """Form ici bolum satiri (mockup 119-124).

    P2 `SectionCreate`'in YERINE GECMEZ; onun yaninda durur ve ayni `Section`
    modelini yazar. Iki alan bilincli olarak YOKTUR:

    * `estimated_amount` — "Tahmini Bedel" yer tutucudur, saklanmaz (§3.4).
      Govdede gelirse Pydantic onu sessizce yok sayar.
    * `sort_order` — sira govdeden gelmez, dizideki sirasindan atanir (0,1,2...).
    """

    name: str = Field(min_length=1, max_length=150)
    code: str | None = Field(default=None, min_length=1, max_length=50)
    manager_user_id: uuid.UUID | None = None
    start_date: date | None = None
    end_date: date | None = None


class SiteCreate(BaseModel):
    """Mockup "Şantiye Ekle" formunun tam govdesi (spec §6.1).

    `contract_amount` YOK (spec §2.1) — santiye payi BOQ dagitiminin turevidir.
    `duration_days` YOK (§3.6) — sure turevdir. `latitude`/`longitude` YOK (§3.5).
    """

    # --- kimlik (mockup 63-73) ---
    name: str = Field(min_length=1, max_length=150)
    # Bossa SNT-{YYYY}-{NNN} uretilir (§3.2); kullanici PATCH ile duzeltebilir.
    code: str | None = Field(default=None, min_length=1, max_length=50)
    status: SiteStatus = SiteStatus.active
    site_manager_user_id: uuid.UUID | None = None
    safety_officer_user_id: uuid.UUID | None = None
    safety_officer_is_outsourced: bool = False
    # --- konum & alan (mockup 76-88) ---
    city: str | None = Field(default=None, max_length=100)
    neighborhood: str | None = Field(default=None, max_length=150)
    parcel: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=300)
    # BICIM DOGRULAMASI YOK (§3.5): yalniz uzunluk sinirlanir.
    gps_coordinates: str | None = Field(default=None, max_length=50)
    land_area_m2: Decimal | None = Field(default=None, ge=0)
    construction_area_m2: Decimal | None = Field(default=None, ge=0)
    # Serbest metin ("2 bodrum + 10 normal") — sayi DEGIL (mockup 86).
    floor_info: str | None = Field(default=None, max_length=100)
    # --- takvim & butce (mockup 91-99) ---
    start_date: date | None = None
    end_date: date | None = None
    budget: Decimal | None = Field(default=None, ge=0)
    # --- tesisler (mockup 147-174) ---
    facilities: SiteFacilitiesInput = Field(default_factory=SiteFacilitiesInput)
    electricity_subscription_no: str | None = Field(default=None, max_length=50)
    water_subscription_no: str | None = Field(default=None, max_length=50)
    planned_worker_count: int | None = Field(default=None, ge=0)
    # --- bolumler + taslak (mockup 102-144, 226) ---
    sections: list[SiteSectionInput] = Field(default_factory=list)
    is_draft: bool = False
    # P2/P1.1a mirasi — mockup'ta yok ama sozlesmede KALIR: `site_manager_user_id`
    # doluysa servis `users.full_name`i bunun uzerine yazar.
    site_manager_name: str | None = Field(default=None, max_length=200)
    delivery_date: date | None = None


class SiteUpdate(BaseModel):
    """`project_id` YOK — santiye baska projeye tasinamaz.
    `sections` YOK — bolumler mevcut P2 uclariyla yonetilir (§7.3).

    Tum alanlar istege bagli; "gonderilmedi" ile "null yapildi" ayrimi
    `model_fields_set`/`exclude_unset` ile korunur.
    """

    name: str | None = Field(default=None, min_length=1, max_length=150)
    code: str | None = Field(default=None, min_length=1, max_length=50)
    status: SiteStatus | None = None
    site_manager_user_id: uuid.UUID | None = None
    safety_officer_user_id: uuid.UUID | None = None
    safety_officer_is_outsourced: bool | None = None
    city: str | None = Field(default=None, max_length=100)
    neighborhood: str | None = Field(default=None, max_length=150)
    parcel: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=300)
    gps_coordinates: str | None = Field(default=None, max_length=50)
    land_area_m2: Decimal | None = Field(default=None, ge=0)
    construction_area_m2: Decimal | None = Field(default=None, ge=0)
    floor_info: str | None = Field(default=None, max_length=100)
    start_date: date | None = None
    end_date: date | None = None
    budget: Decimal | None = Field(default=None, ge=0)
    facilities: SiteFacilitiesInput | None = None
    electricity_subscription_no: str | None = Field(default=None, max_length=50)
    water_subscription_no: str | None = Field(default=None, max_length=50)
    planned_worker_count: int | None = Field(default=None, ge=0)
    is_draft: bool | None = None
    site_manager_name: str | None = Field(default=None, max_length=200)
    delivery_date: date | None = None


class SectionMilestoneInput(BaseModel):
    """P11 — kilometre tasi giris satiri. `id` OPSIYONELDIR ve satirin KIMLIGINI
    korur (P9 `ShareholderInput` emsali, spec §3):

    * id verilirse mevcut satir YERINDE guncellenir (birincil anahtar yasar);
    * verilmezse YENI satirdir;
    * bilinmeyen ya da BASKA bolume ait id sessizce yeni satira DONMEZ, 422 verir
      (bkz. `service._merge_milestones`).

    `sort_order` GOVDEDEN GELMEZ: sira dizideki sirasindan atanir (0,1,2...) —
    `SiteSectionInput` deseninin birebiri. Iki kaynak (dizi sirasi + acik alan)
    birbiriyle celisebilirdi ve mockup'ta sira girisi CIZILMEMISTIR.
    """

    id: uuid.UUID | None = None
    title: str = Field(min_length=1, max_length=200)
    milestone_date: date


class SectionCreate(BaseModel):
    """`Form - Bolum Ekle`in tam govdesi (P6 §5, T3).

    Yer tutucu `budget` alani BURADA DA YOKTUR (spec §2.2): o BOQ turevidir ve
    girdi olarak alinmaz. BLF-B1: eski elle `budget_amount` alani da KALKTI; eski
    istemci onu gonderirse Pydantic varsayilani (`extra="ignore"`) sessizce yok sayar.

    Mockup'ta gorunup burada OLMAYANLAR — spec §6, bu dilimde ARA COZUM
    YAZILMAZ: BOQ atamalari (Form 131-211), taseron/makine (88-98),
    bagimlilik/milestone/Gantt (115-123, 237), belgeler (214-233).
    "Süre (Gün)" (Form 109) `readonly` bir TUREVDIR, saklanmaz.
    """

    # Bossa `BLM-NN` uretilir (Form 68 ipucu: "Boş bırakılırsa otomatik").
    code: str | None = Field(default=None, min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=150)
    # Varsayilan `planned` (spec §2.3): yeni bolum kural olarak planlanmis dogar.
    status: SectionStatus = SectionStatus.planned
    # §7 tablosu 5. satir: FK verilirse servis `manager_name`i `users.full_name`
    # ile DOLDURUR — iki alan ayri ayri gonderilmez, ad FK'nin turevidir.
    manager_user_id: uuid.UUID | None = None
    manager_name: str | None = Field(default=None, max_length=200)
    start_date: date | None = None
    end_date: date | None = None
    # Form 69 "Bölüm Sırası" mockup'ta `*` tasir ama VARSAYILANI oldugu icin
    # govdeden hic gelmese bile ASLA bos kalmaz — bu yuzden `guards`ta ayrica
    # zorunlu kilinmaz (`P3.1` sira deseni korunur, davranis DEGISMEDI).
    sort_order: int = Field(default=0, ge=0)

    # --- P6 · T3 genislemesi (spec §3/§5). Tipler `SectionUpdate` ile BIREBIR
    # AYNIDIR (`ge=0` dahil): iki govde arasinda ayrisan bir kural, ayni alani
    # POST'ta kabul edip PATCH'te reddeden sessiz bir tutarsizlik olurdu.
    # BLF-B1: enum yerine sirket geneli tip kimligi; var olmayan id servis katmaninda 422.
    section_type_id: uuid.UUID | None = None
    description: str | None = None
    # `manager_user_id` ile AYNI kural: FK verilirse servis `deputy_manager_name`i
    # `users.full_name` ile EZER; ad FK'nin turevidir.
    deputy_manager_user_id: uuid.UUID | None = None
    deputy_manager_name: str | None = Field(default=None, max_length=200)
    planned_worker_count: int | None = Field(default=None, ge=0)
    # Form 242 "Taslak Kaydet" / 243 "Bölümü Oluştur". Varsayilan YAYINDIR:
    # zorunluluklar yalniz burada `False` iken kosar (kalici karar 4).
    is_draft: bool = False

    # --- P11 genislemesi (spec §3). Form 115-117 TEK oncul select'i + 120-125
    # milestone girisi. Coklu bagimlilik ve ayri milestone CRUD ucu ACILMAZ.
    # Bag YALNIZ BILGIDIR: tarih kisiti ZORLANMAZ (S3).
    depends_on_section_id: uuid.UUID | None = None
    milestones: list[SectionMilestoneInput] = Field(default_factory=list)


class SectionUpdate(BaseModel):
    """`site_id` YOK — bolum baska santiyeye tasinamaz."""

    code: str | None = Field(default=None, min_length=1, max_length=50)
    name: str | None = Field(default=None, min_length=1, max_length=150)
    status: SectionStatus | None = None
    # §7 tablosu 6. satir. FK degisince ad anlik goruntusu de guncellenir; yoksa
    # eski ad yerinde kalir ve kaydin kendisi hakkinda YALAN SOYLER.
    manager_user_id: uuid.UUID | None = None
    manager_name: str | None = Field(default=None, max_length=200)
    start_date: date | None = None
    end_date: date | None = None
    sort_order: int | None = Field(default=None, ge=0)

    # --- P6 · T2 genislemesi (spec §3/§5). HEPSI OPSIYONEL: "gonderilmedi" ile
    # "null yapildi" ayrimi `exclude_unset` ile korunur.
    #
    # `planned_worker_count` icin `ge=0` BURADA durur: kural
    # Pydantic'te alan bazli 422 uretir; DB'deki `ck_sections_*` CHECK'leri
    # emniyet agi olarak KALIR ama kullaniciya anlasilir mesaj vermez.
    # BLF-B1: enum yerine sirket geneli tip kimligi; var olmayan id servis katmaninda 422.
    section_type_id: uuid.UUID | None = None
    description: str | None = None
    # `manager_user_id` (yukarida) ile AYNI kural: FK verilirse servis
    # `deputy_manager_name`i `users.full_name` ile EZER; ad FK'nin turevidir.
    deputy_manager_user_id: uuid.UUID | None = None
    deputy_manager_name: str | None = Field(default=None, max_length=200)
    planned_worker_count: int | None = Field(default=None, ge=0)
    is_draft: bool | None = None

    # --- P11 genislemesi (spec §3) ---
    # `depends_on_section_id`: "gonderilmedi" ile "acikca null'landi" ayrimi
    # `exclude_unset` ile korunur — null gondermek BAGI KOPARIR.
    depends_on_section_id: uuid.UUID | None = None
    # `milestones`: liste gonderilirse TUM liste birlestirilir (gonderilmeyen
    # mevcut satir DUSER); alan hic gonderilmezse satirlara DOKUNULMAZ. Bos liste
    # gondermek ile alani gondermemek AYNI SEY DEGILDIR — ilki hepsini siler.
    # Acik `null` "dokunma" olarak okunur: liste alaninin null'u bir SILME emri
    # sayilsaydi, kismi govde gonderen eski istemciler satirlari sessizce
    # supururdu; silmek isteyen taraf `[]` gonderir ve niyetini acikca yazar.
    milestones: list[SectionMilestoneInput] | None = None

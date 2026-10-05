import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.field_mask import Hassas
from app.modules.projects.models import PriceIndexType, ProjectStatus, ProjectType

# sites.models, projects.models'i import eder; projects.models hicbir sey geri
# import etmez — bu yon TEK YONLUDUR, cember YOKTUR (P10 cost_cards dersi).
from app.modules.sites.models import SectionStatus

# VKN/TCKN: 10 veya 11 haneli rakam (spec §3.2). Mesaj Turkce ve alana ozel.
_TAX_NUMBER_PATTERN = re.compile(r"^\d{10,11}$")
_TAX_NUMBER_MESSAGE = "VKN 10 veya 11 haneli rakam olmalıdır."


# --- İşveren (employers) şemaları (spec §3.1, §3.2) ---


class EmployerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tax_number: Annotated[str | None, Hassas.yok] = Field(default=None, max_length=11)
    contact_person: str | None = Field(default=None, max_length=200)

    @field_validator("tax_number")
    @classmethod
    def _validate_tax_number(cls, value: str | None) -> str | None:
        if value is not None and not _TAX_NUMBER_PATTERN.fullmatch(value):
            raise ValueError(_TAX_NUMBER_MESSAGE)
        return value


class EmployerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    tax_number: Annotated[str | None, Hassas.yok]
    contact_person: str | None
    is_active: bool


class EmployerListResponse(BaseModel):
    items: list[EmployerResponse]


# --- Sözleşme okuma yanıtı (spec §3.3, §5.4) ---


class ProjectContractResponse(BaseModel):
    """Detay/liste yanıtında sözleşme (spec §2.4). Yalnız okuma; giriş `ProjectContractInput`."""

    model_config = ConfigDict(from_attributes=True)

    contract_no: str | None
    signature_date: date | None
    amount: Annotated[Decimal | None, Hassas.sozlesme_fiyat]
    advance_pct: Annotated[Decimal, Hassas.yok]
    retainage_pct: Annotated[Decimal, Hassas.yok]
    vat_pct: Annotated[Decimal, Hassas.yok]
    late_penalty_daily: Annotated[Decimal | None, Hassas.sozlesme_fiyat]
    has_price_escalation: bool
    index_type: PriceIndexType | None
    base_index_value: Annotated[Decimal | None, Hassas.yok]


class ProjectBudgetLines(BaseModel):
    """Dört bütçe kalemi okuma yanıtı (spec §3.3). Toplam `budget` ayrı alanda durur."""

    material: Annotated[Decimal | None, Hassas.maliyet_kar]
    labor: Annotated[Decimal | None, Hassas.maliyet_kar]
    subcontractor: Annotated[Decimal | None, Hassas.maliyet_kar]
    overhead: Annotated[Decimal | None, Hassas.maliyet_kar]


# --- B6 yer tutucu deseni (dashboard spec §2.3; bu ekran icin spec §5.3) ---


class MetricPlaceholder(BaseModel):
    """Tek degerli alanin zarfi: ya GERCEK deger ya durust bos durum tasir.

    P10 T3: zarf sozlesmesi artik pydantic duzeyinde BAGLIDIR ve iki yonlu
    calisir (ROADMAP §3 "celiskili sozlesme" borcu):

    * `available=True` ⇒ `pending_module is None` — dolu bir alanin "hangi modul
      gelince dolacak" bilgisi TASIMASI anlamsizdir; eski hâlinde `pending_module`
      zorunlu oldugu icin dolu zarf bile bir modul adi tasimak zorundaydi ve
      ekran o alani "hâlâ eksik" sanabiliyordu.
    * `available=False` + `pending_module` DOLU — alan henuz BAGLANMADI; bos zarf
      kaynagini bildirir, aksi hâlde ekran "—" basip nedenini soyleyemez.
    * `available=False` + `pending_module is None` — 🔴 **ILR-1/2 UCUNCU HAL:
      ROLUN IZNI YOK** (kullanici karari 2026-08-27). `pending_module` IZIN
      anlamiyla YUKLENMEZ: *"bu modul daha yazilmadi"* ile *"bunu gormeye yetkin
      yok"* farkli iki durumdur ve ilkini ikincisi icin kullanmak ekrani YALANCI
      yapar. Bu hâl YALNIZ `restricted()` fabrikasindan kurulur.

    Alan TIPI DEGISMEDI (`MetricPlaceholder` kalir): bu zarflari tuketen UI
    CANLIDA (E4 proje kartlari) ve kirici bir sema degisikligi yapilmaz.

    `CountPlaceholder`a bu kural UYGULANMAZ — orada dolu zarfin `pending_module`
    tasimasi BILINCLI bir emsaldir (bkz. o sinifin notu).
    """

    available: bool = False
    value: Decimal | None = None
    pending_module: str | None = None

    @model_validator(mode="after")
    def _validate_envelope(self) -> Self:
        if self.available and self.pending_module is not None:
            raise ValueError("Dolu zarf pending_module taşımaz (available=True ⇒ None).")
        return self

    def kisitli(self) -> "MetricPlaceholder":
        """🔴 `core.field_scope`in ZARF PROTOKOLÜ — kapsam maskesi bunu çağırır.

        Maske gizlediği alana `None` yazsaydı bu zarfın bulunduğu alanlarda
        ŞEMA KIRILIRDI (alan zorunlu bir nesnedir) ve ekran *"veri yok"* ile
        *"yetkin yok"*u ayırt edemezdi. Üçüncü hâl ZATEN bunun için var
        (kullanıcı kararı 2026-08-27); maske onu yeniden icat etmez, buradan
        ALIR — böylece anlam zarfın yanında TEK KOPYA kalır ve `core` bir ürün
        modülünü ithal etmek zorunda kalmaz.
        """
        return restricted()


def metric(value: Decimal | None, pending_module: str) -> MetricPlaceholder:
    """Zarfi TEK noktadan kurar: deger varsa dolu, yoksa bos (P10 T3).

    Cagiranlar `available`/`pending_module` uclusunu elle KURMAZ — ucluyu her
    modulde yeniden yazmak, zarf sozlesmesini her modulde yeniden yorumlamak
    demekti. "Kaynak yok" kararinin kendisi cagiranda kalir (ornegin girilmemis
    butce `None` gecer), zarfin bicimi burada.
    """
    if value is None:
        return MetricPlaceholder(pending_module=pending_module)
    return MetricPlaceholder(available=True, value=value)


def restricted() -> MetricPlaceholder:
    """ILR-1/2 — ÜÇÜNCÜ hâl: **rolün izni yok.**

    🔴 Bu, "alan henüz bağlanmadı"dan AYRI bir durumdur ve `pending_module`
    TAŞIMAZ. Gerekçe (kullanıcı kararı 2026-08-27): *"bu modül daha yazılmadı"*
    ile *"bunu görmeye yetkin yok"* kullanıcı için tamamen farklı iki durumdur;
    ilkini ikincisi için kullanmak ekranı YALANCI yapar. Ekran `available`
    bayrağına dallanır (ölçüldü: `frontend/src/components/project-detail/
    SiteCard.tsx:209,222`) ve "—" basar, sahte bir gerekçe söylemez.

    ⚠️ `MetricPlaceholder()` çıplak çağrısı artık ValidationError ATMAZ (kural
    gevşetildi, çünkü pydantic bu hâli varsayılanlardan ayırt edemez). Disiplini
    AST bekçisi korur: üretim kodunda `pending_module`suz zarf YALNIZ bu
    fabrika üzerinden kurulur.
    """
    return MetricPlaceholder(available=False, value=None, pending_module=None)


class CountPlaceholder(BaseModel):
    """Sayac alaninin zarfi ("48 isci", "3 hissedar" gibi).

    ⚠️ Eski baslik *"veri kaynagi henuz YAZILMAMIS sayac alani"* diyordu;
    2026-08-22 denetimi bunu OLCEREK curuttu ve P-YT4 (2026-08-23) burada da
    duzeltti: `pending_module` "modul yok" DEMEZ, "veri hangi modulun
    MULKIYETINDE" der (`projects/cards.py::_metric` kanonu). Bos kalmanin
    gercek sebebi her CAGRI YERINDE yazilidir.

    `MetricPlaceholder`in P10 T3'te kazandigi "dolu zarf `pending_module`
    TASIMAZ" kurali BURAYA UYGULANMAZ: puantaj sayaci (`_worker_count`)
    `available=True` + `pending_module="timesheet"` doner ve bu BILINCLI bir
    emsaldir — ayni serit uzerindeki diger sayaclar hâlâ yer tutucudur, ekran
    seridin kaynagini oradan okur. Kirmak, canli taahhut kartini bozardi.
    """

    available: bool = False
    count: int | None = None
    pending_module: str


# --- Tip kartlari (spec §5.3) ---


class ContractingCard(BaseModel):
    """Taahhut karti — sozlesme bedeli/isveren ustte gercek, gerisi bos durum."""

    spent: Annotated[MetricPlaceholder, Hassas.maliyet_kar]
    physical_progress: Annotated[MetricPlaceholder, Hassas.yok]
    # 🔴 ILR-2'DE EKLENDI — **OPSIYONEL** (`| None`, varsayilan `None`).
    # `required` yapmak frontend `typecheck`ini KIRARDI (K-DEVIR2): yol ve
    # operasyon sayisinin sabit kalmasi "kirici degil"in KANITI DEGILDIR.
    #
    # `physical_progress` ile KASTEN AYRI: biri gunlukten (saha), oteki
    # onaylanmis isveren hakedisinden (onay) turer. Mockup'in tek "Fiziksel
    # İlerleme" karosu ASLINDA mali bir sayi basiyordu (`Harcanan / Sözleşme
    # Bedeli`) — ONAYLI SAPMA, geri alinmaz.
    financial_progress: Annotated[MetricPlaceholder | None, Hassas.sozlesme_fiyat] = None
    final_progress_payment: Annotated[MetricPlaceholder, Hassas.sozlesme_fiyat]
    worker_count: CountPlaceholder
    subcontractor_count: CountPlaceholder


class InvestmentCard(BaseModel):
    """Kendi yatirim karti.

    `total_cost` (E4 122) KULLANICI KARARI 2026-08-09 ile **HARCANAN**dir
    (`costs.total_spent` = arsa + taşeron `approved`+`paid` BRÜT), butce DEGIL;
    `estimated_profit`/`margin` ise BUTCE tabanlidir. Ayrinti: `_investment_card`.
    """

    sales_target: Annotated[Decimal | None, Hassas.satis_alici]
    land_cost: Annotated[Decimal | None, Hassas.maliyet_kar]
    sold_amount: Annotated[MetricPlaceholder, Hassas.satis_alici]
    sales_ratio: Annotated[MetricPlaceholder, Hassas.satis_alici]
    unit_summary: CountPlaceholder
    total_cost: Annotated[MetricPlaceholder, Hassas.maliyet_kar]
    estimated_profit: Annotated[MetricPlaceholder, Hassas.maliyet_kar, Hassas.satis_alici]
    margin: Annotated[MetricPlaceholder, Hassas.maliyet_kar, Hassas.satis_alici]


class ShareholderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    share_pct: Annotated[Decimal, Hassas.yok]


class LandShareCard(BaseModel):
    landowner_name: str
    our_share_pct: Annotated[Decimal, Hassas.yok]
    owner_share_pct: Annotated[Decimal, Hassas.yok]
    land_cost: Annotated[
        Decimal | None, Hassas.maliyet_kar
    ]  # daima 0 — tanim geregi, saklanmaz (spec §3.3)
    contract_no: str | None
    notary_date: date | None
    land_area_m2: Annotated[Decimal | None, Hassas.yok]
    construction_area_m2: Annotated[Decimal | None, Hassas.yok]
    delivery_date: date | None
    daily_penalty: Annotated[Decimal | None, Hassas.sozlesme_fiyat]
    guarantee_amount: Annotated[Decimal | None, Hassas.sozlesme_fiyat]
    shareholder_count: int
    shareholders: list[ShareholderResponse]
    our_unit_count: CountPlaceholder
    owner_unit_count: CountPlaceholder
    our_share_value: Annotated[MetricPlaceholder, Hassas.satis_alici]
    construction_cost: Annotated[MetricPlaceholder, Hassas.maliyet_kar]
    estimated_profit: Annotated[MetricPlaceholder, Hassas.maliyet_kar, Hassas.satis_alici]
    margin: Annotated[MetricPlaceholder, Hassas.maliyet_kar, Hassas.satis_alici]
    construction_progress: Annotated[MetricPlaceholder, Hassas.yok]


# --- Maliyet/kâr yanıtı (P10 spec §3; `GET /projects/{id}/costs`) ---


class ProjectCostBreakdown(BaseModel):
    """KY 113-161 "Maliyet Kırılımı" kartının satırları.

    `land_cost` üç ayrı şey söyler ve bu yüzden `Decimal | None`dır (P10 spec §2,
    `costs.land_cost`): kendi yatırımda girilen bedel, kat karşılığında tanım
    gereği `0`, taahhütte `None` = kavram yok. Zarf KULLANILMAZ çünkü değer
    yer tutucu değildir, kaynağı VARDIR.

    Üç kalem (`permits` KY 134-140 · `financing` 141-147 · `marketing` 148-154)
    ise zarflıdır ve mockup'ta rakam göründüğü için 0 basmak sahte bilgi
    üretmek olurdu (spec §2).

    ⚠️ **P-YT4 (2026-08-23):** eski cümle *"kaynak modül henüz veri YAZMIYOR"*
    idi ve BAYAT. `accounting` da `treasury` de CANLIDIR ve İKİSİ DE veri
    yazar — yazamadıkları şey PROJE KIRILIMIDIR (muhasebenin üç tablosunda da
    `project_id` yoktur; hazinede proje bağı yalnız çek/senet portföyündedir ve
    o bir kredi faizi gideri değildir). Tam ölçüm `cost_summary.py`nin
    `_ACCOUNTING`/`_TREASURY` notundadır — tek kopya orada yaşar.
    """

    land_cost: Annotated[Decimal | None, Hassas.maliyet_kar]
    # KY 127-132 "İnşaat Maliyeti ₺10.240.000 / %68 harcandı · Bütçe: ₺15,1M".
    # `spent` taşeron hakedişlerinden (approved+paid BRÜT, S1/S2), `budget` dört
    # bütçe kaleminden gelir — arsa bütçeye DAHİL DEĞİLDİR, ayrı satırdır.
    construction_spent: Annotated[Decimal | None, Hassas.maliyet_kar]
    construction_budget: Annotated[Decimal | None, Hassas.maliyet_kar]
    permits: Annotated[MetricPlaceholder, Hassas.maliyet_kar]
    financing: Annotated[MetricPlaceholder, Hassas.maliyet_kar]
    marketing: Annotated[MetricPlaceholder, Hassas.maliyet_kar]
    # KY 156-159 "Toplam Harcanan": yalnız KAYNAĞI OLAN kalemlerin toplamı
    # (arsa + inşaat). Yer tutucu üç kalem toplama GİRMEZ — bilinmeyeni 0
    # sayıp toplama katmak, ekranda eksik olduğu belli olmayan bir sayı üretir.
    total_spent: Annotated[Decimal | None, Hassas.maliyet_kar]


class ProjectProfitProjection(BaseModel):
    """KY 168-194 / KK 121-141 kâr projeksiyonu bloğu (`costs.profit_projection`).

    Alanlar proje tipine göre BAŞKA şeyleri ölçer (spec §2): kendi yatırımda
    gelir = ünite liste fiyatları toplamı, kat karşılığında bizim pay değeri,
    taahhütte sözleşme bedeli. Tipi `project_type` alanından okunur.

    Taahhütte `profit` KARTTA BASILMAZ (E4 180-181 yalnız bedel/harcanan
    gösterir) ama iç türev olarak döner — ekran neyi basacağına kendi karar
    verir, backend bilgi saklamaz.

    **`realized_sales` / `remaining_stock_value` (KULLANICI KARARI 2026-08-09):**
    KY 173-180'in iki satırı uca EKLENDİ. Tanımları mevcut tek-kaynaklardan gelir:
    `realized_sales` = satış BEDELLERİ toplamı, ölçüt `sales.summary._SOLD_STATUSES`
    (`active`+`deed_transferred`; iptal edilmiş satış hiç okunmaz) ·
    `remaining_stock_value` = satılmamış ünitelerin LİSTE fiyatları toplamı, ölçüt
    `units.summary` `available_units` (`sales_status is listed`).

    İkisinin toplamı `revenue`a (ünite liste fiyatları toplamı) eşit OLMAK ZORUNDA
    DEĞİLDİR: biri satış bedelinden, diğeri liste fiyatından gelir ve iskontolu
    satış aralarında fark doğurur. Zarf KULLANILMAZ — bu blok `Decimal | None`
    alanlar taşır (`revenue`/`cost`/`profit` deseninin aynısı); taahhütte iki alan
    `None`dır, çünkü ünite/satış kavramı yoktur.
    """

    revenue: Annotated[Decimal | None, Hassas.sozlesme_fiyat, Hassas.satis_alici]
    cost: Annotated[Decimal | None, Hassas.maliyet_kar]
    profit: Annotated[Decimal | None, Hassas.maliyet_kar, Hassas.sozlesme_fiyat, Hassas.satis_alici]
    margin_pct: Annotated[
        Decimal | None, Hassas.maliyet_kar, Hassas.sozlesme_fiyat, Hassas.satis_alici
    ]
    realized_sales: Annotated[Decimal | None, Hassas.satis_alici]
    remaining_stock_value: Annotated[Decimal | None, Hassas.satis_alici]


class SubcontractorCostRow(BaseModel):
    """KY 205-249 taşeron maliyet tablosunun bir satırı — SÖZLEŞME başına.

    Satır birimi mockup'ta iş kapsamıdır: her satırda taşeron adı + kategori
    rozeti + ayrı bir "İş Kalemi" metni vardır ("Kaba İnşaat + Kalıp" · "Elektrik
    Tesisatı (52 ünite)" · "PVC Pencere + Cephe", KY 219/227/235) ve bu SÖZLEŞME
    düzeyi bir kavramdır. Taşeron başına gruplarsak aynı taşeronun iki iş kapsamı
    tek satıra ezilir ve sözleşme kimliği geri getirilemez şekilde kaybolur.

    **"İş Kalemi" sütunu `work_category` ile beslenir (KULLANICI KARARI
    2026-08-09):** bu sütun için YENİ KOLON AÇILMAZ — `subcontractor_contracts.
    work_category` zaten vardır ve satır birimi sözleşme olduğu için kategori
    doğrudan sözleşmeden okunur. Değer NULL olabilir ve bu MEŞRUDUR (taslak
    sözleşmede kategori girilmemiş olabilir): satır yine açılır, sütun ekranda BOŞ
    basılır — uydurma metin ÜRETİLMEZ. Satır ayrıca `contract_id`/`contract_no`
    taşır ki ekran sözleşmeye gidebilsin.

    `subcontractor_id` boş olabilir (sözleşmede kartoteks bağı zorunlu değildir);
    ad anlık görüntüsü `subcontractor_name`de kalır. `work_category` doğrudan
    sözleşmedendir (KY 219'un ad altındaki alt satırı) — satır artık tek bir
    sözleşme olduğu için "kategoriler ayrışırsa None" kuralı GEREKMEZ.

    ## `progress_pct` — FİNANSAL ilerleme, FİZİKSEL DEĞİL

    🔴 Bu alan **paranın oranıdır**: `paid / contract_amount × 100`. İşin
    sahada ne kadarının bittiğini SÖYLEMEZ; yalnız sözleşme bedelinin ne
    kadarının ÖDENDİĞİNİ söyler. Kardeş alan `ContractingCard.
    physical_progress` BAŞKA BİR KAVRAMDIR (fiziksel imalat ilerlemesi) ve
    henüz yer tutucudur — ikisi birbirinin yerine KULLANILAMAZ, ekranda da
    "Fiziksel İlerleme" etiketiyle basılamaz. (Bu repoda bir mockup'ın harcama
    oranını "Fiziksel İlerleme" diye etiketlediği CANLI bir tuzak zaten var;
    bu alan onun ikinci örneği OLMAYACAK.)

    İkiz alan `progress_payments.schemas.ProgressPaymentSummary.progress_pct`tir
    (işveren tarafı, "§8 finansal ilerleme") — ad ve tip bilerek AYNIDIR, formül
    de tek kopyadan çağrılır (`cost_summary._row` notu).

    Payda `0` ise (kalemsiz sözleşme ya da bütün kalemleri `unit_price IS NULL`
    olan sözleşme — ikisi de üretimde MEŞRU) değer `None`'dur: uydurma `%0`
    basmak "veri yok"u "ilerleme yok" diye gösterirdi. Bedeli olup hiç ödeme
    görmemiş sözleşme ise GERÇEK `0.00` döner (KY 236-243 "₺1,8M / ₺0" satırı
    mockup'ta harfiyen `%0` basar).
    """

    contract_id: uuid.UUID
    contract_no: str | None
    subcontractor_id: uuid.UUID | None
    subcontractor_name: str | None
    work_category: str | None
    # Sözleşme bedeli TÜREVDİR: `subcontractor_contracts`ta `amount` kolonu
    # yoktur, bedel `Σ kalem quantity × unit_price`tır (contracts K3 ilkesi).
    contract_amount: Annotated[Decimal | None, Hassas.maliyet_kar]
    paid: Annotated[Decimal | None, Hassas.maliyet_kar]
    pending: Annotated[Decimal | None, Hassas.maliyet_kar]
    # "İlerleme" sütunu (KY 214/222/230 · KK 217/223/229). Payda tanımsızsa None.
    progress_pct: Annotated[Decimal | None, Hassas.maliyet_kar]


class SubcontractorCostSummary(BaseModel):
    """KY 244-248 tfoot "TOPLAM TAŞERON MALİYETİ" üçlüsü.

    Satırların toplamıdır ve AYNI kaynaktan hesaplanır: iki ayrı toplama yolu
    açılsaydı tablo ile alt toplam zamanla ayrışırdı (`_list_stmt` dersi).

    **`progress_pct` BURADA YOKTUR ve bu bilinçlidir:** KY tfoot'unun "İlerleme"
    hücresi HARFİYEN BOŞTUR (`<td></td>`, KY 248), KK tablosunun ise tfoot'u hiç
    yoktur. Toplam bir ilerleme yüzdesi eklemek mockup'ın İSTEMEDİĞİ bir sayıyı
    icat etmek olurdu; üstelik "hangi ortalama" sorusunun tek doğru cevabı da
    yoktur (satırların ortalaması ile `Σödenen / Σbedel` farklı sayılardır).
    Sütun SATIR düzeyinde yaşar.
    """

    contract_amount: Annotated[Decimal | None, Hassas.maliyet_kar]
    paid: Annotated[Decimal | None, Hassas.maliyet_kar]
    pending: Annotated[Decimal | None, Hassas.maliyet_kar]


class ProjectCostsResponse(BaseModel):
    """`GET /projects/{id}/costs` (P10 spec §3) — SALT OKUMA türev yanıtı.

    Hiçbir maliyet saklanmaz, hepsi mevcut veriden türer; bu yüzden uç audit
    de YAZMAZ (okuma ucu).
    """

    project_id: uuid.UUID
    project_type: ProjectType
    breakdown: ProjectCostBreakdown
    profit: ProjectProfitProjection
    subcontractors: list[SubcontractorCostRow]
    subcontractor_total: SubcontractorCostSummary


# --- P11 · Portfoy Gantt (GET /projects/timeline) ---


class TimelineMilestone(BaseModel):
    """Kilometre tasi satiri (P11 spec §3): YALNIZ ad + tarih.

    Durum alani YOKTUR (§6 S2): "Tamamlandi" gorunumu `milestone_date < today`
    TUREVIDIR ve istemci `today` damgasiyla hesaplar.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    milestone_date: date


class TimelineSection(BaseModel):
    """Gantt'in ikinci seviyesi: bolum/faz satiri (PT mockup 165-215).

    ILERLEME YUZDESI YOKTUR (§6 S1, kullanici karari): `progress_pct` alani ne
    `None` ne de pending zarfiyla acilir — bar rengi YALNIZ `status`tan turer
    (PT legend 49-51: completed/active/planned). Kaynagi olmayan bir sayiyi
    bos zarfla dondurmek, ekranda doldurulmayi bekleyen sahte bir sozlesme
    birakir.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: SectionStatus
    start_date: date | None
    end_date: date | None
    sort_order: int
    # Yalniz BILGIDIR (spec §3): Gantt'ta baglanti cizgisi cizmek icin. Tarih
    # kisiti backend'de ZORLANMAZ.
    depends_on_section_id: uuid.UUID | None
    milestones: list[TimelineMilestone]


class TimelineProject(BaseModel):
    """Gantt'in ust seviyesi: proje satiri. Santiye seviyesi YOKTUR (spec §1) —
    bolumler santiyeler uzerinden toplanip dogrudan projenin altina dizilir."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    status: ProjectStatus
    start_date: date | None
    end_date: date | None
    contract_amount: Annotated[Decimal | None, Hassas.sozlesme_fiyat]
    sections: list[TimelineSection]


class ProjectTimelineResponse(BaseModel):
    """HAM veri (spec §6 S4): ay izgarasi, zoom kipi ve bar genisligi ISTEMCI
    isidir; uc hicbir sorgu parametresi almaz.

    `today` SUNUCU damgasidir (`core.timezone`): istemcinin saatine birakilirsa
    TR gecesi 00:00-03:00 arasinda "bugun" cizgisi bir gun kayar.

    PT 300-303 portfoy ozeti (toplam sozlesme/hakedis) buraya KONMAZ — o
    dashboard isidir (spec §4).
    """

    today: date
    items: list[TimelineProject]


# --- Liste/detay yanitlari ---


class ProjectListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    # URL-2 — okunabilir URL kimligi. Frontend URL-3'te `/projeler/<slug>`u
    # buradan basacak. `None` olabilir (adi slug'lanamayan ya da servis
    # disinda acilmis kayit) — o durumda URL UUID olarak kalir.
    slug: str | None = None
    name: str
    project_type: ProjectType
    category: str | None
    city: str | None
    status: ProjectStatus
    start_date: date | None
    end_date: date | None
    contract_no: str | None
    contract_amount: Annotated[Decimal | None, Hassas.sozlesme_fiyat]
    # employer_name anlık görüntüsü KALIR (spec §2.3): join'siz okunur, kırılmasın.
    employer_name: str | None
    # B6: işveren/sözleşme ilişki nesneleri + bütçe kalemleri + taslak bayrağı (ekleme,
    # kırıcı değil). Taslakta employer/contract None olabilir; alanlar sözleşmede kalır.
    employer: EmployerResponse | None
    contract: ProjectContractResponse | None
    budget_lines: ProjectBudgetLines
    is_draft: bool
    budget: Annotated[Decimal | None, Hassas.maliyet_kar]
    progress_pct: Annotated[Decimal | None, Hassas.yok]
    contracting: ContractingCard | None
    investment: InvestmentCard | None
    land_share: LandShareCard | None


class ProjectDetailResponse(ProjectListItem):
    # P2 eklemesi (spec §1): GERCEK deger, yer tutucu degil — sayacin girdisi
    # (sites tablosu) P2'de yazildi. P1 sozlesmesine ekleme, kirici degisiklik degil.
    site_count: int


class ProjectCounts(BaseModel):
    all: int
    taahhut: int
    kendi_yatirim: int
    kat_karsiligi: int
    completed: int
    # B6: taslak sekmesi sayacı (spec §5.4). completed ve diğer sayaçlar aynen kalır.
    draft: int


class ProjectListResponse(BaseModel):
    """🔴 İKİ SAYAÇ FARKLI ŞEYLERİ SAYAR — karıştırma.

    * `counts` — SÜZGEÇTEN DE SAYFALAMADAN DA ETKİLENMEZ; hep tüm görünür
      kümeyi sayar (spec §5.1). Mockup'ın "Tümü / Taahhüt / Kat Karşılığı /
      Tamamlanan / Taslak" sekme rakamları buradan basılır; sekme rakamının
      seçili sekmeye göre değişmesi anlamsız olurdu.
    * `total` — SÜZGEÇLENMİŞ kümenin boyutu (`type`/`status` uygulandıktan
      SONRA, sayfalamadan ÖNCE). Sayfa çubuğunun sayfa sayısı buradan çıkar;
      `counts.all` kullanılsaydı süzgeçli listede yanlış sayfa sayısı görünürdü.

    K4 (BOR-TEMIZ T5): `counts` ve `items` AYNEN korundu — kaldırma/yeniden
    adlandırma geriye dönük kırıcı olurdu; zarfa yalnız üç alan EKLENDİ.
    """

    counts: ProjectCounts
    items: list[ProjectListItem]
    total: int
    limit: int
    offset: int


# --- Giris semalari ---


class ProjectInvestmentInput(BaseModel):
    sales_target: Annotated[Decimal | None, Hassas.satis_alici] = Field(default=None, ge=0)
    land_cost: Annotated[Decimal | None, Hassas.maliyet_kar] = Field(default=None, ge=0)


class ShareholderInput(BaseModel):
    """P9 spec §4.1: `id` OPSIYONELDIR ve satirin KIMLIGINI korur.

    id verilirse mevcut satir yerinde guncellenir (`units.shareholder_id` bagi
    ayakta kalir); verilmezse yeni satirdir. id'siz eski govdeler geriye uyumlu
    calisir. Bilinmeyen ya da baska projeye ait id sessizce yeni satira DONMEZ,
    422 verir (bkz. `service._merge_shareholders`).
    """

    id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    share_pct: Annotated[Decimal, Hassas.yok] = Field(gt=0, le=100)


class ProjectLandShareInput(BaseModel):
    landowner_name: str = Field(min_length=1, max_length=200)
    our_share_pct: Annotated[Decimal, Hassas.yok] = Field(gt=0, lt=100)
    owner_share_pct: Annotated[Decimal, Hassas.yok] = Field(gt=0, lt=100)
    contract_no: str | None = Field(default=None, max_length=100)
    notary_date: date | None = None
    land_area_m2: Annotated[Decimal | None, Hassas.yok] = Field(default=None, ge=0)
    construction_area_m2: Annotated[Decimal | None, Hassas.yok] = Field(default=None, ge=0)
    delivery_date: date | None = None
    daily_penalty: Annotated[Decimal | None, Hassas.sozlesme_fiyat] = Field(default=None, ge=0)
    guarantee_amount: Annotated[Decimal | None, Hassas.sozlesme_fiyat] = Field(default=None, ge=0)
    shareholders: list[ShareholderInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def _pct_total_must_be_100(self) -> Self:
        if self.our_share_pct + self.owner_share_pct != 100:
            msg = "Pay yüzdelerinin toplamı 100 olmalıdır"
            raise ValueError(msg)
        return self


class ProjectContractInput(BaseModel):
    """İşveren sözleşmesi girişi (spec §2.4, §3.3). Yalnız `taahhut` projelerinde.

    Yüzdeler alan düzeyinde 0..100 (§3.6 kural 6). `has_price_escalation=true` iken
    endeks alanlarının zorunluluğu (kural 5) ve endeks kapalıyken NULL olması
    (ck_contract_escalation) servis + DB CHECK ile denetlenir.
    """

    contract_no: str | None = Field(default=None, max_length=100)
    signature_date: date | None = None
    amount: Annotated[Decimal | None, Hassas.sozlesme_fiyat] = Field(default=None, ge=0)
    advance_pct: Annotated[Decimal, Hassas.yok] = Field(default=Decimal("20"), ge=0, le=100)
    retainage_pct: Annotated[Decimal, Hassas.yok] = Field(default=Decimal("5"), ge=0, le=100)
    vat_pct: Annotated[Decimal, Hassas.yok] = Field(default=Decimal("20"), ge=0, le=100)
    late_penalty_daily: Annotated[Decimal | None, Hassas.sozlesme_fiyat] = Field(default=None, ge=0)
    has_price_escalation: bool = True
    index_type: PriceIndexType | None = None
    base_index_value: Annotated[Decimal | None, Hassas.yok] = Field(default=None, ge=0)


class ProjectBudgetInput(BaseModel):
    """Dört bütçe kalemi (spec §3.3). Toplam `budget`'i servis hesaplar; istemci `budget` yok."""

    material: Annotated[Decimal, Hassas.maliyet_kar] = Field(default=Decimal("0"), ge=0)
    labor: Annotated[Decimal, Hassas.maliyet_kar] = Field(default=Decimal("0"), ge=0)
    subcontractor: Annotated[Decimal, Hassas.maliyet_kar] = Field(default=Decimal("0"), ge=0)
    overhead: Annotated[Decimal, Hassas.maliyet_kar] = Field(default=Decimal("0"), ge=0)


class ProjectSiteInput(BaseModel):
    """Satır içi şantiye (spec §3.4). Kod verilmezse P2 türeticisi çalışır (B5)."""

    name: str = Field(min_length=1, max_length=150)
    code: str | None = Field(default=None, max_length=50)
    site_manager_name: str | None = Field(default=None, max_length=200)
    construction_area_m2: Annotated[Decimal | None, Hassas.yok] = Field(default=None, ge=0)


class ProjectCreate(BaseModel):
    # code artik OPSIYONEL: bossa sunucu PRJ-{YYYY}-{NNN} uretir (spec §3.5).
    code: str | None = Field(default=None, min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=150)
    project_type: ProjectType
    status: ProjectStatus = ProjectStatus.active
    category: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=100)
    parcel: str | None = Field(default=None, max_length=50)
    address: Annotated[str | None, Hassas.yok] = Field(default=None, max_length=300)
    start_date: date | None = None
    end_date: date | None = None
    # employer_name gövdeden KALDIRILDI (spec §3.3): serbest metin işveren yolu kapandı.
    employer_id: uuid.UUID | None = None
    contract: ProjectContractInput | None = None
    budget_lines: ProjectBudgetInput = Field(default_factory=ProjectBudgetInput)
    sites: list[ProjectSiteInput] = Field(default_factory=list)
    is_draft: bool = False
    investment: ProjectInvestmentInput | None = None
    land_share: ProjectLandShareInput | None = None


class ProjectUpdate(BaseModel):
    """project_type YOK — tip PATCH ile degistirilemez (spec §3.5)."""

    name: str | None = Field(default=None, min_length=1, max_length=150)
    status: ProjectStatus | None = None
    category: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=100)
    start_date: date | None = None
    end_date: date | None = None
    contract_no: str | None = Field(default=None, max_length=100)
    contract_amount: Annotated[Decimal | None, Hassas.sozlesme_fiyat] = Field(default=None, ge=0)
    employer_name: str | None = Field(default=None, max_length=200)
    # İşveren sözleşmesi KURULDUKTAN SONRA da düzeltilebilir olmalı: avans /
    # teminat / KDV oranları yalnız create'ten yazılabildiği sürece yanlış girilen
    # bir `advance_pct` o projedeki HER hakedişte kalıcı yanlış mahsup üretirdi
    # (progress_payments/calculations.py:100) ve `DELETE /projects/{id}` ucu da yok.
    contract: ProjectContractInput | None = None
    investment: ProjectInvestmentInput | None = None
    land_share: ProjectLandShareInput | None = None

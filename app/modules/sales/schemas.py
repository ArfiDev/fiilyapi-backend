"""Ünite satışı şemaları — P8 spec §2/§4, mockup `Form - Daire Satisi` (F) ve
`Satış Yönetimi` (S).

## Gövdede BİLİNÇLİ OLARAK olmayan alanlar

| alan | neden yok |
|---|---|
| `list_price_snapshot` | F84 mockup'ta `readonly` — sunucu ÜNİTEDEN yazar (anlık görüntü) |
| `status` | başlangıç değeri `sale_type`tan türer; geçişler T5'in uçlarıdır |
| `unit_id` (PATCH'te) | ünite değişimi İKİ ünitenin senkronunu ve tekliği birlikte ilgilendirir |
| `sale_type` (PATCH'te) | `status` ile eşleşiktir; rezervasyon→satış geçişi T5 `activate` ucudur |
| maliyet / kâr KOLONU | yok; değer `MetricPlaceholder` ZARFINDA döner (P10 T3'te bağlandı) |
| belgeler / peşinat faturası | F168-207 → `pending_modules`; iki modül de canlı, BAĞ yok |

`min_sale_price` KONTROLÜ HİÇBİR KATMANDA YOKTUR (kalıcı karar 2): ne şemada,
ne serviste, ne DB'de — uyarı bile üretilmez.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.field_mask import Hassas
from app.modules.customers.models import CustomerType
from app.modules.projects.schemas import MetricPlaceholder
from app.modules.sales.models import (
    DeedCondition,
    InstallmentPaymentMethod,
    PaymentPlanType,
    SaleType,
    UnitSaleStatus,
)

# KDV kümesi ({1, 10, 20}) `units.schemas.VatRate`ten GELİR — F87 ile UE 93 AYNI
# kuraldır (karar 9) ve iki kopya validator zamanla ayrışır.
from app.modules.units.schemas import VatRate

# Yer tutucu anahtarları tek yerde.
#
# 🔴 P-YT3 DENETİMİ (2026-08-23): `COST_MODULE` **`PENDING_MODULES`tan
# ÇIKARILDI**. Sebep bir çelişkiydi: liste ekrana *"maliyet/kârın kaynağı yok"*
# derken `UnitSaleResponse.unit_cost` ve `sale_profit` AYNI YANITTA dolu
# geliyordu (P10 T3, `e7b84cb`, 2026-08-09'dan beri). Bu listeye bakıp maliyet
# sütununu gizleyen bir ekran, verisi OLAN bir sütunu gizlerdi.
# Sabit KALIR: `SalesSummaryResponse` onu hâlâ kullanır (aşağıdaki gerekçe).
COST_MODULE = "project_costs"
# Ölçüldü: `documents` tablosunda satışa bağ YOKTUR (`project_id`/`site_id`/
# `folder_id` dışında kolon yok) — "bu satışın belgeleri" listelenemez.
DOCUMENTS_MODULE = "documents"
# Ölçüldü: `Invoice`ta `unit_sale_id` KOLONU YOKTUR; kaynak bağları
# `progress_payments` / `subcontractor_progress_payments` /
# `equipment_rental_invoices` / `purchase_orders`tır. F206-207 peşinat faturası
# bir satışa BAĞLANAMAZ. (Modülün kendisi FAT-1'den beri CANLI — eski yorumun
# "kodu henüz yazılmadı" cümlesi bayattı.)
INVOICING_MODULE = "invoicing"
PENDING_MODULES = [DOCUMENTS_MODULE, INVOICING_MODULE]

_LABEL_SEPARATOR = " · "


def unit_label(block_name: str, unit_no: str) -> str:
    """S159 "A · Daire 12" etiketinin sunucu tarafı: blok adı + ünite numarası.

    Ekran "Daire" sözcüğünü ünite tipinden kendisi ekler; sunucu Türkçe ETİKET
    ÜRETMEZ, yalnız iki veri parçasını birleştirir.
    """
    return f"{block_name}{_LABEL_SEPARATOR}{unit_no}"


class _SaleFormFields(BaseModel):
    """F56-F163'ün ortak gövdesi — `Create` ve `Update` için TEK kopya."""

    discount_amount: Annotated[Decimal | None, Hassas.satis_alici] = Field(
        default=None, ge=0, max_digits=18, decimal_places=2
    )
    vat_pct: Annotated[VatRate, Hassas.yok] = None  # F87 — küme {1, 10, 20} (karar 9)
    advisor_user_id: uuid.UUID | None = None  # F75
    reservation_deposit: Annotated[Decimal | None, Hassas.satis_alici] = Field(
        default=None, ge=0, max_digits=18, decimal_places=2
    )  # S188
    reservation_due_date: date | None = None  # S188 "15 gün süre"
    deed_condition: DeedCondition | None = None  # F156
    planned_deed_date: date | None = None  # F157
    delivery_date: date | None = None  # F158
    has_condominium_easement: bool = False  # F161
    has_mortgage: bool = False  # F162
    # DOLU = gecikme faizi uygulanır, NULL = uygulanmaz (F163). Ayrı bir
    # `has_late_fee` bayrağı AÇILMAZ — iki alan birbiriyle çelişebilirdi.
    late_fee_monthly_pct: Annotated[Decimal | None, Hassas.yok] = Field(
        default=None, ge=0, max_digits=5, decimal_places=2
    )
    payment_plan_type: PaymentPlanType | None = None  # F99
    down_payment: Annotated[Decimal | None, Hassas.satis_alici] = Field(
        default=None, ge=0, max_digits=18, decimal_places=2
    )
    installment_count: int | None = Field(default=None, ge=0)  # F104
    first_installment_date: date | None = None  # F105
    term_interest_pct: Annotated[Decimal | None, Hassas.yok] = Field(
        default=None, ge=0, max_digits=5, decimal_places=2
    )


# Servis, form alanlarını TEK TEK yazmak yerine bu kümeyi kullanır.
SALE_FORM_FIELDS = frozenset(_SaleFormFields.model_fields)


class UnitSaleCreate(_SaleFormFields):
    unit_id: uuid.UUID  # F55
    customer_id: uuid.UUID  # F70-76 (T2 kartoteksinden seçilir)
    sale_type: SaleType  # F56
    sale_price: Annotated[Decimal, Hassas.satis_alici] = Field(
        ge=0, max_digits=18, decimal_places=2
    )  # F86


class UnitSaleUpdate(_SaleFormFields):
    """TÜM alanlar opsiyoneldir; "gönderilmedi" ile "null yapıldı" ayrımı serviste

    `model_dump(exclude_unset=True)` ile çözülür (P1/P2/P4 deseni).

    `has_condominium_easement`/`has_mortgage` burada `None` varsayılanı alır:
    `False` varsayılanı, gönderilmeyen kutucuğu her PATCH'te SESSİZCE temizlerdi.
    """

    sale_price: Annotated[Decimal | None, Hassas.satis_alici] = Field(
        default=None, ge=0, max_digits=18, decimal_places=2
    )
    has_condominium_easement: bool | None = None
    has_mortgage: bool | None = None


class UnitSaleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    unit_id: uuid.UUID
    customer_id: uuid.UUID
    sale_type: SaleType
    status: UnitSaleStatus
    # S150-151 kolonlarının kaynağı (ünite ve alıcı ayrı çağrı gerektirmesin).
    block_name: str
    unit_no: str
    unit_label: str
    customer_name: Annotated[str | None, Hassas.satis_alici]
    customer_type: CustomerType
    customer_national_id: Annotated[str | None, Hassas.satis_alici]
    customer_tax_number: Annotated[str | None, Hassas.satis_alici]
    list_price_snapshot: Annotated[Decimal | None, Hassas.satis_alici]
    discount_amount: Annotated[Decimal | None, Hassas.satis_alici]
    sale_price: Annotated[Decimal | None, Hassas.satis_alici]
    vat_pct: Annotated[Decimal | None, Hassas.yok]
    advisor_user_id: uuid.UUID | None
    advisor_name: str | None
    reservation_deposit: Annotated[Decimal | None, Hassas.satis_alici]
    reservation_due_date: date | None
    deed_condition: DeedCondition | None
    planned_deed_date: date | None
    delivery_date: date | None
    has_condominium_easement: bool
    has_mortgage: bool
    late_fee_monthly_pct: Annotated[Decimal | None, Hassas.yok]
    payment_plan_type: PaymentPlanType | None
    down_payment: Annotated[Decimal | None, Hassas.satis_alici]
    installment_count: int | None
    first_installment_date: date | None
    term_interest_pct: Annotated[Decimal | None, Hassas.yok]
    # --- Tahsilat türevleri (S153-155, S180) — KOLON DEĞİL ---
    paid_amount: Annotated[Decimal | None, Hassas.satis_alici]
    remaining_amount: Annotated[Decimal | None, Hassas.satis_alici]
    installment_total: int
    installment_paid_count: int
    overdue_installment_count: int
    # --- DS 62/90 maliyet + kâr (yer tutucu DEĞİL, BAĞLI) ---
    #
    # 🔴 P-YT3 DENETİMİ (2026-08-23) — ESKİ YORUM YANLIŞTI. *"F62 Maliyet ve F90
    # Bu Satıştan Kâr sütunu YOKTUR"* diyordu; oysa P10 T3 (`e7b84cb`,
    # 2026-08-09) iki alanı da BAĞLADI: `service._cost_metrics` her yanıtta
    # gerçek değeri basar (kanıt: `tests/sales/test_sales_cost_binding.py`).
    #
    # 🔴 `default_factory` KALDIRILDI (K3). Kalan varsayılan, "boş durum"
    # kararının İKİNCİ KOPYASIYDI: gerçek karar `_cost_metrics`te yaşar ve
    # kaynak yoksa (m²'si girilmemiş ünite, bütçesiz proje) zarfı ORASI boş
    # doğurur. Şemadaki varsayılan ise bir çağıran alanı unuttuğunda —
    # `project_costs` canlı ve değer hesaplanabilirken — sessizce
    # "bekleniyor" basardı. Alan zorunlu olunca o dal YAPISAL OLARAK imkânsız.
    #
    # ⚠️ SÖZLEŞME ETKİSİ (K5): iki alan artık OpenAPI'de `required`.
    # `openapi-typescript` onları zorunlu üretir — sunucu zaten HER yanıtta
    # gönderdiği için bu, sözleşmenin gerçeğe UYDURULMASIDIR.
    unit_cost: Annotated[MetricPlaceholder, Hassas.maliyet_kar]
    sale_profit: Annotated[MetricPlaceholder, Hassas.maliyet_kar]
    # F168-202 satış belgeleri + F206-207 peşinat faturası. İki modül de CANLI;
    # eksik olan MODÜL değil satışa giden BAĞdır (gerekçeler `PENDING_MODULES`
    # sabitlerinin yanında ölçüldü). `contracts` şemalarındaki desenin aynısı.
    pending_modules: list[str] = Field(default_factory=lambda: list(PENDING_MODULES))


# --- Ödeme planı (T4; F110-147) ---


class SaleInstallmentInput(BaseModel):
    """`PUT /sales/{id}/installments` gövdesinin TEK satırı.

    `paid_amount`/`paid_at` gövdede YOKTUR: tahsilat yalnız `pay` ucundan işlenir
    (§8 S2). Plan düzenlemesiyle tahsilat aynı gövdeden gelseydi, kullanıcı bir
    satırın tutarını değiştirirken tahsilatını da sessizce sıfırlayabilirdi.
    """

    sequence_no: int = Field(ge=0)  # 0 = peşinat (T1 model notu)
    label: str = Field(min_length=1, max_length=50)  # F118
    due_date: date  # F120
    amount: Annotated[Decimal, Hassas.satis_alici] = Field(
        ge=0, max_digits=18, decimal_places=2
    )  # F121
    payment_method: InstallmentPaymentMethod | None = None  # F122/129


class SaleInstallmentsSave(BaseModel):
    """DEĞİŞTİRME semantiği: `items` planın YENİ HÂLİDİR, gövdede geçmeyen satır SİLİNİR."""

    items: list[SaleInstallmentInput]


class InstallmentPayInput(BaseModel):
    """Tahsilat (§8 S2) — kısmi ödeme desteklenir, bu yüzden bayrak değil TUTAR.

    `paid_at` istemciden ALINMAZ: satır TAM ödendiğinde sunucu saati yazılır
    (geriye dönük tarih girişi bir muhasebe kaydıdır ve hazine dilimine aittir).
    """

    amount: Annotated[Decimal, Hassas.satis_alici] = Field(gt=0, max_digits=18, decimal_places=2)


class SaleInstallmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sale_id: uuid.UUID
    sequence_no: int
    label: str
    due_date: date
    amount: Annotated[Decimal | None, Hassas.satis_alici]
    payment_method: InstallmentPaymentMethod | None
    paid_amount: Annotated[Decimal | None, Hassas.satis_alici]
    paid_at: datetime | None
    # TÜREVLER (kolon DEĞİL): satırın kalanı ve S180'in "gecikmiş" göstergesi.
    # `is_overdue` T5'te EKLENDİ: satış düzeyindeki sayaç
    # (`installment_stats.overdue_count`) HANGİ satırın geciktiğini söylemez ve
    # plan tablosu (F110-147) satır satır boyanır. Sunucu tarafında üretilir ki
    # "bugün" tanımı TEK yerde kalsın — istemci saati ile sunucu saati ayrışırsa
    # aynı taksit iki ekranda farklı renkte görünürdü.
    remaining_amount: Annotated[Decimal | None, Hassas.satis_alici]
    is_overdue: bool = False


class SalePlanResponse(BaseModel):
    """F110-147 tablosu + F143 TOPLAM satırı.

    Eşitlik `total_amount == sale_price` yalnız PLAN YAZAN uçlarda zorlanır
    (`POST generate-plan` ve `PUT installments` → `INSTALLMENT_TOTAL_MISMATCH`,
    spec §2). Ardından gelen bir `PATCH /sales/{id}` bedel değişimi (kapı yalnız
    bedeli TAHSİLATA bağlar, plan toplamına değil — bkz.
    `guards.SALE_PRICE_BELOW_COLLECTED` gerekçesi) planı GEÇİCİ olarak hizasız
    bırakabilir; kullanıcı `generate-plan`/`PUT installments` ile yeniden
    hizalar. `total_amount` satırlardan CANLI toplanır, yanıtta durur ki ekran
    toplamı kendisi toplamak zorunda kalmasın.
    """

    sale_id: uuid.UUID
    sale_price: Annotated[Decimal | None, Hassas.satis_alici]
    total_amount: Annotated[Decimal | None, Hassas.satis_alici]
    paid_amount: Annotated[Decimal | None, Hassas.satis_alici]
    # F106 vade farkının GÖSTERİM tutarı — plana YAZILMAZ (bkz. `plan.py` kararı).
    term_interest_amount: Annotated[Decimal | None, Hassas.satis_alici]
    items: list[SaleInstallmentResponse]


class UnitSaleTotals(BaseModel):
    """S205-215 TOPLAM satırı — satır türevleriyle AYNI kaynaktan toplanır."""

    count: int
    sale_price_total: Annotated[Decimal | None, Hassas.satis_alici]
    paid_total: Annotated[Decimal | None, Hassas.satis_alici]
    remaining_total: Annotated[Decimal | None, Hassas.satis_alici]


class UnitSaleListResponse(BaseModel):
    totals: UnitSaleTotals
    items: list[UnitSaleResponse]


# --- Durum geçişleri (T5; spec §4) ---


class SaleCancelInput(BaseModel):
    """`POST /sales/{id}/cancel` gövdesi — gerekçe ZORUNLUDUR.

    Gerekçe `unit_sales`te bir kolona DEĞİL denetim günlüğüne yazılır
    (`transitions.py` gerekçesi): iptal kaydın bir niteliği değil bir olaydır.
    Boşluk kırpılır ki " " gönderen istemci "gerekçe verdim" sanmasın.
    """

    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


# --- Satış özeti (T5; mockup S55-59, S218-234) ---


class SoldKpi(BaseModel):
    """S55 "Satılan (Tapulu)" · 34 · ₺31,4M.

    Başlık iki şeyi birden söylüyor ("satılan" ve "tapulu"), bu yüzden sayaç
    İKİYE ayrılır: `count` gerçekleşmiş satışların tamamıdır (`active` +
    `deed_transferred`), `deed_transferred_count` ise tapusu devredilmiş
    olanlardır. Tek sayaç dönseydi ekran hangisini gösterdiğini bilemezdi.
    """

    count: int
    deed_transferred_count: int
    amount: Annotated[Decimal | None, Hassas.satis_alici]  # Σ `sale_price`


class ReservedKpi(BaseModel):
    """S56 "Rezerve" · 5 · ₺4,2M potansiyel + §8 S4'ün "süresi doldu" sayacı."""

    count: int
    expired_count: int  # S188 "15 gün süre" — OTOMATİK İPTAL YOK, yalnız gösterge
    amount: Annotated[Decimal | None, Hassas.satis_alici]


class AvailableUnitsKpi(BaseModel):
    """S57 "Boş Ünite" · 13 · ₺12,6M stok.

    Kaynak `unit_sales` DEĞİL `units.sales_status`tur: boş ünitenin satış kaydı
    yoktur, dolayısıyla satış tablosundan sayılamaz. Değer LİSTE FİYATIDIR
    (stok değeri), satış bedeli değil — henüz bir bedel üzerinde anlaşılmamıştır.
    """

    count: int
    list_price_total: Annotated[Decimal | None, Hassas.satis_alici]


class CollectionKpi(BaseModel):
    """S58 "Tahsil Edilen" · ₺24,8M · "%79 tahsilat".

    Payda tfoot TOPLAM'ıdır (S208-210): 24,82 / 31,42 = %79,0 — yani oran
    "tahsil edilen / açık satışların satış bedeli toplamı"dır. Satış yoksa oran
    `None`dır: 0/0'ı %0 diye basmak "hiç satış yok" ile "hiç tahsilat yok"u
    aynı ekrana düşürürdü.
    """

    collected_amount: Annotated[Decimal | None, Hassas.satis_alici]
    contracted_amount: Annotated[Decimal | None, Hassas.satis_alici]
    # 🔴 PARA — kullanıcı kararı 2026-09-19. `tahsil edilen × 100 ÷ sözleşmeye
    #    bağlanan` (summary.py): İKİ girdisi de `para` etiketli, o hâlde türevi de
    #    paradır. `sales` satırında kapsam taşıyan TEK rol `accounting`tir, yani
    #    `operasyonel` etiketi pratikte YALNIZCA zarar üretiyordu: muhasebe
    #    "Tahsil Edilen" kartında ₺ tutarı görüyor ama oranı "—" görüyordu.
    collection_pct: Annotated[Decimal | None, Hassas.satis_alici]


class OverdueKpi(BaseModel):
    """S59 "Vadesi Geçen" · ₺840K · "3 taksit".

    Tutar taksitin TAMAMI değil KALANIDIR: kısmen tahsil edilmiş bir taksitin
    ödenmiş kısmı borç değildir. `late_fee_amount` §8 S5 gereği yalnız GÖSTERİM
    türevidir (tahakkuk YAZILMAZ).
    """

    installment_count: int
    amount: Annotated[Decimal | None, Hassas.satis_alici]
    late_fee_amount: Annotated[Decimal | None, Hassas.satis_alici]


class UpcomingCollection(BaseModel):
    """S220-234'ün tek satırı: "A · Daire 19 — Hasan Demir · Taksit 6 & 7 …"."""

    installment_id: uuid.UUID
    sale_id: uuid.UUID
    unit_label: str  # S159 "A · Daire 12"
    customer_name: Annotated[str | None, Hassas.satis_alici]
    sequence_no: int
    label: str
    due_date: date
    amount: Annotated[Decimal | None, Hassas.satis_alici]
    paid_amount: Annotated[Decimal | None, Hassas.satis_alici]
    remaining_amount: Annotated[Decimal | None, Hassas.satis_alici]
    is_overdue: bool
    days_overdue: int  # S222 "Vadesi 15 gün geçti"
    late_fee_amount: Annotated[
        Decimal | None, Hassas.satis_alici
    ]  # S223 "Gecikme faizi: ₺4.200" — GÖSTERİM türevi


class ExpiredReservation(BaseModel):
    """S188 "Kapora alındı · 15 gün süre" süresi DOLMUŞ hâli (§8 S4).

    Zamanlanmış iş YOKTUR: kayıt `reservation` KALIR, bu liste yalnızca
    kullanıcıyı elle iptale (ya da aktifleştirmeye) yönlendirir.
    """

    sale_id: uuid.UUID
    unit_label: str
    customer_name: Annotated[str | None, Hassas.satis_alici]
    reservation_due_date: date
    days_expired: int
    reservation_deposit: Annotated[Decimal | None, Hassas.satis_alici]


class SalesSummaryResponse(BaseModel):
    """`GET /projects/{id}/sales/summary` — S55-59 + S218-234 TEK yanıtta."""

    project_id: uuid.UUID
    # "Bugün" sunucuda belirlenir ve yanıtta AÇIKÇA döner: "gecikmiş"/"süresi
    # doldu" türevlerinin tamamı bu tarihe göredir ve ekran hangi güne göre
    # hesaplandığını görebilmelidir.
    as_of: date
    sold: SoldKpi
    reserved: ReservedKpi
    available_units: AvailableUnitsKpi
    collection: CollectionKpi
    overdue: OverdueKpi
    upcoming_collections: list[UpcomingCollection]
    expired_reservations: list[ExpiredReservation]
    # KALICI KARAR 3: maliyet/kâr KPI'sı AÇILMAZ.
    #
    # 🔴 P-YT3 (2026-08-23) — (B) GEÇERLİ, etiket KALDI ama SEBEBİ DEĞİŞTİ:
    # eskiden "kaynak yok"tu, bugün kaynak VAR (`costs.allocation` canlı ve
    # satış satırında kullanılıyor). Eksik olan şey KPI ALANIDIR, veri değil —
    # yani etiket artık bir modül boşluğunu değil bir ÜRÜN KARARINI bildiriyor.
    # `UnitSaleResponse`taki kardeşinden farkı budur: orada alan VAR ve dolu,
    # burada alan hiç açılmadı.
    pending_modules: list[str] = Field(default_factory=lambda: [COST_MODULE])

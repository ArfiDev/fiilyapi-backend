import enum
import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DeleteKind(str, enum.Enum):
    """Önizleme/silme motorunun tanıdığı kayıt türleri. Her dilim yeni üye ekler.

    Üye adı = URL parçası. Motorun kayıt defteri (`core/silme`) ile üye kümesi birebir eşit
    olmak ZORUNDADIR; `tests/modules/silme/test_silme_tur_bekcisi.py` çakar.
    """

    site = "site"
    section = "section"
    block = "block"
    unit = "unit"
    # SIL-B2 — mali aile (KARARLAR §1.7 K2: kaydın fişi ve stornosu da birlikte silinir).
    progress_payment = "progress_payment"  # işveren hakedişi (URL: /progress-payments)
    subcontractor_progress_payment = "subcontractor_progress_payment"  # taşeron hakedişi
    invoice = "invoice"  # fatura
    payment = "payment"  # ödeme / tahsilat
    journal_entry = "journal_entry"  # yevmiye fişi (+ stornosu)
    financial_instrument = "financial_instrument"  # çek / senet


#: Önizleme yanıtındaki fiş dökümü tavanı (şantiye/proje ağacında binlerce fiş olabilir).
JOURNAL_ENTRY_PREVIEW_LIMIT = 200


class DeleteRelation(str, enum.Enum):
    """Bağlı kaydın kökle ilişkisi. Bir tablo birden çok yoldan bağlıysa EN GÜÇLÜSÜ yazılır
    (restrict > linked > cascade)."""

    cascade = "cascade"  # DB bağı CASCADE: kayıtla zaten birlikte giderdi
    restrict = (
        "restrict"  # normalde silmeyi ENGELLEYEN bağ; Sistem Yöneticisi için birlikte silinir
    )
    linked = "linked"  # FK OLMAYAN bağ: muhasebe fişi, onay zinciri (kayıtlı kancalar)


class DeletePreviewGroup(BaseModel):
    """Silinecek bağlı kayıtların bir türü."""

    table: str = Field(
        description="Teknik tablo anahtarı (ör. `site_diary_entries`); yalnız anahtar."
    )
    label: str = Field(description="Türkçe tür adı (ör. `Günlük kaydı`).")
    count: int = Field(ge=1)
    relation: DeleteRelation
    is_financial: bool = Field(
        description="Mali kayıt (hakediş, fatura, ödeme, muhasebe fişi): onay penceresi vurgular. "
        "Mali kayıt silmeyi ENGELLEMEZ (SIL-B2); yalnız kullanıcıya açıkça gösterilir."
    )
    samples: list[str] = Field(
        description="İlk birkaç kaydın adı (en çok 5; adı olmayan türlerde boş). Sayı `count`tur."
    )


class DeleteDetachedGroup(BaseModel):
    """SilinMEYEN ama köke bağı KOPACAK kayıtların bir türü (SET NULL)."""

    table: str
    label: str
    count: int = Field(ge=1)
    is_financial: bool = Field(
        default=False,
        description="Bağı kopan kayıt MALİ bir kayıttır (ör. şantiyesi silinen fatura): kayıt ve "
        "fişi KALIR, yalnız şantiye bağı kopar. Onay penceresi bunu açıkça yazar.",
    )


class DeleteJournalEntry(BaseModel):
    """Ağaçta silinecek bir muhasebe fişi (önizleme dökümü; aynı liste denetim satırına yazılır)."""

    entry_no: str = Field(description="Fiş numarası (ör. `YEV-2026-0214`).")
    entry_date: date
    status: str = Field(description="`draft` | `posted` | `reversed`.")
    is_reversal: bool = Field(description="Bu fiş bir STORNO fişidir (`reversal_of_id` dolu).")
    source_type: str | None = Field(description="Fişi doğuran belge ailesi; elle fişte boş.")
    total: Decimal = Field(description="Fişin borç toplamı (= alacak toplamı).")
    period_closed: bool = Field(
        description="Fişin muhasebe dönemi KAPALI: silinince dönem kilidi atlanır, mizan "
        "geriye dönük değişir (K2). Denetim satırına ayrıca yazılır."
    )


class DeleteClosedPayrollPeriod(BaseModel):
    """Silinecek puantajın düştüğü, bordrosu KAPANMIŞ ay (bordro yerinde kalır)."""

    year: int
    month: int
    status: str = Field(description="`pending_approval` | `approved` | `paid`.")


class DeleteOtherProject(BaseModel):
    """Kökün projesi DIŞINDAKİ bir projede silinecek kayıtlar (zincirleme kapsam)."""

    project_id: uuid.UUID
    name: str
    count: int = Field(
        ge=1,
        description="Bu projeden silinecek kayıt sayısı (`groups` ile aynı sayım biriminde). "
        "Projesi belirlenemeyen kayıtlar (muhasebe fişi, onay zinciri) sayılmaz.",
    )


class DeleteStatusChange(BaseModel):
    """Ağaç DIŞINDA kalan bir kaydın silme sonrası durum değişikliği (önizleme + denetim)."""

    model_config = ConfigDict(populate_by_name=True)

    kind: str = Field(
        description="`invoice` | `progress_payment` | `subcontractor_progress_payment` | "
        "`equipment_rental_invoice`."
    )
    label: str = Field(
        description="Görünen ad (ör. `Fatura F-0007`, `İşveren hakedişi #3 · Kule`)."
    )
    from_: str = Field(alias="from", description="Mevcut durum (ham değer; ör. `collected`).")
    to: str = Field(description="Silme sonrası durum (ham değer; ör. `sent`).")


class DeleteSourceWithoutEntry(BaseModel):
    """Fişi silinecek ama KENDİSİ silinmeyecek kaynak belge (yalnız kök fiş silmede)."""

    table: str
    label: str = Field(description="Türkçe tür adı (ör. `Fatura`).")
    ref: str = Field(description="Belgenin görünen adı (ör. fatura no); bulunamazsa boş.")
    message: str = Field(
        description="Hazır uyarı metni: `Kaynak belge fişsiz kalacak: Fatura F-1`."
    )


class DeletePreviewResponse(BaseModel):
    kind: DeleteKind
    id: uuid.UUID
    kind_label: str = Field(description="Kök türün Türkçe adı (ör. `Şantiye`).")
    label: str = Field(description="Kök kaydın görünen adı (ör. `Kule Şantiyesi`).")
    dependent_count: int = Field(
        ge=0, description="Kök HARİÇ, birlikte silinecek toplam kayıt sayısı (`groups` toplamı)."
    )
    groups: list[DeletePreviewGroup] = Field(
        description="Birlikte silinecek kayıtlar: `count` azalan, sonra `label`. Boş = bağlı yok."
    )
    detached: list[DeleteDetachedGroup] = Field(
        description="Silinmeyecek, yalnız bağı kopacak kayıtlar (ör. bölümü boşalan personel)."
    )
    journal_entry_count: int = Field(
        ge=0, description="Ağaçtaki TÜM muhasebe fişi sayısı (kök fiş dahil; storno fişleri dahil)."
    )
    journal_entries: list[DeleteJournalEntry] = Field(
        description="Silinecek fişlerin dökümü: fiş no artan, en çok "
        f"{JOURNAL_ENTRY_PREVIEW_LIMIT} satır. Kalan sayı `journal_entry_count`tan okunur; "
        "denetim satırı TAM listeyi taşır."
    )
    other_projects: list[DeleteOtherProject] = Field(
        description="Kökün projesi DIŞINDAKİ projelerde silinecek kayıtlar (ör. şantiye → onaylı "
        "hakediş → fatura → ödeme → ortak çek → başka projenin ödemesi): sayı azalan. Boş = "
        "silme yalnız kökün projesinde kalır."
    )
    status_changes: list[DeleteStatusChange] = Field(
        description="Silinmeyecek kayıtların silme sonrası durum değişiklikleri (fatura "
        "`collected → sent`, hakediş `paid → approved`). Ağaçta DEĞİLDİR; kancalar uygular."
    )
    closed_period_entry_count: int = Field(
        ge=0, description="Bu fişlerden kaçının muhasebe dönemi KAPALI (dönem kilidi atlanacak)."
    )
    closed_payroll_timesheet_count: int = Field(
        ge=0,
        description="Bordrosu KAPANMIŞ aya düşen silinecek puantaj satırı sayısı. Yalnız puantaj "
        "gider; bordro dönemi, satırları ve fişi yerinde kalır, mizan değişmez.",
    )
    closed_payroll_periods: list[DeleteClosedPayrollPeriod] = Field(
        description="Bu puantajların düştüğü kapanmış bordro dönemleri (yıl, ay artan)."
    )
    closed_payroll_message: str | None = Field(
        description="Hazır uyarı: `Bordrosu kapanmış ayda N puantaj satırı siliniyor; bordro "
        "değişmez`. Sayı 0 ise boş."
    )
    documents_left_without_entry: list[DeleteSourceWithoutEntry] = Field(
        description="Fişi silinecek ama kendisi KALACAK kaynak belgeler (yalnız `journal_entry` "
        "kökünde dolar): belge–fiş tutarsızlığı bilinçlidir ve burada açıkça gösterilir."
    )
    preview_token: str = Field(
        description="Bu ağacın karması. DELETE'e `preview_token` olarak AYNEN verilir; "
        "ağaç arada değişirse DELETE 409 `preview_stale` döner."
    )


class DeleteErrorResponse(BaseModel):
    """409 ve 428 gövdesi. `code` yalnız silme önkoşullarında dolar: 428 `preview_required`,
    409 `preview_stale` (önizleme eskidi). Başka 409'lar (ör. beklenmeyen veri bütünlüğü hatası)
    yalnız `detail` taşır, `code` boştur. (`financial_pending` SIL-B2'de KALDIRILDI: mali kayıt
    silmeyi artık engellemez.)"""

    detail: str
    code: Literal["preview_required", "preview_stale"] | None = None

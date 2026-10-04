import enum
import uuid
from typing import Literal

from pydantic import BaseModel, Field


class DeleteKind(str, enum.Enum):
    """Önizleme/silme motorunun tanıdığı kayıt türleri. Her dilim yeni üye ekler (SIL-B2…).

    Üye adı = URL parçası. Motorun kayıt defteri (`core/silme`) ile üye kümesi birebir eşit
    olmak ZORUNDADIR; `tests/modules/silme/test_silme_tur_bekcisi.py` çakar.
    """

    site = "site"
    section = "section"
    block = "block"
    unit = "unit"


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
        description="Mali kayıt (hakediş, fatura, ödeme, muhasebe fişi): onay penceresi vurgular."
    )
    samples: list[str] = Field(
        description="İlk birkaç kaydın adı (en çok 5; adı olmayan türlerde boş). Sayı `count`tur."
    )


class DeleteDetachedGroup(BaseModel):
    """SilinMEYEN ama köke bağı KOPACAK kayıtların bir türü (SET NULL)."""

    table: str
    label: str
    count: int = Field(ge=1)


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
    preview_token: str = Field(
        description="Bu ağacın karması. DELETE'e `preview_token` olarak AYNEN verilir; "
        "ağaç arada değişirse DELETE 409 `preview_stale` döner."
    )


class DeleteErrorResponse(BaseModel):
    """409 ve 428 gövdesi. `code` yalnız silme önkoşullarında dolar: 428 `preview_required`,
    409 `preview_stale` (önizleme eskidi) ya da `financial_pending` (ağaçta mali kayıt var; mali
    silme sonraki sürümde açılacak). Başka 409'lar (ör. beklenmeyen veri bütünlüğü hatası)
    yalnız `detail` taşır, `code` boştur."""

    detail: str
    code: Literal["preview_required", "preview_stale", "financial_pending"] | None = None

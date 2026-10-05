"""Onay motorunun okuma/yazma semalari (sozlesme Y5, Y7)."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.approvals.definitions import HistoryDecision
from app.modules.approvals.models import ApprovalDocumentType, ApprovalRole
from app.modules.approvals.service import HistoryChainView, PendingChainView

__all__ = [
    "ApprovalHistoryItem",
    "ApprovalHistoryResponse",
    "ApprovalInboxItem",
    "ApprovalInboxResponse",
    "ApprovalSettingsRead",
    "ApprovalSettingsUpdate",
    "ApprovalStepRead",
]


class ApprovalSettingsRead(BaseModel):
    approval_threshold_try: Decimal


class ApprovalSettingsUpdate(BaseModel):
    """Esik ayari. Kolonun kendisi `Numeric(18, 2)`dir; sema onunla BIREBIR.

    🔴 Bu alan `CompanyUpdate`e EKLENMEZ (R7): `PUT /company` "Sirket Bilgileri"
    formudur ve `settings: full` seviyesine acikken esik `approvals: admin`
    ister. Tek govdede birlesselerdi dusuk kapidan gecen istek yuksek kapinin
    ardindaki degeri yazardi.
    """

    model_config = ConfigDict(extra="forbid")

    approval_threshold_try: Decimal = Field(ge=0, max_digits=18, decimal_places=2)


class ApprovalStepRead(BaseModel):
    """Adim SERIDI (mockup `Onay Kutusu.dc.html:129-135`).

    🔴 "bekliyor / onaylandi" bir DURUM ALANI olarak DONMEZ: `decided_at`
    NULL'sa adim beklemededir ve bunu ekran soyler (KANON E).
    """

    step_no: int
    approval_role: ApprovalRole
    decided_at: datetime | None
    decided_by_name: str | None


class ApprovalInboxItem(BaseModel):
    """Onay kutusu satiri — mockup kartinin BES parcasi (`Onay Kutusu.dc.html`).

    | Kart parcasi | Mockup | Alan |
    |---|---|---|
    | tip rozeti | `:123` `:157` `:216` | `document_type` |
    | olusturan + zaman | `:124` `:159` `:217` | `created_by_name` · `created_at` |
    | baslik | `:126` `:161` `:219` | `title` |
    | alt baslik | `:127` `:162` `:220` | `subtitle` |
    | adim seridi | `:129-135` `:164-170` `:222-224` | `steps` |
    | tutar(lar) | `:138-139` `:173` `:227-228` | `gross_amount` · `net_amount` |

    🔴 `net_amount` SATINALMADA `null`dur: talebin brut/net ayrimi YOKTUR
    (mockup `:173` TEK kutu). 🔴 `gross_amount` da `null` OLABILIR — tutarin
    BELIRLENEMEDIGI hâl gercektir (fiyatsiz kalem) ve `0` yazmak "eksik veri"
    ile "sifir tutar"i ayirt edilemez kilardi (SA kanonu).
    """

    chain_id: uuid.UUID
    document_type: ApprovalDocumentType
    document_id: uuid.UUID
    created_by_name: str | None
    created_at: datetime
    threshold_snapshot: Decimal
    amount_snapshot: Decimal | None
    current_step_no: int
    steps: list[ApprovalStepRead]
    title: str | None
    subtitle: str | None
    gross_amount: Decimal | None
    net_amount: Decimal | None
    can_decide: bool

    @classmethod
    def from_view(cls, view: PendingChainView) -> "ApprovalInboxItem":
        return cls(
            chain_id=view.chain_id,
            document_type=view.document_type,
            document_id=view.document_id,
            created_by_name=view.created_by_name,
            created_at=view.created_at,
            threshold_snapshot=view.threshold_snapshot,
            amount_snapshot=view.amount_snapshot,
            current_step_no=view.current_step_no,
            steps=[
                ApprovalStepRead(
                    step_no=adim.step_no,
                    approval_role=adim.approval_role,
                    decided_at=adim.decided_at,
                    decided_by_name=adim.decided_by_name,
                )
                for adim in view.steps
            ],
            title=view.title,
            subtitle=view.subtitle,
            gross_amount=view.gross_amount,
            net_amount=view.net_amount,
            can_decide=view.can_decide,
        )


class ApprovalInboxResponse(BaseModel):
    """Onay kutusu zarfi.

    🔴 IZN-B3b: `my_approval_roles` KALKTI — onay rolu artik PROJEYE baglidir ve tek bir liste
    yanlis olurdu. "Bu satiri SIMDI onaylayabilir miyim" sorusunu satirdaki `can_decide` yanitlar
    (adimin sahibi aktor + kendi evraki / gorevler ayriligi bekcileri + acik zincir).
    🔴 ACILIYET/RENK SUNUCUDA URETILMEZ (K10 kanonu).
    """

    items: list[ApprovalInboxItem]
    total: int
    limit: int
    offset: int


class ApprovalHistoryItem(ApprovalInboxItem):
    """Onay GECMISI satiri (OKT-B1): bekleyen kartin AYNI alanlari + sonuc.

    * `decision` (zincirin SON DURUMU): `approved` (tum adimlar onayli) |
      `rejected` | `pending` (zincir suruyor; yalniz `decision=all` sorgusunda).
    * `decided_by`: zincirin SON kararini veren: retse reddeden, onaysa SON
      imzayi atan kullanicinin ad soyadi; kullanici silinmisse VE zincir
      suruyorsa `null`.
    * `decided_at`: ret ani ya da son imza ani; zincir suruyorsa `null`.
    * `reason`: YALNIZ retse gerekce, aksi `null`.
    * `current_step_no`: retse reddedilen adim, suren zincirde siradaki adim,
      onaylanmissa son adim.
    * `can_decide` (IZN-B3b): aktor bu zincirin SIRADAKI adimini SIMDI onaylayip/reddedebilir mi.
      Yalniz suren zincirde `true` olabilir; onaylanmis / reddedilmis satirda ve baskasinin
      adiminda `false`.
    """

    decision: HistoryDecision
    decided_by: str | None
    decided_at: datetime | None
    reason: str | None

    @classmethod
    def from_history_view(cls, view: HistoryChainView) -> "ApprovalHistoryItem":
        temel = ApprovalInboxItem.from_view(view)
        return cls(
            **{ad: getattr(temel, ad) for ad in ApprovalInboxItem.model_fields},
            decision=view.decision,
            decided_by=view.decided_by_name,
            decided_at=view.decided_at,
            reason=view.reason,
        )


class ApprovalHistoryResponse(BaseModel):
    """Gecmis zarfi: bekleyen kutusunun zarfi ile ayni bicim. `total`, secilen
    `decision` suzgecine gore suzulmus VE gorunur kapsamla sinirli toplamdir
    (frontend sekme sayaci)."""

    items: list[ApprovalHistoryItem]
    total: int
    limit: int
    offset: int

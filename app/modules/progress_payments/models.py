import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

# Enum PAYLASILIR: `quantity_source` DB tipi taseron hakedisi diliminde
# (a3b4c5d6e7f8) yaratildi; isveren satiri AYNI tipi kullanir. Yeni tip acmak iki
# tarafta ayni rozetin iki farkli tipini dogururdu (site_diary spec §4).
from app.modules.subcontractor_progress_payments.models import QuantitySource


class ProgressPaymentStatus(str, enum.Enum):
    """Hakediş durum makinesi — spec §7. OLU 24 / E15 69 "Onay Bekliyor" / SHK 129
    "Onaylandı" rozeti / SHK 103 "Ödendi"."""

    draft = "draft"
    pending_approval = "pending_approval"
    approved = "approved"
    paid = "paid"


class ProgressPayment(Base):
    """İşveren hakedişi (spec §4.1). D1: sözleşmeye (= projeye) bağlı TEK kayıt —
    şantiye kırılımı satır düzeyindedir (`ProgressPaymentLine.site_id`).

    `is_draft` KOLONU AÇILMAZ — `status` zaten taşıyor; `app/core/access.py`'deki
    `Deletable` protokolünün istediği nitelik aşağıda **property** olarak verilir
    (P5'teki ayrı `is_draft` sütunu + `status` çifti burada gereksiz, çünkü hakedişin
    durum makinesi taslağı zaten bir durum olarak içeriyor).

    Avans/teminat/KDV `amount` kolonları da AÇILMAZ (K3 türev ilkesi, spec §4.1) —
    brüt/KDV/kesinti/net her okuyuşta satırlardan ve snapshot yüzdelerden hesaplanır.
    """

    __tablename__ = "progress_payments"
    __table_args__ = (
        UniqueConstraint("project_id", "sequence_no", name="uq_progress_payments_project_sequence"),
        CheckConstraint(
            "period_month IS NULL OR period_month BETWEEN 1 AND 12",
            name="ck_progress_payments_month_range",
        ),
        CheckConstraint(
            "vat_pct BETWEEN 0 AND 100 "
            "AND advance_pct BETWEEN 0 AND 100 "
            "AND retainage_pct BETWEEN 0 AND 100",
            name="ck_progress_payments_pct_range",
        ),
        CheckConstraint(
            "default_coefficient > 0", name="ck_progress_payments_coefficient_positive"
        ),
        # URL-4: slug GLOBAL tekildir ve indeks KISMIDIR (`WHERE slug IS NOT NULL`)
        # — kolon nullable oldugu icin coklu NULL serbest kalmak ZORUNDA.
        Index(
            "uq_progress_payments_slug",
            "slug",
            unique=True,
            postgresql_where=text("slug IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_contracts.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Servis uretir: proje ici maks+1 (proje kodu deseni, kalici karar 9).
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # URL-4: okunabilir URL kimligi. Anahtar BILESIKTIR (`uq_progress_payments_
    # project_sequence` = proje + sira) — bu yuzden URL-2 karar 1'i korumak icin
    # bilesen AYRISTIRILMAZ, `<proje-slug>-<sira>` olarak URETILIP SAKLANIR
    # (`kopru-guclendirme-5`). Mockup olculdu (`Ekran 15 - İşveren Hakedişi`:
    # h1 `İşveren Hakedişi #5`, alt satir `Güneşkent A-Blok · …`) — insan adi
    # tam olarak PROJE + SIRA'dir.
    # Projesinin slug'i NULL ise bu da NULL kalir: uydurma taban yazilmaz.
    slug: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # Kullanicinin doldurdugu alan — taslakta bos (kalici karar 4).
    period_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    period_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ProgressPaymentStatus] = mapped_column(
        Enum(ProgressPaymentStatus, name="progress_payment_status"),
        nullable=False,
        default=ProgressPaymentStatus.draft,
        server_default="draft",
    )
    # Snapshot (D5): olusturmada project_contracts'tan kopyalanir. Kullanici
    # doldurmaz -> NOT NULL serbest (kullanicinin doldurdugu alan degil).
    vat_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    advance_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    retainage_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    # OLU 69-70 genel katsayi (Dn/D0) — yalniz YENI satirlara ontanimli iner.
    default_coefficient: Mapped[Decimal] = mapped_column(
        Numeric(8, 3), nullable=False, default=Decimal("1.000"), server_default=text("1.000")
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # can_delete korkulugu (app/core/access.py) created_by + is_draft ister.
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    lines: Mapped[list["ProgressPaymentLine"]] = relationship(
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="ProgressPaymentLine.sort_order",
    )

    @property
    def is_draft(self) -> bool:
        """`Deletable` protokolü (`app/core/access.py:47-52`) için — kolon DEĞİL."""
        return self.status == ProgressPaymentStatus.draft


class ProgressPaymentLine(Base):
    """Hakediş satırı — (poz, şantiye, bu dönemin miktarı) üçlüsü (spec §4.2).
    OLU tablosunun bir satırı; poz/şantiye kolonları bu tablodaki AYRI kayıtlardır
    (OLU 91 "Şantiye Bazlı").

    Snapshot beşlisi (`code/description/unit/contract_unit_price/group_name`)
    oluşturma anında `employer_contract_items`'tan kopyalanır ve bir daha
    kendiliğinden güncellenmez (D3/D5) — yalnız `refresh-prices` ucu (H7) tazeler.
    """

    __tablename__ = "progress_payment_lines"
    __table_args__ = (
        CheckConstraint("coefficient > 0", name="ck_progress_payment_lines_coefficient_positive"),
        CheckConstraint("quantity >= 0", name="ck_progress_payment_lines_quantity_nonneg"),
        Index(
            "uq_progress_payment_lines_item_site",
            "payment_id",
            "contract_item_id",
            "site_id",
            unique=True,
            postgresql_where=text("contract_item_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("progress_payments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # SET NULL: kalem silinse de hakedis evraki snapshot'la ayakta kalir
    # (P5 §3.3 `boq_items` gerekcesinin aynisi).
    contract_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employer_contract_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # RESTRICT: hakedisi olan santiye silinemez (P5 §7 desenine sites/guards.py eklemesi).
    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sites.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    # KAT-B2.4: Bakanlik poz no'sunun SNAPSHOT'i (snapshot besliyi altiliya cikarir);
    # kaynak kalemin `source_code`u satir olusurken KOPYALANIR, istemciden ALINMAZ.
    source_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    contract_unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    coefficient: Mapped[Decimal] = mapped_column(
        Numeric(8, 3), nullable=False, default=Decimal("1.000"), server_default=text("1.000")
    )
    # Bu donemin miktari (D4). OLU 172 value="0" kanitiyla 0 mesru — BOQ'daki
    # > 0'dan bilincli fark.
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    group_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Miktarin kaynagi — taseron satirindaki kolonun BIREBIR karsiligi (site_diary
    # spec §4). Enum TIPI PAYLASILIR (`quantity_source`, a3b4c5d6e7f8'de yaratildi):
    # ayni anlam kumesi, iki tarafta ayni rozet — yeni tip ACILMAZ.
    quantity_source: Mapped[QuantitySource] = mapped_column(
        Enum(QuantitySource, name="quantity_source"),
        nullable=False,
        default=QuantitySource.manual,
        server_default="manual",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

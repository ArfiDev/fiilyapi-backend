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


class SubcontractorPaymentStatus(str, enum.Enum):
    """Taşeron hakedişi durum makinesi — işveren hakedişiyle BİREBİR aynı dört durum
    (spec §5). Enum tipi yine de AYRIDIR (`subcontractor_payment_status`): iki evrak
    ailesinin durum kümesi ileride ayrışabilir, paylaşılan tip ikisini birbirine kilitler.

    "Revize Gerekli" (L177) BEŞİNCİ durum DEĞİLDİR — `reject` kaydı `draft`a döndürür,
    rozet `draft AND rejected_at IS NOT NULL` türevidir (spec §5).
    """

    draft = "draft"
    pending_approval = "pending_approval"
    approved = "approved"
    paid = "paid"


class QuantitySource(str, enum.Enum):
    """Satır miktarının kaynağı — O87 "Günlük kayıttan" rozetinin altyapısı (spec §2).

    `site_diary` modülü bu dilimde YOKTUR; bu yüzden bu dilimde üretilen her satır
    `manual`dır. `diary` değeri şantiye günlüğü dilimi geldiğinde dolar.
    """

    manual = "manual"
    diary = "diary"


class SubcontractorProgressPayment(Base):
    """Taşeron hakedişi (spec §2). İşveren `ProgressPayment` deseninin birebiri,
    işaretli farklarla:

    - `sequence_no` **sözleşme kapsamlıdır** (işverende proje kapsamlı) — mockup #47/#48
      sözleşme içi sayaç gösterir → UQ (contract_id, sequence_no).
    - `section_id` bilgi alanıdır (spec §8 S2): O58 "Bölüm" seçicisi, NULL = "Tüm Bölümler".
      Kotaya/hesaba GİRMEZ, salt etiket/filtre.
    - `rejected_at` + `rejection_reason` damgaları (spec §5).

    Tutar kolonu YOKTUR (K3 türev ilkesi) — brüt/KDV/avans/teminat/net her okuyuşta
    satırlardan ve snapshot yüzdelerden hesaplanır. `is_draft` kolon değil, property.
    """

    __tablename__ = "subcontractor_progress_payments"
    __table_args__ = (
        UniqueConstraint(
            "contract_id",
            "sequence_no",
            name="uq_subcontractor_progress_payments_contract_sequence",
        ),
        CheckConstraint(
            "period_month IS NULL OR period_month BETWEEN 1 AND 12",
            name="ck_subcontractor_progress_payments_month_range",
        ),
        CheckConstraint(
            "vat_pct BETWEEN 0 AND 100 "
            "AND advance_pct BETWEEN 0 AND 100 "
            "AND retainage_pct BETWEEN 0 AND 100",
            name="ck_subcontractor_progress_payments_pct_range",
        ),
        CheckConstraint(
            "default_coefficient > 0",
            name="ck_subcontractor_progress_payments_coefficient_positive",
        ),
        # URL-4: slug GLOBAL tekildir ve indeks KISMIDIR (`WHERE slug IS NOT NULL`)
        # — kolon nullable oldugu icin coklu NULL serbest kalmak ZORUNDA.
        Index(
            "uq_subcontractor_progress_payments_slug",
            "slug",
            unique=True,
            postgresql_where=text("slug IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subcontractor_contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Gorunurluk suzgeci (`visible_projects`): sozlesme uzerinden turetilebilir ama
    # her liste sorgusunda JOIN gerektirirdi — sozlesmeden kopyalanir.
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Servis uretir: SOZLESME ici maks+1 (isverendeki proje ici sayacin karsiligi).
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # URL-4: `<sozlesme-slug>-<sira>` (`tsz-2025-001-48`). Bilesik anahtarin
    # (`contract_id`, `sequence_no`) saklanan karsiligi — mockup olculdu
    # (`Taşeron Hakediş Oluştur`: breadcrumb `Akın İnşaat TSZ-2025-001` /
    # `Hakediş #48`). Sozlesmenin slug'i NULL ise bu da NULL kalir.
    slug: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # Kullanicinin doldurdugu alan — taslakta bos.
    period_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    period_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[SubcontractorPaymentStatus] = mapped_column(
        Enum(SubcontractorPaymentStatus, name="subcontractor_payment_status"),
        nullable=False,
        default=SubcontractorPaymentStatus.draft,
        server_default="draft",
    )
    # Snapshot: olusturmada `subcontractor_contracts`tan kopyalanir (spec §2).
    # KDV kaynagi sozlesmenin yeni `vat_pct` kolonudur (spec §8 S1, default 20).
    vat_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    advance_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    retainage_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    # Fiyat farki katsayisi (onayli sapma, spec §3) — yalniz YENI satirlara ontanimli iner.
    default_coefficient: Mapped[Decimal] = mapped_column(
        Numeric(8, 3), nullable=False, default=Decimal("1.000"), server_default=text("1.000")
    )
    # SET NULL: bolum silinse de hakedis evraki ayakta kalir (bilgi alani, spec §8 S2).
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # "Revize Gerekli" rozetinin (L177) kaynagi — yeniden submit'te temizlenir (spec §5).
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    lines: Mapped[list["SubcontractorProgressPaymentLine"]] = relationship(
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="SubcontractorProgressPaymentLine.sort_order",
    )

    @property
    def is_draft(self) -> bool:
        """`Deletable` protokolü (`app/core/access.py`) için — kolon DEĞİL."""
        return self.status == SubcontractorPaymentStatus.draft


class SubcontractorProgressPaymentLine(Base):
    """Taşeron hakediş satırı — (sözleşme kalemi, bu dönemin miktarı) ikilisi (spec §2).

    İşveren satırından farkı: **şantiye kırılımı YOKTUR** — taşeron sözleşmesi zaten
    tek şantiyeye (ya da proje geneline) bağlıdır, kırılım sözleşmede yaşar.

    Snapshot beşlisi (`code/description/unit/contract_unit_price/group_name`) oluşturma
    anında `subcontractor_contract_items`tan kopyalanır; `group_name`
    `source_contract_item_id → employer_contract_groups` zincirinden çözülüp
    snapshot'lanır. Sonradan yalnız `refresh-prices` ucu tazeler.
    """

    __tablename__ = "subcontractor_progress_payment_lines"
    __table_args__ = (
        CheckConstraint("coefficient > 0", name="ck_subcontractor_pp_lines_coefficient_positive"),
        CheckConstraint("quantity >= 0", name="ck_subcontractor_pp_lines_quantity_nonneg"),
        # Kismi benzersiz indeks: bagi kopmus (NULL) satirlar coklanabilir.
        Index(
            "uq_subcontractor_pp_lines_item",
            "payment_id",
            "contract_item_id",
            unique=True,
            postgresql_where=text("contract_item_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subcontractor_progress_payments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # SET NULL: kalem silinse de hakedis evraki snapshot'la ayakta kalir.
    contract_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subcontractor_contract_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    # KAT-B2.4: Bakanlik poz no'sunun SNAPSHOT'i (snapshot besliyi altiliya cikarir);
    # kaynak kalemin `source_code`u satir olusurken KOPYALANIR, istemciden ALINMAZ.
    source_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    # NOT NULL: fiyatsiz (`unit_price IS NULL`) sozlesme kalemi hakedise ALINAMAZ
    # (spec §2 guard'i, 422) — "girilmedi != 0 TL".
    contract_unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    coefficient: Mapped[Decimal] = mapped_column(
        Numeric(8, 3), nullable=False, default=Decimal("1.000"), server_default=text("1.000")
    )
    # Bu donemin miktari; 0 mesru (isveren satirindaki gerekcenin aynisi).
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    group_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
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

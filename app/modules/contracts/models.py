import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
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


class ContractStatus(str, enum.Enum):
    """Sözleşme durumu — SZL 61/71/91 rozetleri (spec §3.1)."""

    active = "active"
    completed = "completed"
    on_hold = "on_hold"


class PaymentPeriod(str, enum.Enum):
    """Hakediş periyodu — FORM 101 açılır sırası (spec §3.5)."""

    monthly = "monthly"
    biweekly = "biweekly"
    on_completion = "on_completion"


class EmployerContractGroup(Base):
    """İşveren sözleşmesi poz grubu (POZ 90/125/140, spec §3.2). `BoqGroup` deseninin
    birebiri — bilinçli simetri: poz dağılımı bu iki yapıyı satır satır eşleştirir.
    Baştaki "A —" harfi SAKLANMAZ, sıra `sort_order`'dan türer, harfi frontend basar.
    """

    __tablename__ = "employer_contract_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_contracts.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    items: Mapped[list["EmployerContractItem"]] = relationship(
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="EmployerContractItem.sort_order, EmployerContractItem.code",
    )


class EmployerContractItem(Base):
    """İşveren sözleşmesi poz kalemi (POZ tablo satırı, spec §3.2). `BoqItem`
    deseninin birebiri. `project_id` grup üzerinden dolaylı erişilebilir ama ayrıca
    burada tutulur: `(project_id, code)` benzersizliği için gereklidir; DB'de
    bileşik FK ile grup→sözleşme tutarlılığı ZORLANMAZ (`BoqItem` §3.3 invariant
    1'in aynısı) — yazma yolu tekil olduğu için servis korkuluğu yeterli kabul edilir.
    """

    __tablename__ = "employer_contract_items"
    __table_args__ = (
        UniqueConstraint("project_id", "code", name="uq_employer_contract_items_project_code"),
        CheckConstraint("quantity > 0", name="ck_employer_contract_items_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_employer_contract_items_unit_price_nonneg"),
        # TKL-B3.1: son fiyat saglayicisi `DISTINCT ON (catalog_item_id)` okur; bagsiz
        # (cogunluk) satirlar indekse girmez (`boq/models.py` kismi indeks emsali).
        Index(
            "ix_employer_contract_items_catalog_item_id",
            "catalog_item_id",
            postgresql_where=text("catalog_item_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_contracts.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employer_contract_groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # Birim serbest metindir, enum degil (BoqItem karari, spec §3.2).
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # TKL-B3.1: katalog iz bagi. SABIT (yalniz olusturmada verilir), kopya degerler
    # (kod/aciklama/birim/fiyat) kalemde yasar — bag yalniz "hangi katalog pozundan" der.
    # `MIRRORED_ITEM_FIELDS`e GIRMEZ (BOQ/taseron/hakedis kopyasi etkilenmez).
    catalog_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ev_catalog_items.id", ondelete="SET NULL"), nullable=True
    )
    # KAT-B2.1: Bakanlik poz no'sunun KOPYASI (snapshot). Istemciden alinmaz; katalog bagi varsa
    # sunucu kopyalar, donusturmede teklif kaleminin snapshot'indan gelir; baglisiz = NULL.
    # `MIRRORED_ITEM_FIELDS`e GIRMEZ (BOQ ayna kumesi degismez).
    source_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # TKL-B3.1: `unit_price` DEGER olarak her degistiginde ilerler (son fiyat sagligi icin);
    # API'de DONMEZ.
    price_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Subcontractor(Base):
    """Taşeron kartoteksi asgari çekirdeği (spec §3.4). `Employer` deseninin birebiri —
    tam cari hesap alanları (kısa ad, cari kod, IBAN, adres) Alt-Proje 3'ün işidir.
    """

    __tablename__ = "subcontractors"
    __table_args__ = (
        # Kismi benzersiz indeks: VKN opsiyoneldir, coklu NULL serbest olmali
        # (`Employer.uq_employers_tax_number` deseninin aynisi, spec §3.4).
        Index(
            "uq_subcontractors_tax_number",
            "tax_number",
            unique=True,
            postgresql_where=text("tax_number IS NOT NULL"),
        ),
        Index("ix_subcontractors_name", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    tax_number: Mapped[str | None] = mapped_column(String(11), nullable=True)
    contact_person: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # category enum DEGIL, String: FORM 82'deki alti secenek frontend'de sabit liste
    # olarak durur, sunucu listeyi zorlamaz (spec §3.4).
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SubcontractorContract(Base):
    """Taşeron sözleşmesi (spec §3.5). `amount` kolonu YOKTUR (K3) — bedel türevdir.
    `employer_contract_id` kolonu YOKTUR: proje ile işveren sözleşmesi 1-1 olduğu
    için `project_id` üzerinden türer, ikinci bir FK aynı bilgiyi iki yerde tutar.
    """

    __tablename__ = "subcontractor_contracts"
    __table_args__ = (
        CheckConstraint(
            "advance_pct BETWEEN 0 AND 100 AND retainage_pct BETWEEN 0 AND 100",
            name="ck_subcontract_pct_range",
        ),
        CheckConstraint("payment_term_days >= 0", name="ck_subcontract_payment_term"),
        # Ayri kisit (mevcut `ck_subcontract_pct_range`e eklenmedi): kolon sonradan
        # geldi, kisiti yeniden yazmak yerine additive tutuldu.
        CheckConstraint("vat_pct BETWEEN 0 AND 100", name="ck_subcontract_vat_pct_range"),
        # Sozlesme no doldurulmussa global tekildir, NULL (taslak) coklanabilir.
        Index(
            "uq_subcontractor_contracts_contract_no",
            "contract_no",
            unique=True,
            postgresql_where=text("contract_no IS NOT NULL"),
        ),
        # URL-4: slug GLOBAL tekildir ve indeks KISMIDIR (`WHERE slug IS NOT NULL`)
        # — kolon nullable oldugu icin coklu NULL serbest kalmak ZORUNDA.
        Index(
            "uq_subcontractor_contracts_slug",
            "slug",
            unique=True,
            postgresql_where=text("slug IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # NULL = proje geneli (K4). RESTRICT: sozlesmesi olan santiye silinemez (spec §7).
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sites.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    # Nullable: taslak destegi. RESTRICT: sozlesmesi olan taseron silinemez.
    subcontractor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subcontractors.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Anlik goruntu — servis her yazmada kartotekten kopyalar (projects.employer_name
    # deseni, spec §3.5). Kartoteks silinse de evrakta ad kalir.
    subcontractor_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    work_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    contract_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # URL-4: kaynak ONCE `contract_no`dur (`tsz-2026-004`), yoksa
    # `subcontractor_name` + `work_category`.
    # 🔴 EMIR PREMISE'I OLCULEREK DUZELTILDI: emir `contract_no`yu "unique
    # DEGIL" sayiyordu; GERCEKTE `uq_subcontractor_contracts_contract_no`
    # KISMI BENZERSIZ INDEKSI vardir (bu dosyada, `WHERE contract_no IS NOT
    # NULL`) — doldurulmussa SIRKET GENELI TEKILDIR. Mockup da olculdu
    # (`Form - Sözleşme Oluştur.dc.html:90`: `Sözleşme No <span class="req">*</span>`,
    # ornek deger `TSZ-2026-004`) → alan ZORUNLU.
    # Kolon yine de nullable KALIR (taslak destegi, K-taslak): NULL satirlar ad+
    # kategori tabanina duser, ikisi de bossa slug NULL kalir.
    slug: Mapped[str | None] = mapped_column(String(160), nullable=True)
    signature_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_notarized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    late_penalty_daily: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    # Isveren tarafindaki 20'den farkli — mockup boyle (FORM 99, spec §3.5).
    advance_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=10, server_default=text("10")
    )
    retainage_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=5, server_default=text("5")
    )
    # Taseron hakedisi spec §8 S1: mockup celiskiliydi (liste %18, form %20) ->
    # sozlesme duzeyinde tasinir, default 20; %18 eski oran artefakti sayildi.
    # Hakedis olusturmada snapshot'lanir.
    vat_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=20, server_default=text("20")
    )
    payment_period: Mapped[PaymentPeriod] = mapped_column(
        Enum(PaymentPeriod, name="payment_period"),
        nullable=False,
        default=PaymentPeriod.monthly,
        server_default="monthly",
    )
    payment_term_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    # Mockup'ta checked ama on-isaretler ornek veridir, uygulanmaz (santiye formu
    # spec §14.2 kurali) — varsayilan false.
    materials_by_contractor: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    subcontractor_files_own_sgk: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    vat_withholding: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    status: Mapped[ContractStatus] = mapped_column(
        Enum(ContractStatus, name="contract_status"),
        nullable=False,
        default=ContractStatus.active,
        server_default="active",
    )
    is_draft: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
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

    items: Mapped[list["SubcontractorContractItem"]] = relationship(
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="SubcontractorContractItem.sort_order, SubcontractorContractItem.code",
    )


class SubcontractorContractItem(Base):
    """Taşeron sözleşmesi kalemi (spec §3.6). Ayrı grup tablosu AÇILMAZ — grup
    başlıkları `source_contract_item_id` → `employer_contract_items.group_id`
    üzerinden türer. Bağsız kalemler `group: null` ile döner (servis işi).
    """

    __tablename__ = "subcontractor_contract_items"
    __table_args__ = (
        UniqueConstraint(
            "contract_id", "code", name="uq_subcontractor_contract_items_contract_code"
        ),
        CheckConstraint("quantity > 0", name="ck_subcontractor_contract_items_quantity_positive"),
        CheckConstraint(
            "unit_price IS NULL OR unit_price >= 0",
            name="ck_subcontractor_contract_items_unit_price_nonneg",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subcontractor_contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Bag kopsa da (kaynak kalem silinse de) taseron kalemi ve fiyati kalir.
    source_contract_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employer_contract_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    # Nullable bilincli: isverenden yuklenen kalem fiyatsiz gelir; "girilmedi" ile
    # "0 TL" ayrimi korunur (spec §3.6).
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

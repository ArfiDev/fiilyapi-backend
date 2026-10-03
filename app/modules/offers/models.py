"""Teklif Hazirlama modeli (TKL-B4.1, TKL-PLAN §2.3a, T31-T37).

Alti tablo: `offer_settings` (tekil), `offer_counters`, `offers` (kunye), `offer_revisions`,
`offer_groups`, `offer_items` + (B5.1) sablon: `offer_templates`, `offer_template_groups`,
`offer_template_items`.

PARA BIRIMI YALNIZ TL (T36): hicbir tabloda para birimi kolonu YOKTUR ve acilmaz. Tum tutarlar
KDV haric TL `Numeric(18,2)`.

BIRIMLER: yuzdeler YUZDE biriminde saklanir (12 = %12), `Numeric(5,2)`; kar `Numeric(6,2)`
(0-999,99), genel gider ve KDV 0-100. Miktar `Numeric(14,3)` > 0; adam-saat `Numeric(12,4)` > 0.

KALEM-GRUP TUTARLILIGI (kalemin grubu AYNI revizyonda olmalidir): DB'DE zorlanir. `offer_groups`
uzerinde `UNIQUE (id, revision_id)` + `offer_items`te bilesik FK `(group_id, revision_id)`.
Servis korkuluguna birakilsaydi (B4.2) bir hata baska revizyonun grubuna kalem yazar ve
"revizyon kopyalama" (gruplar + kalemler) iki revizyonu sessizce karistirirdi; maliyeti bir
fazladan indeks. Bilesik FK yalniz bu tabloya ozeldir, baska tabloyu etkilemez.
"""

from __future__ import annotations

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
    ForeignKeyConstraint,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.modules.projects.models import PriceIndexType

#: Ayarin tohum degerleri (migration ayni degerleri tohumlar).
DEFAULT_OVERHEAD_PCT = Decimal("12.00")
DEFAULT_PROFIT_PCT = Decimal("15.00")
DEFAULT_VAT_PCT = Decimal("20.00")
DEFAULT_VALIDITY_DAYS = 30
DEFAULT_PAYMENT_TERMS = "Ödeme aylık hakedişle, 30 gün vadeli"

#: Tavanlar (CHECK ve API semalari AYNI sabiti okur).
MAX_PCT = Decimal("100")
MAX_PROFIT_PCT = Decimal("999.99")
MAX_VALIDITY_DAYS = 365


class OfferRevisionStatus(str, enum.Enum):
    """Revizyon durumu (T16/T31): `draft → sent → won | lost`; `draft | sent → withdrawn`."""

    draft = "draft"
    sent = "sent"
    won = "won"
    lost = "lost"
    withdrawn = "withdrawn"


class OfferPriceEscalation(str, enum.Enum):
    """Fiyat farki: `tuik` (endeksli, `price_index_type` dolu) | `fixed` (sabit fiyat)."""

    tuik = "tuik"
    fixed = "fixed"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _user_fk() -> Mapped[uuid.UUID | None]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class OfferSettings(Base):
    """Teklif ayarlari — TEKIL satir (`company.only_row` deseni: UNIQUE + `IS TRUE` CHECK).

    Yeni revizyon bu degerleri KOPYALAR (sonradan degisen ayar mevcut teklifleri etkilemez).
    Migration tek satiri 12 / 15 / 20 / 30 gun / varsayilan odeme metniyle tohumlar.
    """

    __tablename__ = "offer_settings"
    __table_args__ = (
        CheckConstraint("only_row IS TRUE", name="ck_offer_settings_single_row"),
        CheckConstraint(
            "default_overhead_pct >= 0 AND default_overhead_pct <= 100",
            name="ck_offer_settings_overhead_range",
        ),
        CheckConstraint(
            "default_profit_pct >= 0 AND default_profit_pct <= 999.99",
            name="ck_offer_settings_profit_range",
        ),
        CheckConstraint(
            "default_vat_pct >= 0 AND default_vat_pct <= 100", name="ck_offer_settings_vat_range"
        ),
        CheckConstraint(
            "default_validity_days >= 1 AND default_validity_days <= 365",
            name="ck_offer_settings_validity_range",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    only_row: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true"), unique=True
    )
    default_overhead_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=DEFAULT_OVERHEAD_PCT, server_default=text("12.00")
    )
    default_profit_pct: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=DEFAULT_PROFIT_PCT, server_default=text("15.00")
    )
    default_vat_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=DEFAULT_VAT_PCT, server_default=text("20.00")
    )
    default_validity_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_VALIDITY_DAYS, server_default=text("30")
    )
    default_payment_terms: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=DEFAULT_PAYMENT_TERMS,
        server_default=text(f"'{DEFAULT_PAYMENT_TERMS}'"),
    )
    updated_at: Mapped[datetime] = _updated_at()


class OfferCounter(Base):
    """Yil bazli teklif numarasi sayaci (`TKL-{yil}-{n:04d}`) — MONOTON, asla geri sarilmaz.

    `accounting.JournalEntryCounter` emsali (gerekce `offers/numbering.py`); tek fark kolon
    anlamidir: `last_no` = DAGITILAN SON numara. UPSERT tek ifadede `last_no + 1` yazip
    DONDURDUGU icin donen deger dogrudan dagitilan numaradir (emsaldeki `- 1` okuma tuzagi yok).
    """

    __tablename__ = "offer_counters"
    __table_args__ = (CheckConstraint("last_no >= 1", name="ck_offer_counters_last_no_positive"),)

    # `autoincrement=False`: yil bir dizi degeri degil veridir (SERIAL'e ayartmasin).
    year: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    last_no: Mapped[int] = mapped_column(Integer, nullable=False)


class Offer(Base):
    """Teklif kunyesi — revizyonlar arasinda ORTAK alanlar. Durum = son revizyonun durumu."""

    __tablename__ = "offers"
    __table_args__ = (
        UniqueConstraint("offer_no", name="uq_offers_offer_no"),
        Index("ix_offers_employer_id", "employer_id"),
        Index("ix_offers_template_id", "template_id"),
        UniqueConstraint("project_id", name="uq_offers_project_id"),
        CheckConstraint("btrim(title) <> ''", name="ck_offers_title_not_blank"),
        CheckConstraint(
            "(project_id IS NULL) = (converted_at IS NULL)", name="ck_offers_conversion_pair"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    offer_no: Mapped[str] = mapped_column(String(32), nullable=False)
    employer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employers.id", ondelete="RESTRICT"), nullable=False
    )
    #: Olusturma/guncelleme anindaki isveren adi (anlik goruntu).
    employer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    scope_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    prepared_by_user_id: Mapped[uuid.UUID | None] = _user_fk()
    #: Sablondan olusturulduysa sablon (B5.1); sablon silinince NULL. Sablon "kullanim sayisi"
    #: bu bagdan TUREVDIR (ayri sayac yok).
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("offer_templates.id", ondelete="SET NULL"), nullable=True
    )
    #: Donusturme izi (TKL-B6): teklifin dogurdugu proje (bir proje TEK tekliften; RESTRICT —
    #: donusturulmus projenin silinmesi engellenir). `project_id` ve `converted_at` ya ikisi
    #: dolu ya ikisi NULL (CHECK). Donusturulen revizyon TUREVDIR (`won` son durum → son revizyon).
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=True
    )
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    converted_by_user_id: Mapped[uuid.UUID | None] = _user_fk()
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class OfferRevision(Base):
    """Teklif revizyonu (Rev.0'dan). Durum damgalari CHECK'lerle durumla tutarli tutulur."""

    __tablename__ = "offer_revisions"
    __table_args__ = (
        UniqueConstraint("offer_id", "rev_no", name="uq_offer_revisions_offer_rev"),
        CheckConstraint("rev_no >= 0", name="ck_offer_revisions_rev_no_nonneg"),
        CheckConstraint(
            "validity_days >= 1 AND validity_days <= 365", name="ck_offer_revisions_validity_range"
        ),
        CheckConstraint(
            "overhead_pct >= 0 AND overhead_pct <= 100", name="ck_offer_revisions_overhead_range"
        ),
        CheckConstraint(
            "profit_pct >= 0 AND profit_pct <= 999.99", name="ck_offer_revisions_profit_range"
        ),
        CheckConstraint("vat_pct >= 0 AND vat_pct <= 100", name="ck_offer_revisions_vat_range"),
        CheckConstraint(
            "delivery_days IS NULL OR delivery_days >= 0",
            name="ck_offer_revisions_delivery_nonneg",
        ),
        # tuik <=> endeks turu dolu (SO-5: varsayilan `fixed`).
        CheckConstraint(
            "(price_escalation = 'tuik') = (price_index_type IS NOT NULL)",
            name="ck_offer_revisions_escalation_index",
        ),
        # Durum damgalari: taslakta hicbiri yok; gonderilmis/kazanilmis/kaybedilmis `sent_at`
        # tasir (vazgecme taslaktan da olabilir, SO-2); won/lost/withdrawn damgasi YALNIZ o
        # durumda dolu.
        CheckConstraint(
            "status NOT IN ('sent', 'won', 'lost') OR sent_at IS NOT NULL",
            name="ck_offer_revisions_stamp_sent",
        ),
        CheckConstraint(
            "status <> 'draft' OR sent_at IS NULL", name="ck_offer_revisions_stamp_draft"
        ),
        CheckConstraint(
            "(status = 'won') = (won_at IS NOT NULL)", name="ck_offer_revisions_stamp_won"
        ),
        CheckConstraint(
            "(status = 'lost') = (lost_at IS NOT NULL)", name="ck_offer_revisions_stamp_lost"
        ),
        CheckConstraint(
            "(status = 'withdrawn') = (withdrawn_at IS NOT NULL)",
            name="ck_offer_revisions_stamp_withdrawn",
        ),
        # T37: kayip nedeni / kazanan tutar YALNIZ `lost` iken dolu olabilir (istege bagli).
        CheckConstraint(
            "status = 'lost' OR (lost_reason IS NULL AND winning_amount IS NULL)",
            name="ck_offer_revisions_lost_fields_only_lost",
        ),
        CheckConstraint(
            "winning_amount IS NULL OR winning_amount >= 0",
            name="ck_offer_revisions_winning_amount_nonneg",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("offers.id", ondelete="CASCADE"), nullable=False
    )
    rev_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[OfferRevisionStatus] = mapped_column(
        Enum(OfferRevisionStatus, name="offer_revision_status"),
        nullable=False,
        default=OfferRevisionStatus.draft,
        server_default=text("'draft'"),
    )
    offer_date: Mapped[date] = mapped_column(Date, nullable=False)
    validity_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_VALIDITY_DAYS, server_default=text("30")
    )
    overhead_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    profit_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    vat_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    payment_terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_escalation: Mapped[OfferPriceEscalation] = mapped_column(
        Enum(OfferPriceEscalation, name="offer_price_escalation"),
        nullable=False,
        default=OfferPriceEscalation.fixed,
        server_default=text("'fixed'"),
    )
    #: PG tipi `price_index_type` PROJELERLE PAYLASILIR (yeniden yaratilmaz).
    price_index_type: Mapped[PriceIndexType | None] = mapped_column(
        Enum(PriceIndexType, name="price_index_type"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_by_user_id: Mapped[uuid.UUID | None] = _user_fk()
    won_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    won_by_user_id: Mapped[uuid.UUID | None] = _user_fk()
    lost_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lost_by_user_id: Mapped[uuid.UUID | None] = _user_fk()
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_by_user_id: Mapped[uuid.UUID | None] = _user_fk()
    #: T37 (para alani): yalniz `lost` iken, istege bagli. Para birimi YALNIZ TL.
    lost_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    winning_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    #: Son KAYIT zamani: kosul/grup/kalem yazimi ve durum gecisi ilerletir (okuma ilerletmez);
    #: FE bayatlik kontrolu. Servis `datetime.now(UTC)` yazar (`now()` islem baslangicidir).
    updated_at: Mapped[datetime] = _updated_at()
    created_by_user_id: Mapped[uuid.UUID | None] = _user_fk()


class OfferGroup(Base):
    """Revizyon ici kalem grubu. `(id, revision_id)` UQ'su kalemin bilesik FK hedefidir."""

    __tablename__ = "offer_groups"
    __table_args__ = (
        UniqueConstraint("id", "revision_id", name="uq_offer_groups_id_revision"),
        Index("ix_offer_groups_revision_id", "revision_id"),
        CheckConstraint("btrim(name) <> ''", name="ck_offer_groups_name_not_blank"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("offer_revisions.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class OfferItem(Base):
    """Teklif kalemi. `poz_no`/`description`/`unit` katalogdan KOPYADIR (sunucu doldurur).

    `quantity` NULL = miktar girilmedi (SO-21; sablondan teklif: toplamlara girmez).
    `cost_unit_price` NULL = maliyet girilmemis (fiyatsiz kalem, toplamlara girmez);
    `overhead_pct`/`profit_pct` NULL = revizyon yuzdesi gecerli; `offer_unit_price` NULL = elle
    teklif B.F. yok (hesaplanir). Hesap `offers/calc.py`dedir; bu tablo yalniz girdiyi saklar.
    """

    __tablename__ = "offer_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["group_id", "revision_id"],
            ["offer_groups.id", "offer_groups.revision_id"],
            ondelete="CASCADE",
            name="fk_offer_items_group_revision",
        ),
        Index("ix_offer_items_revision_id", "revision_id"),
        Index("ix_offer_items_group_id", "group_id"),
        Index("ix_offer_items_catalog_item_id", "catalog_item_id"),
        # SO-21: NULL = "miktar girilmedi" (sablondan teklif); dolu ise > 0.
        CheckConstraint(
            "quantity IS NULL OR quantity > 0", name="ck_offer_items_quantity_null_or_positive"
        ),
        CheckConstraint("unit_mhr > 0", name="ck_offer_items_unit_mhr_positive"),
        CheckConstraint(
            "cost_unit_price IS NULL OR cost_unit_price >= 0", name="ck_offer_items_cost_nonneg"
        ),
        CheckConstraint(
            "offer_unit_price IS NULL OR offer_unit_price >= 0", name="ck_offer_items_offer_nonneg"
        ),
        # SO-4: elle teklif B.F. kar % geri hesabi ve `maliyet + GG + kar = tutar` degismezi icin
        # maliyet ister (servis Turkce 422 verir; CHECK SON savunmadir).
        CheckConstraint(
            "offer_unit_price IS NULL OR cost_unit_price IS NOT NULL",
            name="ck_offer_items_manual_price_needs_cost",
        ),
        CheckConstraint(
            "overhead_pct IS NULL OR (overhead_pct >= 0 AND overhead_pct <= 100)",
            name="ck_offer_items_overhead_range",
        ),
        CheckConstraint(
            "profit_pct IS NULL OR (profit_pct >= 0 AND profit_pct <= 999.99)",
            name="ck_offer_items_profit_range",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("offer_revisions.id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    catalog_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ev_catalog_items.id", ondelete="RESTRICT"), nullable=False
    )
    poz_no: Mapped[str] = mapped_column(String(32), nullable=False)
    #: KAT-B2.1: Bakanlik poz no'sunun KOPYASI (snapshot; katalogdaki kod sonradan degisse/silinse
    #: de kalem eklendigi anin kodunu tutar). NULL = katalog kaleminde kod yoktu.
    source_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    #: NULL = miktar girilmedi (SO-21): toplamlara girmez, gonderimde engellenir.
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    unit_mhr: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    cost_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    overhead_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    profit_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    offer_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)


class OfferTemplate(Base):
    """Teklif sablonu (B5.1, T12): ad + aciklama + GG/kar + gruplar/kalemler (katalog bagi).

    FIYAT ve MIKTAR SAKLANMAZ. `is_default` kismi tekil indeksle TEK satirda dogru olabilir
    (`uq_offer_templates_single_default`); yeni varsayilan eskisini AYNI islemde dusurur.
    """

    __tablename__ = "offer_templates"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_offer_templates_name_not_blank"),
        CheckConstraint(
            "overhead_pct IS NULL OR (overhead_pct >= 0 AND overhead_pct <= 100)",
            name="ck_offer_templates_overhead_range",
        ),
        CheckConstraint(
            "profit_pct IS NULL OR (profit_pct >= 0 AND profit_pct <= 999.99)",
            name="ck_offer_templates_profit_range",
        ),
        Index(
            "uq_offer_templates_single_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: NULL = sablonda oran yok → teklifte ayar degeri.
    overhead_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    profit_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    created_by_user_id: Mapped[uuid.UUID | None] = _user_fk()
    updated_by_user_id: Mapped[uuid.UUID | None] = _user_fk()


class OfferTemplateGroup(Base):
    __tablename__ = "offer_template_groups"
    __table_args__ = (
        UniqueConstraint("id", "template_id", name="uq_offer_template_groups_id_template"),
        Index("ix_offer_template_groups_template_id", "template_id"),
        CheckConstraint("btrim(name) <> ''", name="ck_offer_template_groups_name_not_blank"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("offer_templates.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class OfferTemplateItem(Base):
    """Sablon kalemi: yalniz katalog bagi + sira (kopya alanlar/fiyat/miktar YOK)."""

    __tablename__ = "offer_template_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["group_id", "template_id"],
            ["offer_template_groups.id", "offer_template_groups.template_id"],
            ondelete="CASCADE",
            name="fk_offer_template_items_group_template",
        ),
        Index("ix_offer_template_items_template_id", "template_id"),
        Index("ix_offer_template_items_group_id", "group_id"),
        Index("ix_offer_template_items_catalog_item_id", "catalog_item_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("offer_templates.id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    catalog_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ev_catalog_items.id", ondelete="RESTRICT"), nullable=False
    )

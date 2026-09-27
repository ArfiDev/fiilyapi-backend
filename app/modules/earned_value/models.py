"""Planlama (EV) uzanti tablolari — PLANLAMA-SPEC §2.5, §2.7, §3.8, §3.9.

🔴 Moduler kural (§2.7): cekirdek tablolara KOLON EKLENMEZ. Burada her sey kendi
uzanti tablosunda durur ve cekirdege yalniz FK ile baglanir (BOQ grubu/kalemi,
bolum, santiye). Cekirdek bu dosyayi import ETMEZ.

## Katmanlar
* **Sirket duzeyi** (K2, K4): `ev_disciplines` · `ev_catalog_items`.
* **Santiye duzeyi, revizyonsuz** (K1, B1-6): `ev_site_settings` · `ev_holidays` ·
  `ev_composite_metrics` (+ `ev_composite_metric_terms`).
* **Revizyon** (B1-5): `ev_revisions`; ayni anda EN FAZLA bir taslak ve bir aktif.
* **Revizyona bagli butce girdileri** (B1-6) — yalniz TASLAK yazilir:
  `ev_group_disciplines` (BOQ grubu → disiplin) · `ev_item_settings` (is tipi) ·
  `ev_leaf_settings` (kalem × bolum orani + ezmeler) · `ev_distributions` ·
  `ev_windows` (disiplin × bolum penceresi ezmesi).
* **Donmus baseline** (K8): `ev_baseline_leaves` + `ev_baseline_curve`. Dondurma aninin
  fotografidir; BOQ sonradan degisse de DEGISMEZ — bu yuzden BOQ kalem/bolum
  kimlikleri FK DEGIL duz kolondur (kod/ad snapshot'iyla).

## "Bolumsuz" yaprak (B1-3)
Kalemin bolumlere tahsis EDILMEMIS kalani `section_id IS NULL` yapraktir. NULL'lu
benzersizlik iki kismi indeksle kurulur (PG'de `NULL <> NULL`).

## Sayi hassasiyeti
Oran Numeric(12,4) · miktar BOQ gibi Numeric(14,3) → butce = qty × oran 7 ondalik
tasir. Egri kolonu Numeric(24,8): yayma kuantumu (engine) + en buyuk paya verilen
kuantum-alti ARTIK (Hamilton, `policy.largest_remainder`; butcenin kendi hassasiyeti)
SATIR KAYBI OLMADAN yazilir — "Σ egri = butce" DB'den okununca da `==` tutar
(bekcisi `tests/earned_value_budget/test_ev_models.py`).
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
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.core.db import Base
from app.core.errors import EarnedValueValidationError
from app.modules.earned_value import guards
from app.modules.earned_value.engine import ContractorType
from app.modules.earned_value.labels import NAME_KEY_MAX_LEN, UOM_KEY_MAX_LEN, normalize_label

#: Dagilim tipleri (B1-1/B1-2). DB enum'u motorun `Distribution` enum'undan BAGIMSIZ
#: tanimlanir (model motor surumune kilitlenmesin); esitligi
#: `tests/earned_value_budget/test_ev_models.py` civiler.
DISTRIBUTION_VALUES = ("linear", "bell", "front", "back")
DEFAULT_DISTRIBUTION = "linear"

RATE_PRECISION = (12, 4)
QTY_PRECISION = (14, 3)
MHR_PRECISION = (24, 8)


class RateSource(str, enum.Enum):
    """Oranin nereden geldigi (K4) — atama aninda kopyalanir, kaynak saklanir."""

    CATALOG = "catalog"
    HISTORY = "history"
    MANUAL = "manual"


class RevisionStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class CompositeMeasure(str, enum.Enum):
    """Pacal metrigin TEK olcusu (K25)."""

    SPENT = "spent"
    EARNED = "earned"
    BUDGET = "budget"


def _contractor_enum() -> Enum:
    return Enum(
        ContractorType,
        name="ev_contractor_type",
        values_callable=lambda e: [m.value for m in e],
    )


def _distribution_enum() -> Enum:
    return Enum(*DISTRIBUTION_VALUES, name="ev_distribution")


def _rate_source_enum() -> Enum:
    return Enum(RateSource, name="ev_rate_source", values_callable=lambda e: [m.value for m in e])


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _fk(target: str, ondelete: str, *, nullable: bool = False, index: bool = True):
    return mapped_column(
        UUID(as_uuid=True),
        ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
        index=index,
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------- sirket duzeyi


class EvDiscipline(Base):
    """Sirket disiplin listesi (K2): kod, ad, grafik rengi, varsayilan kendi/taseron."""

    __tablename__ = "ev_disciplines"
    __table_args__ = (
        UniqueConstraint("code", name="uq_ev_disciplines_code"),
        CheckConstraint("color ~ '^#[0-9A-Fa-f]{6}$'", name="ck_ev_disciplines_color_hex"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False)
    default_contractor_type: Mapped[ContractorType] = mapped_column(
        _contractor_enum(), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class EvCatalogItem(Base):
    """Birim oran katalogu satiri — sirket geneli is tipi (KAT). Silme/arsiv YOK (B1-9).

    Tekillik (KATALOG-UQ): `(discipline_id, name_key, uom_key)`. Anahtarlari UYGULAMA yazar
    (`labels.normalize_label`, TEK kaynak; `_sync_key` ad/birim her atandiginda), DB yalniz
    ESITLIGI zorlar. Ifade indeksi (`lower()` …) KULLANILMAZ: Postgres'in kucultmesi DB
    ctype'ina ve glibc/ICU surumune bagli, Python'la birebir degil (KATALOG-UQ K1).

    KATALOG-UQ-2 duzeltme turu (2026-09-27): `name_key`/`uom_key` kolonlari `name`/`uom`dan
    DAHA GENIS (800/200) — NFKC normalizasyonu bazi kod noktalarinda metni UZATIR (en fazla
    18 kat, `…`→`"..."` gibi), "normalize metni UZATMAZ" varsayimi YANLISTI. Genisletme
    migration'i `3102e435238c`; `_sync_key` yine de asiri uzun (kolon sinirini asan) bir
    anahtar dogarsa ACIK Turkce hatayla durur (bkz. asagida).
    """

    __tablename__ = "ev_catalog_items"
    __table_args__ = (
        UniqueConstraint(
            "discipline_id",
            "name_key",
            "uom_key",
            name="uq_ev_catalog_items_disc_name_key_uom_key",
        ),
        CheckConstraint("standard_unit_mhr > 0", name="ck_ev_catalog_items_rate_positive"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    discipline_id: Mapped[uuid.UUID] = _fk("ev_disciplines.id", "RESTRICT")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    uom: Mapped[str] = mapped_column(String(50), nullable=False)
    #: `normalize_label(name)` / `normalize_label(uom)` — elle YAZILMAZ, `_sync_key` turetir.
    #: 🔴 Uzunluk kaynaginkiyle AYNI DEGIL: NFKC normalizasyonu metni UZATABILIR (1268 kod
    #: noktasi, en fazla 18 kat — ör. `…`→`"..."`, `½`→`"1⁄2"`, `㎡`→`"m2"`); bu yuzden
    #: kolonlar `name`/`uom`dan (200/50) DAHA GENIS (800/200, KATALOG-UQ-2 duzeltme turu,
    #: migration `3102e435238c`). Sinirlar `labels.NAME_KEY_MAX_LEN`/`UOM_KEY_MAX_LEN` ile
    #: AYNI olmali — `_sync_key` bunu asan degeri kolona ULASMADAN reddeder.
    name_key: Mapped[str] = mapped_column(String(NAME_KEY_MAX_LEN), nullable=False)
    uom_key: Mapped[str] = mapped_column(String(UOM_KEY_MAX_LEN), nullable=False)
    standard_unit_mhr: Mapped[Decimal] = mapped_column(Numeric(*RATE_PRECISION), nullable=False)
    default_contractor_type: Mapped[ContractorType] = mapped_column(
        _contractor_enum(), nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    standard_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    @validates("name", "uom")
    def _sync_key(self, field: str, value: str) -> str:
        """Ad/birim her atandiginda (kurucu dahil) anahtar yeniden turer — bayat anahtar
        olamaz. Toplu `update()` ifadesi bu kancayi ATLAR: katalogda oyle bir yazar yoktur.

        KATALOG-UQ-2 duzeltme turu: NFKC normalizasyonu metni UZATABILIR — `name`/`uom`
        kolon sinirinda (200/50) kalan bir deger, turetilen anahtarda kolon sinirini
        (800/200) ASABILIR. Kontrol edilmezse DB `22001` (metin tasmasi) fırlatirdi ve
        `_field_overflow_handler` bunu genel "Gönderilen değer alanın sınırını aşıyor"
        422'sine cevirirdi — kullanici ADIN degil ANAHTARIN tastigini hic ogrenemezdi.
        Bu kontrol ACIK ve alana ozel Turkce mesajla ERKEN durur."""
        key = normalize_label(value)
        max_len = NAME_KEY_MAX_LEN if field == "name" else UOM_KEY_MAX_LEN
        if len(key) > max_len:
            field_label = "Ad" if field == "name" else "Birim"
            raise EarnedValueValidationError(
                guards.CATALOG_KEY_TOO_LONG.format(field=field_label, max_len=max_len)
            )
        setattr(self, f"{field}_key", key)
        return value


# ---------------------------------------------------- santiye duzeyi (revizyonsuz)


class EvSiteSettings(Base):
    """Santiye EV ayarlari (AYP). Satir YOKSA varsayilanlar gecerlidir (`defaults.py`).

    `weekly_off_days` bit maskesidir: bit k = `date.weekday() == k` (0 Pzt … 6 Paz).
    Baslangic/bitis tarihi AYAR DEGILDIR (K7): baseline'dan turer.
    """

    __tablename__ = "ev_site_settings"
    __table_args__ = (
        CheckConstraint("week_start_dow BETWEEN 0 AND 6", name="ck_ev_site_settings_week_start"),
        CheckConstraint("weekly_off_days BETWEEN 0 AND 126", name="ck_ev_site_settings_off_days"),
        CheckConstraint(
            "standard_daily_hours >= 1 AND standard_daily_hours <= 16",
            name="ck_ev_site_settings_daily_hours",
        ),
        CheckConstraint("tolerance_points >= 0", name="ck_ev_site_settings_tolerance"),
        CheckConstraint(
            "daily_green_from >= daily_red_below AND daily_high_above >= daily_green_from",
            name="ck_ev_site_settings_daily_bands",
        ),
        CheckConstraint(
            "weekly_green_from >= weekly_red_below", name="ck_ev_site_settings_weekly_bands"
        ),
    )

    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True
    )
    week_start_dow: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    weekly_off_days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    standard_daily_hours: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    tolerance_points: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    daily_red_below: Mapped[Decimal] = mapped_column(Numeric(5, 3), nullable=False)
    daily_green_from: Mapped[Decimal] = mapped_column(Numeric(5, 3), nullable=False)
    daily_high_above: Mapped[Decimal] = mapped_column(Numeric(5, 3), nullable=False)
    weekly_red_below: Mapped[Decimal] = mapped_column(Numeric(5, 3), nullable=False)
    weekly_green_from: Mapped[Decimal] = mapped_column(Numeric(5, 3), nullable=False)
    updated_by_user_id: Mapped[uuid.UUID | None] = _fk(
        "users.id", "SET NULL", nullable=True, index=False
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class EvHoliday(Base):
    """Elle tatil (AYP Tatiller) — tek gun ya da ARALIK (`date_from` = `date_to` tek gun)."""

    __tablename__ = "ev_holidays"
    __table_args__ = (CheckConstraint("date_to >= date_from", name="ck_ev_holidays_range"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    site_id: Mapped[uuid.UUID] = _fk("sites.id", "CASCADE")
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="")


class EvCompositeMetric(Base):
    """Pacal metrik (K25): pay = is tipleri (tek olcu), payda = bir is tipinin miktari."""

    __tablename__ = "ev_composite_metrics"

    id: Mapped[uuid.UUID] = _uuid_pk()
    site_id: Mapped[uuid.UUID] = _fk("sites.id", "CASCADE")
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    measure: Mapped[CompositeMeasure] = mapped_column(
        Enum(
            CompositeMeasure,
            name="ev_composite_measure",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    denominator_boq_item_id: Mapped[uuid.UUID] = _fk("boq_items.id", "CASCADE")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class EvCompositeMetricTerm(Base):
    __tablename__ = "ev_composite_metric_terms"

    metric_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ev_composite_metrics.id", ondelete="CASCADE"),
        primary_key=True,
    )
    boq_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("boq_items.id", ondelete="CASCADE"), primary_key=True
    )


# ------------------------------------------------------------------- revizyon


class EvRevision(Base):
    """Butce revizyonu (B1-5). Taslak → (dondur) → aktif → (yenisi donunca) arsiv."""

    __tablename__ = "ev_revisions"
    __table_args__ = (
        UniqueConstraint("site_id", "number", name="uq_ev_revisions_site_number"),
        CheckConstraint("number >= 0", name="ck_ev_revisions_number"),
        CheckConstraint(
            "(status = 'draft') = (frozen_at IS NULL)", name="ck_ev_revisions_frozen_iff_not_draft"
        ),
        Index(
            "uq_ev_revisions_one_draft",
            "site_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
        ),
        Index(
            "uq_ev_revisions_one_active",
            "site_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    site_id: Mapped[uuid.UUID] = _fk("sites.id", "CASCADE")
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[RevisionStatus] = mapped_column(
        Enum(
            RevisionStatus,
            name="ev_revision_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    frozen_by_user_id: Mapped[uuid.UUID | None] = _fk(
        "users.id", "SET NULL", nullable=True, index=False
    )
    created_by_user_id: Mapped[uuid.UUID | None] = _fk(
        "users.id", "SET NULL", nullable=True, index=False
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


# ------------------------------------------------- revizyona bagli butce girdileri


class EvGroupDiscipline(Base):
    """BOQ grubu → sirket disiplini (K2). Satir yok = "Disiplinsiz"."""

    __tablename__ = "ev_group_disciplines"

    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ev_revisions.id", ondelete="CASCADE"), primary_key=True
    )
    boq_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("boq_groups.id", ondelete="CASCADE"), primary_key=True
    )
    discipline_id: Mapped[uuid.UUID] = _fk("ev_disciplines.id", "RESTRICT")


class EvItemSettings(Base):
    """Is tipi (L3 = BOQ kalemi) ayari (K3). Satir yok = disiplin varsayilani · dogrudan."""

    __tablename__ = "ev_item_settings"

    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ev_revisions.id", ondelete="CASCADE"), primary_key=True
    )
    boq_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("boq_items.id", ondelete="CASCADE"), primary_key=True
    )
    contractor_type: Mapped[ContractorType | None] = mapped_column(
        _contractor_enum(), nullable=True
    )
    is_direct: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    catalog_item_id: Mapped[uuid.UUID | None] = _fk(
        "ev_catalog_items.id", "SET NULL", nullable=True, index=False
    )


class EvLeafSettings(Base):
    """Yaprak (kalem × bolum; bolum NULL = "Bolumsuz") orani + ezmeleri (K3, K4, K12)."""

    __tablename__ = "ev_leaf_settings"
    __table_args__ = (
        CheckConstraint("unit_mhr IS NULL OR unit_mhr >= 0", name="ck_ev_leaf_settings_rate"),
        CheckConstraint(
            "(unit_mhr IS NULL) = (rate_source IS NULL)", name="ck_ev_leaf_settings_rate_source"
        ),
        Index(
            "uq_ev_leaf_settings_section",
            "revision_id",
            "boq_item_id",
            "section_id",
            unique=True,
            postgresql_where=text("section_id IS NOT NULL"),
        ),
        Index(
            "uq_ev_leaf_settings_unsectioned",
            "revision_id",
            "boq_item_id",
            unique=True,
            postgresql_where=text("section_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    revision_id: Mapped[uuid.UUID] = _fk("ev_revisions.id", "CASCADE")
    boq_item_id: Mapped[uuid.UUID] = _fk("boq_items.id", "CASCADE")
    section_id: Mapped[uuid.UUID | None] = _fk("sections.id", "CASCADE", nullable=True)
    unit_mhr: Mapped[Decimal | None] = mapped_column(Numeric(*RATE_PRECISION), nullable=True)
    rate_source: Mapped[RateSource | None] = mapped_column(_rate_source_enum(), nullable=True)
    contractor_type: Mapped[ContractorType | None] = mapped_column(
        _contractor_enum(), nullable=True
    )
    is_direct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class EvDistribution(Base):
    """Disiplin × revizyon dagilim tipi (B1-2). Satir yok = dogrusal."""

    __tablename__ = "ev_distributions"

    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ev_revisions.id", ondelete="CASCADE"), primary_key=True
    )
    discipline_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ev_disciplines.id", ondelete="RESTRICT"), primary_key=True
    )
    distribution: Mapped[str] = mapped_column(_distribution_enum(), nullable=False)


class EvWindow(Base):
    """Disiplin × bolum yayma penceresi EZMESI (B1-3). Satir yok = bolum tarihleri."""

    __tablename__ = "ev_windows"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_ev_windows_range"),
        Index(
            "uq_ev_windows_section",
            "revision_id",
            "discipline_id",
            "section_id",
            unique=True,
            postgresql_where=text("section_id IS NOT NULL"),
        ),
        Index(
            "uq_ev_windows_unsectioned",
            "revision_id",
            "discipline_id",
            unique=True,
            postgresql_where=text("section_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    revision_id: Mapped[uuid.UUID] = _fk("ev_revisions.id", "CASCADE")
    discipline_id: Mapped[uuid.UUID] = _fk("ev_disciplines.id", "RESTRICT")
    section_id: Mapped[uuid.UUID | None] = _fk("sections.id", "CASCADE", nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)


# ------------------------------------------------------------ donmus baseline (K8)


class EvBaselineLeaf(Base):
    """Dondurma anindaki yaprak fotografi. BOQ/bolum kimlikleri FK DEGIL (bkz. modul)."""

    __tablename__ = "ev_baseline_leaves"
    __table_args__ = (
        CheckConstraint("planned_qty >= 0", name="ck_ev_baseline_leaves_qty"),
        CheckConstraint("budget_mhr >= 0", name="ck_ev_baseline_leaves_budget"),
        CheckConstraint(
            "(window_start IS NULL) = (window_end IS NULL)", name="ck_ev_baseline_leaves_window"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    revision_id: Mapped[uuid.UUID] = _fk("ev_revisions.id", "CASCADE")
    boq_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    boq_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    section_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: NULL = disiplinsiz grup (§3.10 F0-2: yalniz DOLAYLI kalem tasiyan disiplinsiz grup
    #: dondurmayi ENGELLEMEZ; o yapraklar egriye girmez ama fotografta durur).
    discipline_id: Mapped[uuid.UUID | None] = _fk(
        "ev_disciplines.id", "RESTRICT", nullable=True, index=False
    )
    item_code: Mapped[str] = mapped_column(String(50), nullable=False)
    item_description: Mapped[str] = mapped_column(Text, nullable=False)
    section_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    uom: Mapped[str] = mapped_column(String(50), nullable=False)
    planned_qty: Mapped[Decimal] = mapped_column(Numeric(*QTY_PRECISION), nullable=False)
    unit_mhr: Mapped[Decimal | None] = mapped_column(Numeric(*RATE_PRECISION), nullable=True)
    rate_source: Mapped[RateSource | None] = mapped_column(_rate_source_enum(), nullable=True)
    contractor_type: Mapped[ContractorType] = mapped_column(_contractor_enum(), nullable=False)
    is_direct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    distribution: Mapped[str] = mapped_column(_distribution_enum(), nullable=False)
    window_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    window_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    budget_mhr: Mapped[Decimal] = mapped_column(Numeric(*MHR_PRECISION), nullable=False)


class EvBaselineCurve(Base):
    """Donmus planli egri: yaprak × gun planned_mhr (K8, K9). Sifir gunler YAZILMAZ."""

    __tablename__ = "ev_baseline_curve"
    __table_args__ = (CheckConstraint("mhr <> 0", name="ck_ev_baseline_curve_nonzero"),)

    leaf_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ev_baseline_leaves.id", ondelete="CASCADE"),
        primary_key=True,
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    mhr: Mapped[Decimal] = mapped_column(Numeric(*MHR_PRECISION), nullable=False)


# ------------------------------------------------------- saha: gunluk saat dagitimi (B2)
#
# PLANLAMA-SPEC §2 + §3.12: muhendis gunun BUTUN saatlerini is kodlarina (= butce agaci
# dugumu) boler (kisi-hucre bolme). Satir = bir kisi (puantajdan, B2-5) ya da bir taseron
# firmasi (gunlukteki taseron satiri: kisi × saat). Hucre = satir × kod × saat.
# Dugum kimligi agacla AYNI metindir (`d:` · `g:` · `i:` · `l:`) — FK DEGIL: kod BOQ'tan
# turer ve B3 onu donmus baseline agacina karsi cozer (bilinmeyen kod = uyari, sessiz kayip
# DEGIL). 🔴 KVKK (B1-11): kisi × kod × saat hucreleri KISISEL VERIDIR; modul AI'da AGREGA.

ALLOCATION_RULE_VALUES = ("direct", "prorata_by_daily_qty")
ROW_KIND_VALUES = ("personnel", "subcontractor")


class EvDayCode(Base):
    """Gunun is kodu (kolon) + dagitim kurali (§2: `direct | prorata_by_daily_qty`)."""

    __tablename__ = "ev_day_codes"

    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    node_id: Mapped[str] = mapped_column(String(90), primary_key=True)
    rule: Mapped[str] = mapped_column(
        Enum(*ALLOCATION_RULE_VALUES, name="ev_allocation_rule"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class EvDayRow(Base):
    """Dagitim izgarasinin satiri. `source_hours` = DAGITIM ANINDAKI kaynak saat kopyasi
    (kisi: puantaj saati · taseron: kisi × saat) — sonradan puantaj degisirse ekran
    "⚠ Puantaj degisti (9 → 11 sa)" der (§4.2)."""

    __tablename__ = "ev_day_rows"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'personnel') = (personnel_id IS NOT NULL) AND "
            "(kind = 'subcontractor') = (subcontractor_id IS NOT NULL)",
            name="ck_ev_day_rows_kind_ref",
        ),
        CheckConstraint("source_hours >= 0", name="ck_ev_day_rows_hours"),
        Index(
            "uq_ev_day_rows_personnel",
            "site_id",
            "day",
            "personnel_id",
            unique=True,
            postgresql_where=text("personnel_id IS NOT NULL"),
        ),
        Index(
            "uq_ev_day_rows_subcontractor",
            "site_id",
            "day",
            "subcontractor_id",
            unique=True,
            postgresql_where=text("subcontractor_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    site_id: Mapped[uuid.UUID] = _fk("sites.id", "CASCADE")
    day: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(
        Enum(*ROW_KIND_VALUES, name="ev_day_row_kind"), nullable=False
    )
    personnel_id: Mapped[uuid.UUID | None] = _fk("personnel.id", "CASCADE", nullable=True)
    subcontractor_id: Mapped[uuid.UUID | None] = _fk("subcontractors.id", "CASCADE", nullable=True)
    source_hours: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)


class EvDayCell(Base):
    """Satir × kod × saat (0,5 sa adim istemcide; backend > 0 ister)."""

    __tablename__ = "ev_day_cells"
    __table_args__ = (CheckConstraint("hours > 0", name="ck_ev_day_cells_hours"),)

    row_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ev_day_rows.id", ondelete="CASCADE"), primary_key=True
    )
    node_id: Mapped[str] = mapped_column(String(90), primary_key=True)
    hours: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)


class EvDayNote(Base):
    """Gunun dagitim notu: dagitilmamis saat GEREKCESI (K14 "dagitilmamis saat")."""

    __tablename__ = "ev_day_notes"

    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    unallocated_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_user_id: Mapped[uuid.UUID | None] = _fk(
        "users.id", "SET NULL", nullable=True, index=False
    )
    updated_at: Mapped[datetime] = _updated_at()


# ------------------------------------------------------------- gun kilidi (B2-6)


class EvReportApproval(Base):
    """Onaylanan gunluk ilerleme raporu — o tarihe KADAR gunlugu ve puantaji kilitler (§2).

    Satiri B3'un "rapor onayi" ucu yazar; B2 yalniz kilidi OKUR ve istisna acar.
    """

    __tablename__ = "ev_report_approvals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    site_id: Mapped[uuid.UUID] = _fk("sites.id", "CASCADE")
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = _fk(
        "users.id", "SET NULL", nullable=True, index=False
    )


class EvDayUnlock(Base):
    """GUN duzeyi kilit istisnasi (B2-6 b): yalniz o gun acilir; o tarihi kapsayan rapor
    SONRADAN yeniden onaylaninca gun yeniden kilitlenir (istisna eski onayi ezer, yeniyi
    DEGIL)."""

    __tablename__ = "ev_day_unlocks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    site_id: Mapped[uuid.UUID] = _fk("sites.id", "CASCADE")
    day: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    unlocked_by_user_id: Mapped[uuid.UUID | None] = _fk(
        "users.id", "SET NULL", nullable=True, index=False
    )
    unlocked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EvReportSnapshot(Base):
    """Onaylanan GUNLUK raporun DONMUS payload'u (B3; §3.13 B3-5).

    Onaydan sonra veri degisse de onayli rapor DEGISMEZ. Ayni gun yeniden onaylanirsa
    (B2-6 gun acma sonrasi) YENI SURUM yazilir, eskisi tarihce olarak kalir. Rapor no =
    proje gun no (K23); surum no ayridir.
    """

    __tablename__ = "ev_report_snapshots"
    __table_args__ = (
        UniqueConstraint("site_id", "report_date", "version", name="uq_ev_report_snapshots_ver"),
        CheckConstraint("version >= 1", name="ck_ev_report_snapshots_version"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    site_id: Mapped[uuid.UUID] = _fk("sites.id", "CASCADE")
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    day_no: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    approval_id: Mapped[uuid.UUID] = _fk("ev_report_approvals.id", "CASCADE")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = _fk(
        "users.id", "SET NULL", nullable=True, index=False
    )

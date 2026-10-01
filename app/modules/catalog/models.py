"""Cekirdek katalog: disiplin listesi + birim oran katalogu (TKL-B1).

TKL-B1: cekirdege tasindi, tablo adlari `ev_*` bilincli korunur (TKL-PLAN §1). Teklif/sozlesme
katalogu cekirdekten kullanir; EV (istege bagli modul) bu dosyadan
import eder, TERSI YOK. DB semasi tasima oncesiyle BIREBIR aynidir.

`ContractorType` motorunkinin (`earned_value/engine/types.py`) IKIZIDIR (cekirdek motoru import
edemez); uyesi/degeri esitligini `tests/modules/catalog/test_contractor_type_esitligi.py`
bekcileri. PG tip adi `ev_contractor_type` AYNI kalir.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.core.db import Base
from app.core.errors import EarnedValueValidationError
from app.core.labels import NAME_KEY_MAX_LEN, UOM_KEY_MAX_LEN, normalize_label
from app.modules.catalog.guards import CATALOG_KEY_TOO_LONG

RATE_PRECISION = (12, 4)


class ContractorType(str, enum.Enum):
    """Motor `ContractorType`inin cekirdek ikizi — AYNI uyeler/degerler/sira."""

    OWN = "own"
    SUBCON = "subcon"


def _contractor_enum() -> Enum:
    return Enum(
        ContractorType,
        name="ev_contractor_type",
        values_callable=lambda e: [m.value for m in e],
    )


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


def _as_core_ct(value: ContractorType | str) -> ContractorType:
    """Kolona motor enumu ya da duz metin atansa da kolonda HEP cekirdek enum durur: tip
    oturum gecmisine (identity map, flush/refresh) bagli olmasin (TKL-B1 curutme bulgusu)."""
    return ContractorType(value)


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

    @validates("default_contractor_type")
    def _core_contractor_type(self, _field: str, value: ContractorType | str) -> ContractorType:
        return _as_core_ct(value)


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

    @validates("default_contractor_type")
    def _core_contractor_type(self, _field: str, value: ContractorType | str) -> ContractorType:
        return _as_core_ct(value)

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
                CATALOG_KEY_TOO_LONG.format(field=field_label, max_len=max_len)
            )
        setattr(self, f"{field}_key", key)
        return value

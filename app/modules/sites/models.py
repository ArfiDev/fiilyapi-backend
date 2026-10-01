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
from sqlalchemy.orm import Mapped, backref, mapped_column, relationship, validates

from app.core.db import Base
from app.core.errors import SiteValidationError
from app.core.labels import NAME_KEY_MAX_LEN, normalize_label
from app.modules.projects.models import Project


class SiteStatus(str, enum.Enum):
    """project_status ile ayni ucludur ama AYRI enum'dur (spec §2.3): sirf bugun
    ayni olduklari icin paylasilan bir enum'a baglamak, santiyeye ileride
    `suspended` gibi bir durum eklemeyi imkansiz kilar.

    Sira mockup satir 71'den gelir: Hazirlik · Aktif (secili) · Beklemede.
    `completed` UI'da gorunmez ama KALIR — `SiteCounts.completed`, `_remaining_days`
    ve P2 liste sekmesi ona baglidir (spec §3.1)."""

    preparation = "preparation"
    active = "active"
    on_hold = "on_hold"
    completed = "completed"


class SectionStatus(str, enum.Enum):
    """Sira `Form - Bolum Ekle` satir 71'den gelir: Planlandi · Aktif · Beklemede.
    `on_hold` P6'da eklendi (spec §4 / §7 S1 onayi); `completed` KALIR."""

    planned = "planned"
    active = "active"
    on_hold = "on_hold"
    completed = "completed"


#: `section_types.name` uzunlugu — `SectionTypeCreate.name` `max_length`i ile AYNI sayi.
SECTION_TYPE_NAME_MAX_LEN = 100

#: Anahtar (normalize) kolonu `name`den GENISTIR: NFKC bazi kod noktalarinda metni UZATIR
#: (KATALOG-UQ-2: en fazla 18 kat). 400 = `NAME_KEY_MAX_LEN`in (800) bu tabloya uygun yarisi;
#: asan anahtar `_sync_key`de ACIK Turkce hatayla durur, DB `22001`e dusmez.
SECTION_TYPE_KEY_MAX_LEN = 400

SECTION_TYPE_KEY_TOO_LONG = (
    "Bölüm tipi adı normalize edildikten sonra çok uzun (sınır: {max_len} karakter); "
    "özel karakterler (üst simge, ligatür, tam genişlik vb.) normalizasyonda uzayabilir"
)

SECTION_TYPE_NAME_BLANK = "Bölüm tipi adı boş olamaz"

assert SECTION_TYPE_KEY_MAX_LEN <= NAME_KEY_MAX_LEN


class SectionType(Base):
    """Bolum tipi — SIRKET GENELI genisletilebilir liste (BLF-B1, kullanici karari F-b).

    Eskiden 7 sabit degerli PG enum'u (`section_type`) idi; artik tablo. Yedi eski
    tip migration ile tohumlanir (testlerde `create_all` kurdugu icin tohum YOKTUR,
    bkz. `tests/_section_types.py`). Silme/yeniden adlandirma ucu bu dilimde YOKTUR;
    `sections.section_type_id` FK'si RESTRICT'tir.

    Tekillik KATALOG-UQ deseniyle: `name_key` anahtarini UYGULAMA yazar
    (`app.core.labels.normalize_label`, TEK kaynak; `_sync_key` her `name` atamasinda),
    DB yalniz ESITLIGI zorlar (`uq_section_types_name_key`). `lower()` ifade indeksi
    KULLANILMAZ: PG kucultmesi Python ile birebir degil (KATALOG-UQ K1).
    """

    __tablename__ = "section_types"
    __table_args__ = (
        UniqueConstraint("name_key", name="uq_section_types_name_key"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_section_types_name_nonblank"),
        # Gorunmez karakterden olusan ad `btrim`i gecer ama anahtari bos kalir.
        CheckConstraint("name_key <> ''", name="ck_section_types_name_key_nonblank"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(SECTION_TYPE_NAME_MAX_LEN), nullable=False)
    #: `normalize_label(name)` — elle YAZILMAZ, `_sync_key` turetir.
    name_key: Mapped[str] = mapped_column(String(SECTION_TYPE_KEY_MAX_LEN), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @validates("name")
    def _sync_key(self, _field: str, value: str) -> str:
        """Ad her atandiginda (kurucu dahil) anahtar yeniden turer — bayat anahtar olamaz.
        Toplu `update()` bu kancayi ATLAR; bu tabloda oyle bir yazar yoktur."""
        key = normalize_label(value)
        if not key:
            raise SiteValidationError(SECTION_TYPE_NAME_BLANK)
        if len(key) > SECTION_TYPE_KEY_MAX_LEN:
            raise SiteValidationError(
                SECTION_TYPE_KEY_TOO_LONG.format(max_len=SECTION_TYPE_KEY_MAX_LEN)
            )
        self.name_key = key
        return value


class Site(Base):
    """Santiye — proje altindaki ikinci katman (Alt-Proje 2 · P2, spec §2.1).

    `contract_amount` sutunu YOK: isveren sozlesmesi proje duzeyindedir, santiye
    payi BOQ dagitiminin turevidir (spec §2.1). `site_manager_name` FK degil
    serbest metindir — santiye sefi her zaman sistem kullanicisi olmayabilir.
    """

    __tablename__ = "sites"
    __table_args__ = (
        # Kisit PROJE ICI tekil KALIR (spec §3.2). Kod uretimi sirket geneli tekildir
        # ama kisiti global `UNIQUE`'e cevirmek mevcut ad-turevi kodlari (`A-BLOK`
        # iki projede birden olabilir) patlatir.
        UniqueConstraint("project_id", "code", name="uq_sites_project_code"),
        # URL-2: slug PROJE İÇİNDE tekildir — `uq_sites_project_code`in aynası.
        # URL nested'dir (`/projeler/<p>/santiyeler/<s>`), yani kapsam URL'de
        # zaten vardır; global tekillik iki projede birden `a-blok` bulunmasını
        # gereksiz yere yasaklardı.
        # Kısmi indeks: slug NULLABLE (gerekçe `Site.slug` alan yorumunda).
        Index(
            "uq_sites_project_slug",
            "project_id",
            "slug",
            unique=True,
            postgresql_where=text("slug IS NOT NULL"),
        ),
        # ISG uzmani YA sistem kullanicisidir YA dis kaynak (OSGB) — ikisi birden
        # olamaz (spec §3.3). Ucuncu gecerli dal: hicbiri (ISG hicbir kosulda zorunlu degil).
        CheckConstraint(
            "NOT (safety_officer_is_outsourced AND safety_officer_user_id IS NOT NULL)",
            name="ck_sites_safety_officer",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Kod ZORUNLU, verilmezse SNT-{YYYY}-{NNN} uretilir
    # (service._next_site_code, spec §3.2) ve kullanici PATCH ile duzeltebilir.
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    # URL-2 — okunabilir URL kimliği. `Project.slug` ile AYNI kurallar:
    # oluşturulurken üretilir, ad değişince DEĞİŞMEZ, nullable (gerekçe orada).
    # 🔴 TEK FARK KAPSAMDIR: burada tekillik PROJE İÇİDİR (`code` ile aynı),
    # `Project.slug`ta şirket geneli. `Section.slug`ta ŞANTİYE İÇİ.
    slug: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[SiteStatus] = mapped_column(
        Enum(SiteStatus, name="site_status"),
        nullable=False,
        default=SiteStatus.active,
        server_default="active",
    )
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    site_manager_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # P1.1a §2.6: mockup santiye satirindaki "Insaat Alani (m²)". Additive + nullable;
    # P2 yuzeylerini kirmaz, P2 ekranlari bu alani okumaz (okuma P3'un isi).
    construction_area_m2: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # ----------------------------------------------------------------- #
    # Santiye formu genislemesi (spec §3.0) — 22 kolon, mockup sirasiyla.
    #
    # HICBIRI "NOT NULL + varsayilansiz" DEGILDIR. Gerekce TASLAK destegidir:
    # "Taslak Kaydet" yarim doldurulmus formu kaydeder, yani mockup'ta zorunlu (*)
    # isaretli alanlar (sef, il/ilce, insaat alani, tarihler) bile DB'de bos
    # durabilmelidir. Zorunluluk YALNIZ uygulama katmaninda, YALNIZ taslak-disi
    # POST'ta uygulanir (spec §5.1). DB'de zorunluluk = taslak yok demektir.
    # ----------------------------------------------------------------- #

    # Santiye sefi (mockup 69): FK + ad anlik goruntusu. FK `SET NULL` cunku
    # kullanici silinse de `site_manager_name` evrakta referans olarak KALMALI.
    site_manager_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # ISG uzmani (mockup 70): uc kolonlu ayrim — sistem kullanicisi / OSGB / hicbiri.
    # OSGB firma adi alani ICAT EDILMEZ; mockup'ta boyle bir input yok (spec §3.3).
    safety_officer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    safety_officer_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    safety_officer_is_outsourced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # Konum & alan (mockup 80-86)
    neighborhood: Mapped[str | None] = mapped_column(String(150), nullable=True)
    parcel: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # GPS TEK metin kolonudur, DOGRULAMA YOK (spec §3.5): bugun hicbir tuketicisi
    # yok. `latitude`/`longitude` Numeric ikilisi bilincli olarak ACILMAZ.
    gps_coordinates: Mapped[str | None] = mapped_column(String(50), nullable=True)
    land_area_m2: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # "2 bodrum + 10 normal" gibi serbest metin — sayi DEGIL (mockup 86).
    floor_info: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Takvim & butce (mockup 97). `budget` PLANLANAN butcedir, sozlesme bedeli
    # degil (spec §3.7). Nullable: "girilmedi" ile "sifir butce" ayrimi korunur.
    # Sure (Gun) TUREVDIR, saklanmaz (spec §3.6).
    budget: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)

    # Depo & santiye altyapisi (mockup 153-165) — 8 ayri Boolean kolon (spec §4).
    # Varsayilan `false`: mockup'taki on-isaretler ornek veridir, UYGULANMAZ (§14.2).
    has_closed_warehouse: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    has_open_storage: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    has_cold_storage: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    has_site_office: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    has_canteen: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    has_changing_room_wc: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    has_dormitory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    has_infirmary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # Abonelikler ve planlanan isci sayisi (mockup 170-172)
    electricity_subscription_no: Mapped[str | None] = mapped_column(String(50), nullable=True)
    water_subscription_no: Mapped[str | None] = mapped_column(String(50), nullable=True)
    planned_worker_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Taslak (mockup 226 "Taslak Kaydet"). Mevcut satirlar `false` = yayinda sayilir.
    is_draft: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    sections: Mapped[list["Section"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", order_by="Section.sort_order"
    )
    # backref, Project'e `sites` koleksiyonunu ekler: bagimlilik tek yonlu kalir
    # (projects modulu sites'i import etmez, sites projects'i import eder).
    # lazy="selectin" zorunlu — async oturumda tembel yukleme MissingGreenlet atar.
    project: Mapped[Project] = relationship(
        Project,
        lazy="selectin",
        backref=backref(
            "sites", lazy="selectin", cascade="all, delete-orphan", order_by="Site.code"
        ),
    )


class Section(Base):
    """Bolum — santiyenin ic kirilimi (spec §2.2). ISTEGE BAGLI katmandir:
    santiye sifir bolumle gecerlidir, otomatik "Genel" bolumu ACILMAZ (spec §2.4).

    BOLUM BEDELI — BLF-B1 (kullanici karari F-a, 2026-10-01): bedel YALNIZ turevdir
    (`SectionResponse.budget`, BOQ tahsislerinden). `budget_amount` kolonu MODELDE
    KALIR (nullable) ama ARTIK OKUNMAZ/YAZILMAZ; kolon ayri dilimde DROP edilir
    (CLEAN-B2 kanonu: once model/okuma, sonra migration). Eski P6 §7 S2a "elle bedel"
    karari bununla sona erdi.
    """

    __tablename__ = "sections"
    __table_args__ = (
        # Kismi benzersiz indeks: kodsuz bolumler serbestce coklanabilir, kod
        # verilmisse santiye icinde benzersizdir.
        Index(
            "uq_sections_site_code",
            "site_id",
            "code",
            unique=True,
            postgresql_where=text("code IS NOT NULL"),
        ),
        # URL-2: slug ŞANTİYE İÇİNDE tekildir — `uq_sections_site_code`in aynası.
        # 🔴 ÖLÇÜLDÜ: `Section.code` zaten nullable ve KISMİ indekslidir, yani
        # bu üç varlık aynı değildir; slug'ın deseni burada `code`unkiyle
        # kendiliğinden örtüşür, `sites`ta ise `code` NOT NULL olduğu için
        # ayrışır (bkz. `Site.slug`).
        Index(
            "uq_sections_site_slug",
            "site_id",
            "slug",
            unique=True,
            postgresql_where=text("slug IS NOT NULL"),
        ),
        CheckConstraint(
            "planned_worker_count IS NULL OR planned_worker_count >= 0",
            name="ck_sections_planned_worker_count",
        ),
        CheckConstraint(
            "budget_amount IS NULL OR budget_amount >= 0",
            name="ck_sections_budget_amount",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sites.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    # URL-2 — okunabilir URL kimliği; kapsam ŞANTİYE İÇİ (bkz. `__table_args__`).
    slug: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[SectionStatus] = mapped_column(
        Enum(SectionStatus, name="section_status"),
        nullable=False,
        default=SectionStatus.planned,
        server_default="planned",
    )
    # Bolum sorumlusu (mockup 111): FK + `manager_name` anlik goruntusu. `SET NULL`
    # cunku kullanici silinse de ad kalmali. (P2'deki "estimated_amount EKLENMEZ"
    # notu P6'da gecersiz kaldi — bkz. sinif docstring'i, `budget_amount`.)
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    manager_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # ----------------------------------------------------------------- #
    # P11 — Gantt bagimliligi (spec §2, S3). TEK oncul: `Form - Bolum Ekle`
    # 115-117 tek select cizer, coklu bagimlilik CIZILMEMISTIR (liste tablosu
    # ACILMAZ). Bag YALNIZ BILGIDIR (BE 117 "Gantt'ta baglanti cizgisi"):
    # tarih kisiti ZORLANMAZ, oncul bitmeden baslayan bolum 422 ALMAZ.
    # `SET NULL`: oncul bolum silinince bagimli bolum SILINMEZ, bagi kopar —
    # bilgi bagi bir varlik kosulu degildir.
    # ----------------------------------------------------------------- #
    depends_on_section_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ----------------------------------------------------------------- #
    # P6 — `Form - Bolum Ekle` alanlari (spec §3). `is_draft` DISINDA hepsi
    # nullable: taslak destegi, mockup'taki `*` yalniz UI ipucudur; zorunluluk
    # uygulama katmaninda ve YALNIZ taslak-disi POST'ta uygulanir.
    # ----------------------------------------------------------------- #
    # BLF-B1: PG enum yerine sirket geneli `section_types` tablosuna FK. RESTRICT:
    # kullanilan tip silinemez. Taslak destegi icin nullable.
    section_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("section_types.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    # lazy="joined": gerekce async ortamda senkron presenter'in tembel yuklemeye dusmemesi
    # (`MissingGreenlet`). Tip bolum SELECT'iyle `LEFT OUTER JOIN` olarak gelir. N+1 yuzeyi
    # yalniz bolum DETAYINDA ve tek satirdir (liste yaniti `section_type` tasimaz); olcum
    # `test_section_detail_loads_type_in_same_select_without_extra_query`.
    section_type: Mapped[SectionType | None] = relationship(lazy="joined")
    # Aciklama / Kapsam (Form 74-75): uzunluk siniri YOK, `Text`.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Yardimci Sorumlu (Form 84): `manager_user_id` deseni — FK `SET NULL` +
    # ad anlik goruntusu, kullanici silinse de evraktaki referans KALIR.
    deputy_manager_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    deputy_manager_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Form 85 — `sites.planned_worker_count` deseni.
    planned_worker_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ESKI elle bedel — BLF-B1'den beri okunmaz/yazilmaz (DROP ayri dilimde).
    budget_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    # Taslak (Form 242 "Taslak Kaydet"). Mevcut satirlar `false` = yayinda sayilir.
    is_draft: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # lazy="selectin" ZORUNLU — async oturumda tembel yukleme MissingGreenlet atar.
    # Siralama (sort_order, id): sort_order esitliginde de deterministik kalir,
    # yoksa timeline yaniti ayni veri icin farkli sirayla donebilirdi.
    milestones: Mapped[list["SectionMilestone"]] = relationship(
        "SectionMilestone",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="(SectionMilestone.sort_order, SectionMilestone.id)",
        back_populates="section",
    )


class SectionMilestone(Base):
    """Bolum kilometre tasi (P11 spec §2) — `Form - Bolum Ekle` 120-125 girisi:
    yalnizca AD + TARIH. Ayri bir CRUD ucu YOKTUR; satirlar bolum govdesiyle
    id-korunumlu birlestirilir (P9 `ShareholderInput.id` emsali).

    DURUM KOLONU YOKTUR (spec §6 S2, kullanici karari): "Tamamlandi" gorunumu
    `milestone_date` ile bugunun TUREVIDIR. Elle isaretlenen bir durum hicbir
    mockup'ta cizilmemistir — icat edilmez.

    `created_at`/`updated_at` YOKTUR: govde ile birlestirilen alt satir deseni
    (`land_share_shareholder` emsali) zaman damgasi tasimaz — satirlar bagimsiz
    bir yasam dongusune sahip degildir, denetim izi bolum mutasyonundadir.
    """

    __tablename__ = "section_milestones"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    section_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    milestone_date: Mapped[date] = mapped_column(Date, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    section: Mapped[Section] = relationship("Section", back_populates="milestones")

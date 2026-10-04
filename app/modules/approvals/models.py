"""Onay zinciri motorunun tablolari (OK-1A, sozlesme Y0).

UC TABLO:
  * `user_approval_roles` — kullanici <-> ONAY ROLU atamasi (COK-A-COK, K1)
  * `approval_chains`     — bir evragin acilmis zincir ORNEGI
  * `approval_steps`      — zincirin adimlari (sira + rol + karar damgasi)

## 🔴 ONAY ROLU, SISTEM ROLU DEGILDIR

`roles.Role` (`users.role_id`) SISTEM rolüdür: izin matrisini, yani hangi
modulde hangi SEVIYEYE (`none < view < draft < request < approve < full <
admin`) sahip olundugunu belirler. `ApprovalRole` ise bir evrak uzerindeki
IMZA SIRASIDIR ve hicbir yetki VERMEZ.

Ikisi kasitli olarak AYRIDIR ve kullanici karariyla (K1) boyle acildi:

* bir kisi BIRDEN COK onay rolu tasiyabilir (kucuk sirkette ayni kisi hem
  Muhasebe hem Patron olabilir). Tek kolon bu kisiyi `admin` olmaya zorlar ve
  SISTEM YONETICILIGI ile TICARI YETKIYI birbirine karistirirdi;
* buna karsilik bir kisi AYNI evragin IKI adimini onaylayamaz (gorevler
  ayriligi) — yani "iki rol" iki imza HAKKI degil, iki imza ADAYLIGIDIR.

Enum DEGERLERI `roles/seed_data.py`deki `ROLES[*]["key"]` sozlugüyle BIREBIR
ayni yazilir (`site_chief` · `project_manager` · `accounting` · `patron` ·
`procurement`). Ikinci bir yazim acilsaydi ayni kavram icin iki sozluk dogar ve
ekranda eslestirme elle yapilmak zorunda kalirdi. Turkce etiketler
`audit/messages.py`dedir, enum DEGERINDE degil.

## Zincirin TANIMI burada DEGIL

Adim listesi bir URUN KURALIDIR ve `definitions.py`dedir (kodda). DB'ye
konsaydi kimsenin cizmedigi bir duzenleme yuzeyi acilirdi. Veritabaninda duran
sey yalniz ZINCIR ORNEGIDIR: hangi evrak, hangi adimlar, kim ne zaman karar
verdi.

## Neden `document_id` FK DEGIL

Zincir UC AYRI evrak ailesine baglanir (taseron hakedisi · satinalma talebi ·
isveren hakedisi) ve Postgres'te cok-bicimli (polymorphic) bir FK yoktur.
Butunluk uygulama katmanindadir; `UNIQUE(document_type, document_id)` ise bir
evragin AYNI ANDA EN FAZLA BIR acik zinciri olmasini DB duzeyinde zorlar.
"""

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
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ApprovalRole(str, enum.Enum):
    """Onay zincirinin ADIM ROLU — sistem rolu DEGILDIR (modul docstring'i).

    Degerler `roles/seed_data.py` anahtarlariyla BIREBIR aynidir; bu bilinclidir
    ve degistirilmemelidir (R1).
    """

    site_chief = "site_chief"
    project_manager = "project_manager"
    accounting = "accounting"
    patron = "patron"
    procurement = "procurement"


class ApprovalDocumentType(str, enum.Enum):
    """Zincire giren evrak aileleri (K4 — bu dilim YALNIZ bu ucudur).

    Fatura ve bordro donemi OK-1B'nin isidir. IZIN TALEBI ve BORDRO SATIRI
    zincire HIC GIRMEZ (kullanici karari): izin talebinin ₺ tutari yoktur ve
    esik anlamsizdir; 48 personellik bir bordro ise 48 zincir acardi.
    """

    subcontractor_progress_payment = "subcontractor_progress_payment"
    purchase_request = "purchase_request"
    progress_payment = "progress_payment"


#: `approval_role` IKI tabloda kullanilir (atama + adim). Tip nesnesi TEK YERDE
#: kurulur ve `metadata`ya baglanir: her kolonda ayri bir `Enum(...)` yazilsaydi
#: `create_all` ayni tipi IKI KEZ yaratmayi denerdi ("type already exists").
#: Desen `procurement.models.payment_terms_enum`den alinmistir.
approval_role_enum = Enum(ApprovalRole, name="approval_role", metadata=Base.metadata)


class UserApprovalRole(Base):
    """Kullanicinin TASIDIGI onay rollerinden BIRI (K1: cok-a-cok).

    `ON DELETE CASCADE`: kullanici silinince atamasi da gider — atama bir IZ
    degil, bir YETKILENDIRMEDIR; sahibi yoksa anlami da yoktur. (Karara baglanmis
    ADIMLAR ise `SET NULL` ile KORUNUR: onay bir olgudur ve silinmez.)
    """

    __tablename__ = "user_approval_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "approval_role", name="uq_user_approval_roles_user_role"),
        Index("ix_user_approval_roles_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    approval_role: Mapped[ApprovalRole] = mapped_column(approval_role_enum, nullable=False)


class ApprovalChain(Base):
    """Bir evrak icin ACILMIS zincir ornegi.

    🔴 **IKI SNAPSHOT DE ZORUNLUDUR** (MK-2 N-carpanli snapshot kanonu). Adim
    listesi `amount >= threshold` KARSILASTIRMASININ turevidir; turev bir deger
    N carpandan olusuyorsa snapshot N'in HEPSINI kapsamalidir. MK-2'de saat
    donduruldu bedel unutuldu, MK-3'te bedel donduruldu payda unutuldu — burada
    her iki carpan da satirda durur ve "neden bu adimlar?" sorusu zincirin
    KENDISINDEN yanitlanir.

    🔴 `amount_snapshot` NULL OLABILIR ve NULL "BELIRLENEMEDI" demektir
    (fail-closed, SA kanonu). Sozlesme bu kolonu NOT NULL baglamisti; olculen
    teknik engel su: tutarin belirlenemedigi hâller GERCEKTIR (fiyatsiz kalem ·
    satirsiz hakedis · `contract_amount IS NULL`) ve o hâlde yazilacak her sayi
    YALAN olurdu. `0` yazilsaydi "eksik veri" ile "sifir tutar" denetim
    yuzeyinde AYIRT EDILEMEZDI — SA'da esigi fiilen atlatan kusurun ta kendisi.
    Karar zaten `approval_steps` satirlarinda maddilesmistir (Patron adimi
    EKLENMISTIR); NULL yalnizca sebebi durust anlatir.
    """

    __tablename__ = "approval_chains"
    __table_args__ = (
        # OKT-B1: eski `UNIQUE(document_type, document_id)` KISMI indekse donustu — ret
        # zinciri artik SILMEZ, reddedilmis zincirler kayit olarak durur.
        Index(
            "uq_approval_chains_open_document",
            "document_type",
            "document_id",
            unique=True,
            postgresql_where=text("rejected_at IS NULL"),
        ),
        Index("ix_approval_chains_created_by_user_id", "created_by_user_id"),
        CheckConstraint(
            "(rejected_at IS NULL) = (rejection_reason IS NULL)",
            name="ck_approval_chains_rejection_pair",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_type: Mapped[ApprovalDocumentType] = mapped_column(
        Enum(ApprovalDocumentType, name="approval_document_type"), nullable=False
    )
    # Cok-bicimli referans — FK YOKTUR (modul docstring'i).
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    threshold_snapshot: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    amount_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    # SET NULL: kullanici silinse de zincir ve onay izi AYAKTA kalir.
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 🔴 OKT-B1 — RET KAYDI. `rejected_at IS NOT NULL` => zincir REDDEDILDI ve
    # TERMINALDIR: ne bekleyen kutusunda, ne "acik zincir" sorgularinda, ne de
    # `uq_approval_chains_open_document` kapsaminda sayilir. Kayit YALNIZ gecmis
    # ekranina (`GET /approvals/history`) hizmet eder. Eski K2 ("ret zinciri
    # siler") kullanici karariyla (KARARLAR 62ae58a) bununla degisti.
    # `rejected_by_user_id` SET NULL: kullanici silinse de ret izi ayakta kalir.
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApprovalStep(Base):
    """Zincirin TEK adimi. `decided_at IS NULL` => henuz karara baglanmamis.

    RET adim satirinda DURUM DEGILDIR (OKT-B1): reddedilen adim KARARA BAGLANMAMIS
    kalir (`decided_at IS NULL`) ve ret zincir satirindadir (`approval_chains.
    rejected_*`). Bu yuzden karara baglanmis her adim hâlâ ONAYLANMIS adimdir ve
    `decided_by_user_id` "kim onayladi" sorusunu tek basina yanitlar. Reddedilen
    adimin numarasi, zincirin karara baglanmamis adimlarinin EN KUCUGUDUR.
    """

    __tablename__ = "approval_steps"
    __table_args__ = (
        UniqueConstraint("chain_id", "step_no", name="uq_approval_steps_chain_step_no"),
        Index("ix_approval_steps_chain_id", "chain_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("approval_chains.id", ondelete="CASCADE"), nullable=False
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    approval_role: Mapped[ApprovalRole] = mapped_column(approval_role_enum, nullable=False)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

import enum
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.access import AccessLevel, Scope
from app.core.db import Base
from app.core.sayfalar import HiddenCategory, PageLevel

SYSTEM_ADMIN_KEY = "system_admin"

#: IZN-B1'in eklediği 6 rolün anahtarları (`seed_data.IZN_ROLE_ORDER` ile eşitliği bekçide).
#: Bu roller `role_permissions` satırı TAŞIMAZ; B2'den itibaren kapılar sayfa hücrelerinden karar
#: verir ve roller kullanıcıya atanabilir (B1 atama kilidi kalktı).
IZN_ROLE_KEYS = frozenset(
    {
        "planning_engineer",
        "technical_office",
        "warehouse_keeper",
        "viewer",
        "finance_manager",
        "cost_engineer",
    }
)


class ModuleGroup(str, enum.Enum):
    GENEL = "GENEL"
    SAHA = "SAHA"
    STOK_SATINALMA = "STOK_SATINALMA"
    MALI = "MALI"
    SISTEM = "SISTEM"


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    emoji: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Module(Base):
    """İzin matrisinin satırları. Sabit referans verisi — migration ile seed edilir."""

    __tablename__ = "modules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    group: Mapped[ModuleGroup] = mapped_column(
        Enum(ModuleGroup, name="module_group"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RolePermission(Base):
    """Matrisin bir hücresi: (rol, modül) -> seviye + kapsam."""

    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "module_id", name="uq_role_module"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("modules.id", ondelete="CASCADE"), nullable=False
    )
    access_level: Mapped[AccessLevel] = mapped_column(
        Enum(AccessLevel, name="access_level"), nullable=False, default=AccessLevel.none
    )
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope"), nullable=False, default=Scope.all
    )


class RolePagePermission(Base):
    """Sayfa bazlı izin matrisinin bir hücresi: (rol, sayfa) -> düzey + onay (IZN-B1).

    `page_key` DB'de SERBEST METİNdir (FK yok): sayfa kataloğu kod sabitidir
    (`app/core/sayfalar.py`); bütünlüğü servis ve `tests/core/test_sayfa_katalogu_bekcisi.py`
    korur. Sistem Yöneticisi hücre TAŞIMAZ — çözücü `role.key == SYSTEM_ADMIN_KEY` için her
    yere "evet" der (IZN-PLAN §1.1). Silme bir düzey değildir; yalnız Sistem Yöneticisi siler.
    """

    __tablename__ = "role_page_permissions"
    __table_args__ = (
        # Onay, görünmeyen bir sayfada anlamsızdır. "Bu sayfada onay eylemi var mı" kuralı
        # katalogdadır (`onay_var`) ve DB'ye sığmaz; o kapıyı yazma servisi (B2) kurar.
        CheckConstraint(
            "NOT can_approve OR level <> 'none'",
            name="ck_role_page_permissions_approve_needs_level",
        ),
    )

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    page_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    level: Mapped[PageLevel] = mapped_column(
        Enum(PageLevel, name="page_level"), nullable=False, default=PageLevel.none
    )
    can_approve: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class RoleHiddenField(Base):
    """Rolün gizlediği hassas alan kategorisi. Satır var = o kategori bu rolde gizli (IZN-B1)."""

    __tablename__ = "role_hidden_fields"

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    category: Mapped[HiddenCategory] = mapped_column(
        Enum(HiddenCategory, name="hidden_category"), primary_key=True
    )

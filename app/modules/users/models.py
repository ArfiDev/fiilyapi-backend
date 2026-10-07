import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.modules.roles.models import Role


class UserStatus(str, enum.Enum):
    active = "active"
    on_leave = "on_leave"
    passive = "passive"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    title: Mapped[str] = mapped_column(String(150), nullable=False, default="")
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status"), nullable=False, default=UserStatus.active
    )
    # IZN-B3: "Tüm projeler" işareti. true → kişi her projeyi ANA rolüyle görür ve proje içi
    # sayfalarda da ana rolle çalışır; `project_members` satırı taşımaz, disiplin kısıtı olmaz.
    all_projects: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Token iptali için sürüm sayacı. Token'lara gömülür; logout ve parola sıfırlama bunu
    # artırır, böylece o andan önce basılmış tüm token'lar geçersiz olur.
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # lazy="raise" kasıtlıdır: async ortamda tembel yükleme sessizce patlar.
    # Bu ayar, ilişkiyi açıkça yüklemeyi unuttuğumuzda hatayı geliştirme anında görünür kılar.
    role: Mapped[Role] = relationship(lazy="raise")


class ProjectMember(Base):
    """Proje ekibi (IZN-B3, KARARLAR §1.7): kişi × proje × O PROJEDEKİ rol.

    Kişi yalnız ekibinde olduğu projeyi görür; proje içi sayfalar bu satırdaki rolle çalışır
    (şirket geneli sayfalar ANA rolle). `users.all_projects=true` kişide satır bulunmaz.
    Rol FK'si RESTRICT: ekipte kullanılan rol silinemez (`RoleResponse.user_count` bunu sayar).
    """

    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_project_members_user_project"),
        Index("ix_project_members_project_id", "project_id"),
        Index("ix_project_members_role_id", "role_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProjectMemberDiscipline(Base):
    """Ekip üyesinin O PROJEDEKİ disiplinleri (IZN-B3). Satır yok = o projede kısıtsız.

    Global `user_disciplines`in yerini alır. Disiplin RESTRICT (atanmış disiplin silinemez;
    `catalog_service.delete_discipline` anlamlı 409 verir), üye silinince satırlar CASCADE gider.
    """

    __tablename__ = "project_member_disciplines"
    __table_args__ = (Index("ix_project_member_disciplines_discipline_id", "discipline_id"),)

    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id", ondelete="CASCADE"), primary_key=True
    )
    discipline_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ev_disciplines.id", ondelete="RESTRICT"), primary_key=True
    )

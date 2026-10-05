import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.discipline_ref import DisciplineRef
from app.modules.users.models import UserStatus

#: Bir proje ekibi satırına atanabilecek en fazla disiplin (şirket kataloğu küçüktür; sınır gövdeyi
#: sınırlamak içindir, iş kuralı değil).
MAX_PROJECT_DISCIPLINES = 100


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=150)
    title: str = Field(default="", max_length=150)
    role_id: uuid.UUID
    status: UserStatus = UserStatus.active


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=150)
    title: str | None = Field(default=None, max_length=150)
    role_id: uuid.UUID | None = None
    status: UserStatus | None = None


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


class ProjectMemberInput(BaseModel):
    """Bir projedeki ekip satırı (PUT gövdesi): o projedeki rol + o projedeki disiplinler."""

    project_id: uuid.UUID
    role_id: uuid.UUID
    # Boş = o projede KISITSIZ. Yinelenen kimlik tekilleştirilir (sınır tekilleştirmeden ÖNCE).
    discipline_ids: list[uuid.UUID] = Field(
        default_factory=list, max_length=MAX_PROJECT_DISCIPLINES
    )


class UserAccessInput(BaseModel):
    """`PUT /users/{id}/access` gövdesi: ana rol + proje ekibi, TAM DEĞİŞTİRME (atomik).

    `all_projects=true` iken `projects` BOŞ olmak zorundadır (422): bu kişi her projeyi ana
    rolüyle görür, ekip satırı ve disiplin kısıtı taşımaz.
    """

    role_id: uuid.UUID
    all_projects: bool = False
    projects: list[ProjectMemberInput] = Field(default_factory=list)


class ProjectMemberResponse(BaseModel):
    project_id: uuid.UUID
    project_name: str
    role_id: uuid.UUID
    # Disiplin çipleri (kod sırasıyla); boş = "Tüm disiplinler".
    disciplines: list[DisciplineRef]


class UserAccessResponse(BaseModel):
    """`GET`/`PUT /users/{id}/access` yanıtı (ikisi AYNI şema). `projects` ad sırasıyla."""

    role_id: uuid.UUID
    all_projects: bool
    projects: list[ProjectMemberResponse]


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    title: str
    role_id: uuid.UUID
    status: UserStatus
    last_login_at: datetime | None = None
    # IZN-B3: "Tüm projeler" işareti ve proje ekibi satır sayısı (liste satırı: "N proje" /
    # "Tüm projeler"). `all_projects=true` kişide `project_count` 0'dır. Tek COUNT … GROUP BY.
    all_projects: bool
    project_count: int


class UserListResponse(BaseModel):
    items: list[UserResponse]
    total: int
    limit: int
    offset: int

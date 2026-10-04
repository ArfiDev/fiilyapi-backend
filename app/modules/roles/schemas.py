import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.core.access import AccessLevel, Scope
from app.modules.roles.models import ModuleGroup


class RoleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    key: str
    name: str
    emoji: str
    description: str
    is_system: bool
    # IZN-B1: kullanıcıya atanabilir mi? `false` = henüz etkin olmayan yeni rol (B2 kapı köprüsüne
    # dek). FE rol seçicilerinde bu alanla süzer; rol anahtarı elle kodlanmaz.
    is_assignable: bool


class RoleCreate(BaseModel):
    key: str = Field(min_length=2, max_length=50, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=100)
    emoji: str = Field(default="", max_length=8)
    description: str = Field(default="", max_length=2000)


class RoleRename(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    emoji: str = Field(default="", max_length=8)
    description: str = Field(default="", max_length=2000)


class ModuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    key: str
    name: str
    group: ModuleGroup
    sort_order: int


class PermissionCell(BaseModel):
    module_key: str
    access_level: AccessLevel
    scope: Scope


class PermissionUpdate(BaseModel):
    access_level: AccessLevel
    scope: Scope

import uuid

from pydantic import BaseModel, EmailStr

from app.core.sayfalar import HiddenCategory, PageKey
from app.modules.pages.schemas import PageGrant
from app.modules.users.models import UserStatus


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class MeProject(BaseModel):
    """Proje ekibi satırı (IZN-B3): o projedeki rol (anahtar) + o projedeki disiplinler."""

    project_id: uuid.UUID
    # Rolün sayfa haritası ana rolden FARKLIYSA `MeResponse.role_pages[role_key]`te durur;
    # ana rolle aynıysa istemci `MeResponse.pages`i kullanır.
    role_key: str
    # Boş = o projede disiplin kısıtı YOK. `id`ye göre sıralı.
    discipline_ids: list[uuid.UUID]


class MeRolePages(BaseModel):
    """Ekip rolünün sayfa izinleri + gizli alanları (`MeResponse.pages`/`hidden_fields` biçimi)."""

    pages: dict[PageKey, PageGrant]
    hidden_fields: list[HiddenCategory]


class MeResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    title: str
    role_key: str
    # IZN-B1: Sistem Yöneticisi her yere erişir, hücre taşımaz; silme yetkisi yalnız bunda.
    is_system_admin: bool
    status: UserStatus
    # IZN-B1 (EKLEYİCİ): aktörün ANA rolünün sayfa izinleri: sayfa anahtarı -> {level, approve}.
    # Anahtar kümesi `GET /pages` kataloğudur. Sistem Yöneticisi için her sayfa
    # {edit, approve=onay eylemi var mı}. Rolün satırı olmayan sayfa katalogdan `none`
    # (approve=false) ile DOLDURULUR (IZN-B6a-me; kapılar da eksik hücreyi none sayar).
    # Kapılar bu hücrelerden karar verir (IZN-B6b: eski `permissions` modül haritası kalktı).
    pages: dict[PageKey, PageGrant]
    # Ana rolün gizlediği hassas alan kategorileri (kutucuk işaretli olanlar), sıralı.
    hidden_fields: list[HiddenCategory]
    # IZN-B3: "Tüm projeler" işareti. true → `projects` BOŞ gelir ve istemci proje içi sayfalarda
    # da ANA rolün `pages` haritasını kullanır; disiplin kısıtı olmaz.
    all_projects: bool
    # IZN-B3: proje başına rol + disiplin (proje kimliğine göre sıralı). `all_projects` kişide boş.
    projects: list[MeProject]
    # IZN-B3: ekipte kullanılan ve ANA rolden FARKLI rollerin sayfa haritası, rol ANAHTARIyla.
    # Boyut: rol başına ≈100 sayfa ≈ 3–4 KB; kişi başına farklı rol sayısıyla büyür, proje
    # sayısıyla DEĞİL (aynı rolü 30 projede taşıyan kişi tek harita taşır).
    role_pages: dict[str, MeRolePages]

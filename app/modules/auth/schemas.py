import uuid

from pydantic import BaseModel, EmailStr

from app.core.access import AccessLevel
from app.core.discipline_ref import DisciplineRef
from app.core.sayfalar import HiddenCategory, PageKey, PageLevel
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


class PageGrant(BaseModel):
    """Bir sayfadaki erişim: düzey + onay eylemi. Silme düzey DEĞİLDİR (`is_system_admin`)."""

    level: PageLevel
    approve: bool


class MeResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    title: str
    role_key: str
    # IZN-B1: Sistem Yöneticisi her yere erişir, hücre taşımaz; silme yetkisi yalnız bunda.
    is_system_admin: bool
    status: UserStatus
    # Aktörün KENDİ izin haritası: modül anahtarı -> erişim seviyesi.
    # Ek yetki İSTEMEZ (bilinçli): `/roles/{id}/permissions` `user_management:view`
    # arar, bu yüzden salt-okunur bir rol kendi seviyesini göremiyordu ve frontend
    # yazma butonlarını gizleyemiyordu. Kendi izninin okunması yetki sızıntısı
    # değildir — aktör zaten o seviyeyi uçları deneyerek keşfedebilir.
    # İzin satırı olmayan modül haritada YER ALMAZ; frontend bunu "bilinmezlik"
    # sayıp kontrolü görünür bırakır (güvenlik sınırı her zaman backend'dedir).
    permissions: dict[str, AccessLevel]
    # Kullanicinin atanmis disiplinleri (DSC-B0/B0b; id'ye gore sirali; `/users/{id}/
    # disciplines` ile AYNI anahtar). Bos = atamasiz = KISITSIZ (proje muduru/admin).
    # Modul kaydi yoksa da bos.
    disciplines: list[DisciplineRef]
    # IZN-B1 (EKLEYİCİ): aktörün ANA rolünün sayfa izinleri: sayfa anahtarı -> {level, approve}.
    # Anahtar kümesi `GET /pages` kataloğudur. Sistem Yöneticisi için her sayfa
    # {edit, approve=onay eylemi var mı}. Rolün satırı olmayan sayfa haritada YER ALMAZ; frontend
    # bunu "bilinmezlik" sayar (`permissions` ile aynı kural). Henüz KAPI DEĞİL: uç kapıları
    # `permissions` (eski modül matrisi) ile çalışmaya devam eder (B2'de köprülenir).
    pages: dict[PageKey, PageGrant]
    # Ana rolün gizlediği hassas alan kategorileri (kutucuk işaretli olanlar), sıralı.
    hidden_fields: list[HiddenCategory]

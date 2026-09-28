import uuid

from pydantic import BaseModel, EmailStr

from app.core.access import AccessLevel
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


class MeResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    title: str
    role_key: str
    status: UserStatus
    # Aktörün KENDİ izin haritası: modül anahtarı -> erişim seviyesi.
    # Ek yetki İSTEMEZ (bilinçli): `/roles/{id}/permissions` `user_management:view`
    # arar, bu yüzden salt-okunur bir rol kendi seviyesini göremiyordu ve frontend
    # yazma butonlarını gizleyemiyordu. Kendi izninin okunması yetki sızıntısı
    # değildir — aktör zaten o seviyeyi uçları deneyerek keşfedebilir.
    # İzin satırı olmayan modül haritada YER ALMAZ; frontend bunu "bilinmezlik"
    # sayıp kontrolü görünür bırakır (güvenlik sınırı her zaman backend'dedir).
    permissions: dict[str, AccessLevel]
    # Kullanicinin atanmis disiplinleri (DSC-B0; id'ye gore sirali). Bos = atamasiz =
    # KISITSIZ (proje muduru/admin). Modul kaydi yoksa da bos.
    disciplines: list[uuid.UUID]

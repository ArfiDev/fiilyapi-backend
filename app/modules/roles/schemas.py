import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.core.sayfalar import HiddenCategory, PageKey
from app.modules.pages.schemas import PageGrant


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
    # IZN-B2/B3: rolü ANA rol olarak YA DA bir proje ekibi satırında proje rolü olarak taşıyan
    # FARKLI kullanıcı sayısı (iki yoldan bağlı kişi tek sayılır). `0` ⇔ rol silinebilir
    # (Sistem Yöneticisi hariç). Frontend artık `/users`tan HESAPLAMAZ.
    user_count: int
    # IZN-B2: Sistem Yöneticisi kartı: sayfa izinleri değiştirilemez, rol silinemez/kopyası
    # kilidi taşımaz. `is_system` bu anlamı taşımaz (Patron'da da true).
    is_locked: bool


class RoleCreate(BaseModel):
    key: str = Field(min_length=2, max_length=50, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=100)
    emoji: str = Field(default="", max_length=8)
    description: str = Field(default="", max_length=2000)


class RoleRename(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    emoji: str = Field(default="", max_length=8)
    description: str = Field(default="", max_length=2000)


class RoleCopy(BaseModel):
    """`POST /roles/{id}/copy` gövdesi. `key` SUNUCUDA addan türetilir (çakışırsa `_2` eki)."""

    name: str = Field(min_length=1, max_length=100)
    emoji: str = Field(default="", max_length=8)
    description: str = Field(default="", max_length=2000)


class RolePagesResponse(BaseModel):
    """Bir rolün sayfa izinleri (Sayfa İzinleri ekranı): `/auth/me.pages` ile AYNI biçim.

    `pages` rolün 100 sayfasının TÜMÜNÜ taşır (hücresi olmayan sayfa `none`/`false` döner).
    Sistem Yöneticisi için her sayfa `edit` + (onay eylemi varsa) `approve=true`, `is_locked=true`.
    """

    role_id: uuid.UUID
    is_locked: bool
    pages: dict[PageKey, PageGrant]
    hidden_fields: list[HiddenCategory]
    # IZN-B4: yeni alan maskesi HER rolde geçerlidir, bu yüzden bu alan HER ZAMAN `true` (eski
    # hibrit kural kalktı). Alan yalnız frontend uyumu için KALIR; B6'da kaldırılabilir.
    hidden_fields_effective: bool


class RolePagesUpdate(BaseModel):
    """`PUT /roles/{id}/pages` gövdesi: Sayfa İzinleri ekranının "Kaydet"i TÜM tabloyu gönderir.

    TAM matris: `pages` katalogdaki 100 sayfanın HEPSİNİ taşımak zorundadır (eksik anahtar 422;
    bilinmeyen/fazla anahtar Pydantic 422'si — anahtar bir ENUM'dur). Gerekçe: kısmi gövde
    "gönderilmeyen sayfa ne olur" belirsizliğini doğurur ve eski bir istemci yeni eklenen sayfayı
    sessizce `none` bırakır; tam gövde ile sonuç gövdenin kendisidir (idempotent, farksız).
    `hidden_fields` gizli kategori kümesinin TAM değiştirmesidir (boş = hiçbiri gizli değil;
    yinelenen değer tekilleştirilir). Pages + hidden_fields TEK transaction'da yazılır (atomik).
    Kurallar (servis, 422): `approve=true` yalnız `has_approval=true` sayfada; `level=none` iken
    `approve=true` olamaz. Bu dilimde `tum_tutarlar` dışındaki kategoriler yalnız SAKLANIR
    (maske IZN-B4).
    """

    pages: dict[PageKey, PageGrant]
    hidden_fields: list[HiddenCategory]

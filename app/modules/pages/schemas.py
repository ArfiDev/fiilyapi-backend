from pydantic import BaseModel

from app.core.sayfalar import PageGroup, PageKey, PageKind


class PageResponse(BaseModel):
    """Katalogdaki bir sayfa. `key` bir ENUM'dur: OpenAPI'den TS birleşik tipi (union) üretilir."""

    key: PageKey
    name: str
    group: PageGroup
    group_name: str
    # Yalnız "Proje içi sekmeler" grubunda: Proje / Şantiye / Bölüm.
    subgroup: str | None
    route: str
    kind: PageKind
    # Sayfada "Onaylar" kutucuğu gösterilir mi (onay zinciri / onay eylemi var mı).
    has_approval: bool
    # Aynı veriyi besleyen sayfaları toplayan kaynak adı (B2/B5 uç kapıları bunun üstüne kurulur).
    source: str
    # Yalnız kök sayfada: aynı veriyi proje bağlamında gösteren proje-içi sayfalar (menü K5 kuralı).
    twins: list[PageKey]

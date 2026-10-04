from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.sayfalar import GRUP_ADLARI, SAYFALAR
from app.modules.pages.schemas import PageResponse
from app.modules.users.models import User

router = APIRouter(tags=["pages"], responses=COMMON_ERROR_RESPONSES)


@router.get("/pages", response_model=list[PageResponse])
async def list_pages_endpoint(
    _user: Annotated[User, Depends(get_current_user)],
) -> list[PageResponse]:
    """Sayfa kataloğu: 100 sayfa, menü sırasıyla.

    Kapı: yalnız oturum (`get_current_user`) — modül izni DEĞİL. Katalog bir kod sabitidir
    (kiracı verisi taşımaz) ve her oturumun frontend'i `nav-config`i bu anahtarlarla çakıştırır;
    izin ekranı (B2/F2) zaten kendi yazma kapısını taşır. `GET /company` ve `/auth/me` ile aynı
    desen. DB'ye dokunmaz.
    """
    return [
        PageResponse(
            key=sayfa.key,
            name=sayfa.ad,
            group=sayfa.grup,
            group_name=GRUP_ADLARI[sayfa.grup],
            subgroup=sayfa.alt_grup,
            route=sayfa.rota,
            kind=sayfa.tur,
            has_approval=sayfa.onay_var,
            source=sayfa.kaynak,
            twins=list(sayfa.ikizler),
        )
        for sayfa in SAYFALAR
    ]

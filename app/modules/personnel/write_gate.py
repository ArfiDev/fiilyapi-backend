"""Personel oluşturma (POST) yazma kapısı — tekillik denetimi sızıntısını kapatır (IZN-B5a, 21c).

Genel kural (`core/mask_route`): POST gövdesinde gizli kategorili alan SERBESTTİR (gizlilik okuma
içindir). İSTİSNA: alan TEKİLLİK denetliyorsa (`tc_no` → `uq_personnel_tc_no`, 409 "TCKN kayıtlı")
gizli maskeli bir rol bu alanı DOLU göndererek kaydın VARLIĞINI öğrenebilirdi. Bu yüzden böyle
bir alan aktörün maskesinde gizliyse dolu gönderim tekillik denetiminden ÖNCE 403 alır (PATCH'teki
gizli alan 403'üyle AYNI gövde). Gizli alanı göremeyen rol alanı boş bırakıp personel açabilir.

Personelde tekilliği denetlenen kişisel alan YALNIZ `tc_no`dur (ölçüldü: modeldeki tek `UNIQUE`
`uq_personnel_tc_no`; `iban`/`sgk_no`/`phone`/`email` için tekillik denetimi yok → serbest kalır).
"""

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.field_mask import gizli_mi, yazilan_hassas_alanlar
from app.core.mask_context import kumeleri_coz
from app.core.mask_route import _YAZMA_REDDI
from app.modules.personnel.schemas import PersonnelCreate
from app.modules.users.models import User

TEKIL_KISISEL_ALANLAR = frozenset({"tc_no"})


async def reject_hidden_unique_fields(
    session: AsyncSession, user: User, request: Request, data: PersonnelCreate
) -> None:
    """Tekil kişisel alan dolu ve aktörün maskesinde gizliyse 403 (tekillik denetiminden önce)."""
    adaylar = [a for a in yazilan_hassas_alanlar(data) if a.ad in TEKIL_KISISEL_ALANLAR]
    if not adaylar or data.tc_no is None:
        return
    kumeler = await kumeleri_coz(session, user, request)
    ihlal = sorted({a.ad for a in adaylar if gizli_mi(a, kumeler.varsayilan)})
    if ihlal:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"{_YAZMA_REDDI}: {', '.join(ihlal)}"
        )

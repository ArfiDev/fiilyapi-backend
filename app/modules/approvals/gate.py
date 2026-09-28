"""OK-1C — zincir adiminin onay rolu, MODUL KAPISINI IKAME EDER.

## Neden bir kapi daha var

OK-1A onay zinciri motorunu canliya aldi ama zincir FIILEN ISLETILEMIYORDU:
bir adim, adini tasiyan SISTEM rolüyle gecilemiyordu (`progress_payments`
satirinda `site_chief` **draft**, `procurement` satirinda `accounting`
**none**; uclarin ikisi de `approve` ister). Yani zincirin TANIMLADIGI imzaci
ucdan iceri giremiyordu.

Kullanici karari (2026-08-22): **bir kullanici o adimin onay rolunu tasiyorsa,
modul seviyesi yetmese bile O ADIMI onaylayabilir.**

## Genisleme DARDIR

Kapi yalnizca `/approve` ve `/reject` uclarina, yalnizca ADIMI BEKLEYEN evrakta
ve yalnizca zincirin SIRADAKI adiminda acilir. Ayni modulun baska hicbir ucu
(liste · detay · yazma · `submit` · `mark-paid` · `unapprove`) DEGISMEZ ve
modul seviyesi her ucta AYNEN korunur — ikame kapiyi GEVSETMEZ, ona bir YEDEK
dal ekler. Sinir `tests/modules/approvals/test_ok1c_dar_kapsam.py`de iki
katmanda (yapisal + davranissal) bekcilenir.

## `core/permissions.py` DEGISMEDI

`require_permission` 112 cagri tarafindan kullaniliyor; ikame oraya bir bayrak
olarak eklenseydi butun deponun kapi davranisi tek bir kosula bagli hâle
gelirdi. Bu dosya onu SARAR, degistirmez.
"""

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.permissions import require_permission
from app.modules.approvals import service
from app.modules.approvals.models import ApprovalDocumentType
from app.modules.users.models import User

__all__ = ["require_permission_or_chain_step"]


async def _zincir_adimi_ikame_ediyor(
    request: Request,
    user: User,
    session: AsyncSession,
    *,
    document_type: ApprovalDocumentType,
    document_id_param: str,
) -> bool:
    """Yoldaki evrak kimligini cozer ve ikame kosulunu sorar.

    🔴 FAIL-CLOSED IKI YERDE: kimlik UUID'ye cevrilemezse `False` doner (kapi
    yakaladigi 403'u aynen firlatir). FastAPI zaten sonra 422 verecekti ama
    KAPI ondan ONCE kosar — yani bugunku davranis, kimligi bozuk bir istekte de
    BIREBIR korunur.
    """
    ham = request.path_params.get(document_id_param)
    try:
        document_id = uuid.UUID(str(ham))
    except (TypeError, ValueError):
        return False
    return await service.chain_step_substitutes_permission(
        session, actor_id=user.id, document_type=document_type, document_id=document_id
    )


def require_permission_or_chain_step(
    module_key: str,
    min_level: AccessLevel,
    *,
    document_type: ApprovalDocumentType,
    document_id_param: str,
):
    """Modul kapisi GECMEZSE zincirin SIRADAKI adimina bakan uc kapisi.

    Sira baglayicidir:

    1. **Once bugunku modul kapisi.** Gecerse HEMEN donulur — ikame HIC kosmaz
       ve sicak yolda **+0 sorgu** olur (`test_MODUL_KAPISINDAN_GECEN_aktorun_
       sorgu_sayisi_ARTMAMALIDIR` bunu 27'de sabitler).
    2. Yakalanan istisna 403 DEGILSE aynen yeniden firlatilir; 401 gibi baska
       bir kapinin cevabi ikame tarafindan YUTULMAZ.
    3. 403 ise zincir sorusu sorulur; yanit `True` degilse **yakalanan 403 aynen
       firlatilir** — `detail` metni DEGISMEZ, cunku kullaniciya hangi katmanin
       durdurdugunu soylemek tek basina bir bilgidir. Sorunun kendisinin IKI
       dalli olmasinin (ve ikincisinin neden bir sizintiyi kapattiginin) gerekcesi
       `service.chain_step_substitutes_permission` docstring'indedir.

    🔴 **KAPANIS (CLOSURE) ZORUNLULUGU.** `require_permission(...)` cagrisi
    fabrikanin govdesinde ONDEN kurulup `_check` icinde yalniz hazir nesne
    kullanilsaydi, `module_key` ve `min_level` `_check`in SERBEST DEGISKENI
    OLMAZDI — ve dar kapsamin YAPISAL bekcisi kapilari tam olarak
    `__code__.co_freevars` uzerinden tanidigi icin bu kapiyi GOREMEZDI. Bu
    yuzden cagri bilerek `_check`in ICINDEDIR. Maliyeti istek basina bir kapanis
    nesnesi ayirmaktir (bir DB sorgusunun yaninda olculemez); karsiliginda
    "hangi uc hangi kapiyi tasiyor" sorusu rota tablosundan URETILEBILIR kalir
    ve iki kapi ASLA ayrisamaz.
    """

    async def _check(
        request: Request,
        user: Annotated[User, Depends(get_current_user)],
        session: DbSession,
    ) -> None:
        try:
            await require_permission(module_key, min_level).dependency(user=user, session=session)
        except HTTPException as modul_kapisi:
            if modul_kapisi.status_code != status.HTTP_403_FORBIDDEN:
                raise
            ikame = await _zincir_adimi_ikame_ediyor(
                request,
                user,
                session,
                document_type=document_type,
                document_id_param=document_id_param,
            )
            if not ikame:
                raise modul_kapisi from None

    return Depends(_check)

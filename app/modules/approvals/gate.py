"""OK-1C — zincir adiminin SAHIBI (projedeki rolu adim rolu olan kisi), MODUL KAPISINI IKAME EDER.

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
from app.core.gate_context import record_gate
from app.core.page_gate import pages_ok
from app.modules.approvals import service
from app.modules.approvals.models import ApprovalDocumentType
from app.modules.projects.context import request_project
from app.modules.users.models import User

__all__ = ["require_pages_or_chain_step"]


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


def require_pages_or_chain_step(
    page_keys: tuple[str, ...],
    *,
    module_key: str,
    document_type: ApprovalDocumentType,
    document_id_param: str,
):
    """Sayfa Onaylar kapısı GEÇMEZSE zincirin SIRADAKİ adımına bakan uç kapısı (IZN-B2).

    Eski `require_permission_or_chain_step(modül, approve)`in yerine: onay eylemi artık modül
    kapısından değil `page_keys` sayfalarının (+ikizlerinin) ONAYLAR bayrağından geçer
    (`core.page_gate.pages_ok`). `module_key` evrakın izin modülüdür ve YALNIZ kimlik olarak
    taşınır (OK-1C yapısal bekçileri kapanıştan okur); `min_level` sabit `approve`dir.

    Sıra baglayicidir:

    1. **Önce sayfa kapısı.** Geçerse HEMEN dönülür — ikame HİÇ koşmaz (+0 sorgu).
    2. Geçmezse zincir sorusu sorulur; yanıt `True` değilse bugünkü 403 AYNEN fırlatılır
       (`detail` metni DEĞİŞMEZ: hangi katmanın durdurduğu tek başına bir bilgidir).

    Kapsam DARDIR (OK-1C): yalnız `/approve` ve `/reject`; modülün başka ucu ikame edilmez.
    """
    min_level = AccessLevel.approve

    async def _check(
        request: Request,
        user: Annotated[User, Depends(get_current_user)],
        session: DbSession,
    ) -> None:
        _kimlik = (module_key, min_level)  # yapısal bekçilerin okuduğu kapanış değişkenleri
        # IZN-B3: proje bağlamı (yol/gövde) çözülürse O PROJEDEKİ rolle karar verilir ve geçen kapı
        # bağlama `pages_ok`un İÇİNDE yazılır (eskiden yazılmıyordu: başka projedeki ekip rolü ve
        # ana rol onay uçlarını açıyordu — KRİTİK bulgu).
        project_id = await request_project(session, request)
        if await pages_ok(session, user, page_keys, "approve", project_id=project_id):
            return
        ikame = await _zincir_adimi_ikame_ediyor(
            request,
            user,
            session,
            document_type=document_type,
            document_id_param=document_id_param,
        )
        if not ikame:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Bu işlem için yetkiniz yok"
            )
        # Zincir adımı sahibi: sayfa kapısı geçmedi ama kapı ÇALIŞTI (üyelik süzgeci yeterli).
        record_gate(session, ())

    return Depends(_check)

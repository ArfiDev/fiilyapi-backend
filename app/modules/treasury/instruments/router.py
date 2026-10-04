"""FIN-1 uclari (yedi) — E10 `Cek & Odeme` ekraninin tamami.

Kapi `treasury` iznidir (K9, seed'de HAZIR — 🔴 **matris DEGISMEDI, yeni izin
modulu ACILMADI, izin migration'i YOKTUR**). Seviye sirasi
`none < view < draft < request < approve < full < admin` (`app/core/access.py`):

| Uc | Yetki |
|---|---|
| `GET /financial-instruments` | `view` |
| `GET /financial-instruments/summary` | `view` |
| `POST /financial-instruments` | `full` |
| `GET /financial-instruments/{id}` | `view` |
| `PATCH /financial-instruments/{id}` | `full` |
| `POST /financial-instruments/{id}/status` | `full` |
| `DELETE /financial-instruments/{id}` | **`admin`** |

🔴 **DELETE neden `admin`, emir "full yazma uclari" derken:** repo kanonu
(`KURALLAR-BACKEND-SEFI.md` §8) **"silme yalniz `admin`"** der ve `full` silmeyi
KAPSAMAZ; kardes uc `DELETE /bank-accounts/{id}` de aynen boyledir. Cek/senet
MALI bir kayittir ve muhasebeci onu tek basina dusurememelidir. Emir bu ucu
ayrica adlandirmadigi icin kayitli kanon uygulandi (KURALLAR §2: "kayitli
emsal/kanon soruyu cozuyorsa UYGULA ve raporda bildir").

## 🔴 ROTA SIRASI (MK-2 dersi, `main.py:94-104`)

`/financial-instruments/summary` iki segmentli bir LITERAL yoldur ve
`/financial-instruments/{instrument_id}` (UUID) ile CAKISIR. FastAPI yollari
KAYIT SIRASINA gore esler — literal sonra kaydedilseydi `summary` bir UUID
sanilir ve **422**ye duserdi. Bu yuzden ASAGIDA once tanimlidir ve
`test_rota_sirasi_summary_UUID_SANILMAZ` bunu kilitler.

`GET` uclari `record_audit` CAGIRMAZ (WORKFLOW kurali — okumalar denetlenmez);
dort yazma ucunun her biri TEK denetim satiri yazar ve metin SERVIS katmaninda,
kayit degismeden/yok olmadan ONCE kurulur.

🔴 Yeni `AuditAction` uyesi ACILMADI (TB3/T3 kanonu): durum gecisi de
`update`tir, ayrim METINDEDIR.
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES, DELETE_403_YANITI
from app.core.permissions import require_pages, require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.treasury.instruments import derive, service
from app.modules.treasury.instruments.schemas import (
    FinancialInstrumentCreate,
    FinancialInstrumentListResponse,
    FinancialInstrumentResponse,
    FinancialInstrumentStatusChange,
    FinancialInstrumentSummaryResponse,
    FinancialInstrumentUpdate,
)
from app.modules.treasury.models import (
    FinancialInstrumentDirection,
    FinancialInstrumentKind,
    FinancialInstrumentStatus,
)
from app.modules.users.models import User

router = APIRouter(tags=["treasury"], responses=COMMON_ERROR_RESPONSES)

_VIEW = require_permission(service.PERMISSION_MODULE, AccessLevel.view)
_FULL = require_permission(service.PERMISSION_MODULE, AccessLevel.full)
#: IZN-B2: çek/senet durum değiştir = Çek & Ödeme sayfası ONAYLAR (eşik eski `full`).
_STATUS_APPROVE = require_pages(("mali.cek_odeme",), "approve")

# TB3 sayfalama standardi: varsayilan 50, tavan 200 — tavan asimi sessizce
# KIRPILMAZ, **422** doner (ST/SA/`invoicing`/`bank-accounts` ile birebir).
_LIMIT = Annotated[int, Query(ge=1, le=200)]
_OFFSET = Annotated[int, Query(ge=0)]

_NOT_FOUND = {404: {"description": "Çek/senet kaydı bulunamadı"}}
_YAZMA_YANITLARI = {
    404: {"description": "Kayıt ya da gövdedeki proje/banka hesabı bulunamadı"},
    422: {"description": "Gövde kuralı ihlali (vade keşide tarihinden önce, tutar ölçeği)"},
}


async def _audit(
    request: Request,
    session: AsyncSession,
    user: User,
    action: AuditAction,
    detail: str,
) -> None:
    """Denetim satiri (B5 deseni). Metin PARAMETREDIR, burada kurulmaz."""
    await record_audit(
        session,
        action=action,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


# --- Uc 1: liste ---


@router.get(
    "/financial-instruments",
    response_model=FinancialInstrumentListResponse,
    dependencies=[_VIEW],
)
async def list_financial_instruments_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    direction: FinancialInstrumentDirection | None = None,
    instrument_kind: FinancialInstrumentKind | None = None,
    status_filter: Annotated[FinancialInstrumentStatus | None, Query(alias="status")] = None,
    project_id: uuid.UUID | None = None,
    due_before: date | None = None,
    due_after: date | None = None,
    q: str | None = None,
    limit: _LIMIT = 50,
    offset: _OFFSET = 0,
) -> FinancialInstrumentListResponse:
    """E10:99-161 tablosu — `direction` sekmeleri (E10:94-95) ve `instrument_kind`
    (E10:96 `Senetler`) SUZGECTIR, ayri uc DEGIL.

    🔴 Yanit SAYFALIDIR (`limit`/`offset`/`total`): portfoy buyur ve sayfasiz
    liste bir sonraki turun borcu olurdu. `limit` varsayilan 50, tavan 200 —
    asim **422** (kirpma DEGIL).

    🔴 `status` bir SUZGECTIR ama "Vadede" DEGILDIR: rozet turevdir (K2) ve her
    satirda `is_due` alani olarak doner. "Bu ay vadeli olanlar" isteniyorsa yol
    `status=portfolio&due_before=<ay sonu>`tur.

    Kapsam suzgeci HER ZAMAN uygulanir ve `total`a DA yansir; projesiz (sirket
    geneli) cekler izni olan herkese gorunur (`scope_clause`in ucuncu hali).
    """
    return await service.list_instruments(
        session,
        user,
        direction=direction,
        instrument_kind=instrument_kind,
        status=status_filter,
        project_id=project_id,
        due_before=due_before,
        due_after=due_after,
        q=q,
        limit=limit,
        offset=offset,
    )


# --- Uc 2: olustur ---


@router.post(
    "/financial-instruments",
    response_model=FinancialInstrumentResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_YAZMA_YANITLARI,
    dependencies=[_FULL],
)
async def create_financial_instrument_endpoint(
    request: Request,
    data: FinancialInstrumentCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> FinancialInstrumentResponse:
    """E10:65 `+ Cek Ekle`.

    ⚠️ **Formun MOCKUP'I YOKTUR** — alan kumesi tablonun sutunlarindan
    (E10:104-110) ve emrin K1 tablosundan alindi, UYDURULMADI.

    * `status` govdede KABUL EDILMEZ (**422**, K7): yeni kayit HER ZAMAN
      `portfolio` dogar;
    * `due_date < issue_date` → **422**;
    * `amount` `0.005` gibi bir deger → **422** (sessizce yuvarlanmaz);
    * gorunmeyen `project_id` / var olmayan `bank_account_id` → **404**.
    """
    instrument, detail = await service.create_instrument(session, user, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return service.response_of(instrument, as_of=derive.as_of_today())


# --------------------------------------------------------------------------- #
# 🔴 LITERAL YOL — asagidaki `{instrument_id}` (UUID) rotasindan ONCE kaydedilir.
# FastAPI yollari KAYIT SIRASINA gore esler; sonra kaydedilseydi `summary` bir
# UUID sanilip 422'ye duserdi (MK-2 dersi). Eklenecek her yeni literal yol da
# bu satirin USTUNE gelir.
# --------------------------------------------------------------------------- #


@router.get(
    "/financial-instruments/summary",
    response_model=FinancialInstrumentSummaryResponse,
    dependencies=[_VIEW],
)
async def financial_instruments_summary_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> FinancialInstrumentSummaryResponse:
    """E10:69-90 — dort kart, DORDU DE TUREV (K8).

    🔴 **Kartlar ORTUSUR ve bu TANIMDIR:** portfoydeki bir cek ayni anda "bu ay
    vadeli"dir; mockup da 8 + 5 ≠ 3'u ayri sayar (E10:73,78,83). Kartlarin
    toplaminin portfoye esit olmasi BEKLENMEZ.

    "Bu ay" TAKVIM AYIDIR (ayin 1'i – son gunu), "bugunden 30 gun" DEGIL.
    `as_of` ECHO edilir: onsuz kartin hangi aya gore hesaplandigi dogrulanamaz.
    """
    return await service.build_summary(session, user)


# --- Uc 3: detay ---


@router.get(
    "/financial-instruments/{instrument_id}",
    response_model=FinancialInstrumentResponse,
    responses=_NOT_FOUND,
    dependencies=[_VIEW],
)
async def get_financial_instrument_endpoint(
    instrument_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> FinancialInstrumentResponse:
    """Tek kayit + turev `is_due`. Gorunmeyen kayit ile var OLMAYAN kayit AYNI
    404 govdesini dondurur (repo kanonu)."""
    instrument = await service.visible_instrument(session, user, instrument_id)
    return service.response_of(instrument, as_of=derive.as_of_today())


# --- Uc 4: PATCH ---


@router.patch(
    "/financial-instruments/{instrument_id}",
    response_model=FinancialInstrumentResponse,
    responses={**_YAZMA_YANITLARI, 409: {"description": "Portföyden çıkmış kayıtta yön/tür"}},
    dependencies=[_FULL],
)
async def update_financial_instrument_endpoint(
    request: Request,
    instrument_id: uuid.UUID,
    data: FinancialInstrumentUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> FinancialInstrumentResponse:
    """Kismi guncelleme; kayit DENETIMLERDEN ONCE kilitlenir (TOCTOU).

    🔴 **`status` govdede KABUL EDILMEZ (422, K7)** — durum degisiminin TEK yolu
    asagidaki `/status` ucudur. Ayni uca konsaydi invaryantin IKI YAZMA KAPISI
    dogar ve biri kilitsiz kalirdi (BOQ-SEC-B kanonu).
    """
    instrument, detail = await service.update_instrument(session, user, instrument_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return service.response_of(instrument, as_of=derive.as_of_today())


# --- Uc 5: durum gecisi ---


@router.post(
    "/financial-instruments/{instrument_id}/status",
    response_model=FinancialInstrumentResponse,
    responses={
        **_NOT_FOUND,
        409: {"description": "Geçersiz geçiş · terminal durum · yön uyuşmazlığı"},
    },
    dependencies=[_STATUS_APPROVE],
)
async def change_financial_instrument_status_endpoint(
    request: Request,
    instrument_id: uuid.UUID,
    data: FinancialInstrumentStatusChange,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> FinancialInstrumentResponse:
    """🔴 K7 — durumun TEK yazma kapisi (K2 tablosu burada koşar).

    Uc ayri **409** tanisi doner: terminal durum · yon uyusmazligi · gecersiz
    gecis. Ayni metni paylassalardi kullanicinin yapabilecegi sey gizlenirdi.

    Satir DENETIMLERDEN ONCE kilitlenir (EŞİK=KİLİT): kilitsiz olsaydi iki
    eszamanli istek AYNI `portfolio` degerini okur ve IKISI DE gecerdi.
    """
    instrument, detail = await service.change_status(session, user, instrument_id, data.status)
    await _audit(request, session, user, AuditAction.update, detail)
    return service.response_of(instrument, as_of=derive.as_of_today())


# --- Uc 6: DELETE ---


@router.delete(
    "/financial-instruments/{instrument_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **DELETE_403_YANITI,
        **_NOT_FOUND,
        409: {"description": "Yalnızca portföydeki ve ödemesiz çek/senet silinebilir"},
    },
    dependencies=[require_system_admin()],
)
async def delete_financial_instrument_endpoint(
    request: Request,
    instrument_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Çek/senet kaydını siler. YALNIZ Sistem Yöneticisi.

    Yalnız portföydeki kayıt silinir; terminal durumdaki ya da bağlı ödemesi olan evrak **409**
    (iş kuralı). Tahsil edilmiş bir çekin kaydı hiçbir koşulda silinemez.
    """
    detail = await service.delete_instrument(session, user, instrument_id)
    await _audit(request, session, user, AuditAction.delete, detail)

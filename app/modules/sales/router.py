"""Ünite satışı uçları — P8 T3 (spec §4).

`units/router.py` deseninin aynısı: uçlar İKİ ayrı kök altına dağılır — proje
bağlamlı uçlar `/projects/{project_id}/sales`, kimliği yukarı çözümleyen tekil
uçlar `/sales/{sale_id}` altındadır; bu yüzden router prefix TAŞIMAZ.

**BFF TUZAĞI (frontend dilimi için):** kök `sales`tir ve `customers` (T2) ile
BİRLİKTE `src/app/api/backend/[...path]/route.ts` `ALLOWED_ROOTS` listesine
eklenmelidir — eklenmezse modül YALNIZ CANLIDA 404 verir, jsdom testleri görmez.

Durum geçişleri (`activate` / `transfer-deed` / `cancel`), plan OKUMA ucu ve
`summary` T5'te eklendi; geçerli geçiş çiftleri TEK tablodadır
(`transitions.TRANSITIONS`) ve uçlar kendi durum kontrollerini YAZMAZ.

Ödeme planı uçları (T4) `installments.py` servisini çağırır; üçü de `sales:full`
ister (aşağıdaki `pay` gerekçesine bakınız).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import kapsam_kapisi, require_permission
from app.core.ratelimit import client_ip
from app.core.scoped_route import kapsam_rotasi, kapsamdan_oku
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.sales import installments, service, transitions
from app.modules.sales.schemas import (
    InstallmentPayInput,
    SaleCancelInput,
    SaleInstallmentResponse,
    SaleInstallmentsSave,
    SalePlanResponse,
    SalesSummaryResponse,
    UnitSaleCreate,
    UnitSaleListResponse,
    UnitSaleResponse,
    UnitSaleUpdate,
)
from app.modules.users.models import User

# 🔴 KAPSAM MASKESİ — İKİ PARÇA DA GEREKLİ (kullanıcı kararı 2026-09-19):
#    `route_class` dönen modeli maskeler, `dependencies` aktörün kapsamını
#    köprüye yazar. Biri eksikse maske SESSİZCE `all` görür ve hiçbir şey
#    gizlemez. Çifti `tests/core/test_kapsam_baglantisi.py` çakar.
router = APIRouter(
    tags=["sales"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=kapsam_rotasi("sales", kapsamdan_oku),
    dependencies=[kapsam_kapisi("sales")],
)

# Spec §8 S1 (kullanıcı kararı): satış yetkisi proje yetkisinden AYRILIR —
# `sales` kendi izin modülüdür (matris 19). Kapsam (`visible_projects`) yine
# `projects` üzerinden gelir: izin "yetki", `user_project_access` "kapsam"dır.
_VIEW = require_permission("sales", AccessLevel.view)
_FULL = require_permission("sales", AccessLevel.full)
# KALICI KARAR 2026-07-30: SİLME bir seviye yukarıdadır — `full` yazmayı
# kapsar, SİLMEYİ KAPSAMAZ (`app/core/access.py` §5.0).
_ADMIN = require_permission("sales", AccessLevel.admin)


async def _audit(
    request: Request, session: AsyncSession, user: User, action: AuditAction, detail: str
) -> None:
    """Denetim satırı (B5 deseni). Metin servis katmanından HAZIR gelir.

    Yalnız YAZMA uçları çağırır — okuma uçları denetim satırı ÜRETMEZ (P4 T7
    kuralı). `record_audit` commit etmez: satır asıl işlemle AYNI transaction'a
    girer, dolayısıyla reddedilen (409/422) bir istek denetim satırı bırakmaz.
    """
    await record_audit(
        session,
        action=action,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


@router.get(
    "/projects/{project_id}/sales", response_model=UnitSaleListResponse, dependencies=[_VIEW]
)
async def list_sales_endpoint(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UnitSaleListResponse:
    """S150-212. "Tahsil Edilen"/"Kalan" TÜREVDİR (`sale_installments`), kolon değil."""
    return await service.list_sales(session, user, project_id)


@router.post(
    "/projects/{project_id}/sales",
    response_model=UnitSaleResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL],
)
async def create_sale_endpoint(
    request: Request,
    project_id: uuid.UUID,
    data: UnitSaleCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UnitSaleResponse:
    """Üç kapı: ünite bu projeye ait olmalı (404) · `landowner` ünite satılamaz

    (422, spec §8 S3) · ünitede ikinci AÇIK kayıt olamaz (409).
    """
    sale, detail = await service.create_sale(session, user, project_id, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return sale


@router.get("/sales/{sale_id}", response_model=UnitSaleResponse, dependencies=[_VIEW])
async def get_sale_endpoint(
    sale_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UnitSaleResponse:
    """Kimlik YUKARI çözümlenir (satış → proje → görünürlük); görünmeyen projenin
    satışı 404 döner, 403 DEĞİL — üstelik var olmayanla AYNI gövdeyi verir."""
    return await service.get_sale(session, user, sale_id)


@router.patch("/sales/{sale_id}", response_model=UnitSaleResponse, dependencies=[_FULL])
async def update_sale_endpoint(
    request: Request,
    sale_id: uuid.UUID,
    data: UnitSaleUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UnitSaleResponse:
    """Durum geçişleri BU UÇTAN YAPILMAZ: `status` şemada yoktur, `activate` /
    `transfer-deed` / `cancel` uçları T5'in işidir."""
    sale, detail = await service.update_sale(session, user, sale_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return sale


@router.delete("/sales/{sale_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_ADMIN])
async def delete_sale_endpoint(
    request: Request,
    sale_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Spec §4: YALNIZ `reservation` silinir; `active`/`deed_transferred` 409 ile

    reddedilir ve iptal edilerek (T5 `cancel`) kapatılır. Kapı `_ADMIN`dir —
    `units`/`blocks` DELETE uçlarıyla tutarlı (kalıcı karar 2026-07-30). Yetki
    kapısı durum korkuluğundan ÖNCE çalışır: yetkisiz aktör 403 alır ve kaydın
    hangi durumda olduğunu ÖĞRENEMEZ.
    """
    detail = await service.delete_sale(session, user, sale_id)
    await _audit(request, session, user, AuditAction.delete, detail)


# --- Ödeme planı (T4; spec §4, mockup F99-F147) ---


@router.post(
    "/sales/{sale_id}/generate-plan", response_model=SalePlanResponse, dependencies=[_FULL]
)
async def generate_sale_plan_endpoint(
    request: Request,
    sale_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SalePlanResponse:
    """F100 "Plan Oluştur" — SUNUCU OTORİTESİ: satırlar satış kaydının plan
    sütunlarından (F103-106) üretilir, gövde ALINMAZ.

    Mevcut plan üzerine yazılır; tahsilatı olan plan 409 ile korunur.
    """
    plan, detail = await installments.generate_plan(session, user, sale_id)
    await _audit(request, session, user, AuditAction.create, detail)
    return plan


@router.put("/sales/{sale_id}/installments", response_model=SalePlanResponse, dependencies=[_FULL])
async def save_sale_installments_endpoint(
    request: Request,
    sale_id: uuid.UUID,
    data: SaleInstallmentsSave,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SalePlanResponse:
    """⚠️ **DEĞİŞTİRME** semantiği (`PUT /progress-payments/{id}/lines` ikizi):

    gövde planın TAMAMIDIR, gövdede geçmeyen satır SİLİNİR. `contracts`
    dağıtımının BİRLEŞTİRME ucuyla karıştırılmamalıdır.

    Σ `amount` = `sale_price` sunucuda doğrulanır (422); tahsilatı olan satır
    plandan çıkarılamaz (409).
    """
    plan, detail = await installments.save_installments(session, user, sale_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return plan


@router.post(
    "/sales/installments/{installment_id}/pay",
    response_model=SaleInstallmentResponse,
    dependencies=[_FULL],
)
async def pay_sale_installment_endpoint(
    request: Request,
    installment_id: uuid.UUID,
    data: InstallmentPayInput,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SaleInstallmentResponse:
    """§8 S2 tahsilatı — kısmi ödeme destekli; aşırı ödeme 422.

    ## Kapı neden `sales:full` (karar + gerekçe)

    Tahsilat bir YAZMA işlemidir ve `AccessLevel` merdiveninde yazmanın karşılığı
    `full`tür; `draft`/`request`/`approve` ara seviyeleri BELGE İŞ AKIŞLARI
    içindir (hakediş taslak→onay zinciri) ve tahsilatın böyle bir akışı YOKTUR.
    Ayrı bir "tahsilat" seviyesi AÇILMADI: `sales` izin modülü T1'de matrise
    (19.) tek satır olarak girdi, ikinci bir satır matrisin seed'i + migration'ı
    + testini birlikte değiştirmeyi gerektirirdi ve spec §4 böyle bir ayrım
    tarif etmiyor. `accounting` rolü bugün `sales=(view, finance)` olduğundan
    tahsilat İŞLEYEMEZ — bu bilinçli sonuçtur; değişmesi gerekiyorsa doğru yer
    izin matrisi seed'idir, uç kapısı değil.

    Kimlik YUKARI çözümlenir (taksit → satış → proje): görünmeyen taksit 404
    döner ve var olmayanla AYNI gövdeyi verir.
    """
    installment, detail = await installments.pay_installment(session, user, installment_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return installment


@router.get("/sales/{sale_id}/installments", response_model=SalePlanResponse, dependencies=[_VIEW])
async def get_sale_plan_endpoint(
    sale_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SalePlanResponse:
    """F110-147 plan tablosunu OKUR (T5'te eklendi).

    T4 planı yazan üç ucu kapattı ama okuyan bir uç bırakmamıştı; plan yalnız
    yazma yanıtlarında görülebiliyordu ve `GET /sales/{id}` satırları taşımıyor.
    Yanıt zarfı T4'ün `SalePlanResponse`udur — PAYLAŞILIR, kopyalanmaz.

    Kapı `_VIEW`dir: plan okumak bir YAZMA değildir, muhasebe (`sales=view`)
    tahsilat takibi için planı görebilmelidir.
    """
    return await installments.get_plan(session, user, sale_id)


# --- Durum geçişleri (T5; spec §4) ---
#
# Üçü de `sales:full` ister: geçiş kaydın durumunu DEĞİŞTİRİR, dolayısıyla bir
# yazma işlemidir (`pay` ucundaki gerekçenin aynısı). Geçerli çiftler TEK
# tablodadır (`transitions.TRANSITIONS`); uçlar durum kontrolü YAPMAZ.


async def _transition(
    request: Request,
    session: AsyncSession,
    user: User,
    sale_id: uuid.UUID,
    action: transitions.SaleAction,
    reason: str | None = None,
) -> UnitSaleResponse:
    """Üç ucun ORTAK gövdesi — geçiş + denetim satırı tek yerde.

    Denetim metni servis katmanından HAZIR gelir; reddedilen (409/404) istek
    denetim satırı BIRAKMAZ çünkü `perform` metni üretmeden önce fırlatır.
    """
    result = await transitions.perform(session, user, sale_id, action, reason=reason)
    await _audit(request, session, user, AuditAction.update, result.detail)
    return result.response


@router.post("/sales/{sale_id}/activate", response_model=UnitSaleResponse, dependencies=[_FULL])
async def activate_sale_endpoint(
    request: Request,
    sale_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UnitSaleResponse:
    """S56 "Rezerve" → S55 "Satılan": kapora sözleşmeye dönüştü.

    Ünite `reserved`tan `sold`a geçer (spec §3). `reservation` DIŞINDAKİ her
    durum 409'dur.
    """
    return await _transition(request, session, user, sale_id, transitions.SaleAction.activate)


@router.post(
    "/sales/{sale_id}/transfer-deed", response_model=UnitSaleResponse, dependencies=[_FULL]
)
async def transfer_sale_deed_endpoint(
    request: Request,
    sale_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UnitSaleResponse:
    """S166 "Tapu Devredildi" — TERMİNAL durum; ünite `sold` KALIR (spec §3)."""
    return await _transition(request, session, user, sale_id, transitions.SaleAction.transfer_deed)


@router.post("/sales/{sale_id}/cancel", response_model=UnitSaleResponse, dependencies=[_FULL])
async def cancel_sale_endpoint(
    request: Request,
    sale_id: uuid.UUID,
    data: SaleCancelInput,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UnitSaleResponse:
    """`reservation`/`active` → `cancelled`; ünite vitrine (`listed`) döner.

    GEREKÇE ZORUNLUDUR (422) ve `unit_sales`e DEĞİL denetim günlüğüne yazılır:
    iptal kaydın bir niteliği değil bir olaydır, kolon açılmaz (`transitions.py`).
    """
    return await _transition(
        request, session, user, sale_id, transitions.SaleAction.cancel, data.reason
    )


# --- Satış özeti (T5; mockup S55-59 + S218-234) ---


@router.get(
    "/projects/{project_id}/sales/summary",
    response_model=SalesSummaryResponse,
    dependencies=[_VIEW],
)
async def sales_summary_endpoint(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SalesSummaryResponse:
    """S55-59 KPI'ları + S218-234 "Yaklaşan Tahsilatlar (30 Gün)".

    Gecikme faizi (S223) ve rezervasyon "süresi doldu" (S188) YALNIZ GÖSTERİM
    türevidir (§8 S4/S5): ne tahakkuk yazılır ne otomatik iptal koşar.

    Okuma ucudur → denetim satırı ÜRETMEZ (P4 T7 kuralı).
    """
    return await service.sales_summary(session, user, project_id)

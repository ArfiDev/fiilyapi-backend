"""Tedarikçi + satın alma talebi uçları (SA T2) — spec §4'ün ilk iki satırı.

Kapı `procurement` iznidir (spec §2, seed'de HAZIR — matris DEĞİŞMEDİ). Seviye
sırası `none < view < draft < request < approve < full < admin`
(`app/core/access.py`) ve üç kapı buradan çıkar:

| Uç | Yetki |
|---|---|
| `GET /suppliers` | `view` |
| `POST /suppliers` | `full` |
| `GET /suppliers/{id}` | `view` |
| `PATCH /suppliers/{id}` | `full` |
| `GET /purchase-requests` | `view` |
| `POST /purchase-requests` | `request` |
| `GET /purchase-requests/{id}` | `view` |
| `PATCH /purchase-requests/{id}` | `request` |
| `DELETE /purchase-requests/{id}` | `request` + `can_delete` |

**Neden tedarikçi `full`, talep `request`:** matriste şantiye şefi ve saha
mühendisi `request` seviyesindedir (`roles/seed_data.py`). Talebi sahadan açan
onlardır; tedarikçi KATALOĞU ise satınalmanın işidir — şef katalog kaydı
açamamalıdır. Tek bir kapı seçilseydi ya şef talep açamaz ya da herkes
tedarikçi ekleyebilirdi.

**DELETE kapısı `_ADMIN` DEĞİLDİR** ve bu bilinçli bir istisnadır
(`contracts/router.py.delete_subcontractor_contract_endpoint` emsali): saf
`_ADMIN` kapısı şefin KENDİ taslağını silmesini de engellerdi ve spec §5.0'ın
taslak istisnasını uçta anlamsız bırakırdı. Kesin kararı servisteki
`can_delete` verir.

`GET` uçları `record_audit` ÇAĞIRMAZ (WORKFLOW kuralı — okumalar denetlenmez);
yazma uçlarının hepsi tek denetim satırı yazar ve metin servis katmanında,
kayıt değişmeden/yok olmadan ÖNCE kurulur.

Router prefix TAŞIMAZ: uçlar iki ayrı kök altına dağılır (`/suppliers` ve
`/purchase-requests`) — `inventory`/`documents` deseninin birebiri.

## AÇILMAYAN uçlar (spec §4, §5 — icat yasağı)

* **`DELETE /suppliers/{id}` YOKTUR** (spec §4): kullanımdan kaldırma
  `PATCH {"is_active": false}` iledir; teklifi/siparişi olan tedarikçi FK
  RESTRICT'i yüzünden zaten düşürülemez. Yol tanımlı olmadığı için FastAPI
  **405** döner ve bu bir BEKÇİ TESTİYLE kilitlidir (`test_silme_ucu_yoktur_405`).
* Onay zinciri MOTORU, tedarikçi puanı, e-posta/bildirim, **mal kabul ucu** ve
  kısmi teslim alanı HİÇBİR dilimde açılmaz (spec §5, kalıcı karar). Teslim
  damgasının tek yolu `purchase_order_id` taşıyan bir STOK GİRİŞİDİR (§7 S4).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import http
from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.mask_route import MaskeRotasi, maskele_baglamli
from app.core.openapi import COMMON_ERROR_RESPONSES, DELETE_403_YANITI
from app.core.page_gate import gate_flags
from app.core.permissions import require_pages, require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.core.slug import parse_ref
from app.core.tasima import assert_tasima_yetkisi
from app.modules.approvals.gate import require_pages_or_chain_step
from app.modules.approvals.models import ApprovalDocumentType
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.procurement import export, guards, service, summary, transitions
from app.modules.procurement.models import (
    PurchaseOrderStatus,
    PurchasePriority,
    PurchaseRequestStatus,
)
from app.modules.procurement.schemas import (
    PurchaseOrderCreate,
    PurchaseOrderListResponse,
    PurchaseOrderResponse,
    PurchaseOrderUpdate,
    PurchaseQuoteCreate,
    PurchaseQuoteListResponse,
    PurchaseQuoteResponse,
    PurchaseQuoteUpdate,
    PurchaseRequestCreate,
    PurchaseRequestListResponse,
    PurchaseRequestRejection,
    PurchaseRequestResponse,
    PurchaseRequestUpdate,
    PurchasingSummaryResponse,
    SupplierCard,
    SupplierCreate,
    SupplierListResponse,
    SupplierResponse,
    SupplierUpdate,
)
from app.modules.users.models import User

router = APIRouter(tags=["procurement"], responses=COMMON_ERROR_RESPONSES, route_class=MaskeRotasi)

_VIEW = require_permission(service.PERMISSION_MODULE, AccessLevel.view)
_REQUEST = require_permission(service.PERMISSION_MODULE, AccessLevel.request)
_FULL = require_permission(service.PERMISSION_MODULE, AccessLevel.full)
#: OK-1C — `approve`/`reject`in kapısı. `_APPROVE` sabiti SİLİNDİ: bu iki uç
#: onun TEK çağıranıydı ve ölü sabit bırakmak yanıltıcı olurdu. Modül seviyesi
#: AYNEN `approve`tır; seviye yetmediğinde zincirin SIRADAKİ adımının sahibi
#: (evrakın projesinde adım rolü olan kişi) onu İKAME EDER (`approvals/gate.py`).
#: Diğer 21 uç DEĞİŞMEDİ.
_CHAIN_APPROVE = require_pages_or_chain_step(
    ("stok.satinalma_talepleri",),
    module_key=service.PERMISSION_MODULE,
    document_type=ApprovalDocumentType.purchase_request,
    document_id_param="request_id",
)
#: IZN-B2: "Seç ve Sipariş Ver" = Teklif Karşılaştırma sayfası ONAYLAR (eşik eski `full`).
_QUOTE_SELECT = require_pages(("stok.teklif_karsilastirma",), "approve")

# TB3 sayfalama standardı: varsayılan 50, tavan 200 — tavan aşımı sessizce
# KIRPILMAZ, 422 döner (ST T2 ile birebir).
_LIMIT = Annotated[int, Query(ge=1, le=200)]
_OFFSET = Annotated[int, Query(ge=0)]

#: `audit`/`boq`/`units` ile AYNI sabit — dosya tipini uçlar arasında tek bir
#: metin belirler.
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def _audit(
    request: Request,
    session: AsyncSession,
    user: User,
    action: AuditAction,
    detail: str,
) -> None:
    """Denetim satırı (B5 deseni). Metin PARAMETREDİR, burada kurulmaz."""
    await record_audit(
        session,
        action=action,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


# --- Tedarikçi kataloğu (TED) ---


@router.get("/suppliers", response_model=SupplierListResponse, dependencies=[_VIEW])
async def list_suppliers_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    q: str | None = None,
    category: str | None = None,
    is_active: bool | None = None,
    limit: _LIMIT = 50,
    offset: _OFFSET = 0,
) -> SupplierListResponse:
    """TED kart ızgarasının veri kaynağı — süzgeçler AND'lidir.

    `q` AD ve KATEGORİ üzerinde kısmi arar (kart ikisini üst üste basar).
    `is_active` gönderilmezse PASİF tedarikçiler de listelenir: sessiz gizleme
    yok, ekran hangi kümeyi istediğini açıkça söyler.

    "Bu Yıl Toplam Sipariş" (TED 52) TÜREVDİR ve yalnız GÖRÜNEN projelerin
    siparişlerini kapsar (`service` gerekçesi).
    """
    items, total = await service.list_suppliers(
        session, user, q=q, category=category, is_active=is_active, limit=limit, offset=offset
    )
    return SupplierListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post(
    "/suppliers",
    response_model=SupplierResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL],
)
async def create_supplier_endpoint(
    request: Request,
    data: SupplierCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SupplierResponse:
    """Yeni tedarikçi kartı. Ad ve VKN tekilliği ZORLANMAZ (`service` gerekçesi).

    Yanıt "Bu Yıl Toplam Sipariş" TAŞIMAZ: yeni kartta değer zorunlu olarak
    sıfırdır ve onun için ayrıca bir toplama sorgusu koşturmak gereksizdir.
    """
    supplier, detail = await service.create_supplier(session, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return SupplierResponse.model_validate(supplier)


@router.get("/suppliers/{supplier_id}", response_model=SupplierCard, dependencies=[_VIEW])
async def get_supplier_endpoint(
    supplier_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SupplierCard:
    """Tekil kart — liste ile AYNI türetmeyi kullanır (iki ekran aynı tutarı
    göstersin diye ikinci bir formül yazılmaz)."""
    return await service.get_supplier_card(session, user, supplier_id)


@router.patch("/suppliers/{supplier_id}", response_model=SupplierResponse, dependencies=[_FULL])
async def update_supplier_endpoint(
    request: Request,
    supplier_id: uuid.UUID,
    data: SupplierUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> SupplierResponse:
    """Kısmi güncelleme. **Kullanımdan kaldırma da buradan geçer**
    (`{"is_active": false}`) — DELETE ucu yoktur (modül docstring'i)."""
    supplier, detail = await service.update_supplier(session, supplier_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return SupplierResponse.model_validate(supplier)


# --- Satın alma talebi (FST + SAT) ---


@router.get("/purchase-requests", response_model=PurchaseRequestListResponse, dependencies=[_VIEW])
async def list_purchase_requests_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    status_filter: Annotated[PurchaseRequestStatus | None, Query(alias="status")] = None,
    project_id: uuid.UUID | None = None,
    priority: PurchasePriority | None = None,
    q: str | None = None,
    limit: _LIMIT = 50,
    offset: _OFFSET = 0,
) -> PurchaseRequestListResponse:
    """SAT tablosunun veri kaynağı — süzgeçler AND'lidir.

    Kapsam süzgeci HER ZAMAN uygulanır: görünmeyen projenin talebi listede
    YOKTUR ve `total`a da girmez.

    Tahmini toplam ve kalem sayısı TÜREVDİR; satır KALEMLERİ TAŞIMAZ (şema
    gerekçesi). `limit` varsayılan 50, tavan 200 — aşım 422.
    """
    return await service.list_requests(
        session,
        user,
        status=status_filter,
        project_id=project_id,
        priority=priority,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/purchase-requests",
    response_model=PurchaseRequestResponse,
    status_code=status.HTTP_201_CREATED,
    responses={404: {"description": "Seçilen proje/şantiye/bölüm/malzeme bulunamadı"}},
    dependencies=[_REQUEST],
)
async def create_purchase_request_endpoint(
    request: Request,
    data: PurchaseRequestCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PurchaseRequestResponse:
    """FST formunun kaydı: başlık + kalemler TEK gövde, ATOMİK.

    **TASLAK-FARKINDALIKLI (P6 emsali):** zorunlu tek alan `project_id`dir;
    "Taslak Kaydet" yarım formu saklar. Sıkı doğrulama (`validation.
    submit_blockers`) `submit` ucundadır ve **T3'ündür**.

    * kalem XOR ihlali / miktar ≤ 0 / uzunluk tavanı → **422**
    * görünmeyen ya da olmayan proje/şantiye/bölüm/malzeme kartı → **404**

    Bozuk bir kalem varsa HİÇBİR ŞEY yazılmaz — ne başlık ne satır. Denetime
    TALEP BAŞINA TEK satır düşer. `request_no` sunucu üretir; gövdedeki numara
    ve durum YOK SAYILIR.
    """
    purchase_request, detail = await service.create_request(session, user, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return await service.build_request_detail(session, user, purchase_request)


@router.get(
    "/purchase-requests/{request_id}",
    response_model=PurchaseRequestResponse,
    dependencies=[_VIEW],
)
async def get_purchase_request_endpoint(
    request_id: str,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PurchaseRequestResponse:
    """FST detayı: başlık + kalemler + TÜREVLER (satır tutarı · tahmini toplam ·
    "Mevcut Stok"). Görünmeyen talep var olmayanla AYNI 404'ü alır.

    URL-4 — yol parametresi UUID **ya da** `request_no` kabul eder.
    🔴 YOL ADI `request_id` OLARAK KALIR (URL-2 kararı 1): şablon değişmezse
    üretilmiş istemcinin yol anahtarı da değişmez; değişen yalnız TİPTİR
    (`uuid` -> `string`).

    🔴 **KARDEŞ UÇLAR `uuid` KALIR** — sözleşme bu yol parametresi adı altında
    BİLEREK ASİMETRİKTİR: bu uç `string`, ama `…/{request_id}/quotes` ·
    `/select-and-order` ve tüm PATCH/DELETE/durum geçişleri `uuid` bekler.
    Anahtar YALNIZCA **link üretimi ve detay okuması** içindir; istemci kardeş
    çağrılarda gövdeden dönen `id`yi kullanır (URL-2 kararı 3)."""
    purchase_request = await service.visible_request(session, user, parse_ref(request_id))
    return await service.build_request_detail(session, user, purchase_request)


@router.patch(
    "/purchase-requests/{request_id}",
    response_model=PurchaseRequestResponse,
    responses={
        404: {"description": "Talep ya da seçilen proje/şantiye/bölüm/malzeme bulunamadı"},
        409: {"description": "Yalnızca taslak talep düzenlenebilir"},
    },
    dependencies=[_REQUEST],
)
async def update_purchase_request_endpoint(
    request: Request,
    request_id: uuid.UUID,
    data: PurchaseRequestUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PurchaseRequestResponse:
    """**YALNIZ taslakta** (spec §4); değilse **409** — yetki değil DURUM engeli.

    `lines` gönderilirse kalemler REPLACE edilir (tek atomik işlem); hiç
    göndermemek onlara DOKUNMAZ, boş liste hepsini SİLER.
    """
    await assert_tasima_yetkisi(
        session,
        request,
        user,
        gate_flags(service.PERMISSION_MODULE, AccessLevel.request),
        data,
        proje_yok=guards.REQUEST_PROJECT_INVALID,
        santiye_yok=guards.REQUEST_SITE_INVALID,
    )
    purchase_request = await service.visible_request_locked(session, user, request_id)
    purchase_request, detail = await service.update_request(session, user, purchase_request, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return await service.build_request_detail(session, user, purchase_request)


@router.delete(
    "/purchase-requests/{request_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**DELETE_403_YANITI, 409: {"description": "Yalnızca taslak talep silinebilir"}},
    dependencies=[require_system_admin()],
)
async def delete_purchase_request_endpoint(
    request: Request,
    request_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Satınalma talebini siler. YALNIZ Sistem Yöneticisi.

    YALNIZ taslak talep silinir; aksi hâlde **409** (iş kuralı, Sistem Yöneticisi'ni de durdurur).
    Talebi açan kişi kendi taslağını da silemez. Yanıttaki `can_delete` bayrağı bu kuraldan
    beslenir: yalnız Sistem Yöneticisi için `true`. Kalemler CASCADE ile gider.
    """
    purchase_request = await service.visible_request_locked(session, user, request_id)
    detail = await service.delete_request(session, user, purchase_request)
    await _audit(request, session, user, AuditAction.delete, detail)


# --- Onay akışı (T3) ---
#
# Kapılar: `submit` → `request` (talebi açan onu onaya da gönderir) ·
# `approve`/`reject` → `approve` (matriste PM'in seviyesi) · ₺500K üstü onay
# ek olarak `full` ister ve bu SERVİS katmanındadır (`transitions`), çünkü
# karar TUTARA bağlıdır ve bir route bağımlılığı tutarı bilemez.


@router.post(
    "/purchase-requests/{request_id}/submit",
    response_model=PurchaseRequestResponse,
    responses={
        404: {"description": "Talep bulunamadı"},
        409: {"description": "Talep bu işleme uygun durumda değil"},
        422: {"description": "Onaya göndermeyi engelleyen eksikler var"},
    },
    dependencies=[_REQUEST],
)
async def submit_purchase_request_endpoint(
    request: Request,
    request_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PurchaseRequestResponse:
    """`draft → pending_approval`. **SIKI doğrulama buradadır.**

    Taslak gevşektir (T2); onaya gönderirken `validation.submit_blockers`
    koşar ve engellerin HEPSİ tek 422'de döner — uzun bir formda eksikleri
    birer birer keşfettirmek kabul edilemez. Engel varsa durum DEĞİŞMEZ.
    """
    purchase_request = await service.visible_request_locked(session, user, request_id)
    purchase_request, detail = await service.perform_request_action(
        session, user, purchase_request, transitions.RequestAction.submit
    )
    await _audit(request, session, user, AuditAction.update, detail)
    return await service.build_request_detail(session, user, purchase_request)


@router.post(
    "/purchase-requests/{request_id}/approve",
    response_model=PurchaseRequestResponse,
    responses={
        403: {"description": "₺500K ve üstü talep üst seviye yetki ister"},
        404: {"description": "Talep bulunamadı"},
        409: {"description": "Talep bu işleme uygun durumda değil"},
    },
    dependencies=[_CHAIN_APPROVE],
)
async def approve_purchase_request_endpoint(
    request: Request,
    request_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PurchaseRequestResponse:
    """`pending_approval → quote_wait` (§3: onay ARA durum üretmez).

    🔴 **OK-1A T3: YOL ve KAPI KORUNDU, ANLAM DEĞİŞTİ.** Uç artık onay
    ZİNCİRİNİN sıradaki adımını ilerletir (mockup `Onay Kutusu.dc.html:150-178`:
    Satınalma → Proje Müdürü → Muhasebe, eşik üstünde + Patron). Talep ancak SON
    adımda `quote_wait`e geçer; ara adımlarda `pending_approval`da KALIR ve
    damga ATILMAZ. Zincirsiz ESKİ kayıtlarda bugünkü tek adımlı davranış sürer.

    **₺500K eşiği BURADA ve ONAY ANINDA koşar** (`transitions`): tutar o anki
    kalemlerden yeniden hesaplanır, kayıtta donmuş bir toplam okunmaz. Bu izin
    kapısı zincirle DEĞİŞMEDİ — iki katman birbirinin yedeğidir.
    `approved_by_user_id`/`approved_at` SON adımda damgalanır.
    """
    purchase_request = await service.visible_request_locked(session, user, request_id)
    purchase_request, detail = await service.perform_request_action(
        session, user, purchase_request, transitions.RequestAction.approve
    )
    await _audit(request, session, user, AuditAction.approve, detail)
    return await service.build_request_detail(session, user, purchase_request)


@router.post(
    "/purchase-requests/{request_id}/reject",
    response_model=PurchaseRequestResponse,
    responses={
        404: {"description": "Talep bulunamadı"},
        409: {"description": "Talep bu işleme uygun durumda değil"},
    },
    dependencies=[_CHAIN_APPROVE],
)
async def reject_purchase_request_endpoint(
    request: Request,
    request_id: uuid.UUID,
    data: PurchaseRequestRejection,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PurchaseRequestResponse:
    """`pending_approval → rejected`. **Gerekçe ZORUNLUDUR** (boş → 422).

    Gerekçe `rejection_reason` KOLONUNA yazılır (SAT ekranı onu kaydın üstünde
    gösterir) — `sale_cancelled`ın denetim-günlüğü kararının aksine burada
    kalıcı bir yer vardır. `rejected` TERMİNALDİR: diriltme geçişi yoktur,
    ihtiyaç sürüyorsa YENİ talep açılır.

    🔴 **OK-1A T3:** ret onay zincirini de BİTİRİR (`approval_chains` satırı
    DAMGALANIR, SİLİNMEZ — OKT-B1). Hakediş ikilisinden FARK: orada evrak `draft`a
    döner ve yeniden gönderilince YENİ bir zincir açılır; burada `rejected`
    TERMİNAL olduğu için ikinci bir zincir HİÇ açılmaz.
    """
    purchase_request = await service.visible_request_locked(session, user, request_id)
    purchase_request, detail = await service.perform_request_action(
        session, user, purchase_request, transitions.RequestAction.reject, reason=data.reason
    )
    await _audit(request, session, user, AuditAction.update, detail)
    return await service.build_request_detail(session, user, purchase_request)


# --- Teklif alt-kaynağı (TEK) ---
#
# Teklif TALEBİN ALTINDA yaşar; ayrı bir `/quotes/{id}` kökü AÇILMADI — açılsaydı
# teklif, talebin kapsam süzgecinden bağımsız bir giriş kapısı kazanırdı. Yol
# çaprazı (başka talebin teklifi) **404**tür.
#
# OKUMA `view`, YAZMA `full`: teklif toplamak ve pazarlık etmek satınalmanın
# işidir (tedarikçi kataloğuyla aynı kapı), şefin değil.


@router.get(
    "/purchase-requests/{request_id}/quotes",
    response_model=PurchaseQuoteListResponse,
    dependencies=[_VIEW],
)
async def list_quotes_endpoint(
    request_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PurchaseQuoteListResponse:
    """TEK karşılaştırma ekranı. **Okuma her durumda açıktır** — siparişe dönmüş
    bir talebin karşılaştırma geçmişi silinmez.

    Sayfalama YOKTUR (şema gerekçesi): teklifler bir talebin altındadır ve
    "EN İYİ FİYAT" rozeti eksik bir küme üzerinden hesaplanamaz.
    """
    purchase_request = await service.visible_request(session, user, request_id)
    return await service.list_quotes(session, purchase_request)


@router.get(
    "/purchase-requests/{request_id}/quotes/export.xlsx",
    dependencies=[_VIEW],
    response_class=Response,
    responses={
        200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Excel dosyası"},
        404: {"description": "Talep bulunamadı"},
    },
)
async def export_quote_comparison_endpoint(
    request_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> Response:
    """TEK 38 "Excel" düğmesi — karşılaştırmanın dışa aktarımı (§7 S5).

    Veri kaynağı EKRANLA AYNIDIR (`service.list_quotes`): "Toplam" sütunu
    tekrar hesaplanmaz, kartın taşıdığı değer yazılır. Sayfalama yoktur —
    teklifler bir talebin altındadır ve eksik bir küme karşılaştırma değildir.

    Yol `/export.xlsx`tir (`audit-log/export.xlsx` emsali): uzantı yolda
    durduğu için tarayıcı indirmesi ayrıca bir tahmine muhtaç kalmaz.
    """
    purchase_request = await service.visible_request(session, user, request_id)
    karsilastirma = await service.list_quotes(session, purchase_request)
    # IZN-B4d: maliyet gizliyse tutar hücreleri boş kalır (`maskele_baglamli`).
    kartlar = (await maskele_baglamli(karsilastirma)).items
    buffer = export.build_quote_comparison_workbook(kartlar)
    dosya_adi = f"{purchase_request.request_no}-{export.XLSX_FILENAME_SUFFIX}"
    return Response(
        content=buffer.getvalue(),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": http.content_disposition(dosya_adi)},
    )


@router.post(
    "/purchase-requests/{request_id}/quotes",
    response_model=PurchaseQuoteResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Talep ya da seçilen tedarikçi bulunamadı"},
        409: {"description": "Teklifler yalnızca teklif bekleyen talebe eklenebilir"},
    },
    dependencies=[_FULL],
)
async def create_quote_endpoint(
    request: Request,
    request_id: uuid.UUID,
    data: PurchaseQuoteCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PurchaseQuoteResponse:
    """Yalnız `quote_wait` (aksi **409**). `delivery_time` SERBEST metindir."""
    purchase_request = await service.visible_request_locked(session, user, request_id)
    quote, detail = await service.create_quote(session, purchase_request, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return quote


@router.patch(
    "/purchase-requests/{request_id}/quotes/{quote_id}",
    response_model=PurchaseQuoteResponse,
    responses={
        404: {"description": "Talep ya da teklif bulunamadı"},
        409: {"description": "Teklifler yalnızca teklif bekleyen talepte düzenlenebilir"},
    },
    dependencies=[_FULL],
)
async def update_quote_endpoint(
    request: Request,
    request_id: uuid.UUID,
    quote_id: uuid.UUID,
    data: PurchaseQuoteUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PurchaseQuoteResponse:
    """Kısmi güncelleme. Nakliye kuralı BİRLEŞİK değerlerde koşar (**422**):
    gövde yalnız `shipping_cost` taşısa bile DB'deki `shipping_included`
    hesaba katılır."""
    purchase_request = await service.visible_request_locked(session, user, request_id)
    quote, detail = await service.update_quote(session, purchase_request, quote_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return quote


@router.delete(
    "/purchase-requests/{request_id}/quotes/{quote_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **DELETE_403_YANITI,
        404: {"description": "Talep ya da teklif bulunamadı"},
        409: {"description": "Teklifler yalnızca teklif bekleyen talepte silinebilir"},
    },
    dependencies=[require_system_admin()],
)
async def delete_quote_endpoint(
    request: Request,
    request_id: uuid.UUID,
    quote_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Tedarikçi teklifini siler. YALNIZ Sistem Yöneticisi.

    Yanlış girilmiş teklif, talep hâlâ `quote_wait` iken silinir; aksi hâlde **409** (iş kuralı).
    """
    purchase_request = await service.visible_request_locked(session, user, request_id)
    detail = await service.delete_quote(session, purchase_request, quote_id)
    await _audit(request, session, user, AuditAction.delete, detail)


@router.post(
    "/purchase-requests/{request_id}/quotes/{quote_id}/select-and-order",
    response_model=PurchaseOrderResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Talep ya da teklif bulunamadı"},
        409: {"description": "Talep bu işleme uygun durumda değil"},
    },
    dependencies=[_QUOTE_SELECT],
)
async def select_and_order_endpoint(
    request: Request,
    request_id: uuid.UUID,
    quote_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PurchaseOrderResponse:
    """TEK'in "Sipariş Ver" düğmesi — **ATOMİK** üçlü (spec §3).

    Teklif işaretlenir (aynı talepteki diğerleri sıfırlanır) · sipariş üretilir
    (`SP-YYYY-NNNN`, tutar = teklif × talebin toplam miktarı + nakliye) · talep
    `ordered` olur. Ara adımda hata çıkarsa HİÇBİRİ kalmaz (servisteki açık
    SAVEPOINT). Denetime TEK satır düşer: kullanıcının yaptığı tek bir eylemdir.

    🔴 **SA-KILIT — kapı `visible_request_locked`TİR, `visible_request` DEĞİL.**
    Bu bir DURUM GEÇİŞİDİR (`quote_wait → ordered`) ve
    `transitions.apply_request_transition` kendi sözleşmesinde talep satırının
    ÇAĞIRAN tarafından ZATEN kilitlenmiş olmasını şart koşar — `submit`/
    `approve`/`reject` de aynı kapıdan geçer. Kilitsiz açıldığında CANLIDA tek
    talebe İKİ SİPARİŞ yazılıyordu ve tedarikçiye para İKİ KEZ taahhüt
    ediliyordu (ölçüldü: 2 sipariş / ₺500.000, beklenen ₺250.000).

    🔴 **BEKLEMEK YENİDEN-DOĞRULAMA DEĞİLDİR** — kusurun asıl dersi budur.
    İkinci istek kilitsiz kapıda da BEKLİYORDU (`apply_request_transition`
    içindeki `UPDATE`, birincinin satır kilidine çarpıyordu); ama geçiş matrisi
    o `UPDATE`ten ÖNCE, BELLEKTEKİ **bayat** `quote_wait` üzerinde koşmuştu.
    Bloke çözülünce karar YENİDEN sorulmuyordu. Kilit kapıya alınınca ikinci
    istek matristen ÖNCE bekler ve satırı TAZE okur (`populate_existing=True`),
    `(ordered, select-and-order)` çifti tabloda olmadığı için **409** alır.

    🔴 **KİLİT SIRASI** (deadlock): `purchase_requests` satırı HER ZAMAN İLK
    kilittir — `submit`/`approve`/`reject` yolundaki sıranın aynısı. Bu uçta
    sıra: talep satırı → teklif satırları → `pg_advisory_xact_lock(82502)`
    (numara). Ters sırada ilerleyen bir yol YOKTUR: doğrudan sipariş
    (`create_order`) danışma kilidini alır ama talep satırını HİÇ kilitlemez.

    Bekçiler `tests/modules/procurement/test_select_and_order_yarisi.py`dedir.
    """
    purchase_request = await service.visible_request_locked(session, user, request_id)
    order, detail = await service.select_and_order(session, user, purchase_request, quote_id)
    await _audit(request, session, user, AuditAction.create, detail)
    return order


# --- Sipariş (SIP) ---
#
# **`DELETE /purchase-orders/{id}` YOKTUR:** verilmiş bir sipariş bir OLAYDIR;
# geri alınması bir iptal akışı ister ve o akış hiçbir mockup'ta çizilmemiştir.
# Yol tanımlı olmadığı için FastAPI **405** döner (bekçi testiyle kilitli).


@router.get("/purchase-orders", response_model=PurchaseOrderListResponse, dependencies=[_VIEW])
async def list_orders_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    status_filter: Annotated[PurchaseOrderStatus | None, Query(alias="status")] = None,
    project_id: uuid.UUID | None = None,
    supplier_id: uuid.UUID | None = None,
    q: str | None = None,
    limit: _LIMIT = 50,
    offset: _OFFSET = 0,
) -> PurchaseOrderListResponse:
    """SIP tablosu — süzgeçler AND'lidir, kapsam HER ZAMAN uygulanır.

    `q` sipariş NUMARASI ve NOT üzerinde arar; tedarikçi için AYRI ve kesin bir
    süzgeç (`supplier_id`) vardır. `limit` varsayılan 50, tavan 200 — aşım 422.
    """
    return await service.list_orders(
        session,
        user,
        status=status_filter,
        project_id=project_id,
        supplier_id=supplier_id,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/purchase-orders",
    response_model=PurchaseOrderResponse,
    status_code=status.HTTP_201_CREATED,
    responses={404: {"description": "Seçilen proje ya da tedarikçi bulunamadı"}},
    dependencies=[_FULL],
)
async def create_order_endpoint(
    request: Request,
    data: PurchaseOrderCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PurchaseOrderResponse:
    """DOĞRUDAN (talepsiz) sipariş — §7 S3, SIP 35 "+ Sipariş Oluştur".

    Gövde `request_id` KABUL ETMEZ (şema gerekçesi): talebe bağlı siparişin tek
    yolu `select-and-order`dır. Numara sunucu üretir, durum `approved` başlar.
    """
    order, detail = await service.create_order(session, user, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return order


@router.get(
    "/purchase-orders/{order_id}", response_model=PurchaseOrderResponse, dependencies=[_VIEW]
)
async def get_order_endpoint(
    order_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PurchaseOrderResponse:
    """Görünmeyen projenin siparişi var olmayanla AYNI 404'ü alır."""
    return await service.get_order_detail(session, user, order_id)


@router.patch(
    "/purchase-orders/{order_id}",
    response_model=PurchaseOrderResponse,
    responses={
        404: {"description": "Sipariş bulunamadı"},
        409: {"description": "Siparişin durumu bu işleme uygun değil"},
    },
    dependencies=[_FULL],
)
async def update_order_endpoint(
    request: Request,
    order_id: uuid.UUID,
    data: PurchaseOrderUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PurchaseOrderResponse:
    """Tek meşru geçiş `approved → in_transit`tir.

    **`delivered`e ELLE geçilmez (409)** — o damgayı `purchase_order_id` taşıyan
    bir STOK GİRİŞİ atar (§7 S4, T4'ün zinciri). Elle açık olsaydı hiç mal
    girmemiş bir sipariş teslim görünür, stok bakiyesiyle satınalma kaydı
    sessizce ayrışırdı. `total_amount` da düzeltilemez (şema gerekçesi).

    🔴 **SA-KILIT T3 — kapı `visible_order_locked`TİR.** Bu da bir DURUM
    GEÇİŞİDİR ve `transitions.assert_order_transition` kararını BELLEKTEKİ
    `status` üzerinden verir. Kilitsiz açıkken ÖLÇÜLDÜ (2026-08-23): stok
    girişinin `delivered` damgasıyla eş zamanlı bir `PATCH {"status":
    "in_transit"}` geldiğinde sipariş **`in_transit`**, bağlı talep
    **`delivered`** kalıyordu — teslim damgası KAYBOLUYOR ve ikili ÇELİŞKİLİ
    oluyordu (mal girmiş ama sipariş "yolda"). Aynı kilitsizlik iki eş zamanlı
    `PATCH`in İKİSİNİ birden geçiriyordu; şimdi ikincisi **409** alır.
    """
    order = await service.visible_order_locked(session, user, order_id)
    order_response, detail = await service.update_order(session, order, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return order_response


# --- KPI şeridi (T4) ---
#
# Kök `purchasing`tir (spec §4) ve bu ÜÇÜNCÜ bir kök demektir (`/suppliers`,
# `/purchase-requests`, `/purchase-orders` yanında) — **BFF proxy izin
# listesine `purchasing` eklenmezse modül canlıda 404 verir** (kayıtlı tuzak).


@router.get("/purchasing/summary", response_model=PurchasingSummaryResponse, dependencies=[_VIEW])
async def purchasing_summary_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    project_id: uuid.UUID | None = None,
) -> PurchasingSummaryResponse:
    """SAT 69-86 + SIP 38-43 KPI'ları — alan gerekçeleri `summary.py`dedir.

    İki ekranın para kartı ("Bu Ay Sipariş" / "Bu Ay Toplam") TEK alandır ve
    "Aktif Siparişler" ST'nin "Bekleyen Sipariş" zarfıyla aynı kümeden gelir.

    `project_id` süzgeci kapsamı GENİŞLETMEZ, daraltır: görünmeyen bir proje
    kimliği verildiğinde sayaçlar sıfır kalır. Üç sorgu koşar (N+1 yok).
    """
    return await summary.build_summary(session, user, project_id=project_id)

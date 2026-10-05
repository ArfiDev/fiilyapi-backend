"""Hesap planı uçları (MU-1 T3a) — spec §7'nin 1-5 numaralı yolları.

Kapı **`accounting`** iznidir (spec §2/K8, seed'de HAZIR — 🔴 **matris
DEĞİŞMEDİ, yeni izin modülü AÇILMADI, izin migration'ı YOKTUR**). Seviye sırası
`none < view < draft < request < approve < full < admin` (`app/core/access.py`)
ve üç kapı buradan çıkar:

| Uç | Yetki |
|---|---|
| `GET /chart-of-accounts` | `view` |
| `GET /chart-of-accounts/export.xlsx` | `view` |
| `POST /chart-of-accounts` | `full` |
| `GET /chart-of-accounts/{id}` | `view` |
| `PATCH /chart-of-accounts/{id}` | `full` |
| `DELETE /chart-of-accounts/{id}` | **`admin`** |

**Neden `DELETE` düz `admin`:** `full` silmeyi KAPSAMAZ (repo kanonu). Hesap
planı MALİ bir katalogtur ve tüm yevmiyenin taşıyıcısıdır; muhasebeci bir hesabı
tek başına düşürememelidir (`invoicing`/`treasury` deseni). Normal kullanımdan
kaldırma yolu `PATCH {"is_active": false}`tur (HP:62 `Durum` sütunu).

`GET` uçları `record_audit` ÇAĞIRMAZ (WORKFLOW kuralı — okumalar denetlenmez);
üç yazma ucunun her biri TEK denetim satırı yazar ve metin servis katmanında,
kayıt değişmeden/yok olmadan ÖNCE kurulur. 🔴 Yeni `AuditAction` üyesi AÇILMAZ.

## AÇILMAYAN uçlar (spec §9, icat yasağı)

Yevmiye uçları (`/journal-entries…`, `/journal`) **T3b'nindir** ve AYRI bir
router'dadır. Mizan (HP:33), KDV beyanı (HP:36) ve mali tablolar (HP:38)
`reports_router`dadır; banka mutabakatı (HP:34) hâlâ AÇILMAMIŞTIR.

`Excel` (HP:49) EXPORT-XLSX'te açıldı: `GET /chart-of-accounts/export.xlsx`,
liste ucuyla AYNI servis çağrısından beslenir ve `limit=None` ile kümenin
TAMAMINI yazar.

## Rota sırası

🔴 `/chart-of-accounts/{account_id}` (UUID) ile çakışan iki-segmentli LİTERAL
yol BU DİLİMDE YOKTUR. Buna karşılık bu router'a ileride
`/chart-of-accounts/<literal>` biçiminde bir yol eklenirse UUID rotasının
ÜSTÜNE konmalıdır — FastAPI yolları KAYIT SIRASINA göre eşler ve sonra
kaydedilen literal bir UUID sanılıp 422'ye düşer (MK-2 dersi, `main.py:94-104`).
Ayrılmış yer aşağıda işaretlidir; bekçi testi
`test_rota_sirasi_chart_of_accounts_literal_yol_YOKTUR`.
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
from app.core.permissions import require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.modules.accounting import accounts_service, export, guards
from app.modules.accounting.models import ChartAccountType
from app.modules.accounting.schemas import (
    ChartAccountCreate,
    ChartAccountListResponse,
    ChartAccountResponse,
    ChartAccountUpdate,
)
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.users.models import User

router = APIRouter(tags=["accounting"], responses=COMMON_ERROR_RESPONSES, route_class=MaskeRotasi)

_VIEW = require_permission(guards.PERMISSION_MODULE, AccessLevel.view)
_FULL = require_permission(guards.PERMISSION_MODULE, AccessLevel.full)

# K7 sayfalama standardı: varsayılan 50, tavan 200 — tavan aşımı sessizce
# KIRPILMAZ, 422 döner (ST/SA/`invoicing`/`treasury` ile birebir).
_LIMIT = Annotated[int, Query(ge=1, le=200)]
_OFFSET = Annotated[int, Query(ge=0)]

_NOT_FOUND = {404: {"description": "Hesap bulunamadı"}}
_YAZMA_YANITLARI = {
    409: {"description": "Kod zaten kayıtlı ya da üst hesabın yevmiye kaydı var"},
    422: {"description": "Kod biçimi geçersiz ya da türev alan gönderildi"},
}


async def _audit(
    request: Request,
    session: AsyncSession,
    user: User,
    action: AuditAction,
    detail: str,
) -> None:
    """Denetim satırı (B5 deseni). Metin PARAMETREDİR, burada kurulmaz.

    🔴 Yeni `AuditAction` üyesi AÇILMADI (TB3/T3 kanonu): `action` gerçek bir
    Postgres enum tipidir ve yeni üye migration ister. Ayrım METİNDEDİR.
    """
    await record_audit(
        session,
        action=action,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


# --- Uç 1: liste ---


@router.get("/chart-of-accounts", response_model=ChartAccountListResponse, dependencies=[_VIEW])
async def list_chart_accounts_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    q: str | None = None,
    account_type: ChartAccountType | None = None,
    is_active: bool | None = None,
    limit: _LIMIT = 50,
    offset: _OFFSET = 0,
) -> ChartAccountListResponse:
    """HP:58-62 tablosu — her satır **TÜRETİLMİŞ `balance`** + `class_code` + `level` taşır.

    Süzgeçler HP'nin filtre çubuğundan gelir: `q` (HP:47, **kod ve ad** üzerinde;
    LIKE jokeri KAÇIRILIR) · `account_type` (HP:60 `Tür`) · `is_active` (HP:62
    `Durum`). Sıralama `code ASC`tir ve hiyerarşiyi kendiliğinden üretir.

    🔴 `Tür` ile `Durum` AYRI ŞEYLERDİR (R3): ikisi de Türkçe'de "aktif" okunur
    ama biri dört üyeli kapalı bir enum, öteki boolean bir kaldırma bayrağıdır.

    `limit` varsayılan 50, tavan 200 — aşım **422** (kırpma DEĞİL).

    🔴 **Proje/şantiye kapsam süzgeci YOKTUR (spec §3):** hesap planı şirket
    geneli bir katalogtur, erişimi `accounting` izni denetler.
    """
    return await accounts_service.list_accounts(
        session, q=q, account_type=account_type, is_active=is_active, limit=limit, offset=offset
    )


# --- Uç 2: oluştur ---


@router.post(
    "/chart-of-accounts",
    response_model=ChartAccountResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_YAZMA_YANITLARI,
    dependencies=[_FULL],
)
async def create_chart_account_endpoint(
    request: Request,
    data: ChartAccountCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ChartAccountResponse:
    """Yeni hesap (HP:50 `+ Hesap Ekle`).

    * `code` KAPALI biçim kümesindedir: `NN` · `NNN` · `NNN.NN` — üçüncü kırılım
      ve sınıf kodu **422**dir (DB CHECK'i son savunma)
    * `account_type` KAPALI kümedir: Aktif/Pasif/Gelir/Gider (K5)
    * aynı kod **409**
    * 🔴 **K-Ş3:** fiş satırı OLAN bir hesabın altına çocuk açmak **409**
    * `balance`/`class_code`/`level` gövdeden GELEMEZ (**422**): türevdirler
    """
    account, detail = await accounts_service.create_account(session, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return await accounts_service.get_account_response(session, account.id)


# --------------------------------------------------------------------------- #
# 🔴 İKİ SEGMENTLİ LİTERAL YOLLAR — aşağıdaki UUID rotasının ÜSTÜNDE durmak
# ZORUNDADIRLAR (MK-2 dersi): FastAPI yolları KAYIT SIRASINA göre eşler ve
# sonra kaydedilen `export.xlsx` bir `account_id` sanılıp **422**ye düşerdi.
# --------------------------------------------------------------------------- #


@router.get(
    "/chart-of-accounts/export.xlsx",
    dependencies=[_VIEW],
    response_class=Response,
    responses={200: {"content": {export.XLSX_MEDIA_TYPE: {}}, "description": "Excel dosyasi"}},
)
async def export_chart_accounts_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    q: str | None = None,
    account_type: ChartAccountType | None = None,
    is_active: bool | None = None,
) -> Response:
    """Hesap planının Excel çıktısı (HP:49 `Excel`).

    🔴 Zarf **`accounts_service.list_accounts` ile — LİSTE UCUYLA AYNI
    ÇAĞRIDAN** gelir ve ÜÇ süzgeç de (HP:47 arama · HP:60 `Tür` · HP:62
    `Durum`) birebir aynı adlarla geçer: kullanıcı ekranda ne süzdüyse dosyada
    onu bulur. İkinci bir sorgu/süzgeç yolu AÇILMAZ — ayrı yazılsalardı
    dosyanın süzgeci ile ekranınki zamanla ayrışır ve **süzgeç dışı satırların
    dosyaya sızması** (veri kaçağı) hiçbir yerden görülmezdi.

    🔴 **`limit`/`offset` YOKTUR: eşleşen TÜM hesaplar yazılır** (`limit=None`).
    Sessiz kırpma yapılmaz — 200 hesabı aşan bir hesap planında dosya, eksik
    olduğu HİÇBİR YERDEN anlaşılamayan bir belge olurdu. Liste ucunun kendi
    tavanı (200, aşımı **422**) DEĞİŞMEDİ.

    Kapsam süzgeci YOKTUR (spec §3): hesap planı şirket geneli bir katalogdur.

    Okuma ucudur: `record_audit` ÇAĞIRMAZ ve `Request` parametresi bile ALMAZ.
    """
    accounts = await accounts_service.list_accounts(
        session, q=q, account_type=account_type, is_active=is_active, limit=None, offset=0
    )
    return Response(
        content=export.build_chart_of_accounts_workbook(
            await maskele_baglamli(accounts)
        ).getvalue(),
        media_type=export.XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": http.content_disposition(export.chart_of_accounts_filename())
        },
    )


# --------------------------------------------------------------------------- #
# 🔴 AYRILMIŞ YER — başka iki segmentli LİTERAL `/chart-of-accounts/<literal>` yolları.
#
# Aşağıdaki `/chart-of-accounts/{account_id}` (UUID) rotasıyla ÇAKIŞIRLAR:
# FastAPI yolları KAYIT SIRASINA göre eşler, sonra kaydedilen literal bir UUID
# sanılıp 422'ye düşer (MK-2 dersi). Bugün YALNIZ `export.xlsx` vardır ve
# yukarıdadır; eklenecek olan HER yeni literal de bu satırın ÜSTÜNE gelir.
# --------------------------------------------------------------------------- #


# --- Uç 3: detay ---


@router.get(
    "/chart-of-accounts/{account_id}",
    response_model=ChartAccountResponse,
    responses=_NOT_FOUND,
    dependencies=[_VIEW],
)
async def get_chart_account_endpoint(
    account_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ChartAccountResponse:
    """Tek hesap + türetilmiş bakiye. Bakiye liste ucuyla AYNI kaynaktan gelir."""
    return await accounts_service.get_account_response(session, account_id)


# --- Uç 4: PATCH ---


@router.patch(
    "/chart-of-accounts/{account_id}",
    response_model=ChartAccountResponse,
    responses={**_NOT_FOUND, **_YAZMA_YANITLARI},
    dependencies=[_FULL],
)
async def update_chart_account_endpoint(
    request: Request,
    account_id: uuid.UUID,
    data: ChartAccountUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> ChartAccountResponse:
    """Kısmi güncelleme; kayıt DENETİMLERDEN ÖNCE kilitlenir (TOCTOU).

    🔴 `code` değişimi yalnız **hiç fiş satırı olmayan** hesapta serbesttir
    (**409**): aksi hâlde tüm geçmiş yevmiye sessizce kayardı. Aynı kodu geri
    göndermek serbesttir — kapı DEĞİŞİME bakar.

    Kullanımdan kaldırma yolu budur: `{"is_active": false}`.
    """
    account, detail = await accounts_service.update_account(session, account_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return await accounts_service.get_account_response(session, account.id)


# --- Uç 5: DELETE ---


@router.delete(
    "/chart-of-accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **DELETE_403_YANITI,
        **_NOT_FOUND,
        409: {"description": "Hesaba bağlı yevmiye satırı ya da alt hesap var"},
    },
    dependencies=[require_system_admin()],
)
async def delete_chart_account_endpoint(
    request: Request,
    account_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Hesabı siler. YALNIZ Sistem Yöneticisi.

    Fiş satırı (`journal_lines`) ya da alt hesabı olan hesap silinmez: **409** (iş kuralı,
    Sistem Yöneticisi'ni de durdurur). Yanıt gövdesizdir.
    """
    detail = await accounts_service.delete_account(session, account_id)
    await _audit(request, session, user, AuditAction.delete, detail)

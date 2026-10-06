"""Muhasebe dönemi uçları (MU-2 T3) — liste · kapat · aç.

Kapı **`accounting`** iznidir (`guards.PERMISSION_MODULE`) — 🔴 **yeni izin
modülü AÇILMADI, matris DEĞİŞMEDİ, izin migration'ı YOKTUR.** Seviye sırası
`none < view < draft < request < approve < full < admin` (`app/core/access.py`):

| Uç | Yetki |
|---|---|
| `GET /accounting-periods` | `view` |
| `POST /accounting-periods/{year}/{month}/close` | `full` |
| `POST /accounting-periods/{year}/{month}/reopen` | **`admin`** |

**Neden `reopen` düz `admin`:** kapanmış bir dönemi yeniden açmak MALİ İZİ geri
sarar — kapalıyken reddedilen her yazma o an mümkün hâle gelir.
`DELETE /journal-entries/{id}`in gerekçesinin birebiri: kaydın sahibi onu giren
muhasebeci değil ŞİRKETTİR. `full` (muhasebe) kapatabilir ama kendi kapattığını
tek başına GERİ ALAMAZ.

**409, 403 DEĞİL:** kullanıcının yetkisi VARDIR; engelleyen şey DÖNEMİN
durumudur (zaten kapalı / zaten açık / içinde taslak fiş var).

## 🔴 Yol parametresi aralığı ŞEMADA durur, CHECK'te DEĞİL

`year` `ge=2000 le=2100`, `month` `ge=1 le=12` — `ck_accounting_periods_*_range`
kısıtlarıyla BİREBİR. Aralık dışı değer `Path(...)`ta **422** alır; modelin
CHECK'ine düşürülseydi UPSERT `IntegrityError` fırlatır ve kullanıcıya ayrımsız
bir 409 (ya da 500) giderdi — `month=13`ün neden reddedildiği hiç öğrenilemezdi.

## ROTA SIRASI

`/accounting-periods` kökü BAŞKA HİÇBİR router'ın yoluyla çakışmaz (`main.py`
yorumunda `grep` ile doğrulanmıştır). Router'ın KENDİ içinde de çakışma yoktur:
`/accounting-periods` tek segmentlidir, ötekiler ÜÇ segmentlidir ve son segment
LİTERALDİR (`close`/`reopen`) — UUID sanılabilecek bir yol yoktur.

`GET` uçları `record_audit` ÇAĞIRMAZ (WORKFLOW kuralı — okumalar denetlenmez).
🔴 Yeni `AuditAction` üyesi AÇILMADI: kapatma `approve`, açma `update`; ayrım
`messages.accounting_period_*` metnindedir.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_page, require_permission
from app.core.ratelimit import client_ip
from app.modules.accounting import guards, periods_service
from app.modules.accounting.periods_schemas import (
    AccountingPeriodListResponse,
    AccountingPeriodResponse,
)
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.users.models import User

router = APIRouter(
    prefix="/accounting-periods",
    tags=["accounting"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=MaskeRotasi,
)

_VIEW = require_permission(guards.PERMISSION_MODULE, AccessLevel.view)
# IZN-B5b madde 4: dönem kapatma = `mali.donem_kapanisi` Düzenler'i (eskiden `accounting:full`).
_KAPAT = require_page("mali.donem_kapanisi", "edit")
#: IZN-B2 §2.4: "dönemi yeniden aç" = Dönem Kapanışı sayfası ONAYLAR (eşik eski `admin` ile aynı).
_ADMIN = require_page("mali.donem_kapanisi", "approve")

# K7 sayfalama standardı: varsayılan 50, tavan 200 — tavan aşımı sessizce
# KIRPILMAZ, 422 döner (`router.py` ve ST/SA/`invoicing`/`treasury` ile birebir).
_LIMIT = Annotated[int, Query(ge=1, le=200)]
_OFFSET = Annotated[int, Query(ge=0)]

# 🔴 Bantlar `ck_accounting_periods_year_range` / `_month_range` ile BİREBİR —
# gerekçe modül docstring'indedir.
_YEAR_PATH = Annotated[int, Path(ge=2000, le=2100)]
_MONTH_PATH = Annotated[int, Path(ge=1, le=12)]
_YEAR_QUERY = Annotated[int | None, Query(ge=2000, le=2100)]

_DONEM_CAKISMASI = {
    409: {"description": "Dönem bu işlem için uygun durumda değil (zaten kapalı/açık · taslak fiş)"}
}


@router.get("", response_model=AccountingPeriodListResponse, dependencies=[_VIEW])
async def list_accounting_periods_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: _YEAR_QUERY = None,
    limit: _LIMIT = 50,
    offset: _OFFSET = 0,
) -> AccountingPeriodListResponse:
    """Dönem kayıtları — 🔴 sıralama **`year DESC, month DESC`** (en yeni başta).

    Yön fiş listesinin `entry_date DESC` kanonuyla aynıdır: kullanıcının ilgisi
    daima en son döneme yakındır; artan sıra, on yıl sonra ilk sayfayı 2026'da
    bırakırdı. Sıralama BELİRLEYİCİDİR — `(year, month)` UNIQUE'tir, ikinci bir
    ölçüte ihtiyaç yoktur.

    🔴 **Liste EKSİK GÖRÜNEBİLİR ve bu doğrudur:** dönem kaydı proaktif açılmaz
    (YAGNI), yalnız bir kapanış ya da bir YAZMA işlemi ona dokunduğunda doğar.
    Listede olmayan dönem **AÇIKTIR**; "kapalı" bilgisi her zaman bir SATIRDIR.

    `limit` varsayılan 50, tavan 200 — aşım **422** (kırpma DEĞİL).
    """
    return await periods_service.list_periods(session, year=year, limit=limit, offset=offset)


@router.post(
    "/{year}/{month}/close",
    response_model=AccountingPeriodResponse,
    responses=_DONEM_CAKISMASI,
    dependencies=[_KAPAT],
)
async def close_accounting_period_endpoint(
    request: Request,
    year: _YEAR_PATH,
    month: _MONTH_PATH,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> AccountingPeriodResponse:
    """Dönemi kapatır — kapandıktan sonra o aya HİÇBİR fiş yazılamaz.

    🔴 **EŞİK = KİLİT:** dönem satırı UPSERT edilip `FOR UPDATE` ile kilitlenir
    ve bu TÜM denetimlerden ÖNCE olur (ayrıntı `periods_service.py` modül
    docstring'i). İki eşzamanlı istekten yalnız biri geçer, öteki **409** alır ve
    ortada TEK satır kalır.

    **409 iki sebepten:** dönem zaten kapalı · dönemde `draft` fiş var.
    `posted`/`reversed` fiş ENGEL DEĞİLDİR — kapanışın amacı onları DONDURMAKTIR.

    Denetim satırı `AuditAction.approve` ile yazılır (yeni üye AÇILMADI).
    """
    period, detail = await periods_service.close_period(session, user, year, month)
    await record_audit(
        session,
        action=AuditAction.approve,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return AccountingPeriodResponse.model_validate(period)


@router.post(
    "/{year}/{month}/reopen",
    response_model=AccountingPeriodResponse,
    responses=_DONEM_CAKISMASI,
    dependencies=[_ADMIN],
)
async def reopen_accounting_period_endpoint(
    request: Request,
    year: _YEAR_PATH,
    month: _MONTH_PATH,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> AccountingPeriodResponse:
    """Dönemi yeniden açar — **YALNIZ `admin`** (gerekçe modül docstring'inde).

    Kilit sırası `close` ile BİREBİR AYNIDIR. Kapanış damgası SÖKÜLÜR:
    `ck_accounting_periods_closed_stamp` `open` bir dönemde damganın NULL
    olmasını şart koşar — bırakılsaydı mali iz yalan söylerdi.

    **409:** dönem zaten açık. Kaydı hiç olmayan dönem de AÇIKTIR (kayıt yoksa
    dönem açık sayılır) ve aynı 409'a düşer; satır yine de UPSERT ile doğar,
    çünkü kilitlenecek bir satır olmadan iki eşzamanlı istek serileşemezdi.

    Denetim satırı `AuditAction.update` ile yazılır.
    """
    period, detail = await periods_service.reopen_period(session, user, year, month)
    await record_audit(
        session,
        action=AuditAction.update,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    return AccountingPeriodResponse.model_validate(period)

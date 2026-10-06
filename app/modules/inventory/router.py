"""Katalog + depo uçları (ST T2) — spec §4'ün ilk iki satırı.

Kapı `inventory` iznidir (spec §7 S5, seed'de HAZIR — matris DEĞİŞMEDİ): okuma
`view`, yazma `full`, silme `admin`. Üç seviye üç ayrı bağımlılıktır ve BURADA
durur; servis katmanı yetkiye değil KAPSAMA (`visible_projects`) bakar.

| Uç | Yetki |
|---|---|
| `GET /stock/items` | `view` |
| `POST /stock/items` | `full` |
| `PATCH /stock/items/{id}` | `full` |
| `GET /warehouses` | `view` |
| `POST /warehouses` | `full` |
| `PATCH /warehouses/{id}` | `full` |
| `DELETE /warehouses/{id}` | `admin` |

`GET` uçları `record_audit` ÇAĞIRMAZ (WORKFLOW kuralı — okumalar denetlenmez);
yazma uçlarının hepsi tek denetim satırı yazar ve metin servis katmanında, kayıt
değişmeden/yok olmadan ÖNCE kurulur.

Router prefix TAŞIMAZ: uçlar iki ayrı kök altına dağılır (`/stock/items` ve
`/warehouses`) — `documents/router.py` deseninin birebiri.

## AÇILMAYAN uçlar (spec §4, §5 — icat yasağı)

* **`DELETE /stock/items/{id}` YOKTUR.** Hareketi olan kart `stock_entry_lines`
  RESTRICT'i yüzünden zaten silinemez; kullanımdan kaldırma
  `PATCH {"is_active": false}` iledir. Yol tanımlı olmadığı için FastAPI 405
  döner ve bu bir BEKÇİ TESTİYLE kilitlenmiştir
  (`test_silme_ucu_yoktur_405`) — ileride biri DELETE eklemeye kalkarsa test
  kırılır.
* **Kart DETAY ucu (`GET /stock/items/{id}`) YOKTUR:** spec §4 onu saymaz ve
  hiçbir mockup tekil kart ekranı çizmez; liste gövdesi kartın tüm alanlarını
  zaten taşır.
* **Hareket / özet / şantiye stok uçları T3'ündür.** Sipariş, tedarikçi
  kataloğu, "Bekleyen Sipariş" değeri ve oto-bildirim **SA dilimine** aittir ve
  bu modülde HİÇBİRİ açılmaz.
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES, DELETE_403_YANITI
from app.core.permissions import require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.inventory import service
from app.modules.inventory.balance import StockStatus
from app.modules.inventory.models import StockCategory, StockEntryType
from app.modules.inventory.schemas import (
    SectionStockResponse,
    SiteStockResponse,
    StockEntryCreate,
    StockEntryListResponse,
    StockEntryResponse,
    StockItemCreate,
    StockItemListResponse,
    StockItemResponse,
    StockItemUpdate,
    StockSummaryResponse,
    WarehouseCreate,
    WarehouseListResponse,
    WarehouseResponse,
    WarehouseUpdate,
)
from app.modules.users.models import User

router = APIRouter(tags=["inventory"], responses=COMMON_ERROR_RESPONSES, route_class=MaskeRotasi)

_VIEW = require_permission(service.PERMISSION_MODULE, AccessLevel.view)
_FULL = require_permission(service.PERMISSION_MODULE, AccessLevel.full)
# SILME uclari `require_system_admin` ile kapilidir (SIL-B1): modul seviyesi degil rol ANAHTARI.


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


# --- Malzeme kartı (katalog) ---


@router.get("/stock/items", response_model=StockItemListResponse, dependencies=[_VIEW])
async def list_stock_items_endpoint(
    session: DbSession,
    category: StockCategory | None = None,
    q: str | None = None,
    is_active: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StockItemListResponse:
    """E3 tablosunun veri kaynağı — süzgeçler AND'lidir.

    `q` KOD ve AD üzerinde kısmi arar (E3 satırları ikisini üst üste basar, tek
    alanda aramak kullanıcıyı "SNK-0421 yok" sanısına düşürürdü).

    **Durum süzgeci (Kritik/Normal/Fazla) BURADA YOKTUR:** durum bakiyeden
    TÜREVDİR (spec §3) ve hareket toplamı gerektirir — T3'ün özet ucunun işidir.

    `limit` varsayılan 50, tavan 200 (TB3 standardı): tavan aşımı sessizce
    kırpılmaz, 422 döner.
    """
    items, total = await service.list_stock_items(
        session, category=category, q=q, is_active=is_active, limit=limit, offset=offset
    )
    return StockItemListResponse(
        items=[StockItemResponse.model_validate(i) for i in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/stock/items",
    response_model=StockItemResponse,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "Bu malzeme kodu zaten kayıtlı"}},
    dependencies=[_FULL],
)
async def create_stock_item_endpoint(
    request: Request,
    data: StockItemCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> StockItemResponse:
    """Yeni malzeme kartı. `code` GLOBAL tekildir; çakışma → 409."""
    item, detail = await service.create_stock_item(session, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return StockItemResponse.model_validate(item)


@router.patch(
    "/stock/items/{item_id}",
    response_model=StockItemResponse,
    responses={409: {"description": "Bu malzeme kodu zaten kayıtlı"}},
    dependencies=[_FULL],
)
async def update_stock_item_endpoint(
    request: Request,
    item_id: uuid.UUID,
    data: StockItemUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> StockItemResponse:
    """Kısmi güncelleme. **Kullanımdan kaldırma da buradan geçer**
    (`{"is_active": false}`) — DELETE ucu yoktur (modül docstring'i)."""
    item, detail = await service.update_stock_item(session, item_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return StockItemResponse.model_validate(item)


# --- Depo ---


@router.get("/warehouses", response_model=WarehouseListResponse, dependencies=[_VIEW])
async def list_warehouses_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WarehouseListResponse:
    """Görünen depolar: merkez depoların HEPSİ + görünen projelerin şantiye
    depoları (spec §7 S2b; gerekçe `repository._warehouse_scope`).

    **`site_id` SÜZGECİ AÇILMADI:** hiçbir mockup depo listesini şantiyeye göre
    daraltmaz (E3 depo kırılımını kartın satırında gösterir, ŞS zaten tek
    şantiyenin ekranıdır ve verisi T3'ün `GET /sites/{id}/stock` ucundan gelir).
    Gerçek bir ihtiyaç doğarsa T3/F-ST tek satırla ekler.
    """
    items, total = await service.list_warehouses(session, user, limit=limit, offset=offset)
    return WarehouseListResponse(
        items=[WarehouseResponse.model_validate(w) for w in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/warehouses",
    response_model=WarehouseResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "Bu kapsamda aynı adlı depo var"},
        422: {"description": "Seçilen şantiye görünmüyor ya da yok"},
    },
    dependencies=[_FULL],
)
async def create_warehouse_endpoint(
    request: Request,
    data: WarehouseCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> WarehouseResponse:
    """Yeni depo. `site_id` verilmezse MERKEZ depodur (SG 84).

    * kapsam dışı `site_id` → 422 (var olmayan kimlikle AYNI cümle)
    * ad çakışması → 409 (kontrol UYGULAMA katmanındadır; merkez dalında DB
      kısıtı `NULLS DISTINCT` yüzünden İŞLEMEZ)
    """
    warehouse, detail = await service.create_warehouse(session, user, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return WarehouseResponse.model_validate(warehouse)


@router.patch(
    "/warehouses/{warehouse_id}",
    response_model=WarehouseResponse,
    responses={409: {"description": "Bu kapsamda aynı adlı depo var"}},
    dependencies=[_FULL],
)
async def rename_warehouse_endpoint(
    request: Request,
    warehouse_id: uuid.UUID,
    data: WarehouseUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> WarehouseResponse:
    """YALNIZ ad değişir. Depo TAŞIMA ucu yoktur (gerekçe `schemas`ta)."""
    warehouse, site = await service.visible_warehouse(session, user, warehouse_id)
    warehouse, detail = await service.rename_warehouse(session, warehouse, site, data.name)
    await _audit(request, session, user, AuditAction.update, detail)
    return WarehouseResponse.model_validate(warehouse)


@router.delete(
    "/warehouses/{warehouse_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**DELETE_403_YANITI, 409: {"description": "Depoda stok hareketi var"}},
    dependencies=[require_system_admin()],
)
async def delete_warehouse_endpoint(
    request: Request,
    warehouse_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Depoyu siler. YALNIZ Sistem Yöneticisi.

    YALNIZ HAREKETSİZ depo silinir; hareketi varsa **409** (iş kuralı). Yanıt `204 No Content`,
    gövdesizdir.
    """
    warehouse, site = await service.visible_warehouse(session, user, warehouse_id)
    detail = await service.delete_warehouse(session, warehouse, site)
    await _audit(request, session, user, AuditAction.delete, detail)


# --- Hareketler (T3) ---


@router.post(
    "/stock/entries",
    response_model=StockEntryResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Hedef ya da kaynak depo bulunamadı"},
        422: {"description": "Tip kuralı ihlali ya da geçersiz kart/kullanıcı"},
    },
    dependencies=[_FULL],
)
async def create_stock_entry_endpoint(
    request: Request,
    data: StockEntryCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> StockEntryResponse:
    """SG formunun kaydı: başlık + satırlar TEK gövde, ATOMİK.

    * `purchase` → miktar pozitif, kaynak depo YASAK
    * `transfer` → kaynak depo ZORUNLU (kendine transfer 422); miktar hedef
      depoya artı, KAYNAK depodan eksi yansır (ÇİFT BACAK, §7 S4)
    * `adjustment` → miktar NEGATİF olabilir (sayım farkı/iade/SARF tek kapısı)

    **Eksi bakiye ENGELLENMEZ** (§7 S4): katı engel sayım düzeltmesini
    kilitlerdi; eksi bakiye özet uçlarında raporlanır.

    Bozuk bir satır varsa HİÇBİR ŞEY yazılmaz — ne başlık ne satır.
    Denetime GİRİŞ BAŞINA TEK satır düşer (spec §4).
    """
    entry, lines, detail = await service.create_stock_entry(session, user, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return service.to_entry_response(entry, lines)


@router.get("/stock/entries", response_model=StockEntryListResponse, dependencies=[_VIEW])
async def list_stock_entries_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    entry_type: StockEntryType | None = None,
    warehouse_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StockEntryListResponse:
    """ "Stok Hareketi" ekranının verisi. Süzgeçler AND'lidir.

    `warehouse_id` İKİ BACAĞI da kapsar (hedef VEYA kaynak): kullanıcı "bu
    deponun hareketleri" derken oraya gireni de oradan çıkanı da kasteder.

    **DÜZELTME/SİLME UCU YOKTUR:** hareket geçmişi değiştirilmez, yanlış kayıt
    ters işaretli bir `adjustment` ile düzeltilir — geçmişi yeniden yazmak
    bakiye tarihini de yeniden yazardı.
    """
    items, total = await service.list_stock_entries(
        session,
        user,
        entry_type=entry_type,
        warehouse_id=warehouse_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return StockEntryListResponse(items=items, total=total, limit=limit, offset=offset)


# --- Türev özetler ---


@router.get("/stock/summary", response_model=StockSummaryResponse, dependencies=[_VIEW])
async def stock_summary_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    status_filter: Annotated[StockStatus | None, Query(alias="status")] = None,
    category: StockCategory | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StockSummaryResponse:
    """E3 tablosu + KPI şeridi. Kapsam: GÖRÜNEN tüm depolar, merkez DAHİL.

    Bakiye ve durum TÜREVDİR (spec §3) — bakiye kolonu yoktur. Durum formülü
    §7 S1'dir ve sabitleri `inventory/balance.py`de TEK yerde durur;
    `min_stock` yoksa durum `None` döner.

    KPI şeridi SAYFAYI değil SÜZÜLEN KÜMEYİ özetler.
    """
    return await service.build_stock_summary(
        session, user, status=status_filter, category=category, q=q, limit=limit, offset=offset
    )


@router.get("/sites/{site_id}/stock", response_model=SiteStockResponse, dependencies=[_VIEW])
async def site_stock_endpoint(
    site_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    section_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SiteStockResponse:
    """ŞS tablosu + KPI şeridi (86-91).

    **Merkez depo hiçbir şantiyenin bakiyesine GİRMEZ** (spec §3): şantiye
    bakiyesi "o şantiyenin depoları"dır. Görünmeyen şantiye 404 döner ve gövde
    var olmayan kimliğinkiyle aynıdır.

    "Aylık İhtiyaç" HÂLÂ YER TUTUCUDUR (plan ızgarası malzeme satırı taşımaz).
    🔴 **"Bölüm" ise STOK-BOLUM ile GERÇEĞE döndü:** kartın bu şantiyedeki
    hareketlerinde atfedilmiş bölüm adlarını taşır; atıf yoksa boş kalır.

    `section_id` SÜZGECİ SATIR KÜMESİNİ daraltır, `balance`ı DEĞİŞTİRMEZ:
    bakiye depo düzeyindedir ("STOK DEPODA DURUR, BÖLÜM TÜKETİR"). Cümlesi
    *"bu bölümde kullanılmış malzemelerin ŞANTİYE bakiyesi"*dir. Bölümün kendi
    miktarları `GET /sections/{id}/stock`tan gelir. Başka şantiyenin bölümü
    verilirse **404**.
    """
    return await service.build_site_stock(
        session, user, site_id, section_id=section_id, limit=limit, offset=offset
    )


@router.get(
    "/sections/{section_id}/stock",
    response_model=SectionStockResponse,
    responses={404: {"description": "Bölüm bulunamadı"}},
    dependencies=[_VIEW],
)
async def section_stock_endpoint(
    section_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SectionStockResponse:
    """`A1 › Malzeme` sekmesinin ve `Bölüm Malzeme Durumu` kartının verisi.

    🔴 **BAKİYE DÖNMEZ** (ürün kararı): bakiye depo düzeyindedir ve bölüme
    ikinci bir bakiye kaynağı açmak iki-kaynak problemini doğururdu. Bunun
    yerine (malzeme, poz) çifti başına `assigned` / `issued` / `net` döner —
    tanımları `SectionStockRow` docstring'indedir. **Sarf toplamı
    `issued_quantity`dir.**

    Kapı `inventory:view`tir: bu bir STOK ekranıdır, veri stok hareketinden
    türer. `sites`/`boq` kapıları BURADA KULLANILMAZ — kullanılsaydı stok
    okuyabilen `procurement` rolü kendi verisini göremezdi.

    Görünmeyen bölüm ile var olmayan bölüm AYNI 404 gövdesini alır.
    """
    return await service.build_section_stock(session, user, section_id, limit=limit, offset=offset)

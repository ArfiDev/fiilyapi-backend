"""Satinalma is kurallari (T2) — tedarikci katalogu + satin alma talebi.

Spec: `docs/superpowers/specs/2026-08-12-sa-satinalma-design.md` §2, §3, §4.

IKI KATMANLI koruma (`inventory/service.py` deseninin birebiri): `procurement`
izni router'da YETKIYI verir, bu modul `projects.service.visible_projects` ile
KAPSAMI belirler.

## Kapsam iki varlikta FARKLIDIR — ve bu bilincli

* **`suppliers` (katalog): kapsam suzgeci YOKTUR.** Tabloda `project_id` kolonu
  bile yoktur (spec §2): ayni "Demirsan A.S." her projede kullanilir. IDOR
  unutulmus DEGILDIR — sonraki okuyucu buraya proje suzgeci EKLEMESIN
  (`stock_items`/`personnel` deseninin aynisi). **Ama kartin PARA turevi
  ("Bu Yil Toplam Siparis") KAPSAMLIDIR:** gorunmeyen projenin siparisi tutara
  girmez.
* **`purchase_requests`: kapsam suzgeci VARDIR.** Talep bir PROJEYE aittir;
  gorunmeyen projenin talebi listede yoktur ve tekil erisimde **404** doner —
  var olmayanla ayirt edilemez.

## Taslak-farkindalikli zorunluluk (P6 emsali)

Tutarlilik kurallari (XOR, `quantity > 0`, uzunluk tavanlari) HER yazmada
kosar ve SEMA katmanindadir. Zorunluluk kurallari (ihtiyac tarihi, en az bir
kalem) yalnizca taslak DISINDA kosar; TEK kaynaklari `validation.py`dir ve
onlari cagiran `submit` ucu **T3'undur**.

## 🔴 Paket yapisi (TB-PROC) — davranis DEGISMEDI

Dosya 1009 satirdaydi (tavan 800). SORUMLULUGA gore sekize bolundu; hicbir uc,
SQL, yanit govdesi, hata metni ya da izin kapisi degismedi. Dis yuzey KORUNDU:
eski `service.py`nin TUM modul duzeyi adlari (`_` ile baslayan ozel adlar ve
yalnizca ITHAL EDILMIS adlar DAHIL) buradan aynen okunabilir. `router.py` ve
`stock_link.py` onlara `service.X` biciminde ULASIYORDU; cagiran tarafta tek
satir degismedi.

Katmanlar (ok yonu = bagimlilik, cember YOK):

    core  ←  suppliers  ←  quotes  ←  orders
      ↑
      ├──  request_access  ←  request_writes
      │                    ←  request_reads
      └──  request_actions

* `core.py`            — izin anahtari · kapsam (`_visible_project_ids`) · `_strip`
* `suppliers.py`       — TED katalogu (kapsam suzgeci YOK, PARA turevi kapsamli)
* `request_access.py`  — talebin kapilari: kapsam 404 · durum 409 · silme 403
* `request_writes.py`  — talep olustur/guncelle/sil (dogrulama yazimdan ONCE)
* `request_reads.py`   — FST detayi + SAT tablosu (N+1 yok)
* `request_actions.py` — T3 gecisleri: submit / approve / reject
* `quotes.py`          — TEK alt-kaynagi + "EN IYI FIYAT" rozeti
* `orders.py`          — SIP + `select_and_order` (ATOMIK, acik SAVEPOINT)

### Neden ITHAL EDILMIS adlar da yeniden ihrac ediliyor

Eski `service.py`nin ad uzayinda `uuid`, `Decimal`, `repository`, `messages`
gibi adlar da GORUNURDU (`service.repository` yazan bir cagiran calisirdi).
Bugun hicbir cagirani yok — ama "bugun yok" ile "sozlesme degil" ayni sey
degildir ve bolme dilimi davranis DEGISTIRMEMELIDIR. Kume
`tests/modules/procurement/tbproc_servis_yuzeyi.txt` anlik goruntusunde
DONDURULMUSTUR; bu adlari dusurmek isteyen sonraki okuyucu once o dosyayi
bilincli olarak guncellemek zorundadir — kaza ile dusuremez.

`X as X` bicimi bilinclidir: acik yeniden-ihrac, `noqa` olmadan F401'i susturur
ve `__all__`e girmeyen ozel adlari da kapsar (`personnel/service` emsali).
"""

import uuid as uuid
from decimal import Decimal as Decimal

from sqlalchemy import Row as Row
from sqlalchemy.ext.asyncio import AsyncSession as AsyncSession

from app.core.errors import ConflictError as ConflictError
from app.core.errors import NotFoundError as NotFoundError
from app.core.errors import ProcurementValidationError as ProcurementValidationError
from app.core.timezone import today as today

# Bu IKI ad KAYNAKTAKINDEN FARKLI yazilir (`service`->`approvals_service`,
# `repository`->`sites_repository`), bu yuzden `X as X` bicimi kullanilamaz ve
# F401 elle susturulur. Ikisi de eski `service.py`nin ad uzayinda VARDI.
from app.modules.approvals import service as approvals_service  # noqa: F401
from app.modules.audit import messages as messages
from app.modules.inventory.models import StockItem as StockItem
from app.modules.procurement import guards as guards
from app.modules.procurement import numbering as numbering
from app.modules.procurement import repository as repository
from app.modules.procurement import transitions as transitions
from app.modules.procurement.models import PurchaseOrder as PurchaseOrder
from app.modules.procurement.models import PurchaseOrderStatus as PurchaseOrderStatus
from app.modules.procurement.models import PurchasePriority as PurchasePriority
from app.modules.procurement.models import PurchaseQuote as PurchaseQuote
from app.modules.procurement.models import PurchaseRequest as PurchaseRequest
from app.modules.procurement.models import PurchaseRequestLine as PurchaseRequestLine
from app.modules.procurement.models import PurchaseRequestStatus as PurchaseRequestStatus
from app.modules.procurement.models import Supplier as Supplier
from app.modules.procurement.schemas import PurchaseOrderCreate as PurchaseOrderCreate
from app.modules.procurement.schemas import PurchaseOrderListResponse as PurchaseOrderListResponse
from app.modules.procurement.schemas import PurchaseOrderResponse as PurchaseOrderResponse
from app.modules.procurement.schemas import PurchaseOrderUpdate as PurchaseOrderUpdate
from app.modules.procurement.schemas import PurchaseQuoteCard as PurchaseQuoteCard
from app.modules.procurement.schemas import PurchaseQuoteCreate as PurchaseQuoteCreate
from app.modules.procurement.schemas import PurchaseQuoteListResponse as PurchaseQuoteListResponse
from app.modules.procurement.schemas import PurchaseQuoteResponse as PurchaseQuoteResponse
from app.modules.procurement.schemas import PurchaseQuoteUpdate as PurchaseQuoteUpdate
from app.modules.procurement.schemas import PurchaseRequestCreate as PurchaseRequestCreate
from app.modules.procurement.schemas import PurchaseRequestLineCreate as PurchaseRequestLineCreate
from app.modules.procurement.schemas import (
    PurchaseRequestLineResponse as PurchaseRequestLineResponse,
)
from app.modules.procurement.schemas import (
    PurchaseRequestListResponse as PurchaseRequestListResponse,
)
from app.modules.procurement.schemas import PurchaseRequestListRow as PurchaseRequestListRow
from app.modules.procurement.schemas import PurchaseRequestResponse as PurchaseRequestResponse
from app.modules.procurement.schemas import PurchaseRequestUpdate as PurchaseRequestUpdate
from app.modules.procurement.schemas import SupplierCard as SupplierCard
from app.modules.procurement.schemas import SupplierCreate as SupplierCreate
from app.modules.procurement.schemas import SupplierResponse as SupplierResponse
from app.modules.procurement.schemas import SupplierUpdate as SupplierUpdate
from app.modules.procurement.service.core import PERMISSION_MODULE as PERMISSION_MODULE
from app.modules.procurement.service.core import _strip as _strip
from app.modules.procurement.service.core import _visible_project_ids as _visible_project_ids
from app.modules.procurement.service.orders import _order_response as _order_response
from app.modules.procurement.service.orders import _to_order_response as _to_order_response
from app.modules.procurement.service.orders import create_order as create_order
from app.modules.procurement.service.orders import get_order_detail as get_order_detail
from app.modules.procurement.service.orders import list_orders as list_orders
from app.modules.procurement.service.orders import select_and_order as select_and_order
from app.modules.procurement.service.orders import update_order as update_order
from app.modules.procurement.service.orders import visible_order as visible_order
from app.modules.procurement.service.orders import (
    visible_order_locked as visible_order_locked,
)
from app.modules.procurement.service.quotes import _assert_quote_wait as _assert_quote_wait
from app.modules.procurement.service.quotes import _assert_shipping_rule as _assert_shipping_rule
from app.modules.procurement.service.quotes import _quote_fields as _quote_fields
from app.modules.procurement.service.quotes import _quote_response as _quote_response
from app.modules.procurement.service.quotes import _visible_quote as _visible_quote
from app.modules.procurement.service.quotes import create_quote as create_quote
from app.modules.procurement.service.quotes import delete_quote as delete_quote
from app.modules.procurement.service.quotes import list_quotes as list_quotes
from app.modules.procurement.service.quotes import update_quote as update_quote
from app.modules.procurement.service.request_access import _assert_draft as _assert_draft
from app.modules.procurement.service.request_access import _assert_scope as _assert_scope
from app.modules.procurement.service.request_access import (
    _assert_stock_items_exist as _assert_stock_items_exist,
)
from app.modules.procurement.service.request_access import can_delete_request as can_delete_request
from app.modules.procurement.service.request_access import visible_request as visible_request
from app.modules.procurement.service.request_access import (
    visible_request_locked as visible_request_locked,
)
from app.modules.procurement.service.request_actions import (
    _TRANSITION_MESSAGES as _TRANSITION_MESSAGES,
)
from app.modules.procurement.service.request_actions import (
    perform_request_action as perform_request_action,
)
from app.modules.procurement.service.request_reads import _base_fields as _base_fields
from app.modules.procurement.service.request_reads import _line_total as _line_total
from app.modules.procurement.service.request_reads import _to_line_response as _to_line_response
from app.modules.procurement.service.request_reads import (
    build_request_detail as build_request_detail,
)
from app.modules.procurement.service.request_reads import list_requests as list_requests
from app.modules.procurement.service.request_writes import _new_lines as _new_lines
from app.modules.procurement.service.request_writes import create_request as create_request
from app.modules.procurement.service.request_writes import delete_request as delete_request
from app.modules.procurement.service.request_writes import update_request as update_request
from app.modules.procurement.service.suppliers import _to_supplier_card as _to_supplier_card
from app.modules.procurement.service.suppliers import create_supplier as create_supplier
from app.modules.procurement.service.suppliers import get_supplier as get_supplier
from app.modules.procurement.service.suppliers import get_supplier_card as get_supplier_card
from app.modules.procurement.service.suppliers import list_suppliers as list_suppliers
from app.modules.procurement.service.suppliers import update_supplier as update_supplier
from app.modules.projects.service import visible_projects as visible_projects
from app.modules.sites import repository as sites_repository  # noqa: F401
from app.modules.users.models import User as User

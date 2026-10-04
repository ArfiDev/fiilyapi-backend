"""Personel servisi (puantaj spec §1, §2, §3, §5 + İK-1 spec §1, §5).

`customers/service.py`nin kardeşi: proje-bağımsız kartoteks, `NotFoundError` -> 404,
alanlar-arası kural servis korkuluğunda (`guards`) -> 422, benzersizlik -> 409.

**Silme ucu YOK** (spec §3): `timesheet_entries.personnel_id` FK'si RESTRICT'tir —
puantajı olan bir işçi silinemez. Kartoteksten çıkarma `is_active=false` PATCH'idir.

**İK-1 kart genişlemesi (spec §5):** yeni kart kolonları HEPSİ opsiyoneldir; taslak
(`is_draft=true`) gevşektir, yayın (`is_draft=false`) PE ✱ kümesini zorunlu kılar.
Zorunluluk BİRLEŞİK kayıt üzerinde koşar (P6 `_merged` deseni): PATCH kısmi gövde
gönderdiğinden yalnız gövdeye bakmak, yayın kaydını eksik alana düşürürdü. TCKN
checksum + UQ (`DuplicateError` -> 409) ve atama alanlarının (`assigned_project_id`/
`assigned_section_id`) varlık/kapsam doğrulaması da burada.

## 🔴 Paket yapısı (TB-REFACTOR) — davranış DEĞİŞMEDİ

Dosya 1124 satırdaydı (tavan 800). COHESION'a göre altıya bölündü; hiçbir uç,
SQL, yanıt gövdesi, hata metni ya da izin kapısı değişmedi. Dış imza KORUNDU:
eski `service.py`nin TÜM modül düzeyi adları (özel `_` adları DÂHİL) buradan
aynen okunabilir — `router.py`, `document_type_router.py` ve testler onlara
`service.X` biçimiyle ULAŞIYORDU. Çağıran tarafta tek satır değişmedi.

Katmanlar (ok yönü = bağımlılık, çember YOK):

    core  ←  documents  ←  leave_requests  ←  leave_decisions  ←  leave_summary
      ↑          ↑
      └── documents_summary

* `core.py`              — kartoteks: `get_personnel` · create/update · PE ✱ kapısı
* `documents.py`         — İK-1 T3 belge alt-kaynağı
* `documents_summary.py` — İK-1 T4 belge takibi özeti (BT mockup)
* `leave_requests.py`    — İK-2 T2 izin talebi CRUD
* `leave_decisions.py`   — İK-2 T3 onay/red + bakiye (🔴 EŞİK = KİLİT burada)
* `leave_summary.py`     — İK-2 T4 izin özeti (İZ mockup)

`X as X` biçimi bilinçlidir: açık yeniden-ihraç, `noqa` olmadan F401'i susturur
ve `__all__`e girmeyen özel adları da kapsar.
"""

from app.modules.personnel.service.core import (
    _CARD_FIELDS as _CARD_FIELDS,
)
from app.modules.personnel.service.core import (
    PERMISSION_MODULE as PERMISSION_MODULE,
)
from app.modules.personnel.service.core import (
    SUMMARY_LIST_LIMIT as SUMMARY_LIST_LIMIT,
)
from app.modules.personnel.service.core import (
    _assert_publish_ready as _assert_publish_ready,
)
from app.modules.personnel.service.core import (
    _validate_assignment_scope as _validate_assignment_scope,
)
from app.modules.personnel.service.core import (
    _validate_tckn as _validate_tckn,
)
from app.modules.personnel.service.core import (
    create_personnel as create_personnel,
)
from app.modules.personnel.service.core import (
    get_personnel as get_personnel,
)
from app.modules.personnel.service.core import (
    update_personnel as update_personnel,
)
from app.modules.personnel.service.documents import (
    _assert_document_visible as _assert_document_visible,
)
from app.modules.personnel.service.documents import (
    _assert_type_xor_label as _assert_type_xor_label,
)
from app.modules.personnel.service.documents import (
    _document_label as _document_label,
)
from app.modules.personnel.service.documents import (
    _document_response as _document_response,
)
from app.modules.personnel.service.documents import (
    _resolve_document_type as _resolve_document_type,
)
from app.modules.personnel.service.documents import (
    _resolve_type_for_read as _resolve_type_for_read,
)
from app.modules.personnel.service.documents import (
    create_personnel_document as create_personnel_document,
)
from app.modules.personnel.service.documents import (
    delete_personnel_document as delete_personnel_document,
)
from app.modules.personnel.service.documents import (
    list_personnel_documents as list_personnel_documents,
)
from app.modules.personnel.service.documents import (
    update_personnel_document as update_personnel_document,
)
from app.modules.personnel.service.documents_summary import (
    build_hr_documents_summary as build_hr_documents_summary,
)
from app.modules.personnel.service.leave_decisions import (
    _assert_approvable as _assert_approvable,
)
from app.modules.personnel.service.leave_decisions import (
    _assert_decidable as _assert_decidable,
)
from app.modules.personnel.service.leave_decisions import (
    _assert_withdrawable as _assert_withdrawable,
)
from app.modules.personnel.service.leave_decisions import (
    _balance_response as _balance_response,
)
from app.modules.personnel.service.leave_decisions import (
    _leave_balance_parts as _leave_balance_parts,
)
from app.modules.personnel.service.leave_decisions import (
    _lock_decision_scope as _lock_decision_scope,
)
from app.modules.personnel.service.leave_decisions import (
    _stamp_decision as _stamp_decision,
)
from app.modules.personnel.service.leave_decisions import (
    approve_leave_request as approve_leave_request,
)
from app.modules.personnel.service.leave_decisions import (
    get_leave_balance as get_leave_balance,
)
from app.modules.personnel.service.leave_decisions import (
    reject_leave_request as reject_leave_request,
)
from app.modules.personnel.service.leave_decisions import (
    upsert_leave_balance as upsert_leave_balance,
)
from app.modules.personnel.service.leave_decisions import (
    withdraw_leave_request as withdraw_leave_request,
)
from app.modules.personnel.service.leave_requests import (
    _assert_date_order as _assert_date_order,
)
from app.modules.personnel.service.leave_requests import (
    _assert_pending as _assert_pending,
)
from app.modules.personnel.service.leave_requests import (
    _create_leave_request_for as _create_leave_request_for,
)
from app.modules.personnel.service.leave_requests import (
    _leave_response as _leave_response,
)
from app.modules.personnel.service.leave_requests import (
    _resolve_leave_type as _resolve_leave_type,
)
from app.modules.personnel.service.leave_requests import (
    create_leave_request as create_leave_request,
)
from app.modules.personnel.service.leave_requests import (
    create_self_leave_request as create_self_leave_request,
)
from app.modules.personnel.service.leave_requests import (
    delete_leave_request as delete_leave_request,
)
from app.modules.personnel.service.leave_requests import (
    find_overlapping_approved_leave as find_overlapping_approved_leave,
)
from app.modules.personnel.service.leave_requests import (
    get_leave_request as get_leave_request,
)
from app.modules.personnel.service.leave_requests import (
    get_leave_request_row as get_leave_request_row,
)
from app.modules.personnel.service.leave_requests import (
    list_leave_requests as list_leave_requests,
)
from app.modules.personnel.service.leave_requests import (
    list_leave_types as list_leave_types,
)
from app.modules.personnel.service.leave_requests import (
    list_self_leave_requests as list_self_leave_requests,
)
from app.modules.personnel.service.leave_requests import (
    resolve_self_personnel as resolve_self_personnel,
)
from app.modules.personnel.service.leave_requests import (
    update_leave_request as update_leave_request,
)
from app.modules.personnel.service.leave_summary import (
    _balance_sort_key as _balance_sort_key,
)
from app.modules.personnel.service.leave_summary import (
    _month_window as _month_window,
)
from app.modules.personnel.service.leave_summary import (
    build_hr_leaves_summary as build_hr_leaves_summary,
)

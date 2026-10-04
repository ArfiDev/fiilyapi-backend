"""P2/P6 santiye + bolum SERVISI — 1087 satirdan cohesion'a gore bolunmus paket.

🔴 **Davranis DEGISMEDI** (TB-REFACTOR): hicbir uc, SQL, yanit govdesi, hata
metni ya da izin kapisi degismedi. Dis imza KORUNDU — eski `service.py`nin TUM
modul duzeyi adlari (ozel `_` adlari DAHIL) buradan aynen okunabilir, cunku
`router.py`, `boq.service` ve `projects.service` onlara `service.X` /
`from app.modules.sites.service import X` bicimiyle ULASIYORDU.

Katmanlar (ok yonu = bagimlilik, cember YOK):

    codes · presenters · visibility · writes_common      (yaprak)
        ^          ^          ^  ^          ^  ^
        |          |          |  |          |  |
    site_writes · section_writes · reads · deletes

* `presenters.py`     — ORM satiri -> Pydantic yanit + yer tutucu sayaclar
* `codes.py`          — `SNT-…` / `BLM-…` kod uretimi (projects de kullanir)
* `visibility.py`     — gorunurluk suzgeci + 404 zinciri (boq da kullanir)
* `writes_common.py`  — iki yazma ailesinin PAYLASTIGI yardimcilar
* `reads.py`          — liste + detay okuma uclari
* `site_writes.py`    — `create_site` / `update_site`
* `section_writes.py` — `create_section` / `update_section`

`X as X` bicimi BILINCLIDIR: acik yeniden-ihrac, `noqa` olmadan F401'i susturur
ve `__all__`e girmeyen ozel adlari da kapsar.
"""

from app.modules.sites.service.codes import (
    _SECTION_CODE_DIGITS as _SECTION_CODE_DIGITS,
)
from app.modules.sites.service.codes import (
    _SECTION_CODE_PREFIX as _SECTION_CODE_PREFIX,
)
from app.modules.sites.service.codes import (
    _SITE_CODE_PREFIX as _SITE_CODE_PREFIX,
)
from app.modules.sites.service.codes import (
    _next_section_code as _next_section_code,
)
from app.modules.sites.service.codes import (
    _next_site_code as _next_site_code,
)
from app.modules.sites.service.presenters import (
    _BOQ as _BOQ,
)
from app.modules.sites.service.presenters import (
    _CONTRACTS as _CONTRACTS,
)
from app.modules.sites.service.presenters import (
    _PROGRESS_PAYMENTS as _PROGRESS_PAYMENTS,
)
from app.modules.sites.service.presenters import (
    _PROJECT_COSTS as _PROJECT_COSTS,
)
from app.modules.sites.service.presenters import (
    _SUBCONTRACTS as _SUBCONTRACTS,
)
from app.modules.sites.service.presenters import (
    _TIMESHEET as _TIMESHEET,
)
from app.modules.sites.service.presenters import (
    _card_fields as _card_fields,
)
from app.modules.sites.service.presenters import (
    _count as _count,
)
from app.modules.sites.service.presenters import (
    _facilities as _facilities,
)
from app.modules.sites.service.presenters import (
    _metric as _metric,
)
from app.modules.sites.service.presenters import (
    _remaining_days as _remaining_days,
)
from app.modules.sites.service.presenters import (
    _resolve_city as _resolve_city,
)
from app.modules.sites.service.presenters import (
    _section_counts as _section_counts,
)
from app.modules.sites.service.presenters import (
    _site_counts as _site_counts,
)
from app.modules.sites.service.presenters import (
    _to_milestone as _to_milestone,
)
from app.modules.sites.service.presenters import (
    _totals as _totals,
)
from app.modules.sites.service.presenters import (
    _worker_count as _worker_count,
)
from app.modules.sites.service.presenters import (
    to_card as to_card,
)
from app.modules.sites.service.presenters import (
    to_detail as to_detail,
)
from app.modules.sites.service.presenters import (
    to_section as to_section,
)
from app.modules.sites.service.presenters import (
    to_section_detail as to_section_detail,
)
from app.modules.sites.service.reads import (
    build_section_detail as build_section_detail,
)
from app.modules.sites.service.reads import (
    build_site_detail as build_site_detail,
)
from app.modules.sites.service.reads import (
    get_section_detail as get_section_detail,
)
from app.modules.sites.service.reads import (
    get_site_detail as get_site_detail,
)
from app.modules.sites.service.reads import (
    list_sections_for_site as list_sections_for_site,
)
from app.modules.sites.service.reads import (
    list_sites_overview as list_sites_overview,
)
from app.modules.sites.service.section_writes import (
    _SECTION_MANAGER_FIELDS as _SECTION_MANAGER_FIELDS,
)
from app.modules.sites.service.section_writes import (
    _SECTION_VALIDATED_FIELDS as _SECTION_VALIDATED_FIELDS,
)
from app.modules.sites.service.section_writes import (
    _merge_milestones as _merge_milestones,
)
from app.modules.sites.service.section_writes import (
    _resolved_manager_names as _resolved_manager_names,
)
from app.modules.sites.service.section_writes import (
    _validate_dependency as _validate_dependency,
)
from app.modules.sites.service.section_writes import (
    create_section as create_section,
)
from app.modules.sites.service.section_writes import (
    update_section as update_section,
)
from app.modules.sites.service.site_writes import (
    _OUTSOURCED_SAFETY_OFFICER_LABEL as _OUTSOURCED_SAFETY_OFFICER_LABEL,
)
from app.modules.sites.service.site_writes import (
    _VALIDATED_FIELDS as _VALIDATED_FIELDS,
)
from app.modules.sites.service.site_writes import (
    _apply_facilities as _apply_facilities,
)
from app.modules.sites.service.site_writes import (
    _resolve_safety_officer as _resolve_safety_officer,
)
from app.modules.sites.service.site_writes import (
    _resolve_section_manager_names as _resolve_section_manager_names,
)
from app.modules.sites.service.site_writes import (
    _write_sections as _write_sections,
)
from app.modules.sites.service.site_writes import (
    create_site as create_site,
)
from app.modules.sites.service.site_writes import (
    update_site as update_site,
)
from app.modules.sites.service.visibility import (
    _PROJECT_MISSING as _PROJECT_MISSING,
)
from app.modules.sites.service.visibility import (
    _SECTION_MISSING as _SECTION_MISSING,
)
from app.modules.sites.service.visibility import (
    _SITE_MISSING as _SITE_MISSING,
)
from app.modules.sites.service.visibility import (
    _visible_project as _visible_project,
)
from app.modules.sites.service.visibility import (
    _visible_section as _visible_section,
)
from app.modules.sites.service.visibility import (
    _visible_site as _visible_site,
)
from app.modules.sites.service.writes_common import (
    _merged_for_validation as _merged_for_validation,
)
from app.modules.sites.service.writes_common import (
    _resolve_user_name as _resolve_user_name,
)

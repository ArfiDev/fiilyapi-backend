"""Hakediş "günlükten doldur" öneri uçları (T5; spec §4, §7 S2).

Yollar HAKEDİŞ modüllerinin altındadır ama kod BURADA yaşar: okunan veri
günlüktür, kararlar (yalnız `submitted`, dönem süzgeci, BOQ köprüsü) günlük
modülünün kararlarıdır. Hakediş modüllerine taşınsaydı aynı kurallar iki yerde
tutulurdu. Modül dışına yine TEK router çıkar (`router.py` sonunda bağlanır).

## İZİN KARARI: İKİ KAPI

Uçlar `progress_payments.view` **VE** `site_diary.view` ister.

Yalnız hakediş izni yeterli sayılsaydı `accounting` rolü (matriste
`progress_payments=_APR` ama `site_diary=_N`) günlük verisini okurdu — izin
matrisinin günlüğü açıkça REDDETTİĞİ bir role, hakediş yolunun altındaki bir
uçtan sızardı. Yalnız günlük izni yeterli sayılsaydı bu kez hakediş kalem
kimlikleri (`contract_item_id`, sözleşme kırılımı) hakedişi görmeyen role
sızardı. İki veri kümesi birleştiği için KESİŞİM istenir.

Meşru kullanıcıyı kilitlemez: `site_chief`/`field_engineer` (`_DRF`/`_F`),
`project_manager` (`_APR`/`_V`), `patron` ve `system_admin` iki kapıdan da geçer
— yani öneriyi hakedişe uygulayabilecek HER rol ucu kullanabilir.

🛑 İkisi de `GET`tir ve SALT OKUNURDUR: `record_audit` dahil hiçbir yazma
yapılmaz (gerekçe `suggestion.py` başlığında).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.errors import SiteValidationError
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.modules.site_diary import guards, service, suggestion
from app.modules.site_diary.guards import MAX_YEAR, MIN_YEAR
from app.modules.site_diary.schemas import EmployerDiarySuggestion, SubcontractorDiarySuggestion
from app.modules.users.models import User

router = APIRouter(tags=["site-diary"], responses=COMMON_ERROR_RESPONSES)

# İki hakediş router'ının kendi kapılarında kullandığı izin anahtarının aynısı
# (`progress_payments/router.py`, `subcontractor_progress_payments/router.py`):
# taşeron hakedişi AYRI bir izin modülü DEĞİLDİR, aynı satırı paylaşır.
_PAYMENTS_MODULE = "progress_payments"

_GATES = [
    require_permission(_PAYMENTS_MODULE, AccessLevel.view),
    require_permission(service.PERMISSION_MODULE, AccessLevel.view),
]


def _assert_period(year: int | None, month: int | None) -> None:
    """T2/T4 ile BİREBİR: `month` yalnız `year` ile anlamlıdır ("her yılın
    temmuzu" bir dönem değildir). Uçlar farklı davransaydı aynı ekranın hakediş
    önerisi ile özeti farklı dönemleri kabul ederdi."""
    if month is not None and year is None:
        raise SiteValidationError(guards.YEAR_REQUIRED_FOR_MONTH)


@router.get(
    "/projects/{project_id}/progress-payments/diary-suggestion",
    response_model=EmployerDiarySuggestion,
    dependencies=_GATES,
)
async def employer_diary_suggestion_endpoint(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: Annotated[int | None, Query(ge=MIN_YEAR, le=MAX_YEAR)] = None,
    month: Annotated[int | None, Query(ge=1, le=12)] = None,
) -> EmployerDiarySuggestion:
    """İşveren hakedişi için "günlükten doldur" ÖNERİSİ — **hiçbir şey yazmaz**.

    Yanıtın `lines` alanı `PUT /progress-payments/{id}/lines` gövdesine BİREBİR
    uyar; uygulamak kullanıcının o ayrı çağrısıdır. Kesin kararlar
    `suggestion.employer_suggestion`tedir.
    """
    _assert_period(year, month)
    return await suggestion.employer_suggestion(session, user, project_id, year=year, month=month)


@router.get(
    "/subcontractor-contracts/{contract_id}/progress-payments/diary-suggestion",
    response_model=SubcontractorDiarySuggestion,
    dependencies=_GATES,
)
async def subcontractor_diary_suggestion_endpoint(
    contract_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: Annotated[int | None, Query(ge=MIN_YEAR, le=MAX_YEAR)] = None,
    month: Annotated[int | None, Query(ge=1, le=12)] = None,
) -> SubcontractorDiarySuggestion:
    """Taşeron hakedişi için "günlükten doldur" ÖNERİSİ — **hiçbir şey yazmaz**.

    Kapsam sözleşmeden gelir: şantiyeye bağlı sözleşmede YALNIZ o şantiyenin
    günlüğü, şantiyesiz (proje geneli) sözleşmede ise projenin TÜM şantiyelerinin
    günlüğü toplanır (kullanıcı kararı 2026-08-27 — eski spec §7 S5 "kapsam
    dışıdır" kuralı TERSİNE ÇEVRİLDİ). Gruplama her iki hâlde de yalnız KALEMDİR.
    """
    _assert_period(year, month)
    return await suggestion.subcontractor_suggestion(
        session, user, contract_id, year=year, month=month
    )

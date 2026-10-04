"""Silme uçlarının ORTAK OpenAPI parçaları (SIL-B1): sorgu parametresi + hata gövdeleri.

Motor üstünden çalışan her DELETE ucu (genel `/admin/silme/...` ve mevcut `DELETE /sites/{id}`
gibi aile uçları) AYNI parametreyi ve AYNI yanıt belgesini taşır; kopya sürüklenmesin diye tek yer.
"""

from typing import Annotated, Any

from fastapi import Query

from app.core.silme.hatalar import (
    FINANCIAL_PENDING_DETAIL,
    PREVIEW_REQUIRED_DETAIL,
    PREVIEW_STALE_DETAIL,
)
from app.modules.silme.schemas import DeleteErrorResponse

PreviewTokenQuery = Annotated[
    str | None,
    Query(
        max_length=128,
        description="`GET /admin/silme/{kind}/{id}/onizleme` yanıtındaki `preview_token`, AYNEN. "
        "Eksikse 428 `preview_required`; silme anındaki ağaçla uyuşmazsa 409 `preview_stale`.",
    ),
]

DELETE_WITH_PREVIEW_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {
        "model": DeleteErrorResponse,
        "description": f"`code=preview_stale`: `{PREVIEW_STALE_DETAIL}`. "
        f"`code=financial_pending`: `{FINANCIAL_PENDING_DETAIL}` (ağaçta `is_financial` grup var; "
        "HİÇBİR ŞEY silinmez).",
    },
    428: {
        "model": DeleteErrorResponse,
        "description": f"`code=preview_required`: `{PREVIEW_REQUIRED_DETAIL}`",
    },
}

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.day_hooks import DaysLockedError, DiarySubmitBlockedError
from app.core.errors import (
    AccountingValidationError,
    ApprovalNotAllowedError,
    ApprovalValidationError,
    BoqGroupSiteMismatchError,
    ConflictError,
    CustomerValidationError,
    DocumentValidationError,
    DomainError,
    DuplicateError,
    EarnedValueValidationError,
    EquipmentValidationError,
    InventoryValidationError,
    InvoicingValidationError,
    NotFoundError,
    OfferValidationError,
    PayrollValidationError,
    PermissionLockedError,
    PersonnelValidationError,
    ProcurementValidationError,
    ProjectTypeMismatchError,
    ProjectValidationError,
    RelatedRecordsExistError,
    RoleValidationError,
    SectionTypeTakenError,
    SiteValidationError,
    TreasuryValidationError,
    UnitValidationError,
)
from app.core.silme.hatalar import (
    CODE_FINANCIAL_PENDING,
    CODE_PREVIEW_REQUIRED,
    CODE_PREVIEW_STALE,
    DeleteFinancialPendingError,
    DeletePreviewRequiredError,
    DeletePreviewStaleError,
)


async def _permission_locked_handler(request: Request, exc: PermissionLockedError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(exc)})


async def _delete_preview_required_handler(
    request: Request, exc: DeletePreviewRequiredError
) -> JSONResponse:
    """428 + `code` — silme önizlemesiz çalışmaz (SIL-B1)."""
    return JSONResponse(
        status_code=status.HTTP_428_PRECONDITION_REQUIRED,
        content={"detail": str(exc), "code": CODE_PREVIEW_REQUIRED},
    )


async def _delete_preview_stale_handler(
    request: Request, exc: DeletePreviewStaleError
) -> JSONResponse:
    """409 + `code` — istemci "önizlemeyi yenile" akışını metne bakmadan seçer (SIL-B1)."""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc), "code": CODE_PREVIEW_STALE},
    )


async def _delete_financial_pending_handler(
    request: Request, exc: DeleteFinancialPendingError
) -> JSONResponse:
    """409 + `code=financial_pending` — mali bağlı kayıt var, silme SIL-B2'ye kadar kapalı."""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc), "code": CODE_FINANCIAL_PENDING},
    )


async def _approval_not_allowed_handler(
    request: Request, exc: ApprovalNotAllowedError
) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(exc)})


async def _not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})


async def _project_type_mismatch_handler(
    request: Request, exc: ProjectTypeMismatchError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _project_validation_handler(
    request: Request, exc: ProjectValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _diary_submit_blocked_handler(
    request: Request, exc: DiarySubmitBlockedError
) -> JSONResponse:
    """422 + `reasons` listesi (istemci kontrol cubugunu buna gore boyar)."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": str(exc),
            "reasons": exc.reasons,
            # EV-BORC-2: her madde YAPISAL kodla (istemci metne regex'le bakmasin); ek alan.
            "reason_items": [{"code": r.code, "message": r.message} for r in exc.items],
        },
    )


async def _days_locked_handler(request: Request, exc: DaysLockedError) -> JSONResponse:
    """409 + `locked_days` (ISO tarih listesi) — istemci kilitli gunleri salt okunur basar."""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": str(exc),
            "locked_days": [d.isoformat() for d in exc.locked_days],
            # EV-BORC-4: gun + kilidi koyan rapor tarihi (ek alan)
            "day_locks": [
                {
                    "day": lock.day.isoformat(),
                    "report_date": lock.report_date.isoformat() if lock.report_date else None,
                }
                for lock in exc.day_locks
            ],
        },
    )


async def _earned_value_validation_handler(
    request: Request, exc: EarnedValueValidationError
) -> JSONResponse:
    content: dict[str, object] = {"detail": str(exc)}
    if exc.errors:  # yalniz yapisal hata tasiyan (katalog toplu ekleme) 422'ler; digerleri DEGISMEZ
        content["errors"] = exc.errors
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=content)


async def _site_validation_handler(request: Request, exc: SiteValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _inventory_validation_handler(
    request: Request, exc: InventoryValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _section_type_taken_handler(request: Request, exc: SectionTypeTakenError) -> JSONResponse:
    """409 + `existing {id, name}` — istemci mevcut tipi metinden ayiklamaz (BLF-B1)."""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": str(exc),
            "existing": {"id": str(exc.existing_id), "name": exc.existing_name},
        },
    )


async def _duplicate_error_handler(request: Request, exc: DuplicateError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


async def _related_records_exist_handler(
    request: Request, exc: RelatedRecordsExistError
) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


async def _conflict_error_handler(request: Request, exc: ConflictError) -> JSONResponse:
    content: dict[str, object] = {"detail": str(exc)}
    if exc.errors:  # yalniz yapisal hata tasiyan (donusturme kod cakismasi) 409'lar
        content["errors"] = exc.errors
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=content)


async def _boq_group_site_mismatch_handler(
    request: Request, exc: BoqGroupSiteMismatchError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _unit_validation_handler(request: Request, exc: UnitValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _customer_validation_handler(
    request: Request, exc: CustomerValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _personnel_validation_handler(
    request: Request, exc: PersonnelValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _document_validation_handler(
    request: Request, exc: DocumentValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _procurement_validation_handler(
    request: Request, exc: ProcurementValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _payroll_validation_handler(
    request: Request, exc: PayrollValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _equipment_validation_handler(
    request: Request, exc: EquipmentValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _role_validation_handler(request: Request, exc: RoleValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _offer_validation_handler(request: Request, exc: OfferValidationError) -> JSONResponse:
    content: dict[str, object] = {"detail": str(exc)}
    if exc.errors:  # yalniz yapisal hata tasiyan (donusturme) 422'ler; digerleri DEGISMEZ
        content["errors"] = exc.errors
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=content)


async def _invoicing_validation_handler(
    request: Request, exc: InvoicingValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _treasury_validation_handler(
    request: Request, exc: TreasuryValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _accounting_validation_handler(
    request: Request, exc: AccountingValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _approval_validation_handler(
    request: Request, exc: ApprovalValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


async def _domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


async def _integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT, content={"detail": "Veri bütünlüğü hatası"}
    )


#: PostgreSQL SQLSTATE'leri — "değer alanın sınırını aşıyor" sınıfı.
#: `22003` sayısal taşma (numeric field overflow), `22001` metin taşması
#: (string data right truncation). İkisi de kullanıcının DÜZELTEBİLECEĞİ bir
#: ALAN hatasıdır; 22 sınıfının geri kalanı (ör. `22012` sıfıra bölme) sunucu
#: hatasıdır ve 500 KALIR — kapı bilerek DARdır.
FIELD_OVERFLOW_SQLSTATES = frozenset({"22003", "22001"})

FIELD_OVERFLOW_DETAIL = "Gönderilen değer alanın sınırını aşıyor"


async def _field_overflow_handler(request: Request, exc: DBAPIError) -> JSONResponse:
    """Kolon sınırını aşan değeri 422'ye çevirir; diğer DB hatalarını 500 BIRAKIR.

    🔴 `IntegrityError` DEĞİL, `DBAPIError` kaydedilir ve ayrım SQLSTATE'ten
    yapılır. Gerekçe ÖLÇÜLDÜ: asyncpg sürücüsünde `sqlalchemy.exc.DataError`
    HİÇ doğmaz — `dialects/postgresql/asyncpg.py:1010-1021`
    `_asyncpg_error_translate` yalnız altı sınıfı eşler,
    `NumericValueOutOfRangeError` düz `Error`a düşer ve SQLAlchemy onu
    `DBAPIError` olarak sarar. `DataError`e kaydedilmiş bir işleyici hiç
    çağrılmazdı (bekçi: tests/core/test_data_error_422.py).

    `IntegrityError` daha türemiş bir sınıf olarak AYRICA kayıtlıdır; Starlette
    işleyiciyi `type(exc).__mro__` üzerinde arar, bu yüzden 409 yolu gölgelenmez.
    """
    sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    if sqlstate not in FIELD_OVERFLOW_SQLSTATES:
        # Altyapı/sunucu hatası: bugünkü davranış (500) KORUNUR.
        raise exc
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": FIELD_OVERFLOW_DETAIL},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Alan hatalarını uygun HTTP koduna çeviren handler'ları kaydeder.

    Daha spesifik alt sınıflar önce kaydedilir; FastAPI istisna tipini tam eşleşmeyle
    bulamazsa MRO üzerinden yukarı çıkar, bu yüzden `DomainError` en genel yedek olarak
    en sonda kalmalıdır.
    """
    app.add_exception_handler(PermissionLockedError, _permission_locked_handler)
    app.add_exception_handler(ApprovalNotAllowedError, _approval_not_allowed_handler)
    app.add_exception_handler(DeletePreviewRequiredError, _delete_preview_required_handler)
    app.add_exception_handler(DeletePreviewStaleError, _delete_preview_stale_handler)
    app.add_exception_handler(DeleteFinancialPendingError, _delete_financial_pending_handler)
    app.add_exception_handler(NotFoundError, _not_found_handler)
    app.add_exception_handler(ProjectTypeMismatchError, _project_type_mismatch_handler)
    app.add_exception_handler(ProjectValidationError, _project_validation_handler)
    app.add_exception_handler(SiteValidationError, _site_validation_handler)
    app.add_exception_handler(EarnedValueValidationError, _earned_value_validation_handler)
    app.add_exception_handler(DiarySubmitBlockedError, _diary_submit_blocked_handler)
    app.add_exception_handler(DaysLockedError, _days_locked_handler)
    app.add_exception_handler(InventoryValidationError, _inventory_validation_handler)
    app.add_exception_handler(SectionTypeTakenError, _section_type_taken_handler)
    app.add_exception_handler(DuplicateError, _duplicate_error_handler)
    app.add_exception_handler(RelatedRecordsExistError, _related_records_exist_handler)
    app.add_exception_handler(ConflictError, _conflict_error_handler)
    app.add_exception_handler(BoqGroupSiteMismatchError, _boq_group_site_mismatch_handler)
    app.add_exception_handler(UnitValidationError, _unit_validation_handler)
    app.add_exception_handler(CustomerValidationError, _customer_validation_handler)
    app.add_exception_handler(PersonnelValidationError, _personnel_validation_handler)
    app.add_exception_handler(DocumentValidationError, _document_validation_handler)
    app.add_exception_handler(ProcurementValidationError, _procurement_validation_handler)
    app.add_exception_handler(PayrollValidationError, _payroll_validation_handler)
    app.add_exception_handler(EquipmentValidationError, _equipment_validation_handler)
    app.add_exception_handler(RoleValidationError, _role_validation_handler)
    app.add_exception_handler(OfferValidationError, _offer_validation_handler)
    app.add_exception_handler(InvoicingValidationError, _invoicing_validation_handler)
    app.add_exception_handler(TreasuryValidationError, _treasury_validation_handler)
    app.add_exception_handler(AccountingValidationError, _accounting_validation_handler)
    app.add_exception_handler(ApprovalValidationError, _approval_validation_handler)
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.add_exception_handler(IntegrityError, _integrity_error_handler)
    app.add_exception_handler(DBAPIError, _field_overflow_handler)

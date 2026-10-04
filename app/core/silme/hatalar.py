"""Silme motorunun hata sınıfları (SIL-B1). Çevirisi `exception_handlers.py`dedir."""

from app.core.errors import DomainError

PREVIEW_REQUIRED_DETAIL = "Silmeden önce önizleme alınmalı; önizlemeyi açıp onaylayın"
PREVIEW_STALE_DETAIL = "Silinecek kayıtlar değişti; önizlemeyi yenileyin"
FINANCIAL_PENDING_DETAIL = (
    "Bu kaydın bağlı mali kayıtları var; mali kayıt silme bir sonraki sürümde açılacak"
)

#: Yapısal kodlar — istemci metne bakmaz, `code` alanına bakar.
CODE_PREVIEW_REQUIRED = "preview_required"
CODE_PREVIEW_STALE = "preview_stale"
CODE_FINANCIAL_PENDING = "financial_pending"


class DeletePreviewRequiredError(DomainError):
    """DELETE'te `preview_token` yok — 428 (Precondition Required).

    Bağlı kayıtları birlikte silen bir uç, kullanıcının "şunlar da silinecek" listesini
    GÖRMEDEN çalışmaz (KARARLAR §1.7: önce liste, sonra onay). 422 değil 428: gövde/sorgu
    biçimi geçerlidir, eksik olan bir ÖN KOŞULdur.
    """


class DeletePreviewStaleError(DomainError):
    """`preview_token` silme anındaki ağaçla uyuşmuyor — 409.

    Önizleme ile silme arasında bağlı kayıt eklendi/silindi/taşındı. Kullanıcı bu listeyi
    görüp onaylamadı; önizleme yenilenmeli.
    """


class DeleteFinancialPendingError(DomainError):
    """Ağaçta MALİ kayıt var — 409 `financial_pending`. HİÇBİR ŞEY silinmez.

    GEÇİCİ KURAL (CEO eki, SIL-B1): mali kayıtların (fiş, fatura, ödeme, onaylı/ödenmiş
    hakediş…) silinmesi SIL-B2'nin işidir; o dilim gelene kadar mali bağı olan kayıt silinmez.
    Önizleme yine TAM gösterilir (`is_financial=true` gruplar). SIL-B2 bu hatayı ve
    `tests/modules/silme/test_silme_mali_kapi.py`yi kaldırır.
    """

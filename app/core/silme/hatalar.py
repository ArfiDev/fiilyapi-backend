"""Silme motorunun hata sınıfları (SIL-B1). Çevirisi `exception_handlers.py`dedir."""

from app.core.errors import DomainError

PREVIEW_REQUIRED_DETAIL = "Silmeden önce önizleme alınmalı; önizlemeyi açıp onaylayın"
PREVIEW_STALE_DETAIL = "Silinecek kayıtlar değişti; önizlemeyi yenileyin"

#: Yapısal kodlar — istemci metne bakmaz, `code` alanına bakar.
CODE_PREVIEW_REQUIRED = "preview_required"
CODE_PREVIEW_STALE = "preview_stale"


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

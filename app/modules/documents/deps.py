"""Belge modülünün FastAPI bağımlılıkları — depolama dikişi burada durur.

Uçlar ve servis katmanı somut `DbStorageBackend`i ASLA import etmez; ihtiyaç
duydukları tip `StorageBackend` arayüzüdür ve örneği buradan gelir. R2/S3'e
geçiş bu dosyadaki TEK satırın değişmesidir (spec §7 S1'in "tek sınıf" sözü).

Testler `app.dependency_overrides[get_storage_backend]` ile sahte bir backend
takar — kanıt `tests/documents/test_storage_backend.py`.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import DbSession, get_db
from app.modules.documents.storage import DbStorageBackend, StorageBackend


def get_storage_backend(session: DbSession) -> StorageBackend:
    """İstek başına depolama backend'i.

    Dönüş tipi bilinçli olarak ARAYÜZDÜR: bağımlılığı kullanan kod somut tipi
    tip düzeyinde de görmez, dolayısıyla ona bağlanamaz.
    """
    return DbStorageBackend(session)


def get_streaming_storage_backend(
    session: Annotated[AsyncSession, Depends(get_db, scope="request")],
) -> StorageBackend:
    """Akış (download) ucu için depolama backend'i — TEK `scope="request"` yeri.

    `DbStorageBackend.stream` gövdeyi parça parça DB'den okur; gövde function
    kapsamı kapandıktan SONRA üretildiği için oturum yanıt yazılırken açık
    kalmalıdır. Uç GET'tir ve yazma yapmaz ⇒ teardown'da gizlenecek hata yok.
    Uç, `DbSession` da taşıdığı için bilinçli bir İKİNCİ oturum açılır; yalnız
    blob okur. Yükleme uçları `get_storage_backend`i (function) kullanmalıdır:
    orada storage ve uç aynı oturumu paylaşmalıdır.
    """
    return DbStorageBackend(session)

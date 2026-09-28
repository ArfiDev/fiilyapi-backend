"""Yardımcı ASGI uygulamaları — `test_teardown_commit_http_olcum.py` gerçek bir
uvicorn süreciyle bunları çalıştırıp GERÇEK bir soket üzerinden ölçer.

Ayrı dosyada tutulma sebebi: `uvicorn <modül>:<uygulama>` alt süreç olarak
başlatılabilsin diye içe aktarılabilir bir modül gerekir.

## Ölçülen şey: ÜRETİM takma adının kapsamı

Rotalar `get_db`nin bir taklidini DOĞRUDAN `Depends(...)`e vermez; ÜRETİMDEKİ
`DbSession` takma adını (`app/core/db.py`) kullanır ve taklit
`app.dependency_overrides[get_db]` ile takılır. Böylece ölçülen kapsam,
takma adın taşıdığı `scope="function"`dır — takma ad değişirse ölçüm de değişir.
Taklit, `get_db`nin şekliyle aynıdır: `try` içinde `yield` + `commit`, ama
commit KASITLI olarak patlar.

Bu modülde `from __future__ import annotations` KASITLI olarak YOKTUR: kapanış
(closure) içindeki `Annotated[...]` imzası ertelenmiş değerlendirmede yerel
adları göremez ve FastAPI `db`yi sıradan bir zorunlu query parametresi sanır.
"""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import DbSession, get_db
from app.core.exception_handlers import register_exception_handlers

TEARDOWN_MESAJI = "teardown commit anında patladı"


class _PatlayanSession:
    def __init__(self, hata: Exception) -> None:
        self._hata = hata

    async def commit(self) -> None:
        raise self._hata


def _taklit_kur(hata_uretici):
    async def _get_db_taklit() -> AsyncGenerator[_PatlayanSession, None]:
        """`app/core/db.py:get_db` ile AYNI şekil: `try` içinde `yield` + commit."""
        session = _PatlayanSession(hata_uretici())
        try:
            yield session
            await session.commit()
        except Exception:
            raise

    return _get_db_taklit


def _runtime_hatasi() -> Exception:
    return RuntimeError(TEARDOWN_MESAJI)


def _integrity_hatasi() -> Exception:
    return IntegrityError("INSERT ...", {}, Exception(TEARDOWN_MESAJI))


def uygulama_kur(hata_uretici) -> FastAPI:
    """Üretim `DbSession` takma adını kullanan iki rotalı uygulama.

    * `/kaydet` — ÜRETİM takma adı (`db: DbSession`) → function kapsamı.
    * `/kaydet-request` — NEGATİF KONTROL: açıkça `scope="request"`.
    """
    uygulama = FastAPI()
    register_exception_handlers(uygulama)
    uygulama.dependency_overrides[get_db] = _taklit_kur(hata_uretici)

    @uygulama.post("/kaydet")
    async def kaydet(db: DbSession) -> dict[str, bool]:
        return {"ok": True}

    @uygulama.post("/kaydet-request")
    async def kaydet_request(
        db: Annotated[AsyncSession, Depends(get_db, scope="request")],
    ) -> dict[str, bool]:
        return {"ok": True}

    return uygulama


#: teardown `RuntimeError` — kayıtlı handler'ı olmayan hata → 500.
app_runtime = uygulama_kur(_runtime_hatasi)
#: teardown `IntegrityError` — kayıtlı `_integrity_error_handler` → 409.
app_integrity = uygulama_kur(_integrity_hatasi)

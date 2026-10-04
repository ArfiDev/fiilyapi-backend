import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded

from app.core.bootstrap import ensure_company, ensure_first_admin
from app.core.config import Settings, settings
from app.core.exception_handlers import register_exception_handlers
from app.core.project_access import gate_request_scope
from app.core.ratelimit import limiter, rate_limit_exceeded_handler
from app.core.router_registry import ROUTERS

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Bootstrap başarısız olursa API yine de ayağa kalksın (örn. /docs erişilebilir kalsın);
    # hatayı sessizce yutmuyoruz — logluyoruz.
    try:
        await ensure_first_admin()
    except Exception:
        logger.exception("İlk admin bootstrap'ı başarısız oldu")
    try:
        await ensure_company()
    except Exception:
        logger.exception("Sirket bootstrap'i basarisiz oldu")
    yield


def _configure_cors(app: FastAPI, cfg: Settings) -> None:
    """Env'de origin verilmişse sıkı CORS middleware'i ekler.

    Wildcard `*` + credentials birlikte KULLANILMAZ — origin'ler açık liste olmalı.
    Liste boşsa (dev varsayılanı) middleware hiç eklenmez.
    """
    origins = cfg.cors_origin_list
    if not origins:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


app = FastAPI(
    title="FİİL Yapı ERP API",
    version="0.1.0",
    lifespan=lifespan,
    # IZN-B3: her istekte proje kapı bağlamını sıfırlar (`core/project_access`).
    dependencies=[Depends(gate_request_scope, scope="function")],
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
register_exception_handlers(app)
_configure_cors(app, settings)

# 🔴 Router listesi ve **SIRASI** `app/core/router_registry.py`dedir (AI-0a T1).
# Sıra davranıştır (FastAPI rotaları kayıt sırasına göre eşler) ve gerekçeleri —
# üç router-arası gölgeleme çifti dahil — o dosyanın docstring'inde durur. Burada
# döngüden başka bir şey OLMAMALIDIR: ikinci bir tüketici (`readplane.py`) aynı
# listeyi okuyor ve iki listenin ayrışması sessiz bir kusur sınıfıdır.
for _router in ROUTERS:
    app.include_router(_router)


# ⚠️ `/health` BİLEREK satır-içidir ve `ROUTERS`ta YOKTUR — ama uygulamanın GET
# kümesinin İÇİNDEDİR. Okuma düzlemi bu yüzden registry'den değil, uygulamanın
# **rota tablosundan** türetilir (`app/modules/ai/readplane.py`).
@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}

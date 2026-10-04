"""SIL-B1 — router bekçisi: HER `DELETE` ucu `require_system_admin` kapısını taşımalıdır.

KARARLAR §1.7 (K4): silme YALNIZ Sistem Yöneticisi'nindir, HER KOŞULDA, istisnasız. Kapıyı
unutulan tek bir DELETE ucu o kararı sessizce deler; bu test uygulamanın GERÇEK rota tablosunu
tarar (kelime aramasıyla değil) ve eksik kapıyı KIRMIZI yapar.

Pozitif kontrol: aynı tarayıcı, kapısız bir DELETE ucu içeren sentetik uygulamada o ucu BULUR —
yani tarayıcının kendisi kör değildir ("hiçbir şeyi hiçbir şeyle karşılaştırmak" tuzağı).
"""

from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import iter_route_contexts

from app.core.access import AccessLevel
from app.core.permissions import require_permission, require_system_admin
from app.main import app

#: Kapıyı taşımasa da DELETE olabilecek istisna uçlar. BOŞ OLMALIDIR (K4: istisna yok).
ISTISNA_YOLLAR: frozenset[str] = frozenset()


def _kapi_mi(cagri) -> bool:
    return getattr(cagri, "__qualname__", "").startswith("require_system_admin.<locals>")


def _bagimliliklar(dep: Dependant):
    for alt in dep.dependencies:
        yield alt
        yield from _bagimliliklar(alt)


def delete_uclari_kapisiz(uygulama: FastAPI) -> list[str]:
    eksik = []
    for ctx in iter_route_contexts(uygulama.routes):
        if not ctx.methods or "DELETE" not in ctx.methods:
            continue
        if ctx.path in ISTISNA_YOLLAR:
            continue
        if not any(_kapi_mi(d.call) for d in _bagimliliklar(ctx.dependant)):
            eksik.append(ctx.path)
    return sorted(eksik)


def _delete_yollari(uygulama: FastAPI) -> list[str]:
    return [
        c.path for c in iter_route_contexts(uygulama.routes) if c.methods and "DELETE" in c.methods
    ]


def test_her_delete_ucu_sistem_yoneticisi_kapisini_tasir() -> None:
    assert delete_uclari_kapisiz(app) == []


def test_tarayici_gercek_rota_tablosunu_goruyor() -> None:
    """Boş tablo üzerinde "eksik yok" demek hiçbir şey kanıtlamaz: bilinen uçlar listede olmalı."""
    yollar = set(_delete_yollari(app))
    assert len(yollar) >= 47
    assert {
        "/sites/{site_id}",
        "/users/{user_id}",
        "/ai/conversations/{conversation_id}",
        "/company/logo",
        "/offers/{offer_id}",
        "/admin/silme/{kind}/{record_id}",
    } <= yollar


def test_pozitif_kontrol_kapisiz_delete_ucunu_yakalar() -> None:
    sentetik = FastAPI()
    r = APIRouter()

    @r.delete("/kapili", dependencies=[require_system_admin()])
    async def kapili() -> None: ...

    @r.delete("/eski-kapi", dependencies=[require_permission("sites", AccessLevel.admin)])
    async def eski_kapi() -> None: ...

    @r.delete("/kapisiz")
    async def kapisiz() -> None: ...

    @r.delete("/dolayli", dependencies=[Depends(lambda: None)])
    async def dolayli() -> None: ...

    sentetik.include_router(r)

    assert delete_uclari_kapisiz(sentetik) == ["/dolayli", "/eski-kapi", "/kapisiz"]


def test_istisna_listesi_bos() -> None:
    assert ISTISNA_YOLLAR == frozenset()

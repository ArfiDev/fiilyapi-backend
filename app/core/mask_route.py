"""GLOBAL maske rota sınıfı (IZN-B4): her router `route_class=MaskeRotasi` taşır.

## Neden ROTA düzeyinde

Eski kapsam maskesi (`scoped_route.kapsam_rotasi`) yalnız 6 izin modülünün routerlarındaydı ve her
router iki parça gerektiriyordu (rota sınıfı + `kapsam_kapisi` bağımlılığı). Yeni maske TÜM
routerlara yayılır ve TEK parçadır: rota sınıfı kendi bağımlılığını kendisi ekler
(`dependencies=[maske bağlamı]`), yani "sınıfı koydum bağımlılığı unuttum" hâli doğamaz. Bekçisi
`tests/core/test_hassas_alan_bekcisi.py`: `ROUTERS`un HER rotası `MaskeRotasi`dir.

## Bağlam LAZY çözülür

Bağımlılık yalnız `(session, request)`i bir `ContextVar`a koyar; etkin rolün kategorileri
(`mask_context.kumeleri_coz`, ≤ 3 sorgu) ANCAK yanıtın şeması hassas alan taşıyorsa ve çıktı
gerçekten maskelenecekse çözülür. `/auth/me`, `/users`, silme uçları gibi hassas alanı olmayan
uçlar SIFIR ek sorgu yer.

`ContextVar` + `yield`li bağımlılık + `reset`, `scoped_route.kapsam_bagimligi_kur`un ÖLÇÜLMÜŞ
desenidir (keep-alive bağlantıda ardışık istekler AYNI task'ta koşar; `reset` olmazsa bir isteğin
bağlamı sonrakine taşınır).

## Kimlik

Bağlam kullanıcıyı `Authorization: Bearer` başlığından `deps.get_current_user` ile çözer
(`HTTPBearer` bağımlılığı OpenAPI'ye `security` yazardı; başlığı elle okumak sözleşmeyi
değiştirmez). Çözülemeyen kimlikte FAIL-CLOSED: `HEPSI_GIZLI`. Kimliksiz bir ucun hassas alan
döndürmesi zaten bir kusurdur; gizlemek doğru yöndür.

## Hassas olmayan yanıt

Şema ağacında HİÇ hassas etiket yoksa sarmalayıcı yanıta dokunmaz (`sema_plani`).

## 🔴 Maskelenemeyen çıktılar

`Response` döndüren uçlar (xlsx/csv/dosya) maskeden GEÇEMEZ: elde baytlar var, alan bilgisi yok.
Kural değişmedi: gövdesini kendi üreten uç maskeyi KENDİSİ uygular (`maskele_baglamli`) ve
`tests/core/test_hassas_alan_bekcisi.py::EXPORT_UCLARI` kaydı her export ucunu açıkça listeler.

## Yazma kapısı

Bkz. `field_mask` docstring'i: gizli kategorili alanı gövdede DOLU gönderen aktör → 403. Kapı
uç çağrılmadan ÖNCE koşar (yan etki yok), yalnız GÖVDEDE hassas etiketli alan DOLU ise DB'ye gider.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, TypeAdapter
from starlette.responses import Response

from app.core.db import DbSession
from app.core.field_mask import (
    HEPSI_GIZLI,
    MaskeKumeleri,
    gizli_mi,
    maskele,
    sema_plani,
    semalar_icinde,
    yazilan_hassas_alanlar,
)
from app.core.mask_context import kumeleri_coz

__all__ = ["MaskeBaglami", "MaskeRotasi", "maskele_baglamli"]

_GUVENLI_METOTLAR = frozenset({"GET", "HEAD", "OPTIONS"})

_YAZMA_REDDI = "Bu alanları düzenleme yetkiniz yok"


class MaskeBaglami:
    """Bir isteğin (oturum, istek) çifti + tembel çözülen kümeler."""

    def __init__(self, session: Any, request: Request) -> None:
        self._session = session
        self._request = request
        self._kumeler: MaskeKumeleri | None = None

    async def _kullanici(self) -> object | None:
        from app.core.deps import get_current_user  # döngüyü önler

        scheme, _, token = self._request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        try:
            return await get_current_user(
                HTTPAuthorizationCredentials(scheme=scheme, credentials=token), self._session
            )
        except HTTPException:
            return None

    async def kumeler(self) -> MaskeKumeleri:
        if self._kumeler is None:
            user = await self._kullanici()
            self._kumeler = (
                HEPSI_GIZLI
                if user is None
                else await kumeleri_coz(self._session, user, self._request)
            )
        return self._kumeler


_BAGLAM: ContextVar[MaskeBaglami | None] = ContextVar("_hassas_alan_baglami", default=None)


async def _maske_baglami_bagimliligi(
    request: Request, session: DbSession
) -> AsyncGenerator[None, None]:
    """Rota bağımlılığı: bağlamı köprüye yazar, isteğin ÖMRÜYLE sınırlar (`reset`)."""
    token = _BAGLAM.set(MaskeBaglami(session, request))
    try:
        yield
    finally:
        _BAGLAM.reset(token)


async def _kumeler_simdi() -> MaskeKumeleri:
    baglam = _BAGLAM.get()
    return HEPSI_GIZLI if baglam is None else await baglam.kumeler()


async def maskele_baglamli[TModel: BaseModel](model: TModel) -> TModel:
    """Gövdesini KENDİ üreten uçlar (xlsx/csv/dosya) için ELLE maske: modeli dosyaya çevirmeden
    ÖNCE buradan geçirin. Etkin rol isteğin bağlamından çözülür (yanlış rolle çağırmak mümkün
    değildir: parametre YOK)."""
    return maskele(model, await _kumeler_simdi())


def _hazir_mi(sonuc: Any) -> bool:
    if isinstance(sonuc, BaseModel):
        return True
    return isinstance(sonuc, list | tuple) and all(isinstance(o, BaseModel) for o in sonuc)


class _RotaDurumu:
    """Rota kurulduktan SONRA doldurulur (`response_model` `APIRoute.__init__`te çözülür)."""

    def __init__(self) -> None:
        self.hassas = False
        self._response_model: Any = None
        self._adapter: TypeAdapter[Any] | None = None

    def baslat(self, response_model: Any) -> None:
        self._response_model = response_model
        self.hassas = response_model is not None and any(
            sema_plani(sema).hassas_agac for sema in semalar_icinde(response_model)
        )

    def normalize(self, sonuc: Any) -> Any:
        """Uç ham nesne/sözlük döndürüyorsa FastAPI'nin yapacağı doğrulamayı ÖNCE yapar: maske
        yalnız model örneğinde çalışır, ham sözlük maskesiz sızardı."""
        if _hazir_mi(sonuc):
            return sonuc
        if self._adapter is None:
            self._adapter = TypeAdapter(self._response_model)
        return self._adapter.validate_python(sonuc, from_attributes=True)


async def _yazma_kapisi(kwargs: dict[str, Any]) -> None:
    adaylar = [alan for deger in kwargs.values() for alan in yazilan_hassas_alanlar(deger)]
    if not adaylar:
        return
    kumeler = await _kumeler_simdi()
    ihlal = sorted({a.ad for a in adaylar if gizli_mi(a, kumeler.varsayilan)})
    if ihlal:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"{_YAZMA_REDDI}: {', '.join(ihlal)}"
        )


def _sarmala(
    endpoint: Callable[..., Any], yazma: bool, durum: _RotaDurumu
) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(endpoint)
    async def _sarili(*args: Any, **kwargs: Any) -> Any:
        if yazma:
            await _yazma_kapisi(kwargs)  # UÇ ÇAĞRILMADAN ÖNCE: yetki kapısı işten önce sorulur
        sonuc = endpoint(*args, **kwargs)
        if inspect.isawaitable(sonuc):
            sonuc = await sonuc
        if not durum.hassas or sonuc is None or isinstance(sonuc, Response):
            return sonuc
        sonuc = durum.normalize(sonuc)
        return maskele(sonuc, await _kumeler_simdi())

    return _sarili


class MaskeRotasi(APIRoute):
    """`APIRouter(route_class=MaskeRotasi)`: yanıtı etkin rolün maskesinden geçirir, yazma
    gövdesinde gizli kategorili alanı reddeder. Bağlam bağımlılığını KENDİSİ ekler."""

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        metotlar = {m.upper() for m in (kwargs.get("methods") or ("GET",))}
        durum = _RotaDurumu()
        kwargs["dependencies"] = [
            Depends(_maske_baglami_bagimliligi, scope="function"),
            *(kwargs.get("dependencies") or []),
        ]
        super().__init__(
            path, _sarmala(endpoint, bool(metotlar - _GUVENLI_METOTLAR), durum), **kwargs
        )
        durum.baslat(self.response_model)

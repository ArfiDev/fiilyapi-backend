"""🔴 YAPISAL BEKÇİ (TEARDOWN-B1): `get_db` her uçta TEK ve `function` kapsamlıdır.

## Neden

FastAPI bağımlılık önbellek anahtarı `(call, scopes, computed_scope)`tur
(`fastapi/dependencies/models.py:92-96`). Aynı istekte `get_db` iki farklı
kapsamla (`function` + `request`) istenirse ÖNBELLEK ÇAKIŞMAZ ve istek başına
İKİ oturum açılır. Ayrıca `scope="request"` teardown'u (commit) yanıt
yazıldıktan SONRA koşturur: başarısız yazma istemciye 200 görünür
(`test_teardown_commit_http_olcum.py`). `conftest.py`nin `get_db` override'ı her
çağrıda AYNI `db_session`ı döndürdüğü için mevcut testler bu hatayı GÖREMEZ;
bu yüzden yapısal bekçi şarttır.

## Üç bekçi

a) ROTA bekçisi — ana uygulama + AI okuma düzlemi: her rotanın bağımlılık
   ağacında `call is get_db` düğümlerinin önbellek anahtarı kümesi tam
   `{function}` olmalı (⇒ `get_current_user`/`require_permission`/kapsam alt
   bağımlılıkları dahil TEK oturum). Tek izinli istisna
   `GET /documents/{document_id}/download`: `{function, request}` (bilinçli
   ikinci oturum; yazmasız akış ucu). İzin listesi bayat kalırsa da kırmızı.
b) KAYNAK bekçisi — `app/` altındaki AST `Depends(get_db...)` çağrıları yalnız
   izinli dosyalarda ve beklenen kapsamla.
c) DAVRANIŞ kanıtı `test_getdb_tek_oturum.py`de (DB'li).

⚠️ FastAPI 0.141 `include_router`ı `_IncludedRouter` olarak tutar; `app.routes`ı
düz dolaşmak alt yönlendirici rotalarını GÖRMEZ. Tarama `iter_route_contexts`
ve `ctx.dependant` (efektif bağlam) ile yapılır; ayrıca "get_db taşıyan rota
sayısı > 0" kontrolü sessiz-boş taramayı yakalar.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant, _get_cache_key
from fastapi.routing import APIRoute, iter_route_contexts

from app.core.db import get_db
from app.main import app
from app.modules.ai.readplane import build_read_plane

FUNCTION = "function"
REQUEST = "request"

#: (yöntem, yol) -> izinli kapsam kümesi. İzinli rota GET (yazmasız) olmalıdır.
IZINLI_KAPSAM_ISTISNALARI: dict[tuple[str, str], frozenset[str]] = {
    ("GET", "/documents/{document_id}/download"): frozenset({FUNCTION, REQUEST}),
}

APP_DIZINI = Path(__file__).resolve().parents[2] / "app"

#: dosya (app/'e göre) -> `Depends(get_db...)` çağrılarının beklenen kapsamı.
IZINLI_KAYNAK_DOSYALARI: dict[str, str] = {
    "core/db.py": FUNCTION,
    "modules/documents/deps.py": REQUEST,
}


# --------------------------- a) rota bekçisi ---------------------------


def _get_db_kapsamlari(dependant: Dependant, gorulen: set[int] | None = None) -> set[str]:
    gorulen = gorulen if gorulen is not None else set()
    if id(dependant) in gorulen:
        return set()
    gorulen.add(id(dependant))
    kapsamlar: set[str] = set()
    if dependant.call is get_db:
        # anahtar = (call, oauth_scopes, computed_scope)
        kapsamlar.add(_get_cache_key(dependant=dependant)[2])
    for alt in dependant.dependencies:
        kapsamlar |= _get_db_kapsamlari(alt, gorulen)
    return kapsamlar


def _rota_kapsamlari(uygulama: FastAPI) -> dict[tuple[str, str], set[str]]:
    """(yöntem, yol) -> get_db kapsam kümesi (yalnız get_db taşıyan rotalar)."""
    sonuc: dict[tuple[str, str], set[str]] = {}
    for ctx in iter_route_contexts(uygulama.routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        dependant = ctx.dependant or ctx.original_route.dependant
        kapsamlar = _get_db_kapsamlari(dependant)
        if not kapsamlar:
            continue
        for yontem in ctx.methods:
            sonuc[(yontem, ctx.path)] = kapsamlar
    return sonuc


def _uygulamalar() -> list[tuple[str, FastAPI]]:
    return [("ana", app), ("okuma_duzlemi", build_read_plane(app))]


@pytest.mark.parametrize(("ad", "uygulama"), _uygulamalar(), ids=["ana", "okuma_duzlemi"])
def test_HER_rotada_get_db_yalniz_function_kapsamlidir(ad: str, uygulama: FastAPI) -> None:
    """🔴 ASIL BEKÇİ. Mutasyon: `DbSession`dan `scope="function"` düşerse ya da
    bir uçta çıplak `Depends(get_db)` kullanılırsa o rota `{request}` /
    `{function, request}` olur ve test KIRMIZI olur."""
    kapsamlar = _rota_kapsamlari(uygulama)

    assert kapsamlar, (
        f"[{ad}] hiçbir rotada get_db bulunamadı — tarayıcı sessizce boş "
        "dönüyor (`_IncludedRouter` dolaşımı bozulmuş olabilir)."
    )

    ihlaller = {
        rota: sorted(k)
        for rota, k in kapsamlar.items()
        if k != IZINLI_KAPSAM_ISTISNALARI.get(rota, frozenset({FUNCTION}))
    }
    assert not ihlaller, (
        f"[{ad}] get_db kapsamı beklenenden farklı rotalar (⇒ istek başına birden "
        "çok oturum ya da teardown'u yanıttan sonra koşan commit): "
        f"{ihlaller}. Uçlar `DbSession` takma adını kullanmalıdır."
    )


def test_izin_listesi_bayat_girdi_icermez_ve_yalniz_GET_icerir() -> None:
    """İstisna listesi gerçek rotalara işaret etmeli; yazma yöntemi izinli OLAMAZ."""
    ana = _rota_kapsamlari(app)
    for (yontem, yol), beklenen in IZINLI_KAPSAM_ISTISNALARI.items():
        assert yontem == "GET", f"İstisna yazmasız (GET) olmalı: {(yontem, yol)}"
        assert (yontem, yol) in ana, (
            f"BAYAT izin girdisi: {(yontem, yol)} artık get_db taşıyan bir rota değil."
        )
        assert ana[(yontem, yol)] == set(beklenen), (
            f"İstisna girdisi güncel değil: {(yontem, yol)} gerçekte {sorted(ana[(yontem, yol)])}"
        )


# --------------------------- b) kaynak bekçisi ---------------------------


def _depends_get_db_cagrilari(kaynak: str) -> Iterator[str]:
    """Her `Depends(get_db, ...)` çağrısı için kapsam (`scope=` yoksa "default")."""
    for dugum in ast.walk(ast.parse(kaynak)):
        if not isinstance(dugum, ast.Call):
            continue
        ad = dugum.func
        if not (
            (isinstance(ad, ast.Name) and ad.id == "Depends")
            or (isinstance(ad, ast.Attribute) and ad.attr == "Depends")
        ):
            continue
        if not (
            dugum.args and isinstance(dugum.args[0], ast.Name) and dugum.args[0].id == "get_db"
        ):
            continue
        kapsam = "default"
        for kw in dugum.keywords:
            if kw.arg == "scope" and isinstance(kw.value, ast.Constant):
                kapsam = str(kw.value.value)
        yield kapsam


def _kaynak_cagrilari() -> dict[str, list[str]]:
    sonuc: dict[str, list[str]] = {}
    for yol in sorted(APP_DIZINI.rglob("*.py")):
        kapsamlar = list(_depends_get_db_cagrilari(yol.read_text(encoding="utf-8")))
        if kapsamlar:
            sonuc[yol.relative_to(APP_DIZINI).as_posix()] = kapsamlar
    return sonuc


def test_Depends_get_db_yalniz_izinli_dosyalarda_ve_beklenen_kapsamla() -> None:
    """🔴 AST tabanlı (yorum/docstring metni sayılmaz). Mutasyon: izinsiz bir
    dosyaya `Depends(get_db)` eklenirse ya da `db.py` kapsamı düşerse KIRMIZI."""
    cagrilar = _kaynak_cagrilari()

    izinsiz = {d: k for d, k in cagrilar.items() if d not in IZINLI_KAYNAK_DOSYALARI}
    assert not izinsiz, (
        f"İzinsiz `Depends(get_db...)` çağrıları: {izinsiz}. Uçlar/bağımlılıklar "
        "`DbSession` takma adını kullanmalıdır (`from app.core.db import DbSession`)."
    )

    bayat = set(IZINLI_KAYNAK_DOSYALARI) - set(cagrilar)
    assert not bayat, f"BAYAT izin girdisi (dosyada artık Depends(get_db) yok): {bayat}"

    yanlis_kapsam = {
        d: k
        for d, k in cagrilar.items()
        if any(kapsam != IZINLI_KAYNAK_DOSYALARI[d] for kapsam in k)
    }
    assert not yanlis_kapsam, (
        f"Beklenen kapsam {IZINLI_KAYNAK_DOSYALARI} ile uyuşmayan çağrılar: {yanlis_kapsam}"
    )

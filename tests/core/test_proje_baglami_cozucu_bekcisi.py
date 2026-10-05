"""IZN-B3 — proje bağlamı çözücü BEKÇİSİ: disipline duyarlı (`DisciplineScoped`) her rotanın yol
parametresi `projects.context.RESOLVERS`ta bir çözücüye sahiptir.

Neden: disiplin PROJE BAŞINA atanır; çözücüsü olmayan bir rota SESSİZCE "çok proje" kapsamına düşer
(kısıtlı kişi için fail-closed ama yanlış: o projede kısıtsız olması gerekirken her şeyi göremez).
Yeni bir kapsamlı uç eklenip çözücü unutulursa bu test kırmızı verir. Parametresiz (liste) rotalar
bilinçli olarak çok proje kapsamıyla çalışır ve `PROJESIZ_ROTALAR`da gerekçesiyle durur.
"""

from __future__ import annotations

import re

from fastapi.routing import APIRoute, iter_route_contexts

from app.core.discipline_deps import resolve_discipline_scope
from app.main import app
from app.modules.projects.context import RESOLVERS
from tests.core.test_disiplin_rota_bekcisi import _isaretli

PARAM = re.compile(r"{(\w+)}")

#: Yol parametresi olmayan kapsamlı rotalar: çok proje kapsamı (kısıtlı olduğu projeler haritası).
PROJESIZ_ROTALAR = {
    ("GET", "/projects"): "proje kartları: `physical_for_projects` kapsamı proje başına böler",
    ("POST", "/projects"): "yeni proje; kartı çok proje kapsamıyla kurulur",
    ("GET", "/dashboard/summary"): "panel: kısıtlı olduğu herhangi bir projede hakediş kartı gizli",
    ("GET", "/progress-payments"): "liste: kısıtlı olduğu herhangi bir projede 403 (Ü2)",
    ("GET", "/subcontractor-progress-payments"): "liste: aynı kural (Ü2)",
    ("GET", "/subcontractor-progress-payments/summary"): "özet: aynı kural (Ü2)",
}

#: Proje bağlamı yol parametresinden DEĞİL başka yoldan çözülenler (yok).
COZUCUSUZ_PARAMLI: dict[tuple[str, str], str] = {}


def _kapsamli_rotalar() -> list[tuple[str, str]]:
    sonuc: list[tuple[str, str]] = []
    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        dependant = ctx.dependant or ctx.original_route.dependant
        if not _isaretli(dependant):
            continue
        sonuc += [(yontem, ctx.path) for yontem in sorted(ctx.methods)]
    return sonuc


def _onek(yol: str) -> str:
    return "/" + yol.lstrip("/").split("/", 1)[0]


def test_tarama_bos_degil() -> None:
    assert len(_kapsamli_rotalar()) > 80, "kapsamlı rota taraması sessiz-boş"


def test_her_kapsamli_rota_cozuculu_ya_da_projesizdir() -> None:
    sorunlu = []
    for yontem, yol in _kapsamli_rotalar():
        paramlar = PARAM.findall(yol)
        if not paramlar:
            if (yontem, yol) not in PROJESIZ_ROTALAR:
                sorunlu.append((yontem, yol, "parametresiz ama PROJESIZ_ROTALAR'da yok"))
            continue
        if not any((_onek(yol), p) in RESOLVERS for p in paramlar):
            sorunlu.append((yontem, yol, f"çözücü yok: {paramlar}"))
    assert not sorunlu, sorunlu


def _tum_rota_parametreleri() -> set[tuple[str, str]]:
    sonuc: set[tuple[str, str]] = set()
    for ctx in iter_route_contexts(app.routes):
        if isinstance(ctx.original_route, APIRoute):
            sonuc |= {(_onek(ctx.path), p) for p in PARAM.findall(ctx.path)}
    return sonuc


def test_cozucu_tablosu_ve_projesiz_liste_bayat_girdi_icermez() -> None:
    bayat = sorted(set(RESOLVERS) - _tum_rota_parametreleri())
    assert not bayat, f"hiçbir rota kullanmıyor: {bayat}"
    assert set(PROJESIZ_ROTALAR) <= set(_kapsamli_rotalar()), "bayat projesiz rota"
    assert resolve_discipline_scope  # bağımlılık adı değişirse import kırılır

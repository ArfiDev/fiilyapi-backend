"""DSC-B5 S3 — AI: disiplin KISITLI kullanıcıya hakediş araçları HİÇ sunulmaz.

Tanım tek yerdedir (`ToolSpec.disiplin_kisitliya_kapali`); rota bağımlılık ağacıyla
(`RequireUnrestricted`) eşitliği burada kilitlenir — yeni bir hakediş aracı bayrağı
unutursa ya da bir araç Ü2 kapılı uca taşınırsa kırmızı.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute, iter_route_contexts

from app.core.discipline_deps import require_unrestricted
from app.main import app
from app.modules.ai import audit as ai_audit
from app.modules.ai.registry import ToolRegistry
from app.modules.ai.result import Ok, ToolError
from app.modules.ai.tools.catalog import READ_TOOLS
from tests._disiplin_dunyasi import kur
from tests.core.test_disiplin_rota_bekcisi import _bagimlilik_agacinda
from tests.discipline_scope._b4_dunya import pm_atamasiz_ekle

pytestmark = pytest.mark.asyncio

HAKEDIS_ARACLARI = {"isveren_hakedisleri", "taseron_hakedisleri"}


@pytest.fixture(autouse=True)
def _denetim_sussun(monkeypatch):
    async def _sahte(**kwargs):
        return None

    monkeypatch.setattr(ai_audit, "record_tool_call", _sahte)


@pytest.fixture
async def dunya(seeded_db, client, user_factory, project_factory):
    d = await kur(seeded_db, client, user_factory, project_factory)
    await pm_atamasiz_ekle(seeded_db, client, user_factory, d)
    return d


def _u2_kapili_yollar() -> set[str]:
    kapili: set[str] = set()
    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        if _bagimlilik_agacinda(
            ctx.dependant or ctx.original_route.dependant, require_unrestricted
        ):
            kapili.add(ctx.path)
    return kapili


async def test_bayrak_rota_bagimlilik_agaciyla_esit() -> None:
    kapili = _u2_kapili_yollar()
    assert kapili, "tarama boş"
    for spec in READ_TOOLS:
        turetilen = any(uc in kapili for uc in spec.ucler)
        assert spec.disiplin_kisitliya_kapali == turetilen, spec.ad
    assert {s.ad for s in READ_TOOLS if s.disiplin_kisitliya_kapali} == HAKEDIS_ARACLARI


async def test_kisitli_katalogunda_hakedis_araclari_yok_atamasizda_var(
    dunya, actor_factory
) -> None:
    kayit = ToolRegistry(READ_TOOLS)
    civil = {s.ad for s in kayit.katalog(await actor_factory(dunya.kullanici["civil"]))}
    pm = {s.ad for s in kayit.katalog(await actor_factory(dunya.kullanici["pm_atamasiz"]))}
    assert not (civil & HAKEDIS_ARACLARI), civil
    assert HAKEDIS_ARACLARI <= pm, pm
    assert civil == pm - HAKEDIS_ARACLARI  # yalnız hakediş araçları düşer


async def test_kisitli_dogrudan_cagirirsa_kontrollu_ret(
    dunya, transport_factory, actor_factory
) -> None:
    kayit = ToolRegistry(READ_TOOLS)
    for arac in sorted(HAKEDIS_ARACLARI):
        ret = await kayit.invoke(
            arac_adi=arac,
            argumanlar={},
            actor=await actor_factory(dunya.kullanici["civil"]),
            transport=transport_factory(dunya.kullanici["civil"]),
        )
        assert isinstance(ret, ToolError) and ret.kod == "yetkisiz_arac", (arac, ret)
        pozitif = await kayit.invoke(
            arac_adi=arac,
            argumanlar={},
            actor=await actor_factory(dunya.kullanici["pm_atamasiz"]),
            transport=transport_factory(dunya.kullanici["pm_atamasiz"]),
        )
        assert not isinstance(pozitif, ToolError), (
            arac,
            pozitif,
        )  # boş dünya: ScopedEmpty olabilir


async def test_gosterge_ozeti_kisitlida_hakedis_portfoyu_kapali(
    dunya, transport_factory, actor_factory
) -> None:
    """Dünyada gecikmiş hakediş YOK → risk sayısı eşit kalır (gecikme düşümü
    `tests/discipline_scope/test_b5_panel.py` kaynak durumuyla kanıtlanır); portföy kapanır."""
    kayit = ToolRegistry(READ_TOOLS)
    sonuc = {}
    for ad in ("civil", "pm_atamasiz"):
        ret = await kayit.invoke(
            arac_adi="gosterge_ozeti",
            argumanlar={},
            actor=await actor_factory(dunya.kullanici[ad]),
            transport=transport_factory(dunya.kullanici[ad]),
        )
        assert isinstance(ret, Ok), (ad, ret)
        sonuc[ad] = ret.data
    assert sonuc["civil"]["risk_notu"] == sonuc["pm_atamasiz"]["risk_notu"]
    assert sonuc["civil"]["portfoy"] != sonuc["pm_atamasiz"]["portfoy"]

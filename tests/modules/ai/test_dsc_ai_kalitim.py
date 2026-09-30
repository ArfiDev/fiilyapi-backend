"""DSC-B4 ADIM 0 — AI KALITIM ÖLÇÜMÜ: disiplin süzgeci AI araçlarına UÇTAN KALITILIYOR mu?

AI araçları (`ToolRegistry(READ_TOOLS).invoke` + gerçek `build_read_plane`) kendi süzgeçlerini
YAZMAZ; uçları çağırır. Bu dosya, disiplin dünyasında (`tests/_disiplin_dunyasi.kur`) civil
(KAB) / elek (DUV) / pm_atamasiz aktörlerinin araç çıktılarını ölçer:

* `is_kalemleri` ve `gunluk_kayit`: uç B1'de süzülmüştür; araç KALITIR (Civil yalnız KAB kalemini
  görür, toplamlar Σ ile atamasıza eşittir).
* `santiye_detayi` / `projeleri_listele` / `proje_detayi`: araç, disipline duyarlı bir alanı
  yüzeye ÇIKARMIYOR (aynı uç, araç alanı yüzeye çıkarmıyor) → Civil çıktısı == pm_atamasiz çıktısı.
  B4 uçlarda süzgeç koysa bile bu böyle KALMALI.
* `gosterge_ozeti`: stok kararı gereği disiplinsiz → Civil == pm_atamasiz (risk sayısı eşit).

POZİTİF KONTROL: aynı roldeki (project_manager) `pm_atamasiz`. F1: izin kapısı maskelemesi
dışlanır — iki PM de aynı izinlerle aracı çağırabilir; fark yalnız disiplin atamasıdır.
"""

from __future__ import annotations

import pytest

from app.modules.ai import audit as ai_audit
from app.modules.ai.registry import ToolRegistry
from app.modules.ai.result import Ok
from app.modules.ai.tools.catalog import READ_TOOLS
from tests._disiplin_dunyasi import kur
from tests.discipline_scope._b4_dunya import pm_atamasiz_ekle

pytestmark = pytest.mark.asyncio


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


async def _cagir(arac, user, transport_factory, actor_factory, **argumanlar):
    sonuc = await ToolRegistry(READ_TOOLS).invoke(
        arac_adi=arac,
        argumanlar=argumanlar,
        actor=await actor_factory(user),
        transport=transport_factory(user),
    )
    assert isinstance(sonuc, Ok), f"{arac}: {type(sonuc).__name__} {sonuc.mesaj()}"
    return sonuc.data


def _kalemler(veri) -> list[dict]:
    return [k for g in veri["gruplar"] for k in g["items"]]


async def test_is_kalemleri_civil_yalniz_kendi_kalemi(dunya, transport_factory, actor_factory):
    civil = dunya.kullanici["civil"]
    veri = await _cagir(
        "is_kalemleri", civil, transport_factory, actor_factory, site_id=str(dunya.santiye.id)
    )
    metin = str(veri)
    assert [k["code"] for k in _kalemler(veri)] == ["01.001"]
    assert "Betonarme" in metin
    assert "Beton" in metin
    for yasak in ("02.001", "03.001", "Tuğla", "Boya", "Duvar"):
        assert yasak not in metin, yasak
    assert veri["kalem_sayisi"] == 1


async def test_is_kalemleri_toplam_civil_elek_i3_esittir_atamasiz(
    dunya, transport_factory, actor_factory
):
    site = str(dunya.santiye.id)
    civil, elek, pm = (dunya.kullanici[a] for a in ("civil", "elek", "pm_atamasiz"))
    v_civil, v_elek, v_pm = [
        await _cagir("is_kalemleri", u, transport_factory, actor_factory, site_id=site)
        for u in (civil, elek, pm)
    ]
    from decimal import Decimal

    i3 = Decimal(40) * Decimal(30)
    civil_toplam, elek_toplam, atamasiz_toplam = (
        Decimal(str(v["grand_total"])) for v in (v_civil, v_elek, v_pm)
    )
    assert civil_toplam == Decimal(1000)
    assert elek_toplam == Decimal(1000)
    assert civil_toplam + elek_toplam + i3 == atamasiz_toplam
    assert v_pm["kalem_sayisi"] == 3


async def test_gunluk_kayit_lines_total_toplami(dunya, transport_factory, actor_factory):
    site = str(dunya.santiye.id)

    async def toplam(kullanici: str):
        veri = await _cagir(
            "gunluk_kayit",
            dunya.kullanici[kullanici],
            transport_factory,
            actor_factory,
            site_id=site,
        )
        return {k["entry_date"]: k["lines_total"] for k in veri}

    from decimal import Decimal

    civil, elek, pm = await toplam("civil"), await toplam("elek"), await toplam("pm_atamasiz")
    assert set(civil) == set(elek) == set(pm)
    # Disiplinsiz kalan (I3 + bağı kopmuş NULL satır; Ü1): 05.05 → 60+5, 05.06 → 0, 05.07 → 30.
    kalan = {"2026-05-05": Decimal(65), "2026-05-06": Decimal(0), "2026-05-07": Decimal(30)}
    for gun, atamasiz in pm.items():
        assert Decimal(civil[gun]) + Decimal(elek[gun]) + kalan[gun] == Decimal(atamasiz), gun
    assert civil == {"2026-05-05": "80.00", "2026-05-06": "20.00", "2026-05-07": "0.00"}
    assert elek == {"2026-05-05": "80.00", "2026-05-06": "20.00", "2026-05-07": "120.00"}


@pytest.mark.parametrize("arac", ["santiye_detayi", "proje_detayi", "projeleri_listele"])
async def test_arac_duyarli_alan_okumuyor_civil_esittir_pm_atamasiz(
    dunya, transport_factory, actor_factory, arac
):
    """Aynı uç, araç alanı yüzeye çıkarmıyor: Civil çıktısı == pm_atamasiz çıktısı."""
    argumanlar = {
        "santiye_detayi": {"site_id": str(dunya.santiye.id)},
        "proje_detayi": {"project_id": str(dunya.proje.id)},
        "projeleri_listele": {},
    }[arac]
    civil = await _cagir(
        arac, dunya.kullanici["civil"], transport_factory, actor_factory, **argumanlar
    )
    pm = await _cagir(
        arac, dunya.kullanici["pm_atamasiz"], transport_factory, actor_factory, **argumanlar
    )
    assert civil == pm


async def test_gosterge_ozeti_stok_disiplinsiz_civil_esittir_pm_atamasiz(
    dunya, transport_factory, actor_factory
):
    civil = await _cagir(
        "gosterge_ozeti", dunya.kullanici["civil"], transport_factory, actor_factory
    )
    pm = await _cagir(
        "gosterge_ozeti", dunya.kullanici["pm_atamasiz"], transport_factory, actor_factory
    )
    assert civil["risk_notu"] == pm["risk_notu"]
    # DSC-B5 (Ü2): portföy (hakediş hasılatı) kısıtlıda kapanır; GERİSİ (stok riskleri dahil) eşit.
    assert civil["portfoy"] != pm["portfoy"]
    assert {k: v for k, v in civil.items() if k != "portfoy"} == {
        k: v for k, v in pm.items() if k != "portfoy"
    }

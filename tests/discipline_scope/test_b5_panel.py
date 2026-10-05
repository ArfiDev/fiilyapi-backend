"""DSC-B5 PANEL (`GET /dashboard/summary`): kısıtlı kullanıcıda hakediş kaynaklı alanlar kapanır.

* portföy (toplam hakediş, işveren hasılatı) → `restricted()` (`available=False`);
* risk kartı `sources[progress_payments].state == restricted`, hakediş gecikme uyarısı yok;
* stok riskleri, onay sayacı, proje kartları DEĞİŞMEZ (disiplinsiz/B4 kararı);
* F1 pozitif kontrol: aynı roldeki atamasız PM hepsini `ok`/dolu görür.
Atamasız admin gövdesinin birebirliği: `test_b4_golden_atamasiz.py::b4_panel` (golden).
"""

from __future__ import annotations

from decimal import Decimal

from httpx import AsyncClient

from tests.discipline_scope._b4_dunya import DunyaB4

MODUL = "progress_payments"


async def _panel(client: AsyncClient, x: DunyaB4, ad: str) -> dict:
    ret = await client.get("/dashboard/summary", headers=x.d.baslik[ad])
    assert ret.status_code == 200, ret.text
    return ret.json()


def _kaynak(govde: dict) -> dict[str, str]:
    return {s["module"]: s["state"] for s in govde["risks"]["sources"]}


async def test_kisitli_projenin_hakedisi_panelden_cikar_kisitsiz_projeninki_kalir(
    client: AsyncClient, dunya_b4: DunyaB4
) -> None:
    """IZN-B3: disiplin PROJE BASINA. `civil` P1'de kisitli → P1 hakedisi portfoy toplamina GIRMEZ;
    P2'de atamasiz → o proje (hakedissiz: 0.00) toplamda kalir; kaynak "ok" (P2 izinli)."""
    civil = await _panel(client, dunya_b4, "civil")
    pm = await _panel(client, dunya_b4, "pm_atamasiz")
    assert _kaynak(civil)[MODUL] == "ok"
    assert civil["portfolio"]["available"] is True
    assert Decimal(civil["portfolio"]["value"]) == Decimal("0")  # yalniz P2 (hakedissiz)
    assert Decimal(pm["portfolio"]["value"]) > Decimal("0")  # P1 onayli hakedisi dahil
    assert all(a["module"] != MODUL for a in civil["risks"]["items"])


async def test_atamasiz_es_hakedis_kaynagi_ok_ve_portfoy_dolu(
    client: AsyncClient, dunya_b4: DunyaB4
) -> None:
    pm = await _panel(client, dunya_b4, "pm_atamasiz")
    assert _kaynak(pm)[MODUL] == "ok"
    assert pm["portfolio"]["available"] is True
    assert pm["portfolio"]["value"] is not None


async def test_kisitli_yalniz_hakedis_alanlari_farkli_gerisi_ayni(
    client: AsyncClient, dunya_b4: DunyaB4
) -> None:
    civil = await _panel(client, dunya_b4, "civil")
    pm = await _panel(client, dunya_b4, "pm_atamasiz")
    kaynak_civil, kaynak_pm = _kaynak(civil), _kaynak(pm)
    assert {m: s for m, s in kaynak_civil.items() if m != MODUL} == {
        m: s for m, s in kaynak_pm.items() if m != MODUL
    }
    # hakediş gecikme uyarıları dışındaki risk satırları eşit (stok/takvim disiplinsiz)
    kalan = [a for a in pm["risks"]["items"] if a["module"] != MODUL]
    assert civil["risks"]["items"] == kalan
    for alan in ("projects", "pending_approvals", "receivables", "average_margin", "role_name"):
        assert civil[alan] == pm[alan], alan

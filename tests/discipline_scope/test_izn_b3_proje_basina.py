"""IZN-B3 — disiplin kapsamı PROJE BAŞINA (HTTP): aynı kişi P1'de kısıtlı, P2'de atamasız.

`dunya_b4`: P1 (şantiye A/B; `civil` KAB, `elek` DUV ile kısıtlı) ve P2 (şantiye C; revizyonsuz,
iki kişi de P2'de ekipte ama disiplin ATAMASIZ). Eskiden disiplin KULLANICI başınaydı: `civil` P2'de
de kısıtlı sayılır ve revizyonsuz şantiyede HİÇBİR kalem göremezdi. Şimdi:

* P1 BOQ: yalnız kendi disiplininin kalemi · P2 BOQ: TÜM kalemler (o projede kısıtsız).
* Ü6 (`RequireUnrestricted`): P1'de yapısal işlem 403, P2'de AYNI kişi yapar.
* "Tüm projeler" kişisi her yerde kısıtsızdır (bayat disiplin satırı yok sayılır).
* 🔴 Mutasyon (c) bekçisi: disiplin kapsamında proje parametresini yok sayan bir sağlayıcı bu
  dosyayı kırmızı yapar (P2'de kalem kaybolur / P1'de kısıt kalkar).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests._proje_ekibi import tum_projeler
from tests.discipline_scope._b4_dunya import DunyaB4


def _kalemler(govde: dict) -> set[str]:
    return {i["id"] for g in govde["groups"] for i in g["items"]}


async def _boq(client: AsyncClient, site_id, baslik) -> set[str]:
    resp = await client.get(f"/sites/{site_id}/boq", headers=baslik)
    assert resp.status_code == 200, resp.text
    return _kalemler(resp.json())


@pytest.mark.parametrize("aktor", ["civil", "elek"])
async def test_P1de_kisitli_P2de_kisitsiz_boq_gorunurlugu(
    dunya_b4: DunyaB4, client: AsyncClient, aktor: str
) -> None:
    x = dunya_b4
    h = x.d.baslik[aktor]
    p1 = await _boq(client, x.d.santiye.id, h)
    hepsi_p1 = await _boq(client, x.d.santiye.id, x.d.baslik["atamasiz"])
    assert p1 and p1 < hepsi_p1  # P1: kendi disiplininin alt kümesi
    p2 = await _boq(client, x.santiye_c.id, h)
    assert p2 == {str(x.ic1.id)}  # P2: atamasız → TÜM kalemler (eskiden disiplinsiz kalem gizliydi)
    assert p2 == await _boq(client, x.santiye_c.id, x.d.baslik["atamasiz"])


async def test_U6_yapisal_islem_P1de_403_P2de_ayni_kisiye_acik(
    dunya_b4: DunyaB4, client: AsyncClient
) -> None:
    x = dunya_b4
    h = x.d.baslik["civil"]
    kapali = await client.post(
        f"/sites/{x.d.santiye.id}/boq/groups", headers=h, json={"name": "Yeni Grup P1"}
    )
    assert kapali.status_code == 403, kapali.text
    acik = await client.post(
        f"/sites/{x.santiye_c.id}/boq/groups", headers=h, json={"name": "Yeni Grup P2"}
    )
    assert acik.status_code == 201, acik.text


async def test_tum_projeler_kisisi_bayat_disiplin_satirina_ragmen_her_projede_kisitsiz(
    dunya_b4: DunyaB4, client: AsyncClient, seeded_db
) -> None:
    x = dunya_b4
    await tum_projeler(seeded_db, x.d.kullanici["civil"])
    h = x.d.baslik["civil"]
    seeded_db.expunge_all()
    assert await _boq(client, x.d.santiye.id, h) == await _boq(
        client, x.d.santiye.id, x.d.baslik["atamasiz"]
    )
    yeni = await client.post(
        f"/sites/{x.d.santiye.id}/boq/groups", headers=h, json={"name": "Tüm projeler"}
    )
    assert yeni.status_code == 201, yeni.text

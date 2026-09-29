"""DSC-B2 — EV bütçe KALEM-BAZLI yazmalar (PATCH items/{id} · PATCH leaves): Ü7 + S1 + F2.

Taslakta G1→DUV, G2→DUV; R(site) AKTİF (G1→KAB, G2→DUV): elek'in taslak ağacında yalnız I2
görünür (I1'in aktif disiplini KAB). Kısıtlıda gizli kalem/yaprak/katalog, OLMAYANLA aynı
durum + gövde + content-type'ı verir; her 4xx'in yanında aynı roldeki ATAMASIZ eş 2xx alır (F1).
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests._disiplin_dunyasi import Dunya, _kimlik
from tests.discipline_scope._b2_yardim import ozet

YAZANLAR = True
YOK = uuid.UUID(int=0xBEEF)
KAB_KATALOG = _kimlik(21, 1)
DUV_KATALOG = _kimlik(21, 2)


def _taban(d: Dunya) -> str:
    return f"/sites/{d.santiye.id}/earned-value/budget"


async def _patch_kalem(client: AsyncClient, d: Dunya, baslik, kalem, govde: dict):  # noqa: ANN202
    return await client.patch(f"{_taban(d)}/items/{kalem}", headers=baslik, json=govde)


async def test_yabanci_kalem_patch_olmayan_kalemle_ayni_404_atamasiz_es_200(
    client: AsyncClient, dunya: Dunya, elek_yazar, yazar_atamasiz
) -> None:
    govde = {"is_direct": False}
    yabanci = await _patch_kalem(client, dunya, elek_yazar, dunya.i["i1"].id, govde)
    olmayan = await _patch_kalem(client, dunya, elek_yazar, YOK, govde)
    assert ozet(yabanci) == ozet(olmayan) and yabanci.status_code == 404
    poz = await _patch_kalem(client, dunya, yazar_atamasiz, dunya.i["i1"].id, govde)
    assert poz.status_code == 200, poz.text


async def test_kendi_kalemi_patch_yaniti_suzulmustur(
    client: AsyncClient, dunya: Dunya, elek_yazar
) -> None:
    resp = await _patch_kalem(client, dunya, elek_yazar, dunya.i["i2"].id, {"is_direct": False})
    assert resp.status_code == 200, resp.text
    assert str(dunya.i["i1"].id) not in resp.text and str(dunya.g["g1"].id) not in resp.text
    assert str(dunya.i["i2"].id) in resp.text


async def test_s1_yabanci_disiplin_katalogu_olmayan_katalogla_ayni_404_atamasiz_es_200(
    client: AsyncClient, dunya: Dunya, elek_yazar, yazar_atamasiz
) -> None:
    """Kısıtlı elek DUV kalemine KAB kataloğu bağlayamaz: bugünkü "yok" yanıtı (404)."""
    i2 = dunya.i["i2"].id
    yabanci = await _patch_kalem(
        client, dunya, elek_yazar, i2, {"catalog_item_id": str(KAB_KATALOG)}
    )
    olmayan = await _patch_kalem(client, dunya, elek_yazar, i2, {"catalog_item_id": str(YOK)})
    assert ozet(yabanci) == ozet(olmayan) and yabanci.status_code == 404
    # pozitif: kendi disiplininin kataloğu 200; atamasız eş KAB kataloğunu da bağlayabilir
    kendi = await _patch_kalem(client, dunya, elek_yazar, i2, {"catalog_item_id": str(DUV_KATALOG)})
    assert kendi.status_code == 200, kendi.text
    poz = await _patch_kalem(
        client, dunya, yazar_atamasiz, i2, {"catalog_item_id": str(KAB_KATALOG)}
    )
    assert poz.status_code == 200, poz.text


async def test_yaprak_patch_gorunmeyen_yaprak_olmayanla_ayni_422_atamasiz_es_200(
    client: AsyncClient, dunya: Dunya, elek_yazar, yazar_atamasiz
) -> None:
    def govde(kalem: uuid.UUID) -> dict:
        return {"leaves": [{"boq_item_id": str(kalem), "section_id": None, "unit_mhr": "1"}]}

    url = f"{_taban(dunya)}/leaves"
    yabanci = await client.patch(url, headers=elek_yazar, json=govde(dunya.i["i1"].id))
    olmayan = await client.patch(url, headers=elek_yazar, json=govde(YOK))
    a, b = ozet(yabanci), ozet(olmayan)
    assert a[0] == b[0] == 422 and a[1] == b[1] and a[2] == b[2]
    poz = await client.patch(url, headers=yazar_atamasiz, json=govde(dunya.i["i1"].id))
    assert poz.status_code == 200, poz.text


async def test_yaprak_patch_kendi_yaprak_yaniti_suzulmus_yazma_gizliye_dokunmaz(
    client: AsyncClient, dunya: Dunya, elek_yazar
) -> None:
    govde = {
        "leaves": [
            {"boq_item_id": str(dunya.i["i2"].id), "section_id": str(dunya.s1.id), "unit_mhr": "3"}
        ]
    }
    resp = await client.patch(f"{_taban(dunya)}/leaves", headers=elek_yazar, json=govde)
    assert resp.status_code == 200, resp.text
    assert str(dunya.i["i1"].id) not in resp.text and str(dunya.g["g1"].id) not in resp.text
    assert str(dunya.i["i2"].id) in resp.text


async def test_civil_pozitif_taslakta_g1_kabe_eslenince_kalem_ve_yaprak_yazilir(
    client: AsyncClient, seeded_db, dunya: Dunya, civil_yazar, atamasiz
) -> None:
    """Atamasız taslakta G1→KAB eşler; civil kendi I1'ini yazabilir (200) ve DB'ye yazılır."""
    from decimal import Decimal

    from sqlalchemy import select

    from app.modules.earned_value.models import (
        EvItemSettings,
        EvLeafSettings,
        EvRevision,
        RevisionStatus,
    )

    esleme = {
        "items": [{"boq_group_id": str(dunya.g["g1"].id), "discipline_id": str(dunya.kab.id)}]
    }
    r = await client.put(f"{_taban(dunya)}/group-disciplines", headers=atamasiz, json=esleme)
    assert r.status_code == 200, r.text
    taslak = (
        await seeded_db.execute(
            select(EvRevision.id).where(
                EvRevision.site_id == dunya.santiye.id, EvRevision.status == RevisionStatus.DRAFT
            )
        )
    ).scalar_one()
    i1 = dunya.i["i1"].id
    resp = await _patch_kalem(client, dunya, civil_yazar, i1, {"is_direct": False})
    assert resp.status_code == 200, resp.text
    kalem = await seeded_db.scalar(
        select(EvItemSettings).where(
            EvItemSettings.revision_id == taslak, EvItemSettings.boq_item_id == i1
        )
    )
    assert kalem is not None and kalem.is_direct is False
    govde = {"leaves": [{"boq_item_id": str(i1), "section_id": str(dunya.s1.id), "unit_mhr": "7"}]}
    resp = await client.patch(f"{_taban(dunya)}/leaves", headers=civil_yazar, json=govde)
    assert resp.status_code == 200, resp.text
    yaprak = await seeded_db.scalar(
        select(EvLeafSettings).where(
            EvLeafSettings.revision_id == taslak,
            EvLeafSettings.boq_item_id == i1,
            EvLeafSettings.section_id == dunya.s1.id,
        )
    )
    assert yaprak is not None and yaprak.unit_mhr == Decimal(7)
    assert str(dunya.i["i2"].id) not in resp.text

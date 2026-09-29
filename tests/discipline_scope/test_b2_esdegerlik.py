"""DSC-B2 ADIM 0a — EŞDEĞERLİK (B1 açık #5): `diary_adapter.visible_node_ids` ↔
`node_ids(prune_tree(active_tree, scope, None, frozen=True))` her kısıtlı kapsam için AYNI küme.

Biri tek sorguyla donmuş yaprak fotoğrafından (`ev_baseline_leaves`), öbürü tam ağaç kurulup
`d:` kökünden budanarak gelir; iki yol ayrışırsa gün dağıtımının "bilinen kod" kararı ile
kod-ağacı ekranı farklı kümeyi gösterir. Kenar durumlar (dondurma ÖNCESİ kurulur, sonrası
bozulur): bölümsüz kalanı SIFIR kalem (tüm miktar bölümlere tahsisli) · dondurmadan SONRA
silinen kalem + boş kalan BOQ grubu · NULL disiplin (eşlemesiz grup) · iki disiplinli kapsam ·
kapsamda hiç disiplin karşılığı olmayan kimlik.

`test_dondurma_sonrasi_silinen_grup_iki_yolda_da_budanir`: silinen grup kenarı açıkça adlı.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import DisciplineScope
from app.modules.earned_value import diary_adapter
from app.modules.earned_value.budget_scope import node_ids, prune_tree
from tests._disiplin_dunyasi import Dunya


async def _ok(resp, kod: int = 200) -> dict:  # noqa: ANN001
    assert resp.status_code == kod, resp.text
    return resp.json() if resp.content else {}


async def _kenar_durumlari_kur(client: AsyncClient, d: Dunya) -> dict[str, uuid.UUID]:
    """Taslakta yeni grup/kalemler açar, eşler, dondurur; SONRA kalem + grup siler."""
    admin = d.baslik["atamasiz"]
    site = d.santiye.id
    taban = f"/sites/{site}/earned-value/budget"
    kimlik: dict[str, uuid.UUID] = {}
    for ad, isim in (("g4", "Zemin"), ("g5", "Çatı"), ("g6", "Eşlemesiz 2")):
        veri = await _ok(
            await client.post(f"/sites/{site}/boq/groups", headers=admin, json={"name": isim}),
            201,
        )
        kimlik[ad] = uuid.UUID(veri["id"])
    for ad, grup, kod, miktar in (
        ("i4", "g4", "04.001", "40"),  # tamamı bölümlere tahsisli → bölümsüz kalan SIFIR
        ("i5", "g4", "04.002", "30"),  # tahsissiz → yalnız bölümsüz yaprak
        ("i6", "g5", "05.001", "20"),  # dondurmadan sonra silinecek
        ("i7", "g6", "06.001", "10"),  # eşlemesiz → d:none
    ):
        veri = await _ok(
            await client.post(
                f"/sites/{site}/boq/items",
                headers=admin,
                json={
                    "group_id": str(kimlik[grup]),
                    "code": kod,
                    "description": kod,
                    "unit": "m2",
                    "quantity": miktar,
                    "unit_price": "1",
                },
            ),
            201,
        )
        kimlik[ad] = uuid.UUID(veri["id"])
    await _ok(
        await client.put(
            f"/boq/items/{kimlik['i4']}/allocations",
            headers=admin,
            json={
                "allocations": [
                    {"section_id": str(d.s1.id), "quantity": "25"},
                    {"section_id": str(d.s2.id), "quantity": "15"},
                ]
            },
        )
    )
    await _ok(
        await client.put(
            f"{taban}/group-disciplines",
            headers=admin,
            json={
                "items": [
                    {"boq_group_id": str(kimlik["g4"]), "discipline_id": str(d.kab.id)},
                    {"boq_group_id": str(kimlik["g5"]), "discipline_id": str(d.duv.id)},
                    {"boq_group_id": str(d.g["g1"].id), "discipline_id": str(d.kab.id)},
                ]
            },
        )
    )
    yapraklar = [
        {"boq_item_id": str(kimlik["i4"]), "section_id": str(d.s1.id), "unit_mhr": "1"},
        {"boq_item_id": str(kimlik["i4"]), "section_id": str(d.s2.id), "unit_mhr": "1"},
        {"boq_item_id": str(kimlik["i5"]), "section_id": None, "unit_mhr": "1"},
        {"boq_item_id": str(kimlik["i6"]), "section_id": None, "unit_mhr": "1"},
    ]
    await _ok(await client.patch(f"{taban}/leaves", headers=admin, json={"leaves": yapraklar}))
    await _ok(await client.post(f"{taban}/freeze", headers=admin, json={}))
    # dondurmadan SONRA: kalem sil (fotoğraf kalır), sonra artık boş grubu sil
    await _ok(await client.delete(f"/boq/items/{kimlik['i6']}", headers=admin), 204)
    await _ok(await client.delete(f"/boq/groups/{kimlik['g5']}", headers=admin), 204)
    return kimlik


def _kapsamlar(d: Dunya) -> dict[str, DisciplineScope]:
    return {
        "kab": DisciplineScope(frozenset({d.kab.id})),
        "duv": DisciplineScope(frozenset({d.duv.id})),
        "ikisi": DisciplineScope(frozenset({d.kab.id, d.duv.id})),
        "tanimsiz": DisciplineScope(frozenset({uuid.UUID(int=12345)})),
    }


async def _iki_yol(session: AsyncSession, d: Dunya, scope: DisciplineScope):
    agac = await diary_adapter.active_tree(session, d.santiye.id)
    assert agac is not None
    yol_agac = node_ids(prune_tree(agac, scope, None, frozen=True))
    yol_sorgu = await diary_adapter.visible_node_ids(session, d.santiye.id, scope)
    return yol_agac, yol_sorgu, agac


@pytest.mark.parametrize("ad", ["kab", "duv", "ikisi", "tanimsiz"])
async def test_visible_node_ids_prune_tree_ile_ayni_kume_kenar_durumlarla(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, ad: str
) -> None:
    kimlik = await _kenar_durumlari_kur(client, dunya)
    scope = _kapsamlar(dunya)[ad]
    yol_agac, yol_sorgu, agac = await _iki_yol(seeded_db, dunya, scope)
    assert yol_sorgu == yol_agac, (
        f"AYRIŞMA ({ad}): yalnız sorguda {sorted(yol_sorgu - yol_agac)} · "
        f"yalnız ağaçta {sorted(yol_agac - yol_sorgu)}"
    )
    tum = node_ids(agac)
    if ad == "tanimsiz":
        assert yol_agac == frozenset()
        return
    assert yol_agac, "boş küme kıyaslamayı anlamsız kılar"
    assert "d:none" not in yol_agac and "d:none" in tum  # d:none hep dışarıda, ağaçta VAR
    # kenar durum somutları
    i4, i5 = kimlik["i4"], kimlik["i5"]
    if ad in ("kab", "ikisi"):
        assert f"i:{i4}" in yol_agac
        assert f"l:{i4}:{dunya.s1.id}" in yol_agac
        assert f"l:{i4}:none" not in yol_agac and not any(
            n.startswith(f"l:{i4}:") and n.endswith(":none") for n in yol_agac
        )  # sıfır kalan → bölümsüz yaprak YOK
        assert any(n.startswith(f"l:{i5}:") for n in yol_agac)
    # DONDURMA SONRASI SİLİNEN GRUP: eşleme satırı grupla birlikte gider → yaprakları d:none
    # (ağaçta VAR, ama hiçbir kapsamda görünmez); iki yol da onu BUDAR (fail-closed).
    assert f"i:{kimlik['i6']}" not in yol_agac and f"i:{kimlik['i6']}" in tum
    assert f"g:{kimlik['g5']}" not in yol_agac and f"g:{kimlik['g5']}" in tum
    assert f"i:{kimlik['i7']}" not in yol_agac and f"g:{kimlik['g6']}" not in yol_agac


async def test_visible_node_ids_atamasiz_dunyada_da_ayni_kume(
    seeded_db: AsyncSession, dunya: Dunya
) -> None:
    """Kenar durumsuz temel dünya (I3 · G3 eşlemesiz d:none) — üç kapsam."""
    for scope in _kapsamlar(dunya).values():
        yol_agac, yol_sorgu, _ = await _iki_yol(seeded_db, dunya, scope)
        assert yol_sorgu == yol_agac


@pytest.mark.parametrize("ad", ["duv", "ikisi"])
async def test_dondurma_sonrasi_silinen_grup_iki_yolda_da_budanir(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, ad: str
) -> None:
    """Dondurmada Duvar'a eşli grup + kalemi, dondurma SONRASI silinir: fotoğraf yaprağı
    `discipline_id=duv` taşır ama canlı eşleme satırı gitmiştir → ağaç yolunda d:none.
    Sorgu yolu da (fotoğraf disiplinine DEĞİL canlı eşlemeye bakarak) aynı kararı vermeli."""
    kimlik = await _kenar_durumlari_kur(client, dunya)
    scope = _kapsamlar(dunya)[ad]
    yol_agac, yol_sorgu, agac = await _iki_yol(seeded_db, dunya, scope)
    silinen = {f"g:{kimlik['g5']}", f"i:{kimlik['i6']}", f"l:{kimlik['i6']}:none"}
    assert silinen <= node_ids(agac)  # ağaçta duruyor (d:none altında)
    assert not (silinen & yol_agac) and not (silinen & yol_sorgu)
    assert yol_sorgu == yol_agac

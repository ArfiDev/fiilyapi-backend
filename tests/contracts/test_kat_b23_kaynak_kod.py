# ruff: noqa: F811  (fikstürler test_subcontract_items'ten ice aktarilir, parametre adi ayni)
"""KAT-B2.3 — taseron sozlesme kalemine Bakanlik poz no'su (`source_code`) SNAPSHOT'i.

Uc yazma yolu (isverenden yukle, tekil POST, ic ice POST) kaynak isveren kaleminden kopyalar;
bagsiz kalem NULL (K4); istemci Create/Update/ic ice govdede gonderemez (K9, 422); kaynak kalemin
kodu degisse/kalem silinse kopya korunur; yeniden yukleme MEVCUT kalemi doldurmaz/degistirmez (K14).
"""

import uuid

import pytest
from sqlalchemy import select

from app.modules.contracts.models import (
    EmployerContractItem,
    SubcontractorContract,
    SubcontractorContractItem,
)

from .test_subcontract_items import (  # noqa: F401  (fikstürler)
    proje_isveren_pozlu,
    taseron,
    taseron_sozlesmesi,
)

KODLAR = {"03.001": "15.100.1001", "03.002": None, "04.001": "15.100.1003"}


@pytest.fixture
async def kaynaklar(seeded_db, proje_isveren_pozlu) -> dict[str, EmployerContractItem]:
    """Isveren kalemleri kod → kalem; Bakanlik no'lari: biri dolu, biri kodsuz, biri dolu."""
    kalemler = (
        (
            await seeded_db.execute(
                select(EmployerContractItem).where(
                    EmployerContractItem.project_id == proje_isveren_pozlu
                )
            )
        )
        .scalars()
        .all()
    )
    sonuc = {k.code: k for k in kalemler}
    for kod, bakanlik in KODLAR.items():
        sonuc[kod].source_code = bakanlik
    await seeded_db.flush()
    return sonuc


async def _taseron_kalemleri(db, contract_id) -> dict[str, SubcontractorContractItem]:
    satirlar = (
        (
            await db.execute(
                select(SubcontractorContractItem).where(
                    SubcontractorContractItem.contract_id == contract_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {s.code: s for s in satirlar}


def _govde(kod: str, kaynak: uuid.UUID | None = None, **ek) -> dict:
    govde = {"code": kod, "description": "Ek", "unit": "m²", "quantity": 5, "unit_price": 10}
    if kaynak is not None:
        govde["source_contract_item_id"] = str(kaynak)
    return {**govde, **ek}


async def _yukle(client, headers, sozlesme):
    return await client.post(
        f"/subcontractor-contracts/{sozlesme}/items/load-from-employer", headers=headers
    )


async def test_isverenden_yukle_kaynak_kalem_kodunu_kopyalar_yanitta_doner(
    client, admin_headers, taseron_sozlesmesi, kaynaklar, seeded_db
):
    yanit = await _yukle(client, admin_headers, taseron_sozlesmesi)
    assert yanit.status_code == 200, yanit.text
    satirlar = await _taseron_kalemleri(seeded_db, taseron_sozlesmesi)
    assert {k: v.source_code for k, v in satirlar.items()} == KODLAR
    detay = await client.get(
        f"/subcontractor-contracts/{taseron_sozlesmesi}", headers=admin_headers
    )
    assert {i["code"]: i["source_code"] for i in detay.json()["items"]} == KODLAR


async def test_tekil_ekleme_bagli_kalem_kopyalar_bagsiz_ve_kodsuz_NULL(
    client, admin_headers, taseron_sozlesmesi, kaynaklar, seeded_db
):
    bagli = await client.post(
        f"/subcontractor-contracts/{taseron_sozlesmesi}/items",
        json=_govde("T1", kaynaklar["03.001"].id),
        headers=admin_headers,
    )
    kodsuz = await client.post(
        f"/subcontractor-contracts/{taseron_sozlesmesi}/items",
        json=_govde("T2", kaynaklar["03.002"].id),
        headers=admin_headers,
    )
    bagsiz = await client.post(
        f"/subcontractor-contracts/{taseron_sozlesmesi}/items",
        json=_govde("T3"),
        headers=admin_headers,
    )
    assert [r.status_code for r in (bagli, kodsuz, bagsiz)] == [201, 201, 201]
    assert bagli.json()["source_code"] == "15.100.1001"
    assert bagli.json()["code"] == "T1"  # code'a dokunulmaz
    assert kodsuz.json()["source_code"] is None
    assert bagsiz.json()["source_code"] is None
    satirlar = await _taseron_kalemleri(seeded_db, taseron_sozlesmesi)
    assert satirlar["T1"].source_code == "15.100.1001"
    assert satirlar["T3"].source_code is None


async def test_ic_ice_yazma_her_kaleme_kendi_kaynagini_kopyalar(
    client, admin_headers, proje_isveren_pozlu, taseron, kaynaklar, seeded_db
):
    yanit = await client.post(
        f"/projects/{proje_isveren_pozlu}/subcontractor-contracts",
        json={
            "is_draft": True,
            "subcontractor_id": str(taseron),
            "items": [
                _govde("N1", kaynaklar["04.001"].id),
                _govde("N2"),
                _govde("N3", kaynaklar["03.002"].id),
                _govde("N4", kaynaklar["03.001"].id),
            ],
        },
        headers=admin_headers,
    )
    assert yanit.status_code == 201, yanit.text
    assert {i["code"]: i["source_code"] for i in yanit.json()["items"]} == {
        "N1": "15.100.1003",
        "N2": None,
        "N3": None,
        "N4": "15.100.1001",
    }
    satirlar = await _taseron_kalemleri(seeded_db, uuid.UUID(yanit.json()["id"]))
    assert {k: v.source_code for k, v in satirlar.items()}["N4"] == "15.100.1001"


async def test_SNAPSHOT_kaynak_kodu_degisir_kalem_silinir_taseron_kopyasi_korunur(
    client, admin_headers, taseron_sozlesmesi, kaynaklar, seeded_db
):
    await _yukle(client, admin_headers, taseron_sozlesmesi)
    # isveren kalem kodu PATCH'le degisemez (K2) → degisimi dogrudan DB'de simule et
    kaynaklar["03.001"].source_code = "99.9.9"
    kaynaklar["04.001"].source_code = None
    await seeded_db.flush()
    satirlar = await _taseron_kalemleri(seeded_db, taseron_sozlesmesi)
    assert satirlar["03.001"].source_code == "15.100.1001"
    assert satirlar["04.001"].source_code == "15.100.1003"
    # kaynak kalem SILININCE (FK SET NULL) bag kopar, kopya KALIR
    await seeded_db.delete(kaynaklar["03.001"])
    await seeded_db.flush()
    await seeded_db.refresh(satirlar["03.001"])
    assert satirlar["03.001"].source_contract_item_id is None
    detay = await client.get(
        f"/subcontractor-contracts/{taseron_sozlesmesi}", headers=admin_headers
    )
    assert {i["code"]: i["source_code"] for i in detay.json()["items"]}["03.001"] == "15.100.1001"
    # PATCH baska alan kopyaya dokunmaz
    kalem = satirlar["03.001"]
    yama = await client.patch(
        f"/subcontractor-contracts/items/{kalem.id}",
        json={"description": "Yeni"},
        headers=admin_headers,
    )
    assert yama.status_code == 200 and yama.json()["source_code"] == "15.100.1001"


async def test_K14_yeniden_yukleme_mevcut_kalemi_doldurmaz_degistirmez_yenilere_kopyalar(
    client, admin_headers, taseron_sozlesmesi, kaynaklar, seeded_db
):
    await _yukle(client, admin_headers, taseron_sozlesmesi)
    satirlar = await _taseron_kalemleri(seeded_db, taseron_sozlesmesi)
    satirlar["03.001"].source_code = None  # eski (backfill'siz) satir
    satirlar["04.001"].source_code = "ESKI.1"  # farkli eski deger
    kaynaklar["03.002"].source_code = "88.8.8"  # kaynakta sonradan dolmus
    yeni = EmployerContractItem(
        project_id=kaynaklar["03.001"].project_id,
        group_id=kaynaklar["03.001"].group_id,
        code="05.001",
        description="Yeni",
        unit="m",
        quantity=1,
        unit_price=1,
        source_code="15.100.1005",
    )
    seeded_db.add(yeni)
    await seeded_db.flush()

    yanit = await _yukle(client, admin_headers, taseron_sozlesmesi)
    assert yanit.status_code == 200, yanit.text
    assert (yanit.json()["created_count"], yanit.json()["skipped_count"]) == (1, 3)
    await seeded_db.refresh(satirlar["03.001"])
    sonra = await _taseron_kalemleri(seeded_db, taseron_sozlesmesi)
    assert sonra["03.001"].source_code is None  # DOLDURULMADI
    assert sonra["04.001"].source_code == "ESKI.1"  # DEGISTIRILMEDI
    assert sonra["03.002"].source_code is None  # kaynakta sonradan dolsa da
    assert sonra["05.001"].source_code == "15.100.1005"  # yeni kalem kopyalar


@pytest.mark.parametrize("deger", ["X.1", None])
async def test_K9_istemci_source_code_gonderemez_tekil_PATCH_ic_ice_422(
    client, admin_headers, proje_isveren_pozlu, taseron, taseron_sozlesmesi, kaynaklar, seeded_db,
    deger,
):  # fmt: skip
    tekil = await client.post(
        f"/subcontractor-contracts/{taseron_sozlesmesi}/items",
        json=_govde("T1", source_code=deger),
        headers=admin_headers,
    )
    assert tekil.status_code == 422 and "source_code" in tekil.text
    var = await client.post(
        f"/subcontractor-contracts/{taseron_sozlesmesi}/items",
        json=_govde("T2", kaynaklar["03.001"].id),
        headers=admin_headers,
    )
    yama = await client.patch(
        f"/subcontractor-contracts/items/{var.json()['id']}",
        json={"source_code": deger},
        headers=admin_headers,
    )
    assert yama.status_code == 422 and "source_code" in yama.text
    onceki = (await seeded_db.execute(select(SubcontractorContract.id))).scalars().all()
    ic_ice = await client.post(
        f"/projects/{proje_isveren_pozlu}/subcontractor-contracts",
        json={
            "is_draft": True,
            "subcontractor_id": str(taseron),
            "items": [_govde("N1"), _govde("N2", source_code=deger)],
        },
        headers=admin_headers,
    )
    assert ic_ice.status_code == 422 and "source_code" in ic_ice.text
    # hicbir sey yazilmadi: yalniz "T2" var, yeni sozlesme yok, kopya degismedi
    satirlar = await _taseron_kalemleri(seeded_db, taseron_sozlesmesi)
    assert set(satirlar) == {"T2"} and satirlar["T2"].source_code == "15.100.1001"
    sonra = (await seeded_db.execute(select(SubcontractorContract.id))).scalars().all()
    assert sorted(sonra) == sorted(onceki)

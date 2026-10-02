"""TKL-B6.5 — UCTAN UCA: GERCEK EV adaptoru kayitliyken `POST /offers/{id}/convert`.

Sahte kanca (`tohum_kancasi`) KULLANILMAZ: port gercek kayitla (`earned_value/catalog_router.py`
import yan etkisi) kosar. Her testin basinda kayit `contract_seed.registered()` ile dogrulanir
(kayit dusurulurse bu dosya SAHTE-YESIL degil KIRMIZI olur — bkz. mutasyon M1).

Dunya (elle hesaplanmis):
  Katalog  MIM: Siva 1.5 · Boya 2.0 · Alci 0.75   ELK: Kablo 3.0   (standart adam-saat)
  Teklif   grup A: Siva 10 (maliyet 100, adam-saat = katalog) · Boya 5 (maliyet 80, adam-saat 4 =
           KATALOGDAN FARKLI) · grup B: Alci 2 (maliyet 50, = katalog) · Kablo 3 (maliyet 200)
  Govde    A-1 Siva 10 x 120 · A-2 Boya 5 x 90 · B-1 Alci 2 x 60 · B-2 Kablo 3 x 250
  Beklenen oran yuvasi: A-1 1.5 catalog · A-2 4 offer · B-1 0.75 catalog · B-2 3 catalog
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core import contract_seed
from app.core.db import get_db
from app.main import app
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.catalog.models import ContractorType, EvCatalogItem, EvDiscipline
from app.modules.catalog.service import next_poz_no
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.earned_value import contract_adapter
from app.modules.earned_value.models import (
    EvBaselineCurve,
    EvBaselineLeaf,
    EvContractItemRate,
    EvGroupDiscipline,
    EvItemSettings,
    EvLeafSettings,
    EvRevision,
    EvWindow,
    RateSource,
    RevisionStatus,
)
from app.modules.offers.models import Offer
from app.modules.projects.models import Project
from app.modules.sites.models import Site

from ._convert import url
from ._offers import detay, durum_yap, grup, kalem, revizyon, teklif, tum_kalemler

D = Decimal
START, END = date(2026, 11, 1), date(2027, 10, 31)


@pytest.fixture(autouse=True)
def _gercek_adaptor_kayitli():
    """GERCEK kayit (sahte kanca YOK): EV adaptoru portta, baska kanca yok."""
    assert contract_seed.registered() == (contract_adapter.seed_hook,)
    snapshot = contract_seed.registered()
    yield
    contract_seed.restore(snapshot)


@pytest.fixture
async def mimelk(seeded_db) -> dict:
    """MIM + ELK disiplinleri ve dort katalog kalemi."""
    out: dict = {}
    for kod, ad in (("MIM", "Mimari"), ("ELK", "Elektrik")):
        disc = EvDiscipline(
            code=kod, name=ad, color="#2563eb", default_contractor_type=ContractorType.OWN
        )
        seeded_db.add(disc)
        await seeded_db.flush()
        out[kod] = disc
    plan = (
        ("siva", "MIM", "Sıva", D("1.5")),
        ("boya", "MIM", "Boya", D("2")),
        ("alci", "MIM", "Alçı", D("0.75")),
        ("kablo", "ELK", "Kablo", D("3")),
    )
    for anahtar, kod, ad, mhr in plan:
        item = EvCatalogItem(
            poz_no=await next_poz_no(seeded_db, out[kod]),
            discipline_id=out[kod].id,
            name=ad,
            uom="m2",
            standard_unit_mhr=mhr,
            default_contractor_type=ContractorType.OWN,
        )
        seeded_db.add(item)
        await seeded_db.flush()
        out[anahtar] = item
    return out


@pytest.fixture
async def kz(client, admin, isveren, mimelk) -> dict:
    """Kazanilmis teklif: id + poz adi → teklif kalemi."""
    o = await teklif(client, admin, isveren, vat_pct="20")
    ga = await grup(client, admin, o["id"], name="A")
    gb = await grup(client, admin, o["id"], name="B")
    await kalem(client, admin, o["id"], ga["id"], mimelk["siva"].id, quantity="10",
                cost_unit_price="100")  # fmt: skip
    await kalem(client, admin, o["id"], ga["id"], mimelk["boya"].id, quantity="5",
                cost_unit_price="80", unit_mhr="4")  # fmt: skip
    await kalem(client, admin, o["id"], gb["id"], mimelk["alci"].id, quantity="2",
                cost_unit_price="50")  # fmt: skip
    await kalem(client, admin, o["id"], gb["id"], mimelk["kablo"].id, quantity="3",
                cost_unit_price="200")  # fmt: skip
    await durum_yap(client, admin, o["id"], "won")
    rev = await revizyon(client, admin, o["id"])
    return {
        "id": o["id"],
        "no": o["offer_no"],
        "kalem": {k["description"]: k for k in tum_kalemler(rev)},
    }


def _satir(k: dict, code: str, quantity: str, price: str) -> dict:
    return {
        "catalog_item_id": k["catalog_item_id"],
        "offer_item_id": k["id"],
        "code": code,
        "description": k["description"],
        "unit": k["unit"],
        "quantity": quantity,
        "unit_price": price,
    }


def _govde(kz: dict, **over) -> dict:
    k = kz["kalem"]
    body = {
        "project": {
            "name": "Uçtan Uca Projesi",
            "city": "Ankara",
            "start_date": START.isoformat(),
            "end_date": END.isoformat(),
        },
        "contract": {
            "contract_no": "SZL-E2E-1",
            "signature_date": "2026-10-20",
            "has_price_escalation": False,
        },
        "groups": [
            {
                "name": "A",
                "items": [
                    _satir(k["Sıva"], "A-1", "10", "120"),
                    _satir(k["Boya"], "A-2", "5", "90"),
                ],
            },
            {
                "name": "B",
                "items": [
                    _satir(k["Alçı"], "B-1", "2", "60"),
                    _satir(k["Kablo"], "B-2", "3", "250"),
                ],
            },
        ],
    }
    body.update(over)
    return body


async def _donustur(client, admin, kz, **over) -> dict:
    resp = await client.post(url(kz["id"]), json=_govde(kz, **over), headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _hepsi(db, model, *where):
    return list((await db.execute(select(model).where(*where))).scalars())


async def _sayi(db, model) -> int:
    return await db.scalar(select(func.count()).select_from(model)) or 0


async def _kalemler(db, project_id) -> dict[str, EmployerContractItem]:
    rows = await _hepsi(db, EmployerContractItem, EmployerContractItem.project_id == project_id)
    return {r.code: r for r in rows}


async def _gruplar(db, project_id) -> dict[str, EmployerContractGroup]:
    rows = await _hepsi(db, EmployerContractGroup, EmployerContractGroup.project_id == project_id)
    return {r.name: r for r in rows}


def _beklenen_yuva(mimelk) -> dict[str, tuple[Decimal, RateSource]]:
    return {
        "A-1": (D("1.5"), RateSource.CATALOG),
        "A-2": (D("4"), RateSource.OFFER),
        "B-1": (D("0.75"), RateSource.CATALOG),
        "B-2": (D("3"), RateSource.CATALOG),
    }


def _beklenen_katalog(mimelk) -> dict[str, uuid.UUID]:
    return {
        "A-1": mimelk["siva"].id,
        "A-2": mimelk["boya"].id,
        "B-1": mimelk["alci"].id,
        "B-2": mimelk["kablo"].id,
    }


async def _yuvalar(db, kalemler) -> dict[str, tuple[Decimal, RateSource]]:
    rows = await _hepsi(db, EvContractItemRate)
    by_id = {r.contract_item_id: r for r in rows}
    return {code: (by_id[k.id].unit_mhr, by_id[k.id].source) for code, k in kalemler.items()}


async def _revizyon(db, site_id) -> EvRevision:
    return (await db.execute(select(EvRevision).where(EvRevision.site_id == site_id))).scalar_one()


async def _boq(db, site_id) -> tuple[dict[str, BoqItem], dict[str, BoqGroup]]:
    items = await _hepsi(db, BoqItem, BoqItem.site_id == site_id)
    groups = await _hepsi(db, BoqGroup, BoqGroup.site_id == site_id)
    return {i.code: i for i in items}, {g.name: g for g in groups}


async def _yaprak_oranlari(db, rev_id, boq_items) -> dict[str, tuple]:
    rows = await _hepsi(db, EvLeafSettings, EvLeafSettings.revision_id == rev_id)
    by_item = {r.boq_item_id: r for r in rows}
    out = {}
    for code, item in boq_items.items():
        leaf = by_item[item.id]
        assert leaf.section_id is None  # bolumsuz yaprak
        out[code] = (leaf.unit_mhr, leaf.rate_source)
    return out


async def _katalog_baglari(db, rev_id, boq_items) -> dict[str, uuid.UUID | None]:
    rows = await _hepsi(db, EvItemSettings, EvItemSettings.revision_id == rev_id)
    by_item = {r.boq_item_id: r.catalog_item_id for r in rows}
    return {code: by_item.get(item.id) for code, item in boq_items.items()}


# ------------------------------------------------------------------------------- 1


async def test_santiyeli_donusturme_eslemesiz_rev0_taslak_ve_yuvalar(
    client, admin, db_session, kz, mimelk
) -> None:
    resp = await _donustur(client, admin, kz, open_site=True)

    assert [(w["code"], w["group_name"]) for w in resp["warnings"]] == [
        ("mixed_discipline_group", "B")
    ]
    pid = uuid.UUID(resp["project_id"])
    assert resp["contract_item_count"] == 4 and resp["site_id"] is not None
    # sozlesme: gruplar + kalemler, katalog izi
    gruplar, kalemler = await _gruplar(db_session, pid), await _kalemler(db_session, pid)
    assert set(gruplar) == {"A", "B"} and set(kalemler) == {"A-1", "A-2", "B-1", "B-2"}
    assert {c: k.catalog_item_id for c, k in kalemler.items()} == _beklenen_katalog(mimelk)
    # santiye BOQ: tam miktar + grup adlari
    site = await db_session.get(Site, uuid.UUID(resp["site_id"]))
    assert site is not None and site.project_id == pid
    boq, boq_gruplari = await _boq(db_session, site.id)
    assert set(boq_gruplari) == {"A", "B"}
    assert {c: i.quantity for c, i in boq.items()} == {
        "A-1": D("10"), "A-2": D("5"), "B-1": D("2"), "B-2": D("3"),
    }  # fmt: skip
    assert all(i.contract_item_id == kalemler[c].id for c, i in boq.items())
    # oran yuvalari: farkli kalem 'offer', diger 'catalog'
    assert await _yuvalar(db_session, kalemler) == _beklenen_yuva(mimelk)
    # EV Rev.0 TASLAK, dondurulmamis
    rev = await _revizyon(db_session, site.id)
    assert (rev.number, rev.status, rev.frozen_at) == (0, RevisionStatus.DRAFT, None)
    assert rev.name == f"Rev.0 — {kz['no']}"
    # grup A → MIM eslendi, grup B eslenmedi
    esleme = {r.boq_group_id: r.discipline_id for r in await _hepsi(db_session, EvGroupDiscipline)}
    assert esleme == {boq_gruplari["A"].id: mimelk["MIM"].id}
    # BOQ kalemlerinde katalog bagi + bolumsuz yaprakta oran ve kaynak
    assert await _katalog_baglari(db_session, rev.id, boq) == _beklenen_katalog(mimelk)
    assert await _yaprak_oranlari(db_session, rev.id, boq) == _beklenen_yuva(mimelk)
    # pencere = proje baslangic-bitis (yalniz eslenen disiplin: MIM)
    pencereler = await _hepsi(db_session, EvWindow)
    assert [(w.discipline_id, w.section_id, w.start_date, w.end_date) for w in pencereler] == [
        (mimelk["MIM"].id, None, START, END)
    ]
    # baseline YOK (dondurma Planlama'dan)
    assert await _sayi(db_session, EvBaselineLeaf) == 0
    assert await _sayi(db_session, EvBaselineCurve) == 0
    # teklif donusturuldu
    assert (await detay(client, admin, kz["id"]))["conversion_state"] == "converted"


# ------------------------------------------------------------------------------- 2


async def test_elle_esleme_karisik_grubu_cozer_uyari_yok(
    client, admin, db_session, kz, mimelk
) -> None:
    resp = await _donustur(
        client, admin, kz, open_site=True, group_disciplines={"B": str(mimelk["ELK"].id)}
    )

    assert resp["warnings"] == []
    _boq_items, boq_gruplari = await _boq(db_session, uuid.UUID(resp["site_id"]))
    esleme = {r.boq_group_id: r.discipline_id for r in await _hepsi(db_session, EvGroupDiscipline)}
    assert esleme == {
        boq_gruplari["A"].id: mimelk["MIM"].id,
        boq_gruplari["B"].id: mimelk["ELK"].id,
    }
    # her eslenen disipline Bolumsuz pencere (proje tarihleri)
    pencereler = {(w.discipline_id, w.section_id, w.start_date, w.end_date) for w in
                  await _hepsi(db_session, EvWindow)}  # fmt: skip
    assert pencereler == {
        (mimelk["MIM"].id, None, START, END),
        (mimelk["ELK"].id, None, START, END),
    }
    rev = await _revizyon(db_session, uuid.UUID(resp["site_id"]))
    assert rev.status == RevisionStatus.DRAFT and rev.frozen_at is None


# ------------------------------------------------------------------------------- 3


async def test_santiyesiz_donusturme_sonra_santiye_ac_dagit_ve_sozlesmeden_doldur(
    client, admin, db_session, kz, mimelk
) -> None:
    resp = await _donustur(client, admin, kz, group_disciplines={"A": str(mimelk["MIM"].id)})

    # yalniz proje + sozlesme + oran yuvalari; EV revizyonu YOK; esleme saklanmadi → uyari
    assert resp["site_id"] is None
    assert [w["code"] for w in resp["warnings"]] == ["group_disciplines_ignored_without_site"]
    pid = uuid.UUID(resp["project_id"])
    kalemler = await _kalemler(db_session, pid)
    assert await _yuvalar(db_session, kalemler) == _beklenen_yuva(mimelk)
    assert await _sayi(db_session, EvRevision) == 0
    assert await _sayi(db_session, EvGroupDiscipline) == 0
    assert await _sayi(db_session, Site) == 0

    # sonradan santiye ac + sozlesme kalemlerini tam dagit (mevcut uclar)
    site_resp = await client.post(
        f"/projects/{pid}/sites",
        json={
            "name": "Sonradan Şantiye",
            "site_manager_name": "Şef Kişi",
            "city": "Ankara",
            "neighborhood": "Çankaya",
            "construction_area_m2": "1000",
            "land_area_m2": "2000",
            "start_date": "2026-11-01",
            "end_date": "2027-10-31",
        },
        headers=admin,
    )
    assert site_resp.status_code == 201, site_resp.text
    site_id = site_resp.json()["id"]
    dagit = await client.put(
        f"/projects/{pid}/contract/distribution",
        json={
            "allocations": [
                {"contract_item_id": str(k.id), "site_id": site_id, "quantity": str(k.quantity)}
                for k in kalemler.values()
            ]
        },
        headers=admin,
    )
    assert dagit.status_code == 200, dagit.text

    doldur_url = f"/sites/{site_id}/earned-value/budget/fill-from-contract"
    ilk = await client.post(doldur_url, headers=admin)
    assert ilk.status_code == 200, ilk.text
    govde = ilk.json()
    assert (
        govde["filled_item_count"],
        govde["filled_leaf_count"],
        govde["linked_item_count"],
        govde["mapped_group_count"],
        govde["unrated_item_count"],
    ) == (4, 4, 4, 1, 0)  # grup A tek-disiplinli → MIM; grup B karisik → eslenmez
    boq, boq_gruplari = await _boq(db_session, uuid.UUID(site_id))
    assert [(w["code"], w["boq_group_id"]) for w in govde["warnings"]] == [
        ("mixed_discipline_group", str(boq_gruplari["B"].id))
    ]
    rev = await _revizyon(db_session, uuid.UUID(site_id))
    assert (rev.number, rev.status, rev.frozen_at) == (0, RevisionStatus.DRAFT, None)
    # oranlar yuvalardan, kaynaklar KORUNARAK (offer/catalog); katalog baglari; grup eslemesi
    assert await _yaprak_oranlari(db_session, rev.id, boq) == _beklenen_yuva(mimelk)
    assert await _katalog_baglari(db_session, rev.id, boq) == _beklenen_katalog(mimelk)
    esleme = {r.boq_group_id: r.discipline_id for r in await _hepsi(db_session, EvGroupDiscipline)}
    assert esleme == {boq_gruplari["A"].id: mimelk["MIM"].id}

    # ikinci cagri: SIFIR yazma (satir sayilari + sayaçlar)
    onceki = [
        await _sayi(db_session, m)
        for m in (EvRevision, EvLeafSettings, EvItemSettings, EvGroupDiscipline, EvWindow)
    ]
    ikinci = await client.post(doldur_url, headers=admin)
    assert ikinci.status_code == 200, ikinci.text
    g2 = ikinci.json()
    assert (
        g2["filled_item_count"], g2["filled_leaf_count"],
        g2["linked_item_count"], g2["mapped_group_count"],
    ) == (0, 0, 0, 0)  # fmt: skip
    sonraki = [
        await _sayi(db_session, m)
        for m in (EvRevision, EvLeafSettings, EvItemSettings, EvGroupDiscipline, EvWindow)
    ]
    assert sonraki == onceki


# ------------------------------------------------------------------------------- 4


async def test_ev_kancasi_hata_verirse_tum_donusturme_geri_alinir(
    client, admin, isveren, kz, seeded_db, monkeypatch
) -> None:
    await seeded_db.commit()  # tohum dis islemde kalsin

    async def _gercek_gibi():
        try:
            yield seeded_db
            await seeded_db.commit()
        except Exception:
            await seeded_db.rollback()
            raise

    app.dependency_overrides[get_db] = _gercek_gibi

    class AdaptorPatladi(RuntimeError):
        pass

    async def _patlat(*_a, **_k):
        raise AdaptorPatladi("EV adaptörü patladı")

    # yuvalar ADAPTORDE yazilir (flush edilir) SONRA Rev.0 asamasi patlar → geri alma yuvalari da
    # silmeli
    monkeypatch.setattr(contract_adapter.contract_rates, "apply_contract_to_draft", _patlat)

    with pytest.raises(AdaptorPatladi):
        await client.post(url(kz["id"]), json=_govde(kz, open_site=True), headers=admin)

    for model in (
        Project, EmployerContractGroup, EmployerContractItem, Site, BoqItem,
        EvContractItemRate, EvRevision, EvLeafSettings, EvGroupDiscipline,
    ):  # fmt: skip
        assert await _sayi(seeded_db, model) == 0, model.__name__
    offer = await seeded_db.scalar(select(Offer).where(Offer.offer_no == kz["no"]))
    assert offer is not None and offer.project_id is None and offer.converted_at is None

    # POZITIF KONTROL: adaptor duzelince ayni istek basarili ve kalici
    monkeypatch.undo()
    resp = await client.post(url(kz["id"]), json=_govde(kz, open_site=True), headers=admin)
    assert resp.status_code == 200, resp.text
    assert await _sayi(seeded_db, EvContractItemRate) == 4
    assert await _sayi(seeded_db, EvRevision) == 1


# ------------------------------------------------------------------------------- 5


async def test_planlamada_dondurma_karisik_grup_engeller_esleyince_gecer(
    client, admin, db_session, kz, mimelk
) -> None:
    resp = await _donustur(client, admin, kz, open_site=True)
    site_id = resp["site_id"]
    taban = f"/sites/{site_id}/earned-value/budget"
    boq, boq_gruplari = await _boq(db_session, uuid.UUID(site_id))

    # grup B karisik-eslenmemis + dogrudan butcesi var → SO-55: dondurma 422
    engel = await client.post(f"{taban}/freeze", json={}, headers=admin)
    assert engel.status_code == 422, engel.text
    assert "disciplineless_group" in engel.text
    assert await _sayi(db_session, EvBaselineLeaf) == 0

    # Planlama'da grup B'yi eslestir (MIM: adaptorun pencere yazdigi disiplin) → engel kalmaz
    esle = await client.put(
        f"{taban}/group-disciplines",
        json={
            "items": [{"boq_group_id": str(boq_gruplari["B"].id),
                       "discipline_id": str(mimelk["MIM"].id)}]
        },
        headers=admin,
    )  # fmt: skip
    assert esle.status_code == 200, esle.text
    assert esle.json()["freeze_blockers"] == []
    gec = await client.post(f"{taban}/freeze", json={}, headers=admin)
    assert gec.status_code == 200, gec.text

    rev = await _revizyon(db_session, uuid.UUID(site_id))
    assert (rev.status, rev.frozen_at is not None) == (RevisionStatus.ACTIVE, True)
    baseline = await _hepsi(db_session, EvBaselineLeaf, EvBaselineLeaf.revision_id == rev.id)
    kaynaklar = {b.item_code: (b.unit_mhr, b.rate_source) for b in baseline}
    assert kaynaklar == _beklenen_yuva(mimelk)  # 'offer' satiri (A-2) baseline'a tasindi
    assert [b.item_code for b in baseline if b.rate_source == RateSource.OFFER] == ["A-2"]


# ------------------------------------------------------------------------------- 6


async def _son_fiyatlar(client, admin, mimelk) -> dict[str, tuple[str, Decimal]]:
    resp = await client.get("/catalog/items", headers=admin)
    assert resp.status_code == 200, resp.text
    by_id = {i["id"]: i["last_price"] for i in resp.json()["items"]}
    out = {}
    for anahtar in ("siva", "boya", "alci", "kablo"):
        lp = by_id[str(mimelk[anahtar].id)]
        out[anahtar] = (lp["source"], Decimal(lp["price"]))
    return out


async def test_katalog_son_fiyat_donusturmeden_sonra_tkl_maliyetten_szl_satisa_kayar(
    client, admin, kz, mimelk
) -> None:
    # SO-40: ONCE yalniz kazanilmis teklif var → TKL (maliyet B.F.); SZL yok
    assert await _son_fiyatlar(client, admin, mimelk) == {
        "siva": ("TKL", D("100")),
        "boya": ("TKL", D("80")),
        "alci": ("TKL", D("50")),
        "kablo": ("TKL", D("200")),
    }

    await _donustur(client, admin, kz)

    # SONRA: sozlesme `price_changed_at` = donusturme ani > teklifin `won_at` → "en yeni" SZL
    # (sozlesme B.F. = govdedeki satis fiyati)
    assert await _son_fiyatlar(client, admin, mimelk) == {
        "siva": ("SZL", D("120")),
        "boya": ("SZL", D("90")),
        "alci": ("SZL", D("60")),
        "kablo": ("SZL", D("250")),
    }

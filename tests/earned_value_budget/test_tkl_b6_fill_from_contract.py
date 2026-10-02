"""TKL-B6.4 — "Sozlesmeden doldur" (`POST …/budget/fill-from-contract`) + SO-44.

Dunya `_tkl_b6.py`. Yuvalar elle: ci1 2.5 offer · ci3 0.9 catalog (ci2/ci4/ci5 yuvasiz → katalog
standardi: tugla2 0.60 · beton 1.80 · tugla 0.55) · ci6 yuvasiz+katalogsuz.
Tasarim: "BOSLARI DOLDUR" (`fill_from_catalog` ile ayni) — dolu oran/bag/esleme EZILMEZ.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from app.modules.audit.models import AuditLog
from app.modules.boq.models import BoqItem, BoqItemSectionAllocation
from app.modules.earned_value.models import (
    EvContractItemRate,
    EvGroupDiscipline,
    EvItemSettings,
    EvLeafSettings,
    EvRevision,
    EvWindow,
    RateSource,
)
from app.modules.sites.models import SiteStatus

from . import _tkl_b6 as w

D = Decimal


def _url(site, tail: str = "/fill-from-contract") -> str:  # noqa: ANN001
    return f"/sites/{site.id}/earned-value/budget{tail}"


async def _yuvalar(db, dunya) -> None:
    db.add_all(
        [
            EvContractItemRate(
                contract_item_id=dunya.ci["ci1"].id, unit_mhr=D("2.5"), source=RateSource.OFFER
            ),
            EvContractItemRate(
                contract_item_id=dunya.ci["ci3"].id, unit_mhr=D("0.9"), source=RateSource.CATALOG
            ),
        ]
    )
    await db.flush()


async def _kur(db, proje, santiye, katalog):
    dunya = await w.kur(db, proje, santiye, katalog)
    await _yuvalar(db, dunya)
    return dunya


async def _yapraklar(db, santiye) -> dict:
    rev = (
        await db.execute(select(EvRevision).where(EvRevision.site_id == santiye.id))
    ).scalar_one()
    rows = (
        await db.execute(select(EvLeafSettings).where(EvLeafSettings.revision_id == rev.id))
    ).scalars()
    return {(r.boq_item_id, r.section_id): (r.unit_mhr, r.rate_source) for r in rows}


async def test_yuvali_yuvasiz_katalogsuz_ve_baglantisiz_kalemler(
    client, admin, seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    kab, duv = disiplinler
    dunya = await _kur(seeded_db, proje, santiye, katalog)
    proje.start_date, proje.end_date = w.START, w.END
    bagsiz = BoqItem(
        site_id=santiye.id, group_id=dunya.bg["Betonarme"].id, code="EL-1", description="Elle",
        unit="m3", quantity=D(5), unit_price=D(0), sort_order=50,
    )  # fmt: skip
    seeded_db.add(bagsiz)
    await seeded_db.flush()

    resp = await client.post(_url(santiye), headers=admin)

    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert (out["filled_item_count"], out["filled_leaf_count"]) == (5, 5)
    assert (out["linked_item_count"], out["mapped_group_count"]) == (5, 2)
    assert out["unrated_item_count"] == 1  # ci6: yuva da katalog da yok
    assert out["warnings"] == [
        {
            "code": "mixed_discipline_group",
            "message": out["warnings"][0]["message"],
            "boq_group_id": str(dunya.bg["Karışık"].id),
        }
    ]
    yap = await _yapraklar(seeded_db, santiye)
    bi = dunya.bi
    assert yap == {
        (bi["ci1"].id, None): (D("2.5"), RateSource.OFFER),  # yuva (offer)
        (bi["ci2"].id, None): (D("0.60"), RateSource.CATALOG),  # katalog standardi (SO-34)
        (bi["ci3"].id, None): (D("0.9"), RateSource.CATALOG),  # yuva (catalog)
        (bi["ci4"].id, None): (D("1.80"), RateSource.CATALOG),
        (bi["ci5"].id, None): (D("0.55"), RateSource.CATALOG),
    }  # ci6 ve baglantisiz EL-1 satiri YOK
    baglar = {
        r.boq_item_id: r.catalog_item_id
        for r in (await seeded_db.execute(select(EvItemSettings))).scalars()
    }
    assert baglar[bi["ci1"].id] == katalog["beton"].id
    assert bagsiz.id not in baglar and bi["ci6"].id not in baglar
    esleme = {
        r.boq_group_id: r.discipline_id
        for r in (await seeded_db.execute(select(EvGroupDiscipline))).scalars()
    }
    assert esleme == {dunya.bg["Betonarme"].id: kab.id, dunya.bg["Duvar"].id: duv.id}
    rev = (await seeded_db.execute(select(EvRevision))).scalar_one()
    assert (rev.number, rev.status.value) == (0, "draft")
    audit = await seeded_db.scalar(
        select(AuditLog.detail).where(AuditLog.detail.like("Bütçe sözleşmeden dolduruldu%"))
    )
    # O2: yazilan HER sey metinde (satir + bag + esleme + pencere + taslak acma)
    assert audit is not None and audit.endswith(
        "· 5 satır · 5 katalog bağı · 2 grup eşlemesi · 2 pencere · taslak açıldı"
    )
    pencereler = {
        r.discipline_id: (r.start_date, r.end_date, r.section_id)
        for r in (await seeded_db.execute(select(EvWindow))).scalars()
    }
    assert pencereler == {kab.id: (w.START, w.END, None), duv.id: (w.START, w.END, None)}


async def test_bolumlu_ve_bolumsuz_tum_yapraklar_dolar(
    client, admin, seeded_db, proje, santiye, bolumler, katalog, disiplinler
) -> None:
    s1, s2 = bolumler
    dunya = await _kur(seeded_db, proje, santiye, katalog)
    ci1 = dunya.bi["ci1"]
    seeded_db.add_all(
        [
            BoqItemSectionAllocation(boq_item_id=ci1.id, section_id=s1.id, quantity=D(6)),
            BoqItemSectionAllocation(boq_item_id=ci1.id, section_id=s2.id, quantity=D(3)),
        ]
    )
    await seeded_db.flush()

    assert (await client.post(_url(santiye), headers=admin)).status_code == 200

    yap = await _yapraklar(seeded_db, santiye)
    ci1_rows = {k[1]: v for k, v in yap.items() if k[0] == ci1.id}
    assert ci1_rows == {
        s1.id: (D("2.5"), RateSource.OFFER),
        s2.id: (D("2.5"), RateSource.OFFER),
        None: (D("2.5"), RateSource.OFFER),  # kalan 1 → Bolumsuz
    }


async def test_dolu_oran_bag_ve_esleme_ezilmez_ikinci_cagri_bos(
    client, admin, seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    kab, duv = disiplinler
    dunya = await _kur(seeded_db, proje, santiye, katalog)
    ci1 = dunya.bi["ci1"]
    # elle: ci1 orani 9 (manual), ci1 bagi tugla2, Betonarme grubu → DUV
    ok = await client.patch(
        _url(santiye, "/leaves"), headers=admin,
        json={"leaves": [{"boq_item_id": str(ci1.id), "section_id": None, "unit_mhr": "9"}]},
    )  # fmt: skip
    assert ok.status_code == 200, ok.text
    ok = await client.patch(
        _url(santiye, f"/items/{ci1.id}"), headers=admin,
        json={"catalog_item_id": str(katalog["tugla2"].id)},
    )  # fmt: skip
    assert ok.status_code == 200, ok.text
    grp = dunya.bg["Betonarme"].id
    ok = await client.put(
        _url(santiye, "/group-disciplines"), headers=admin,
        json={"items": [{"boq_group_id": str(grp), "discipline_id": str(duv.id)}]},
    )  # fmt: skip
    assert ok.status_code == 200, ok.text

    out = (await client.post(_url(santiye), headers=admin)).json()

    yap = await _yapraklar(seeded_db, santiye)
    assert yap[(ci1.id, None)] == (D("9"), RateSource.MANUAL)  # EZILMEDI
    assert out["filled_leaf_count"] == 4  # ci2..ci5
    bag = (
        await seeded_db.execute(select(EvItemSettings).where(EvItemSettings.boq_item_id == ci1.id))
    ).scalar_one()
    assert bag.catalog_item_id == katalog["tugla2"].id
    esleme = {
        r.boq_group_id: r.discipline_id
        for r in (await seeded_db.execute(select(EvGroupDiscipline))).scalars()
    }
    assert esleme[dunya.bg["Betonarme"].id] == duv.id and esleme[dunya.bg["Duvar"].id] == duv.id
    ikinci = (await client.post(_url(santiye), headers=admin)).json()
    assert (
        ikinci["filled_leaf_count"],
        ikinci["linked_item_count"],
        ikinci["mapped_group_count"],
    ) == (0, 0, 0)


async def test_yazma_izni_ve_tamamlanmis_santiye(
    client, admin, saha, muhasebe, ik, seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    await _kur(seeded_db, proje, santiye, katalog)
    assert (await client.post(_url(santiye), headers=muhasebe)).status_code == 403  # view
    assert (await client.post(_url(santiye), headers=ik)).status_code == 403  # none
    assert (await client.post(_url(santiye))).status_code == 401
    assert (await client.post(_url(santiye), headers=saha)).status_code == 200  # draft yeter
    santiye.status = SiteStatus.completed
    await seeded_db.flush()
    assert (await client.post(_url(santiye), headers=admin)).status_code == 409


# ------------------------------------------------------------------ SO-44


async def test_offer_kaynagi_yaprak_patchinde_422(
    client, admin, seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    dunya = await _kur(seeded_db, proje, santiye, katalog)
    item = str(dunya.bi["ci1"].id)
    govde = {"leaves": [{"boq_item_id": item, "unit_mhr": "3", "rate_source": "offer"}]}
    resp = await client.patch(_url(santiye, "/leaves"), headers=admin, json=govde)
    assert resp.status_code == 422, resp.text
    # kaynak oransiz da gelse reddedilir (sessizce yutulmaz)
    only = {"leaves": [{"boq_item_id": item, "rate_source": "offer"}]}
    assert (
        await client.patch(_url(santiye, "/leaves"), headers=admin, json=only)
    ).status_code == 422
    # reddedilen istek taslak ACMAZ / yazmaz
    assert (await seeded_db.execute(select(EvRevision))).scalars().all() == []
    ok = {"leaves": [{"boq_item_id": item, "unit_mhr": "3", "rate_source": "history"}]}
    assert (await client.patch(_url(santiye, "/leaves"), headers=admin, json=ok)).status_code == 200


async def test_mevcut_offer_yaprak_baska_alanla_patchlenince_kaynak_korunur(
    client, admin, seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    dunya = await _kur(seeded_db, proje, santiye, katalog)
    ci1 = dunya.bi["ci1"]
    assert (await client.post(_url(santiye), headers=admin)).status_code == 200
    resp = await client.patch(
        _url(santiye, "/leaves"), headers=admin,
        json={"leaves": [{"boq_item_id": str(ci1.id), "contractor_type": "subcon"}]},
    )  # fmt: skip
    assert resp.status_code == 200, resp.text
    yap = await _yapraklar(seeded_db, santiye)
    assert yap[(ci1.id, None)] == (D("2.5"), RateSource.OFFER)


# ------------------------------------------------------------------ TKL-B6.6 (O2 / O5 / O6)


async def _denetim_sayisi(db) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.detail.like("Bütçe sözleşmeden dolduruldu%"))
        )
        or 0
    )


async def test_yalniz_esleme_yazan_doldurma_da_denetlenir(
    client, admin, seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    """O2: oran yazilmasa da (yalniz grup eslemesi) denetim satiri yazilir (P5)."""
    await _kur(seeded_db, proje, santiye, katalog)
    proje.start_date, proje.end_date = w.START, w.END
    assert (await client.post(_url(santiye), headers=admin)).status_code == 200
    ilk = await _denetim_sayisi(seeded_db)
    for row in (await seeded_db.execute(select(EvGroupDiscipline))).scalars():
        await seeded_db.delete(row)
    await seeded_db.flush()

    out = (await client.post(_url(santiye), headers=admin)).json()

    assert (out["filled_leaf_count"], out["mapped_group_count"]) == (0, 2)
    assert await _denetim_sayisi(seeded_db) == ilk + 1
    metinler = (
        await seeded_db.scalars(
            select(AuditLog.detail).where(AuditLog.detail.like("Bütçe sözleşmeden dolduruldu%"))
        )
    ).all()
    assert any(t.endswith("· 0 satır · 2 grup eşlemesi") for t in metinler), metinler
    # sifir yazma = denetim YOK
    assert (await client.post(_url(santiye), headers=admin)).status_code == 200
    assert await _denetim_sayisi(seeded_db) == ilk + 1


async def test_mevcut_pencere_ezilmez_eksik_pencere_tamamlanir(
    client, admin, seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    """O5/MD: doldur eksik `(disiplin, NULL)` pencereyi proje tarihiyle yazar, mevcudu EZMEZ."""
    kab, duv = disiplinler
    await _kur(seeded_db, proje, santiye, katalog)
    proje.start_date, proje.end_date = w.START, w.END
    assert (await client.post(_url(santiye), headers=admin)).status_code == 200
    pencereler = {r.discipline_id: r for r in (await seeded_db.execute(select(EvWindow))).scalars()}
    kendi = (date(2026, 7, 1), date(2026, 9, 30))
    pencereler[kab.id].start_date, pencereler[kab.id].end_date = kendi  # elle girilmis
    await seeded_db.delete(pencereler[duv.id])  # eksik
    await seeded_db.flush()

    out = (await client.post(_url(santiye), headers=admin)).json()

    assert out["warnings"][-1]["code"] != "no_project_dates"
    sonra = {
        r.discipline_id: (r.start_date, r.end_date)
        for r in (await seeded_db.execute(select(EvWindow))).scalars()
    }
    await seeded_db.refresh(pencereler[kab.id])
    assert sonra == {kab.id: kendi, duv.id: (w.START, w.END)}


async def test_projede_tarih_yoksa_pencere_yazilmaz_uyari_doner(
    client, admin, seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    await _kur(seeded_db, proje, santiye, katalog)
    assert proje.start_date is None and proje.end_date is None

    out = (await client.post(_url(santiye), headers=admin)).json()

    assert "no_project_dates" in [x["code"] for x in out["warnings"]]
    assert (await seeded_db.execute(select(EvWindow))).scalars().all() == []


async def test_orani_bos_ama_satiri_olan_yaprak_guncellenir_pk_cakismaz(
    client, admin, seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    """O6/MG: onceden yazilmis (yuklenici turu) ama oransiz yaprak satiri UPDATE edilir."""
    dunya = await _kur(seeded_db, proje, santiye, katalog)
    ci1 = dunya.bi["ci1"]
    ok = await client.patch(
        _url(santiye, "/leaves"), headers=admin,
        json={"leaves": [{"boq_item_id": str(ci1.id), "contractor_type": "subcon"}]},
    )  # fmt: skip
    assert ok.status_code == 200, ok.text
    rev = (await seeded_db.execute(select(EvRevision))).scalar_one()
    onceki = (
        await seeded_db.execute(
            select(EvLeafSettings).where(
                EvLeafSettings.revision_id == rev.id, EvLeafSettings.boq_item_id == ci1.id
            )
        )
    ).scalar_one()
    assert onceki.unit_mhr is None  # satir VAR, oran BOS

    resp = await client.post(_url(santiye), headers=admin)

    assert resp.status_code == 200, resp.text
    satirlar = (
        (
            await seeded_db.execute(
                select(EvLeafSettings).where(
                    EvLeafSettings.revision_id == rev.id, EvLeafSettings.boq_item_id == ci1.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(satirlar) == 1
    await seeded_db.refresh(satirlar[0])
    assert (satirlar[0].unit_mhr, satirlar[0].rate_source) == (D("2.5"), RateSource.OFFER)
    assert satirlar[0].contractor_type is not None  # elle girilen tur korundu


async def test_yazilacak_sey_yoksa_taslak_acilmaz(
    client, admin, seeded_db, proje, santiye, katalog, disiplinler
) -> None:
    """O6/MI: sozlesme baglantisi olmayan santiyede doldur revizyon ACMAZ, denetim yazmaz."""
    await w.kur(seeded_db, proje, santiye, katalog, boq=False)
    proje.start_date, proje.end_date = w.START, w.END

    resp = await client.post(_url(santiye), headers=admin)

    assert resp.status_code == 200, resp.text
    assert (await seeded_db.execute(select(EvRevision))).scalars().all() == []
    assert await _denetim_sayisi(seeded_db) == 0

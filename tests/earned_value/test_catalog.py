"""PLN-B1.3 — birim oran katalogu (KAT; PLANLAMA-SPEC §3.8 K4, §3.9 B1-4/B1-9).

Gerceklesen (K4) saha verisidir ve PLN-B2'de dogar: B1'de `catalog_actuals` her kalem
icin BOS doner. "Gerceklesen standart yap" bu yuzden B1'de HER ZAMAN 409'dur; ortalamali
yol `catalog_actuals` monkeypatch'lenerek sinanir.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.catalog.models import EvCatalogItem, EvDiscipline
from app.modules.earned_value import catalog_service, guards
from app.modules.earned_value.models import EvItemSettings, EvRevision, RevisionStatus
from app.modules.earned_value.schemas_catalog import CatalogActual, CatalogActualSite
from app.modules.sites.models import Site

from .conftest import audit_count, audit_details

URL = "/earned-value/catalog"


def new(discipline: EvDiscipline, **overrides) -> dict:
    return {
        "discipline_id": str(discipline.id),
        "name": "Beton döküm",
        "uom": "m³",
        "standard_unit_mhr": "1.80",
        "default_contractor_type": "own",
        "description": "C30 pompa ile döküm + vibrasyon",
        **overrides,
    }


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _loc_fields(resp) -> set[str]:
    return {str(err["loc"][-1]) for err in resp.json()["detail"]}


def _fake_actuals(avg: str):
    async def _actuals(session, item_ids):
        site = CatalogActualSite(
            site_id=uuid.uuid4(),
            site_name="Güneşkent B-Blok",
            end_date=None,
            qty=Decimal("5400"),
            rate=Decimal(avg),
        )
        return {
            item_id: CatalogActual(
                avg=Decimal(avg), min=Decimal(avg), max=Decimal(avg), site_count=1, sites=[site]
            )
            for item_id in item_ids
        }

    return _actuals


# ------------------------------------------------------------------- liste


async def test_liste_satiri_bos_gerceklesen_ve_disiplin_ozeti(
    client: AsyncClient, admin, kab: EvDiscipline, katalog_fabrikasi
) -> None:
    await katalog_fabrikasi(kab, "Beton döküm", description="C30")
    resp = await client.get(URL, headers=admin)
    assert resp.status_code == 200, resp.text
    [row] = resp.json()
    assert row["discipline"] == {
        "id": str(kab.id),
        "code": "KAB",
        "name": "Kaba İnşaat",
        "color": "#2563EB",
    }
    assert Decimal(row["standard_unit_mhr"]) == Decimal("1.8")
    assert row["actual"] == {"avg": None, "min": None, "max": None, "site_count": 0, "sites": []}
    assert row["diff_pct"] is None
    assert row["used_by_site_count"] == 0
    assert row["standard_updated_at"] is not None
    assert row["description"] == "C30"


async def test_liste_disiplin_sonra_ad_sirali_ve_suzgecler(
    client: AsyncClient, admin, disiplin_fabrikasi, katalog_fabrikasi
) -> None:
    kab = await disiplin_fabrikasi("KAB", sort_order=1)
    duv = await disiplin_fabrikasi("DUV", sort_order=2)
    await katalog_fabrikasi(duv, "Tuğla duvar", uom="m²")
    await katalog_fabrikasi(kab, "Kalıp", uom="m²")
    await katalog_fabrikasi(kab, "Beton döküm")
    await katalog_fabrikasi(kab, "100%_özel")

    names = [r["name"] for r in (await client.get(URL, headers=admin)).json()]
    assert names == ["100%_özel", "Beton döküm", "Kalıp", "Tuğla duvar"]

    only_duv = await client.get(URL, params={"discipline_id": str(duv.id)}, headers=admin)
    assert [r["name"] for r in only_duv.json()] == ["Tuğla duvar"]

    search = await client.get(URL, params={"q": "BETON"}, headers=admin)
    assert [r["name"] for r in search.json()] == ["Beton döküm"]

    # LIKE joker karakterleri kacirilir: "%" yalniz gercekten "%" iceren adi bulur.
    literal = await client.get(URL, params={"q": "%_"}, headers=admin)
    assert [r["name"] for r in literal.json()] == ["100%_özel"]


async def test_kullanan_santiye_sayisi_distinct_santiye(
    client: AsyncClient,
    admin,
    kab: EvDiscipline,
    katalog_fabrikasi,
    santiye: Site,
    ikinci_santiye: Site,
    seeded_db: AsyncSession,
) -> None:
    item = await katalog_fabrikasi(kab, "Beton döküm")
    other = await katalog_fabrikasi(kab, "Kalıp", uom="m²")

    async def bind(site: Site, number: int, status: RevisionStatus) -> None:
        rev = EvRevision(site_id=site.id, number=number, status=status)
        if status is not RevisionStatus.DRAFT:
            rev.frozen_at = datetime.now(UTC)
        group = BoqGroup(site_id=site.id, name=f"G{number}")
        seeded_db.add_all([rev, group])
        await seeded_db.flush()
        boq = BoqItem(
            site_id=site.id,
            group_id=group.id,
            code=f"K{number}",
            description="Beton",
            unit="m³",
            quantity=Decimal("1"),
            unit_price=Decimal("1"),
        )
        seeded_db.add(boq)
        await seeded_db.flush()
        seeded_db.add(
            EvItemSettings(revision_id=rev.id, boq_item_id=boq.id, catalog_item_id=item.id)
        )
        await seeded_db.flush()

    # Ayni santiyede iki revizyon (aktif + taslak) + ikinci santiye → 2 santiye.
    await bind(santiye, 0, RevisionStatus.ACTIVE)
    await bind(santiye, 1, RevisionStatus.DRAFT)
    await bind(ikinci_santiye, 0, RevisionStatus.DRAFT)

    rows = {r["name"]: r for r in (await client.get(URL, headers=admin)).json()}
    assert rows["Beton döküm"]["used_by_site_count"] == 2
    assert rows["Kalıp"]["used_by_site_count"] == 0
    assert other.id  # ikinci kalem sayima karismaz


async def test_liste_gerceklesen_varsa_diff_orani(
    client: AsyncClient, admin, kab: EvDiscipline, katalog_fabrikasi, monkeypatch
) -> None:
    await katalog_fabrikasi(kab, "Beton döküm", rate="2.0000")
    monkeypatch.setattr(catalog_service, "catalog_actuals", _fake_actuals("2.3"))
    [row] = (await client.get(URL, headers=admin)).json()
    assert Decimal(row["diff_pct"]) == Decimal("0.15")  # (2,3 − 2) ÷ 2, ham oran
    assert row["actual"]["site_count"] == 1
    assert row["actual"]["sites"][0]["site_name"] == "Güneşkent B-Blok"


async def test_catalog_actuals_tamamlanmis_santiye_yoksa_bos(seeded_db) -> None:
    """K4: gerceklesen yalniz TAMAMLANMIS santiyeden gelir; yoksa her kimlik icin bos."""
    ids = [uuid.uuid4(), uuid.uuid4()]
    result = await catalog_service.catalog_actuals(seeded_db, ids)
    assert set(result) == set(ids)
    for actual in result.values():
        assert (actual.avg, actual.min, actual.max, actual.site_count, actual.sites) == (
            None,
            None,
            None,
            0,
            [],
        )


# ------------------------------------------------------------------- ekle


async def test_ekle_201_ve_audit(
    client: AsyncClient, admin, kab: EvDiscipline, seeded_db: AsyncSession
) -> None:
    resp = await client.post(URL, json=new(kab), headers=admin)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "Beton döküm" and data["discipline"]["code"] == "KAB"
    assert Decimal(data["standard_unit_mhr"]) == Decimal("1.8")
    assert data["actual"]["site_count"] == 0 and data["used_by_site_count"] == 0
    assert await seeded_db.get(EvCatalogItem, uuid.UUID(data["id"])) is not None
    details = await audit_details(seeded_db)
    assert "Birim oran kataloğuna iş tipi eklendi: Beton döküm (m³)" in details


async def test_ayni_disiplin_ad_birim_409_alana_ozel(
    client: AsyncClient, admin, kab: EvDiscipline, katalog_fabrikasi, seeded_db: AsyncSession
) -> None:
    var = await katalog_fabrikasi(kab, "Beton döküm")
    before = await audit_count(seeded_db)
    resp = await client.post(URL, json=new(kab), headers=admin)
    assert resp.status_code == 409
    assert resp.json()["detail"] == guards.CATALOG_ITEM_TAKEN_AS.format(name=var.name, uom=var.uom)
    assert await audit_count(seeded_db) == before


async def test_farkli_birim_ya_da_disiplin_ayni_ad_serbest(
    client: AsyncClient, admin, disiplin_fabrikasi, katalog_fabrikasi
) -> None:
    kab = await disiplin_fabrikasi("KAB")
    duv = await disiplin_fabrikasi("DUV")
    await katalog_fabrikasi(kab, "Beton döküm")
    assert (await client.post(URL, json=new(kab, uom="m²"), headers=admin)).status_code == 201
    assert (await client.post(URL, json=new(duv), headers=admin)).status_code == 201


@pytest.mark.parametrize("rate", ["0", "-1", "1.23456", "123456789.0"])
async def test_standart_oran_pozitif_ve_hassasiyet_icinde(
    client: AsyncClient, admin, kab: EvDiscipline, rate: str
) -> None:
    resp = await client.post(URL, json=new(kab, standard_unit_mhr=rate), headers=admin)
    assert resp.status_code == 422, resp.text
    assert "standard_unit_mhr" in _loc_fields(resp)


async def test_olmayan_disiplin_404(client: AsyncClient, admin, kab: EvDiscipline) -> None:
    payload = {**new(kab), "discipline_id": str(uuid.uuid4())}
    resp = await client.post(URL, json=payload, headers=admin)
    assert resp.status_code == 404
    assert resp.json()["detail"] == guards.DISCIPLINE_MISSING


async def test_turev_alan_govdede_422(client: AsyncClient, admin, kab: EvDiscipline) -> None:
    resp = await client.post(URL, json=new(kab, used_by_site_count=3), headers=admin)
    assert resp.status_code == 422
    assert "used_by_site_count" in _loc_fields(resp)


# ------------------------------------------------------------------ guncelle


async def test_oran_degisince_standard_updated_at_yenilenir(
    client: AsyncClient, admin, kab: EvDiscipline, katalog_fabrikasi, seeded_db: AsyncSession
) -> None:
    item = await katalog_fabrikasi(kab, "Beton döküm")
    stamp = (await client.get(URL, headers=admin)).json()[0]["standard_updated_at"]

    renamed = await client.patch(f"{URL}/{item.id}", json={"name": "Beton"}, headers=admin)
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["standard_updated_at"] == stamp  # oran degismedi

    same_rate = await client.patch(
        f"{URL}/{item.id}", json={"standard_unit_mhr": "1.8"}, headers=admin
    )
    assert same_rate.json()["standard_updated_at"] == stamp  # deger ayni

    changed = await client.patch(
        f"{URL}/{item.id}", json={"standard_unit_mhr": "2.05"}, headers=admin
    )
    assert changed.status_code == 200
    assert Decimal(changed.json()["standard_unit_mhr"]) == Decimal("2.05")
    assert _ts(changed.json()["standard_updated_at"]) > _ts(stamp)
    assert "Birim oran kataloğu iş tipi güncellendi: Beton (m³)" in await audit_details(seeded_db)


@pytest.mark.parametrize(
    "field", ["discipline_id", "name", "uom", "standard_unit_mhr", "default_contractor_type"]
)
async def test_patch_acik_null_422_alan_adli(
    client: AsyncClient, admin, kab: EvDiscipline, katalog_fabrikasi, field: str
) -> None:
    item = await katalog_fabrikasi(kab, "Beton döküm")
    resp = await client.patch(f"{URL}/{item.id}", json={field: None}, headers=admin)
    assert resp.status_code == 422
    assert field in _loc_fields(resp)


async def test_patch_aciklama_null_temizler(
    client: AsyncClient, admin, kab: EvDiscipline, katalog_fabrikasi
) -> None:
    item = await katalog_fabrikasi(kab, "Beton döküm", description="C30")
    resp = await client.patch(f"{URL}/{item.id}", json={"description": None}, headers=admin)
    assert resp.status_code == 200 and resp.json()["description"] is None


async def test_patch_baska_kalemle_cakisirsa_409(
    client: AsyncClient, admin, disiplin_fabrikasi, katalog_fabrikasi
) -> None:
    kab = await disiplin_fabrikasi("KAB")
    duv = await disiplin_fabrikasi("DUV")
    var = await katalog_fabrikasi(kab, "Beton döküm")
    moving = await katalog_fabrikasi(duv, "Beton döküm")
    resp = await client.patch(
        f"{URL}/{moving.id}", json={"discipline_id": str(kab.id)}, headers=admin
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == guards.CATALOG_ITEM_TAKEN_AS.format(name=var.name, uom=var.uom)


async def test_patch_olmayan_kalem_404(client: AsyncClient, admin) -> None:
    resp = await client.patch(f"{URL}/{uuid.uuid4()}", json={"name": "X"}, headers=admin)
    assert resp.status_code == 404
    assert resp.json()["detail"] == guards.CATALOG_ITEM_MISSING


async def test_silme_ucu_yok(client: AsyncClient, admin, kab, katalog_fabrikasi) -> None:
    item = await katalog_fabrikasi(kab, "Beton döküm")
    assert (await client.delete(f"{URL}/{item.id}", headers=admin)).status_code == 405


# ------------------------------------------------------- gerceklesen standart yap


async def test_adopt_b1de_her_zaman_409_ve_audit_yok(
    client: AsyncClient, admin, kab: EvDiscipline, katalog_fabrikasi, seeded_db: AsyncSession
) -> None:
    item = await katalog_fabrikasi(kab, "Beton döküm")
    before = await audit_count(seeded_db)
    resp = await client.post(f"{URL}/{item.id}/adopt-actual", headers=admin)
    assert resp.status_code == 409
    assert resp.json()["detail"] == guards.CATALOG_NO_ACTUAL
    assert await audit_count(seeded_db) == before
    await seeded_db.refresh(item)
    assert item.standard_unit_mhr == Decimal("1.8")


async def test_adopt_ortalama_varsa_standart_olur_ve_audit_yazar(
    client: AsyncClient,
    admin,
    kab: EvDiscipline,
    katalog_fabrikasi,
    seeded_db: AsyncSession,
    monkeypatch,
) -> None:
    item = await katalog_fabrikasi(kab, "Beton döküm", rate="1.8000")
    stamp = (await client.get(URL, headers=admin)).json()[0]["standard_updated_at"]
    monkeypatch.setattr(catalog_service, "catalog_actuals", _fake_actuals("2.0512345"))

    resp = await client.post(f"{URL}/{item.id}/adopt-actual", headers=admin)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert Decimal(data["standard_unit_mhr"]) == Decimal("2.0512")  # Numeric(12,4) kuantumu
    assert _ts(data["standard_updated_at"]) > _ts(stamp)
    await seeded_db.refresh(item)
    assert item.standard_unit_mhr == Decimal("2.0512")
    details = await audit_details(seeded_db)
    assert "Gerçekleşen standart yapıldı: Beton döküm (m³) · 1,8 → 2,0512 a-s/birim" in details


async def test_adopt_olmayan_kalem_404(client: AsyncClient, admin) -> None:
    resp = await client.post(f"{URL}/{uuid.uuid4()}/adopt-actual", headers=admin)
    assert resp.status_code == 404
    assert resp.json()["detail"] == guards.CATALOG_ITEM_MISSING


# --- EV-BORC-5: tekillik öneri eşleşmesiyle AYNI normalizasyonla ---------------------------


@pytest.mark.parametrize(
    ("ad", "birim"),
    [("BETON DÖKÜM", "m³"), ("  beton   döküm ", "m³"), ("Beton döküm", "M3")],
    ids=["buyuk-harf", "bosluk", "birim-ust-simge"],
)
async def test_EVBORC5_normalized_duplicate_is_409(
    client: AsyncClient, admin, kab: EvDiscipline, katalog_fabrikasi, ad: str, birim: str
) -> None:
    var = await katalog_fabrikasi(kab, "Beton döküm")
    resp = await client.post(URL, json=new(kab, name=ad, uom=birim), headers=admin)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.CATALOG_ITEM_TAKEN_AS.format(name=var.name, uom=var.uom)


async def test_EVBORC5_turkish_dotted_i_duplicate_is_409(
    client: AsyncClient, admin, kab: EvDiscipline, katalog_fabrikasi
) -> None:
    """«KİREÇ SIVA» ile «kireç sıva» aynı iş tipidir.

    `casefold` İ'yi "i̇" yapardı; `normalize_label` Türkçe harfleri elle çevirir."""
    await katalog_fabrikasi(kab, "kireç sıva")
    resp = await client.post(URL, json=new(kab, name="KİREÇ SIVA"), headers=admin)
    assert resp.status_code == 409, resp.text


async def test_EVBORC5_different_uom_is_not_duplicate_and_rename_to_variant_is_409(
    client: AsyncClient, admin, kab: EvDiscipline, katalog_fabrikasi
) -> None:
    await katalog_fabrikasi(kab, "Beton döküm")
    other = await client.post(URL, json=new(kab, name="beton DÖKÜM", uom="ton"), headers=admin)
    assert other.status_code == 201, other.text  # birim farklı → ayrı iş tipi
    kalip = await katalog_fabrikasi(kab, "Kalıp")
    resp = await client.patch(f"{URL}/{kalip.id}", json={"name": "BETON DÖKÜM"}, headers=admin)
    assert resp.status_code == 409, resp.text

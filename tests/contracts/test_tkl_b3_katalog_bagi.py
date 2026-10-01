"""TKL-B3.1 — sozlesme kalemi katalog iz bagi + fiyat degisim damgasi + toplu ekleme ucu.

Kapsam: tekil/toplu olusturmada `catalog_item_id` (varlik dogrulamasi: yoksa 404), bagin
SABITLIGI (PATCH'te 422), `price_changed_at` kurallari (yalniz `unit_price` DEGER olarak
degisince ilerler; API'de DONMEZ), toplu ucun hep-ya-hic dogrulamasi, kapi (izin/kapsam) ve
tek denetim satiri.
"""

import asyncio
import uuid
from contextlib import contextmanager
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select

from app.core.db import get_db
from app.main import app
from app.modules.audit import messages
from app.modules.audit.models import AuditLog
from app.modules.catalog.models import EvCatalogItem, EvDiscipline
from app.modules.catalog.service import next_poz_no
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.contracts.schemas import EMPLOYER_ITEMS_BULK_MAX
from app.modules.earned_value.engine import ContractorType
from app.modules.projects.models import ProjectContract

KATALOG_YOK = "Katalog iş tipi bulunamadı"
BAG_DEGISMEZ = "Katalog bağı sonradan değiştirilemez"


def _govde(grup, code="03.001", **kwargs) -> dict:
    govde = {
        "group_id": str(grup),
        "code": code,
        "description": "Beton",
        "unit": "m³",
        "quantity": 100,
        "unit_price": 1850,
    }
    govde.update(kwargs)
    return govde


@pytest.fixture
async def proje(seeded_db, project_factory):
    project = await project_factory(code="TKL-B31-01", name="Katalog Bağı Projesi")
    seeded_db.add(
        ProjectContract(
            project_id=project.id,
            contract_no="SZL-B31",
            amount=Decimal("1000000"),
            advance_pct=Decimal("10"),
        )
    )
    await seeded_db.flush()
    return project


@pytest.fixture
async def grup(seeded_db, proje) -> uuid.UUID:
    group = EmployerContractGroup(project_id=proje.id, name="A — Kaba İşler", sort_order=0)
    seeded_db.add(group)
    await seeded_db.flush()
    return group.id


@pytest.fixture
async def katalog(seeded_db) -> list[uuid.UUID]:
    disiplin = EvDiscipline(
        code="KB1",
        name="Kaba",
        color="#2563eb",
        default_contractor_type=ContractorType.OWN,
    )
    seeded_db.add(disiplin)
    await seeded_db.flush()
    kalemler = []
    for ad in ("Beton", "Kalıp", "Demir"):
        kalemler.append(
            EvCatalogItem(
                poz_no=await next_poz_no(seeded_db, disiplin),
                discipline_id=disiplin.id,
                name=ad,
                uom="m3",
                standard_unit_mhr=Decimal("1.5"),
                default_contractor_type=ContractorType.OWN,
            )
        )
    seeded_db.add_all(kalemler)
    await seeded_db.flush()
    return [k.id for k in kalemler]


async def _satir(db, item_id) -> EmployerContractItem:
    row = (
        await db.execute(
            select(EmployerContractItem)
            .where(EmployerContractItem.id == uuid.UUID(str(item_id)))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row


async def _kalem_sayisi(db, project_id) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(EmployerContractItem)
        .where(EmployerContractItem.project_id == project_id)
    )


async def _audit_sayisi(db) -> int:
    return await db.scalar(select(func.count()).select_from(AuditLog))


# --------------------------------------------------------------- tekil olusturma


@pytest.mark.asyncio
async def test_tekil_baglidir_ve_okuma_yanitinda_catalog_item_id_doner(
    client, admin_headers, proje, grup, katalog
):
    yanit = await client.post(
        f"/projects/{proje.id}/contract/items",
        json=_govde(grup, catalog_item_id=str(katalog[0])),
        headers=admin_headers,
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["catalog_item_id"] == str(katalog[0])
    assert "price_changed_at" not in yanit.json()

    liste = await client.get(f"/projects/{proje.id}/contract/items", headers=admin_headers)
    assert liste.status_code == 200, liste.text
    (kalem,) = liste.json()["groups"][0]["items"]
    assert kalem["catalog_item_id"] == str(katalog[0])
    assert "price_changed_at" not in kalem


@pytest.mark.asyncio
async def test_tekil_bagsiz_olusur_catalog_item_id_null(client, admin_headers, proje, grup):
    yanit = await client.post(
        f"/projects/{proje.id}/contract/items", json=_govde(grup), headers=admin_headers
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["catalog_item_id"] is None


@pytest.mark.asyncio
async def test_tekil_olmayan_katalog_404_ve_yazilmaz(client, admin_headers, proje, grup, seeded_db):
    yanit = await client.post(
        f"/projects/{proje.id}/contract/items",
        json=_govde(grup, catalog_item_id=str(uuid.uuid4())),
        headers=admin_headers,
    )
    assert yanit.status_code == 404, yanit.text
    assert KATALOG_YOK in yanit.text
    assert await _kalem_sayisi(seeded_db, proje.id) == 0


# --------------------------------------------------------------- PATCH: bag sabit


@pytest.mark.asyncio
@pytest.mark.parametrize("deger", ["yeni", "null"])
async def test_patch_catalog_item_id_422_ve_bag_degismez(
    client, admin_headers, proje, grup, katalog, seeded_db, deger
):
    olustur = await client.post(
        f"/projects/{proje.id}/contract/items",
        json=_govde(grup, catalog_item_id=str(katalog[0])),
        headers=admin_headers,
    )
    kalem_id = olustur.json()["id"]
    yeni = str(katalog[1]) if deger == "yeni" else None

    yanit = await client.patch(
        f"/contracts/employer/items/{kalem_id}",
        json={"catalog_item_id": yeni, "description": "Değişmemeli"},
        headers=admin_headers,
    )

    assert yanit.status_code == 422, yanit.text
    assert BAG_DEGISMEZ in yanit.text
    satir = await _satir(seeded_db, kalem_id)
    assert satir.catalog_item_id == katalog[0]
    assert satir.description == "Beton"


@pytest.mark.asyncio
async def test_patch_baska_alan_bagi_korur(client, admin_headers, proje, grup, katalog, seeded_db):
    olustur = await client.post(
        f"/projects/{proje.id}/contract/items",
        json=_govde(grup, catalog_item_id=str(katalog[0])),
        headers=admin_headers,
    )
    kalem_id = olustur.json()["id"]

    yanit = await client.patch(
        f"/contracts/employer/items/{kalem_id}",
        json={"description": "Yeni açıklama", "unit_price": 2000},
        headers=admin_headers,
    )

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["catalog_item_id"] == str(katalog[0])
    assert (await _satir(seeded_db, kalem_id)).catalog_item_id == katalog[0]


# --------------------------------------------------------------- price_changed_at


@pytest.mark.asyncio
async def test_price_changed_at_olusturmada_dolu_fiyat_degisince_ilerler_digerinde_ilerlemez(
    client, admin_headers, proje, grup, seeded_db
):
    olustur = await client.post(
        f"/projects/{proje.id}/contract/items",
        json=_govde(grup, unit_price="1250.5"),
        headers=admin_headers,
    )
    kalem_id = olustur.json()["id"]
    t0 = (await _satir(seeded_db, kalem_id)).price_changed_at
    assert t0 is not None

    async def damga() -> object:
        return (await _satir(seeded_db, kalem_id)).price_changed_at

    # aynı değer, farklı yazım: 1250.5 == 1250.50 → ilerlemez
    r = await client.patch(
        f"/contracts/employer/items/{kalem_id}",
        json={"unit_price": "1250.50"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert await damga() == t0

    # başka alan değişince ilerlemez
    r = await client.patch(
        f"/contracts/employer/items/{kalem_id}",
        json={"description": "Başka", "quantity": 7},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert await damga() == t0

    # fiyat değişince ilerler
    await asyncio.sleep(0.01)
    r = await client.patch(
        f"/contracts/employer/items/{kalem_id}", json={"unit_price": "1300"}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    t1 = await damga()
    assert t1 > t0

    # yeniden değişince yine ilerler
    await asyncio.sleep(0.01)
    r = await client.patch(
        f"/contracts/employer/items/{kalem_id}", json={"unit_price": "1301"}, headers=admin_headers
    )
    assert await damga() > t1


# --------------------------------------------------------------- toplu uc


def _bulk(grup, katalog, adet=3, **ilk) -> dict:
    kalemler = [
        _govde(
            grup,
            code=f"05.{n:03d}",
            description=f"Poz {n}",
            sort_order=n,
            catalog_item_id=str(katalog[n % len(katalog)]),
        )
        for n in range(adet)
    ]
    kalemler[0].update(ilk)
    return {"items": kalemler}


@pytest.mark.asyncio
async def test_toplu_mutlu_yol_sira_kod_ve_tek_denetim_satiri(
    client, admin_headers, proje, grup, katalog, seeded_db
):
    once = await _audit_sayisi(seeded_db)

    yanit = await client.post(
        f"/projects/{proje.id}/contract/items/bulk",
        json=_bulk(grup, katalog),
        headers=admin_headers,
    )

    assert yanit.status_code == 201, yanit.text
    govde = yanit.json()["items"]
    assert [k["code"] for k in govde] == ["05.000", "05.001", "05.002"]
    assert [k["catalog_item_id"] for k in govde] == [str(katalog[n]) for n in range(3)]
    assert all(Decimal(k["distributed_quantity"]) == 0 for k in govde)
    assert await _kalem_sayisi(seeded_db, proje.id) == 3
    for k in govde:
        assert (await _satir(seeded_db, k["id"])).price_changed_at is not None

    assert await _audit_sayisi(seeded_db) == once + 1
    beklenen = messages.employer_contract_items_bulk_created(
        proje.name, ["05.000", "05.001", "05.002"]
    )
    satirlar = (await seeded_db.scalars(select(AuditLog).where(AuditLog.detail == beklenen))).all()
    assert len(satirlar) == 1


def test_toplu_denetim_metni_kodlari_kisaltir():
    kodlar = [f"K{n}" for n in range(messages.BULK_AUDIT_CODES_SHOWN + 3)]
    metin = messages.employer_contract_items_bulk_created("P", kodlar)
    assert f"{len(kodlar)} poz eklendi" in metin
    assert "K9" in metin and "K10" not in metin
    assert metin.endswith("(+3)")


@pytest.mark.asyncio
async def test_toplu_govde_ici_kod_tekrari_409_hicbiri_yazilmaz(
    client, admin_headers, proje, grup, katalog, seeded_db
):
    govde = _bulk(grup, katalog)
    govde["items"][2]["code"] = govde["items"][0]["code"]
    once = await _audit_sayisi(seeded_db)

    yanit = await client.post(
        f"/projects/{proje.id}/contract/items/bulk", json=govde, headers=admin_headers
    )

    assert yanit.status_code == 409, yanit.text
    assert "05.000" in yanit.text and "zaten kullanılıyor" in yanit.text
    assert await _kalem_sayisi(seeded_db, proje.id) == 0
    assert await _audit_sayisi(seeded_db) == once


@pytest.mark.asyncio
async def test_toplu_veritabani_kod_cakismasi_409_hicbiri_yazilmaz(
    client, admin_headers, proje, grup, katalog, seeded_db
):
    mevcut = await client.post(
        f"/projects/{proje.id}/contract/items",
        json=_govde(grup, code="05.002"),
        headers=admin_headers,
    )
    assert mevcut.status_code == 201, mevcut.text
    once = await _audit_sayisi(seeded_db)

    yanit = await client.post(
        f"/projects/{proje.id}/contract/items/bulk",
        json=_bulk(grup, katalog),
        headers=admin_headers,
    )

    assert yanit.status_code == 409, yanit.text
    assert "05.002" in yanit.text
    assert await _kalem_sayisi(seeded_db, proje.id) == 1  # yalnız önceden var olan
    assert await _audit_sayisi(seeded_db) == once


@pytest.mark.asyncio
async def test_toplu_olmayan_katalog_404_hicbiri_yazilmaz(
    client, admin_headers, proje, grup, katalog, seeded_db
):
    govde = _bulk(grup, katalog)
    govde["items"][2]["catalog_item_id"] = str(uuid.uuid4())

    yanit = await client.post(
        f"/projects/{proje.id}/contract/items/bulk", json=govde, headers=admin_headers
    )

    assert yanit.status_code == 404, yanit.text
    assert KATALOG_YOK in yanit.text
    assert await _kalem_sayisi(seeded_db, proje.id) == 0


@pytest.mark.asyncio
async def test_toplu_baska_projenin_grubu_422_hicbiri_yazilmaz(
    client, admin_headers, proje, grup, katalog, seeded_db, project_factory
):
    diger = await project_factory(code="TKL-B31-02", name="Diğer Proje")
    seeded_db.add(
        ProjectContract(project_id=diger.id, contract_no="SZL-B31-2", amount=Decimal("1"))
    )
    await seeded_db.flush()
    yabanci = EmployerContractGroup(project_id=diger.id, name="Yabancı", sort_order=0)
    seeded_db.add(yabanci)
    await seeded_db.flush()
    govde = _bulk(grup, katalog)
    govde["items"][1]["group_id"] = str(yabanci.id)

    yanit = await client.post(
        f"/projects/{proje.id}/contract/items/bulk", json=govde, headers=admin_headers
    )

    assert yanit.status_code == 422, yanit.text
    assert await _kalem_sayisi(seeded_db, proje.id) == 0
    assert await _kalem_sayisi(seeded_db, diger.id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("adet", [0, EMPLOYER_ITEMS_BULK_MAX + 1])
async def test_toplu_adet_siniri_422(client, admin_headers, proje, grup, katalog, adet):
    govde = {"items": [_govde(grup, code=f"K{n}") for n in range(adet)]}
    yanit = await client.post(
        f"/projects/{proje.id}/contract/items/bulk", json=govde, headers=admin_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_toplu_ust_sinir_tam_kabul_edilir(client, admin_headers, proje, grup, seeded_db):
    govde = {"items": [_govde(grup, code=f"K{n}") for n in range(EMPLOYER_ITEMS_BULK_MAX)]}
    yanit = await client.post(
        f"/projects/{proje.id}/contract/items/bulk", json=govde, headers=admin_headers
    )
    assert yanit.status_code == 201, yanit.text
    assert await _kalem_sayisi(seeded_db, proje.id) == EMPLOYER_ITEMS_BULK_MAX


@pytest.mark.asyncio
async def test_toplu_yetkisiz_rol_403_kapsam_disi_proje_404_ve_yazilmaz(
    client, site_chief_headers, kisitli_headers, proje, grup, katalog, seeded_db
):
    yetkisiz = await client.post(
        f"/projects/{proje.id}/contract/items/bulk",
        json=_bulk(grup, katalog),
        headers=site_chief_headers,
    )
    assert yetkisiz.status_code == 403, yetkisiz.text

    # `kisitli_headers` kullanıcısının kapsamında olmayan proje: tekil uçla AYNI 404
    govde = _bulk(grup, katalog)
    tekil = await client.post(
        f"/projects/{proje.id}/contract/items", json=govde["items"][0], headers=kisitli_headers
    )
    toplu = await client.post(
        f"/projects/{proje.id}/contract/items/bulk", json=govde, headers=kisitli_headers
    )
    assert tekil.status_code == toplu.status_code == 404, (tekil.text, toplu.text)
    assert await _kalem_sayisi(seeded_db, proje.id) == 0


@pytest.mark.asyncio
async def test_toplu_sozlesmesiz_proje_404(client, admin_headers, project_factory):
    bos = await project_factory(code="TKL-B31-03", name="Sözleşmesiz")
    yanit = await client.post(
        f"/projects/{bos.id}/contract/items/bulk",
        json={"items": [_govde(uuid.uuid4())]},
        headers=admin_headers,
    )
    assert yanit.status_code == 404, yanit.text


def test_toplu_yanit_sarmalayicisinda_kapsam_maskesi_para_alanini_gizler():
    """Yanıt `BaseModel` olduğu için rota sarmalayıcısı `items[]` içindeki para
    alanını (`unit_price`, `Gorunurluk.para`) kapsama göre `None`a çeker."""
    from app.core.access import Scope
    from app.core.field_scope import maskele
    from app.modules.contracts.schemas import (
        EmployerContractItemResponse,
        EmployerContractItemsBulkResponse,
    )

    kalem = EmployerContractItemResponse(
        id=uuid.uuid4(),
        group_id=uuid.uuid4(),
        code="K1",
        description="x",
        unit="m",
        quantity=Decimal("1"),
        unit_price=Decimal("100"),
        sort_order=0,
        catalog_item_id=None,
        distributed_quantity=Decimal("0"),
        remaining_quantity=Decimal("1"),
    )
    yanit = EmployerContractItemsBulkResponse(items=[kalem])

    assert maskele(yanit, Scope.all).items[0].unit_price == Decimal("100")
    assert maskele(yanit, Scope.limited).items[0].quantity == Decimal("1")
    assert maskele(yanit, Scope.limited).items[0].unit_price is None


# --------------------------------------------------------------- TKL-B3.3 onarimlari


@pytest.mark.asyncio
async def test_price_changed_at_kurus_alti_ilerletmez_yuvarlama_ilerletir(
    client, admin_headers, proje, grup, seeded_db
):
    """S3a: karşılaştırma DB ölçeğinde (`_quantize_money`): 1250.504 → 1250.50 (değişim
    YOK, damga sabit); 1250.505 → 1250.51 (değişim VAR, damga ilerler)."""
    olustur = await client.post(
        f"/projects/{proje.id}/contract/items",
        json=_govde(grup, unit_price="1250.50"),
        headers=admin_headers,
    )
    kalem_id = olustur.json()["id"]
    t0 = (await _satir(seeded_db, kalem_id)).price_changed_at

    r = await client.patch(
        f"/contracts/employer/items/{kalem_id}",
        json={"unit_price": "1250.504"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    satir = await _satir(seeded_db, kalem_id)
    assert satir.unit_price == Decimal("1250.50")
    assert satir.price_changed_at == t0

    await asyncio.sleep(0.01)
    r = await client.patch(
        f"/contracts/employer/items/{kalem_id}",
        json={"unit_price": "1250.505"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    satir = await _satir(seeded_db, kalem_id)
    assert satir.unit_price == Decimal("1250.51")
    assert satir.price_changed_at > t0


@contextmanager
def _sayac():
    from tests.conftest import test_engine

    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)


@pytest.mark.asyncio
async def test_toplu_sorgu_sayisi_kalem_sayisindan_bagimsiz(
    client, admin_headers, proje, grup, seeded_db
):
    """S2: katalog varlığı TEK sorguyla doğrulanır, kalem başına refresh YOK — 1 kalem ile
    50 kalem (hepsi ayrı katalog kalemine bağlı) AYNI sayıda sorgu çalıştırır."""
    disiplin = EvDiscipline(
        code="KB2", name="Sayaç", color="#2563eb", default_contractor_type=ContractorType.OWN
    )
    seeded_db.add(disiplin)
    await seeded_db.flush()
    kalemler = [
        EvCatalogItem(
            poz_no=await next_poz_no(seeded_db, disiplin),
            discipline_id=disiplin.id,
            name=f"Sayaç {n}",
            uom="m3",
            standard_unit_mhr=Decimal("1"),
            default_contractor_type=ContractorType.OWN,
        )
        for n in range(50)
    ]
    seeded_db.add_all(kalemler)
    await seeded_db.flush()
    ids = [k.id for k in kalemler]

    def govde(adet: int, onek: str) -> dict:
        return {
            "items": [
                _govde(grup, code=f"{onek}{n:03d}", catalog_item_id=str(ids[n]))
                for n in range(adet)
            ]
        }

    pid = proje.id

    async def istek(adet: int, onek: str):
        # Aynı oturumda tohumlanan katalog nesneleri kimlik haritasında durur (`session.get`
        # sorgusuz döner → N+1 gizlenir); her ölçümden önce sıfırla.
        seeded_db.expire_all()
        return await client.post(
            f"/projects/{pid}/contract/items/bulk", json=govde(adet, onek), headers=admin_headers
        )

    # ısınma: oturum/önbellek durumu ilk ölçüme sızmasın (aynı oturum iki istekte paylaşılır)
    assert (await istek(1, "W")).status_code == 201
    with _sayac() as bir:
        r1 = await istek(1, "A")
    with _sayac() as elli:
        r50 = await istek(50, "B")

    assert r1.status_code == 201 and r50.status_code == 201, (r1.text, r50.text)
    assert len(bir) == len(elli), (len(bir), len(elli))
    assert (
        len(elli) <= 25
    )  # sabit üst sınır (yetki + proje + grup + katalog + kod + insert + audit)
    assert sum("FROM ev_catalog_items" in s for s in elli) == 1


@pytest.mark.asyncio
async def test_toplu_flush_tasmasi_gercek_rollback_ile_422_ve_hicbiri_kalmaz(
    client, admin_headers, proje, grup, katalog, seeded_db
):
    """S3b: doğrulamayı geçen ama flush'ta (Numeric(14,3) taşması) patlayan toplu istek —
    `get_db` benzeri commit/rollback yapan bağımlılıkla: 422 ve kalem sayısı 0."""

    async def _gercek_gibi():
        try:
            yield seeded_db
            await seeded_db.commit()
        except Exception:
            await seeded_db.rollback()
            raise

    pid, gid = proje.id, grup
    await seeded_db.commit()  # tohum dış işleme geçsin (rollback onu silmesin)
    app.dependency_overrides[get_db] = _gercek_gibi
    govde = _bulk(gid, katalog)
    govde["items"][2]["quantity"] = "999999999999"  # Numeric(14,3) taşması → flush'ta 22003

    yanit = await client.post(
        f"/projects/{pid}/contract/items/bulk", json=govde, headers=admin_headers
    )

    assert yanit.status_code == 422, yanit.text
    assert await _kalem_sayisi(seeded_db, pid) == 0
    assert await seeded_db.get(EmployerContractGroup, gid) is not None

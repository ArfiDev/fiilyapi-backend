"""BDG-B1 — `GET/PUT /sites/{site_id}/boq/section-distribution` (kalem x bolum matrisi).

Eszamanlilik testi bu dosyada DEGIL (ayri ajan). Disiplin kisitli kullanici
testleri `tests/discipline_scope/test_bdg_b1_matris.py`dedir (dunya fikturu orada).

🔴 Her sinir iki yonden iddia edilir: ESITLIK gecer, bir birim ustu reddedilir.
Sayaclar icin degismez her yerde: `distributed + unallocated == total`.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.modules.audit.models import AuditAction
from app.modules.boq import repository
from app.modules.boq.models import BoqItem, BoqItemSectionAllocation
from app.modules.sites.models import Section
from tests.conftest import test_engine
from tests.modules._boq import (
    _audit_details,
    _auth,
    _group,
    _item,
    _login_with_access,
    _site,
)

pytestmark = pytest.mark.asyncio

D = Decimal


def _url(site_id) -> str:
    return f"/sites/{site_id}/boq/section-distribution"


async def _section(session, site, name: str, sort_order: int = 0, **kw) -> Section:
    section = Section(site_id=site.id, name=name, sort_order=sort_order, **kw)
    session.add(section)
    await session.flush()
    return section


async def _alloc(session, item, section, quantity: str) -> BoqItemSectionAllocation:
    row = BoqItemSectionAllocation(boq_item_id=item.id, section_id=section.id, quantity=D(quantity))
    session.add(row)
    await session.flush()
    return row


def _cell(item, section, quantity) -> dict:
    return {
        "boq_item_id": str(item.id),
        "section_id": str(section.id),
        "quantity": quantity,
    }


async def _admin(client, db_session, user_factory, email: str = "adm@bdg.co") -> dict:
    return _auth(await _login_with_access(client, db_session, user_factory, "system_admin", email))


def _invariant(govde: dict) -> None:
    assert (
        govde["distributed_item_count"] + govde["unallocated_item_count"]
        == govde["total_item_count"]
    )
    assert govde["unallocated_item_count"] == len(govde["unallocated_item_codes"])


async def _rows(session, item_id) -> dict:
    sonuc = await session.execute(
        select(BoqItemSectionAllocation).where(BoqItemSectionAllocation.boq_item_id == item_id)
    )
    return {r.section_id: r for r in sonuc.scalars().all()}


async def _kur(client, db_session, user_factory, project_factory, kod="BDG-1"):
    """Bir santiye, 2 bolum (ikincisi TASLAK), 1 grup, iki kalem (100 ve 50)."""
    project = await project_factory(kod)
    site = await _site(db_session, project)
    s1 = await _section(db_session, site, "Kat 6-10", 1, code="K1")
    s2 = await _section(db_session, site, "Kat 11-15", 2, is_draft=True)
    grup = await _group(db_session, site)
    i1 = await _item(
        db_session, site, grup, "01.001", quantity=D("100"), unit_price=D("10.00"), sort_order=1
    )
    i2 = await _item(
        db_session, site, grup, "01.002", quantity=D("50"), unit_price=D("20.00"), sort_order=2
    )
    headers = await _admin(client, db_session, user_factory, f"adm-{kod}@bdg.co")
    return site, (s1, s2), (i1, i2), headers


# --- OKUMA -------------------------------------------------------------------


async def test_okuma_sekli_siralar_ve_taslak_bolum_kolon_olarak_gelir(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("BDG-R1")
    site = await _site(db_session, project)
    # sort_order TERS verilir: sirayi kimlik/ekleme sirasi degil sort_order belirler.
    sb = await _section(db_session, site, "B", 2)
    sa = await _section(db_session, site, "A", 1, is_draft=True)
    g2 = await _group(db_session, site, "IKINCI", sort_order=2)
    g1 = await _group(db_session, site, "BIRINCI", sort_order=1)
    k2 = await _item(db_session, site, g1, "02", sort_order=2, quantity=D("10"))
    k1 = await _item(db_session, site, g1, "01", sort_order=1, quantity=D("10"))
    await _item(db_session, site, g2, "03", quantity=D("10"))
    await _alloc(db_session, k1, sb, "4")
    await _alloc(db_session, k1, sa, "6")
    headers = await _admin(client, db_session, user_factory)

    resp = await client.get(_url(site.id), headers=headers)

    assert resp.status_code == 200, resp.text
    govde = resp.json()
    assert govde["site_id"] == str(site.id)
    assert govde["site_name"] == site.name
    assert govde["project_name"] == project.name
    # bolum sirasi sort_order; TASLAK bolum da kolon, is_draft bilgisiyle
    assert [(s["name"], s["is_draft"]) for s in govde["sections"]] == [("A", True), ("B", False)]
    assert [g["name"] for g in govde["groups"]] == ["BIRINCI", "IKINCI"]
    assert [i["code"] for i in govde["groups"][0]["items"]] == ["01", "02"]
    ilk = govde["groups"][0]["items"][0]
    # hucre sirasi bolum sirasiyla (A sonra B), kimlik/ekleme sirasiyla DEGIL
    assert [(a["section_id"], a["quantity"]) for a in ilk["allocations"]] == [
        (str(sa.id), "6.000"),
        (str(sb.id), "4.000"),
    ]
    assert ilk["allocated_quantity"] == "10.000"
    assert ilk["unallocated_quantity"] == "0.000"
    assert k2.id != k1.id
    _invariant(govde)


async def test_sayaclar_kismi_tam_ve_hic_dagitilmamis_kalem(
    client, db_session, user_factory, project_factory
):
    """Kismi (100'un 40'i) UNALLOCATED sayilir, distributed sayilmaz; tam dagitilmis distributed;
    hic payi olmayan UNALLOCATED. Kod listesi BOQ sirasinda."""
    project = await project_factory("BDG-R2")
    site = await _site(db_session, project)
    s1 = await _section(db_session, site, "S1", 1)
    g = await _group(db_session, site)
    kismi = await _item(db_session, site, g, "01.001", sort_order=1, quantity=D("100"))
    tam = await _item(db_session, site, g, "01.002", sort_order=2, quantity=D("50"))
    hic = await _item(db_session, site, g, "01.003", sort_order=3, quantity=D("30"))
    await _alloc(db_session, kismi, s1, "40")
    await _alloc(db_session, tam, s1, "50")
    headers = await _admin(client, db_session, user_factory)

    govde = (await client.get(_url(site.id), headers=headers)).json()

    assert govde["total_item_count"] == 3
    assert govde["unallocated_item_count"] == 2
    assert govde["distributed_item_count"] == 1
    assert govde["unallocated_item_codes"] == ["01.001", "01.003"]  # BOQ sirasi
    _invariant(govde)
    assert hic.id != tam.id


async def test_bolum_ozetleri_tutar_birim_ve_toplam(
    client, db_session, user_factory, project_factory
):
    site, (s1, s2), (i1, i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-R3"
    )
    await _alloc(db_session, i1, s1, "40")
    await _alloc(db_session, i2, s1, "5")
    await _alloc(db_session, i1, s2, "10")

    govde = (await client.get(_url(site.id), headers=headers)).json()

    ozet = {s["section_id"]: s for s in govde["section_summaries"]}
    a = ozet[str(s1.id)]
    assert [
        (i["code"], i["unit"], i["quantity"], i["unit_price"], i["amount"]) for i in a["items"]
    ] == [
        ("01.001", "m³", "40.000", "10.00", "400.00"),
        ("01.002", "m³", "5.000", "20.00", "100.00"),
    ]
    assert a["total_amount"] == "500.00"
    assert ozet[str(s2.id)]["total_amount"] == "100.00"


async def test_bossa_bolumler_ve_bos_grup_kisitsizda_listelenir(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("BDG-R4")
    site = await _site(db_session, project)
    await _group(db_session, site, "BOS GRUP")
    headers = await _admin(client, db_session, user_factory)

    govde = (await client.get(_url(site.id), headers=headers)).json()

    assert [g["name"] for g in govde["groups"]] == ["BOS GRUP"]
    assert govde["total_item_count"] == 0
    assert govde["distributed_item_count"] == 0
    _invariant(govde)


async def test_gorunmeyen_ve_olmayan_santiye_404_GET_ve_PUT(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("BDG-404")
    site = await _site(db_session, project)
    # Erisimi OLMAYAN kullanici: proje erisimi hic verilmez.
    from app.modules.users.models import UserProjectAccess  # noqa: PLC0415

    user = await user_factory(email="yok@bdg.co", password="parola1234", role_key="patron")
    db_session.add(UserProjectAccess(user_id=user.id, project_id=None, all_projects=False))
    await db_session.flush()
    resp = await client.post("/auth/login", json={"email": "yok@bdg.co", "password": "parola1234"})
    headers = _auth(resp.json()["access_token"])
    olmayan = uuid.uuid4()

    for sid in (site.id, olmayan):
        g = await client.get(_url(sid), headers=headers)
        p = await client.put(_url(sid), json={"allocations": []}, headers=headers)
        assert (g.status_code, p.status_code) == (404, 404)
        assert g.json() == p.json()
    gorunmeyen = (await client.get(_url(site.id), headers=headers)).json()
    assert gorunmeyen == (await client.get(_url(olmayan), headers=headers)).json()


# --- YAZMA: birlestirme -----------------------------------------------------


async def test_birlestirme_govdede_olmayan_hucre_korunur_ve_taslak_bolume_yazilir(
    client, db_session, user_factory, project_factory
):
    site, (s1, s2), (i1, i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-W1"
    )
    await _alloc(db_session, i1, s1, "30")
    await _alloc(db_session, i2, s1, "20")

    resp = await client.put(
        _url(site.id), json={"allocations": [_cell(i1, s2, "25.5")]}, headers=headers
    )

    assert resp.status_code == 200, resp.text
    satirlar = await _rows(db_session, i1.id)
    assert satirlar[s1.id].quantity == D("30")  # dokunulmadi
    assert satirlar[s2.id].quantity == D("25.500")  # TASLAK bolume yazildi
    assert (await _rows(db_session, i2.id))[s1.id].quantity == D("20")  # baska kalem korundu
    govde = resp.json()
    ilk = govde["groups"][0]["items"][0]
    assert ilk["allocated_quantity"] == "55.500"
    assert ilk["unallocated_quantity"] == "44.500"
    _invariant(govde)


async def test_null_ve_sifir_hucreyi_siler_olmayan_hucrede_noop(
    client, db_session, user_factory, project_factory
):
    site, (s1, s2), (i1, i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-W2"
    )
    await _alloc(db_session, i1, s1, "30")
    await _alloc(db_session, i1, s2, "20")
    await _alloc(db_session, i2, s1, "10")

    resp = await client.put(
        _url(site.id),
        json={
            "allocations": [
                _cell(i1, s1, None),
                _cell(i1, s2, "0"),
                _cell(i2, s2, None),  # satir zaten yok -> no-op
            ]
        },
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    assert await _rows(db_session, i1.id) == {}
    assert set(await _rows(db_session, i2.id)) == {s1.id}


async def test_guncelleme_satir_kimligini_korur(client, db_session, user_factory, project_factory):
    site, (s1, _s2), (i1, _i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-W3"
    )
    onceki = await _alloc(db_session, i1, s1, "30")
    onceki_id = onceki.id

    resp = await client.put(
        _url(site.id), json={"allocations": [_cell(i1, s1, "45")]}, headers=headers
    )

    assert resp.status_code == 200, resp.text
    yeni = (await _rows(db_session, i1.id))[s1.id]
    assert yeni.id == onceki_id
    assert yeni.quantity == D("45")


async def test_gonderilen_bos_dizi_hicbir_sey_degistirmez(
    client, db_session, user_factory, project_factory
):
    site, (s1, _s2), (i1, _i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-W4"
    )
    await _alloc(db_session, i1, s1, "30")

    resp = await client.put(_url(site.id), json={"allocations": []}, headers=headers)

    assert resp.status_code == 200
    assert (await _rows(db_session, i1.id))[s1.id].quantity == D("30")
    assert (await client.put(_url(site.id), json={}, headers=headers)).status_code == 422


# --- YAZMA: asim -----------------------------------------------------------


async def test_asim_422_kalem_kodu_mesajda_dokunulmayan_paylar_dahil_ve_esitlik_gecer(
    client, db_session, user_factory, project_factory
):
    site, (s1, s2), (i1, _i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-X1"
    )
    # i1 kotasi 100; s1'de dokunulmayacak 60 var. s2'ye 40 yazmak ESITLIK, 40.001 ASIM.
    await _alloc(db_session, i1, s1, "60")

    asim = await client.put(
        _url(site.id), json={"allocations": [_cell(i1, s2, "40.001")]}, headers=headers
    )
    assert asim.status_code == 422, asim.text
    assert "01.001" in asim.json()["detail"]
    assert "aşıyor" in asim.json()["detail"]
    assert set(await _rows(db_session, i1.id)) == {s1.id}  # hicbir sey yazilmadi

    esit = await client.put(
        _url(site.id), json={"allocations": [_cell(i1, s2, "40")]}, headers=headers
    )
    assert esit.status_code == 200, esit.text
    assert (await _rows(db_session, i1.id))[s2.id].quantity == D("40")


async def test_ayni_govdede_biri_digerini_bosaltirsa_asim_sayilmaz(
    client, db_session, user_factory, project_factory
):
    """Govdedeki hucre mevcut payin YERINE gecer: 100'luk kota tam dolu iken
    (s1=100) tek istekte s1 -> 0 ve s2 -> 100 gecerli bir tasimadir."""
    site, (s1, s2), (i1, _i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-X2"
    )
    await _alloc(db_session, i1, s1, "100")

    resp = await client.put(
        _url(site.id),
        json={"allocations": [_cell(i1, s1, None), _cell(i1, s2, "100")]},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    assert set(await _rows(db_session, i1.id)) == {s2.id}


async def test_atomiklik_ikinci_hucrede_patlayan_istek_hicbir_sey_yazmaz(
    client, db_session, user_factory, project_factory
):
    site, (s1, s2), (i1, i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-X3"
    )
    await _alloc(db_session, i2, s1, "10")

    resp = await client.put(
        _url(site.id),
        json={
            "allocations": [
                _cell(i1, s1, "10"),  # gecerli
                _cell(i2, s2, "999"),  # asim (50)
            ]
        },
        headers=headers,
    )

    assert resp.status_code == 422
    assert await _rows(db_session, i1.id) == {}  # ilk (gecerli) hucre de yazilmadi
    assert set(await _rows(db_session, i2.id)) == {s1.id}


async def test_cift_hucre_422(client, db_session, user_factory, project_factory):
    site, (s1, _s2), (i1, _i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-X4"
    )

    resp = await client.put(
        _url(site.id),
        json={"allocations": [_cell(i1, s1, "10"), _cell(i1, s1, "20")]},
        headers=headers,
    )

    assert resp.status_code == 422
    assert "birden fazla" in resp.json()["detail"]
    assert await _rows(db_session, i1.id) == {}


async def test_negatif_miktar_422(client, db_session, user_factory, project_factory):
    site, (s1, _s2), (i1, _i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-X5"
    )
    resp = await client.put(
        _url(site.id), json={"allocations": [_cell(i1, s1, "-1")]}, headers=headers
    )
    assert resp.status_code == 422


async def test_baska_santiyenin_kalemi_ve_bolumu_olmayanla_ayni_422(
    client, db_session, user_factory, project_factory
):
    site, (s1, _s2), (i1, _i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-F1"
    )
    diger = await _site(db_session, await project_factory("BDG-F2"), code="B-BLOK")
    dsec = await _section(db_session, diger, "Yabanci Bolum")
    dgrup = await _group(db_session, diger)
    dkalem = await _item(db_session, diger, dgrup, "99.001")

    async def put(cell: dict):
        resp = await client.put(_url(site.id), json={"allocations": [cell]}, headers=headers)
        return resp.status_code, resp.json()

    yok_kalem = await put(
        {"boq_item_id": str(uuid.uuid4()), "section_id": str(s1.id), "quantity": "1"}
    )
    yabanci_kalem = await put(_cell(dkalem, s1, "1"))
    assert yok_kalem[0] == 422
    assert yabanci_kalem == yok_kalem
    assert yok_kalem[1]["detail"] == "İş kalemi bulunamadı"

    yok_bolum = await put(
        {"boq_item_id": str(i1.id), "section_id": str(uuid.uuid4()), "quantity": "1"}
    )
    yabanci_bolum = await put(_cell(i1, dsec, "1"))
    assert yok_bolum[0] == 422
    assert yabanci_bolum == yok_bolum
    assert yok_bolum[1]["detail"] == "Bölüm bulunamadı"
    assert await _rows(db_session, i1.id) == {}
    assert await _rows(db_session, dkalem.id) == {}


# --- Rol maskesi ------------------------------------------------------------


async def test_finance_rolde_miktarlar_null_sayaclar_ve_kodlar_dolu_PUT_403(
    client, db_session, user_factory, project_factory
):
    site, (s1, _s2), (i1, i2), _admin_h = await _kur(
        client, db_session, user_factory, project_factory, "BDG-M1"
    )
    await _alloc(db_session, i1, s1, "40")
    await _alloc(db_session, i2, s1, "50")
    fin = _auth(
        await _login_with_access(client, db_session, user_factory, "accounting", "acc@bdg.co")
    )

    resp = await client.get(_url(site.id), headers=fin)

    assert resp.status_code == 200, resp.text
    govde = resp.json()
    kalem = govde["groups"][0]["items"][0]
    assert kalem["quantity"] is None
    assert kalem["allocated_quantity"] is None
    assert kalem["unallocated_quantity"] is None
    assert all(a["quantity"] is None for a in kalem["allocations"])
    assert kalem["unit_price"] == "10.00"  # para kovasi acik (finance)
    ozet = govde["section_summaries"][0]
    assert ozet["items"][0]["quantity"] is None
    assert ozet["items"][0]["amount"] is None  # iki kova: miktar geri hesaplanamaz
    assert ozet["total_amount"] is None
    assert ozet["items"][0]["unit"] == "m³"
    # Sayaclar ve kodlar MASKELENMEZ
    assert govde["total_item_count"] == 2
    assert govde["unallocated_item_count"] == 1
    assert govde["distributed_item_count"] == 1
    assert govde["unallocated_item_codes"] == ["01.001"]
    _invariant(govde)

    put = await client.put(_url(site.id), json={"allocations": [_cell(i1, s1, "1")]}, headers=fin)
    assert put.status_code == 403
    assert (await _rows(db_session, i1.id))[s1.id].quantity == D("40")


async def test_limited_rolde_birim_fiyat_ve_amount_null_miktar_acik(
    client, db_session, user_factory, project_factory
):
    site, (s1, _s2), (i1, _i2), _admin_h = await _kur(
        client, db_session, user_factory, project_factory, "BDG-M2"
    )
    await _alloc(db_session, i1, s1, "40")
    sc = _auth(
        await _login_with_access(client, db_session, user_factory, "site_chief", "sc@bdg.co")
    )

    resp = await client.get(_url(site.id), headers=sc)

    assert resp.status_code == 200, resp.text
    govde = resp.json()
    kalem = govde["groups"][0]["items"][0]
    assert kalem["unit_price"] is None
    assert D(kalem["quantity"]) == D("100")
    ozet = govde["section_summaries"][0]
    assert ozet["items"][0]["unit_price"] is None
    assert ozet["items"][0]["amount"] is None
    assert D(ozet["items"][0]["quantity"]) == D("40")
    assert ozet["total_amount"] is None


# --- Denetim + tek kalem ucu uyumu -----------------------------------------


async def test_denetim_yalniz_degisen_kalem_basina_kayit_ve_mesaj(
    client, db_session, user_factory, project_factory
):
    site, (s1, s2), (i1, i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-A1"
    )
    await _alloc(db_session, i2, s1, "10")

    resp = await client.put(
        _url(site.id),
        json={
            "allocations": [
                _cell(i1, s1, "10"),
                _cell(i1, s2, "5"),  # i1: 2 hucre, TEK kayit, 2 bolum
                _cell(i2, s1, "10"),  # DEGISMEYEN hucre -> kayit YOK
                _cell(i2, s2, None),  # olmayan hucrede no-op -> kayit YOK
            ]
        },
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    detaylar = [d for d in await _audit_details(db_session, AuditAction.update) if "tahsis" in d]
    assert detaylar == ["İş kalemi bölüm tahsisleri güncellendi: 01.001 — 2 bölüm"]


async def test_denetim_silme_sonrasi_bolum_sayisini_yazar(
    client, db_session, user_factory, project_factory
):
    site, (s1, s2), (i1, _i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-A2"
    )
    await _alloc(db_session, i1, s1, "10")
    await _alloc(db_session, i1, s2, "10")

    await client.put(_url(site.id), json={"allocations": [_cell(i1, s1, None)]}, headers=headers)

    detaylar = [d for d in await _audit_details(db_session, AuditAction.update) if "tahsis" in d]
    assert detaylar == ["İş kalemi bölüm tahsisleri güncellendi: 01.001 — 1 bölüm"]


async def test_tek_kalem_ucu_matris_yazimindan_sonra_ayni_paylari_gosterir(
    client, db_session, user_factory, project_factory
):
    site, (s1, s2), (i1, _i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-C1"
    )

    await client.put(
        _url(site.id),
        json={"allocations": [_cell(i1, s1, "30"), _cell(i1, s2, "20")]},
        headers=headers,
    )
    tek = (await client.get(f"/boq/items/{i1.id}/allocations", headers=headers)).json()

    assert {a["section_id"]: a["quantity"] for a in tek["allocations"]} == {
        str(s1.id): "30.000",
        str(s2.id): "20.000",
    }
    assert tek["item"]["allocated_quantity"] == "50.000"


# --- Sorgu sayisi -----------------------------------------------------------


@contextmanager
def _sayac() -> Iterator[list[str]]:
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)


async def _buyuk(db_session, project, kalem_sayisi: int, bolum_sayisi: int, kod: str):
    site = await _site(db_session, project, code=kod)
    bolumler = [await _section(db_session, site, f"B{n}", n) for n in range(bolum_sayisi)]
    grup = await _group(db_session, site)
    for n in range(kalem_sayisi):
        kalem = await _item(db_session, site, grup, f"K{n:03d}", quantity=D("100"))
        for bolum in bolumler:
            await _alloc(db_session, kalem, bolum, "1")
    return site


async def test_sorgu_sayisi_kalem_ve_bolum_sayisindan_bagimsiz(
    client, db_session, user_factory, project_factory
):
    kucuk = await _buyuk(db_session, await project_factory("BDG-Q1"), 2, 2, "Q-KUCUK")
    buyuk = await _buyuk(db_session, await project_factory("BDG-Q2"), 6, 5, "Q-BUYUK")
    headers = await _admin(client, db_session, user_factory)
    await client.get(_url(kucuk.id), headers=headers)  # izin/oturum onbellegi isinsin

    sayilar = {}
    for ad, site in (("kucuk", kucuk), ("buyuk", buyuk)):
        kalemler = (
            (await db_session.execute(select(BoqItem).where(BoqItem.site_id == site.id)))
            .scalars()
            .all()
        )
        bolumler = (
            (await db_session.execute(select(Section).where(Section.site_id == site.id)))
            .scalars()
            .all()
        )
        hucreler = [_cell(k, b, "1") for k in kalemler for b in bolumler]
        with _sayac() as ifadeler:
            for kapi in ("GET", "PUT"):
                if kapi == "GET":
                    resp = await client.get(_url(site.id), headers=headers)
                else:
                    resp = await client.put(
                        _url(site.id), json={"allocations": hucreler}, headers=headers
                    )
                assert resp.status_code == 200, resp.text
        sayilar[ad] = len(ifadeler)

    assert sayilar["kucuk"] == sayilar["buyuk"], sayilar


# --- BDG-B1.4: giris siniri + kilit savunmasi ---------------------------------


async def test_quantity_alani_eksikse_422_ve_satir_degismez(
    client, db_session, user_factory, project_factory
):
    site, (s1, _s2), (i1, _i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-B14A"
    )
    await _alloc(db_session, i1, s1, "30")
    hucre = {"boq_item_id": str(i1.id), "section_id": str(s1.id)}  # quantity YOK

    resp = await client.put(_url(site.id), json={"allocations": [hucre]}, headers=headers)

    assert resp.status_code == 422
    assert (await _rows(db_session, i1.id))[s1.id].quantity == D("30")
    # acik null hala siler
    resp = await client.put(
        _url(site.id), json={"allocations": [_cell(i1, s1, None)]}, headers=headers
    )
    assert resp.status_code == 200
    assert await _rows(db_session, i1.id) == {}


@pytest.mark.parametrize("deger", ["0.0004", "1e30", "123456789012.123", "1.2345"])
async def test_hassasiyet_ve_hane_siniri_422_satir_degismez(
    client, db_session, user_factory, project_factory, deger
):
    site, (s1, _s2), (i1, _i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-B14B"
    )
    await _alloc(db_session, i1, s1, "30")

    resp = await client.put(
        _url(site.id), json={"allocations": [_cell(i1, s1, deger)]}, headers=headers
    )

    assert resp.status_code == 422, resp.text
    assert (await _rows(db_session, i1.id))[s1.id].quantity == D("30")


async def test_14_hane_kabul_edilir_ve_asim_kuralina_tabidir(
    client, db_session, user_factory, project_factory
):
    site, (s1, _s2), (i1, _i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-B14C"
    )

    resp = await client.put(
        _url(site.id), json={"allocations": [_cell(i1, s1, "12345678901.123")]}, headers=headers
    )

    assert resp.status_code == 422
    assert "asiyor" in resp.json()["detail"] or "aşıyor" in resp.json()["detail"]
    assert await _rows(db_session, i1.id) == {}


async def test_20001_hucre_422_db_ye_gitmeden(client, db_session, user_factory, project_factory):
    site, _bolumler, _kalemler, headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-B14D"
    )
    hucreler = [
        {"boq_item_id": str(uuid.uuid4()), "section_id": str(uuid.uuid4()), "quantity": "1"}
        for _ in range(20_001)
    ]

    resp = await client.put(_url(site.id), json={"allocations": hucreler}, headers=headers)

    assert resp.status_code == 422


async def test_kalem_dogrulama_ile_kilit_arasinda_silinirse_422_ve_yazma_yok(
    client, db_session, user_factory, project_factory, monkeypatch
):
    site, (s1, _s2), (i1, i2), headers = await _kur(
        client, db_session, user_factory, project_factory, "BDG-B14E"
    )
    gercek = repository.lock_items

    async def silinmis(session, item_ids):
        kilitli = await gercek(session, item_ids)
        kilitli.pop(i2.id)  # kalem dogrulamadan sonra silinmis gibi
        return kilitli

    monkeypatch.setattr(repository, "lock_items", silinmis)

    resp = await client.put(
        _url(site.id),
        json={"allocations": [_cell(i1, s1, "10"), _cell(i2, s1, "5")]},
        headers=headers,
    )

    assert resp.status_code == 422
    assert "İş kalemi bulunamadı" in resp.json()["detail"]
    assert await _rows(db_session, i1.id) == {}
    assert await _rows(db_session, i2.id) == {}


async def test_lock_items_parcali_artan_id_sirasiyla_kilitler(
    client, db_session, user_factory, project_factory, monkeypatch
):
    monkeypatch.setattr(repository, "ITEM_ID_CHUNK", 2)
    project = await project_factory("BDG-B14F")
    site = await _site(db_session, project)
    bolum = await _section(db_session, site, "B", 1)
    grup = await _group(db_session, site)
    kalemler = [
        await _item(db_session, site, grup, f"P{n}", quantity=D("10"), sort_order=n)
        for n in range(5)
    ]
    headers = await _admin(client, db_session, user_factory, "adm-b14f@bdg.co")

    # Govde BILEREK id'ye gore AZALAN sirada: uuid4 rastgele oldugundan olusturma
    # sirasi bazen zaten artandir; ters govde "siralamayi kaldir" mutasyonunu
    # olasilikla degil DETERMINISTIK kirmizi yapar.
    govde_kalemleri = sorted(kalemler, key=lambda k: str(k.id), reverse=True)
    beklenen = [str(i) for i in sorted(k.id for k in kalemler)]
    assert [str(k.id) for k in govde_kalemleri] == beklenen[::-1]

    kilitler: list[list[str]] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if "FOR UPDATE" in statement and "FROM boq_items" in statement:
            kilitler.append([str(p) for p in parameters])

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        resp = await client.put(
            _url(site.id),
            json={"allocations": [_cell(k, bolum, "1") for k in govde_kalemleri]},
            headers=headers,
        )
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)

    assert resp.status_code == 200, resp.text
    assert len(await _rows(db_session, kalemler[0].id)) == 1
    assert [len(k) for k in kilitler] == [2, 2, 1]
    # parcalar ardisik, tum kimlikler GLOBAL artan sirada
    assert [i for k in kilitler for i in k] == beklenen

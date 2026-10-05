"""IZN-B4a ONARIM — opus çürütücünün ölçtüğü bulgular, GERÇEK HTTP.

* K0  PATCH ile VERİ KAYBI: gizli rol iç içe nesneyi (sözleşme / yatırım / arsa payı) kısmi
      gönderince gönderilmeyen (gizli) değer KORUNUR.
* K1  proje başına maske: ana rol açık / B'deki rol gizli (ve tersi) matrisi; satır KENDİ
      projesindeki rolle maskelenir; proje çözülemeyen (şirket geneli) uçta BİRLEŞİM (fail-closed).
* K4  yazma kapısı bypass: `PATCH /units/{B}`, `PATCH /sales/{B}` B'deki gizli rolle 403.
* K6  POST (oluşturma) SERBEST, yanıt maskeli; PATCH'te gizli alan 403; xlsx içe aktarım yaratır.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sayfalar import HiddenCategory
from app.modules.customers.models import Customer, CustomerType
from app.modules.projects.models import (
    Project,
    ProjectContract,
    ProjectInvestment,
    ProjectLandShare,
)
from app.modules.sites.models import Site
from app.modules.units.models import Block, Unit, UnitKind
from tests._hassas_alan import rol_gizli
from tests._proje_ekibi import ekibe_ekle
from tests.modules._boq import _auth

PASSWORD = "parola1234"
H = HiddenCategory
_GIZLI = {H.sozlesme_fiyat, H.satis_alici}


async def _giris(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])


async def _proje(session: AsyncSession, project_factory, kod: str, tutar: str) -> Project:
    project = await project_factory(kod, name=f"Proje {kod}", contract_amount=tutar)
    session.add(
        ProjectContract(
            project_id=project.id,
            contract_no=f"SZL-{kod}",
            amount=Decimal(tutar),
            advance_pct=Decimal("10"),
            retainage_pct=Decimal("5"),
            vat_pct=Decimal("20"),
            has_price_escalation=False,
        )
    )
    await session.flush()
    return project


async def _unite(session: AsyncSession, project: Project, fiyat: str) -> Unit:
    site = Site(project_id=project.id, code=f"{project.code}-S", name="Merkez")
    session.add(site)
    await session.flush()
    blok = Block(project_id=project.id, site_id=site.id, name="A Blok")
    session.add(blok)
    await session.flush()
    unit = Unit(
        project_id=project.id,
        block_id=blok.id,
        unit_no="1",
        unit_kind=UnitKind.apartment,
        list_price=Decimal(fiyat),
    )
    session.add(unit)
    await session.flush()
    return unit


class Dunya:
    """İki proje (A, B) + ünite + B'de satış; kişiler ana rol / A rolü / B rolü ile kurulur."""

    a: Project
    b: Project
    unite_a: Unit
    unite_b: Unit
    satis_b: dict
    musteri: Customer


@pytest.fixture
async def dunya(client: AsyncClient, seeded_db: AsyncSession, project_factory, user_factory):
    d = Dunya()
    d.a = await _proje(seeded_db, project_factory, "MA-A", "1000000.00")
    d.b = await _proje(seeded_db, project_factory, "MA-B", "2000000.00")
    d.unite_a = await _unite(seeded_db, d.a, "1480000.00")
    d.unite_b = await _unite(seeded_db, d.b, "1590000.00")
    d.musteri = Customer(
        customer_type=CustomerType.person, name="Mehmet Aydın", national_id="12345678901"
    )
    seeded_db.add(d.musteri)
    await seeded_db.flush()
    await user_factory(email="yonetici@ma.co", password=PASSWORD, role_key="system_admin")
    yonetici = await _giris(client, "yonetici@ma.co")
    resp = await client.post(
        f"/projects/{d.b.id}/sales",
        json={
            "unit_id": str(d.unite_b.id),
            "customer_id": str(d.musteri.id),
            "sale_type": "sale",
            "sale_price": "1500000.00",
        },
        headers=yonetici,
    )
    assert resp.status_code == 201, resp.text
    d.satis_b = resp.json()
    return d


async def _kisi(
    session: AsyncSession,
    user_factory,
    client: AsyncClient,
    d: Dunya,
    ad: str,
    *,
    ana: set[H],
    a: set[H],
    b: set[H],
) -> dict[str, str]:
    """Ana rol / A'daki rol / B'deki rol AYRI gizli kategorilerle kurulur."""
    r_ana = await rol_gizli(session, f"{ad}_ana", ana)
    r_a = await rol_gizli(session, f"{ad}_a", a)
    r_b = await rol_gizli(session, f"{ad}_b", b)
    user = await user_factory(email=f"{ad}@ma.co", password=PASSWORD, role_key=r_ana.key)
    await ekibe_ekle(session, user, d.a.id, r_a.id)
    await ekibe_ekle(session, user, d.b.id, r_b.id)
    return await _giris(client, f"{ad}@ma.co")


def _kod_tutar(govde: dict, kod: str) -> object:
    satir = next(p for p in govde["items"] if p["code"] == kod)
    return satir["contract_amount"]


# --- K1: proje başına maske ----------------------------------------------------------------------


async def test_K1_ana_rol_acik_B_gizli_B_satiri_gizlenir_A_acik_kalir(
    client, seeded_db, user_factory, dunya
) -> None:
    baslik = await _kisi(
        seeded_db, user_factory, client, dunya, "k1a", ana=set(), a=set(), b=_GIZLI
    )

    liste = (await client.get("/projects", headers=baslik)).json()
    zaman = (await client.get("/projects/timeline", headers=baslik)).json()
    sozlesme_yaniti = await client.get("/contracts?type=employer", headers=baslik)
    assert sozlesme_yaniti.status_code == 200, sozlesme_yaniti.text
    sozlesmeler = sozlesme_yaniti.json()

    assert _kod_tutar(liste, "MA-A") == "1000000.00"  # POZİTİF KONTROL
    assert _kod_tutar(liste, "MA-B") is None
    assert _kod_tutar(zaman, "MA-A") == "1000000.00"
    assert _kod_tutar(zaman, "MA-B") is None
    tutarlar = {s["project_id"]: s["amount"] for s in sozlesmeler["items"]}
    assert tutarlar[str(dunya.a.id)] == "1000000.00"
    assert tutarlar[str(dunya.b.id)] is None
    detay_b = (await client.get(f"/projects/{dunya.b.id}", headers=baslik)).json()
    assert detay_b["contract_amount"] is None


async def test_K1_ana_rol_gizli_B_acik_B_satiri_ACIK_asiri_maskeleme_yok(
    client, seeded_db, user_factory, dunya
) -> None:
    baslik = await _kisi(
        seeded_db, user_factory, client, dunya, "k1b", ana=_GIZLI, a=_GIZLI, b=set()
    )

    liste = (await client.get("/projects", headers=baslik)).json()
    zaman = (await client.get("/projects/timeline", headers=baslik)).json()

    assert _kod_tutar(liste, "MA-A") is None
    assert _kod_tutar(liste, "MA-B") == "2000000.00"  # ters yön: B'deki rol açık
    assert _kod_tutar(zaman, "MA-A") is None
    assert _kod_tutar(zaman, "MA-B") == "2000000.00"


async def test_K1_satis_ve_taksit_uclari_projedeki_rolle_maskelenir(
    client, seeded_db, user_factory, dunya
) -> None:
    gizli_b = await _kisi(
        seeded_db, user_factory, client, dunya, "k1c", ana=set(), a=set(), b={H.satis_alici}
    )
    acik = await _kisi(seeded_db, user_factory, client, dunya, "k1d", ana=set(), a=set(), b=set())
    satis_id = dunya.satis_b["id"]

    acik_satis = (await client.get(f"/sales/{satis_id}", headers=acik)).json()
    gizli_satis = (await client.get(f"/sales/{satis_id}", headers=gizli_b)).json()
    plan = (await client.get(f"/sales/{satis_id}/installments", headers=gizli_b)).json()
    liste = (await client.get(f"/projects/{dunya.b.id}/sales", headers=gizli_b)).json()

    assert acik_satis["sale_price"] == "1500000.00"  # POZİTİF KONTROL: ana rol de açık
    assert gizli_satis["sale_price"] is None  # `/sales/{id}`: proje B'deki rol çözülür
    assert gizli_satis["customer_name"] is None
    assert plan["sale_price"] is None
    assert liste["items"][0]["sale_price"] is None


async def test_K1_sirket_geneli_uc_ana_rol_ve_TUM_ekip_rollerinin_BIRLESIMI(
    client, seeded_db, user_factory, dunya
) -> None:
    """IZN-PLAN §3: proje çözülemeyen uçta fail-closed. Ana rol ve A açık, B alıcıyı gizliyor →
    şirket geneli `/customers`ta alıcı kimliği GİZLİ."""
    baslik = await _kisi(
        seeded_db, user_factory, client, dunya, "k1e", ana=set(), a=set(), b={H.satis_alici}
    )

    musteri = (await client.get(f"/customers/{dunya.musteri.id}", headers=baslik)).json()

    assert musteri["national_id"] is None
    assert musteri["name"] is None  # K3: alıcı adı `satis_alici`


# --- K4: yazma kapısı bypass ---------------------------------------------------------------------


async def test_K4_PATCH_unit_ve_sale_B_deki_gizli_rolle_403_A_da_serbest(
    client, seeded_db, user_factory, dunya
) -> None:
    baslik = await _kisi(
        seeded_db, user_factory, client, dunya, "k4a", ana=set(), a=set(), b={H.satis_alici}
    )

    b_unit = await client.patch(
        f"/units/{dunya.unite_b.id}", json={"list_price": "1.00"}, headers=baslik
    )
    b_sale = await client.patch(
        f"/sales/{dunya.satis_b['id']}", json={"sale_price": "1.00"}, headers=baslik
    )
    a_unit = await client.patch(
        f"/units/{dunya.unite_a.id}", json={"list_price": "1490000.00"}, headers=baslik
    )

    assert b_unit.status_code == 403, b_unit.text
    assert b_sale.status_code == 403, b_sale.text
    assert a_unit.status_code == 200, a_unit.text  # POZİTİF KONTROL: A'da rol açık
    assert a_unit.json()["list_price"] == "1490000.00"
    await seeded_db.refresh(dunya.unite_b)
    assert dunya.unite_b.list_price == Decimal("1590000.00")  # B değişmedi


# --- K6: POST serbest, PATCH 403 -----------------------------------------------------------------


async def test_K6_satis_POST_gizli_rolle_201_yanit_maskeli_PATCH_dolu_alan_403(
    client, seeded_db, user_factory, dunya
) -> None:
    baslik = await _kisi(
        seeded_db, user_factory, client, dunya, "k6a", ana=set(), a={H.satis_alici}, b=set()
    )

    olustur = await client.post(
        f"/projects/{dunya.a.id}/sales",
        json={
            "unit_id": str(dunya.unite_a.id),
            "customer_id": str(dunya.musteri.id),
            "sale_type": "sale",
            "sale_price": "1400000.00",
        },
        headers=baslik,
    )
    assert olustur.status_code == 201, olustur.text
    assert olustur.json()["sale_price"] is None  # yanıt MASKELİ
    assert olustur.json()["customer_name"] is None

    guncelle = await client.patch(
        f"/sales/{olustur.json()['id']}", json={"sale_price": "1.00"}, headers=baslik
    )
    assert guncelle.status_code == 403, guncelle.text


async def test_K6_xlsx_ice_aktarim_gizli_rolle_YARATIR_403_degil(
    client, seeded_db, user_factory, dunya
) -> None:
    """GECE KARARI: içe aktarım toplu OLUŞTURMA (yalnız yeni ünite yazar, var olanı ezmez) →
    CEO'nun POST kuralıyla tutarlı SERBEST; yanıt maskeli."""
    from tests.modules.units._units_import import _XLSX_MIME, _row, _xlsx

    baslik = await _kisi(
        seeded_db, user_factory, client, dunya, "k6b", ana=set(), a={H.satis_alici}, b=set()
    )
    dosya = _xlsx([_row(unit_no="77", **{"Liste Fiyatı": 1250000, "Rayiç Değer": 1100000})])

    yanit = await client.post(
        f"/projects/{dunya.a.id}/units/import",
        files={"file": ("birimler.xlsx", dosya, _XLSX_MIME)},
        headers=baslik,
    )

    assert yanit.status_code in (200, 201), yanit.text
    yeni = (
        await seeded_db.execute(
            select(Unit).where(Unit.project_id == dunya.a.id, Unit.unit_no == "77")
        )
    ).scalar_one()
    assert yeni.list_price == Decimal("1250000.00")  # yazıldı (oluşturma serbest)


# --- K0: PATCH ile veri kaybı --------------------------------------------------------------------


async def test_K0_gizli_rol_kismi_sozlesme_PATCH_bedeli_ve_oranlari_KORUR(
    client, seeded_db, user_factory, dunya
) -> None:
    baslik = await _kisi(
        seeded_db, user_factory, client, dunya, "k0a", ana=set(), a={H.sozlesme_fiyat}, b=set()
    )

    yanit = await client.patch(
        f"/projects/{dunya.a.id}",
        json={"contract": {"contract_no": "SZL-YENI", "has_price_escalation": False}},
        headers=baslik,
    )

    assert yanit.status_code == 200, yanit.text
    a_id = dunya.a.id
    seeded_db.expire_all()
    sozlesme = await seeded_db.get(ProjectContract, a_id)
    proje = await seeded_db.get(Project, a_id)
    assert sozlesme.contract_no == "SZL-YENI"  # gönderilen alan DEĞİŞTİ
    assert sozlesme.amount == Decimal("1000000.00")  # gönderilmeyen (gizli) bedel KORUNDU
    assert sozlesme.advance_pct == Decimal("10.00")  # gönderilmeyen oranlar varsayılana DÖNMEDİ
    assert proje.contract_amount == Decimal("1000000.00")
    assert proje.contract_no == "SZL-YENI"


async def test_K0_gonderilen_sozlesme_alani_degisir_fiyat_farki_kapaninca_endeks_temizlenir(
    client, seeded_db, user_factory, dunya
) -> None:
    sozlesme = await seeded_db.get(ProjectContract, dunya.a.id)
    sozlesme.has_price_escalation = True
    sozlesme.index_type = None
    await seeded_db.flush()
    acik = await _kisi(seeded_db, user_factory, client, dunya, "k0b", ana=set(), a=set(), b=set())

    yanit = await client.patch(
        f"/projects/{dunya.a.id}",
        json={"contract": {"amount": "3000000.00", "has_price_escalation": False}},
        headers=acik,
    )

    assert yanit.status_code == 200, yanit.text
    a_id = dunya.a.id
    seeded_db.expire_all()
    sozlesme = await seeded_db.get(ProjectContract, a_id)
    assert sozlesme.amount == Decimal("3000000.00")
    assert sozlesme.has_price_escalation is False
    assert sozlesme.index_type is None


async def test_K0_gizli_rol_yatirim_ve_arsa_payi_PATCH_gizli_alanlari_KORUR(
    client, seeded_db, user_factory, project_factory
) -> None:
    yatirim = await project_factory("MA-YT", project_type="kendi_yatirim")
    arsa = await project_factory("MA-AP", project_type="kat_karsiligi")
    seeded_db.add(
        ProjectInvestment(project_id=yatirim.id, sales_target=None, land_cost=Decimal("9"))
    )
    seeded_db.add(
        ProjectLandShare(
            project_id=arsa.id,
            landowner_name="Arsa Sahibi",
            our_share_pct=Decimal("60"),
            owner_share_pct=Decimal("40"),
            daily_penalty=Decimal("500.00"),
            guarantee_amount=Decimal("100000.00"),
        )
    )
    await seeded_db.flush()
    rol = await rol_gizli(seeded_db, "k0c_rol", {H.maliyet_kar, H.sozlesme_fiyat})
    user = await user_factory(email="k0c@ma.co", password=PASSWORD, role_key=rol.key)
    for proje in (yatirim, arsa):
        await ekibe_ekle(seeded_db, user, proje.id, rol.id)
    baslik = await _giris(client, "k0c@ma.co")

    r1 = await client.patch(
        f"/projects/{yatirim.id}", json={"investment": {"sales_target": "7000.00"}}, headers=baslik
    )
    r2 = await client.patch(
        f"/projects/{arsa.id}",
        json={
            "land_share": {
                "landowner_name": "Yeni Ad",
                "our_share_pct": "55",
                "owner_share_pct": "45",
            }
        },
        headers=baslik,
    )

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    yatirim_id, arsa_id = yatirim.id, arsa.id
    seeded_db.expire_all()
    inv = (
        await seeded_db.execute(
            select(ProjectInvestment).where(ProjectInvestment.project_id == yatirim_id)
        )
    ).scalar_one()
    ls = (
        await seeded_db.execute(
            select(ProjectLandShare).where(ProjectLandShare.project_id == arsa_id)
        )
    ).scalar_one()
    assert inv.sales_target == Decimal("7000.00")  # gönderilen
    assert inv.land_cost == Decimal("9.00")  # gönderilmeyen gizli alan KORUNDU
    assert ls.landowner_name == "Yeni Ad"
    assert ls.daily_penalty == Decimal("500.00")
    assert ls.guarantee_amount == Decimal("100000.00")


def test_cozucu_tablosu_birim_satis_sozlesme_uclarini_icerir() -> None:
    from app.modules.projects.context import RESOLVERS

    for anahtar in (
        ("/units", "unit_id"),
        ("/blocks", "block_id"),
        ("/sales", "sale_id"),
        ("/sales", "installment_id"),
        ("/contracts", "item_id"),
        ("/contracts", "group_id"),
        ("/subcontractor-contracts", "item_id"),
    ):
        assert anahtar in RESOLVERS, anahtar


def test_uuid_modulu_kullanilir() -> None:
    assert uuid.UUID(int=0)

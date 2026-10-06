"""TKL-B3.2 — `/catalog/items` son fiyat: SZL + HK saglayicilari, birlestirme, maske, N+1, kayit.

Zamanlar ACIKCA yazilir (test islemi icinde `now()` sabittir); beklenen fiyatlar ELLE
hesaplanmis sabitlerdir (uretim fonksiyonundan turetilmez).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.core import last_price
from app.core.access import AccessLevel
from app.core.sayfalar import HiddenCategory
from app.modules.catalog.models import ContractorType, EvCatalogItem, EvDiscipline
from app.modules.catalog.service import next_poz_no
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.progress_payments.models import (
    ProgressPayment,
    ProgressPaymentLine,
    ProgressPaymentStatus,
)
from app.modules.projects.models import ProjectContract
from app.modules.sites.models import Site
from tests._hassas_alan import rol_gizle
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz

from .._boq import _auth, _login_with_access

pytestmark = pytest.mark.asyncio

URL = "/catalog/items"
#: Port kaydi beklenen kaynak kumesi — TEK YERDE (SA B7'de "SA" eklenir).
BEKLENEN_KAYNAKLAR = {"SZL", "HK", "TKL"}
T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
async def admin(client, db_session, user_factory, seeded_db):
    token = await _login_with_access(
        client, db_session, user_factory, "system_admin", f"a.{uuid.uuid4().hex[:6]}@tkl.co"
    )
    return _auth(token)


@pytest.fixture
async def olusturan(user_factory):
    return await user_factory(
        email=f"o.{uuid.uuid4().hex[:6]}@tkl.co", password="parola1234", role_key="system_admin"
    )


@pytest.fixture
async def katalog(seeded_db):
    """Uc kalem: (K1, K2, K3) — poz sirasi ayni."""
    disiplin = EvDiscipline(
        code="LP1", name="Kaba", color="#2563eb", default_contractor_type=ContractorType.OWN
    )
    seeded_db.add(disiplin)
    await seeded_db.flush()
    kalemler = []
    for ad in ("Beton", "Kalıp", "Demir"):
        kalem = EvCatalogItem(
            poz_no=await next_poz_no(seeded_db, disiplin),
            discipline_id=disiplin.id,
            name=ad,
            uom="m3",
            standard_unit_mhr=Decimal("1.5"),
            default_contractor_type=ContractorType.OWN,
        )
        seeded_db.add(kalem)
        await seeded_db.flush()
        kalemler.append(kalem)
    return [k.id for k in kalemler]


async def _proje(db, project_factory, code: str):
    project = await project_factory(code=code, name=f"Proje {code}")
    db.add(
        ProjectContract(
            project_id=project.id,
            contract_no=f"S-{code}",
            amount=Decimal("1000000"),
            advance_pct=Decimal("10"),
            retainage_pct=Decimal("5"),
            vat_pct=Decimal("20"),
        )
    )
    await db.flush()
    group = EmployerContractGroup(project_id=project.id, name="Grup", sort_order=0)
    site = Site(project_id=project.id, code=f"S-{code}", name="Şantiye")
    db.add_all([group, site])
    await db.flush()
    return project, group, site


async def _kalem(db, project, group, catalog_id, fiyat: str, at: datetime, code="01"):
    item = EmployerContractItem(
        project_id=project.id,
        group_id=group.id,
        code=code,
        description="Kalem",
        unit="m3",
        quantity=Decimal("10"),
        unit_price=Decimal(fiyat),
        catalog_item_id=catalog_id,
        price_changed_at=at,
    )
    db.add(item)
    await db.flush()
    return item


async def _hakedis(
    db,
    project,
    site,
    olusturan,
    item,
    *,
    sira,
    durum,
    fiyat,
    katsayi="1.000",
    approved_at=None,
    payment_id=None,
):
    payment = ProgressPayment(
        **({"id": payment_id} if payment_id is not None else {}),
        project_id=project.id,
        sequence_no=sira,
        status=durum,
        vat_pct=Decimal("20"),
        advance_pct=Decimal("10"),
        retainage_pct=Decimal("5"),
        approved_at=approved_at,
        created_by=olusturan.id,
    )
    db.add(payment)
    await db.flush()
    db.add(
        ProgressPaymentLine(
            payment_id=payment.id,
            contract_item_id=item.id,
            site_id=site.id,
            code=item.code,
            description="Kalem",
            unit="m3",
            contract_unit_price=Decimal(fiyat),
            coefficient=Decimal(katsayi),
            quantity=Decimal("1"),
        )
    )
    await db.flush()
    return payment


async def _satir_ekle(db, payment, item, site, *, fiyat, katsayi) -> None:
    """Ayni hakedise ayni kalem icin BASKA santiyeden ikinci satir."""
    db.add(
        ProgressPaymentLine(
            payment_id=payment.id,
            contract_item_id=item.id,
            site_id=site.id,
            code=item.code,
            description="Kalem",
            unit="m3",
            contract_unit_price=Decimal(fiyat),
            coefficient=Decimal(katsayi),
            quantity=Decimal("1"),
        )
    )
    await db.flush()


async def _son(client, headers, catalog_id) -> dict | None:
    items = (await client.get(URL, headers=headers)).json()["items"]
    return next(i for i in items if i["id"] == str(catalog_id))["last_price"]


# --------------------------------------------------------------------- SZL


async def test_szl_iki_projede_en_yeni_kazanir(
    client, admin, seeded_db, project_factory, katalog
) -> None:
    p1, g1, _ = await _proje(seeded_db, project_factory, "PRJ-A")
    p2, g2, _ = await _proje(seeded_db, project_factory, "PRJ-B")
    await _kalem(seeded_db, p1, g1, katalog[0], "100.00", T0)
    await _kalem(seeded_db, p2, g2, katalog[0], "130.00", T0 + timedelta(days=5))
    son = await _son(client, admin, katalog[0])
    assert Decimal(son["price"]) == Decimal("130.00")
    assert son["source"] == "SZL" and son["doc_no"] == "PRJ-B" and son["doc_id"] == str(p2.id)
    assert datetime.fromisoformat(son["at"]) == T0 + timedelta(days=5)


async def test_szl_fiyat_guncellenince_at_ilerler_ve_son_fiyat_degisir(
    client, admin, seeded_db, project_factory, katalog
) -> None:
    p1, g1, _ = await _proje(seeded_db, project_factory, "PRJ-A")
    item = await _kalem(seeded_db, p1, g1, katalog[0], "100.00", T0)
    onceki = await _son(client, admin, katalog[0])
    r = await client.patch(
        f"/contracts/employer/items/{item.id}", json={"unit_price": "111.25"}, headers=admin
    )
    assert r.status_code == 200, r.text
    sonra = await _son(client, admin, katalog[0])
    assert Decimal(sonra["price"]) == Decimal("111.25")
    assert datetime.fromisoformat(sonra["at"]) > datetime.fromisoformat(onceki["at"])


async def test_szl_bagsiz_kalem_kaynak_olmaz(
    client, admin, seeded_db, project_factory, katalog
) -> None:
    p1, g1, _ = await _proje(seeded_db, project_factory, "PRJ-A")
    await _kalem(seeded_db, p1, g1, None, "100.00", T0)
    for kalem in katalog:
        assert await _son(client, admin, kalem) is None


# --------------------------------------------------------------------- HK


async def test_hk_taslak_ve_onay_bekleyen_kaynak_olmaz(
    client, admin, seeded_db, project_factory, katalog, olusturan
) -> None:
    p, g, s = await _proje(seeded_db, project_factory, "PRJ-A")
    item = await _kalem(seeded_db, p, g, katalog[0], "100.00", T0)
    for sira, durum in (
        (1, ProgressPaymentStatus.draft),
        (2, ProgressPaymentStatus.pending_approval),
    ):
        await _hakedis(
            seeded_db,
            p,
            s,
            olusturan,
            item,
            sira=sira,
            durum=durum,
            fiyat="900.00",
            approved_at=T0 + timedelta(days=30),  # tutarsiz damga bile taslagi kaynak yapmaz
        )
    son = await _son(client, admin, katalog[0])
    assert son["source"] == "SZL" and Decimal(son["price"]) == Decimal("100.00")


async def test_hk_onayli_ve_odenmis_kaynak_olur_katsayili_fiyat_etiket(
    client, admin, seeded_db, project_factory, katalog, olusturan
) -> None:
    p, g, s = await _proje(seeded_db, project_factory, "PRJ-A")
    item = await _kalem(seeded_db, p, g, katalog[0], "1234.56", T0)  # taban = hakedis tabani
    # 1234.56 x 1.037 = 1280.23872 → 2 hane yuvarlama 1280.24 (ELLE hesap; katsayi kolonu 3 hane)
    odenmis = await _hakedis(
        seeded_db,
        p,
        s,
        olusturan,
        item,
        sira=3,
        durum=ProgressPaymentStatus.paid,
        fiyat="1234.56",
        katsayi="1.037",
        approved_at=T0 + timedelta(days=10),
    )
    son = await _son(client, admin, katalog[0])
    assert Decimal(son["price"]) == Decimal("1280.24")
    assert son["source"] == "HK" and son["doc_no"] == "HK-PRJ-A-3"
    assert son["doc_id"] == str(odenmis.id)
    # sozlesme fiyati 1000.00 oldu (taban eslesmeli); onayli daha yeni → o kazanir;
    # katsayi 1.2: 1000.00 x 1.2 = 1200.00
    item.unit_price = Decimal("1000.00")
    await seeded_db.flush()
    await _hakedis(
        seeded_db,
        p,
        s,
        olusturan,
        item,
        sira=4,
        durum=ProgressPaymentStatus.approved,
        fiyat="1000.00",
        katsayi="1.200",
        approved_at=T0 + timedelta(days=20),
    )
    son = await _son(client, admin, katalog[0])
    assert Decimal(son["price"]) == Decimal("1200.00") and son["doc_no"] == "HK-PRJ-A-4"


async def test_hk_approved_at_null_onayli_dislanir(
    client, admin, seeded_db, project_factory, katalog, olusturan
) -> None:
    p, g, s = await _proje(seeded_db, project_factory, "PRJ-A")
    item = await _kalem(seeded_db, p, g, katalog[0], "100.00", T0)
    await _hakedis(
        seeded_db,
        p,
        s,
        olusturan,
        item,
        sira=1,
        durum=ProgressPaymentStatus.approved,
        fiyat="900.00",
        approved_at=None,
    )
    son = await _son(client, admin, katalog[0])
    assert son["source"] == "SZL"


async def test_hk_sorgusu_tek_ifade_ve_tasero_tablosuna_dokunmaz(
    seeded_db, project_factory, katalog
) -> None:
    from app.modules.progress_payments import last_price_provider

    with _sayac() as ifadeler:
        await last_price_provider.provide(seeded_db, katalog)
    assert len(ifadeler) == 1
    assert "subcontractor" not in ifadeler[0].lower()


# ----------------------------------------------------------- SZL vs HK


@pytest.mark.parametrize(("hk_gun", "beklenen"), [(10, "HK"), (-10, "SZL")])
async def test_szl_ile_hk_hangisi_yeniyse_o(
    client, admin, seeded_db, project_factory, katalog, olusturan, hk_gun, beklenen
) -> None:
    p, g, s = await _proje(seeded_db, project_factory, "PRJ-A")
    item = await _kalem(seeded_db, p, g, katalog[0], "100.00", T0)
    await _hakedis(
        seeded_db,
        p,
        s,
        olusturan,
        item,
        sira=1,
        durum=ProgressPaymentStatus.approved,
        fiyat="100.00",
        katsayi="1.400",  # 100 x 1.4 = 140.00 (taban = sozlesme fiyati)
        approved_at=T0 + timedelta(days=hk_gun),
    )
    assert (await _son(client, admin, katalog[0]))["source"] == beklenen


# --------------------------------------------------------------------- API


async def test_liste_last_price_dolu_ve_bos(
    client, admin, seeded_db, project_factory, katalog
) -> None:
    p, g, _ = await _proje(seeded_db, project_factory, "PRJ-A")
    await _kalem(seeded_db, p, g, katalog[1], "75.00", T0)
    items = (await client.get(URL, headers=admin)).json()["items"]
    assert [i["last_price"] is not None for i in items] == [False, True, False]


async def test_post_ve_patch_yaniti_last_price_tasir(
    client, admin, seeded_db, project_factory, katalog
) -> None:
    p, g, _ = await _proje(seeded_db, project_factory, "PRJ-A")
    await _kalem(seeded_db, p, g, katalog[0], "75.00", T0)
    r = await client.patch(f"{URL}/{katalog[0]}", json={"name": "Beton C30"}, headers=admin)
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["last_price"]["price"]) == Decimal("75.00")
    yeni = await client.post(
        URL,
        json={
            "discipline_id": (await client.get(URL, headers=admin)).json()["items"][0][
                "discipline"
            ]["id"],
            "name": "Yeni",
            "uom": "m2",
            "standard_unit_mhr": "1.0000",
            "default_contractor_type": "own",
        },
        headers=admin,
    )
    assert yeni.status_code == 201 and yeni.json()["last_price"] is None


async def test_limited_kapsamda_last_price_none_fiyatli_kalemde(
    client, admin, seeded_db, project_factory, katalog, db_session, user_factory
) -> None:
    p, g, _ = await _proje(seeded_db, project_factory, "PRJ-A")
    await _kalem(seeded_db, p, g, katalog[0], "75.00", T0)
    await modul_duzeyi_yaz(db_session, "accounting", "contracts", AccessLevel.view)
    await rol_gizle(db_session, "accounting", HiddenCategory.sozlesme_fiyat)
    token = await _login_with_access(
        client, db_session, user_factory, "accounting", f"l.{uuid.uuid4().hex[:6]}@tkl.co"
    )
    sinirli = _auth(token)
    kalem = next(
        i
        for i in (await client.get(URL, headers=sinirli)).json()["items"]
        if i["id"] == str(katalog[0])
    )
    assert kalem["last_price"] is None
    assert "75.00" not in str(kalem)
    # pozitif kontrol: kisitsiz kapsamda ayni kalem fiyatli
    assert (await _son(client, admin, katalog[0])) is not None


# ------------------------------------------- T29: ayni belgede birden cok fiyat → EN YUKSEK


@pytest.mark.parametrize("ters", [False, True])
async def test_hk_ayni_hakedis_iki_santiye_satiri_en_yuksek_duzeltilmis_bf(
    client, admin, seeded_db, project_factory, katalog, olusturan, ters
) -> None:
    p, g, s = await _proje(seeded_db, project_factory, "PRJ-A")
    s2 = Site(project_id=p.id, code="S2-PRJ-A", name="Şantiye 2")
    seeded_db.add(s2)
    await seeded_db.flush()
    item = await _kalem(seeded_db, p, g, katalog[0], "100.00", T0)
    dusuk, yuksek = ("1.000", "1.500")  # 100x1.000 = 100.00 ; 100x1.500 = 150.00 (ELLE)
    ilk, ikinci = (yuksek, dusuk) if ters else (dusuk, yuksek)
    hk = await _hakedis(
        seeded_db,
        p,
        s,
        olusturan,
        item,
        sira=1,
        durum=ProgressPaymentStatus.approved,
        fiyat="100.00",
        katsayi=ilk,
        approved_at=T0 + timedelta(days=10),
    )
    await _satir_ekle(seeded_db, hk, item, s2, fiyat="100.00", katsayi=ikinci)
    son = await _son(client, admin, katalog[0])
    assert son["source"] == "HK" and Decimal(son["price"]) == Decimal("150.00")
    assert son["doc_id"] == str(hk.id)


async def test_szl_ayni_projede_ayni_katalog_iki_kalem_en_yuksek_fiyat(
    client, admin, seeded_db, project_factory, katalog
) -> None:
    p, g, _ = await _proje(seeded_db, project_factory, "PRJ-A")
    await _kalem(seeded_db, p, g, katalog[0], "120.00", T0, code="01")
    await _kalem(seeded_db, p, g, katalog[0], "90.00", T0 + timedelta(days=1), code="02")
    son = await _son(client, admin, katalog[0])
    assert Decimal(son["price"]) == Decimal("120.00")  # en yeni (90) DEGIL, en yuksek
    assert datetime.fromisoformat(son["at"]) == T0 + timedelta(days=1)  # MAX(price_changed_at)
    assert son["source"] == "SZL" and son["doc_no"] == "PRJ-A" and son["doc_id"] == str(p.id)


# ------------------------------------------- T29/S6: bayat taban HK kaynak sayilmaz


async def test_hk_bayat_taban_kaynak_degil_szl_kalir_taban_esitse_hk(
    client, admin, seeded_db, project_factory, katalog, olusturan
) -> None:
    """Taslak 100'le acildi, sozlesme T5'te 150 oldu, hakedis T10'da onaylandi → HK satiri
    kaynak degil; SZL 150 kalir. Taban guncel fiyata esitse (kontrol) HK kaynak olur."""
    p, g, s = await _proje(seeded_db, project_factory, "PRJ-A")
    item = await _kalem(seeded_db, p, g, katalog[0], "150.00", T0 + timedelta(days=5))
    hk = await _hakedis(
        seeded_db,
        p,
        s,
        olusturan,
        item,
        sira=1,
        durum=ProgressPaymentStatus.approved,
        fiyat="100.00",  # bayat taban
        approved_at=T0 + timedelta(days=10),
    )
    son = await _son(client, admin, katalog[0])
    assert son["source"] == "SZL" and Decimal(son["price"]) == Decimal("150.00")

    # pozitif kontrol: ayni hakedisin tabani guncel fiyata esitlenince HK kaynak olur
    line = (
        await seeded_db.execute(
            select(ProgressPaymentLine).where(ProgressPaymentLine.payment_id == hk.id)
        )
    ).scalar_one()
    line.contract_unit_price = Decimal("150.00")
    await seeded_db.flush()
    son = await _son(client, admin, katalog[0])
    assert son["source"] == "HK" and Decimal(son["price"]) == Decimal("150.00")


# ------------------------------------------- T29: esitlikte deterministik ikincil anahtar


@pytest.mark.parametrize("ters", [False, True])
async def test_szl_esit_zamanda_kucuk_proje_kodu_kazanir_kayit_sirasindan_bagimsiz(
    client, admin, seeded_db, project_factory, katalog, ters
) -> None:
    kodlar = ("PRJ-B", "PRJ-A") if ters else ("PRJ-A", "PRJ-B")
    fiyat = {"PRJ-A": "100.00", "PRJ-B": "130.00"}
    for kod in kodlar:
        p, g, _ = await _proje(seeded_db, project_factory, kod)
        await _kalem(seeded_db, p, g, katalog[0], fiyat[kod], T0)
    son = await _son(client, admin, katalog[0])
    assert son["doc_no"] == "PRJ-A" and Decimal(son["price"]) == Decimal("100.00")


@pytest.mark.parametrize("ters", [False, True])
async def test_hk_esit_onay_zamaninda_kucuk_hakedis_id_kazanir_kayit_sirasindan_bagimsiz(
    client, admin, seeded_db, project_factory, katalog, olusturan, ters
) -> None:
    kucuk = uuid.UUID("00000000-0000-4000-8000-000000000001")
    buyuk = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
    p, g, s = await _proje(seeded_db, project_factory, "PRJ-A")
    item = await _kalem(seeded_db, p, g, katalog[0], "100.00", T0)
    plan = [(2, buyuk, "1.300"), (1, kucuk, "1.100")]  # (sira, id, katsayi): 130.00 / 110.00
    for sira, pid, katsayi in reversed(plan) if ters else plan:
        await _hakedis(
            seeded_db,
            p,
            s,
            olusturan,
            item,
            sira=sira,
            durum=ProgressPaymentStatus.approved,
            fiyat="100.00",
            katsayi=katsayi,
            approved_at=T0 + timedelta(days=10),
            payment_id=pid,
        )
    son = await _son(client, admin, katalog[0])
    assert son["doc_id"] == str(kucuk) and Decimal(son["price"]) == Decimal("110.00")


# ------------------------------- SQL round() ile Python adjusted_unit_price esdegerligi


@pytest.mark.parametrize(
    ("taban", "katsayi", "elle"),
    [
        ("0.01", "1.500", "0.02"),  # 0.015 → yarim kurus yukari
        ("10.05", "1.500", "15.08"),  # 15.075
        ("0.10", "1.050", "0.11"),  # 0.105
        ("1.01", "1.005", "1.02"),  # 1.01505
        ("0.99", "1.005", "0.99"),  # 0.99495 → asagi
        ("1234.56", "1.037", "1280.24"),  # 1280.23872
    ],
)
async def test_hk_sql_round_python_adjusted_unit_price_ile_esdeger(
    seeded_db, project_factory, katalog, olusturan, taban, katsayi, elle
) -> None:
    """Katsayi kolonu Numeric(8,3) → carpim en cok 5 ondalik; yarim kurus sinirlari."""
    from app.modules.progress_payments import last_price_provider
    from app.modules.progress_payments.calculations import adjusted_unit_price

    p, g, s = await _proje(seeded_db, project_factory, "PRJ-A")
    item = await _kalem(seeded_db, p, g, katalog[0], taban, T0)
    await _hakedis(
        seeded_db,
        p,
        s,
        olusturan,
        item,
        sira=1,
        durum=ProgressPaymentStatus.approved,
        fiyat=taban,
        katsayi=katsayi,
        approved_at=T0 + timedelta(days=1),
    )
    sonuc = (await last_price_provider.provide(seeded_db, [katalog[0]]))[katalog[0]]
    assert sonuc.price == adjusted_unit_price(Decimal(taban), Decimal(katsayi))
    assert sonuc.price == Decimal(elle)


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


async def test_liste_sorgu_sayisi_kalem_sayisindan_bagimsiz(
    client, admin, seeded_db, project_factory, katalog
) -> None:
    p, g, _ = await _proje(seeded_db, project_factory, "PRJ-A")
    await _kalem(seeded_db, p, g, katalog[0], "75.00", T0)
    with _sayac() as bir:
        assert (await client.get(URL, headers=admin)).status_code == 200
    disiplin_id = (await client.get(URL, headers=admin)).json()["items"][0]["discipline"]["id"]
    for ad in ("A", "B", "C"):
        kalem = EvCatalogItem(
            poz_no=f"LP1-9{ord(ad)}",
            discipline_id=uuid.UUID(disiplin_id),
            name=ad,
            uom="m3",
            standard_unit_mhr=Decimal("1"),
            default_contractor_type=ContractorType.OWN,
        )
        seeded_db.add(kalem)
        await seeded_db.flush()
        await _kalem(seeded_db, p, g, kalem.id, "10.00", T0, code=f"X{ad}")
    # 3 kalem daha (+ ikisi fiyatli): 6 kalem
    with _sayac() as bes:
        yanit = await client.get(URL, headers=admin)
    assert len(yanit.json()["items"]) == 6
    assert len(bes) == len(bir)


# ------------------------------------------------------------ kayit bekcisi


async def test_uygulama_acilinca_beklenen_saglayicilar_kayitli() -> None:
    from app.main import app  # noqa: F401  (import yan etkisi kaydi tetikler)

    assert {kaynak for kaynak, _ in last_price.registered()} == BEKLENEN_KAYNAKLAR

"""TKL-B4.3 — `TKL` son fiyat saglayicisi (kazanilan teklifin MALIYET B.F.'si).

Gercek saglayici (sahte degil) + HTTP ile kurulan teklifler. Maliyet ve teklif B.F. BILEREK
FARKLI degerlerle kurulur (fiyat = maliyet). Zamanlar `won_at` UPDATE'iyle ACIKCA yazilir
(`now()` islem icinde sabittir); beklenen degerler elle yazilmis sabitlerdir.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update

from app.modules.offers import last_price_provider
from app.modules.offers.models import OfferRevision
from app.modules.progress_payments.models import ProgressPaymentStatus
from tests.modules.catalog.test_last_price_kaynaklar import (
    _hakedis,
    _proje,
    _sayac,
)
from tests.modules.catalog.test_last_price_kaynaklar import (
    _kalem as _szl_kalem,
)

from ._offers import durum_yap, gecis, grup, kalem, revizyon, teklif, tum_kalemler

pytestmark = pytest.mark.asyncio

D = Decimal
T0 = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
async def olusturan(user_factory):
    return await user_factory(
        email=f"o.{uuid.uuid4().hex[:6]}@tkl.co", password="parola1234", role_key="system_admin"
    )


async def _son(client, admin, katalog_id) -> dict | None:
    items = (await client.get("/catalog/items", headers=admin)).json()["items"]
    return next(i for i in items if i["id"] == str(katalog_id))["last_price"]


async def _kur(client, admin, isveren, katalog_id, maliyetler, *, durum="won", **kalem_govde):
    """Bir teklif + her `maliyet` icin bir kalem (None = fiyatsiz) + istenen durum."""
    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    for maliyet in maliyetler:
        await kalem(
            client,
            admin,
            o["id"],
            g["id"],
            katalog_id,
            cost_unit_price=maliyet,
            **(kalem_govde if maliyet is not None else {}),
        )
    await durum_yap(client, admin, o["id"], durum)
    return o


async def _won_at_yaz(db, offer_id: str, an: datetime, rev_no: int = 0) -> None:
    await db.execute(
        update(OfferRevision)
        .where(OfferRevision.offer_id == offer_id, OfferRevision.rev_no == rev_no)
        .values(won_at=an)
    )
    await db.flush()


# ------------------------------------------------------------------ kaynak olma kurali


@pytest.mark.parametrize("durum", ["draft", "sent", "lost", "withdrawn"])
async def test_kazanilmamis_revizyon_kaynak_olmaz(client, admin, isveren, katalog, durum) -> None:
    o = await teklif(client, admin, isveren)
    g = await grup(client, admin, o["id"])
    await kalem(client, admin, o["id"], g["id"], katalog[0].id, cost_unit_price="50.00")
    await durum_yap(client, admin, o["id"], durum)
    assert await _son(client, admin, katalog[0].id) is None


async def test_kazanilan_revizyon_kaynak_olur_fiyat_maliyet_bf_etiket_ve_zaman(
    client, admin, isveren, katalog
) -> None:
    o = await _kur(client, admin, isveren, katalog[0].id, ["50.00"], offer_unit_price="99.00")
    rev = await revizyon(client, admin, o["id"])
    # teklif B.F. 99.00 elle girildi; son fiyat MALIYET (50.00) olmali
    assert D(tum_kalemler(rev)[0]["offer_unit_price"]) == D("99.00")
    son = await _son(client, admin, katalog[0].id)
    assert D(son["price"]) == D("50.00")
    assert son["source"] == "TKL"
    assert son["doc_no"] == f"{o['offer_no']} Rev.0" and son["doc_id"] == o["id"]
    assert datetime.fromisoformat(son["at"]) == datetime.fromisoformat(rev["won_at"])


async def test_etiket_revizyon_no_tasir_kaybedilen_onceki_revizyon_kaynak_olmaz(
    client, admin, isveren, katalog
) -> None:
    o = await _kur(client, admin, isveren, katalog[0].id, ["70.00"], durum="lost")
    assert await _son(client, admin, katalog[0].id) is None
    r = await client.post(f"/offers/{o['id']}/revisions", headers=admin)
    assert r.status_code == 201, r.text
    await durum_yap(client, admin, o["id"], "won", rev_no=1)
    son = await _son(client, admin, katalog[0].id)
    assert son["doc_no"] == f"{o['offer_no']} Rev.1" and son["source"] == "TKL"


async def test_maliyeti_bos_kalem_kaynak_olmaz_dolu_kardesi_olur(
    client, admin, isveren, katalog
) -> None:
    await _kur(client, admin, isveren, katalog[0].id, [None])
    assert await _son(client, admin, katalog[0].id) is None
    await _kur(client, admin, isveren, katalog[1].id, [None, "30.00"])
    assert D((await _son(client, admin, katalog[1].id))["price"]) == D("30.00")


# ------------------------------------------------------------------ T29: en yuksek maliyet


@pytest.mark.parametrize("sira", [["120.00", "90.00"], ["90.00", "120.00"]])
async def test_ayni_revizyonda_ayni_katalog_iki_kalem_en_yuksek_maliyet(
    client, admin, isveren, katalog, sira
) -> None:
    await _kur(client, admin, isveren, katalog[0].id, sira)
    assert D((await _son(client, admin, katalog[0].id))["price"]) == D("120.00")


# ------------------------------------------------------------------ revizyonlar arasi


@pytest.mark.parametrize("ilk_yeni", [True, False])
async def test_iki_kazanilan_teklif_en_yeni_won_at_kazanir(
    client, admin, isveren, katalog, seeded_db, ilk_yeni
) -> None:
    a = await _kur(client, admin, isveren, katalog[0].id, ["100.00"])
    b = await _kur(client, admin, isveren, katalog[0].id, ["130.00"])
    yeni, eski = (a, b) if ilk_yeni else (b, a)
    await _won_at_yaz(seeded_db, yeni["id"], T0 + timedelta(days=5))
    await _won_at_yaz(seeded_db, eski["id"], T0)
    son = await _son(client, admin, katalog[0].id)
    assert son["doc_id"] == yeni["id"]
    assert D(son["price"]) == (D("100.00") if ilk_yeni else D("130.00"))


@pytest.mark.parametrize("ters", [False, True])
async def test_esit_won_at_kucuk_teklif_no_kazanir_kazanma_sirasindan_bagimsiz(
    client, admin, isveren, katalog, seeded_db, ters
) -> None:
    a = await _kur(client, admin, isveren, katalog[0].id, ["100.00"], durum="sent")
    b = await _kur(client, admin, isveren, katalog[0].id, ["130.00"], durum="sent")
    assert a["offer_no"] < b["offer_no"]
    for o in reversed((a, b)) if ters else (a, b):
        assert (await gecis(client, admin, o["id"], "win")).status_code == 200
    for o in (a, b):
        await _won_at_yaz(seeded_db, o["id"], T0)
    son = await _son(client, admin, katalog[0].id)
    assert son["doc_id"] == a["id"] and D(son["price"]) == D("100.00")


# ------------------------------------------------------------------ TKL / SZL / HK


async def _szl(seeded_db, project_factory, katalog_id, fiyat: str, an: datetime):
    p, g, s = await _proje(seeded_db, project_factory, "PRJ-A")
    return p, s, await _szl_kalem(seeded_db, p, g, katalog_id, fiyat, an)


@pytest.mark.parametrize(
    ("szl_gun", "beklenen"),
    [(10, "SZL"), (-10, "TKL"), (0, "SZL")],  # esitlikte SZL < TKL
)
async def test_tkl_ile_szl_en_yeni_kazanir_esitlikte_port_kaynak_sirasi(
    client, admin, isveren, katalog, seeded_db, project_factory, szl_gun, beklenen
) -> None:
    o = await _kur(client, admin, isveren, katalog[0].id, ["60.00"])
    await _won_at_yaz(seeded_db, o["id"], T0)
    await _szl(seeded_db, project_factory, katalog[0].id, "100.00", T0 + timedelta(days=szl_gun))
    assert (await _son(client, admin, katalog[0].id))["source"] == beklenen


@pytest.mark.parametrize(("hk_gun", "beklenen"), [(10, "HK"), (-10, "TKL"), (0, "HK")])
async def test_tkl_ile_hk_en_yeni_kazanir_esitlikte_hk_once(
    client, admin, isveren, katalog, seeded_db, project_factory, olusturan, hk_gun, beklenen
) -> None:
    o = await _kur(client, admin, isveren, katalog[0].id, ["60.00"])
    await _won_at_yaz(seeded_db, o["id"], T0)
    p, s, item = await _szl(
        seeded_db, project_factory, katalog[0].id, "100.00", T0 - timedelta(days=30)
    )
    await _hakedis(
        seeded_db,
        p,
        s,
        olusturan,
        item,
        sira=1,
        durum=ProgressPaymentStatus.approved,
        fiyat="100.00",
        katsayi="1.400",
        approved_at=T0 + timedelta(days=hk_gun),
    )
    assert (await _son(client, admin, katalog[0].id))["source"] == beklenen


# ------------------------------------------------------------------ tek SQL + SO-6


async def test_saglayici_tek_sql_ifadesi(seeded_db, katalog) -> None:
    with _sayac() as ifadeler:
        await last_price_provider.provide(seeded_db, [k.id for k in katalog])
    assert len(ifadeler) == 1


async def test_so6_kazanilan_teklifin_maliyeti_yeni_teklifte_on_doldurulur(
    client, admin, isveren, katalog
) -> None:
    await _kur(client, admin, isveren, katalog[0].id, ["77.50"], offer_unit_price="99.00")
    yeni = await teklif(client, admin, isveren, title="İkinci teklif")
    g = await grup(client, admin, yeni["id"])
    k = await kalem(client, admin, yeni["id"], g["id"], katalog[0].id)  # maliyet GONDERILMEDI
    assert D(k["cost_unit_price"]) == D("77.50")  # son fiyat (TKL) referans fiyati (100) ezer

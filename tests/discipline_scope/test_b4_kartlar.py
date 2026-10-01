"""DSC-B4 ADIM 1: kartlar (proje/şantiye/bölüm fiziksel %) + bölüm sayaçları kendi disiplininden.

CEO kararları: S3 fiziksel % = KENDİ disiplini, projenin TÜM şantiyeleri üzerinden, PAY ve PAYDA
kendi; kendi kalemi yoksa "—" (metric(None)). S5 bölüm `boq_item_count` + `budget` kendi disiplini;
`budget_amount` (elle), `worker_count`, `section_count` DEĞİŞMEZ. Ü3: mali ilerleme, spent,
worker_count, Project.progress_pct süzülmez. Ü7: 404 YOK.

Beklenen sayılar SQL'den BAĞIMSIZ, Python'da (ORM satırları + port `item_disciplines`) hesaplanır;
sonra servis fonksiyonlarının iç PAY/PAYDA toplamlarıyla `==` kıyaslanır (yüzdeyle değil).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import (
    UNRESTRICTED,
    DisciplineScope,
    item_disciplines,
    item_visible_clause,
)
from app.modules.boq import counts as boq_counts
from app.modules.boq import progress as boq_progress
from app.modules.boq.models import BoqItem, BoqItemSectionAllocation
from app.modules.boq.progress import (
    _project_taban,
    _section_taban,
    _site_taban,
    _weighted_for_scope,
    weighted_pct,
)
from app.modules.boq.schemas import quantize_money
from app.modules.site_diary.models import DiaryStatus, SiteDiaryEntry, SiteDiaryLine
from tests._section_types import SEED_TYPE_IDS
from tests.discipline_scope._b4_dunya import DunyaB4

D = Decimal
_HIC = "yok"  # eşlemesiz (disiplinsiz) kalem kümesi


def _kapsam(x: DunyaB4, ad: str) -> DisciplineScope:
    return {
        "civil": DisciplineScope.of({x.d.kab.id}),
        "elek": DisciplineScope.of({x.d.duv.id}),
        "atamasiz": UNRESTRICTED,
    }[ad]


# --------------------------------------------------------------------------- #
# Bağımsız (Python) beklenen değer
# --------------------------------------------------------------------------- #


async def _beklenen(
    session: AsyncSession,
    x: DunyaB4,
    *,
    site_ids: list[uuid.UUID],
    bolum_id: uuid.UUID | None = None,
    kume: str,
) -> tuple[Decimal, Decimal, int, Decimal]:
    """(pay, payda, farklı kalem sayısı, tahsis/BOQ tutarı) — `kume`: civil | elek | yok | hepsi."""
    kalemler = (
        (await session.execute(select(BoqItem).where(BoqItem.site_id.in_(site_ids))))
        .scalars()
        .all()
    )
    taban = {k.id: k.quantity for k in kalemler}
    if bolum_id is not None:
        tahsis = (
            (
                await session.execute(
                    select(BoqItemSectionAllocation).where(
                        BoqItemSectionAllocation.section_id == bolum_id
                    )
                )
            )
            .scalars()
            .all()
        )
        taban = {t.boq_item_id: t.quantity for t in tahsis}
    fiyat = {k.id: k.unit_price for k in kalemler}
    disiplin = await item_disciplines(session, list(taban))
    secili = {
        "civil": {x.d.kab.id},
        "elek": {x.d.duv.id},
    }
    dahil = [
        i
        for i in taban
        if kume == "hepsi"
        or (kume == _HIC and disiplin[i] is None)
        or (kume in secili and disiplin[i] in secili[kume])
    ]
    satirlar = (
        await session.execute(
            select(
                SiteDiaryLine.boq_item_id,
                SiteDiaryLine.section_id,
                SiteDiaryEntry.section_id,
                SiteDiaryLine.quantity,
            )
            .join(SiteDiaryEntry, SiteDiaryEntry.id == SiteDiaryLine.entry_id)
            .where(
                SiteDiaryEntry.status == DiaryStatus.submitted,
                SiteDiaryLine.boq_item_id.in_(dahil or [uuid.uuid4()]),
            )
        )
    ).all()
    gerceklesen: dict[uuid.UUID, Decimal] = {}
    for kalem_id, satir_bolum, baslik_bolum, miktar in satirlar:
        if bolum_id is not None and (satir_bolum or baslik_bolum) != bolum_id:
            continue
        gerceklesen[kalem_id] = gerceklesen.get(kalem_id, D(0)) + miktar
    pay = sum((gerceklesen.get(i, D(0)) * fiyat[i] for i in dahil), D(0))
    payda = sum((taban[i] * fiyat[i] for i in dahil), D(0))
    tutar = quantize_money(sum((quantize_money(taban[i] * fiyat[i]) for i in dahil), D(0)))
    return pay, payda, len(dahil), tutar


async def _gercek(
    session: AsyncSession,
    x: DunyaB4,
    *,
    kapsam: DisciplineScope,
    site: uuid.UUID | None = None,
    proje: uuid.UUID | None = None,
    bolum: uuid.UUID | None = None,
) -> tuple[Decimal, Decimal]:
    if site is not None:
        return await _weighted_for_scope(session, _site_taban(site, kapsam))
    if proje is not None:
        return await _weighted_for_scope(session, _project_taban(proje, kapsam))
    assert bolum is not None
    return await _weighted_for_scope(session, _section_taban(bolum, kapsam), section_id=bolum)


def _yuzde(pay: Decimal, payda: Decimal) -> str | None:
    p = weighted_pct(pay, payda)
    return None if p is None else str(p)


def _p(zarf: dict) -> str | None:
    return zarf["value"]


# Kapsam tanımları: ad -> (site_ids, bolum_id) çözücü
def _kapsamlar(x: DunyaB4) -> dict[str, dict]:
    return {
        "santiye_a": {"site_ids": [x.d.santiye.id], "site": x.d.santiye.id},
        "santiye_b": {"site_ids": [x.santiye_b.id], "site": x.santiye_b.id},
        "proje_1": {
            "site_ids": [x.d.santiye.id, x.santiye_b.id],
            "proje": x.d.proje.id,
        },
        "bolum_s1": {"site_ids": [x.d.santiye.id], "bolum": x.d.s1.id},
        "bolum_s2": {"site_ids": [x.d.santiye.id], "bolum": x.d.s2.id},
        "bolum_sb1": {"site_ids": [x.santiye_b.id], "bolum": x.bolum_b.id},
    }


KAPSAM_ADLARI = ["santiye_a", "santiye_b", "proje_1", "bolum_s1", "bolum_s2", "bolum_sb1"]


@pytest.mark.parametrize("ad", KAPSAM_ADLARI)
async def test_pay_ve_payda_toplami_disiplin_kumelerine_ayrisir(
    dunya_b4: DunyaB4, seeded_db: AsyncSession, ad: str
) -> None:
    """Σ PAY ve PAYDA (Decimal ==): civil + elek + eşlemesiz = atamasız; kümeler Python ile aynı."""
    x = dunya_b4
    k = _kapsamlar(x)[ad]
    hedef = {a: v for a, v in k.items() if a in ("site", "proje", "bolum")}
    toplam = {"pay": D(0), "payda": D(0)}
    for kume, kapsam in (("civil", "civil"), ("elek", "elek")):
        gercek = await _gercek(seeded_db, x, kapsam=_kapsam(x, kapsam), **hedef)
        beklenen = await _beklenen(
            seeded_db, x, site_ids=k["site_ids"], bolum_id=k.get("bolum"), kume=kume
        )
        assert gercek == beklenen[:2], (ad, kume)
        toplam["pay"] += gercek[0]
        toplam["payda"] += gercek[1]
    yok = await _beklenen(seeded_db, x, site_ids=k["site_ids"], bolum_id=k.get("bolum"), kume=_HIC)
    hepsi = await _gercek(seeded_db, x, kapsam=UNRESTRICTED, **hedef)
    assert (toplam["pay"] + yok[0], toplam["payda"] + yok[1]) == hepsi, ad
    assert (
        hepsi
        == (
            await _beklenen(
                seeded_db, x, site_ids=k["site_ids"], bolum_id=k.get("bolum"), kume="hepsi"
            )
        )[:2]
    )
    # Dünya anlamlı: kısıtlı küme atamasızdan KÜÇÜK (süzgeç gerçekten bir şey eliyor).
    civil = await _gercek(seeded_db, x, kapsam=_kapsam(x, "civil"), **hedef)
    assert civil[1] < hepsi[1], ad


@pytest.mark.parametrize("ad", ["bolum_s1", "bolum_s2", "bolum_sb1"])
async def test_bolum_sayaclari_toplami(dunya_b4: DunyaB4, seeded_db: AsyncSession, ad: str) -> None:
    """Σ item_count ve Σ amount: civil + elek + eşlemesiz = atamasız."""
    x = dunya_b4
    k = _kapsamlar(x)[ad]
    bolum = k["bolum"]
    sayilar: dict[str, tuple[int, Decimal]] = {}
    for kapsam in ("civil", "elek", "atamasiz"):
        sonuc = (await boq_counts.by_section(seeded_db, [bolum], _kapsam(x, kapsam))).get(
            bolum, boq_counts.EMPTY
        )
        sayilar[kapsam] = (sonuc.item_count, sonuc.amount)
    for kume, kapsam in (("civil", "civil"), ("elek", "elek")):
        b = await _beklenen(seeded_db, x, site_ids=k["site_ids"], bolum_id=bolum, kume=kume)
        assert sayilar[kapsam] == (b[2], b[3]), (ad, kume)
    yok = await _beklenen(seeded_db, x, site_ids=k["site_ids"], bolum_id=bolum, kume=_HIC)
    assert sayilar["civil"][0] + sayilar["elek"][0] + yok[2] == sayilar["atamasiz"][0]
    assert sayilar["civil"][1] + sayilar["elek"][1] + yok[3] == sayilar["atamasiz"][1]


async def test_kendi_kalemi_olmayan_bolum_gercek_sifir(
    dunya_b4: DunyaB4, client: AsyncClient
) -> None:
    """S2'de elek kalemi YOK: sayaç 0 / 0.00 (gerçek sıfır), yüzde "—", 404 YOK (Ü7)."""
    x = dunya_b4
    yanit = await client.get(f"/sections/{x.d.s2.id}", headers=x.d.baslik["elek"])
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["boq_item_count"] == {"available": True, "count": 0, "pending_module": "boq"}
    assert govde["budget"]["value"] == "0.00" and govde["budget"]["available"] is True
    assert govde["progress_pct"]["available"] is False and _p(govde["progress_pct"]) is None
    # Bölüm listesi de aynı (toplu yol) ve S1'de elek kendi kalemini (Tuğla 50×20) görür.
    liste = await client.get(f"/sites/{x.d.santiye.id}/sections", headers=x.d.baslik["elek"])
    assert liste.status_code == 200
    satirlar = {s["id"]: s for s in liste.json()["items"]}
    assert satirlar[str(x.d.s2.id)]["boq_item_count"]["count"] == 0
    assert satirlar[str(x.d.s1.id)]["boq_item_count"]["count"] == 1
    assert satirlar[str(x.d.s1.id)]["budget"]["value"] == "1000.00"


# --------------------------------------------------------------------------- #
# API: kartlar kendi disiplininden
# --------------------------------------------------------------------------- #


async def _al(client: AsyncClient, baslik: dict, yol: str, **params) -> dict:
    yanit = await client.get(yol, headers=baslik, params=params or None)
    assert yanit.status_code == 200, (yol, yanit.text)
    return yanit.json()


async def _beklenen_yuzde(session: AsyncSession, x: DunyaB4, ad: str, kume: str) -> str | None:
    k = _kapsamlar(x)[ad]
    b = await _beklenen(session, x, site_ids=k["site_ids"], bolum_id=k.get("bolum"), kume=kume)
    return _yuzde(b[0], b[1])


@pytest.mark.parametrize("aktor", ["civil", "elek"])
async def test_kartlar_kendi_disiplininden(
    dunya_b4: DunyaB4, seeded_db: AsyncSession, client: AsyncClient, aktor: str
) -> None:
    x = dunya_b4
    h = x.d.baslik[aktor]
    adm = x.d.baslik["atamasiz"]
    # Şantiye detayı A/B + liste kartları
    for site, ad in ((x.d.santiye, "santiye_a"), (x.santiye_b, "santiye_b")):
        beklenen = await _beklenen_yuzde(seeded_db, x, ad, aktor)
        detay = await _al(client, h, f"/sites/{site.id}")
        assert _p(detay["progress_pct"]) == beklenen, ad
        atamasiz = await _al(client, adm, f"/sites/{site.id}")
        assert _p(atamasiz["progress_pct"]) != beklenen, ad
    liste = await _al(client, h, f"/projects/{x.d.proje.id}/sites")
    kartlar = {c["id"]: c for c in liste["items"]}
    assert _p(kartlar[str(x.d.santiye.id)]["progress_pct"]) == await _beklenen_yuzde(
        seeded_db, x, "santiye_a", aktor
    )
    assert _p(kartlar[str(x.santiye_b.id)]["progress_pct"]) == await _beklenen_yuzde(
        seeded_db, x, "santiye_b", aktor
    )
    # Bölüm S1/S2/SB1: tekil uç + şantiye detayı içindeki bölümler + bölüm listesi
    for bolum, ad in ((x.d.s1, "bolum_s1"), (x.d.s2, "bolum_s2"), (x.bolum_b, "bolum_sb1")):
        beklenen = await _beklenen_yuzde(seeded_db, x, ad, aktor)
        assert _p((await _al(client, h, f"/sections/{bolum.id}"))["progress_pct"]) == beklenen, ad
    a_detay = await _al(client, h, f"/sites/{x.d.santiye.id}")
    for s in a_detay["sections"]:
        ad = "bolum_s1" if s["id"] == str(x.d.s1.id) else "bolum_s2"
        assert _p(s["progress_pct"]) == await _beklenen_yuzde(seeded_db, x, ad, aktor), ad
        assert (
            s["boq_item_count"]["count"]
            == (
                await _beklenen(
                    seeded_db, x, site_ids=[x.d.santiye.id], bolum_id=uuid.UUID(s["id"]), kume=aktor
                )
            )[2]
        )
    # Proje P1 detay + liste (projenin TÜM şantiyeleri üzerinden)
    beklenen_p = await _beklenen_yuzde(seeded_db, x, "proje_1", aktor)
    pd = await _al(client, h, f"/projects/{x.d.proje.id}")
    assert _p(pd["contracting"]["physical_progress"]) == beklenen_p
    pl = await _al(client, h, "/projects")
    satir = next(i for i in pl["items"] if i["id"] == str(x.d.proje.id))
    assert _p(satir["contracting"]["physical_progress"]) == beklenen_p
    admin_pd = await _al(client, adm, f"/projects/{x.d.proje.id}")
    assert _p(admin_pd["contracting"]["physical_progress"]) == "12.66"
    assert beklenen_p != "12.66"


@pytest.mark.parametrize("aktor", ["civil", "elek"])
async def test_proje2_revizyonsuz_kisitlida_tire(
    dunya_b4: DunyaB4, client: AsyncClient, aktor: str
) -> None:
    """P2'de EV revizyonu yok → kalem disiplini NULL → kısıtlıda görünür kalem yok → "—";
    atamasızda golden'daki 40.00."""
    x = dunya_b4
    pd = await _al(client, x.d.baslik[aktor], f"/projects/{x.proje2.id}")
    zarf = pd["contracting"]["physical_progress"]
    assert zarf["available"] is False and zarf["value"] is None
    pl = await _al(client, x.d.baslik[aktor], "/projects")
    satir = next(i for i in pl["items"] if i["id"] == str(x.proje2.id))
    assert satir["contracting"]["physical_progress"]["value"] is None
    adm = await _al(client, x.d.baslik["atamasiz"], f"/projects/{x.proje2.id}")
    assert _p(adm["contracting"]["physical_progress"]) == "40.00"


# --------------------------------------------------------------------------- #
# Ü3 + F1
# --------------------------------------------------------------------------- #


async def _u3_alanlari(client: AsyncClient, x: DunyaB4, baslik: dict) -> dict:
    pd = await _al(client, baslik, f"/projects/{x.d.proje.id}")
    pl = await _al(client, baslik, "/projects")
    p_satir = next(i for i in pl["items"] if i["id"] == str(x.d.proje.id))
    sd = await _al(client, baslik, f"/sites/{x.d.santiye.id}")
    bd = await _al(client, baslik, f"/sections/{x.d.s1.id}")
    sl = await _al(client, baslik, f"/projects/{x.d.proje.id}/sites")

    def proje(p: dict) -> dict:
        c = p["contracting"]
        return {
            "financial": c["financial_progress"],
            "spent": c["spent"],
            "workers": c["worker_count"],
            "budget": p["budget"],
            "progress_pct": p["progress_pct"],
        }

    return {
        "proje_detay": proje(pd),
        "proje_liste": proje(p_satir),
        "santiye": {
            "worker": sd["worker_count"],
            "section_count": sd["section_count"],
            "budget": sd["budget"],
            "bolumler": [(s["worker_count"], s["planned_worker_count"]) for s in sd["sections"]],
        },
        "bolum": (bd["section_type"], bd["worker_count"]),
        "santiye_listesi": [(c["worker_count"], c["section_count"]) for c in sl["items"]],
        "santiye_kpi": sl["totals"],
    }


async def test_u3_mali_ve_sayaclar_kisitlida_degismez(
    dunya_b4: DunyaB4, client: AsyncClient, sabit_bugun_b4
) -> None:
    x = dunya_b4
    adm = x.d.baslik["atamasiz"]
    # BLF-B1: bölüm `budget_amount` kaldırıldı; `budget` zarfı kısıtlıda DİSİPLİNLİ süzülür (S5),
    # kıyaslanamaz. Elle girilen, süzülmeyen bölüm alanı = `section_type` (bölüm detayında; dolu).
    r1 = await client.patch(
        f"/sections/{x.d.s1.id}",
        headers=adm,
        json={"section_type_id": str(SEED_TYPE_IDS["structural"])},
    )
    assert r1.status_code == 200, r1.text
    r2 = await client.patch(f"/sites/{x.d.santiye.id}", headers=adm, json={"budget": "750000"})
    assert r2.status_code == 200, r2.text
    pm = await _u3_alanlari(client, x, x.d.baslik["pm_atamasiz"])
    assert pm["proje_detay"]["workers"]["count"] == 4  # sıfır DEĞİL (today sabit)
    assert pm["proje_detay"]["financial"]["value"] == "5.00"
    assert pm["bolum"][0] == {"id": str(SEED_TYPE_IDS["structural"]), "name": "Kaba İnşaat"}
    for aktor in ("civil", "elek"):
        assert await _u3_alanlari(client, x, x.d.baslik[aktor]) == pm, aktor


async def test_f1_pm_atamasiz_kisitsiz_degerler(dunya_b4: DunyaB4, client: AsyncClient) -> None:
    """Pozitif kontrol: atamasız PM = atamasız golden değerleri (kısıtsız, bugünkü)."""
    x = dunya_b4
    pm = x.d.baslik["pm_atamasiz"]
    assert _p((await _al(client, pm, f"/sites/{x.d.santiye.id}"))["progress_pct"]) == "8.13"
    assert _p((await _al(client, pm, f"/sites/{x.santiye_b.id}"))["progress_pct"]) == "17.19"
    assert _p((await _al(client, pm, f"/sections/{x.d.s1.id}"))["progress_pct"]) == "9.38"
    assert _p((await _al(client, pm, f"/sections/{x.d.s2.id}"))["progress_pct"]) == "12.22"
    pd = await _al(client, pm, f"/projects/{x.d.proje.id}")
    assert _p(pd["contracting"]["physical_progress"]) == "12.66"
    p2 = await _al(client, pm, f"/projects/{x.proje2.id}")
    assert _p(p2["contracting"]["physical_progress"]) == "40.00"
    s1 = await _al(client, pm, f"/sections/{x.d.s1.id}")
    assert s1["boq_item_count"]["count"] == 2 and s1["budget"]["value"] == "1600.00"


# --------------------------------------------------------------------------- #
# Sorgu sayısı
# --------------------------------------------------------------------------- #


@contextmanager
def _sayac(motor):
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(motor.sync_engine, "before_cursor_execute", kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(motor.sync_engine, "before_cursor_execute", kaydet)


async def test_kart_listelerinde_sorgu_sayisi_kisitli_ve_atamasiz_esit(
    dunya_b4: DunyaB4, client: AsyncClient
) -> None:
    from tests.conftest import test_engine

    x = dunya_b4
    yollar = ["/projects", f"/projects/{x.d.proje.id}/sites", f"/sites/{x.d.santiye.id}/sections"]
    for yol in yollar:
        # ilk çağrı ısınma (önbellek); sayım ikinci çağrılarda
        for ad in ("civil", "pm_atamasiz"):
            await client.get(yol, headers=x.d.baslik[ad])
        sayimlar = {}
        for ad in ("civil", "elek", "pm_atamasiz"):
            with _sayac(test_engine) as ifadeler:
                yanit = await client.get(yol, headers=x.d.baslik[ad])
                assert yanit.status_code == 200
            sayimlar[ad] = len(ifadeler)
        assert sayimlar["civil"] == sayimlar["pm_atamasiz"] == sayimlar["elek"], (yol, sayimlar)


# --------------------------------------------------------------------------- #
# Yazma yanıtları (scope'lu)
# --------------------------------------------------------------------------- #


async def test_yazma_yanitlari_kisitlida_suzulmus(
    dunya_b4: DunyaB4, seeded_db: AsyncSession, client: AsyncClient
) -> None:
    x = dunya_b4
    # Kimlikler ve beklenenler yazmadan ÖNCE alınır (yazma sonrası ORM nesneleri süresi dolar).
    a_id, s1_id, proje_id = x.d.santiye.id, x.d.s1.id, x.d.proje.id
    bekl = {
        (ad, k): await _beklenen_yuzde(seeded_db, x, ad, k)
        for ad, k in (
            ("santiye_a", "civil"),
            ("bolum_s1", "civil"),
            ("bolum_s2", "civil"),
            ("bolum_s1", "elek"),
            ("proje_1", "civil"),
        )
    }
    civil_y = x.d.baslik["civil_yazar"]
    elek_y = x.d.baslik["elek_yazar"]
    adm_k = x.d.baslik["admin_kisitli"]
    # PATCH /sites/{id}
    yanit = await client.patch(f"/sites/{a_id}", headers=civil_y, json={"name": "A-Blok Güncel"})
    assert yanit.status_code == 200, yanit.text
    assert _p(yanit.json()["progress_pct"]) == bekl[("santiye_a", "civil")]
    assert _p(yanit.json()["progress_pct"]) != "8.13"
    for s in yanit.json()["sections"]:
        bolum_ad = "bolum_s1" if s["id"] == str(s1_id) else "bolum_s2"
        assert _p(s["progress_pct"]) == bekl[(bolum_ad, "civil")]
    # PATCH /sections/{id} (elek: S1'deki Tuğla)
    yanit = await client.patch(f"/sections/{s1_id}", headers=elek_y, json={"name": "A Blok Güncel"})
    assert yanit.status_code == 200, yanit.text
    assert _p(yanit.json()["progress_pct"]) == bekl[("bolum_s1", "elek")]
    assert yanit.json()["boq_item_count"]["count"] == 1
    assert yanit.json()["budget"]["value"] == "1000.00"
    # POST /sites/{id}/sections (yeni bölüm: kalem yok → "—", sayaç 0)
    yanit = await client.post(
        f"/sites/{a_id}/sections",
        headers=civil_y,
        json={
            "name": "Yeni Blok",
            "code": "BLM-Y",
            "section_type_id": str(SEED_TYPE_IDS["structural"]),
            "manager_name": "Sorumlu",
            "start_date": "2026-06-01",
            "end_date": "2026-06-30",
        },
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["boq_item_count"]["count"] == 0
    assert _p(yanit.json()["progress_pct"]) is None
    # POST /projects/{id}/sites (yeni şantiye; yanıt gövdesi scope'la üretilir)
    yanit = await client.post(
        f"/projects/{proje_id}/sites",
        headers=civil_y,
        json={"name": "Yeni Şantiye", "is_draft": True},
    )
    assert yanit.status_code == 201, yanit.text
    assert _p(yanit.json()["progress_pct"]) is None
    # PATCH /projects/{id}
    yanit = await client.patch(
        f"/projects/{proje_id}", headers=adm_k, json={"name": "Disiplin Projesi Güncel"}
    )
    assert yanit.status_code == 200, yanit.text
    zarf = yanit.json()["contracting"]["physical_progress"]
    assert _p(zarf) == bekl[("proje_1", "civil")]
    assert _p(zarf) != "12.66"
    # POST /projects (yanıt scope'lu detay derleyicisinden geçer)
    yanit = await client.post(
        "/projects",
        headers=adm_k,
        json={
            "name": "Yeni Proje",
            "code": "DSC-P09",
            "project_type": "kendi_yatirim",
            "city": "Bursa",
        },
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["contracting"] is None  # kendi yatırım: taahhüt şeridi yok


# --------------------------------------------------------------------------- #
# Yapısal
# --------------------------------------------------------------------------- #


def test_gerceklesen_tarafina_item_visible_derleme_hatasi() -> None:
    """Süzgeç YALNIZ tabana konur: SiteDiaryLine'ın FROM'unda BoqItem YOK → derlemede RuntimeError
    (fail-open korelasyon düşmesi olmaz). Negatif kontrol."""
    from sqlalchemy.dialects import postgresql

    kisitli = DisciplineScope.of({uuid.uuid4()})
    stmt = select(SiteDiaryLine.boq_item_id).where(item_visible_clause(kisitli, BoqItem))
    with pytest.raises(RuntimeError, match="ItemVisible"):
        stmt.compile(dialect=postgresql.dialect())


def test_scope_zorunlu_varsayilansiz() -> None:
    """S6: fiziksel yüzde ve sayaç okuyucularının `scope` parametresinin VARSAYILANI yoktur."""
    import inspect

    for fn in (
        boq_progress.physical_for_site,
        boq_progress.physical_for_section,
        boq_progress.physical_for_project,
        boq_progress.physical_for_sites,
        boq_progress.physical_for_sections,
        boq_progress.physical_for_projects,
        boq_counts.by_section,
    ):
        parametre = inspect.signature(fn).parameters["scope"]
        assert parametre.default is inspect.Parameter.empty, fn.__name__
    assert not hasattr(boq_progress, "_kapsam_maddeleri")

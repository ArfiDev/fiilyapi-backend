"""PLN-B2.1 (spec §2, B1-3, B2-8) — günlük miktar satırında BÖLÜM kırılımı.

Yaprak = kalem × bölüm. `section_id` boş = "Bölümsüz" (kalemin bölüme tahsis
edilmemiş kalanı).

## Kurallar (bu dosyada TESTLE SABİTLENİR)

* Satır kimliği (kalem, bölüm). Tekillik iki kısmi indeksle: bölümlü dal ve
  Bölümsüz dal (UNIQUE'te NULL'lar farklı sayılır).
* Bölümlü YENİ satır için bölüm şantiyeye ait (422) ve kalemin o bölüme TAHSİSİ
  olmalı (422). Var olan bölümlü satır, tahsisi sonradan kalksa da kalabilir.
* **Bölümsüz her zaman yazılabilir** — eski istemcinin tek dalı; kalanı aşan miktar
  engel değil, `remaining_quantity < 0` (aşım) olarak görünür.
* Yaprak türevleri (gömülü, N+1 yok): `leaf_cumulative_quantity` = aynı şantiye +
  kalem + bölüm için bu günden ÖNCEKİ GÖNDERİLMİŞ günlükler (TÜM zamanlar) + bu
  satır; `planned_quantity` = bölümlüde tahsis, Bölümsüz'de miktar − Σ tahsis;
  `remaining_quantity` = planlı − yaprak kümülatifi.
* Mevcut `cumulative_quantity` (GK229) AYNEN: KALEM düzeyinde, AY içinde.
* Kalem düzeyi toplamlar (özet · hakediş önerisi · BOQ ilerlemesi) bölümleri TOPLAR.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqItemSectionAllocation
from app.modules.boq.progress import realized_by_item
from app.modules.site_diary import guards, repository
from app.modules.site_diary.models import DiaryStatus, SiteDiaryEntry, SiteDiaryLine
from app.modules.sites.models import Section
from tests._silme_yardimci import onizle, sil_aile
from tests.site_diary.conftest import VARSAYILAN_TARIH

pytestmark = pytest.mark.asyncio


# --- Yardımcılar ---


async def _tahsis(session: AsyncSession, item, section, quantity: str) -> BoqItemSectionAllocation:
    row = BoqItemSectionAllocation(
        boq_item_id=item.id, section_id=section.id, quantity=Decimal(quantity)
    )
    session.add(row)
    await session.flush()
    return row


def _satir(item, qty: str, section=None, **ekstra) -> dict:
    govde = {"boq_item_id": str(item.id), "quantity": qty, **ekstra}
    if section is not None:
        govde["section_id"] = str(section.id)
    return govde


async def _gun(client: AsyncClient, headers, site_id, tarih: date, satirlar, *, gonder: bool):
    kayit = await client.post(
        f"/sites/{site_id}/diary", json={"entry_date": tarih.isoformat()}, headers=headers
    )
    assert kayit.status_code == 201, kayit.text
    entry_id = kayit.json()["id"]
    yanit = await client.put(f"/diary/{entry_id}/lines", json={"lines": satirlar}, headers=headers)
    assert yanit.status_code == 200, yanit.text
    if gonder:
        gonderim = await client.post(f"/diary/{entry_id}/submit", headers=headers)
        assert gonderim.status_code == 200, gonderim.text
        return gonderim.json()
    return yanit.json()


def _bul(govde: dict, item, section=None) -> dict:
    sid = None if section is None else str(section.id)
    return next(
        s for s in govde["lines"] if s["boq_item_id"] == str(item.id) and s["section_id"] == sid
    )


@pytest.fixture
async def bolum2(seeded_db: AsyncSession, santiye) -> Section:
    site, _, _ = santiye
    section = Section(site_id=site.id, code="B-2", name="B Blok")
    seeded_db.add(section)
    await seeded_db.flush()
    return section


# --- Geri uyum ---


async def test_ESKI_GOVDE_bolumsuz_satir_aynen_calisir(
    client: AsyncClient, admin_headers, santiye
) -> None:
    """`section_id`siz gövde bugünkü gibi çalışır; yeni alanlar yalnız EKLENİR."""
    site, _, items = santiye
    govde = await _gun(
        client, admin_headers, site.id, VARSAYILAN_TARIH, [_satir(items[0], "3")], gonder=False
    )

    (satir,) = govde["lines"]
    assert satir["section_id"] is None
    assert satir["overrun_reason"] is None
    assert Decimal(satir["quantity"]) == Decimal("3")
    assert Decimal(satir["cumulative_quantity"]) == Decimal("3")
    assert Decimal(satir["leaf_cumulative_quantity"]) == Decimal("3")
    # Tahsis yok: Bölümsüz planlı = kalem miktarının TAMAMI (200).
    assert Decimal(satir["planned_quantity"]) == Decimal("200")
    assert Decimal(satir["remaining_quantity"]) == Decimal("197")


async def test_bolumsuz_satir_tahsisli_kalemde_de_yazilir(
    client: AsyncClient, admin_headers, santiye, bolum, seeded_db: AsyncSession
) -> None:
    """Canlıda tahsisli kalemler var ve eski ekran YALNIZ Bölümsüz yazar — 422 OLMAMALI.
    Kalan tamamen tahsisliyse planlı 0'dır, miktar AŞIM olarak görünür.

    GKS-B1/G4: tamamen tahsisli kalemde POST iskeleti Bölümsüz satır AÇMAZ; bu yüzden gövde
    `section_id: null` anahtarını AÇIKÇA taşır (anahtarsız B2 öncesi imza, kayıtta bölümlü
    satır varken 409 `STALE_CLIENT` alır — `test_gks_b1_g4.py` bunu da sabitler)."""
    site, _, items = santiye
    await _tahsis(seeded_db, items[0], bolum, "200")

    govde = await _gun(
        client,
        admin_headers,
        site.id,
        VARSAYILAN_TARIH,
        [_satir(items[0], "5", section_id=None)],
        gonder=False,
    )

    satir = _bul(govde, items[0])
    assert Decimal(satir["planned_quantity"]) == Decimal("0")
    assert Decimal(satir["remaining_quantity"]) == Decimal("-5")


# --- Doğrulama ---


async def test_bolumlu_satir_tahsis_yoksa_422(
    client: AsyncClient, admin_headers, santiye, bolum
) -> None:
    site, _, items = santiye
    kayit = await _gun(client, admin_headers, site.id, VARSAYILAN_TARIH, [], gonder=False)

    yanit = await client.put(
        f"/diary/{kayit['id']}/lines",
        json={"lines": [_satir(items[0], "1", bolum)]},
        headers=admin_headers,
    )

    assert yanit.status_code == 422, yanit.text
    assert yanit.json()["detail"] == guards.LINE_SECTION_NOT_ALLOCATED


async def test_baska_santiyenin_ya_da_olmayan_bolum_422_ayni_cumle(
    client: AsyncClient, admin_headers, santiye, santiye_fabrikasi, seeded_db: AsyncSession
) -> None:
    site, _, items = santiye
    diger_site, _, _ = await santiye_fabrikasi("SD-X")
    yabanci = Section(site_id=diger_site.id, code="Y-1", name="Yabancı")
    seeded_db.add(yabanci)
    await seeded_db.flush()
    kayit = await _gun(client, admin_headers, site.id, VARSAYILAN_TARIH, [], gonder=False)

    yanitlar = [
        await client.put(
            f"/diary/{kayit['id']}/lines",
            json={"lines": [{**_satir(items[0], "1"), "section_id": str(sid)}]},
            headers=admin_headers,
        )
        for sid in (yabanci.id, uuid.uuid4())
    ]

    assert [y.status_code for y in yanitlar] == [422, 422]
    assert {y.json()["detail"] for y in yanitlar} == {guards.LINE_SECTION_MISMATCH}


async def test_ayni_kalem_ayni_bolum_govdede_iki_kez_409(
    client: AsyncClient, admin_headers, santiye, bolum, seeded_db: AsyncSession
) -> None:
    site, _, items = santiye
    await _tahsis(seeded_db, items[0], bolum, "50")
    kayit = await _gun(client, admin_headers, site.id, VARSAYILAN_TARIH, [], gonder=False)

    for govde in (
        [_satir(items[0], "1", bolum), _satir(items[0], "2", bolum)],
        [_satir(items[0], "1"), _satir(items[0], "2")],
    ):
        yanit = await client.put(
            f"/diary/{kayit['id']}/lines", json={"lines": govde}, headers=admin_headers
        )
        assert yanit.status_code == 409, yanit.text
        assert yanit.json()["detail"] == guards.DUPLICATE_LINE


# --- Mutlu yol + türevler ---


async def test_ayni_kalem_bolumlu_ve_bolumsuz_birlikte_planli_kalan(
    client: AsyncClient, admin_headers, santiye, bolum, bolum2, seeded_db: AsyncSession
) -> None:
    site, _, items = santiye
    await _tahsis(seeded_db, items[0], bolum, "120")
    await _tahsis(seeded_db, items[0], bolum2, "30")

    govde = await _gun(
        client,
        admin_headers,
        site.id,
        VARSAYILAN_TARIH,
        [_satir(items[0], "10", bolum), _satir(items[0], "5")],
        gonder=False,
    )

    assert len(govde["lines"]) == 2
    s1 = _bul(govde, items[0], bolum)
    bs = _bul(govde, items[0])
    assert (Decimal(s1["planned_quantity"]), Decimal(s1["remaining_quantity"])) == (
        Decimal("120"),
        Decimal("110"),
    )
    # Bölümsüz = 200 − (120 + 30) = 50.
    assert (Decimal(bs["planned_quantity"]), Decimal(bs["remaining_quantity"])) == (
        Decimal("50"),
        Decimal("45"),
    )


async def test_yaprak_kumulatifi_tum_zamanlar_yalniz_gonderilmis_ve_ONCEKI_gunler(
    client: AsyncClient, admin_headers, santiye, bolum, seeded_db: AsyncSession
) -> None:
    site, _, items = santiye
    await _tahsis(seeded_db, items[0], bolum, "120")
    kalem = items[0]
    # Önceki AY, gönderilmiş → yaprak kümülatifine GİRER (ay sınırı yok).
    await _gun(
        client, admin_headers, site.id, date(2026, 6, 20), [_satir(kalem, "7", bolum)], gonder=True
    )
    # Aynı ay, önceki gün, gönderilmiş → girer.
    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [_satir(kalem, "10", bolum), _satir(kalem, "3")],
        gonder=True,
    )
    # Önceki gün ama TASLAK → girmez.
    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 7, 12),
        [_satir(kalem, "100", bolum)],
        gonder=False,
    )
    # SONRAKİ gün, gönderilmiş → girmez.
    await _gun(
        client, admin_headers, site.id, date(2026, 7, 20), [_satir(kalem, "50", bolum)], gonder=True
    )

    govde = await _gun(
        client,
        admin_headers,
        site.id,
        VARSAYILAN_TARIH,
        [_satir(kalem, "4", bolum), _satir(kalem, "2")],
        gonder=False,
    )

    s1 = _bul(govde, kalem, bolum)
    bs = _bul(govde, kalem)
    assert Decimal(s1["leaf_cumulative_quantity"]) == Decimal("21")  # 7 + 10 + 4
    assert Decimal(bs["leaf_cumulative_quantity"]) == Decimal("5")  # 3 + 2
    assert Decimal(s1["remaining_quantity"]) == Decimal("99")
    # GK229 `cumulative_quantity` AYNEN: kalem düzeyi, AY içi → 10+3 (7/10) + 4+2 (bugün).
    assert Decimal(s1["cumulative_quantity"]) == Decimal("19")
    assert Decimal(bs["cumulative_quantity"]) == Decimal("19")


async def test_asim_gerekcesi_kaydedilir_kirpilir_bos_null(
    client: AsyncClient, admin_headers, santiye, bolum, seeded_db: AsyncSession
) -> None:
    site, _, items = santiye
    await _tahsis(seeded_db, items[0], bolum, "120")

    govde = await _gun(
        client,
        admin_headers,
        site.id,
        VARSAYILAN_TARIH,
        [
            _satir(items[0], "130", bolum, overrun_reason="  proje revizyonu "),
            _satir(items[1], "1", overrun_reason="   "),
        ],
        gonder=False,
    )

    s1 = _bul(govde, items[0], bolum)
    assert s1["overrun_reason"] == "proje revizyonu"
    assert Decimal(s1["remaining_quantity"]) == Decimal("-10")
    assert _bul(govde, items[1])["overrun_reason"] is None


async def test_tahsisi_kalkan_mevcut_bolumlu_satir_kalir_yenisi_422(
    client: AsyncClient, admin_headers, santiye, bolum, bolum2, seeded_db: AsyncSession
) -> None:
    """DEĞİŞTİRME gövdesi tam kümedir: tahsisi silinen tek kalem, aynı günün diğer
    satırlarının düzeltilmesini KİLİTLEMEMELİ (kapı DEĞİŞİME bağlı)."""
    site, _, items = santiye
    tahsis = await _tahsis(seeded_db, items[0], bolum, "120")
    kayit = await _gun(
        client,
        admin_headers,
        site.id,
        VARSAYILAN_TARIH,
        [_satir(items[0], "4", bolum)],
        gonder=False,
    )
    await seeded_db.delete(tahsis)
    await seeded_db.flush()

    ayni = await client.put(
        f"/diary/{kayit['id']}/lines",
        json={"lines": [_satir(items[0], "6", bolum), _satir(items[1], "1")]},
        headers=admin_headers,
    )
    yeni = await client.put(
        f"/diary/{kayit['id']}/lines",
        json={"lines": [_satir(items[0], "6", bolum), _satir(items[0], "1", bolum2)]},
        headers=admin_headers,
    )

    assert ayni.status_code == 200, ayni.text
    assert Decimal(_bul(ayni.json(), items[0], bolum)["planned_quantity"]) == Decimal("0")
    assert yeni.status_code == 422, yeni.text


async def test_mevcut_satirin_kimligi_korunur(
    client: AsyncClient, admin_headers, santiye, bolum, seeded_db: AsyncSession
) -> None:
    site, _, items = santiye
    await _tahsis(seeded_db, items[0], bolum, "120")
    ilk = await _gun(
        client,
        admin_headers,
        site.id,
        VARSAYILAN_TARIH,
        [_satir(items[0], "1", bolum), _satir(items[0], "2")],
        gonder=False,
    )

    ikinci = await client.put(
        f"/diary/{ilk['id']}/lines",
        json={"lines": [_satir(items[0], "9", bolum), _satir(items[0], "8")]},
        headers=admin_headers,
    )

    assert ikinci.status_code == 200, ikinci.text
    for section in (bolum, None):
        assert _bul(ikinci.json(), items[0], section)["id"] == _bul(ilk, items[0], section)["id"]


# --- DB tekillik (kısmi indeks çifti) ---


async def _entry(seeded_db: AsyncSession, site, user) -> SiteDiaryEntry:
    entry = SiteDiaryEntry(
        site_id=site.id,
        project_id=site.project_id,
        entry_date=VARSAYILAN_TARIH,
        created_by=user.id,
    )
    seeded_db.add(entry)
    await seeded_db.flush()
    return entry


def _db_satir(entry, item, section) -> SiteDiaryLine:
    return SiteDiaryLine(
        entry_id=entry.id,
        boq_item_id=item.id,
        section_id=None if section is None else section.id,
        code=item.code,
        description="x",
        unit="Ton",
        unit_price=Decimal("1"),
        quantity=Decimal("1"),
    )


async def test_DB_bolumlu_ve_bolumsuz_ayni_kalem_birlikte_var_olabilir(
    seeded_db: AsyncSession, santiye, bolum, admin_kullanicisi
) -> None:
    site, _, items = santiye
    entry = await _entry(seeded_db, site, admin_kullanicisi)

    seeded_db.add_all([_db_satir(entry, items[0], bolum), _db_satir(entry, items[0], None)])
    await seeded_db.flush()

    sayi = await seeded_db.execute(
        select(SiteDiaryLine.id).where(SiteDiaryLine.entry_id == entry.id)
    )
    assert len(sayi.all()) == 2


@pytest.mark.parametrize("bolumlu", [True, False])
async def test_DB_ayni_yaprak_iki_kez_IntegrityError(
    seeded_db: AsyncSession, santiye, bolum, admin_kullanicisi, bolumlu: bool
) -> None:
    site, _, items = santiye
    entry = await _entry(seeded_db, site, admin_kullanicisi)
    section = bolum if bolumlu else None
    seeded_db.add(_db_satir(entry, items[0], section))
    await seeded_db.flush()

    with pytest.raises(IntegrityError):
        async with seeded_db.begin_nested():
            seeded_db.add(_db_satir(entry, items[0], section))
            await seeded_db.flush()


# --- Kalem düzeyi toplamlar AYNEN (bölümleri toplar) ---


async def test_ozet_oneri_ve_boq_ilerlemesi_bolumleri_kalemde_toplar(
    client: AsyncClient,
    admin_headers,
    santiye,
    bolum,
    seeded_db: AsyncSession,
    sozlesme_kalemi_fabrikasi,
) -> None:
    site, proje, items = santiye
    await _tahsis(seeded_db, items[0], bolum, "120")
    kalem = await sozlesme_kalemi_fabrikasi(items[0], proje)
    await _gun(
        client,
        admin_headers,
        site.id,
        VARSAYILAN_TARIH,
        [_satir(items[0], "10", bolum), _satir(items[0], "5")],
        gonder=True,
    )

    ozet = await client.get(
        f"/sites/{site.id}/diary/summary", params={"year": 2026, "month": 7}, headers=admin_headers
    )
    oneri = await repository.employer_suggestion_rows(seeded_db, proje.id, year=2026, month=7)
    ilerleme = await realized_by_item(seeded_db, [items[0].id])

    assert ozet.status_code == 200, ozet.text
    (satir,) = [s for s in ozet.json()["items"] if s["boq_item_id"] == str(items[0].id)]
    assert Decimal(satir["quantity"]) == Decimal("15")
    assert Decimal(satir["amount"]) == Decimal("15") * items[0].unit_price
    assert oneri == [(kalem.id, site.id, Decimal("15.000"))]
    assert ilerleme == {items[0].id: Decimal("15.000")}


async def test_ozet_gunun_son_kumulatifine_esit_kalir(
    client: AsyncClient, admin_headers, santiye, bolum, seeded_db: AsyncSession
) -> None:
    """`summary` docstring'indeki değişmez: ayın son gönderilmiş kaydının
    `cumulative_quantity`si özet miktarına eşit — bölümlü satırlarla da."""
    site, _, items = santiye
    await _tahsis(seeded_db, items[0], bolum, "120")
    await _gun(
        client, admin_headers, site.id, date(2026, 7, 3), [_satir(items[0], "2")], gonder=True
    )
    son = await _gun(
        client,
        admin_headers,
        site.id,
        VARSAYILAN_TARIH,
        [_satir(items[0], "10", bolum), _satir(items[0], "5")],
        gonder=True,
    )

    ozet = await client.get(
        f"/sites/{site.id}/diary/summary", params={"year": 2026, "month": 7}, headers=admin_headers
    )

    (satir,) = [s for s in ozet.json()["items"] if s["boq_item_id"] == str(items[0].id)]
    assert Decimal(_bul(son, items[0], bolum)["cumulative_quantity"]) == Decimal(satir["quantity"])


# --- Bölüm silme (FK RESTRICT — geçici karar, CEO sorusu) ---


async def test_bolum_silme_miktar_satiri_varken_satirlar_birlikte_silinir_onizlemede_gorunur(
    client: AsyncClient, admin_headers, santiye, bolum, seeded_db: AsyncSession
) -> None:
    """SIL-B1 (PLN-B2.10 korkuluğu Sistem Yöneticisi için KALKTI): bölüme yazılmış günlük miktar
    satırları (`site_diary_lines.section_id` RESTRICT) önizlemede `restrict` olarak görünür ve
    bölümle BİRLİKTE silinir. Bölümsüz satır (üçüncü) ve günlük başlıkları KALIR."""
    site, _, items = santiye
    await _tahsis(seeded_db, items[0], bolum, "120")
    await _tahsis(seeded_db, items[1], bolum, "100")
    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 7, 3),
        [_satir(items[0], "2", bolum)],
        gonder=True,
    )
    await _gun(
        client,
        admin_headers,
        site.id,
        VARSAYILAN_TARIH,
        [_satir(items[0], "10", bolum), _satir(items[1], "1", bolum), _satir(items[0], "5")],
        gonder=False,
    )

    onizleme = (await onizle(client, admin_headers, "section", bolum.id)).json()
    satirlar = next(g for g in onizleme["groups"] if g["table"] == "site_diary_lines")
    yanit = await sil_aile(client, admin_headers, "section", bolum.id)

    assert (satirlar["count"], satirlar["relation"]) == (3, "restrict")
    assert yanit.status_code == 204, yanit.text
    kalan = await seeded_db.execute(
        select(SiteDiaryLine.id).where(SiteDiaryLine.section_id == bolum.id)
    )
    assert kalan.all() == []
    bolumsuz = await seeded_db.execute(
        select(SiteDiaryLine.id).where(SiteDiaryLine.section_id.is_(None))
    )
    assert len(bolumsuz.all()) == 1  # bölümsüz satır dokunulmadı


async def test_bolum_silme_miktar_satiri_yoksa_bugunku_gibi_kosulsuz(
    client: AsyncClient, admin_headers, santiye, bolum, seeded_db: AsyncSession
) -> None:
    site, _, items = santiye
    await _tahsis(seeded_db, items[0], bolum, "120")
    await _gun(
        client, admin_headers, site.id, VARSAYILAN_TARIH, [_satir(items[0], "5")], gonder=True
    )

    yanit = await sil_aile(client, admin_headers, "section", bolum.id)

    assert yanit.status_code == 204, yanit.text
    entry = (
        await seeded_db.execute(select(SiteDiaryEntry).where(SiteDiaryEntry.site_id == site.id))
    ).scalar_one()
    assert entry.status is DiaryStatus.submitted


async def test_bolum_silme_yalniz_BASLIK_etiketi_varken_bugunku_gibi_silinir(
    client: AsyncClient, admin_headers, santiye, bolum, seeded_db: AsyncSession
) -> None:
    """Başlık etiketi (`site_diary_entries.section_id`, SET NULL) bilgi alanıdır —
    korkuluk YALNIZ satır bölümünü sayar."""
    site, _, _ = santiye
    kayit = await client.post(
        f"/sites/{site.id}/diary",
        json={"entry_date": VARSAYILAN_TARIH.isoformat(), "section_id": str(bolum.id)},
        headers=admin_headers,
    )
    assert kayit.status_code == 201, kayit.text

    yanit = await sil_aile(client, admin_headers, "section", bolum.id)

    assert yanit.status_code == 204, yanit.text

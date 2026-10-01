# ruff: noqa: F811
"""GKS-B1 — `POST /sites/{id}/diary` + isteğe bağlı `lines[]`.

Semantik (ölçüldü + karar): iskelet (kural A, başlık bölümü süzer) KURULUR, gövde AYNI işlemde
iskeletle BİRLEŞİR — gövde miktarı iskelet satırının üzerine yazar, iskelette olmayan GEÇERLİ
satırı ekler, hiçbir iskelet satırını SİLMEZ (`apply_lines`in DEĞİŞTİRME semantiği DEĞİL).
Doğrulamalar `PUT …/lines` ile aynı `_resolve`tur ve günlük yazılmadan ÖNCE koşar: bozuk
gövde ⇒ günlük de, satır da YOK (kısmi yazma yok).
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit import messages
from app.modules.site_diary import guards
from app.modules.site_diary.models import SiteDiaryEntry, SiteDiaryLine
from tests._disiplin_dunyasi import Dunya
from tests.discipline_scope._b2_golden_araclari import audit_kimlikleri, yeni_audit
from tests.discipline_scope.conftest import civil_yazar, dunya  # noqa: F401
from tests.site_diary._gks_b1 import GUN, anahtarlar, beklenen, olustur, satir
from tests.site_diary.conftest import KARISIK_BOLUMSUZ, KARISIK_S1, KarisikSantiye

pytestmark = pytest.mark.asyncio

YAZANLAR = True


async def _kayit_sayisi(session: AsyncSession, site_id: uuid.UUID) -> tuple[int, int]:
    """(günlük sayısı, satır sayısı) — YANIT değil ham tablo."""
    gunluk = await session.scalar(
        select(func.count()).select_from(SiteDiaryEntry).where(SiteDiaryEntry.site_id == site_id)
    )
    satirlar = await session.scalar(
        select(func.count())
        .select_from(SiteDiaryLine)
        .join(SiteDiaryEntry, SiteDiaryEntry.id == SiteDiaryLine.entry_id)
        .where(SiteDiaryEntry.site_id == site_id)
    )
    return gunluk or 0, satirlar or 0


def _bul(govde: dict, kod: str, bolum: str | None) -> dict:
    return next(s for s in govde["lines"] if s["code"] == kod and s["section_name"] == bolum)


# --- Gövdesiz POST: geri uyum (artık kural A ile) ---


@pytest.mark.parametrize("ekstra", [{}, {"lines": None}, {"lines": []}])
async def test_govdesiz_post_iskelet_kural_A_ve_sifir_miktar(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye, ekstra
) -> None:
    ks = karisik_santiye
    yanit = await olustur(client, admin_headers, ks.site.id, **ekstra)
    assert yanit.status_code == 201, yanit.text
    assert anahtarlar(yanit.json()["lines"]) == beklenen(KARISIK_BOLUMSUZ)
    assert all(Decimal(s["quantity"]) == 0 for s in yanit.json()["lines"])
    assert Decimal(yanit.json()["lines_total"]) == 0


async def test_baslik_bolumu_artik_iskeleti_suzer(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    """DAVRANIŞ DEĞİŞİKLİĞİ: bugüne dek başlık bölümü yalnız ETİKETTİ (iskelet TÜM kalemler,
    Bölümsüz); artık o bölüme tahsisli kalemlerle sınırlar (planlı = pay)."""
    ks = karisik_santiye
    yanit = await olustur(client, admin_headers, ks.site.id, bolum=ks.s1)
    assert yanit.status_code == 201, yanit.text
    assert anahtarlar(yanit.json()["lines"]) == beklenen(KARISIK_S1)
    assert yanit.json()["section_id"] == str(ks.s1.id)


async def test_baska_santiyenin_baslik_bolumu_422_ve_kayit_yok(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye, seeded_db: AsyncSession
) -> None:
    ks = karisik_santiye
    yanit = await client.post(
        f"/sites/{ks.site.id}/diary",
        json={"entry_date": GUN.isoformat(), "section_id": str(uuid.uuid4())},
        headers=admin_headers,
    )
    assert yanit.status_code == 422
    assert await _kayit_sayisi(seeded_db, ks.site.id) == (0, 0)


# --- Birleştirme ---


async def test_govde_iskelet_satirinin_uzerine_yazar_hicbir_iskelet_satiri_silinmez(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    """Frontend yalnız DOLDURDUĞU satırı gönderse bile iskelet boşalmaz (veri kaybı yok)."""
    ks = karisik_santiye
    yanit = await olustur(
        client,
        admin_headers,
        ks.site.id,
        lines=[satir(ks, "03", "12.5", "S1"), satir(ks, "01", "4")],
    )

    assert yanit.status_code == 201, yanit.text
    assert anahtarlar(yanit.json()["lines"]) == beklenen(KARISIK_BOLUMSUZ)  # kümede kayıp yok
    assert Decimal(_bul(yanit.json(), "03", "S1")["quantity"]) == Decimal("12.5")
    assert Decimal(_bul(yanit.json(), "01", None)["quantity"]) == 4
    kalan = [
        s
        for s in yanit.json()["lines"]
        if (s["code"], s["section_name"]) not in {("03", "S1"), ("01", None)}
    ]
    assert kalan and all(Decimal(s["quantity"]) == 0 for s in kalan)
    # ₺ ve kümülatifler gövdeden sonra hesaplanır (detay ucuyla aynı yol)
    assert Decimal(_bul(yanit.json(), "03", "S1")["line_amount"]) == Decimal("375.00")
    assert Decimal(_bul(yanit.json(), "03", "S1")["remaining_quantity"]) == Decimal("57.5")
    assert Decimal(yanit.json()["lines_total"]) == Decimal("375.00") + Decimal("40.00")


async def test_govde_iskelette_olmayan_gecerli_satiri_EKLER(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    """Kısmi tahsisli 02'nin S1 satırı ve tam tahsisli 03'ün Bölümsüz'ü iskelette YOK;
    ikisi de geçerli (tahsis var / Bölümsüz her zaman yazılabilir) → eklenir."""
    ks = karisik_santiye
    yanit = await olustur(
        client,
        admin_headers,
        ks.site.id,
        lines=[satir(ks, "02", "3", "S1"), satir(ks, "03", "1", None)],
    )

    assert yanit.status_code == 201, yanit.text
    assert len(yanit.json()["lines"]) == len(KARISIK_BOLUMSUZ) + 2
    assert Decimal(_bul(yanit.json(), "02", "S1")["quantity"]) == 3
    assert Decimal(_bul(yanit.json(), "03", None)["quantity"]) == 1
    assert Decimal(_bul(yanit.json(), "03", None)["planned_quantity"]) == 0  # tam tahsisli → aşım


async def test_baslik_bolumu_secili_iken_govde_baska_bolumun_gecerli_satirini_ekleyebilir(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    ks = karisik_santiye
    yanit = await olustur(
        client,
        admin_headers,
        ks.site.id,
        bolum=ks.s1,
        lines=[satir(ks, "04", "2", "S1"), satir(ks, "05", "5", "S2")],
    )
    assert yanit.status_code == 201, yanit.text
    assert len(yanit.json()["lines"]) == len(KARISIK_S1) + 1
    assert Decimal(_bul(yanit.json(), "04", "S1")["quantity"]) == 2
    assert Decimal(_bul(yanit.json(), "05", "S2")["quantity"]) == 5


async def test_fiyat_snapshot_sunucuda_istemciden_gelen_fiyat_422(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye, seeded_db: AsyncSession
) -> None:
    ks = karisik_santiye
    kotu = await olustur(
        client,
        admin_headers,
        ks.site.id,
        lines=[{**satir(ks, "01", "1"), "unit_price": "1.00"}],
    )
    assert kotu.status_code == 422
    assert await _kayit_sayisi(seeded_db, ks.site.id) == (0, 0)

    iyi = await olustur(client, admin_headers, ks.site.id, lines=[satir(ks, "01", "2")])
    satir_01 = _bul(iyi.json(), "01", None)
    assert Decimal(satir_01["unit_price"]) == ks.items["01"].unit_price
    assert satir_01["description"] == ks.items["01"].description


async def test_asim_gerekcesi_tasinir(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    ks = karisik_santiye
    yanit = await olustur(
        client,
        admin_headers,
        ks.site.id,
        lines=[satir(ks, "04", "60", "S1", overrun_reason="  Ek imalat  ")],
    )
    assert yanit.status_code == 201, yanit.text
    asim = _bul(yanit.json(), "04", "S1")
    assert asim["overrun_reason"] == "Ek imalat"
    assert Decimal(asim["remaining_quantity"]) == Decimal("-10")


# --- Doğrulamalar + ATOMİKLİK (günlük de satır da yazılmaz) ---


async def _yabanci_kalem(santiye_fabrikasi) -> uuid.UUID:
    _, _, items = await santiye_fabrikasi("GKS-Y")
    return items[0].id


async def test_dogrulama_hatalari_422_ve_kismi_yazma_YOK(
    client: AsyncClient,
    admin_headers,
    karisik_santiye: KarisikSantiye,
    santiye_fabrikasi,
    seeded_db: AsyncSession,
) -> None:
    """Geçerli ilk satır + bozuk ikinci satır: istek 422, tabloda günlük YOK, satır YOK.
    Poz sahipliği (başka şantiye / olmayan AYNI 422) · tahsissiz bölüm · yabancı bölüm."""
    from app.modules.sites.models import Section

    ks = karisik_santiye
    yabanci_kalem = await _yabanci_kalem(santiye_fabrikasi)
    diger_site, _, _ = await santiye_fabrikasi("GKS-Z")
    yabanci_bolum = Section(site_id=diger_site.id, code="Y", name="Y")
    seeded_db.add(yabanci_bolum)
    await seeded_db.flush()
    iyi = satir(ks, "01", "1")

    vakalar = {
        "baska-santiye-poz": (
            {"boq_item_id": str(yabanci_kalem), "quantity": "1", "section_id": None},
            guards.LINE_ITEM_MISMATCH,
        ),
        "olmayan-poz": (
            {"boq_item_id": str(uuid.uuid4()), "quantity": "1", "section_id": None},
            guards.LINE_ITEM_MISMATCH,
        ),
        "tahsissiz-bolum": (satir(ks, "01", "1", "S1"), guards.LINE_SECTION_NOT_ALLOCATED),
        "yabanci-bolum": (
            {**satir(ks, "01", "1"), "section_id": str(yabanci_bolum.id)},
            guards.LINE_SECTION_MISMATCH,
        ),
    }
    for ad, (kotu, mesaj) in vakalar.items():
        yanit = await olustur(client, admin_headers, ks.site.id, lines=[iyi, kotu])
        assert yanit.status_code == 422, f"{ad}: {yanit.text}"
        assert yanit.json()["detail"] == mesaj, ad
        assert await _kayit_sayisi(seeded_db, ks.site.id) == (0, 0), f"{ad}: kısmi yazma"

    # Pozitif kontrol: aynı iyi satır TEK başına yazılır (doğrulama gerçekten çalışıyor).
    assert (await olustur(client, admin_headers, ks.site.id, lines=[iyi])).status_code == 201


async def test_olmayan_ve_baska_santiyenin_pozu_AYNI_422_govde(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye, santiye_fabrikasi
) -> None:
    ks = karisik_santiye
    yabanci = await _yabanci_kalem(santiye_fabrikasi)
    a = await olustur(
        client, admin_headers, ks.site.id, lines=[{"boq_item_id": str(yabanci), "quantity": "1"}]
    )
    b = await olustur(
        client,
        admin_headers,
        ks.site.id,
        lines=[{"boq_item_id": str(uuid.uuid4()), "quantity": "1"}],
    )
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (422, a.json())


async def test_govde_icinde_ayni_kalem_bolum_iki_kez_409_ve_kayit_yok(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye, seeded_db: AsyncSession
) -> None:
    ks = karisik_santiye
    yanit = await olustur(
        client, admin_headers, ks.site.id, lines=[satir(ks, "01", "1"), satir(ks, "01", "2")]
    )
    assert yanit.status_code == 409
    assert yanit.json()["detail"] == guards.DUPLICATE_LINE
    assert await _kayit_sayisi(seeded_db, ks.site.id) == (0, 0)


async def test_negatif_miktar_ve_gecersiz_alan_422_ve_kayit_yok(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye, seeded_db: AsyncSession
) -> None:
    ks = karisik_santiye
    for kotu in (satir(ks, "01", "-1"), {**satir(ks, "01", "1"), "id": str(uuid.uuid4())}):
        yanit = await olustur(client, admin_headers, ks.site.id, lines=[kotu])
        assert yanit.status_code == 422, yanit.text
    assert await _kayit_sayisi(seeded_db, ks.site.id) == (0, 0)


# --- Gün kilidi · tarih tekilliği: gövde doğrulamasından ÖNCE ---


async def test_gun_kilitliyse_409_ve_bozuk_govde_bile_kilit_mesajini_alir(
    client: AsyncClient,
    admin_headers,
    karisik_santiye: KarisikSantiye,
    port,
    seeded_db: AsyncSession,
) -> None:
    ks = karisik_santiye
    port.kilitle(ks.site.id, GUN)
    bozuk = {"boq_item_id": str(uuid.uuid4()), "quantity": "1"}

    yanit = await olustur(client, admin_headers, ks.site.id, lines=[bozuk])

    assert yanit.status_code == 409, yanit.text  # 422 DEĞİL: kilit satır doğrulamasından önce
    assert await _kayit_sayisi(seeded_db, ks.site.id) == (0, 0)


async def test_tarih_doluysa_409_ve_mevcut_gunluk_satirlari_degismez(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye, seeded_db: AsyncSession
) -> None:
    ks = karisik_santiye
    ilk = await olustur(client, admin_headers, ks.site.id, lines=[satir(ks, "01", "7")])
    assert ilk.status_code == 201
    sayim = await _kayit_sayisi(seeded_db, ks.site.id)

    ikinci = await olustur(client, admin_headers, ks.site.id, lines=[satir(ks, "01", "99")])

    assert ikinci.status_code == 409
    assert ikinci.json()["detail"] == guards.ENTRY_DATE_TAKEN
    assert await _kayit_sayisi(seeded_db, ks.site.id) == sayim
    detay = await client.get(f"/diary/{ilk.json()['id']}", headers=admin_headers)
    assert Decimal(_bul(detay.json(), "01", None)["quantity"]) == 7


async def test_yetkisiz_rol_403_ve_kayit_yok(
    client: AsyncClient, pm_headers, karisik_santiye: KarisikSantiye, seeded_db: AsyncSession
) -> None:
    ks = karisik_santiye
    yanit = await olustur(client, pm_headers, ks.site.id, lines=[satir(ks, "01", "1")])
    assert yanit.status_code == 403
    assert await _kayit_sayisi(seeded_db, ks.site.id) == (0, 0)


# --- Denetim: mevcut mesaj yeterli (poz sayısı = yazılan satır sayısı) ---


async def test_denetim_mevcut_mesaj_satir_sayisini_tasir(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye, seeded_db: AsyncSession
) -> None:
    ks = karisik_santiye
    once = await audit_kimlikleri(seeded_db)
    yanit = await olustur(client, admin_headers, ks.site.id, lines=[satir(ks, "02", "1", "S1")])
    assert yanit.status_code == 201, yanit.text
    (kayit,) = await yeni_audit(seeded_db, once)
    assert kayit["detail"] == messages.site_diary_entry_created(
        ks.project.name, ks.site.name, GUN, len(KARISIK_BOLUMSUZ) + 1
    )


# --- DSC: kısıtlı yol ---


async def test_kisitli_kullanici_gorunur_kalemi_yazar_gizli_kalem_422_ve_kayit_yok(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar
) -> None:
    """civil: yalnız I1 görür. İskelet TÜM kalemler için açılır (S5), gövde yalnız görünürü
    değiştirir; gizli kalemi (I2) gövdeye koymak, olmayan kalemle AYNI 422'dir."""
    site = dunya.santiye
    gun = date(2026, 5, 20)
    gizli = await client.post(
        f"/sites/{site.id}/diary",
        headers=civil_yazar,
        json={
            "entry_date": gun.isoformat(),
            "lines": [{"boq_item_id": str(dunya.i["i2"].id), "quantity": "1", "section_id": None}],
        },
    )
    olmayan = await client.post(
        f"/sites/{site.id}/diary",
        headers=civil_yazar,
        json={
            "entry_date": gun.isoformat(),
            "lines": [{"boq_item_id": str(uuid.uuid4()), "quantity": "1", "section_id": None}],
        },
    )
    assert gizli.status_code == 422 and gizli.json() == olmayan.json()
    ham = await seeded_db.scalar(
        text("SELECT count(*) FROM site_diary_entries WHERE entry_date = :d"), {"d": gun}
    )
    assert ham == 0

    iyi = await client.post(
        f"/sites/{site.id}/diary",
        headers=civil_yazar,
        json={
            "entry_date": gun.isoformat(),
            "lines": [{"boq_item_id": str(dunya.i["i1"].id), "quantity": "6", "section_id": None}],
        },
    )
    assert iyi.status_code == 201, iyi.text
    assert [(s["code"], Decimal(s["quantity"])) for s in iyi.json()["lines"]] == [
        ("01.001", Decimal(6))
    ]
    satirlar = await seeded_db.execute(
        text("SELECT boq_item_id, quantity FROM site_diary_lines WHERE entry_id = :e"),
        {"e": uuid.UUID(iyi.json()["id"])},
    )
    tablo = {kalem: miktar for kalem, miktar in satirlar.all()}
    assert len(tablo) == 3  # gizli iskelet satırları da yazıldı (S5), miktarı 0
    assert tablo[dunya.i["i1"].id] == Decimal(6)
    assert tablo[dunya.i["i2"].id] == 0 and tablo[dunya.i["i3"].id] == 0

"""T2 — şantiye günlüğü çekirdek CRUD (spec §2, §3; plan T2).

Kapsam: oluşturma (satır iskeleti BOQ pozlarından OTOMATİK), liste + ay filtresi,
detay (satırlar + işçi kırılımı iç içe), PATCH (yalnız draft), DELETE (draft +
`can_delete`), denetim günlüğü.

Kapsam DIŞI (T3/T4/T5): `PUT …/lines`, işçi kırılımı YAZMA semantiği,
`submit`/`reopen`, `summary`, `diary-suggestion`.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditLog
from app.modules.site_diary import guards
from app.modules.site_diary.models import (
    DiaryStatus,
    SiteDiaryEntry,
    SiteDiaryWorkerCount,
    WorkerSource,
)
from tests.site_diary.conftest import VARSAYILAN_TARIH

pytestmark = pytest.mark.asyncio


async def _olustur(client: AsyncClient, headers: dict[str, str], site_id, **govde):
    govde.setdefault("entry_date", VARSAYILAN_TARIH.isoformat())
    return await client.post(f"/sites/{site_id}/diary", json=govde, headers=headers)


# --- Oluşturma: satır iskeleti BOQ pozlarından ---


async def test_olusturma_satir_iskeletini_boq_pozlarindan_uretir(
    client: AsyncClient, admin_headers: dict[str, str], santiye
) -> None:
    site, _, items = santiye
    yanit = await _olustur(client, admin_headers, site.id)
    assert yanit.status_code == 201, yanit.text
    govde = yanit.json()

    assert govde["status"] == "draft"
    assert govde["entry_date"] == VARSAYILAN_TARIH.isoformat()
    assert len(govde["lines"]) == len(items)

    beklenen = sorted(items, key=lambda item: item.code)
    for satir, kalem in zip(govde["lines"], beklenen, strict=True):
        assert satir["boq_item_id"] == str(kalem.id)
        assert satir["code"] == kalem.code
        assert satir["description"] == kalem.description
        assert satir["unit"] == kalem.unit
        assert Decimal(satir["unit_price"]) == kalem.unit_price
        # Miktar 0 baslar: o gun dokunulmayan poz sifir kalir (GK228).
        assert Decimal(satir["quantity"]) == Decimal("0")
        assert Decimal(satir["line_amount"]) == Decimal("0.00")


async def test_olusturma_bos_govdeyle_taslak_acar(
    client: AsyncClient, admin_headers: dict[str, str], santiye
) -> None:
    """Hava/sıcaklık/açıklama NULLABLE'dır — zorunluluk `submit` katmanındadır (T4)."""
    site, _, _ = santiye
    govde = (await _olustur(client, admin_headers, site.id)).json()
    assert govde["weather"] is None
    assert govde["temp_max_c"] is None and "temperature_c" not in govde  # CLEAN-B1 Faz 1
    assert govde["work_done"] is None
    assert govde["safety_meeting_held"] is False
    assert govde["has_incident"] is False
    assert govde["worker_counts"] == []
    assert govde["worker_total"] == 0


async def test_olusturma_bolum_ve_isg_alanlarini_yazar(
    client: AsyncClient, admin_headers: dict[str, str], santiye, bolum
) -> None:
    site, _, _ = santiye
    yanit = await _olustur(
        client,
        admin_headers,
        site.id,
        section_id=str(bolum.id),
        weather="rainy",
        temp_min_c="12",
        temp_max_c="18.5",
        work_done="Kalıp söküm",
        chief_note="Beton dökümü ertelendi",
        safety_meeting_held=True,
        ppe_checked=True,
        has_incident=True,
        incident_note="Küçük parmak sıyrığı",
    )
    assert yanit.status_code == 201, yanit.text
    govde = yanit.json()
    assert govde["section_id"] == str(bolum.id)
    assert govde["weather"] == "rainy"
    assert Decimal(govde["temp_max_c"]) == Decimal("18.5")
    assert govde["work_done"] == "Kalıp söküm"
    assert govde["chief_note"] == "Beton dökümü ertelendi"
    assert govde["safety_meeting_held"] is True
    assert govde["ppe_checked"] is True
    assert govde["has_incident"] is True
    assert govde["incident_note"] == "Küçük parmak sıyrığı"


async def test_baska_santiyenin_bolumu_422(
    client: AsyncClient, admin_headers: dict[str, str], santiye, santiye_fabrikasi, seeded_db
) -> None:
    """Bölüm bilgi alanıdır ama SAHİPSİZ olamaz — başka şantiyenin bölümü 422."""
    from app.modules.sites.models import Section

    site, _, _ = santiye
    yabanci_site, _, _ = await santiye_fabrikasi("SD-Y")
    yabanci = Section(site_id=yabanci_site.id, code="B-9", name="Yabancı Blok")
    seeded_db.add(yabanci)
    await seeded_db.flush()

    yanit = await _olustur(client, admin_headers, site.id, section_id=str(yabanci.id))
    assert yanit.status_code == 422, yanit.text
    assert yanit.json()["detail"] == guards.SECTION_MISMATCH


async def test_ayni_gune_ikinci_kayit_409(
    client: AsyncClient, admin_headers: dict[str, str], santiye
) -> None:
    """UQ (site_id, entry_date) — 500 DEĞİL, NET 409 (plan T2)."""
    site, _, _ = santiye
    assert (await _olustur(client, admin_headers, site.id)).status_code == 201
    yanit = await _olustur(client, admin_headers, site.id)
    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == guards.ENTRY_DATE_TAKEN


async def test_farkli_santiyede_ayni_gun_serbest(
    client: AsyncClient, admin_headers: dict[str, str], santiye, santiye_fabrikasi
) -> None:
    site, proje, _ = santiye
    ikinci, _, _ = await santiye_fabrikasi("SD-B", project=proje)
    assert (await _olustur(client, admin_headers, site.id)).status_code == 201
    assert (await _olustur(client, admin_headers, ikinci.id)).status_code == 201


async def test_pozsuz_santiyede_satirsiz_taslak_acilir(
    client: AsyncClient, admin_headers: dict[str, str], santiye_fabrikasi
) -> None:
    """BOQ henüz girilmemiş şantiyede günlük AÇILABİLİR — satır listesi boştur."""
    site, _, _ = await santiye_fabrikasi("SD-BOS", item_specs=[])
    yanit = await _olustur(client, admin_headers, site.id)
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["lines"] == []


# --- Liste ("Son Kayıtlar" alanları + ay filtresi) ---


async def test_liste_turev_alanlari_tasir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    santiye,
    gunluk_fabrikasi,
    admin_kullanicisi,
    seeded_db: AsyncSession,
) -> None:
    """Durum + işçi toplamı + satır ₺ toplamı TÜREVDİR (kolon yok, spec §2)."""
    site, _, _ = santiye
    entry = await gunluk_fabrikasi(
        site,
        admin_kullanicisi,
        lines=[
            ("01.001", Decimal("12.500"), Decimal("21500.00")),
            ("02.001", Decimal("3.000"), Decimal("1850.00")),
        ],
    )
    seeded_db.add_all(
        [
            SiteDiaryWorkerCount(
                entry_id=entry.id, trade="Kalıpçı", source=WorkerSource.company, count=8
            ),
            SiteDiaryWorkerCount(
                entry_id=entry.id, trade="Demirci", source=WorkerSource.subcontractor, count=5
            ),
        ]
    )
    await seeded_db.flush()

    yanit = await client.get(f"/sites/{site.id}/diary", headers=admin_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["total"] == 1
    kalem = govde["items"][0]
    assert kalem["status"] == "draft"
    assert kalem["worker_total"] == 13
    # 12.5 × 21500 = 268750.00 · 3 × 1850 = 5550.00
    assert Decimal(kalem["lines_total"]) == Decimal("274300.00")


async def test_liste_ay_filtresi(
    client: AsyncClient,
    admin_headers: dict[str, str],
    santiye,
    gunluk_fabrikasi,
    admin_kullanicisi,
) -> None:
    site, _, _ = santiye
    await gunluk_fabrikasi(site, admin_kullanicisi, entry_date=date(2026, 7, 3))
    await gunluk_fabrikasi(site, admin_kullanicisi, entry_date=date(2026, 7, 28))
    await gunluk_fabrikasi(site, admin_kullanicisi, entry_date=date(2026, 8, 1))

    hepsi = await client.get(f"/sites/{site.id}/diary", headers=admin_headers)
    assert hepsi.json()["total"] == 3

    temmuz = await client.get(
        f"/sites/{site.id}/diary", params={"year": 2026, "month": 7}, headers=admin_headers
    )
    assert temmuz.status_code == 200, temmuz.text
    assert temmuz.json()["total"] == 2
    assert {k["entry_date"] for k in temmuz.json()["items"]} == {"2026-07-03", "2026-07-28"}

    yil = await client.get(f"/sites/{site.id}/diary", params={"year": 2026}, headers=admin_headers)
    assert yil.json()["total"] == 3

    bos = await client.get(
        f"/sites/{site.id}/diary", params={"year": 2025, "month": 7}, headers=admin_headers
    )
    assert bos.json()["total"] == 0


async def test_liste_ay_yilsiz_verilemez(
    client: AsyncClient, admin_headers: dict[str, str], santiye
) -> None:
    site, _, _ = santiye
    yanit = await client.get(f"/sites/{site.id}/diary", params={"month": 7}, headers=admin_headers)
    assert yanit.status_code == 422, yanit.text
    assert yanit.json()["detail"] == guards.YEAR_REQUIRED_FOR_MONTH


async def test_liste_tarihe_gore_azalan(
    client: AsyncClient,
    admin_headers: dict[str, str],
    santiye,
    gunluk_fabrikasi,
    admin_kullanicisi,
) -> None:
    """ "Son Kayıtlar" en yeniden eskiye okunur."""
    site, _, _ = santiye
    for gun in (3, 28, 15):
        await gunluk_fabrikasi(site, admin_kullanicisi, entry_date=date(2026, 7, gun))
    govde = (await client.get(f"/sites/{site.id}/diary", headers=admin_headers)).json()
    assert [k["entry_date"] for k in govde["items"]] == [
        "2026-07-28",
        "2026-07-15",
        "2026-07-03",
    ]


async def test_liste_yalniz_kendi_santiyesini_gosterir(
    client: AsyncClient,
    admin_headers: dict[str, str],
    santiye,
    santiye_fabrikasi,
    gunluk_fabrikasi,
    admin_kullanicisi,
) -> None:
    site, proje, _ = santiye
    komsu, _, _ = await santiye_fabrikasi("SD-K", project=proje)
    await gunluk_fabrikasi(site, admin_kullanicisi)
    await gunluk_fabrikasi(komsu, admin_kullanicisi)

    govde = (await client.get(f"/sites/{site.id}/diary", headers=admin_headers)).json()
    assert govde["total"] == 1
    assert govde["items"][0]["site_id"] == str(site.id)


# --- Detay ---


async def test_detay_satirlari_ve_isci_kirilimini_ic_ice_doner(
    client: AsyncClient,
    admin_headers: dict[str, str],
    santiye,
    gunluk_fabrikasi,
    admin_kullanicisi,
    seeded_db: AsyncSession,
) -> None:
    site, _, _ = santiye
    entry = await gunluk_fabrikasi(
        site, admin_kullanicisi, lines=[("01.001", Decimal("2.000"), Decimal("100.00"))]
    )
    seeded_db.add(
        SiteDiaryWorkerCount(
            entry_id=entry.id, trade="Kalıpçı", source=WorkerSource.general, count=4
        )
    )
    await seeded_db.flush()

    yanit = await client.get(f"/diary/{entry.id}", headers=admin_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["id"] == str(entry.id)
    assert Decimal(govde["lines"][0]["line_amount"]) == Decimal("200.00")
    assert Decimal(govde["lines_total"]) == Decimal("200.00")
    assert govde["worker_counts"] == [
        {
            "id": govde["worker_counts"][0]["id"],
            "trade": "Kalıpçı",
            "source": "general",
            "count": 4,
            # PLN-B2.1 (B2-5) — YALNIZ EKLEME: firmasız satırda ikisi de boş.
            "subcontractor_id": None,
            "hours": None,
            # DET-1.B — YALNIZ EKLEME: firmasız satırda firma adı da boş.
            "subcontractor_name": None,
        }
    ]
    assert govde["worker_total"] == 4


async def test_olmayan_gunluk_404(client: AsyncClient, admin_headers: dict[str, str]) -> None:
    yanit = await client.get(f"/diary/{uuid.uuid4()}", headers=admin_headers)
    assert yanit.status_code == 404, yanit.text
    assert yanit.json()["detail"] == guards.ENTRY_MISSING


# --- PATCH (yalnız draft) ---


async def test_patch_taslagi_gunceller(
    client: AsyncClient, admin_headers: dict[str, str], santiye
) -> None:
    site, _, _ = santiye
    entry_id = (await _olustur(client, admin_headers, site.id)).json()["id"]
    yanit = await client.patch(
        f"/diary/{entry_id}",
        json={"weather": "cloudy", "work_done": "Demir bağlama", "safety_meeting_held": True},
        headers=admin_headers,
    )
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["weather"] == "cloudy"
    assert govde["work_done"] == "Demir bağlama"
    assert govde["safety_meeting_held"] is True


async def test_patch_tarihi_degistirebilir_ama_dolu_gune_409(
    client: AsyncClient, admin_headers: dict[str, str], santiye
) -> None:
    site, _, _ = santiye
    ilk = (await _olustur(client, admin_headers, site.id)).json()["id"]
    ikinci = (await _olustur(client, admin_headers, site.id, entry_date="2026-07-16")).json()["id"]

    tasi = await client.patch(
        f"/diary/{ikinci}", json={"entry_date": "2026-07-17"}, headers=admin_headers
    )
    assert tasi.status_code == 200, tasi.text
    assert tasi.json()["entry_date"] == "2026-07-17"

    catis = await client.patch(
        f"/diary/{ikinci}",
        json={"entry_date": VARSAYILAN_TARIH.isoformat()},
        headers=admin_headers,
    )
    assert catis.status_code == 409, catis.text
    assert catis.json()["detail"] == guards.ENTRY_DATE_TAKEN
    assert ilk != ikinci


async def test_patch_submitted_kayda_yasak(
    client: AsyncClient,
    admin_headers: dict[str, str],
    santiye,
    gunluk_fabrikasi,
    admin_kullanicisi,
) -> None:
    """Gönderilmiş kayda YAZMA YASAK — durum geçişi T4'ün işidir."""
    site, _, _ = santiye
    entry = await gunluk_fabrikasi(site, admin_kullanicisi, status=DiaryStatus.submitted)
    yanit = await client.patch(
        f"/diary/{entry.id}", json={"work_done": "Sonradan ekleme"}, headers=admin_headers
    )
    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == guards.ENTRY_NOT_EDITABLE


async def test_patch_status_alanini_kabul_etmez(
    client: AsyncClient, admin_headers: dict[str, str], santiye
) -> None:
    """Durum yalnız geçiş uçlarıyla (T4) değişir — PATCH gövdesi kabul etmez."""
    site, _, _ = santiye
    entry_id = (await _olustur(client, admin_headers, site.id)).json()["id"]
    yanit = await client.patch(
        f"/diary/{entry_id}", json={"status": "submitted"}, headers=admin_headers
    )
    assert yanit.status_code == 422, yanit.text


# --- DELETE (yalnız Sistem Yöneticisi — SIL-B1, K4: istisna yok) ---


async def test_delete_kendi_taslagini_da_silemez_sistem_yoneticisi_siler(
    client: AsyncClient,
    sef_headers: dict[str, str],
    admin_headers: dict[str, str],
    santiye,
    seeded_db: AsyncSession,
) -> None:
    """Eski `can_delete` "kendi taslağını sahibi siler" istisnası KALDIRILDI."""
    site, _, _ = santiye
    entry_id = (await _olustur(client, sef_headers, site.id)).json()["id"]
    yasak = await client.delete(f"/diary/{entry_id}", headers=sef_headers)
    assert yasak.status_code == 403, yasak.text
    assert await seeded_db.get(SiteDiaryEntry, uuid.UUID(entry_id)) is not None
    yanit = await client.delete(f"/diary/{entry_id}", headers=admin_headers)
    assert yanit.status_code == 204, yanit.text
    assert await seeded_db.get(SiteDiaryEntry, uuid.UUID(entry_id)) is None


async def test_delete_baskasinin_taslagini_reddeder(
    client: AsyncClient, sef_headers: dict[str, str], saha_headers: dict[str, str], santiye
) -> None:
    """Başkasının taslağı da 403 (kapı: Sistem Yöneticisi)."""
    site, _, _ = santiye
    entry_id = (await _olustur(client, sef_headers, site.id)).json()["id"]
    yanit = await client.delete(f"/diary/{entry_id}", headers=saha_headers)
    assert yanit.status_code == 403, yanit.text
    assert yanit.json()["detail"] == "Bu işlemi yalnızca Sistem Yöneticisi yapabilir"


async def test_delete_submitted_kaydi_admin_dahil_reddeder(
    client: AsyncClient,
    admin_headers: dict[str, str],
    santiye,
    gunluk_fabrikasi,
    admin_kullanicisi,
) -> None:
    """Katman 1 `can_delete`ten ÖNCE koşar: gönderilmiş kayıt admine de silinmez."""
    site, _, _ = santiye
    entry = await gunluk_fabrikasi(site, admin_kullanicisi, status=DiaryStatus.submitted)
    yanit = await client.delete(f"/diary/{entry.id}", headers=admin_headers)
    assert yanit.status_code == 409, yanit.text
    assert yanit.json()["detail"] == guards.ENTRY_NOT_DELETABLE


async def test_delete_admin_baskasinin_taslagini_silebilir(
    client: AsyncClient, admin_headers: dict[str, str], sef_headers: dict[str, str], santiye
) -> None:
    site, _, _ = santiye
    entry_id = (await _olustur(client, sef_headers, site.id)).json()["id"]
    assert (await client.delete(f"/diary/{entry_id}", headers=admin_headers)).status_code == 204


# --- Denetim günlüğü (tam kapsam) ---


async def _audit_detaylari(session: AsyncSession) -> list[str]:
    rows = (await session.execute(select(AuditLog))).scalars().all()
    return [row.detail for row in rows]


async def test_audit_create_update_delete_yazilir(
    client: AsyncClient,
    sef_headers: dict[str, str],
    admin_headers: dict[str, str],
    santiye,
    seeded_db: AsyncSession,
) -> None:
    site, proje, _ = santiye
    entry_id = (await _olustur(client, sef_headers, site.id)).json()["id"]
    await client.patch(f"/diary/{entry_id}", json={"work_done": "x"}, headers=sef_headers)
    await client.delete(f"/diary/{entry_id}", headers=admin_headers)

    detaylar = await _audit_detaylari(seeded_db)
    etiket = f"{proje.name} · {site.name} · {VARSAYILAN_TARIH.isoformat()}"
    assert any(d.startswith("Günlük kayıt oluşturuldu:") and etiket in d for d in detaylar)
    assert any(d.startswith("Günlük kayıt güncellendi:") and etiket in d for d in detaylar)
    assert any(d.startswith("Günlük kayıt silindi:") and etiket in d for d in detaylar)


async def test_basarisiz_yazma_audit_birakmaz(
    client: AsyncClient, admin_headers: dict[str, str], santiye, seeded_db: AsyncSession
) -> None:
    site, _, _ = santiye
    assert (await _olustur(client, admin_headers, site.id)).status_code == 201
    assert (await _olustur(client, admin_headers, site.id)).status_code == 409
    detaylar = await _audit_detaylari(seeded_db)
    assert sum(1 for d in detaylar if d.startswith("Günlük kayıt oluşturuldu:")) == 1
